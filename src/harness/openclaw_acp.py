"""OpenClaw production backend using its Gateway-backed ACP server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from acp.schema import AllowedOutcome, RequestPermissionResponse

from deepresearch_cli.progress import ProgressReporter
from deepresearch_cli.search.registry import ProviderRegistry, ProviderRegistryError, load_search_environment

from .acp.launch import AcpLaunchSpec
from .acp.client import RecordingAcpClient
from .camofox_fallback import CamofoxFallbackSupport
from .acp_agent import AcpAgentAttemptRuntime, _shielded_runtime_close
from .protocol import AgentInvocation, HarnessError
from .search_mcp import SearchMcpSupport


_SEARCH_BRIDGE_OPERATIONS = {
    "list-search-domains",
    "list-search-sources",
    "start-domain-search",
    "get-search-batch",
    "batch-search",
    "search-results",
    "get-search-hit",
    "fetch-url",
}


def _tool_call_value(tool_call: Any, name: str) -> Any:
    if isinstance(tool_call, Mapping):
        return tool_call.get(name)
    return getattr(tool_call, name, None)


class _OpenClawAcpClient(RecordingAcpClient):
    """Allow only the exact per-attempt Search bridge exec command."""

    def __init__(
        self,
        *,
        allowed_contexts: Mapping[str, Path],
        workspace_root: Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._allowed_contexts = allowed_contexts
        self._workspace_root = workspace_root.resolve()

    def _authorized(self, tool_call: Any) -> bool:
        if _tool_call_value(tool_call, "kind") != "execute":
            return False
        raw = _tool_call_value(tool_call, "raw_input")
        if raw is None:
            raw = _tool_call_value(tool_call, "rawInput")
        command = raw.get("command") if isinstance(raw, Mapping) else None
        if not isinstance(command, str):
            return False
        if any(marker in command for marker in ("\n", "\r", ";", "|", "&", "`", "$(", ">", "<")):
            return False
        try:
            argv = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            return False
        prefix = [os.path.abspath(sys.executable), "-m", "deepresearch_cli.search.tool_cli", "--context"]
        if argv[:4] != prefix or len(argv) < 6:
            return False
        try:
            supplied_context = Path(argv[4]).expanduser().resolve()
        except OSError:
            return False
        allowed = {path.resolve() for path in self._allowed_contexts.values()}
        return supplied_context in allowed and argv[5] in _SEARCH_BRIDGE_OPERATIONS

    def _authorized_workspace_edit(self, tool_call: Any) -> bool:
        """Allow native file mutations only inside this CLI workspace."""
        kind = _tool_call_value(tool_call, "kind")
        if kind not in {"write", "edit", "apply_patch"}:
            return False
        raw = _tool_call_value(tool_call, "raw_input")
        if raw is None:
            raw = _tool_call_value(tool_call, "rawInput")
        if not isinstance(raw, Mapping):
            return False
        # write/edit expose a target path. apply_patch is accepted only when
        # the ACP payload includes an explicit target path as well.
        target = raw.get("file_path") or raw.get("path") or raw.get("filename")
        if not isinstance(target, str):
            return False
        try:
            path = Path(target).expanduser().resolve()
        except OSError:
            return False
        return path == self._workspace_root or self._workspace_root in path.parents

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,
        tool_call: Any,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        del kwargs
        if self._authorized(tool_call) or self._authorized_workspace_edit(tool_call):
            selected = next(
                (option for option in options if option.kind == "allow_once"), None
            )
            if selected is not None:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(
                        outcome="selected", option_id=selected.option_id
                    )
                )
        return await super().request_permission(
            options=options, session_id=session_id, tool_call=tool_call
        )


class OpenClawAcpAttemptRuntime(AcpAgentAttemptRuntime):
    """One OpenClaw ACP session, backed by an existing OpenClaw Gateway."""

    backend_name = "OpenClaw"

    def __init__(
        self,
        workspace: Path,
        *,
        openclaw_command: Optional[str] = None,
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
        if model:
            raise HarnessError(
                "OpenClaw ACP does not expose a per-session model override; "
                "select the model in the OpenClaw agent configuration"
            )
        supplied = openclaw_command or shutil.which("openclaw") or "openclaw"
        resolved = shutil.which(supplied) or (
            supplied if Path(supplied).expanduser().is_file() else None
        )
        self.openclaw_command = (
            str(Path(resolved).expanduser().resolve()) if resolved else supplied
        )
        self.openclaw_profile = profile
        self._search_contexts: dict[str, Path] = {}
        super().__init__(
            workspace,
            acp_command=self.openclaw_command,
            launch_backend="openclaw",
            process_prefix="openclaw-acp-process",
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
        self._client = _OpenClawAcpClient(
            allowed_contexts=self._search_contexts,
            workspace_root=self.workspace,
            raw_observer_enabled=True,
            event_observer=self._observe_session_event,
        )
        self.launch_spec = AcpLaunchSpec(
            backend="openclaw",
            command=self.openclaw_command,
            cwd=self.workspace,
            environment=self._acp_environment(),
            process_prefix="openclaw-acp-process",
        )

    def _acp_args(self, *tail: str) -> tuple[str, ...]:
        return ("acp", *tail)

    def _acp_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self.openclaw_profile:
            environment["OPENCLAW_PROFILE"] = self.openclaw_profile
        return environment

    def _supports_session_mcp(self) -> bool:
        return False

    def _supports_session_edit_mode(self) -> bool:
        # OpenClaw owns tool policy and workspace access at the Gateway/agent
        # layer; its ACP endpoint has no portable DeepResearch edit mode.
        return False

    def _search_mcp_server(
        self,
        *,
        identity: str,
        store_dir: Path,
        batch_timeout_seconds: Optional[float] = None,
        lease_file: Optional[Path] = None,
    ) -> tuple[str, None, Path]:
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
        url = spec.env.get("DEEPRESEARCH_SEARCH_COORDINATOR_URL")
        token = spec.env.get("DEEPRESEARCH_SEARCH_COORDINATOR_TOKEN")
        namespace = spec.env.get("DEEPRESEARCH_SEARCH_NAMESPACE")
        if not url or not token or not namespace:
            raise HarnessError(
                "OpenClaw search requires the run-scoped Search Coordinator"
            )
        context_path = (store_dir.parent / f".{spec.name}.openclaw.json").resolve()
        payload = {
            "schema_version": 1,
            "coordinator_url": url,
            "coordinator_token": token,
            "namespace": namespace,
            "lease_file": str(spec.lease_file),
            "camofox_enabled": self.camofox_fallback_enabled,
            "camofox_base_url": self.camofox_base_url or "http://127.0.0.1:9377",
        }
        descriptor = os.open(
            context_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, json.dumps(payload).encode("utf-8"))
        finally:
            os.close(descriptor)
        self._search_contexts[identity] = context_path
        return spec.name, None, spec.lease_file

    def _prompt_text(self, invocation: AgentInvocation) -> str:
        context = self._search_contexts.get(invocation.invocation_id)
        if context is None:
            return invocation.prompt
        base_argv = [
            os.path.abspath(sys.executable),
            "-m",
            "deepresearch_cli.search.tool_cli",
            "--context",
            str(context),
        ]
        command = (
            subprocess.list2cmdline(base_argv)
            if os.name == "nt"
            else shlex.join(base_argv)
        )
        instructions = f"""

