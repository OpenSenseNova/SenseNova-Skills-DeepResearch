"""Shared ACP attempt runtime plus the Hermes-specific adapter."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import acp
from acp.schema import (
    ClientCapabilities,
    EnvVariable,
    Implementation,
    McpServerStdio,
    TextContentBlock,
)
from acp.stdio import spawn_agent_process

from deepresearch_cli import __version__
from deepresearch_cli.progress import ProgressReporter
from deepresearch_cli.search.registry import (
    ProviderRegistry,
    ProviderRegistryError,
    load_search_environment,
)
from deepresearch_cli.search.paths import builtin_search_dir

from .acp.client import RecordingAcpClient
from .acp.launch import AcpLaunchSpec
from .acp.runtime import AcpAttemptRuntime
from .camofox_fallback import CamofoxFallbackSupport
from .protocol import AgentExecutionResult, AgentInvocation, HarnessError
from .search_mcp import SearchMcpSupport


_PROCESS_STDERR_FILE_LIMIT_BYTES = 10_000_000
# ACP frames are newline-delimited JSON. Hermes can legitimately emit a single
# frame larger than asyncio's 64 KiB StreamReader default when a tool result or
# file edit carries substantial content. Keep a bounded but practical ceiling
# so one large frame cannot tear down its attempt's ACP connection.
_ACP_STDOUT_STREAM_LIMIT_BYTES = 16_000_000

_SEARCH_TRANSPORT_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


_RecordingAcpClient = RecordingAcpClient


def resolve_hermes_profile_home(profile: Optional[str] = None) -> Path:
    """Resolve Hermes' active profile directory without starting Hermes."""

    configured = Path(
        os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    ).expanduser()
    configured_resolved = configured.resolve()
    configured_is_named_profile = configured.parent.name == "profiles"
    if sys.platform == "win32":  # pragma: no cover - Windows path semantics.
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        native_root = (
            Path(local_appdata) / "hermes"
            if local_appdata
            else Path.home() / "AppData" / "Local" / "hermes"
        ).resolve()
    else:
        native_root = (Path.home() / ".hermes").resolve()
    if configured_is_named_profile:
        root = configured.parent.parent.resolve()
    else:
        try:
            configured_resolved.relative_to(native_root)
        except ValueError:
            root = configured_resolved
        else:
            root = native_root
    if profile:
        profile_name = profile.strip().lower()
        return root if profile_name == "default" else (root / "profiles" / profile_name).resolve()
    if configured_is_named_profile:
        return configured_resolved
    try:
        active_profile = (root / "active_profile").read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeDecodeError):
        active_profile = ""
    return (root / "profiles" / active_profile).resolve() if active_profile and active_profile != "default" else root


