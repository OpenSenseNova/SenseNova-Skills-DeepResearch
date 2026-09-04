import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AgentMessageChunk,
    PermissionOption,
    TextContentBlock,
    ToolCallUpdate,
)

from deepresearch_cli.harness.hermes_acp import _RecordingAcpClient
from deepresearch_cli.harness.hermes_acp import (
    HermesAcpAttemptRuntime,
    HermesBackendFactory,
)
from deepresearch_cli.harness import AgentInvocation, HarnessError
from tests.search_test_utils import write_search_source


def _assert_search_mcp_launch(descriptor):
    """Accept the installed console entry point and source fallback forms."""
    if descriptor.args:
        assert descriptor.args == ["-m", "deepresearch_cli.search.mcp_server"]
    else:
        assert descriptor.command.endswith("deepresearch-codex-mcp")


def test_recording_client_collects_agent_text_and_events():
    async def exercise():
        client = _RecordingAcpClient()
        await client.session_update(
            "s1",
            AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text="hello"),
            ),
        )
        assert client.messages["s1"] == ["hello"]
        assert client.events["s1"][0]["sessionUpdate"] == "agent_message_chunk"

    asyncio.run(exercise())


def test_raw_observer_records_wire_update_once_before_dispatch():
    async def exercise():
        client = _RecordingAcpClient(raw_observer_enabled=True)
        raw_update = {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "wire text"},
        }
        client.observe_stream(
            StreamEvent(
                StreamDirection.INCOMING,
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": "s1", "update": raw_update},
                },
            )
        )
        # The later typed dispatcher callback becomes a no-op in raw mode and
        # therefore cannot duplicate the wire-order event.
        await client.session_update(
            "s1",
            AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text="wire text"),
            ),
        )
        assert client.messages["s1"] == ["wire text"]
        assert client.events["s1"] == [raw_update]

    asyncio.run(exercise())


def test_raw_observer_forwards_event_immediately_and_ignores_observer_failure():
    seen = []

    def observer(session_id, event):
        seen.append((session_id, event["toolCallId"]))
        raise RuntimeError("display failed")

    client = _RecordingAcpClient(
        raw_observer_enabled=True, event_observer=observer
    )
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tool-live",
        "kind": "fetch",
        "title": "web search: live event",
    }

    client.observe_stream(
        StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": "s-live", "update": update},
            },
        )
    )

    assert seen == [("s-live", "tool-live")]
    assert client.events["s-live"] == [update]


def test_progress_observer_cannot_mutate_recorded_event():
    def observer(session_id, event):
        del session_id
        event.clear()

    client = _RecordingAcpClient(
        raw_observer_enabled=True, event_observer=observer
    )
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tool-safe",
        "kind": "edit",
        "title": "edit: output.json",
        "content": [{"newText": "authoritative body"}],
    }

    client.observe_stream(
        StreamEvent(
            StreamDirection.INCOMING,
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": "s-safe", "update": update},
            },
        )
    )

    assert client.events["s-safe"] == [update]
    assert client.events["s-safe"][0]["content"] == [
        {"newText": "authoritative body"}
    ]


def test_recording_client_rejects_even_read_permission_requests():
    async def exercise():
        client = _RecordingAcpClient()
        response = await client.request_permission(
            options=[
                PermissionOption(optionId="always", name="Always", kind="allow_always"),
                PermissionOption(optionId="once", name="Once", kind="allow_once"),
                PermissionOption(optionId="reject", name="Reject", kind="reject_once"),
            ],
            session_id="s1",
            tool_call=ToolCallUpdate(toolCallId="t1", kind="read"),
        )
        assert response.outcome.outcome == "selected"
        assert response.outcome.option_id == "reject"

    asyncio.run(exercise())


def test_recording_client_rejects_execute_permission():
    async def exercise():
        client = _RecordingAcpClient()
        response = await client.request_permission(
            options=[
                PermissionOption(optionId="allow", name="Allow", kind="allow_once"),
                PermissionOption(optionId="reject", name="Reject", kind="reject_once"),
            ],
            session_id="s1",
            tool_call=ToolCallUpdate(toolCallId="t1", kind="execute"),
        )
        assert response.outcome.outcome == "selected"
        assert response.outcome.option_id == "reject"

    asyncio.run(exercise())


