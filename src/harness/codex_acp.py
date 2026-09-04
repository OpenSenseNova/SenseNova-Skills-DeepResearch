"""Codex production backend exposed to DeepResearch through ACP."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from acp.schema import EnvVariable, McpServerStdio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from deepresearch_cli.progress import ProgressReporter
from deepresearch_cli.search.registry import (
    ProviderRegistry,
    ProviderRegistryError,
    load_search_environment,
)

from .acp.launch import AcpLaunchSpec
from .camofox_fallback import CamofoxFallbackSupport
from .acp_agent import AcpAgentAttemptRuntime
from .protocol import AgentInvocation, HarnessError
from .search_mcp import SearchMcpSupport


class CodexAcpAttemptRuntime(AcpAgentAttemptRuntime):
    """One ACP bridge and one Codex App Server process for one attempt."""

    backend_name = "Codex"

    def __init__(
        self,
        workspace: Path,
        *,
        codex_command: Optional[str] = None,
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
        supplied = codex_command or shutil.which("codex") or "codex"
        resolved = shutil.which(supplied) or (
            supplied if Path(supplied).expanduser().is_file() else None
        )
        self.codex_command = (
            str(Path(resolved).expanduser().resolve()) if resolved else supplied
        )
        self.codex_profile = profile
        self.codex_model = model
        bridge_python = os.path.abspath(sys.executable)
        super().__init__(
            workspace,
            acp_command=bridge_python,
            launch_backend="codex",
            process_prefix="codex-acp-process",
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
        self._prepared_search_spec = None
        self._codex_home = None
        # Do not resolve a virtual-environment interpreter symlink: the bridge
        # must retain the environment containing deepresearch_cli.
        self.acp_command = bridge_python
        # Keep the active virtual-environment launcher. Resolving this symlink
        # to the base interpreter would lose the editable package installation
        # when the bridge process changes cwd to an attempt workspace.
        self.launch_spec = AcpLaunchSpec(
            backend="codex",
            command=bridge_python,
            args=self._acp_args(),
            cwd=self.workspace,
            process_prefix="codex-acp-process",
        )

    def _acp_args(self, *tail: str) -> tuple[str, ...]:
        del tail
        args = [
            "-m",
            "deepresearch_cli.harness.codex_acp_bridge",
            "--codex-command",
            self.codex_command,
        ]
        if self.codex_profile:
            args.extend(["--profile", self.codex_profile])
        if self.codex_model:
            args.extend(["--model", self.codex_model])
        return tuple(args)

    @staticmethod
    def _write_codex_mcp_config(home: Path, spec: Any) -> None:
        home.mkdir(parents=True, exist_ok=True)
        lines = [f"[mcp_servers.{spec.name}]", f"command = {json.dumps(spec.command)}", f"cwd = {json.dumps(str(home))}", "enabled = true", "required = true", "startup_timeout_sec = 30", "tool_timeout_sec = 120"]
        if spec.args:
            lines.append("args = [" + ", ".join(json.dumps(str(x)) for x in spec.args) + "]")
        if spec.env:
            lines += ["", f"[mcp_servers.{spec.name}.env]"]
            lines.extend(f"{key} = {json.dumps(str(value), ensure_ascii=False)}" for key, value in sorted(spec.env.items()))
        (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _prepare_session_environment(self, invocation: AgentInvocation, servers: list[Any]) -> None:
        if not servers:
            return
        project = invocation.workspace / ".codex"
        project.mkdir(parents=True, exist_ok=True)
        if self._prepared_search_spec is not None:
            self._write_codex_mcp_config(project, self._prepared_search_spec)

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
        if self._prepared_search_spec is not None and identity == self.expected_invocation_id:
            spec = self._prepared_search_spec
        self._prepared_search_spec = spec
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

    async def _verify_native_search_registration(
        self,
        *,
        server_name: str,
        timeout_seconds: float,
    ) -> list[str]:
        """Codex registers session MCP tools through the App Server bridge.

        Unlike Hermes, Codex does not emit Hermes' stderr registration marker.
        The bridge owns the session registration, so the expected tool surface
        is the authoritative check here.
        """
        del timeout_seconds
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
class CodexAcpBackendFactory:
    workspace: Path
    codex_command: Optional[str] = None
    profile: Optional[str] = None
    model: Optional[str] = None
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
        supplied = self.codex_command or shutil.which("codex") or "codex"
        command = shutil.which(supplied) or (
            supplied if Path(supplied).expanduser().is_file() else None
        )
        if command is None:
            raise HarnessError(f"Codex executable not found: {supplied}")
        return str(Path(command).expanduser().resolve())

    async def _run(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self._command(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(os.environ),
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
            raise HarnessError(version_err or version_out or "codex --version failed")
        login_code, login_out, login_err = await self._run("login", "status")
        if login_code:
            raise HarnessError(login_err or login_out or "codex login status failed")
        app_code, app_out, app_err = await self._run("app-server", "--help")
        if app_code:
            raise HarnessError(
                app_err or app_out or "codex app-server is unavailable"
            )
        report: dict[str, Any] = {
            "harness": "codex",
            "transport": "acp",
            "bridge": "codex-app-server",
            "command": self._command(),
            "version": (version_out or version_err).strip(),
            "authentication": (login_out or login_err).strip(),
            "profile": self.profile,
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
            environment = load_search_environment(search_dir)
            registry = ProviderRegistry(
                search_dir=search_dir,
                python_executable=provider_python,
                environment=environment,
            )
            available = 0
            for definition in registry.definitions:
                try:
                    usable = registry.script_path(
                        definition
                    ).is_file() and not registry.missing_modules(definition)
                except ProviderRegistryError:
                    usable = False
                available += int(usable)
            if available == 0:
                raise HarnessError(
                    "no configured search provider is runtime-available"
                )
            report.update(
                {
                    "search_mcp": "configured",
                    "search_dir": str(search_dir),
                    "search_provider_python": provider_python,
                    "search_route_count": len(registry.definitions),
                    "search_route_available_count": available,
                }
            )
            # Verify the actual stdio MCP process and tool surface. Registry
            # metadata alone cannot detect App Server spawn/handshake issues.
            report["search_mcp_probe"] = await self._probe_search_mcp(support)
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

    async def _probe_search_mcp(self, support: SearchMcpSupport) -> Mapping[str, Any]:
        with tempfile.TemporaryDirectory(prefix="deepresearch-codex-mcp-") as raw:
            root = Path(raw)
            spec = support.build(identity="doctor", store_dir=root / "store")
            env = dict(os.environ)
            env.update(spec.env)
            params = StdioServerParameters(
                command=spec.command,
                args=list(spec.args),
                env=env,
            )
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), timeout=30)
                        tools = await asyncio.wait_for(session.list_tools(), timeout=30)
                        names = {tool.name for tool in tools.tools}
                        required = {
                            "list_search_domains",
                            "list_search_sources",
                        }
                        missing = sorted(required - names)
                        if missing:
                            raise HarnessError(
                                "Search MCP is missing required tools: "
                                + ", ".join(missing)
                            )
                        await asyncio.wait_for(
                            session.call_tool("list_search_domains", {}), timeout=30
                        )
                return {"status": "ok", "tool_count": len(names)}
            except Exception as exc:
                raise HarnessError(f"Search MCP probe failed: {exc}") from exc

    async def probe(self) -> Mapping[str, Any]:
        return {
            "codex_acp": "ok",
            "codex_app_server": "available",
            "model_check": "not_run",
        }

    def create(self, invocation: AgentInvocation) -> CodexAcpAttemptRuntime:
        return CodexAcpAttemptRuntime(
            self.workspace,
            codex_command=self._command(),
            profile=self.profile,
            model=self.model,
            progress_reporter=self.progress_reporter,
            search_mcp_enabled=self.search_mcp_enabled,
            search_dir=self.search_dir,
            search_provider_python=self.search_provider_python,
            search_provider_limit=self.search_provider_limit,
            search_support=SearchMcpSupport(
                search_dir=self.search_dir,
                provider_python=self.search_provider_python,
                provider_limit=self.search_provider_limit,
                coordinator=self.search_coordinator,
                camofox_fallback_enabled=self.camofox_fallback_enabled,
                camofox_base_url=self.camofox_base_url,
            ),
            camofox_fallback_enabled=self.camofox_fallback_enabled,
            camofox_home=self.camofox_home,
            camofox_base_url=self.camofox_base_url,
            expected_invocation_id=invocation.invocation_id,
        )
