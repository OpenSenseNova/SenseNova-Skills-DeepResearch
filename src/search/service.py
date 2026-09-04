"""Concurrent directed search orchestration behind the MCP surface."""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import psutil

from .adapters import SubprocessSourceAdapter
from .contracts import (
    DomainSearchRequest,
    SearchRequest,
    json_safe,
    validate_domain_search_requests,
    validate_search_requests,
)
from .registry import DomainRegistry
from .registry.sources import (
    ProviderDefinition,
    ProviderRegistry,
)
from .results import (
    normalize_search_hits,
    parse_json_output,
    provider_payload_error,
    provider_payload_warnings,
)
from .store import SearchStore, SearchStoreError


_HARD_FAILURE = re.compile(
    r"(?:HTTP[^0-9]*)?(?:401|403)|missing[^\n]{0,50}(?:token|cookie|api[ _-]?key)"
    r"|(?:token|cookie|api[ _-]?key)[^\n]{0,50}(?:required|missing|未设置|缺少)",
    flags=re.IGNORECASE,
)

_SAFE_PROCESS_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "TZ",
    }
)
_PROXY_ENV = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)
_MAX_PROVIDER_STDOUT_BYTES = 8 * 1024 * 1024
_MAX_PROVIDER_STDERR_BYTES = 1024 * 1024
_MAX_PERSISTED_PROVIDER_PAYLOAD_BYTES = 512 * 1024
_MAX_BATCH_RESPONSE_BYTES = 192 * 1024
_PROVIDER_INVOCATION_ENV = "DEEPRESEARCH_SEARCH_PROVIDER_INVOCATION"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SearchService:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        store: SearchStore,
        max_workers: int = 8,
        provider_limit: int = 20,
        batch_timeout_seconds: float = 120.0,
        provider_env: Optional[Mapping[str, str]] = None,
        lease_file: Optional[Path] = None,
        domain_registry: Optional[DomainRegistry] = None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if (
            isinstance(provider_limit, bool)
            or not isinstance(provider_limit, int)
            or not 1 <= provider_limit <= 50
        ):
            raise ValueError("provider_limit must be between 1 and 50")
        if not math.isfinite(batch_timeout_seconds) or batch_timeout_seconds <= 0:
            raise ValueError("batch_timeout_seconds must be finite and positive")
        self.registry = registry
        self.store = store
        self.max_workers = max_workers
        self.provider_limit = provider_limit
        self.batch_timeout_seconds = batch_timeout_seconds
        self.provider_env = {
            str(name): str(value)
            for name, value in (provider_env or {}).items()
            if value is not None
        }
        self.lease_file = lease_file.expanduser().resolve() if lease_file else None
        self.domain_registry = domain_registry or DomainRegistry(
            search_dir=self.registry.search_dir,
            source_registry=self.registry,
        )
        self._provider_gates = {
            name: threading.BoundedSemaphore(
                value=self.registry.definition(name).max_parallel
            )
            for name in self.registry.names
        }
        self._domain_gates = {
            definition.name: threading.BoundedSemaphore(
                value=definition.max_parallel
            )
            for definition in self.domain_registry.definitions
        }
        # Coordinator mode serves several Research attempts at once. These
        # gates therefore enforce one run-wide budget and coalesce identical
        # provider/query executions across concurrent MCP clients.
        self._global_gate = threading.BoundedSemaphore(value=max_workers)
        self._pair_gates: dict[str, threading.Lock] = {}
        self._pair_gates_lock = threading.Lock()
        self._completed_execution_cache: dict[str, dict[str, Any]] = {}
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._active_processes_lock = threading.Lock()
        self._source_adapter = SubprocessSourceAdapter(
            registry=self.registry,
            environment_factory=self._provider_process_environment,
        )
        self._domain_batches: dict[str, dict[str, Any]] = {}
        self._domain_batches_lock = threading.RLock()

    def _telemetry(self, event_type: str, **values: Any) -> None:
        record = getattr(self.store, "record_telemetry", None)
        if not callable(record):
            return
        record({"event_type": event_type, "recorded_at": _utc_now(), **values})

    def _redact_text(self, text: str) -> str:
        redacted = text
        for value in self.provider_env.values():
            if len(value) >= 4:
                redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    @staticmethod
    def _bounded_error(value: Any) -> Optional[str]:
        if value in (None, "", [], {}):
            return None
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(
                json_safe(value, max_string_chars=1_000),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return text if len(text) <= 2_000 else text[:1_999] + "\u2026"

    @staticmethod
    def _bounded_provider_payload(value: Any) -> tuple[Any, bool]:
        bounded = json_safe(value, max_string_chars=20_000)
        encoded = json.dumps(
            bounded, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) <= _MAX_PERSISTED_PROVIDER_PAYLOAD_BYTES:
            return bounded, False
        preview = encoded[: _MAX_PERSISTED_PROVIDER_PAYLOAD_BYTES - 256].decode(
            "utf-8", errors="ignore"
        )
        return {
            "_truncated_for_storage": True,
            "_original_bytes_at_least": len(encoded),
            "_json_preview": preview,
        }, True

    def list_search_sources(self) -> dict[str, Any]:
        sources = []
        disabled = self.store.disabled_providers()
        for source in self.registry.list_sources():
            value = dict(source)
            definition = self.registry.definition(value["provider"])
            missing_credentials = self._missing_required_credentials(definition)
            if missing_credentials:
                value["available"] = False
                value["unavailable_reason"] = (
                    "missing required credential environment: "
                    + ", ".join(missing_credentials)
                )
            if value["provider"] in disabled:
                value["available"] = False
                value["disabled_reason"] = disabled[value["provider"]]
            sources.append(value)
        return {
            "sources": sources,
            "provider_result_limit": self.provider_limit,
            "pagination_scope": (
                "search_results pages persisted discoveries returned within each "
                "provider call's result limit; it does not page the upstream platform"
            ),
            "selection_rule": (
                "Choose only providers relevant to the evidence target and write a "
                "provider-specific query. Do not broadcast one generic query."
            ),
        }

    def list_search_domains(self) -> dict[str, Any]:
        def availability(source: str) -> tuple[bool, Optional[str]]:
            definition = self.registry.definition(source)
            missing = self._missing_required_credentials(definition)
            if missing:
                return False, "missing required credential environment: " + ", ".join(missing)
            reason = self.store.disabled_reason(source)
            if reason is not None:
                return False, reason
            return self.registry.availability(definition)

        return {
            "domains": self.domain_registry.list_domains(
                source_registry=self.registry,
                availability=availability,
            ),
            "selection_rule": (
                "Choose every domain relevant to the evidence target, then choose one "
                "operation per domain. The service fans each request out to all sources "
                "declared for that operation."
            ),
            "evidence_rule": (
                "Domain results are discovery candidates, not evidence; fetch/read the "
                "selected source URL before writing a claim."
            ),
        }

    def _missing_required_credentials(
        self, definition: ProviderDefinition
    ) -> tuple[str, ...]:
        return tuple(
            name
            for name in definition.required_credentials
            if not self.provider_env.get(name) and not os.environ.get(name)
        )

    def _provider_process_environment(
        self, definition: ProviderDefinition
    ) -> dict[str, str]:
        """Build a minimal environment for one trusted local provider script.

        In particular, never pass the MCP's env-file path or credentials for
        unrelated routes. Provider scripts are still trusted local executable
        code, but accidental cross-provider credential exposure is removed.
        """

        environment = {
            name: value
            for name, value in os.environ.items()
            if name in _SAFE_PROCESS_ENV
        }
        allowed = set(getattr(definition, "environment_variables", ()))
        allowed.update(_PROXY_ENV)
        for name in allowed:
            value = self.provider_env.get(name) or os.environ.get(name)
            if value:
                environment[name] = value
        return environment

    @staticmethod
    def _terminate_process(process: subprocess.Popen[Any]) -> None:
        """Best-effort bounded termination, including detached descendants."""

        targets: list[psutil.Process] = []
        try:
            root = psutil.Process(process.pid)
            targets = [*root.children(recursive=True), root]
        except (psutil.Error, OSError):
            root = None

        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, PermissionError):
                pass
        for target in reversed(targets):
            try:
                target.terminate()
            except (psutil.Error, OSError):
                pass
        if targets:
            _, alive = psutil.wait_procs(targets, timeout=0.4)
        else:
            alive = []
        for target in alive:
            try:
                target.kill()
            except (psutil.Error, OSError):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=0.5)
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    @staticmethod
    def _terminate_marked_processes(marker: str) -> None:
        """Remove descendants that detached before their provider parent exited.

        Process groups and parent/child traversal cover ordinary providers. A
        child can deliberately create a new session and then be re-parented
        before cleanup observes it, so each provider invocation also carries a
        random, non-secret environment marker. Only same-user processes whose
        inherited marker matches are eligible for termination.
        """

        targets: list[psutil.Process] = []
        try:
            candidates = psutil.process_iter(attrs=["pid"])
            for candidate in candidates:
                if candidate.pid == os.getpid():
                    continue
                try:
                    if candidate.environ().get(_PROVIDER_INVOCATION_ENV) == marker:
                        targets.append(candidate)
                except (psutil.Error, OSError):
                    continue
        except (psutil.Error, OSError):
            # Process enumeration is denied in some sandboxes and managed
            # desktop environments. Detached-child cleanup is best-effort;
            # inability to inspect unrelated processes must not turn an
            # otherwise successful provider call into a search failure.
            return
        for target in targets:
            try:
                target.terminate()
            except (psutil.Error, OSError):
                pass
        if targets:
            _, alive = psutil.wait_procs(targets, timeout=0.4)
        else:
            alive = []
        for target in alive:
            try:
                target.kill()
            except (psutil.Error, OSError):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=0.5)

    @staticmethod
    def _read_bounded_output(
        stream: Any, *, maximum_bytes: int, label: str
    ) -> tuple[str, Optional[str]]:
        stream.flush()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        payload = stream.read(maximum_bytes)
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        warning = None
        if size > maximum_bytes:
            warning = f"{label} truncated at {maximum_bytes} bytes (received {size})"
        return payload, warning

    def _execute(
        self,
        request: SearchRequest,
        *,
        batch_id: str,
        deadline: float,
    ) -> dict[str, Any]:
        started = time.monotonic()
        definition = self.registry.definition(request.provider)
        missing_credentials = self._missing_required_credentials(definition)
        if missing_credentials:
            return self._result_record(
                request,
                batch_id=batch_id,
                status="unavailable",
                started=started,
                error=(
                    "missing required credential environment: "
                    + ", ".join(missing_credentials)
                ),
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return self._result_record(
                request,
                batch_id=batch_id,
                status="timed_out",
                started=started,
                timed_out=True,
                error="batch deadline expired before provider availability check",
            )
        available, unavailable_reason = self.registry.availability(
            definition,
            module_check_timeout_seconds=min(10.0, max(0.01, remaining)),
        )
        if time.monotonic() >= deadline:
            return self._result_record(
                request,
                batch_id=batch_id,
                status="timed_out",
                started=started,
                timed_out=True,
                error="batch deadline expired during provider availability check",
            )
        if not available:
            return self._result_record(
                request,
                batch_id=batch_id,
                status="unavailable",
                started=started,
                error=unavailable_reason,
            )
        prepared = self._source_adapter.prepare(
            request, limit=self.provider_limit
        )
        definition = prepared.definition
        domain_gate = self._domain_gates.get(request.domain or "")
        if domain_gate is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not domain_gate.acquire(timeout=max(0.0, remaining)):
                return self._result_record(
                    request,
                    batch_id=batch_id,
                    status="timed_out",
                    started=started,
                    timed_out=True,
                    error="batch deadline expired before domain execution",
                )
        gate = self._provider_gates[request.provider]
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not gate.acquire(timeout=max(0.0, remaining)):
            if domain_gate is not None:
                domain_gate.release()
            return self._result_record(
                request,
                batch_id=batch_id,
                status="timed_out",
                started=started,
                timed_out=True,
                error="batch deadline expired before provider execution",
            )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._result_record(
                    request,
                    batch_id=batch_id,
                    status="timed_out",
                    started=started,
                    timed_out=True,
                    error="batch deadline expired before provider execution",
                )
            process_environment = dict(prepared.environment)
            provider_invocation_marker = uuid.uuid4().hex
            process_environment[_PROVIDER_INVOCATION_ENV] = (
                provider_invocation_marker
            )
            with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
                mode="w+b"
            ) as stderr_file:
                process = subprocess.Popen(
                    list(prepared.command),
                    cwd=str(prepared.cwd),
                    text=True,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=process_environment,
                    start_new_session=os.name == "posix",
                )
                with self._active_processes_lock:
                    self._active_processes.add(process)
                timed_out = False
                timeout_reason: Optional[str] = None
                output_limit_error: Optional[str] = None
                process_deadline = min(
                    deadline, time.monotonic() + definition.timeout_seconds
                )
                try:
                    while process.poll() is None:
                        stdout_size = os.fstat(stdout_file.fileno()).st_size
                        stderr_size = os.fstat(stderr_file.fileno()).st_size
                        if (
                            stdout_size > _MAX_PROVIDER_STDOUT_BYTES
                            or stderr_size > _MAX_PROVIDER_STDERR_BYTES
                        ):
                            output_limit_error = (
                                "provider output exceeded the bounded capture size: "
                                f"stdout={stdout_size}, stderr={stderr_size}"
                            )
                            self._terminate_process(process)
                            break
                        if self.lease_file is not None and not self.lease_file.is_file():
                            timed_out = True
                            timeout_reason = "search cancelled because the Research attempt lease ended"
                            self._terminate_process(process)
                            break
                        if time.monotonic() >= process_deadline:
                            timed_out = True
                            timeout_reason = "provider process timed out"
                            self._terminate_process(process)
                            break
                        time.sleep(0.05)
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        timeout_reason = timeout_reason or "provider process did not exit"
                        self._terminate_process(process)
                finally:
                    with self._active_processes_lock:
                        self._active_processes.discard(process)
                    self._terminate_marked_processes(provider_invocation_marker)
                stdout, stdout_warning = self._read_bounded_output(
                    stdout_file,
                    maximum_bytes=_MAX_PROVIDER_STDOUT_BYTES,
                    label="provider stdout",
                )
                stderr, stderr_warning = self._read_bounded_output(
                    stderr_file,
                    maximum_bytes=_MAX_PROVIDER_STDERR_BYTES,
                    label="provider stderr",
                )
            stream_warnings = [
                warning for warning in (stdout_warning, stderr_warning) if warning
            ]
            if stream_warnings:
                stderr = "\n".join([stderr, *stream_warnings]).strip()
            stdout = self._redact_text(stdout)
            stderr = self._redact_text(stderr)

            payload: Any = None
            parse_error: Optional[str] = None
            if stdout.strip():
                try:
                    payload = parse_json_output(stdout)
                except Exception as exc:  # one bad source is isolated.
                    parse_error = f"{type(exc).__name__}: {exc}"
            elif not timed_out:
                parse_error = "provider returned empty stdout"

            if time.monotonic() >= deadline:
                return self._result_record(
                    request,
                    batch_id=batch_id,
                    status="timed_out",
                    started=started,
                    timed_out=True,
                    returncode=process.returncode,
                    stderr=stderr[:10_000],
                    parse_error=parse_error,
                    error="batch deadline expired during provider output parsing",
                    process_started=True,
                )

            # The source definition owns any intentional multi-subsource
            # multiplier. Enforce the bound here even when a buggy or hostile
            # script ignores its configured limit argument.
            normalized_item_limit = (
                self.provider_limit * definition.item_limit_multiplier
            )
            hits = normalize_search_hits(
                request=request,
                payload=payload,
                max_items=normalized_item_limit,
                result_shape=definition.result_shape,
            )
            if time.monotonic() >= deadline:
                return self._result_record(
                    request,
                    batch_id=batch_id,
                    status="timed_out",
                    started=started,
                    timed_out=True,
                    returncode=process.returncode,
                    stderr=stderr[:10_000],
                    parse_error=parse_error,
                    error="batch deadline expired during provider result normalization",
                    process_started=True,
                )
            payload_error = provider_payload_error(payload)
            payload_warnings = provider_payload_warnings(payload)
            payload_failed = isinstance(payload, Mapping) and (
                payload.get("success") is False or payload.get("ok") is False
            )
            error = (
                output_limit_error
                or payload_warnings
                or payload_error
                or (stderr.strip()[:1_000] if stderr.strip() else None)
                or parse_error
            )
            if timed_out:
                status = "timed_out"
                error = error or timeout_reason or "provider process timed out"
            elif output_limit_error:
                status = "partial" if hits else "failed"
            elif process.returncode != 0 or not isinstance(payload, Mapping) or payload_failed:
                status = "partial" if hits else "failed"
            elif payload_error or payload_warnings:
                status = "partial" if hits else "failed"
            elif hits:
                status = "ok"
            else:
                status = "empty"

            error_text = json.dumps(error, ensure_ascii=False) if error is not None else ""
            if status == "failed" and _HARD_FAILURE.search(error_text):
                self.store.disable_provider(
                    request.provider,
                    f"hard provider failure: {error_text[:700]}",
                )

            bounded_payload, payload_truncated = self._bounded_provider_payload(
                payload
            )
            return self._result_record(
                request,
                batch_id=batch_id,
                status=status,
                started=started,
                timed_out=timed_out,
                returncode=process.returncode,
                stderr=stderr[:10_000],
                parse_error=parse_error,
                error=self._bounded_error(error),
                warnings=payload_warnings,
                payload=bounded_payload,
                payload_truncated=payload_truncated,
                hits=hits,
                process_started=True,
            )
        except SearchStoreError:
            raise
        except Exception as exc:  # provider process failures never kill peers.
            return self._result_record(
                request,
                batch_id=batch_id,
                status="failed",
                started=started,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            gate.release()
            if domain_gate is not None:
                domain_gate.release()

    @staticmethod
    def _result_record(
        request: SearchRequest,
        *,
        batch_id: str,
        status: str,
        started: float,
        timed_out: bool = False,
        returncode: Optional[int] = None,
        stderr: str = "",
        parse_error: Optional[str] = None,
        error: Any = None,
        warnings: Optional[list[dict[str, Any]]] = None,
        payload: Any = None,
        payload_truncated: bool = False,
        hits: Optional[list[dict[str, Any]]] = None,
        process_started: bool = False,
    ) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            **request.to_dict(),
            "pair_key": request.pair_key,
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "timed_out": timed_out,
            "returncode": returncode,
            "stderr": stderr,
            "parse_error": parse_error,
            "error": error,
            "warnings": list(warnings or []),
            "payload": payload,
            "payload_truncated": payload_truncated,
            "hits": list(hits or []),
            "process_started": process_started,
            "recorded_at": _utc_now(),
        }

    def _pair_gate(self, pair_key: str) -> threading.Lock:
        with self._pair_gates_lock:
            return self._pair_gates.setdefault(pair_key, threading.Lock())

    def _execute_guarded(
        self,
        request: SearchRequest,
        *,
        batch_id: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Execute one external pair once across all concurrent batches."""

        started = time.monotonic()
        pair_gate = self._pair_gate(request.pair_key)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not pair_gate.acquire(timeout=max(0.0, remaining)):
            return self._result_record(
                request,
                batch_id=batch_id,
                status="timed_out",
                started=started,
                timed_out=True,
                error="batch deadline expired while waiting for an identical search",
            )
        try:
            cached = self._completed_execution_cache.get(request.pair_key)
            if cached is not None or self.store.has_pair(request.pair_key):
                hits = (
                    list(cached.get("hits") or [])
                    if cached is not None
                    else self.store.hits_for_pair(request.pair_key)
                )
                record = self._result_record(
                    request,
                    batch_id=batch_id,
                    status="reused_completed",
                    started=started,
                    hits=hits,
                )
                self._telemetry(
                    "source_reused",
                    batch_id=batch_id,
                    **request.to_dict(),
                    pair_key=request.pair_key,
                    status="reused_completed",
                    raw_count=len(hits),
                    cache_reused=1,
                )
                return record

            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._global_gate.acquire(
                timeout=max(0.0, remaining)
            ):
                return self._result_record(
                    request,
                    batch_id=batch_id,
                    status="timed_out",
                    started=started,
                    timed_out=True,
                    error="global search concurrency deadline expired",
                )
            try:
                self._telemetry(
                    "source_started",
                    batch_id=batch_id,
                    **request.to_dict(),
                    pair_key=request.pair_key,
                    status="running",
                )
                record = self._execute(
                    request,
                    batch_id=batch_id,
                    deadline=deadline,
                )
            finally:
                self._global_gate.release()
            self._telemetry(
                "source_finished",
                batch_id=batch_id,
                **request.to_dict(),
                pair_key=request.pair_key,
                status=record.get("status"),
                elapsed_seconds=record.get("elapsed_seconds"),
                raw_count=len(record.get("hits") or []),
                process_started=record.get("process_started") is True,
            )
            if record.get("status") in {"ok", "empty"}:
                self._completed_execution_cache[request.pair_key] = dict(record)
            return record
        finally:
            pair_gate.release()

    @staticmethod
    def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
        summary = {
            key: record.get(key)
            for key in (
                "provider",
                "domain",
                "operation",
                "query",
                "evidence_target",
                "intent",
                "status",
                "hit_count",
                "new_unique_hit_count",
                "duplicate_hit_count",
                "elapsed_seconds",
                "timed_out",
                "error",
                "warnings",
            )
        }
        error = summary.get("error")
        if error is not None:
            error_text = str(error)
            summary["error"] = (
                error_text if len(error_text) <= 800 else error_text[:799] + "…"
            )
        for field in ("evidence_target", "intent"):
            value = str(summary.get(field) or "")
            summary[field] = value if len(value) <= 300 else value[:299] + "…"
        warnings = summary.get("warnings")
        if isinstance(warnings, Sequence) and not isinstance(warnings, str):
            bounded_warnings: list[dict[str, Any]] = []
            for warning in list(warnings)[:5]:
                if not isinstance(warning, Mapping):
                    continue
                attempts = warning.get("attempts")
                warning_error = str(warning.get("error") or "")
                bounded_warnings.append(
                    {
                        "code": str(warning.get("code") or "")[:100],
                        "source": str(warning.get("source") or "")[:100],
                        "provider": str(warning.get("provider") or "")[:100],
                        "error": (
                            warning_error
                            if len(warning_error) <= 400
                            else warning_error[:399] + "…"
                        ),
                        "attempt_count": (
                            len(attempts)
                            if isinstance(attempts, Sequence)
                            and not isinstance(attempts, str)
                            else 0
                        ),
                    }
                )
            summary["warnings"] = bounded_warnings
        else:
            summary["warnings"] = []
        return summary

    def batch_search(
        self, searches: object, *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        return self._batch_search(self._apply_namespace(searches, namespace))

    def _expand_domain_searches(
        self, searches: Sequence[DomainSearchRequest]
    ) -> tuple[list[SearchRequest], list[dict[str, Any]]]:
        expanded: list[SearchRequest] = []
        plans: list[dict[str, Any]] = []
        for search in searches:
            domain, operation = self.domain_registry.resolve(
                search.domain, search.operation
            )
            selected_sources = operation.sources
            source_queries = dict(search.source_queries or {})
            unknown_query_sources = set(source_queries) - set(selected_sources)
            if unknown_query_sources:
                raise ValueError(
                    f"domain {domain.name} operation {operation.name} received source_queries "
                    f"for unrelated sources: {', '.join(sorted(unknown_query_sources))}"
                )
            plans.append(
                {
                    "domain": domain.name,
                    "operation": operation.name,
                    "source_policy": search.source_policy,
                    "sources": list(selected_sources),
                    "query": search.query,
                    "source_queries": source_queries,
                    "evidence_target": search.evidence_target,
                }
            )
            expanded.extend(
                SearchRequest(
                    provider=source,
                    query=source_queries.get(source, search.query),
                    evidence_target=search.evidence_target,
                    intent=search.intent,
                    domain=domain.name,
                    operation=operation.name,
                    namespace=search.namespace,
                )
                for source in selected_sources
            )
        return expanded, plans

    @staticmethod
    def _domain_outcome(result: Mapping[str, Any]) -> str:
        summaries = result.get("provider_summaries")
        statuses = {
            str(item.get("status"))
            for item in summaries or []
            if isinstance(item, Mapping)
        }
        successful = statuses & {"ok", "empty", "reused_completed"}
        if successful and statuses <= {"ok", "empty", "reused_completed"}:
            return "succeeded"
        if successful:
            return "partial_success"
        return "failed"

    @staticmethod
    def _apply_namespace(searches: object, namespace: Optional[str]) -> object:
        if namespace is None:
            return searches
        if not isinstance(searches, Sequence) or isinstance(
            searches, (str, bytes, bytearray)
        ):
            return searches
        output: list[object] = []
        for item in searches:
            if isinstance(item, SearchRequest):
                value = item.to_dict()
            elif isinstance(item, DomainSearchRequest):
                value = item.to_dict()
            elif isinstance(item, Mapping):
                value = dict(item)
            else:
                output.append(item)
                continue
            supplied = value.get("namespace")
            if supplied is not None and supplied != namespace:
                raise ValueError("search namespace cannot be overridden by the caller")
            value["namespace"] = namespace
            output.append(value)
        return output

    def domain_search(
        self, searches: object, *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        """Synchronously fan domain requests out to their relevant sources."""

        domain_requests = validate_domain_search_requests(
            self._apply_namespace(searches, namespace),
            domain_names=self.domain_registry.names,
        )
        expanded, plans = self._expand_domain_searches(domain_requests)
        batch_id = "search-batch-" + uuid.uuid4().hex
        self._record_domain_plans(batch_id, plans, namespace)
        result = self._batch_search(expanded, batch_id=batch_id)
        result["domain_status"] = self._domain_outcome(result)
        result["domain_search_count"] = len(domain_requests)
        result["domain_plans"] = plans
        self._record_domain_completion(batch_id, plans, result["domain_status"], namespace)
        return result

    def _record_domain_plans(
        self,
        batch_id: str,
        plans: Sequence[Mapping[str, Any]],
        namespace: Optional[str],
    ) -> None:
        for plan in plans:
            self._telemetry(
                "domain_planned",
                batch_id=batch_id,
                namespace=namespace,
                domain=plan.get("domain"),
                operation=plan.get("operation"),
                status="running",
                planned_sources=list(plan.get("sources") or []),
            )

    def _record_domain_completion(
        self,
        batch_id: str,
        plans: Sequence[Mapping[str, Any]],
        status: str,
        namespace: Optional[str],
    ) -> None:
        for plan in plans:
            self._telemetry(
                "domain_finished",
                batch_id=batch_id,
                namespace=namespace,
                domain=plan.get("domain"),
                operation=plan.get("operation"),
                status=status,
            )

    def start_domain_search(
        self, searches: object, *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        """Start a domain fan-out without holding the MCP request open."""

        domain_requests = validate_domain_search_requests(
            self._apply_namespace(searches, namespace),
            domain_names=self.domain_registry.names,
        )
        expanded, plans = self._expand_domain_searches(domain_requests)
        batch_id = "search-batch-" + uuid.uuid4().hex
        initial = {
            "batch_id": batch_id,
            "status": "running",
            "domain_search_count": len(domain_requests),
            "planned_source_count": len(expanded),
            "domain_plans": plans,
            "started_at": _utc_now(),
            "namespace": namespace,
        }
        with self._domain_batches_lock:
            self._domain_batches[batch_id] = dict(initial)
        self._record_domain_plans(batch_id, plans, namespace)

        def run() -> None:
            try:
                result = self._batch_search(expanded, batch_id=batch_id)
                completed = {
                    **initial,
                    "status": self._domain_outcome(result),
                    "result": result,
                    "completed_at": _utc_now(),
                }
                self._record_domain_completion(
                    batch_id, plans, completed["status"], namespace
                )
            except Exception as exc:
                completed = {
                    **initial,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "completed_at": _utc_now(),
                }
                self._record_domain_completion(batch_id, plans, "failed", namespace)
            with self._domain_batches_lock:
                self._domain_batches[batch_id] = completed

        threading.Thread(
            target=run,
            name=f"search-domain-{batch_id[-8:]}",
            daemon=True,
        ).start()
        return initial

    def get_search_batch(
        self, batch_id: str, *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError("batch_id must be non-empty text")
        with self._domain_batches_lock:
            value = self._domain_batches.get(batch_id)
            if value is not None:
                if namespace is not None and value.get("namespace") != namespace:
                    raise KeyError(f"unknown domain search batch: {batch_id}")
                return dict(value)
        raise KeyError(f"unknown domain search batch: {batch_id}")

    def _persist_discoveries(
        self,
        *,
        hits: Sequence[Mapping[str, Any]],
        request: SearchRequest,
        batch_id: str,
    ) -> tuple[int, int, list[str]]:
        """Associate every provider hit with one logical research request."""

        new_count = 0
        duplicate_count = 0
        new_hit_ids: list[str] = []
        for hit in hits:
            stored_hit = {
                **dict(hit),
                "batch_id": batch_id,
                **request.to_dict(),
                "source_provider": request.provider,
                "pair_key": request.pair_key,
            }
            hit_id, added = self.store.add_hit(stored_hit)
            if added:
                new_count += 1
                new_hit_ids.append(hit_id)
            else:
                duplicate_count += 1
        return new_count, duplicate_count, new_hit_ids

    def _batch_search(
        self, searches: object, *, batch_id: Optional[str] = None
    ) -> dict[str, Any]:
        requests = validate_search_requests(
            searches,
            provider_names=self.registry.names,
        )
        batch_id = batch_id or "search-batch-" + uuid.uuid4().hex
        batch_started = time.monotonic()
        deadline = batch_started + self.batch_timeout_seconds
        summaries: list[dict[str, Any]] = []
        requests_by_pair: dict[str, list[SearchRequest]] = {}
        for request in requests:
            requests_by_pair.setdefault(request.pair_key, []).append(request)
        runnable: list[SearchRequest] = []
        reused_pairs: list[tuple[SearchRequest, list[SearchRequest]]] = []

        for pair_requests in requests_by_pair.values():
            request = pair_requests[0]
            disabled_reason = self.store.disabled_reason(request.provider)
            if disabled_reason is not None:
                for logical_request in pair_requests:
                    summary = self._summary(
                        {
                            **logical_request.to_dict(),
                            "status": "skipped_disabled",
                            "hit_count": 0,
                            "new_unique_hit_count": 0,
                            "duplicate_hit_count": 0,
                            "elapsed_seconds": 0.0,
                            "timed_out": False,
                            "error": disabled_reason,
                            "warnings": [],
                        }
                    )
                    summaries.append(summary)
                    self.store.record_request(
                        {
                            "batch_id": batch_id,
                            **logical_request.to_dict(),
                            "logical_key": logical_request.logical_key,
                            "pair_key": logical_request.pair_key,
                            "status": "skipped_disabled",
                            "hit_count": 0,
                            "error": disabled_reason,
                            "recorded_at": _utc_now(),
                        }
                    )
            elif self.store.has_pair(request.pair_key):
                reused_pairs.append((request, pair_requests))
                self._telemetry(
                    "source_reused",
                    batch_id=batch_id,
                    **request.to_dict(),
                    pair_key=request.pair_key,
                    status="reused_completed",
                    raw_count=len(self.store.hits_for_pair(request.pair_key)),
                    cache_reused=1,
                )
            else:
                runnable.append(request)

        records: list[dict[str, Any]] = []
        if runnable:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(runnable))
            ) as executor:
                future_requests = {
                    executor.submit(
                        self._execute_guarded,
                        request,
                        batch_id=batch_id,
                        deadline=deadline,
                    ): request
                    for request in runnable
                }
                for future in concurrent.futures.as_completed(future_requests):
                    records.append(future.result())

        new_hit_ids: list[str] = []
        for record in sorted(records, key=lambda item: (item["provider"], item["query"])):
            pair_requests = requests_by_pair[record["pair_key"]]
            hit_count = len(record["hits"])
            pair_new_count = 0
            pair_duplicate_count = 0
            logical_summaries: list[dict[str, Any]] = []
            for logical_request in pair_requests:
                new_count, duplicate_count, added_ids = self._persist_discoveries(
                    hits=record["hits"],
                    request=logical_request,
                    batch_id=batch_id,
                )
                pair_new_count += new_count
                pair_duplicate_count += duplicate_count
                new_hit_ids.extend(added_ids)
                logical_summaries.append(
                    self._summary(
                        {
                            **record,
                            **logical_request.to_dict(),
                            "hit_count": hit_count,
                            "new_unique_hit_count": new_count,
                            "duplicate_hit_count": duplicate_count,
                        }
                    )
                )
            persisted = dict(record)
            persisted["hit_count"] = hit_count
            persisted["logical_request_count"] = len(pair_requests)
            persisted["logical_requests"] = [
                request.to_dict() for request in pair_requests
            ]
            persisted["new_unique_hit_count"] = pair_new_count
            persisted["duplicate_hit_count"] = pair_duplicate_count
            self.store.record_provider_result(persisted)
            for logical_request in pair_requests:
                self.store.record_request(
                    {
                        "batch_id": batch_id,
                        **logical_request.to_dict(),
                        "logical_key": logical_request.logical_key,
                        "pair_key": record["pair_key"],
                        "status": record["status"],
                        "hit_count": hit_count,
                        "logical_request_count": len(pair_requests),
                        "recorded_at": _utc_now(),
                    }
                )
            summaries.extend(logical_summaries)

        # A completed provider/query execution is not run again, but a new
        # logical target still receives its own discovery provenance in the
        # current batch. This makes reuse visible instead of silently dropping
        # the second source purpose from the Agent's result pages.
        for representative, pair_requests in reused_pairs:
            existing = self.store.existing_request(representative.pair_key) or {}
            reusable_hits = self.store.hits_for_pair(representative.pair_key)
            for logical_request in pair_requests:
                new_count, duplicate_count, added_ids = self._persist_discoveries(
                    hits=reusable_hits,
                    request=logical_request,
                    batch_id=batch_id,
                )
                new_hit_ids.extend(added_ids)
                reused_summary = self._summary(
                    {
                        **logical_request.to_dict(),
                        "status": "reused_completed",
                        "hit_count": len(reusable_hits),
                        "new_unique_hit_count": new_count,
                        "duplicate_hit_count": duplicate_count,
                        "elapsed_seconds": 0.0,
                        "timed_out": False,
                        "error": None,
                        "warnings": [],
                    }
                )
                reused_summary["existing_batch_id"] = existing.get("batch_id")
                reused_summary["existing_hit_count"] = existing.get("hit_count")
                summaries.append(reused_summary)
                self.store.record_request(
                    {
                        "batch_id": batch_id,
                        **logical_request.to_dict(),
                        "logical_key": logical_request.logical_key,
                        "pair_key": logical_request.pair_key,
                        "status": "reused_completed",
                        "hit_count": len(reusable_hits),
                        "reused_from_batch_id": existing.get("batch_id"),
                        "recorded_at": _utc_now(),
                    }
                )

        summaries.sort(key=lambda item: (str(item["provider"]), str(item["query"])))
        elapsed = round(time.monotonic() - batch_started, 3)
        batch_record = {
            "batch_id": batch_id,
            "provider_result_limit": self.provider_limit,
            "planned_search_count": len(requests),
            "executed_provider_count": sum(
                1 for record in records if record.get("process_started") is True
            ),
            "reused_search_count": len(reused_pairs)
            + sum(1 for record in records if record.get("status") == "reused_completed"),
            "new_unique_hit_count": len(new_hit_ids),
            "elapsed_seconds": elapsed,
            "recorded_at": _utc_now(),
        }
        self.store.record_batch(batch_record)
        self._telemetry(
            "batch_finished",
            batch_id=batch_id,
            status="completed",
            raw_count=sum(len(record.get("hits") or []) for record in records),
            cache_reused=batch_record["reused_search_count"],
            executed_provider_count=batch_record["executed_provider_count"],
            new_unique_hit_count=batch_record["new_unique_hit_count"],
        )
        page = self.store.search_results(cursor=0, limit=20, batch_id=batch_id)
        response = {
            **batch_record,
            "provider_summaries": summaries,
            "disabled_providers": self.store.disabled_providers(),
            "items": page["items"],
            "next_cursor": page["next_cursor"],
            "total_batch_hits": page["unique_hit_count"],
            "total_batch_discoveries": page["total"],
            "reading_note": (
                "These are discovery results, not verified evidence. Use search_results "
                "for every remaining page, get_search_hit for details, then fetch/read "
                "candidate URLs before writing claims."
            ),
        }
        encoded_size = len(
            json.dumps(
                response, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        if encoded_size > _MAX_BATCH_RESPONSE_BYTES:
            response["items"] = []
            response["next_cursor"] = 0 if page["total"] else None
            response["initial_page_omitted_for_transport"] = True
            encoded_size = len(
                json.dumps(
                    response, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        if encoded_size > _MAX_BATCH_RESPONSE_BYTES:
            response["provider_summaries"] = [
                {
                    "provider": str(summary.get("provider") or "")[:80],
                    "query": str(summary.get("query") or "")[:200],
                    "evidence_target": str(
                        summary.get("evidence_target") or ""
                    )[:120],
                    "intent": str(summary.get("intent") or "")[:120],
                    "status": summary.get("status"),
                    "hit_count": summary.get("hit_count"),
                    "new_unique_hit_count": summary.get("new_unique_hit_count"),
                    "duplicate_hit_count": summary.get("duplicate_hit_count"),
                    "elapsed_seconds": summary.get("elapsed_seconds"),
                    "timed_out": summary.get("timed_out"),
                    "error": str(summary.get("error") or "")[:200] or None,
                    "warning_count": len(summary.get("warnings") or []),
                }
                for summary in summaries
            ]
            response["provider_summaries_compacted_for_transport"] = True
            response["disabled_providers"] = {
                str(provider)[:80]: str(reason)[:200]
                for provider, reason in response["disabled_providers"].items()
            }
            encoded_size = len(
                json.dumps(
                    response, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        if encoded_size > _MAX_BATCH_RESPONSE_BYTES:
            raise SearchStoreError("batch search response exceeds transport budget")
        return response

    def search_results(
        self,
        *,
        cursor: int = 0,
        limit: int = 20,
        provider: Optional[str] = None,
        batch_id: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        if provider is not None and provider not in self.registry.names:
            raise ValueError(f"unsupported provider filter: {provider}")
        result = self.store.search_results(
            cursor=cursor,
            limit=limit,
            provider=provider,
            batch_id=batch_id,
            namespace=namespace,
        )
        result["reading_note"] = (
            "Search snippets are only for candidate selection. Fetch/read the source URL "
            "before using information in evidence."
        )
        return result

    def get_search_hit(
        self, hit_id: str, *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        result = self.store.get_search_hit(hit_id, namespace=namespace)
        result["reading_note"] = (
            "This preserves the provider result but is not the source document. Fetch/read "
            "the URL before using it as evidence."
        )
        return result

    def record_fetch(
        self, values: Mapping[str, Any], *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        """Persist a run-scoped fetch result reported by the tool bridge."""
        url = values.get("url")
        if not isinstance(url, str) or not url or len(url) > 8_000:
            raise ValueError("fetch telemetry requires a bounded URL")
        status = values.get("status")
        if status not in {"ok", "failed"}:
            raise ValueError("fetch telemetry status must be ok or failed")
        final_url = values.get("final_url")
        if final_url is not None and (not isinstance(final_url, str) or len(final_url) > 8_000):
            raise ValueError("fetch telemetry final_url is invalid")
        retrieval = values.get("retrieval")
        if retrieval is not None and retrieval not in {"http", "camofox"}:
            raise ValueError("fetch telemetry retrieval is invalid")
        reason = values.get("reason")
        if reason is not None:
            reason = str(reason)[:500]
        elapsed = values.get("elapsed_seconds")
        try:
            elapsed_value = max(0.0, min(float(elapsed), 86_400.0)) if elapsed is not None else None
        except (TypeError, ValueError):
            elapsed_value = None
        self._telemetry(
            "fetch_finished",
            namespace=namespace,
            operation="fetch_url",
            status=status,
            elapsed_seconds=elapsed_value,
            url=url,
            final_url=final_url,
            retrieval=retrieval,
            reason=reason,
        )
        return {"recorded": True}

    def close(self) -> None:
        """Terminate provider process groups if the MCP transport is stopped."""

        with self._active_processes_lock:
            processes = list(self._active_processes)
        for process in processes:
            if process.poll() is None:
                self._terminate_process(process)
        close_store = getattr(self.store, "close", None)
        if callable(close_store):
            close_store()