def test_stderr_snapshot_waits_for_prompt_tail(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)
        harness._stderr_task = asyncio.current_task()
        harness._append_stderr("previous attempt\n")
        cursor = harness._stderr_end_offset

        async def append_tail():
            await asyncio.sleep(0.03)
            harness._append_stderr("tail log\n")

        pending = asyncio.create_task(append_tail())
        assert await harness._settled_stderr_text(cursor) == "tail log\n"
        assert harness._stderr_text_since(0) == "previous attempt\ntail log\n"
        await pending

    asyncio.run(exercise())


def test_hermes_profile_is_forwarded_as_process_argument(tmp_path):
    harness = HermesAcpAttemptRuntime(tmp_path, profile="research-team")
    assert harness._acp_args() == ("--profile", "research-team", "acp")
    assert harness._acp_args("--check") == (
        "--profile",
        "research-team",
        "acp",
        "--check",
    )


def test_relative_hermes_executable_is_resolved_before_spawn(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "hermes"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    harness = HermesAcpAttemptRuntime(tmp_path / "workspace", hermes_command="./bin/hermes")
    assert harness.hermes_command == str(executable.resolve())


def _invocation(tmp_path):
    return AgentInvocation(
        invocation_id="inv-test",
        run_id="run-test",
        node_instance_id="node-test",
        node_type="research",
        attempt=1,
        workspace=tmp_path,
        input_artifact_refs=[],
        resolved_input_artifacts=[],
        timeout_seconds=30,
        agent_context={"query": "test", "language": "en"},
        prompt="test",
    )


def test_backend_factory_reuses_config_but_creates_distinct_attempt_runtimes(
    tmp_path,
):
    factory = HermesBackendFactory(
        workspace=tmp_path,
        profile="research-team",
        search_mcp_enabled=True,
        search_dir=tmp_path / "search",
        search_provider_limit=7,
    )
    first_invocation = _invocation(tmp_path)
    second_invocation = replace(first_invocation, invocation_id="inv-second")

    first = factory.create(first_invocation)
    second = factory.create(second_invocation)

    assert first is not second
    assert first.profile == second.profile == "research-team"
    assert first.search_provider_limit == second.search_provider_limit == 7
    assert first.expected_invocation_id == "inv-test"
    assert second.expected_invocation_id == "inv-second"


def test_invoke_snapshots_wire_observer_before_typed_dispatch(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-wire")

            async def prompt(self, **kwargs):
                harness._client.observe_stream(
                    StreamEvent(
                        StreamDirection.INCOMING,
                        {
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {
                                "sessionId": "session-wire",
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "wire answer"},
                                },
                            },
                        },
                    )
                )
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process = SimpleNamespace(pid=4321)
        harness._process_instance_id = "hermes-process-wire"

        result = await harness.invoke(_invocation(tmp_path))

        assert result.status == "succeeded"
        assert result.response_text == "wire answer"
        assert result.events[0]["sessionUpdate"] == "agent_message_chunk"
        assert result.native_process_stderr_path is not None
        process_log = tmp_path / result.native_process_stderr_path
        assert process_log.read_text(encoding="utf-8") == ""
        # A chunk arriving after AgentExecutionResult is fixed is still kept in
        # the process-level diagnostics file.
        harness._append_stderr("late shutdown log\n")
        assert process_log.read_text(encoding="utf-8") == "late shutdown log\n"
        harness._process_stderr_file_limit_bytes = 96
        harness._append_stderr("x" * 200)
        bounded = process_log.read_bytes()
        assert len(bounded) <= 96
        assert b"process stderr truncated" in bounded
        harness._append_stderr("ignored after cap\n")
        assert process_log.read_bytes() == bounded

    asyncio.run(exercise())


