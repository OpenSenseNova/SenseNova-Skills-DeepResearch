"""Authenticated localhost server owning run-wide search state and limits."""

from __future__ import annotations

import hmac
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from .registry import DomainRegistry, ProviderRegistry, load_search_environment
from .coordinator_auth import derive_namespace_token
from .service import SearchService
from .sqlite_store import SQLiteSearchStore


_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 512 * 1024


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _integer(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _floating(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


class _Coordinator(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: SearchService, token: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.service = service
        self.token = token

    def dispatch(
        self, method: str, params: Mapping[str, Any], namespace: str
    ) -> dict[str, Any]:
        service = self.service
        if method == "health":
            return {"status": "ok"}
        if method == "list_search_sources":
            return service.list_search_sources()
        if method == "list_search_domains":
            return service.list_search_domains()
        if method == "batch_search":
            return service.batch_search(params.get("searches"), namespace=namespace)
        if method == "domain_search":
            return service.domain_search(params.get("searches"), namespace=namespace)
        if method == "start_domain_search":
            return service.start_domain_search(params.get("searches"), namespace=namespace)
        if method == "get_search_batch":
            return service.get_search_batch(str(params.get("batch_id") or ""), namespace=namespace)
        if method == "search_results":
            return service.search_results(
                cursor=params.get("cursor", 0),
                limit=params.get("limit", 20),
                provider=params.get("provider"),
                batch_id=params.get("batch_id"),
                namespace=namespace,
            )
        if method == "get_search_hit":
            return service.get_search_hit(
                str(params.get("hit_id") or ""), namespace=namespace
            )
        if method == "record_fetch":
            return service.record_fetch(params, namespace=namespace)
        raise ValueError(f"unknown coordinator method: {method}")


class _Handler(BaseHTTPRequestHandler):
    server: _Coordinator

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _write(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_RESPONSE_BYTES:
            status = 500
            encoded = b'{"ok":false,"error":"coordinator response exceeds transport budget"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/rpc":
            self._write(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= _MAX_REQUEST_BYTES:
                raise ValueError("invalid coordinator request size")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("coordinator request must be an object")
            method = value.get("method")
            params = value.get("params", {})
            namespace = value.get("namespace")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("invalid coordinator method or params")
            if not isinstance(namespace, str) or not namespace or len(namespace) > 300:
                raise ValueError("invalid search namespace")
            supplied = self.headers.get("Authorization", "")
            expected_token = (
                self.server.token
                if method == "health"
                else derive_namespace_token(self.server.token, namespace)
            )
            if not hmac.compare_digest(supplied, f"Bearer {expected_token}"):
                self._write(401, {"ok": False, "error": "unauthorized"})
                return
            result = self.server.dispatch(method, params, namespace)
        except Exception as exc:
            self._write(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._write(200, {"ok": True, "result": result})


def _service() -> SearchService:
    search_dir = Path(_required("DEEPRESEARCH_SEARCH_DIR")).expanduser().resolve()
    profile = os.environ.get("DEEPRESEARCH_SEARCH_ENV_FILE")
    environment = load_search_environment(
        search_dir, profile_env_file=Path(profile) if profile else None
    )
    registry = ProviderRegistry(
        search_dir=search_dir,
        python_executable=os.environ.get("DEEPRESEARCH_SEARCH_PROVIDER_PYTHON", sys.executable),
        environment=environment,
    )
    return SearchService(
        registry=registry,
        domain_registry=DomainRegistry(search_dir=search_dir, source_registry=registry),
        store=SQLiteSearchStore(Path(_required("DEEPRESEARCH_SEARCH_DATABASE"))),
        max_workers=_integer("DEEPRESEARCH_SEARCH_MAX_WORKERS", 8),
        provider_limit=_integer("DEEPRESEARCH_SEARCH_PROVIDER_LIMIT", 20),
        batch_timeout_seconds=_floating("DEEPRESEARCH_SEARCH_BATCH_TIMEOUT_SECONDS", 120.0),
        provider_env={
            name: environment[name]
            for name in registry.environment_names
            if environment.get(name)
        },
    )


def main() -> None:
    service = _service()
    server = _Coordinator(service, _required("DEEPRESEARCH_SEARCH_COORDINATOR_TOKEN"))
    ready_file = Path(_required("DEEPRESEARCH_SEARCH_COORDINATOR_READY_FILE"))
    lease_file = Path(_required("DEEPRESEARCH_SEARCH_COORDINATOR_LEASE_FILE"))
    parent_pid = _integer("DEEPRESEARCH_SEARCH_COORDINATOR_PARENT_PID", os.getppid())

    def stop(*_args: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    def watch() -> None:
        while lease_file.is_file():
            try:
                os.kill(parent_pid, 0)
            except OSError:
                break
            time.sleep(0.25)
        server.shutdown()

    threading.Thread(target=watch, name="search-coordinator-watchdog", daemon=True).start()
    ready_file.write_text(
        json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "pid": os.getpid()}),
        encoding="utf-8",
    )
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        service.close()
        server.server_close()
        try:
            ready_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