## OpenClaw Search bridge
This Research attempt has no session-scoped MCP support. Use the native exec tool
to run the deterministic DeepResearch search bridge below. Do not use unrelated
browser/search tools. Every command emits JSON.

Base command:
`{command}`

Operations:
- `list-search-domains`
- `list-search-sources`
- `start-domain-search --searches '<JSON array>'`
- `get-search-batch <batch_id>`
- `batch-search --searches '<JSON array>'`
- `search-results --cursor 0 --limit 20 [--batch-id ID] [--provider NAME]`
- `get-search-hit <hit_id>`
- `fetch-url <public-http-url>`

Search results and snippets are discovery material. Call `fetch-url` for selected
HTML pages before treating them as evidence. If fetch reports Camofox unavailable,
switch to another source instead of retrying or blocking the workflow.
""".strip()
        return invocation.prompt + "\n\n" + instructions

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            for path in self._search_contexts.values():
                with contextlib.suppress(OSError):
                    path.unlink()
            self._search_contexts.clear()


@dataclass(frozen=True)
class OpenClawAcpBackendFactory:
    workspace: Path
    openclaw_command: Optional[str] = None
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

    def _runtime(self, expected_invocation_id: Optional[str] = None) -> OpenClawAcpAttemptRuntime:
        return OpenClawAcpAttemptRuntime(
            self.workspace,
            openclaw_command=self.openclaw_command,
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
                if expected_invocation_id is not None and self.search_coordinator is not None
                else None
            ),
            camofox_fallback_enabled=self.camofox_fallback_enabled,
            camofox_home=self.camofox_home,
            camofox_base_url=self.camofox_base_url,
            expected_invocation_id=expected_invocation_id,
        )

    async def _run(self, *args: str) -> tuple[int, str, str]:
        runtime = self._runtime()
        command = shutil.which(runtime.openclaw_command) or (
            runtime.openclaw_command if Path(runtime.openclaw_command).is_file() else None
        )
        if command is None:
            raise HarnessError(f"OpenClaw executable not found: {runtime.openclaw_command}")
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=runtime._acp_environment(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        return int(process.returncode or 0), stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def preflight(self) -> Mapping[str, Any]:
        version_code, version_out, version_err = await self._run("--version")
        if version_code:
            raise HarnessError(version_err or version_out or "openclaw --version failed")
        status_code, status_out, status_err = await self._run("status", "--json")
        if status_code:
            raise HarnessError(
                status_err or status_out or "OpenClaw Gateway status check failed"
            )
        report: dict[str, Any] = {
            "harness": "openclaw",
            "transport": "acp",
            "bridge": "openclaw-gateway",
            "version": (version_out or version_err).strip(),
            "gateway_status": (status_out or status_err).strip(),
            "profile": self.profile,
            "model": "configured-by-openclaw-agent",
            "ok": True,
        }
        workspace_permissions = await self._workspace_permissions()
        report["workspace_permissions"] = workspace_permissions
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
                    usable = registry.script_path(definition).is_file() and not registry.missing_modules(definition)
                except ProviderRegistryError:
                    usable = False
                available += int(usable)
            if available == 0:
                raise HarnessError("no configured search provider is runtime-available")
            report.update({
                "search_mcp": "openclaw-exec-bridge",
                "search_dir": str(search_dir),
                "search_provider_python": provider_python,
                "search_route_count": len(registry.definitions),
                "search_route_available_count": available,
            })
        else:
            report["search_mcp"] = "disabled"
        report.update(CamofoxFallbackSupport(
            enabled=self.camofox_fallback_enabled,
            home=self.camofox_home,
            base_url=self.camofox_base_url,
        ).report())
        return report

    async def ensure_timeout(self, seconds: Optional[float]) -> Mapping[str, Any]:
        """Ensure OpenClaw's provider watchdog is not shorter than the CLI target."""
        if seconds is None:
            return {"harness_timeout": "disabled"}
        target = max(1, int(seconds))
        provider = "custom-tokenhub-sensetime-com"
        path = f"models.providers.{provider}.timeoutSeconds"
        code, out, err = await self._run("config", "get", path)
        current: Optional[float] = None
        if code == 0:
            with contextlib.suppress(TypeError, ValueError):
                current = float((out or err).strip())
        if current is not None and current >= target:
            return {"harness_timeout": "ok", "harness_timeout_seconds": current}
        set_code, set_out, set_err = await self._run(
            "config", "set", path, str(target), "--strict-json"
        )
        if set_code:
            raise HarnessError(
                "OpenClaw provider timeout could not be adjusted: "
                f"{set_err or set_out or path}"
            )
        restart_code, restart_out, restart_err = await self._run(
            "gateway", "restart"
        )
        if restart_code:
            raise HarnessError(
                "OpenClaw Gateway restart failed after timeout adjustment: "
                f"{restart_err or restart_out or 'unknown error'}"
            )
        return {
            "harness_timeout": "adjusted",
            "harness_timeout_seconds": target,
            "harness_timeout_previous": current,
            "harness_timeout_reloaded": True,
        }

    async def _workspace_permissions(self) -> Mapping[str, Any]:
        """Validate the non-interactive Agent workspace contract before a Run."""

        agents_code, agents_out, agents_err = await self._run("agents", "list", "--json")
        if agents_code:
            raise HarnessError(agents_err or agents_out or "OpenClaw agent list failed")
        try:
            agents = json.loads(agents_out)
        except (TypeError, ValueError) as exc:
            raise HarnessError(f"OpenClaw agent list is not valid JSON: {agents_out}") from exc
        if not isinstance(agents, list) or not agents:
            raise HarnessError("OpenClaw has no configured Agent for DeepResearch")
        selected = next((a for a in agents if isinstance(a, Mapping) and a.get("isDefault")), None)
        selected = selected or next((a for a in agents if isinstance(a, Mapping)), None)
        agent_id = str(selected.get("id") or "").strip() if isinstance(selected, Mapping) else ""
        if not agent_id:
            raise HarnessError("OpenClaw agent list has no usable Agent id")
        prefix = f"agents.entries.{agent_id}"

        def parse_config(path: str, output: str, error: str) -> Any:
            try:
                value = json.loads(output)
            except (TypeError, ValueError) as exc:
                raise HarnessError(
                    f"OpenClaw config {path} is not valid JSON: {error or output}"
                ) from exc
            if isinstance(value, Mapping) and value.get("ok") is False:
                raise HarnessError(
                    f"OpenClaw config {path} is unavailable: "
                    f"{value.get('error', {}).get('message', 'unknown error')}"
                )
            return value

        sandbox_code, sandbox_out, sandbox_err = await self._run(
            "config", "get", f"{prefix}.sandbox", "--json"
        )
        if sandbox_code:
            raise HarnessError(
                f"OpenClaw Agent {agent_id} workspace permission check failed. "
                f"Set {prefix}.sandbox.workspaceAccess to rw (or use "
                "an unsandboxed workspace), then retry."
            )
        sandbox = parse_config(
            f"{prefix}.sandbox", sandbox_out, sandbox_err
        )
        if not isinstance(sandbox, Mapping):
            raise HarnessError(
                f"OpenClaw {prefix}.sandbox must be an object with "
                "workspaceAccess=rw or mode=off"
            )
        workspace_access = sandbox.get("workspaceAccess")
        sandbox_mode = sandbox.get("mode")
        writable_workspace = workspace_access == "rw" or sandbox_mode == "off"
        if not writable_workspace:
            raise HarnessError(
                f"OpenClaw Agent {agent_id} workspace is not writable. Set "
                f"{prefix}.sandbox.workspaceAccess to rw, or use "
                "mode=off for a trusted unsandboxed workspace."
            )

        tools_code, tools_out, tools_err = await self._run(
            "config", "get", f"{prefix}.tools", "--json"
        )
        if tools_code:
            raise HarnessError(
                f"OpenClaw Agent {agent_id} tool permission check failed. "
                "Allow read, write, edit, apply_patch, exec and process for "
                "the Agent before starting DeepResearch."
            )
        tools = parse_config(f"{prefix}.tools", tools_out, tools_err)
        if not isinstance(tools, Mapping):
            raise HarnessError(
                f"OpenClaw {prefix}.tools must be an object with an allow list"
            )
        allowed = {
            item
            for key in ("allow", "alsoAllow")
            for item in tools.get(key, [])
            if isinstance(item, str)
        }
        denied = {
            item for item in tools.get("deny", []) if isinstance(item, str)
        }
        required = {"read", "write", "edit", "apply_patch", "exec", "process"}
        missing = sorted(required - allowed)
        blocked = sorted(required & denied)
        if missing or blocked:
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if blocked:
                details.append("denied=" + ",".join(blocked))
            raise HarnessError(
                f"OpenClaw Agent {agent_id} lacks DeepResearch tools (" + "; ".join(details) + "). "
                f"Update {prefix}.tools before starting DeepResearch."
            )
        return {
            "agent": agent_id,
            "workspace_access": "rw" if workspace_access == "rw" else "host",
            "sandbox_mode": sandbox_mode,
            "required_tools": sorted(required),
        }

    async def probe(self) -> Mapping[str, Any]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        probe_dir = Path(tempfile.mkdtemp(prefix=".openclaw-write-probe-", dir=self.workspace))
        marker = probe_dir / "write-probe.txt"
        invocation_id = "openclaw-write-probe-" + uuid.uuid4().hex[:10]
        runtime = self._runtime(expected_invocation_id=invocation_id)
        try:
            await runtime.start()
            result = await runtime.invoke(AgentInvocation(
                invocation_id=invocation_id,
                run_id="preflight",
                node_instance_id="openclaw-write-probe",
                node_type="preflight",
                attempt=1,
                workspace=probe_dir,
                input_artifact_refs=[],
                resolved_input_artifacts=[],
                timeout_seconds=60,
                agent_context={},
                prompt=(
                    "Use the native write tool to create exactly this UTF-8 text file: "
                    f"{marker}. Its complete content must be: deepresearch-write-ok. "
                    "Do not use exec, shell commands, redirection, or scripts."
                ),
                allow_workspace_edits=True,
            ))
            if result.status != "succeeded" or not marker.is_file() or marker.read_text(encoding="utf-8").strip() != "deepresearch-write-ok":
                raise HarnessError(
                    "OpenClaw ACP write smoke test failed: the child session could not "
                    "create a file in the current Run workspace. Check the selected "
                    "Agent's write/edit tool exposure and permission flow before retrying."
                )
            return {
                "acp_initialize": "ok",
                "model_check": "workspace-write-ok",
                "search_mcp": (
                    "openclaw-exec-bridge" if self.search_mcp_enabled else "disabled"
                ),
            }
        finally:
            await _shielded_runtime_close(runtime)
            shutil.rmtree(probe_dir, ignore_errors=True)

    def create(self, invocation: AgentInvocation) -> OpenClawAcpAttemptRuntime:
        return self._runtime(expected_invocation_id=invocation.invocation_id)