def test_search_mcp_is_injected_and_verified_only_for_research(tmp_path):
    async def exercise():
        search_dir = tmp_path / "search"
        write_search_source(search_dir, "hackernews")
        attempt = tmp_path / "run" / "attempts" / "research" / "attempt-1"
        staging = attempt / "staging"
        staging.mkdir(parents=True)
        research_runtime = HermesAcpAttemptRuntime(
            tmp_path,
            search_mcp_enabled=True,
            search_dir=search_dir,
        )

        class Connection:
            def __init__(self, runtime):
                self.runtime = runtime
                self.sessions = []

            async def new_session(self, **kwargs):
                self.sessions.append(kwargs)
                if kwargs["mcp_servers"]:
                    server_name = kwargs["mcp_servers"][0].name
                    self.runtime._append_stderr(
                        f"MCP server '{server_name}' (stdio): registered "
                            "8 tool(s): "
                        + ", ".join(
                            f"mcp__{server_name}__{name}"
                            for name in (
                                "list_search_domains",
                                "start_domain_search",
                                "get_search_batch",
                                "list_search_sources",
                                "batch_search",
                                "search_results",
                                    "get_search_hit",
                                    "fetch_url",
                                )
                        )
                    )
                return SimpleNamespace(session_id=f"session-{len(self.sessions)}")

            async def prompt(self, **kwargs):
                session_id = kwargs["session_id"]
                assert kwargs["prompt"][0].text == "test"
                self.runtime._client.messages[session_id].append("done")
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        research_connection = Connection(research_runtime)
        research_runtime._started = True
        research_runtime._connection = research_connection
        research_runtime._process_instance_id = "hermes-process-search-mcp"

        research = await research_runtime.invoke(
            replace(_invocation(tmp_path), workspace=staging)
        )
        plan_runtime = HermesAcpAttemptRuntime(tmp_path)
        plan_connection = Connection(plan_runtime)
        plan_runtime._started = True
        plan_runtime._connection = plan_connection
        plan_runtime._process_instance_id = "hermes-process-plan"
        plan = await plan_runtime.invoke(
            replace(
                _invocation(tmp_path),
                invocation_id="inv-plan",
                node_type="plan",
                workspace=staging,
            )
        )

        assert research.status == plan.status == "succeeded"
        research_servers = research_connection.sessions[0]["mcp_servers"]
        assert len(research_servers) == 1
        descriptor = research_servers[0]
        _assert_search_mcp_launch(descriptor)
        environment = {item.name: item.value for item in descriptor.env}
        assert environment["DEEPRESEARCH_SEARCH_STORE_DIR"] == str(
            attempt / "search"
        )
        assert environment["DEEPRESEARCH_SEARCH_DIR"] == str(search_dir)
        assert environment["DEEPRESEARCH_SEARCH_PROVIDER_LIMIT"] == "20"
        assert plan_connection.sessions[0]["mcp_servers"] == []

    asyncio.run(exercise())


def test_camofox_fallback_is_injected_into_search_mcp_without_raw_browser_tools(tmp_path):
    search_dir = tmp_path / "search"
    write_search_source(search_dir, "general")
    runtime = HermesAcpAttemptRuntime(
        tmp_path,
        search_mcp_enabled=True,
        search_dir=search_dir,
        camofox_fallback_enabled=True,
        camofox_base_url="http://127.0.0.1:9377",
    )
    server_name, descriptor, lease = runtime._search_mcp_server(
        identity="inv-test",
        store_dir=tmp_path / "attempt" / "search",
    )
    try:
        environment = {item.name: item.value for item in descriptor.env}
        assert descriptor.name == server_name
        _assert_search_mcp_launch(descriptor)
        assert environment["DEEPRESEARCH_CAMOFOX_FALLBACK"] == "1"
        assert environment["DEEPRESEARCH_CAMOFOX_BASE_URL"] == "http://127.0.0.1:9377"
        assert environment["DEEPRESEARCH_FETCH_IDENTITY"] == "inv-test"
        assert all(item.name != "CAMOFOX_USER_ID" for item in descriptor.env)
    finally:
        lease.unlink(missing_ok=True)



