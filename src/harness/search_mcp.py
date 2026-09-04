"""Backend-neutral launch description for the per-attempt search MCP."""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from deepresearch_cli.search.paths import builtin_search_dir
from deepresearch_cli.search.registry import ProviderRegistry, load_search_environment

from .protocol import HarnessError


SEARCH_TRANSPORT_ENV_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


@dataclass(frozen=True)
class SearchMcpLaunchSpec:
    name: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    lease_file: Path


@dataclass(frozen=True)
class SearchMcpSupport:
    search_dir: Optional[Path] = None
    provider_python: Optional[str] = None
    provider_limit: int = 20
    profile_env_file: Optional[Path] = None
    coordinator: Optional[object] = None
    camofox_fallback_enabled: bool = False
    camofox_base_url: Optional[str] = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.provider_limit, bool)
            or not isinstance(self.provider_limit, int)
            or not 1 <= self.provider_limit <= 50
        ):
            raise ValueError("provider_limit must be an integer between 1 and 50")

    def resolved_search_dir(self) -> Path:
        selected = self.search_dir
        if selected is None:
            value = os.environ.get("DEEPRESEARCH_SEARCH_DIR")
            selected = Path(value) if value else builtin_search_dir()
        resolved = selected.expanduser().resolve()
        if not resolved.is_dir():
            raise HarnessError(
                f"search registry is unavailable: {resolved}; pass --search-dir"
            )
        return resolved

    def resolved_provider_python(self) -> str:
        supplied = (
            self.provider_python
            or os.environ.get("DEEPRESEARCH_SEARCH_PROVIDER_PYTHON")
            or sys.executable
        )
        import shutil

        resolved = shutil.which(supplied)
        if resolved is None:
            candidate = Path(supplied).expanduser()
            resolved = str(candidate.absolute()) if candidate.is_file() else None
        if resolved is None:
            raise HarnessError(
                f"search provider Python executable not found: {supplied}"
            )
        return os.path.abspath(os.path.expanduser(resolved))

    @staticmethod
    def safe_server_name(identity: str) -> str:
        material = f"{identity}:{uuid.uuid4().hex}".encode("utf-8")
        return "drs_" + hashlib.sha256(material).hexdigest()[:20]

    @staticmethod
    def create_lease(path: Path) -> Path:
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return resolved
        try:
            os.write(descriptor, b"active\n")
        finally:
            os.close(descriptor)
        return resolved

    def build(
        self,
        *,
        identity: str,
        store_dir: Path,
        batch_timeout_seconds: float = 120.0,
        lease_file: Optional[Path] = None,
    ) -> SearchMcpLaunchSpec:
        name = self.safe_server_name(identity)
        selected_lease = (
            lease_file or store_dir.parent / f".{name}.lease"
        ).expanduser().resolve()
        fetch_env = {"DEEPRESEARCH_FETCH_IDENTITY": identity}
        if self.camofox_fallback_enabled:
            fetch_env.update(
                {
                    "DEEPRESEARCH_CAMOFOX_FALLBACK": "1",
                    "DEEPRESEARCH_CAMOFOX_BASE_URL": (
                        self.camofox_base_url or "http://127.0.0.1:9377"
                    ),
                }
            )
        if self.coordinator is not None:
            credentials = getattr(self.coordinator, "credentials", None)
            if not callable(credentials):
                raise HarnessError(
                    "run-scoped search coordinator does not support attempt credentials"
                )
            coordinator_url, coordinator_token = credentials(identity)
            if not coordinator_url or not coordinator_token:
                raise HarnessError("run-scoped search coordinator has not started")
            return SearchMcpLaunchSpec(
                name=name,
                command=self._server_command(),
                args=(),
                env={
                    "DEEPRESEARCH_SEARCH_COORDINATOR_URL": str(coordinator_url),
                    "DEEPRESEARCH_SEARCH_COORDINATOR_TOKEN": str(coordinator_token),
                    "DEEPRESEARCH_SEARCH_NAMESPACE": identity,
                    "DEEPRESEARCH_SEARCH_LEASE_FILE": str(selected_lease),
                    "DEEPRESEARCH_SEARCH_STARTUP_LOG": str((store_dir.parent / "mcp-startup.log").resolve()),
                    "DEEPRESEARCH_SEARCH_DISABLE_WATCHDOG": "1",
                    "PYTHONUNBUFFERED": "1",
                    **fetch_env,
                },
                lease_file=self.create_lease(selected_lease),
            )
        search_dir = self.resolved_search_dir()
        provider_python = self.resolved_provider_python()
        profile_env = self.profile_env_file
        environment = load_search_environment(
            search_dir,
            profile_env_file=(
                profile_env.expanduser().resolve()
                if profile_env is not None and profile_env.is_file()
                else None
            ),
        )
        registry = ProviderRegistry(
            search_dir=search_dir,
            python_executable=provider_python,
            environment=environment,
        )
        child_env = {
            "DEEPRESEARCH_SEARCH_STORE_DIR": str(store_dir.resolve()),
            "DEEPRESEARCH_SEARCH_DIR": str(search_dir),
            "DEEPRESEARCH_SEARCH_PROVIDER_PYTHON": provider_python,
            "DEEPRESEARCH_SEARCH_BATCH_TIMEOUT_SECONDS": str(batch_timeout_seconds),
            "DEEPRESEARCH_SEARCH_PROVIDER_LIMIT": str(self.provider_limit),
            "DEEPRESEARCH_SEARCH_LEASE_FILE": str(selected_lease),
            "DEEPRESEARCH_SEARCH_STARTUP_LOG": str((store_dir.parent / "mcp-startup.log").resolve()),
            "PYTHONUNBUFFERED": "1",
            **fetch_env,
        }
        if profile_env is not None and profile_env.is_file():
            child_env["DEEPRESEARCH_SEARCH_ENV_FILE"] = str(
                profile_env.expanduser().resolve()
            )
        for name_ in (
            *registry.configuration_environment_names,
            *registry.environment_names,
            *SEARCH_TRANSPORT_ENV_NAMES,
        ):
            value = environment.get(name_)
            if value:
                child_env[name_] = value
        # Prefer the environment's installed console entry point over
        # ``python -m``.  Virtualenv Python is commonly a symlink to the
        # underlying uv interpreter; Codex App Server may canonicalize that
        # path before spawning it, which drops the editable/package context.
        # The console script retains the venv launcher and is also what users
        # get from a regular wheel installation.
        command = self._server_command()
        return SearchMcpLaunchSpec(
            name=name,
            command=command,
            args=()
            if command != os.path.abspath(sys.executable)
            else ("-m", "deepresearch_cli.search.mcp_server"),
            env=child_env,
            lease_file=self.create_lease(selected_lease),
        )

    @staticmethod
    def _server_command() -> str:
        import shutil

        entrypoint = shutil.which("deepresearch-codex-mcp")
        if entrypoint:
            return os.path.abspath(entrypoint)
        candidate = Path(sys.executable).expanduser().parent / "deepresearch-codex-mcp"
        if candidate.is_file():
            return str(candidate.absolute())
        # Source/check-out environments may not have console scripts installed.
        # Keep the original fallback for those environments.
        return os.path.abspath(sys.executable)
