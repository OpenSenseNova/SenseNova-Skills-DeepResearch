import asyncio
from pathlib import Path
from types import SimpleNamespace

from acp.schema import PermissionOption, ToolCallUpdate

from deepresearch_cli.cli import build_parser
from deepresearch_cli.harness import AgentInvocation
from deepresearch_cli.harness.claude_acp import (
    ClaudeAcpAttemptRuntime,
    ClaudeAcpBackendFactory,
    _ClaudeAcpClient,
)
from deepresearch_cli.harness.registry import build_backend_factory


def _write_fake_adapter(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 0.70.0\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"--cli\" ] && [ \"$2\" = \"auth\" ]; then\n"
        "  echo 'Logged in'\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _invocation(workspace: Path) -> AgentInvocation:
    workspace.mkdir(parents=True, exist_ok=True)
    return AgentInvocation(
        invocation_id="inv-claude-acp-test",
        run_id="run-test",
        node_instance_id="plan-test",
        node_type="plan",
        attempt=1,
        workspace=workspace,
        input_artifact_refs=[],
        resolved_input_artifacts=[],
        timeout_seconds=5,
        agent_context={},
        prompt="write the declared output",
        allow_workspace_edits=True,
    )


def test_claude_code_cli_name_selects_acp_backend(tmp_path):
    parsed = build_parser().parse_args(
        [
            "research",
            "topic",
            "--report-format",
            "formal_report",
            "--harness",
            "claude-code",
            "--harness-command",
            str(tmp_path / "claude-agent-acp"),
        ]
    )

    assert parsed.harness == "claude-code"
    assert isinstance(
        build_backend_factory(
            "claude-code",
            workspace=tmp_path,
            command=str(tmp_path / "claude-agent-acp"),
        ),
        ClaudeAcpBackendFactory,
    )


def test_claude_acp_factory_checks_adapter_and_authentication(tmp_path, monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        monkeypatch.delenv(name, raising=False)
    command = _write_fake_adapter(tmp_path / "claude-agent-acp")
    config_dir = tmp_path / "claude-home"
    factory = ClaudeAcpBackendFactory(
        workspace=tmp_path,
        claude_acp_command=str(command),
        profile=str(config_dir),
        model="claude-sonnet-test",
        search_mcp_enabled=False,
    )

    report = asyncio.run(factory.preflight())

    assert report["harness"] == "claude-code"
    assert report["transport"] == "acp"
    assert report["bridge"] == "@agentclientprotocol/claude-agent-acp"
    assert report["version"] == "0.70.0"
    assert report["authentication"] == "Logged in"
    assert report["profile"] == str(config_dir.resolve())
    assert report["model"] == "claude-sonnet-test"


def test_claude_acp_runtime_uses_profile_model_and_claude_edit_mode(tmp_path):
    command = _write_fake_adapter(tmp_path / "claude-agent-acp")
    config_dir = tmp_path / "claude-home"
    runtime = ClaudeAcpAttemptRuntime(
        tmp_path / "runs",
        claude_acp_command=str(command),
        profile=str(config_dir),
        model="claude-sonnet-test",
        search_mcp_enabled=False,
        expected_invocation_id="inv-claude-acp-test",
    )
    modes = []

    class Connection:
        async def new_session(self, **kwargs):
            assert kwargs["mcp_servers"] == []
            return SimpleNamespace(session_id="claude-session-test")

        async def set_session_mode(self, **kwargs):
            modes.append(kwargs["mode_id"])
            return SimpleNamespace()

        async def prompt(self, **kwargs):
            runtime._client.messages[kwargs["session_id"]].append("done")
            return SimpleNamespace(stop_reason="end_turn", usage=None)

    runtime._started = True
    runtime._connection = Connection()
    runtime._process = SimpleNamespace(pid=1234)
    runtime._process_instance_id = "claude-acp-process-test"
    workspace = tmp_path / "runs" / "run-test" / "attempts" / "plan" / "staging"

    result = asyncio.run(runtime.invoke(_invocation(workspace)))

    assert result.status == "succeeded"
    assert result.response_text == "done"
    assert result.native_session_id == "claude-session-test"
    assert modes == ["acceptEdits"]
    environment = runtime._acp_environment()
    assert environment["CLAUDE_CONFIG_DIR"] == str(config_dir.resolve())
    assert environment["ANTHROPIC_MODEL"] == "claude-sonnet-test"


def test_claude_acp_permission_client_selects_allow_once():
    async def exercise():
        client = _ClaudeAcpClient()
        response = await client.request_permission(
            options=[
                PermissionOption(
                    optionId="once", name="Allow once", kind="allow_once"
                ),
                PermissionOption(
                    optionId="reject", name="Reject", kind="reject_once"
                ),
            ],
            session_id="session-test",
            tool_call=ToolCallUpdate(toolCallId="tool-test", kind="execute"),
        )
        return response

    response = asyncio.run(exercise())
    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "once"