def test_invoke_reports_tool_event_before_prompt_returns(tmp_path):
    async def exercise():
        progress_events = []

        class Reporter:
            def invocation_started(self, invocation):
                progress_events.append(("started", invocation.node_type))

            def acp_event(self, invocation, event):
                progress_events.append(
                    ("event", invocation.node_type, event["toolCallId"])
                )

            def invocation_finished(self, invocation, status):
                progress_events.append(("finished", status))

        harness = HermesAcpAttemptRuntime(tmp_path, progress_reporter=Reporter())

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-progress")

            async def prompt(self, **kwargs):
                harness._client.observe_stream(
                    StreamEvent(
                        StreamDirection.INCOMING,
                        {
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {
                                "sessionId": "session-progress",
                                "update": {
                                    "sessionUpdate": "tool_call",
                                    "toolCallId": "tool-progress",
                                    "kind": "fetch",
                                    "title": "web search: progress",
                                },
                            },
                        },
                    )
                )
                assert progress_events[-1] == (
                    "event",
                    "research",
                    "tool-progress",
                )
                harness._client.messages["session-progress"].append("done")
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-progress"

        result = await harness.invoke(_invocation(tmp_path))

        assert result.status == "succeeded"
        assert progress_events[0] == ("started", "research")
        assert progress_events[-1] == ("finished", "succeeded")

    asyncio.run(exercise())


def test_attempt_runtime_rejects_a_second_invocation(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)

        class Connection:
            next_session = 0
            modes = []

            async def new_session(self, **kwargs):
                self.next_session += 1
                return SimpleNamespace(session_id=f"session-{self.next_session}")

            async def set_session_mode(self, **kwargs):
                self.modes.append(kwargs)
                return SimpleNamespace()

            async def prompt(self, **kwargs):
                session_id = kwargs["session_id"]
                harness._client.messages[session_id].append("done")
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process = SimpleNamespace(pid=4321)
        harness._process_instance_id = "hermes-process-shared"
        run_root = tmp_path / "run-test"
        first_workspace = run_root / "attempts" / "research" / "staging"
        first_workspace.mkdir(parents=True)

        first = await harness.invoke(
            replace(
                _invocation(tmp_path),
                workspace=first_workspace,
                allow_workspace_edits=True,
            )
        )
        with pytest.raises(HarnessError, match="cannot serve multiple invocations"):
            await harness.invoke(
                replace(_invocation(tmp_path), invocation_id="inv-writer")
            )

        assert first.status == "succeeded"
        assert harness._connection.modes == [
            {"mode_id": "accept_edits", "session_id": "session-1"},
        ]
        process_log = run_root / str(first.native_process_stderr_path)
        assert process_log.is_file()

    asyncio.run(exercise())


def test_attempt_runtime_rejects_a_concurrent_second_invocation(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)
        prompt_entered = asyncio.Event()
        release_prompt = asyncio.Event()

        class Connection:
            def __init__(self):
                self.next_session = 0

            async def new_session(self, **kwargs):
                self.next_session += 1
                return SimpleNamespace(session_id=f"session-{self.next_session}")

            async def prompt(self, **kwargs):
                session_id = kwargs["session_id"]
                prompt_entered.set()
                await release_prompt.wait()
                harness._client.messages[session_id].append(
                    f"answer from {session_id}"
                )
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        connection = Connection()
        harness._started = True
        harness._connection = connection
        harness._process = SimpleNamespace(pid=4321)
        harness._process_instance_id = "hermes-process-concurrent"
        run_root = tmp_path / "run-concurrent"
        first_workspace = run_root / "attempts" / "research-d1" / "staging"
        second_workspace = run_root / "attempts" / "research-d2" / "staging"
        first_workspace.mkdir(parents=True)
        second_workspace.mkdir(parents=True)

        first_task = asyncio.create_task(
            harness.invoke(
                replace(
                    _invocation(tmp_path),
                    invocation_id="inv-d1",
                    workspace=first_workspace,
                )
            )
        )
        await prompt_entered.wait()
        with pytest.raises(HarnessError, match="cannot serve multiple invocations"):
            await harness.invoke(
                replace(
                    _invocation(tmp_path),
                    invocation_id="inv-d2",
                    workspace=second_workspace,
                )
            )
        release_prompt.set()
        first = await first_task

        assert first.native_session_id == "session-1"
        assert first.response_text == "answer from session-1"
        assert harness._active_sessions == {}
        assert harness._session_invocations == {}

    asyncio.run(exercise())


def test_task_cancellation_attempts_acp_cancel_before_reraising(tmp_path):
    async def exercise():
        prompt_started = asyncio.Event()
        cancelled_sessions = []

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-1")

            async def prompt(self, **kwargs):
                prompt_started.set()
                await asyncio.Event().wait()

            async def cancel(self, *, session_id):
                cancelled_sessions.append(session_id)

        harness = HermesAcpAttemptRuntime(tmp_path)
        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-cancel"
        task = asyncio.create_task(harness.invoke(_invocation(tmp_path)))
        await prompt_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled_sessions == ["session-1"]

    asyncio.run(exercise())


