"""Claude Code production backend exposed through the official ACP adapter."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from acp.schema import (
    AllowedOutcome,
    EnvVariable,
    McpServerStdio,
    RequestPermissionResponse,
)

from deepresearch_cli.progress import ProgressReporter
from deepresearch_cli.search.registry import (
    ProviderRegistry,
    ProviderRegistryError,
    load_search_environment,
)

from .acp.client import RecordingAcpClient
from .acp.launch import AcpLaunchSpec
from .camofox_fallback import CamofoxFallbackSupport
from .acp_agent import AcpAgentAttemptRuntime
from .protocol import AgentInvocation, HarnessError
from .search_mcp import SearchMcpSupport


_CLAUDE_EXTERNAL_AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


class _ClaudeAcpClient(RecordingAcpClient):
    """Approve one attempt's tool requests without persisting permission rules."""

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,
        tool_call: Any,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        del kwargs
        selected = next(
            (option for option in options if option.kind == "allow_once"),
            None,
        )
        if selected is None:
            return await super().request_permission(
                options=options,
                session_id=session_id,
                tool_call=tool_call,
            )
        return RequestPermissionResponse(
            outcome=AllowedOutcome(
                outcome="selected",
                option_id=selected.option_id,
            )
        )


class ClaudeAcpAttemptRuntime(AcpAgentAttemptRuntime):
    """One claude-agent-acp subprocess serving one DeepResearch attempt."""

    backend_name = "Claude Code ACP"

    def __init__(
        self,
        workspace: Path,
        *,
        claude_acp_command: Optional[str] = None,
        profile: Optional[str] = None,
        model: Optional[str] = None,
        startup_timeout_seconds: float = 30.0,
        progress_reporter: Optional[ProgressReporter] = None,
        search_mcp_enabled: bool = False,
        search_dir: Optional[Path] = None,
        search_provider_python: Optional[str] = None,
        search_provider_limit: int = 20,
        search_support: Optional[SearchMcpSupport] = None,
        camofox_fallback_enabled: bool = False,
        camofox_home: Optional[Path] = None,
        camofox_base_url: Optional[str] = None,
        expected_invocation_id: Optional[str] = None,
    ) -> None:
        supplied = (
            claude_acp_command
            or shutil.which("claude-agent-acp")
            or "claude-agent-acp"
        )
        resolved = shutil.which(supplied) or (
            supplied if Path(supplied).expanduser().is_file() else None
        )
        self.claude_acp_command = (
            str(Path(resolved).expanduser().resolve()) if resolved else supplied
        )
        self.claude_config_dir = (
            str(Path(profile).expanduser().resolve()) if profile else None
        )
        self.claude_model = model
        super().__init__(
            workspace,
            acp_command=self.claude_acp_command,
            launch_backend="claude-code",
            process_prefix="claude-acp-process",
            profile=None,
            startup_timeout_seconds=startup_timeout_seconds,
            progress_reporter=progress_reporter,
            search_mcp_enabled=search_mcp_enabled,
            search_dir=search_dir,
            search_provider_python=search_provider_python,
            search_provider_limit=search_provider_limit,
            search_support=search_support,
            camofox_fallback_enabled=camofox_fallback_enabled,
            camofox_home=camofox_home,
            camofox_base_url=camofox_base_url,
            expected_invocation_id=expected_invocation_id,
        )
        self._client = _ClaudeAcpClient(
            raw_observer_enabled=True,
            event_observer=self._observe_session_event,
        )
        self.launch_spec = AcpLaunchSpec(
            backend="claude-code",
            command=self.claude_acp_command,
            cwd=self.workspace,
            environment=self._acp_environment(),
            process_prefix="claude-acp-process",
        )

    def _acp_args(self, *tail: str) -> tuple[str, ...]:
        return tuple(tail)

    def _acp_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self.claude_config_dir:
            environment["CLAUDE_CONFIG_DIR"] = self.claude_config_dir
        if self.claude_model:
            environment["ANTHROPIC_MODEL"] = self.claude_model
        return environment

    def _workspace_edit_mode(self) -> str:
        return "acceptEdits"

    def _notify_invocation_started(self, invocation: AgentInvocation) -> None:
        self._acp_invocation_started(invocation)

    def _notify_invocation_finished(
        self, invocation: AgentInvocation, status: str
    ) -> None:
        self._acp_invocation_finished(invocation, status)

    def _search_mcp_server(
        self,
        *,
        identity: str,
        store_dir: Path,
        batch_timeout_seconds: Optional[float] = None,
        lease_file: Optional[Path] = None,
    ) -> tuple[str, McpServerStdio, Path]:
        support = self.search_support or SearchMcpSupport(
            search_dir=self.search_dir,
            provider_python=self.search_provider_python,
            provider_limit=self.search_provider_limit,
            camofox_fallback_enabled=self.camofox_fallback_enabled,
            camofox_base_url=self.camofox_base_url,
        )
        spec = support.build(
            identity=identity,
            store_dir=store_dir,
            batch_timeout_seconds=batch_timeout_seconds or 120.0,
            lease_file=lease_file,
        )
        descriptor = McpServerStdio(
            name=spec.name,
            command=spec.command,
            args=list(spec.args),
            env=[
                EnvVariable(name=name, value=value)
                for name, value in sorted(spec.env.items())
            ],
        )
        return spec.name, descriptor, spec.lease_file

    async def _verify_search_tools(
        self,
        *,
        session_id: str,
        server_name: str,
        timeout_seconds: float,
    ) -> list[str]:
        del session_id, timeout_seconds
        return self._expected_search_tools(server_name)

    async def _verify_native_search_registration(
        self,
        *,
        server_name: str,
        timeout_seconds: float,
    ) -> list[str]:
        del timeout_seconds
        return self._expected_search_tools(server_name)

    @staticmethod
    def _expected_search_tools(server_name: str) -> list[str]:
        return [
            f"mcp__{server_name}__{tool}"
            for tool in (
                "list_search_domains",
                "start_domain_search",
                "get_search_batch",
                "list_search_sources",
                "batch_search",
                "search_results",
                "get_search_hit",
                "fetch_url",
            )
        ]