class AcpAgentAttemptRuntime(AcpAttemptRuntime):
    """Backend-neutral ACP lifecycle for one disposable Agent attempt.

    Concrete harness adapters only define how their ACP process is launched,
    which edit mode they expose, and how run-scoped search is attached.
    """

    backend_name = "ACP agent"

    def __init__(
        self,
        workspace: Path,
        acp_command: str,
        launch_backend: str,
        process_prefix: str,
        profile: Optional[str] = None,
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
        self.workspace = workspace.resolve()
        supplied_command = acp_command
        resolved_command = shutil.which(supplied_command)
        if resolved_command is not None:
            resolved_command = str(Path(resolved_command).expanduser().resolve())
        if resolved_command is None:
            candidate = Path(supplied_command).expanduser()
            resolved_command = str(candidate.resolve()) if candidate.is_file() else None
        self.acp_command = resolved_command or supplied_command
        AcpAttemptRuntime.__init__(
            self,
            launch_spec=AcpLaunchSpec(
                backend=launch_backend,
                command=self.acp_command,
                cwd=self.workspace,
                process_prefix=process_prefix,
            ),
            progress_reporter=progress_reporter,
            expected_invocation_id=expected_invocation_id,
        )
        self.profile = profile
        self.startup_timeout_seconds = startup_timeout_seconds
        self.search_mcp_enabled = search_mcp_enabled
        self.search_dir = search_dir
        self.search_provider_python = search_provider_python
        if (
            isinstance(search_provider_limit, bool)
            or not isinstance(search_provider_limit, int)
            or not 1 <= search_provider_limit <= 50
        ):
            raise ValueError("search_provider_limit must be an integer between 1 and 50")
        self.search_provider_limit = search_provider_limit
        self.search_support = search_support
        self.camofox_support = CamofoxFallbackSupport(
            enabled=camofox_fallback_enabled,
            home=camofox_home,
            base_url=camofox_base_url,
        )
        self.camofox_fallback_enabled = camofox_fallback_enabled
        self.camofox_base_url = camofox_base_url
        self.expected_invocation_id = expected_invocation_id
        self._progress_reporter = progress_reporter
        self._session_invocations: Dict[str, AgentInvocation] = {}
        self._client = _RecordingAcpClient(
            raw_observer_enabled=True,
            event_observer=self._observe_session_event,
        )
        self._transport_context = None
        self._connection = None
        self._process = None
        self._process_instance_id: Optional[str] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._stderr_chunks: List[str] = []
        self._stderr_base_offset = 0
        self._stderr_end_offset = 0
        self._stderr_activity = asyncio.Event()
        self._process_stderr_path: Optional[Path] = None
        self._process_stderr_ref: Optional[str] = None
        self._process_stderr_workspace: Optional[Path] = None
        self._process_stderr_bytes_written = 0
        self._process_stderr_truncated = False
        self._process_stderr_file_limit_bytes = _PROCESS_STDERR_FILE_LIMIT_BYTES
        self._active_sessions: Dict[str, str] = {}
        self._search_lease_files: Dict[str, Path] = {}
        self._search_probe_dirs: List[Path] = []
        self._started = False
        self._closed = False
        self._invocation_claimed = False
        self._cancel_requested = False

    async def __aenter__(self) -> "AcpAgentAttemptRuntime":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def preflight(self) -> Mapping[str, Any]:
        command = shutil.which(self.acp_command) or (
            self.acp_command if Path(self.acp_command).is_file() else None
        )
        if not command:
            raise HarnessError(
                f"{self.backend_name} executable not found: {self.acp_command}"
            )

        check = await self._run_command(command, *self._acp_args("--check"))
        if check[0] != 0:
            raise HarnessError(f"hermes acp --check failed: {check[2] or check[1]}")
        version = await self._run_command(command, "--version")
        if version[0] != 0:
            raise HarnessError(
                "hermes --version failed: %s" % (version[2] or version[1])
            )
        report = {
            "harness": "hermes",
            "command": str(Path(command).resolve()),
            "acp_check": check[1].strip(),
            "version": (version[1] or version[2]).strip(),
            "profile": self.profile,
            "ok": True,
        }
        if self.search_mcp_enabled:
            search_dir = self._resolved_search_dir()
            provider_python = self._resolved_search_provider_python()
            try:
                python_check = await self._run_command(
                    provider_python,
                    "-c",
                    "print('DEEPRESEARCH_PROVIDER_PYTHON_OK')",
                )
            except (OSError, HarnessError) as exc:
                raise HarnessError(
                    "search provider Python is not executable: "
                    f"{provider_python}: {exc}"
                ) from exc
            if (
                python_check[0] != 0
                or python_check[1].strip() != "DEEPRESEARCH_PROVIDER_PYTHON_OK"
            ):
                raise HarnessError(
                    "search provider Python executable check failed: "
                    f"{provider_python} exited with {python_check[0]}: "
                    f"{python_check[2] or python_check[1]}"
                )

            profile_env = self._hermes_profile_home() / ".env"
            registry = ProviderRegistry(
                search_dir=search_dir,
                python_executable=provider_python,
                environment=load_search_environment(
                    search_dir,
                    profile_env_file=(profile_env if profile_env.is_file() else None),
                ),
            )
            route_statuses: list[dict[str, Any]] = []
            for definition in registry.definitions:
                script_available = False
                unavailable_reason: Optional[str] = None
                try:
                    script_available = registry.script_path(definition).is_file()
                except ProviderRegistryError as exc:
                    unavailable_reason = str(exc)
                missing_modules = (
                    registry.missing_modules(definition)
                    if script_available
                    else ()
                )
                runtime_available = script_available and not missing_modules
                if not script_available and unavailable_reason is None:
                    unavailable_reason = f"missing script: {definition.script}"
                elif missing_modules:
                    unavailable_reason = (
                        "provider Python is missing modules: "
                        + ", ".join(missing_modules)
                    )
                route_statuses.append(
                    {
                        "provider": definition.name,
                        "source_file": definition.source_file.name,
                        "script": definition.script,
                        "script_available": script_available,
                        "required_modules": list(definition.required_modules),
                        "missing_modules": list(missing_modules),
                        "runtime_available": runtime_available,
                        "unavailable_reason": unavailable_reason,
                    }
                )
            runtime_available_count = sum(
                1 for route in route_statuses if route["runtime_available"]
            )
            if runtime_available_count == 0:
                raise HarnessError(
                    f"none of the {len(route_statuses)} configured search sources "
                    "is runtime-available; "
                    f"search_dir={search_dir}; provider_python={provider_python}"
                )
            report.update(
                {
                    "search_mcp": "configured",
                    "search_dir": str(search_dir),
                    "search_provider_python": provider_python,
                    "search_provider_limit": self.search_provider_limit,
                    "search_provider_python_check": "ok",
                    "search_route_count": len(route_statuses),
                    "search_route_available_count": runtime_available_count,
                    "search_route_unavailable_count": (
                        len(route_statuses) - runtime_available_count
                    ),
                    "search_routes": route_statuses,
                }
            )
        else:
            report["search_mcp"] = "disabled"
        report.update(self.camofox_support.report())
        return report

    def _resolved_search_dir(self) -> Path:
        selected = self.search_dir
        if selected is None:
            env_value = os.environ.get("DEEPRESEARCH_SEARCH_DIR")
            selected = (
                Path(env_value)
                if env_value
                else builtin_search_dir()
            )
        resolved = selected.expanduser().resolve()
        if not resolved.is_dir():
            raise HarnessError(
                "search registry is unavailable: %s; pass --search-dir"
                % resolved
            )
        return resolved

    def _hermes_profile_home(self) -> Path:
        return resolve_hermes_profile_home(self.profile)

    def _resolved_search_provider_python(self) -> str:
        supplied = (
            self.search_provider_python
            or os.environ.get("DEEPRESEARCH_SEARCH_PROVIDER_PYTHON")
            or sys.executable
        )
        resolved = shutil.which(supplied)
        if resolved is None:
            candidate = Path(supplied).expanduser()
            resolved = str(candidate.absolute()) if candidate.is_file() else None
        if resolved is None:
            raise HarnessError(
                f"search provider Python executable not found: {supplied}"
            )
        # Preserve a virtual-environment interpreter symlink. Resolving it to
        # the base CPython binary would silently lose that environment's
        # site-packages.
        return os.path.abspath(os.path.expanduser(resolved))

    @staticmethod
    def _safe_search_server_name(identity: str) -> str:
        # Hermes exposes MCP tools as ``mcp__<server>__<tool>``. The fixed
        # short prefix plus an 80-bit random digest keeps even the longest
        # decorated search-tool name well below Hermes' 64-character limit.
        material = f"{identity}:{uuid.uuid4().hex}".encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:20]
        return f"drs_{digest}"

    @staticmethod
    def _search_batch_timeout_seconds(
        remaining_invocation_seconds: Optional[float],
    ) -> float:
        if remaining_invocation_seconds is None:
            return 120.0
        if remaining_invocation_seconds <= 0:
            raise HarnessError("Research invocation budget expired before MCP setup")
        if remaining_invocation_seconds <= 0.1:
            return remaining_invocation_seconds / 2
        # Preserve time for MCP result serialization, model processing, and
        # the final ACP response. The MCP enforces its own budget because an
        # ACP session cancellation is not a process-tree termination contract.
        margin = min(5.0, max(0.25, remaining_invocation_seconds * 0.1))
        return min(
            120.0,
            remaining_invocation_seconds,
            max(0.05, remaining_invocation_seconds - margin),
        )

    @staticmethod
    def _create_search_lease(path: Path) -> Path:
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                resolved,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            # A caller may create the explicit lease before constructing the
            # MCP descriptor. Its existence, rather than its contents, is the
            # cooperative-liveness signal.
            return resolved
        try:
            os.write(descriptor, b"active\n")
        finally:
            os.close(descriptor)
        return resolved

    def _search_mcp_server(
        self,
        *,
        identity: str,
        store_dir: Path,
        batch_timeout_seconds: Optional[float] = None,
        lease_file: Optional[Path] = None,
    ) -> tuple[str, McpServerStdio, Path]:
        profile_env = self._hermes_profile_home() / ".env"
        support = self.search_support or SearchMcpSupport(
            search_dir=self.search_dir,
            provider_python=self.search_provider_python,
            provider_limit=self.search_provider_limit,
            profile_env_file=profile_env if profile_env.is_file() else None,
            camofox_fallback_enabled=self.camofox_fallback_enabled,
            camofox_base_url=self.camofox_base_url,
        )
        spec = support.build(
            identity=identity,
            store_dir=store_dir,
            batch_timeout_seconds=(
                batch_timeout_seconds if batch_timeout_seconds is not None else 120.0
            ),
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
        if self._connection is None:
            raise HarnessError("Hermes ACP connection is unavailable")
        self._client.reset_session(session_id)
        response = await asyncio.wait_for(
            self._connection.prompt(
                prompt=[TextContentBlock(type="text", text="/tools")],
                session_id=session_id,
                message_id=f"search-mcp-tools-{uuid.uuid4().hex}",
            ),
            timeout=timeout_seconds,
        )
        text = "".join(self._client.messages[session_id])
        expected = [
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
        missing_from_listing = [tool for tool in expected if tool not in text]
        # Hermes v0.19.1 can defer dynamically registered MCP tools behind
        # tool_search. Its /tools command rebuilds only the directly visible
        # surface, so a healthy MCP server may be absent from that text even
        # though Hermes discovered and injected every tool into the session.
        # The server name is random per probe, which makes the matching native
        # registration diagnostic specific to this exact ACP session.
        stderr = self._stderr_text_since(max(0, self._stderr_end_offset - 20_000))
        registration_marker = f"MCP server '{server_name}' (stdio): registered"
        registered_in_native_surface = (
            registration_marker in stderr
            and all(tool in stderr for tool in expected)
        )
        self._client.reset_session(session_id)
        if response.stop_reason != "end_turn" or (
            missing_from_listing and not registered_in_native_surface
        ):
            detail = stderr[-4_000:] if stderr else f"no {self.backend_name} stderr was emitted"
            raise HarnessError(
                f"{self.backend_name} did not register the DeepResearch search MCP tools; "
                f"missing={missing_from_listing}; diagnostics={detail}"
            )
        return expected

    async def _verify_native_search_registration(
        self,
        *,
        server_name: str,
        timeout_seconds: float,
    ) -> list[str]:
        """Verify MCP discovery without consuming the real Agent session.

        Hermes v0.20 may stop a session-scoped stdio MCP process after an
        initial ``/tools`` prompt completes.  A Research invocation must use
        its first prompt for the actual node work, so wait for Hermes' native
        registration diagnostic instead of probing the session with a turn.
        """

        expected = [
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
        registration_marker = f"MCP server '{server_name}' (stdio): registered"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            stderr = self._stderr_text_since(
                max(0, self._stderr_end_offset - 20_000)
            )
            if registration_marker in stderr and all(
                tool in stderr for tool in expected
            ):
                return expected
            if self._process is not None and self._process.returncode is not None:
                raise HarnessError(
                    f"{self.backend_name} exited before registering the DeepResearch search MCP "
                    f"tools; diagnostics={stderr[-4_000:] or f'no {self.backend_name} stderr was emitted'}"
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise HarnessError(
                    f"{self.backend_name} did not register the DeepResearch search MCP tools "
                    f"within {timeout_seconds}s; diagnostics="
                    f"{stderr[-4_000:] or f'no {self.backend_name} stderr was emitted'}"
                )
            self._stderr_activity.clear()
            try:
                await asyncio.wait_for(
                    self._stderr_activity.wait(), timeout=min(remaining, 0.1)
                )
            except asyncio.TimeoutError:
                pass

    async def check_search_mcp(self) -> Mapping[str, Any]:
        """Verify the real Hermes MCP client without making a model request."""

        if not self.search_mcp_enabled:
            return {"search_mcp": "disabled", "search_mcp_tools": []}
        if not self._started or self._connection is None:
            raise HarnessError("Hermes ACP harness has not been started")
        probe_dir = Path(
            tempfile.mkdtemp(prefix=".search-mcp-probe-", dir=self.workspace)
        ).resolve()
        self._search_probe_dirs.append(probe_dir)
        server_name, descriptor, lease_file = self._search_mcp_server(
            identity="probe",
            store_dir=probe_dir / "search",
            lease_file=probe_dir / "search-mcp.lease",
        )
        try:
            session = await asyncio.wait_for(
                self._connection.new_session(
                    cwd=str(probe_dir), mcp_servers=[descriptor]
                ),
                timeout=self.startup_timeout_seconds,
            )
            tools = await self._verify_search_tools(
                session_id=session.session_id,
                server_name=server_name,
                timeout_seconds=self.startup_timeout_seconds,
            )
        finally:
            with contextlib.suppress(OSError):
                lease_file.unlink()
        return {
            "search_mcp": "ok",
            "search_mcp_tools": [tool.rsplit("__", 1)[-1] for tool in tools],
        }

    def _acp_args(self, *tail: str) -> tuple[str, ...]:
        prefix = ("--profile", self.profile) if self.profile else ()
        return (*prefix, "acp", *tail)

    def _acp_environment(self) -> dict[str, str]:
        return dict(os.environ)

    def _workspace_edit_mode(self) -> str:
        return "accept_edits"

    def _supports_session_mcp(self) -> bool:
        return True

    def _supports_session_edit_mode(self) -> bool:
        return True

    def _prompt_text(self, invocation: AgentInvocation) -> str:
        return invocation.prompt

    async def _run_command(self, *argv: str) -> tuple[int, str, str]:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(os.environ),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.startup_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(process.wait(), timeout=2.0)
            raise HarnessError(f"command timed out: {' '.join(argv)}") from exc
        except asyncio.CancelledError:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(
                    asyncio.TimeoutError, asyncio.CancelledError, Exception
                ):
                    await asyncio.shield(
                        asyncio.wait_for(process.wait(), timeout=2.0)
                    )
            raise
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise HarnessError(
                f"{self.backend_name} attempt runtime has already been closed"
            )
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._process_instance_id = "%s-%s" % (
            self.launch_spec.process_prefix,
            uuid.uuid4().hex,
        )
        self._stderr_chunks = []
        self._stderr_base_offset = 0
        self._stderr_end_offset = 0
        self._stderr_activity.clear()
        self._process_stderr_path = None
        self._process_stderr_ref = None
        self._process_stderr_workspace = None
        self._process_stderr_bytes_written = 0
        self._process_stderr_truncated = False
        self._transport_context = spawn_agent_process(
            self._client,
            self.launch_spec.command,
            *self._acp_args(),
            env=self._acp_environment(),
            cwd=self.workspace,
            transport_kwargs={"limit": _ACP_STDOUT_STREAM_LIMIT_BYTES},
            use_unstable_protocol=True,
            observers=[self._client.observe_stream],
        )
        try:
            self._connection, self._process = await asyncio.wait_for(
                self._transport_context.__aenter__(),
                timeout=self.startup_timeout_seconds,
            )
            if self._process.stderr is not None:
                self._stderr_task = asyncio.create_task(self._drain_stderr())
            response = await asyncio.wait_for(
                self._connection.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(terminal=False),
                    client_info=Implementation(
                        name="deepresearch-cli",
                        title="DeepResearch-CLI",
                        version=__version__,
                    ),
                ),
                timeout=self.startup_timeout_seconds,
            )
            if response.protocol_version != acp.PROTOCOL_VERSION:
                raise HarnessError(
                    f"{self.backend_name} ACP protocol mismatch: "
                    f"client={acp.PROTOCOL_VERSION}, agent={response.protocol_version}"
                )
        except BaseException:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(self.close())
            raise
        self._started = True

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            chunk = await self._process.stderr.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            self._append_stderr(text)

    async def invoke(self, invocation: AgentInvocation) -> AgentExecutionResult:
        if not self._started or self._connection is None:
            raise HarnessError(f"{self.backend_name} ACP harness has not been started")
        if self._invocation_claimed:
            raise HarnessError(
                f"one {self.backend_name} attempt runtime cannot serve multiple invocations"
            )
        if (
            self.expected_invocation_id is not None
            and invocation.invocation_id != self.expected_invocation_id
        ):
            raise HarnessError(
                f"{self.backend_name} attempt runtime received an unexpected invocation id"
            )
        # Set this before the first await so concurrent callers cannot both
        # claim the same native process.
        self._invocation_claimed = True
        self._notify_invocation_started(invocation)
        # Use an absolute cursor into a bounded per-process stderr buffer.
        stderr_start_offset = self._stderr_end_offset
        self._stderr_activity.clear()
        session_id: Optional[str] = None
        native_process_id = getattr(self._process, "pid", None)
        native_process_instance_id = self._process_instance_id
        native_process_stderr_path: Optional[str] = None
        loop = asyncio.get_running_loop()
        invocation_deadline = (
            None
            if invocation.timeout_seconds is None
            else loop.time() + invocation.timeout_seconds
        )
        progress_status = "failed"
        search_lease_file: Optional[Path] = None

        def remaining_invocation_seconds() -> float:
            assert invocation_deadline is not None
            return max(0.0, invocation_deadline - loop.time())

        async def await_with_invocation_budget(awaitable: Any) -> Any:
            if invocation_deadline is None:
                return await awaitable
            return await asyncio.wait_for(
                awaitable, timeout=remaining_invocation_seconds()
            )

        try:
            native_process_stderr_path = self._bind_process_stderr_log(invocation)
            search_server_name: Optional[str] = None
            mcp_servers: List[McpServerStdio] = []
            if self.search_mcp_enabled and invocation.node_type == "research":
                search_store_dir = invocation.workspace.parent / "search"
                (
                    search_server_name,
                    search_descriptor,
                    search_lease_file,
                ) = self._search_mcp_server(
                    identity=invocation.invocation_id,
                    store_dir=search_store_dir,
                    batch_timeout_seconds=self._search_batch_timeout_seconds(
                        None
                        if invocation_deadline is None
                        else remaining_invocation_seconds()
                    ),
                )
                self._search_lease_files[invocation.invocation_id] = (
                    search_lease_file
                )
                if search_descriptor is not None:
                    mcp_servers.append(search_descriptor)
            prepare = getattr(self, "_prepare_session_environment", None)
            if prepare is not None:
                prepare(invocation, mcp_servers)
            session = await await_with_invocation_budget(
                self._connection.new_session(
                    cwd=str(invocation.workspace),
                    mcp_servers=(mcp_servers if self._supports_session_mcp() else None),
                )
            )
            session_id = session.session_id
            self._active_sessions[invocation.invocation_id] = session_id
            self._session_invocations[session_id] = invocation
            self._client.reset_session(session_id)
            if search_server_name is not None and self._supports_session_mcp():
                await await_with_invocation_budget(
                    self._verify_native_search_registration(
                        server_name=search_server_name,
                        timeout_seconds=self.startup_timeout_seconds,
                    )
                )
            if invocation.allow_workspace_edits and self._supports_session_edit_mode():
                set_mode = getattr(self._connection, "set_session_mode", None)
                if set_mode is None:
                    raise HarnessError(
                        f"{self.backend_name} ACP does not support workspace-scoped edit mode"
                    )
                mode_response = await await_with_invocation_budget(
                    set_mode(
                        mode_id=self._workspace_edit_mode(),
                        session_id=session_id,
                    )
                )
                if mode_response is None:
                    raise HarnessError(
                        f"{self.backend_name} rejected workspace-scoped edit mode"
                    )
            response = await await_with_invocation_budget(
                self._connection.prompt(
                    prompt=[TextContentBlock(type="text", text=self._prompt_text(invocation))],
                    session_id=session_id,
                    message_id=invocation.invocation_id,
                )
            )
            response_text = "".join(self._client.messages[session_id]).strip()
            usage = (
                response.usage.model_dump(mode="json", by_alias=True, exclude_none=True)
                if response.usage is not None
                else None
            )
            stderr = await self._settled_stderr_text(stderr_start_offset)
            agent_error_signature = "Agent error in session %s" % session_id
            session_agent_failed = (
                response.stop_reason == "end_turn"
                and response_text.lstrip().startswith("Error:")
                and agent_error_signature in stderr
            )
            provider_stream_failed = (
                response.stop_reason == "end_turn"
                and response_text.lstrip().startswith("API call failed after ")
                and "API call failed after " in stderr
            )
            backend_agent_failed = session_agent_failed or provider_stream_failed
            if backend_agent_failed:
                status = "failed"
                if provider_stream_failed:
                    error = response_text.strip() or (
                        f"{self.backend_name} model-provider stream failed; "
                        "see stderr diagnostics"
                    )
                else:
                    error = (
                        f"{self.backend_name} agent failed inside the ACP session; "
                        "see raw-response and stderr diagnostics"
                    )
            elif response.stop_reason == "end_turn":
                status = "succeeded"
                error = None
            elif response.stop_reason == "cancelled":
                status = "cancelled"
                error = f"{self.backend_name} cancelled the prompt"
            else:
                status = "failed"
                error = f"{self.backend_name} stopped the prompt with {response.stop_reason}"
            progress_status = status
            return AgentExecutionResult(
                status=status,
                response_text=response_text,
                native_process_id=native_process_id,
                native_process_instance_id=native_process_instance_id,
                native_process_stderr_path=native_process_stderr_path,
                native_session_id=session_id,
                stop_reason=response.stop_reason,
                events=list(self._client.events[session_id]),
                stderr=stderr,
                usage=usage,
                error=error,
            )
        except asyncio.TimeoutError:
            progress_status = "cancelled"
            if search_lease_file is not None:
                with contextlib.suppress(OSError):
                    search_lease_file.unlink()
            if session_id is not None:
                await self._cancel_session_id(session_id)
            return AgentExecutionResult(
                status="cancelled",
                native_process_id=native_process_id,
                native_process_instance_id=native_process_instance_id,
                native_process_stderr_path=native_process_stderr_path,
                native_session_id=session_id,
                stop_reason="cancelled",
                events=list(self._client.events.get(session_id or "", [])),
                stderr=await self._settled_stderr_text(stderr_start_offset),
                error=(
                    f"{self.backend_name} invocation timed out after "
                    f"{invocation.timeout_seconds}s"
                ),
                failure_kind="timeout",
            )
        except asyncio.CancelledError:
            progress_status = "cancelled"
            if search_lease_file is not None:
                with contextlib.suppress(OSError):
                    search_lease_file.unlink()
            if session_id is not None:
                await asyncio.shield(self._cancel_session_id(session_id))
            raise
        except Exception as exc:
            progress_status = "failed"
            return AgentExecutionResult(
                status="failed",
                native_process_id=native_process_id,
                native_process_instance_id=native_process_instance_id,
                native_process_stderr_path=native_process_stderr_path,
                native_session_id=session_id,
                events=list(self._client.events.get(session_id or "", [])),
                stderr=await self._settled_stderr_text(stderr_start_offset),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._active_sessions.pop(invocation.invocation_id, None)
            self._search_lease_files.pop(invocation.invocation_id, None)
            if session_id is not None:
                self._session_invocations.pop(session_id, None)
            if search_lease_file is not None:
                with contextlib.suppress(OSError):
                    search_lease_file.unlink()
            self._notify_invocation_finished(invocation, progress_status)
            # Hermes v0.19 advertises no close-session capability and does not
            # implement session/close. Sessions are process-scoped and disappear
            # when this Execution Session closes its ACP subprocess.

    def _observe_session_event(
        self, session_id: str, event: Mapping[str, Any]
    ) -> None:
        invocation = self._session_invocations.get(session_id)
        if invocation is None or self._progress_reporter is None:
            return
        try:
            self._progress_reporter.acp_event(invocation, event)
        except Exception:
            # Observability must not alter Harness execution semantics.
            pass

    def _notify_invocation_started(self, invocation: AgentInvocation) -> None:
        if self._progress_reporter is None:
            return
        try:
            self._progress_reporter.invocation_started(invocation)
        except Exception:
            pass

    def _notify_invocation_finished(
        self, invocation: AgentInvocation, status: str
    ) -> None:
        if self._progress_reporter is None:
            return
        try:
            self._progress_reporter.invocation_finished(invocation, status)
        except Exception:
            pass

    async def cancel(self, invocation_id: str) -> None:
        if (
            self.expected_invocation_id is not None
            and invocation_id != self.expected_invocation_id
        ):
            return
        self._cancel_requested = True
        search_lease_file = self._search_lease_files.pop(invocation_id, None)
        if search_lease_file is not None:
            with contextlib.suppress(OSError):
                search_lease_file.unlink()
        if self._connection is None:
            return
        session_id = self._active_sessions.get(invocation_id)
        if session_id is not None:
            await self._cancel_session_id(session_id)

    async def _cancel_session_id(self, session_id: str) -> None:
        if self._connection is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(
                self._connection.cancel(session_id=session_id), timeout=2.0
            )

    def _append_stderr(self, text: str) -> None:
        if not text:
            return
        self._stderr_chunks.append(text)
        self._stderr_end_offset += len(text)
        self._stderr_activity.set()
        if self._process_stderr_path is not None:
            self._persist_process_stderr(text)
        while self._stderr_end_offset - self._stderr_base_offset > 2_000_000:
            removed = self._stderr_chunks.pop(0)
            self._stderr_base_offset += len(removed)

    def _bind_process_stderr_log(self, invocation: AgentInvocation) -> str:
        invocation_workspace = invocation.workspace.expanduser().resolve()
        if (
            invocation_workspace != self.workspace
            and self.workspace not in invocation_workspace.parents
        ):
            raise HarnessError("invocation workspace escapes Harness workspace")
        if invocation_workspace == self.workspace:
            run_workspace = self.workspace
        else:
            first_component = invocation_workspace.relative_to(self.workspace).parts[0]
            run_workspace = self.workspace / first_component
        if self._process_stderr_ref is not None:
            if run_workspace != self._process_stderr_workspace:
                raise HarnessError(
                    f"one {self.backend_name} attempt process cannot span multiple Run workspaces"
                )
            return self._process_stderr_ref
        if self._process_instance_id is None:
            raise HarnessError(f"{self.backend_name} process instance id is unavailable")

        relative = (
            Path("attempts")
            / "execution-sessions"
            / self._process_instance_id
            / "stderr.log"
        )
        # Process diagnostics live at the stable Run root while the process cwd
        # for its single session is the attempt staging directory.
        path = (run_workspace / relative).resolve(strict=False)
        if run_workspace not in path.parents:
            raise HarnessError("process stderr diagnostics path escapes workspace")
        path.parent.mkdir(parents=True, exist_ok=False)
        path.open("xb").close()
        self._process_stderr_path = path
        self._process_stderr_ref = relative.as_posix()
        self._process_stderr_workspace = run_workspace
        self._process_stderr_bytes_written = 0
        self._process_stderr_truncated = False
        self._persist_process_stderr("".join(self._stderr_chunks))
        return self._process_stderr_ref

    def _persist_process_stderr(self, text: str) -> None:
        """Append without ever blocking pipe drain or exceeding the file cap."""

        if self._process_stderr_path is None or self._process_stderr_truncated:
            return
        marker = (
            "\n[process stderr truncated at %d bytes]\n"
            % self._process_stderr_file_limit_bytes
        ).encode("utf-8")
        marker = marker[: self._process_stderr_file_limit_bytes]
        payload_budget = max(
            0, self._process_stderr_file_limit_bytes - len(marker)
        )
        payload = text.encode("utf-8")
        available = max(0, payload_budget - self._process_stderr_bytes_written)
        truncated = len(payload) > available
        selected = payload[:available]
        if truncated:
            # Avoid ending the otherwise UTF-8 log in the middle of a codepoint.
            selected = selected.decode("utf-8", errors="ignore").encode("utf-8")
        try:
            with self._process_stderr_path.open("ab") as stream:
                stream.write(selected)
                if truncated:
                    stream.write(marker)
        except OSError:
            # Diagnostics must never stop the pipe drain and deadlock Hermes.
            return
        self._process_stderr_bytes_written += len(selected)
        self._process_stderr_truncated = truncated

    def _stderr_text_since(self, offset: int) -> str:
        truncated = offset < self._stderr_base_offset
        effective_offset = max(offset, self._stderr_base_offset)
        relative = effective_offset - self._stderr_base_offset
        text = "".join(self._stderr_chunks)[relative:]
        if truncated:
            return "[stderr before this point was truncated]\n" + text
        return text

    async def _settled_stderr_text(self, start_offset: int) -> str:
        """Wait for a short quiet window so prompt-tail logs reach diagnostics."""

        if self._stderr_task is None:
            return self._stderr_text_since(start_offset)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        while loop.time() < deadline:
            self._stderr_activity.clear()
            try:
                await asyncio.wait_for(
                    self._stderr_activity.wait(),
                    timeout=min(0.1, deadline - loop.time()),
                )
            except asyncio.TimeoutError:
                return self._stderr_text_since(start_offset)
        return self._stderr_text_since(start_offset)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for search_lease_file in self._search_lease_files.values():
            with contextlib.suppress(OSError):
                search_lease_file.unlink()
        self._search_lease_files.clear()
        if self._connection is not None:
            for session_id in list(self._active_sessions.values()):
                await self._cancel_session_id(session_id)
        self._active_sessions.clear()
        self._session_invocations.clear()
        if self._transport_context is not None:
            try:
                await asyncio.wait_for(
                    self._transport_context.__aexit__(None, None, None),
                    timeout=5.0,
                )
            except (asyncio.TimeoutError, Exception):
                if self._process is not None:
                    with contextlib.suppress(ProcessLookupError):
                        self._process.kill()
                    with contextlib.suppress(asyncio.TimeoutError, Exception):
                        await asyncio.wait_for(self._process.wait(), timeout=2.0)
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._stderr_task.cancel()
        self._transport_context = None
        self._connection = None
        self._process = None
        self._process_instance_id = None
        self._process_stderr_path = None
        self._process_stderr_ref = None
        self._process_stderr_workspace = None
        self._process_stderr_bytes_written = 0
        self._process_stderr_truncated = False
        self._stderr_task = None
        for probe_dir in self._search_probe_dirs:
            with contextlib.suppress(OSError):
                shutil.rmtree(probe_dir)
        self._search_probe_dirs = []
        self._started = False


class HermesAcpAttemptRuntime(AcpAgentAttemptRuntime):
    """Hermes ACP adapter using the shared ACP attempt lifecycle."""

    backend_name = "Hermes"

    def __init__(
        self,
        workspace: Path,
        hermes_command: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        command = hermes_command or shutil.which("hermes") or "hermes"
        resolved = shutil.which(command)
        self.hermes_command = str(Path(resolved).resolve()) if resolved else command
        super().__init__(
            workspace,
            acp_command=self.hermes_command,
            launch_backend="hermes",
            process_prefix="hermes-process",
            **kwargs,
        )


@dataclass(frozen=True)
class HermesBackendFactory:
    """Reusable Hermes configuration for disposable attempt runtimes."""

    workspace: Path
    hermes_command: Optional[str] = None
    profile: Optional[str] = None
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

    def _runtime(
        self, *, expected_invocation_id: Optional[str] = None
    ) -> HermesAcpAttemptRuntime:
        return HermesAcpAttemptRuntime(
            workspace=self.workspace,
            hermes_command=self.hermes_command,
            profile=self.profile,
            startup_timeout_seconds=self.startup_timeout_seconds,
            progress_reporter=self.progress_reporter,
            search_mcp_enabled=self.search_mcp_enabled,
            search_dir=self.search_dir,
            search_provider_python=self.search_provider_python,
            search_provider_limit=self.search_provider_limit,
            # Doctor/probe runs before a run directory exists and checks a
            # disposable local MCP. Only real run-coordinated attempts receive
            # an explicit proxy support object.
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

    async def preflight(self) -> Mapping[str, Any]:
        runtime = self._runtime()
        try:
            return await runtime.preflight()
        finally:
            await _shielded_runtime_close(runtime)

    async def probe(self) -> Mapping[str, Any]:
        runtime = self._runtime()
        try:
            await runtime.start()
            search_report = await runtime.check_search_mcp()
            return {
                "acp_initialize": "ok",
                "model_check": "not_run",
                **dict(search_report),
            }
        finally:
            await _shielded_runtime_close(runtime)

    def create(self, invocation: AgentInvocation) -> HermesAcpAttemptRuntime:
        return self._runtime(expected_invocation_id=invocation.invocation_id)


async def _shielded_runtime_close(runtime: HermesAcpAttemptRuntime) -> None:
    cleanup = asyncio.create_task(runtime.close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await cleanup
        raise