def test_timeout_attempts_acp_cancel_and_returns_cancelled(tmp_path):
    async def exercise():
        cancelled_sessions = []

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-timeout")

            async def prompt(self, **kwargs):
                await asyncio.Event().wait()

            async def cancel(self, *, session_id):
                cancelled_sessions.append(session_id)

        harness = HermesAcpAttemptRuntime(tmp_path)
        harness._started = True
        harness._connection = Connection()
        harness._process = SimpleNamespace(pid=4321)
        harness._process_instance_id = "hermes-process-test"
        invocation = replace(_invocation(tmp_path), timeout_seconds=0.01)

        result = await harness.invoke(invocation)

        assert result.status == "cancelled"
        assert result.stop_reason == "cancelled"
        assert result.native_process_id == 4321
        assert result.native_process_instance_id == "hermes-process-test"
        assert result.native_process_stderr_path is not None
        assert result.native_session_id == "session-timeout"
        assert result.failure_kind == "timeout"
        assert cancelled_sessions == ["session-timeout"]

    asyncio.run(exercise())


def test_session_creation_uses_node_budget_not_startup_timeout(tmp_path):
    async def exercise():
        class Connection:
            async def new_session(self, **kwargs):
                await asyncio.sleep(0.02)
                return SimpleNamespace(session_id="session-slow-create")

            async def prompt(self, **kwargs):
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness = HermesAcpAttemptRuntime(tmp_path, startup_timeout_seconds=0.001)
        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-slow-create"
        invocation = replace(_invocation(tmp_path), timeout_seconds=0.2)

        result = await harness.invoke(invocation)

        assert result.status == "succeeded"
        assert result.native_session_id == "session-slow-create"

    asyncio.run(exercise())


def test_session_creation_and_prompt_share_one_node_deadline(tmp_path, monkeypatch):
    async def exercise():
        cancelled_sessions = []
        wait_timeouts = []
        original_wait_for = asyncio.wait_for

        async def recording_wait_for(awaitable, timeout):
            wait_timeouts.append(timeout)
            return await original_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)

        class Connection:
            async def new_session(self, **kwargs):
                await asyncio.sleep(0.05)
                return SimpleNamespace(session_id="session-shared-deadline")

            async def prompt(self, **kwargs):
                await asyncio.Event().wait()

            async def cancel(self, *, session_id):
                cancelled_sessions.append(session_id)

        harness = HermesAcpAttemptRuntime(tmp_path)
        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-shared-deadline"
        invocation = replace(_invocation(tmp_path), timeout_seconds=0.2)

        result = await harness.invoke(invocation)

        assert result.status == "cancelled"
        assert result.native_session_id == "session-shared-deadline"
        assert cancelled_sessions == ["session-shared-deadline"]
        assert len(wait_timeouts) >= 2
        assert wait_timeouts[1] < wait_timeouts[0] - 0.03

    asyncio.run(exercise())


def test_disabled_node_timeout_does_not_wrap_agent_operations_in_wait_for(
    tmp_path, monkeypatch
):
    async def exercise():
        wait_for_calls = []
        original_wait_for = asyncio.wait_for

        async def recording_wait_for(awaitable, timeout):
            wait_for_calls.append(timeout)
            return await original_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-unbounded")

            async def set_session_mode(self, **kwargs):
                return SimpleNamespace()

            async def prompt(self, **kwargs):
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness = HermesAcpAttemptRuntime(tmp_path)
        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-unbounded"
        invocation = replace(
            _invocation(tmp_path),
            timeout_seconds=None,
            allow_workspace_edits=True,
        )

        result = await harness.invoke(invocation)

        assert result.status == "succeeded"
        assert result.native_session_id == "session-unbounded"
        assert wait_for_calls == []

    asyncio.run(exercise())


