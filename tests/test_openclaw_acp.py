import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepresearch_cli.cli import build_parser
from deepresearch_cli.harness.openclaw_acp import (
    _OpenClawAcpClient,
    OpenClawAcpAttemptRuntime,
    OpenClawAcpBackendFactory,
)
from deepresearch_cli.harness.protocol import AgentInvocation, HarnessError
from deepresearch_cli.harness.registry import build_backend_factory
from deepresearch_cli.harness.search_mcp import SearchMcpSupport


class _Coordinator:
    url = "http://127.0.0.1:18765"

    @classmethod
    def credentials(cls, namespace: str) -> tuple[str, str]:
        return cls.url, "scoped-" + namespace


def _invocation(workspace: Path) -> AgentInvocation:
    workspace.mkdir(parents=True, exist_ok=True)
    return AgentInvocation(
        invocation_id="openclaw-research-test",
        run_id="run-test",
        node_instance_id="research-d1",
        node_type="research",
        attempt=1,
        workspace=workspace,
        input_artifact_refs=[],
        resolved_input_artifacts=[],
        timeout_seconds=30,
        agent_context={},
        prompt="research the topic",
        allow_workspace_edits=False,
    )


def _write_fake_openclaw(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'OpenClaw 1.2.3'; exit 0; fi\n"
        "if [ \"$1\" = \"status\" ]; then echo '{\"gateway\":\"ok\"}'; exit 0; fi\n"
        "if [ \"$1\" = \"agents\" ] && [ \"$2\" = \"list\" ]; then echo '[{\"id\":\"main\",\"isDefault\":true}]'; exit 0; fi\n"
        "if [ \"$1\" = \"config\" ] && [ \"$2\" = \"get\" ]; then\n"
        "  case \"$3\" in\n"
        "    agents.entries.main.sandbox) echo '{\"mode\":\"off\",\"workspaceAccess\":\"rw\"}' ;;\n"
        "    agents.entries.main.tools) echo '{\"allow\":[\"read\",\"write\",\"edit\",\"apply_patch\",\"exec\",\"process\"]}' ;;\n"
        "    *) exit 2 ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_openclaw_is_a_registered_acp_backend(tmp_path: Path) -> None:
    parsed = build_parser().parse_args(
        [
            "research",
            "topic",
            "--report-format",
            "formal_report",
            "--harness",
            "openclaw",
        ]
    )
    factory = build_backend_factory("openclaw", workspace=tmp_path)
    assert parsed.harness == "openclaw"
    assert isinstance(factory, OpenClawAcpBackendFactory)


def test_openclaw_preflight_checks_gateway_without_model_call(tmp_path: Path) -> None:
    command = _write_fake_openclaw(tmp_path / "openclaw")
    factory = OpenClawAcpBackendFactory(
        workspace=tmp_path,
        openclaw_command=str(command),
        search_mcp_enabled=False,
    )
    report = asyncio.run(factory.preflight())
    assert report["harness"] == "openclaw"
    assert report["transport"] == "acp"
    assert report["bridge"] == "openclaw-gateway"
    assert report["model"] == "configured-by-openclaw-agent"
    assert report["workspace_permissions"]["workspace_access"] == "rw"
    assert set(report["workspace_permissions"]["required_tools"]) == {
        "read", "write", "edit", "apply_patch", "exec", "process"
    }


def test_openclaw_search_bridge_is_attempt_scoped_and_removed(tmp_path: Path) -> None:
    workspace = tmp_path / "runs" / "run-test" / "attempts" / "research" / "staging"
    invocation = _invocation(workspace)
    runtime = OpenClawAcpAttemptRuntime(
        tmp_path / "runs",
        search_mcp_enabled=True,
        search_support=SearchMcpSupport(coordinator=_Coordinator()),
        camofox_fallback_enabled=True,
        expected_invocation_id=invocation.invocation_id,
    )
    name, descriptor, lease = runtime._search_mcp_server(
        identity=invocation.invocation_id,
        store_dir=workspace.parent / "search",
    )
    context_path = runtime._search_contexts[invocation.invocation_id]
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    prompt = runtime._prompt_text(invocation)

    assert name.startswith("drs_")
    assert descriptor is None
    assert lease.is_file()
    assert payload["coordinator_token"] == "scoped-openclaw-research-test"
    if os.name != "nt":
        assert context_path.stat().st_mode & 0o077 == 0
    assert "scoped-openclaw-research-test" not in prompt
    assert "deepresearch_cli.search.tool_cli" in prompt

    asyncio.run(runtime.close())
    assert not context_path.exists()


def test_openclaw_rejects_unsupported_per_session_model_override(tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="OpenClaw agent configuration"):
        OpenClawAcpAttemptRuntime(tmp_path, model="model-test")


def test_openclaw_allows_only_exact_attempt_search_bridge_exec(tmp_path: Path) -> None:
    context = (tmp_path / "attempt.json").resolve()
    client = _OpenClawAcpClient(
        allowed_contexts={"attempt": context}, workspace_root=tmp_path
    )
    options = [
        SimpleNamespace(kind="allow_once", option_id="allow"),
        SimpleNamespace(kind="reject_once", option_id="reject"),
    ]
    base = shlex.join(
        [
            os.path.abspath(sys.executable),
            "-m",
            "deepresearch_cli.search.tool_cli",
            "--context",
            str(context),
        ]
    )

    allowed = asyncio.run(
        client.request_permission(
            options,
            "session",
            SimpleNamespace(
                kind="execute",
                raw_input={"command": base + " list-search-sources"},
            ),
        )
    )
    injected = asyncio.run(
        client.request_permission(
            options,
            "session",
            SimpleNamespace(
                kind="execute",
                raw_input={"command": base + " list-search-sources; id"},
            ),
        )
    )
    wrong_context = asyncio.run(
        client.request_permission(
            options,
            "session",
            SimpleNamespace(
                kind="execute",
                raw_input={
                    "command": base.replace(str(context), str(tmp_path / "other.json"))
                    + " list-search-sources"
                },
            ),
        )
    )

    assert allowed.outcome.option_id == "allow"
    assert injected.outcome.option_id == "reject"
    assert wrong_context.outcome.option_id == "reject"

    write = asyncio.run(
        client.request_permission(
            options,
            "session",
            SimpleNamespace(
                kind="write",
                raw_input={"file_path": str(tmp_path / "staging" / "plan.json")},
            ),
        )
    )
    outside = asyncio.run(
        client.request_permission(
            options,
            "session",
            SimpleNamespace(
                kind="write",
                raw_input={"file_path": str(tmp_path.parent / "plan.json")},
            ),
        )
    )
    assert write.outcome.option_id == "allow"
    assert outside.outcome.option_id == "reject"


def test_openclaw_invocation_omits_session_mcp_and_edit_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "runs" / "run-test" / "attempts" / "plan" / "staging"
    invocation = _invocation(workspace)
    invocation = AgentInvocation(
        **{
            **invocation.__dict__,
            "node_type": "plan",
            "allow_workspace_edits": True,
        }
    )
    runtime = OpenClawAcpAttemptRuntime(
        tmp_path / "runs",
        search_mcp_enabled=False,
        expected_invocation_id=invocation.invocation_id,
    )

    class Connection:
        async def new_session(self, **kwargs):
            assert kwargs["mcp_servers"] is None
            return SimpleNamespace(session_id="openclaw-session")

        async def prompt(self, **kwargs):
            runtime._client.messages[kwargs["session_id"]].append("done")
            return SimpleNamespace(stop_reason="end_turn", usage=None)

    runtime._started = True
    runtime._connection = Connection()
    runtime._process = SimpleNamespace(pid=1234)
    runtime._process_instance_id = "openclaw-acp-process-test"

    result = asyncio.run(runtime.invoke(invocation))

    assert result.status == "succeeded"
    assert result.response_text == "done"
