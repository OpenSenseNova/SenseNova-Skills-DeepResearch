"""Install and manage the CLI-owned local Camofox REST server."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import psutil

from deepresearch_cli.harness.camofox_fallback import (
    CAMOFOX_VERSION,
    DEFAULT_CAMOFOX_BASE_URL,
    default_camofox_home,
)


class CamofoxRuntimeError(RuntimeError):
    pass


class CamofoxRuntime:
    def __init__(self, home: Optional[Path] = None, base_url: Optional[str] = None) -> None:
        self.home = (home or default_camofox_home()).expanduser().resolve()
        self.base_url = (base_url or DEFAULT_CAMOFOX_BASE_URL).rstrip("/")
        self.pid_file = self.home / "run" / "server.pid"
        self.log_file = self.home / "logs" / "server.log"

    @property
    def server_command(self) -> Path:
        return self.home / "node_modules" / ".bin" / "camofox-browser"

    @property
    def engine_dir(self) -> Path:
        return self.home / "engine"

    @property
    def package_manifest(self) -> Path:
        return self.home / "node_modules" / "@askjo" / "camofox-browser" / "package.json"

    def installed_version(self) -> Optional[str]:
        try:
            value = json.loads(self.package_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        version = value.get("version") if isinstance(value, dict) else None
        return str(version) if version else None

    def setup(self, *, npm_command: str = "npm") -> dict[str, Any]:
        node = shutil.which("node")
        npm = shutil.which(npm_command)
        if node is None or npm is None:
            raise CamofoxRuntimeError("Camofox setup requires Node.js >=22 and npm")
        version = subprocess.run(
            [node, "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
        try:
            major = int(version.removeprefix("v").split(".", 1)[0])
        except ValueError as exc:
            raise CamofoxRuntimeError(f"cannot parse Node.js version: {version}") from exc
        if major < 22:
            raise CamofoxRuntimeError(f"Camofox requires Node.js >=22; found {version}")
        self.home.mkdir(parents=True, exist_ok=True)
        command = [
            npm,
            "install",
            "--prefix",
            str(self.home),
            "--save-exact",
            "--no-audit",
            "--no-fund",
            f"@askjo/camofox-browser@{CAMOFOX_VERSION}",
        ]
        install_environment = dict(os.environ)
        # The core package's postinstall currently strips CAMOUFOX_INSTALL_DIR
        # before spawning its downloader. Skip that hook's download and run the
        # bundled downloader ourselves so the engine really stays CLI-owned.
        install_environment["CAMOFOX_SKIP_DOWNLOAD"] = "1"
        completed = subprocess.run(command, text=True, env=install_environment)
        if completed.returncode:
            raise CamofoxRuntimeError(
                f"npm install failed with exit code {completed.returncode}"
            )
        downloader = self.home / "node_modules" / ".bin" / "camoufox-js"
        if not self.server_command.is_file() or not downloader.is_file():
            raise CamofoxRuntimeError("Camofox install completed without the expected bins")
        fetch_environment = dict(os.environ)
        fetch_environment["CAMOUFOX_INSTALL_DIR"] = str(self.engine_dir)
        fetched = subprocess.run([str(downloader), "fetch"], text=True, env=fetch_environment)
        if fetched.returncode:
            raise CamofoxRuntimeError(
                f"Camoufox engine download failed with exit code {fetched.returncode}"
            )
        if not self.engine_dir.is_dir() or not any(self.engine_dir.iterdir()):
            raise CamofoxRuntimeError("Camoufox engine download did not populate the CLI directory")
        return self.status()

    def _health(self, timeout: float = 1.0) -> Optional[dict[str, Any]]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
                return value if isinstance(value, dict) else {"value": value}
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _owned_process(self) -> Optional[psutil.Process]:
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
            process = psutil.Process(pid)
            command = " ".join(process.cmdline())
            if str(self.server_command) not in command:
                return None
            return process
        except (OSError, ValueError, psutil.Error):
            return None

    def status(self) -> dict[str, Any]:
        health = self._health()
        process = self._owned_process()
        installed_version = self.installed_version()
        return {
            "camofox_home": str(self.home),
            "version": installed_version,
            "expected_version": CAMOFOX_VERSION,
            "version_matches": installed_version == CAMOFOX_VERSION,
            "installed": self.server_command.is_file() and installed_version is not None,
            "server_command": str(self.server_command),
            "engine_dir": str(self.engine_dir),
            "engine_installed": self.engine_dir.is_dir() and any(self.engine_dir.iterdir()),
            "base_url": self.base_url,
            "running": health is not None,
            "managed_pid": process.pid if process is not None else None,
            "health": health,
            "log_path": str(self.log_file),
        }

    def start(self, *, timeout_seconds: float = 45.0) -> dict[str, Any]:
        if self._health() is not None:
            return self.status()
        if not self.server_command.is_file():
            raise CamofoxRuntimeError("Camofox is not installed; run `deepresearch browser setup`")
        parts = urlsplit(self.base_url)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"}:
            raise CamofoxRuntimeError("managed Camofox start only supports a local HTTP base URL")
        port = parts.port or 9377
        for directory in (self.pid_file.parent, self.log_file.parent, self.home / "data"):
            directory.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment.update(
            {
                "CAMOFOX_BIND_HOST": "127.0.0.1",
                "CAMOFOX_PORT": str(port),
                "CAMOFOX_CRASH_REPORT_ENABLED": "false",
                "CAMOFOX_DISABLE_DEFAULT_ADDONS": "1",
                "CAMOUFOX_INSTALL_DIR": str(self.home / "engine"),
                "CAMOFOX_COOKIES_DIR": str(self.home / "data" / "cookies"),
                "CAMOFOX_UPLOADS_DIR": str(self.home / "data" / "uploads"),
                "CAMOFOX_PROFILE_DIR": str(self.home / "data" / "profiles"),
                "CAMOFOX_TRACES_DIR": str(self.home / "data" / "traces"),
            }
        )
        log = self.log_file.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [str(self.server_command)],
                cwd=self.home,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        self.pid_file.write_text(str(process.pid), encoding="utf-8")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise CamofoxRuntimeError(
                    f"Camofox exited with {process.returncode}; inspect {self.log_file}"
                )
            if self._health() is not None:
                return self.status()
            time.sleep(0.25)
        raise CamofoxRuntimeError(f"Camofox did not become healthy; inspect {self.log_file}")

    def stop(self) -> dict[str, Any]:
        process = self._owned_process()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        try:
            self.pid_file.unlink()
        except FileNotFoundError:
            pass
        return self.status()
