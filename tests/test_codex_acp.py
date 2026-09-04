import asyncio
from pathlib import Path

from acp.schema import EnvVariable, McpServerStdio

from deepresearch_cli.cli import build_parser
from deepresearch_cli.harness import AgentInvocation
from deepresearch_cli.harness.codex_acp import (
    CodexAcpAttemptRuntime,
    CodexAcpBackendFactory,
)
from deepresearch_cli.harness.codex_acp_bridge.agent import CodexAcpBridgeAgent
from deepresearch_cli.harness.registry import PRODUCTION_BACKENDS, build_backend_factory


def _write_fake_codex(path: Path, *, stall_turn: bool = False) -> Path:
    completion = "" if stall_turn else (
        "        print(json.dumps({'method':'item/started','params':{'threadId':'thr-test','turnId':'turn-test','item':{'id':'tool-1','type':'commandExecution','command':'inspect inputs','status':'inProgress'},'startedAtMs':1}}), flush=True)\n"
        "        print(json.dumps({'method':'item/agentMessage/delta','params':{'threadId':'thr-test','turnId':'turn-test','itemId':'msg-1','delta':'done'}}), flush=True)\n"
        "        print(json.dumps({'method':'thread/tokenUsage/updated','params':{'threadId':'thr-test','turnId':'turn-test','tokenUsage':{'last':{'inputTokens':12,'cachedInputTokens':2,'outputTokens':3,'reasoningOutputTokens':1,'totalTokens':15},'total':{'inputTokens':12,'cachedInputTokens':2,'outputTokens':3,'reasoningOutputTokens':1,'totalTokens':15}}}}), flush=True)\n"
        "        print(json.dumps({'method':'item/completed','params':{'threadId':'thr-test','turnId':'turn-test','item':{'id':'tool-1','type':'commandExecution','command':'inspect inputs','status':'completed'},'completedAtMs':2}}), flush=True)\n"
        "        print(json.dumps({'method':'turn/completed','params':{'threadId':'thr-test','turn':{'id':'turn-test','status':'completed','items':[]}}}), flush=True)\n"
    )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli bridge-test')\n"
        "    raise SystemExit\n"
        "if 'login' in sys.argv and 'status' in sys.argv:\n"
        "    print('Logged in')\n"
        "    raise SystemExit\n"
        "if 'app-server' in sys.argv and '--help' in sys.argv:\n"
        "    print('Codex App Server')\n"
        "    raise SystemExit\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    method = message.get('method')\n"
        "    request_id = message.get('id')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'id':request_id,'result':{'userAgent':'fake'}}), flush=True)\n"
        "    elif method == 'thread/start':\n"
        "        print(json.dumps({'id':request_id,'result':{'thread':{'id':'thr-test'}}}), flush=True)\n"
        "    elif method == 'turn/start':\n"
        "        print(json.dumps({'id':request_id,'result':{'turn':{'id':'turn-test','status':'inProgress','items':[]}}}), flush=True)\n"
        + completion
        + "    elif method == 'turn/interrupt':\n"
        "        print(json.dumps({'id':request_id,'result':{}}), flush=True)\n"
        "        print(json.dumps({'method':'turn/completed','params':{'threadId':'thr-test','turn':{'id':'turn-test','status':'interrupted','items':[]}}}), flush=True)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _invocation(workspace: Path, *, timeout: float = 5) -> AgentInvocation:
    workspace.mkdir(parents=True, exist_ok=True)
    return AgentInvocation(
        invocation_id="inv-codex-acp-test",
        run_id="run-test",
        node_instance_id="research-test",
        node_type="research",
        attempt=1,
        workspace=workspace,
        input_artifact_refs=[],
        resolved_input_artifacts=[],
        timeout_seconds=timeout,
        agent_context={},
        prompt="write the declared output",
        allow_workspace_edits=True,
    )


def test_codex_cli_name_selects_acp(tmp_path):
    assert PRODUCTION_BACKENDS == (
        "hermes", "codex", "claude-code", "openclaw"
    )
    parsed = build_parser().parse_args(
        [
            "research",
            "topic",
            "--report-format",
            "formal_report",
            "--harness",
            "codex",
            "--harness-command",
            str(tmp_path / "codex"),
        ]
    )

    assert parsed.harness == "codex"
    assert isinstance(
        build_backend_factory(
            "codex", workspace=tmp_path, command=str(tmp_path / "codex")
        ),
        CodexAcpBackendFactory,
    )


def test_codex_acp_factory_preflight_checks_app_server(tmp_path):
    command = _write_fake_codex(tmp_path / "codex")
    factory = CodexAcpBackendFactory(
        workspace=tmp_path,
        codex_command=str(command),
        search_mcp_enabled=False,
    )

    report = asyncio.run(factory.preflight())

    assert report["transport"] == "acp"
    assert report["bridge"] == "codex-app-server"
    assert report["version"] == "codex-cli bridge-test"


def test_codex_acp_bridge_maps_app_server_turn_to_common_result(tmp_path):
    command = _write_fake_codex(tmp_path / "codex")
    runtime = CodexAcpAttemptRuntime(
        tmp_path / "runs",
        codex_command=str(command),
        search_mcp_enabled=False,
        expected_invocation_id="inv-codex-acp-test",
    )

    async def exercise():
        await runtime.start()
        try:
            return await runtime.invoke(_invocation(tmp_path / "runs" / "staging"))
        finally:
            await runtime.close()

    result = asyncio.run(exercise())

    assert result.status == "succeeded"
    assert result.response_text == "done"
    assert result.native_session_id.startswith("codex-acp-")
    assert result.native_process_instance_id.startswith("codex-acp-process-")
    assert result.usage == {
        "cachedReadTokens": 2,
        "inputTokens": 12,
        "outputTokens": 3,
        "thoughtTokens": 1,
        "totalTokens": 15,
    }
    assert [event["sessionUpdate"] for event in result.events] == [
        "tool_call",
        "agent_message_chunk",
        "tool_call_update",
    ]


def test_codex_acp_timeout_interrupts_the_app_server_turn(tmp_path):
    command = _write_fake_codex(tmp_path / "codex", stall_turn=True)
    runtime = CodexAcpAttemptRuntime(
        tmp_path / "runs",
        codex_command=str(command),
        search_mcp_enabled=False,
        expected_invocation_id="inv-codex-acp-test",
    )

    async def exercise():
        await runtime.start()
        try:
            return await runtime.invoke(
                _invocation(tmp_path / "runs" / "staging", timeout=0.1)
            )
        finally:
            await runtime.close()

    result = asyncio.run(exercise())

    assert result.status == "cancelled"
    assert result.stop_reason == "cancelled"
    assert "Codex invocation timed out" in result.error
    assert result.failure_kind == "timeout"


def test_codex_acp_mcp_secrets_stay_in_the_bridge_config():
    descriptor = McpServerStdio(
        name="private_search",
        command="/usr/bin/python3",
        args=["-m", "search_server"],
        env=[EnvVariable(name="PRIVATE_TOKEN", value="secret-value")],
    )

    config = CodexAcpBridgeAgent._mcp_config([descriptor])

    assert config["mcp_servers"]["private_search"]["env"] == {
        "PRIVATE_TOKEN": "secret-value"
    }