@dataclass(frozen=True)
class ClaudeAcpBackendFactory:
    workspace: Path
    claude_acp_command: Optional[str] = None
    profile: Optional[str] = None
    model: Optional[str] = None
    startup_timeout_seconds: float = 30.0
    progress_reporter: Optional[ProgressReporter] = None
    search_mcp_enabled: bool = False
    search_dir: Optional[Path] = None
    search_provider_python: Optional[str] = None
    search_provider_limit: int = 20
    search_coordinator: Optional[object] = None
    camofox_fallback_enabled: bool = False
    camofox_home: Optional[Path] = None
    camofox_base_url: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace.expanduser().resolve())

    def _command(self) -> str:
        supplied = (
            self.claude_acp_command
            or shutil.which("claude-agent-acp")
            or "claude-agent-acp"
        )
        command = shutil.which(supplied) or (
            supplied if Path(supplied).expanduser().is_file() else None
        )
        if command is None:
            raise HarnessError(
                "Claude ACP adapter not found: "
                f"{supplied}; install @agentclientprotocol/claude-agent-acp"
            )
        return str(Path(command).expanduser().resolve())

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self.profile:
            environment["CLAUDE_CONFIG_DIR"] = str(
                Path(self.profile).expanduser().resolve()
            )
        if self.model:
            environment["ANTHROPIC_MODEL"] = self.model
        return environment

    async def _run(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self._command(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        return (
            int(process.returncode or 0),
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def preflight(self) -> Mapping[str, Any]:
        version_code, version_out, version_err = await self._run("--version")
        if version_code:
            raise HarnessError(
                version_err or version_out or "claude-agent-acp --version failed"
            )
        environment = self._environment()
        external_auth = next(
            (name for name in _CLAUDE_EXTERNAL_AUTH_ENV if environment.get(name)),
            None,
        )
        if external_auth:
            authentication = f"environment:{external_auth}"
        else:
            auth_code, auth_out, auth_err = await self._run(
                "--cli", "auth", "status", "--text"
            )
            if auth_code:
                raise HarnessError(
                    auth_err or auth_out or "Claude Code authentication check failed"
                )
            authentication = (auth_out or auth_err).strip()
        report: dict[str, Any] = {
            "harness": "claude-code",
            "transport": "acp",
            "bridge": "@agentclientprotocol/claude-agent-acp",
            "command": self._command(),
            "version": (version_out or version_err).strip(),
            "authentication": authentication,
            "profile": (
                str(Path(self.profile).expanduser().resolve())
                if self.profile
                else None
            ),
            "model": self.model,
            "ok": True,
        }
        if self.search_mcp_enabled:
            support = SearchMcpSupport(
                search_dir=self.search_dir,
                provider_python=self.search_provider_python,
                provider_limit=self.search_provider_limit,
            )
            search_dir = support.resolved_search_dir()
            provider_python = support.resolved_provider_python()
            registry = ProviderRegistry(
                search_dir=search_dir,
                python_executable=provider_python,
                environment=load_search_environment(search_dir),
            )
            available = 0
            for definition in registry.definitions:
                try:
                    usable = (
                        registry.script_path(definition).is_file()
                        and not registry.missing_modules(definition)
                    )
                except ProviderRegistryError:
                    usable = False
                available += int(usable)
            if available == 0:
                raise HarnessError("no configured search provider is runtime-available")
            report.update(
                {
                    "search_mcp": "configured",
                    "search_dir": str(search_dir),
                    "search_provider_python": provider_python,
                    "search_route_count": len(registry.definitions),
                    "search_route_available_count": available,
                }
            )
        else:
            report["search_mcp"] = "disabled"
        report.update(
            CamofoxFallbackSupport(
                enabled=self.camofox_fallback_enabled,
                home=self.camofox_home,
                base_url=self.camofox_base_url,
            ).report()
        )
        return report

    async def probe(self) -> Mapping[str, Any]:
        runtime = self._runtime()
        try:
            await runtime.start()
            search_report = await runtime.check_search_mcp()
            return {
                "claude_acp": "ok",
                "acp_initialize": "ok",
                "model_check": "not_run",
                **dict(search_report),
            }
        finally:
            await _shielded_runtime_close(runtime)

    def _runtime(
        self, *, expected_invocation_id: Optional[str] = None
    ) -> ClaudeAcpAttemptRuntime:
        return ClaudeAcpAttemptRuntime(
            self.workspace,
            claude_acp_command=self._command(),
            profile=self.profile,
            model=self.model,
            startup_timeout_seconds=self.startup_timeout_seconds,
            progress_reporter=self.progress_reporter,
            search_mcp_enabled=self.search_mcp_enabled,
            search_dir=self.search_dir,
            search_provider_python=self.search_provider_python,
            search_provider_limit=self.search_provider_limit,
            search_support=(
                SearchMcpSupport(
                    search_dir=self.search_dir,
                    provider_python=self.search_provider_python,
                    provider_limit=self.search_provider_limit,
                    coordinator=self.search_coordinator,
                    camofox_fallback_enabled=self.camofox_fallback_enabled,
                    camofox_base_url=self.camofox_base_url,
                )
                if expected_invocation_id is not None
                and self.search_coordinator is not None
                else None
            ),
            camofox_fallback_enabled=self.camofox_fallback_enabled,
            camofox_home=self.camofox_home,
            camofox_base_url=self.camofox_base_url,
            expected_invocation_id=expected_invocation_id,
        )

    def create(self, invocation: AgentInvocation) -> ClaudeAcpAttemptRuntime:
        return self._runtime(expected_invocation_id=invocation.invocation_id)


async def _shielded_runtime_close(runtime: ClaudeAcpAttemptRuntime) -> None:
    cleanup = asyncio.create_task(runtime.close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await cleanup
        raise