def test_session_creation_exhausting_node_budget_does_not_cancel_unknown_session(
    tmp_path,
):
    async def exercise():
        cancelled_sessions = []

        class Connection:
            async def new_session(self, **kwargs):
                await asyncio.Event().wait()

            async def cancel(self, *, session_id):
                cancelled_sessions.append(session_id)

        harness = HermesAcpAttemptRuntime(tmp_path, startup_timeout_seconds=0.001)
        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-create-timeout"
        invocation = replace(_invocation(tmp_path), timeout_seconds=0.01)

        result = await harness.invoke(invocation)

        assert result.status == "cancelled"
        assert result.native_session_id is None
        assert result.error == "Hermes invocation timed out after 0.01s"
        assert cancelled_sessions == []

    asyncio.run(exercise())


def test_non_end_turn_stop_reason_is_failed_and_preserved(tmp_path):
    async def exercise():
        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-2")

            async def prompt(self, **kwargs):
                return SimpleNamespace(stop_reason="max_tokens", usage=None)

        harness = HermesAcpAttemptRuntime(tmp_path)
        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-stop-reason"
        result = await harness.invoke(_invocation(tmp_path))
        assert result.status == "failed"
        assert result.stop_reason == "max_tokens"
        assert "max_tokens" in result.error

    asyncio.run(exercise())


def test_end_turn_with_matching_hermes_agent_error_is_failed(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-agent-error")

            async def prompt(self, **kwargs):
                harness._client.messages["session-agent-error"].append(
                    "Error: upstream HTTP 401"
                )
                harness._append_stderr(
                    "Agent error in session session-agent-error\nTraceback ...\n"
                )
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-agent-error"

        result = await harness.invoke(_invocation(tmp_path))

        assert result.status == "failed"
        assert result.stop_reason == "end_turn"
        assert result.response_text == "Error: upstream HTTP 401"
        assert "failed inside the ACP session" in result.error
        assert "Agent error in session session-agent-error" in result.stderr

    asyncio.run(exercise())


def test_end_turn_text_beginning_with_error_is_not_failed_without_log_signature(
    tmp_path,
):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-legitimate-error-heading")

            async def prompt(self, **kwargs):
                harness._client.messages[
                    "session-legitimate-error-heading"
                ].append("Error: terminology and measurement\n\nResearch discussion.")
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-legitimate-error-heading"

        result = await harness.invoke(_invocation(tmp_path))

        assert result.status == "succeeded"
        assert result.error is None

    asyncio.run(exercise())


def test_end_turn_provider_stream_failure_is_failed_and_preserved(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-provider-eof")

            async def prompt(self, **kwargs):
                harness._client.messages["session-provider-eof"].append(
                    "API call failed after 3 retries: unexpected EOF"
                )
                harness._append_stderr(
                    "API call failed after 3 retries. unexpected EOF\n"
                )
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-provider-eof"

        result = await harness.invoke(_invocation(tmp_path))

        assert result.status == "failed"
        assert result.stop_reason == "end_turn"
        assert result.error == "API call failed after 3 retries: unexpected EOF"
        assert "unexpected EOF" in result.stderr

    asyncio.run(exercise())


def test_end_turn_agent_error_waits_for_delayed_stderr_tail(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)
        harness._stderr_task = asyncio.current_task()

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-delayed-error")

            async def prompt(self, **kwargs):
                harness._client.messages["session-delayed-error"].append(
                    "Error: delayed provider failure"
                )

                async def append_tail():
                    await asyncio.sleep(0.03)
                    harness._append_stderr(
                        "Agent error in session session-delayed-error\n"
                    )

                asyncio.create_task(append_tail())
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-delayed-error"

        result = await harness.invoke(_invocation(tmp_path))

        assert result.status == "failed"
        assert "Agent error in session session-delayed-error" in result.stderr

    asyncio.run(exercise())


def test_end_turn_agent_error_log_from_another_session_does_not_leak(tmp_path):
    async def exercise():
        harness = HermesAcpAttemptRuntime(tmp_path)

        class Connection:
            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="session-current")

            async def prompt(self, **kwargs):
                harness._client.messages["session-current"].append(
                    "Error: a legitimate report heading"
                )
                harness._append_stderr(
                    "Agent error in session session-previous\n"
                )
                return SimpleNamespace(stop_reason="end_turn", usage=None)

        harness._started = True
        harness._connection = Connection()
        harness._process_instance_id = "hermes-process-current"

        result = await harness.invoke(_invocation(tmp_path))

        assert result.status == "succeeded"
        assert result.error is None

    asyncio.run(exercise())
