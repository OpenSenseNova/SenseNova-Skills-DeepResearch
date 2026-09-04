"""Lifecycle owner for one run-scoped search coordinator process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from deepresearch_cli.search.paths import builtin_search_dir
from deepresearch_cli.search.registry import ProviderRegistry, load_search_environment
from deepresearch_cli.search.coordinator_auth import derive_namespace_token

from .protocol import AgentInvocation, HarnessError
from .search_mcp import SEARCH_TRANSPORT_ENV_NAMES


class SearchCoordinatorManager:
    """Start lazily for Research and reopen the same database on resume."""

    def __init__(
        self,
        *,
        runs_dir: Path,
        run_id: str,
        search_dir: Optional[Path] = None,
        provider_python: Optional[str] = None,
        provider_limit: int = 20,
        max_workers: int = 8,
        profile_env_file: Optional[Path] = None,
    ) -> None:
        self.runs_dir = runs_dir.expanduser().resolve()
        self.run_id = run_id
        self.search_dir = (search_dir or builtin_search_dir()).expanduser().resolve()
        self.provider_python = provider_python or sys.executable
        self.provider_limit = provider_limit
        self.max_workers = max_workers
        self.profile_env_file = profile_env_file
        self.url: Optional[str] = None
        self.token: Optional[str] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stderr_file = None
        self._lock = asyncio.Lock()
        self._start_task: Optional[asyncio.Task[None]] = None

    @property
    def root(self) -> Path:
        return self.runs_dir / self.run_id / "search"

    async def ensure_started(self, invocation: AgentInvocation) -> None:
        if invocation.run_id != self.run_id:
            raise HarnessError("search coordinator run id does not match invocation")
        async with self._lock:
            if self._process is not None and self._process.returncode is None:
                if self.url:
                    return
                task = self._start_task
            else:
                task = None
            if task is None:
                self._start_task = asyncio.create_task(self._start_once(invocation))
                task = self._start_task
        await task

    async def _start_once(self, invocation: AgentInvocation) -> None:
        """Start one coordinator and let all concurrent callers await it."""
        async with self._lock:
            if self._process is not None and self._process.returncode is None and self.url:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            lease = self.root / ".coordinator.lease"
            ready = self.root / ".coordinator.ready.json"
            if self._process is None or self._process.returncode is not None:
                with contextlib.suppress(OSError):
                    ready.unlink()
            lease.write_text("active\n", encoding="utf-8")
            self.token = secrets.token_urlsafe(32)
            environment = load_search_environment(
                self.search_dir,
                profile_env_file=(
                    self.profile_env_file
                    if self.profile_env_file is not None and self.profile_env_file.is_file()
                    else None
                ),
            )
            registry = ProviderRegistry(
                search_dir=self.search_dir,
                python_executable=self.provider_python,
                environment=environment,
            )
            inherited_names = (
                "PATH", "HOME", "USERPROFILE", "TMPDIR", "TMP", "TEMP",
                "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "VIRTUAL_ENV",
                "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TZ",
            )
            child_env = {
                name: os.environ[name] for name in inherited_names if os.environ.get(name)
            }
            child_env.update({
                "PYTHONUNBUFFERED": "1",
                "DEEPRESEARCH_SEARCH_DIR": str(self.search_dir),
                "DEEPRESEARCH_SEARCH_PROVIDER_PYTHON": self.provider_python,
                "DEEPRESEARCH_SEARCH_PROVIDER_LIMIT": str(self.provider_limit),
                "DEEPRESEARCH_SEARCH_MAX_WORKERS": str(self.max_workers),
                "DEEPRESEARCH_SEARCH_BATCH_TIMEOUT_SECONDS": "120",
                "DEEPRESEARCH_SEARCH_DATABASE": str(self.root / "search.sqlite3"),
                "DEEPRESEARCH_SEARCH_COORDINATOR_READY_FILE": str(ready),
                "DEEPRESEARCH_SEARCH_COORDINATOR_LEASE_FILE": str(lease),
                "DEEPRESEARCH_SEARCH_COORDINATOR_PARENT_PID": str(os.getpid()),
                "DEEPRESEARCH_SEARCH_COORDINATOR_TOKEN": self.token,
            })
            if self.profile_env_file is not None and self.profile_env_file.is_file():
                child_env["DEEPRESEARCH_SEARCH_ENV_FILE"] = str(self.profile_env_file)
            for name in (
                *registry.configuration_environment_names,
                *registry.environment_names,
                *SEARCH_TRANSPORT_ENV_NAMES,
            ):
                value = environment.get(name)
                if value:
                    child_env[name] = value
            log_path = self.root / "coordinator.stderr.log"
            self._stderr_file = log_path.open("ab", buffering=0)
            self._process = await asyncio.create_subprocess_exec(
                os.path.abspath(sys.executable),
                "-m",
                "deepresearch_cli.search.coordinator_server",
                cwd=str(self.runs_dir),
                env=child_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=self._stderr_file,
            )
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if self._process.returncode is not None:
                    break
                if ready.is_file():
                    try:
                        value = json.loads(ready.read_text(encoding="utf-8"))
                        self.url = str(value["url"])
                        await asyncio.to_thread(self._health)
                        self._start_task = None
                        return
                    except (OSError, KeyError, ValueError, json.JSONDecodeError, urllib.error.URLError):
                        await asyncio.sleep(0.1)
                        continue
                await asyncio.sleep(0.05)
            with contextlib.suppress(OSError):
                lease.unlink()
            if self._process is not None and self._process.returncode is None:
                self._process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
            self._process = None
            self.url = None
            self.token = None
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None
            raise HarnessError(f"search coordinator failed to start; inspect {log_path}")

    def _health(self) -> None:
        assert self.url and self.token
        payload = json.dumps(
            {"method": "health", "params": {}, "namespace": "coordinator-health"}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url + "/rpc",
            data=payload,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            if response.status != 200:
                raise HarnessError("search coordinator health check failed")

    def credentials(self, namespace: str) -> tuple[str, str]:
        """Return URL and a credential scoped to one Research attempt."""
        if not self.url or not self.token:
            raise HarnessError("run-scoped search coordinator has not started")
        return self.url, derive_namespace_token(self.token, namespace)

    async def close(self) -> None:
        start_task = self._start_task
        if start_task is not None and not start_task.done():
            start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await start_task
        async with self._lock:
            lease = self.root / ".coordinator.lease"
            with contextlib.suppress(OSError):
                lease.unlink()
            process = self._process
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            self._process = None
            self._start_task = None
            self.url = None
            self.token = None
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None
