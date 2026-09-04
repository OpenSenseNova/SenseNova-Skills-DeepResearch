"""Append-only JSONL fallback for standalone/diagnostic search MCP processes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .results import canonical_hit_keys


# Tool responses must stay comfortably below common MCP/client message limits.
# Pagination is byte-aware, so a caller may receive fewer than ``limit`` items
# and continue from ``next_cursor`` without losing a discovery.
MAX_SEARCH_RESULTS_BYTES = 128 * 1024
MAX_SEARCH_HIT_BYTES = 192 * 1024
MAX_PUBLIC_ITEM_BYTES = 16 * 1024
MAX_PROVENANCE_ITEMS = 32
MAX_HITS_FOR_PAIR = 256

_COMPLETED_PAIR_STATUSES = frozenset({"ok", "empty"})
_PUBLIC_FIELDS = (
    "hit_id",
    "discovery_id",
    "batch_id",
    "source_provider",
    "provider",
    "query",
    "evidence_target",
    "intent",
    "domain",
    "operation",
    "namespace",
    "title",
    "url",
    "snippet",
    "metadata",
)
_FIELD_LIMITS = {
    "hit_id": 200,
    "discovery_id": 200,
    "batch_id": 200,
    "source_provider": 100,
    "provider": 100,
    "query": 500,
    "evidence_target": 1_000,
    "intent": 1_000,
    "title": 800,
    "url": 4_096,
    "snippet": 4_000,
}


class SearchStoreError(RuntimeError):
    pass


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _bounded_text(value: Any, limit: int) -> Any:
    if value is None:
        return None
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # Decode with ``ignore`` only at the truncation boundary so a multibyte
    # character is never split into invalid UTF-8.
    return encoded[: max(0, limit - 3)].decode("utf-8", errors="ignore") + "\u2026"


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Return a deterministic, JSON-compatible preview of nested metadata."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value, 800)
    if depth >= 3:
        return _bounded_text(value, 400)
    if isinstance(value, Mapping):
        return {
            _bounded_text(key, 120): _bounded_json(item, depth=depth + 1)
            for key, item in list(value.items())[:24]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_json(item, depth=depth + 1) for item in value[:16]]
    return _bounded_text(value, 800)


def _pair_key_for_hit(hit: Mapping[str, Any]) -> Optional[str]:
    explicit = hit.get("pair_key")
    if isinstance(explicit, str) and explicit:
        return explicit
    provider = str(hit.get("source_provider") or "").strip()
    query = hit.get("query")
    if not provider or not isinstance(query, str) or not query.strip():
        return None
    normalized_query = re.sub(r"\s+", " ", query).strip().casefold()
    return f"{provider}\u0000{normalized_query}"


class SearchStore:
    """Append-only search ledger with canonical hits and discovery provenance.

    A canonical hit represents one deduplicated candidate document. Every call
    to :meth:`add_hit` also writes a distinct discovery occurrence, including
    the batch, provider/query and logical target that found that candidate.
    Derived indexes are rebuilt from JSONL on startup.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with_context = self.root.stat().st_mode & 0o777
        if with_context & 0o077:
            os.chmod(self.root, 0o700)
        self._lock = threading.RLock()
        self._requests_path = self.root / "requests.jsonl"
        self._providers_path = self.root / "provider-results.jsonl"
        self._hits_path = self.root / "hits.jsonl"
        self._discoveries_path = self.root / "discoveries.jsonl"
        self._batches_path = self.root / "batches.jsonl"
        self._disabled_path = self.root / "disabled-providers.jsonl"
        self._pair_keys: set[str] = set()
        self._requests_by_pair: dict[str, dict[str, Any]] = {}
        self._dedupe_to_hit_id: dict[str, str] = {}
        self._hits_by_id: dict[str, dict[str, Any]] = {}
        self._discoveries_by_id: dict[str, dict[str, Any]] = {}
        self._discoveries_by_hit_id: dict[str, list[dict[str, Any]]] = {}
        self._discoveries_by_pair: dict[str, list[dict[str, Any]]] = {}
        self._disabled: dict[str, str] = {}
        for path in (
            self._requests_path,
            self._providers_path,
            self._hits_path,
            self._discoveries_path,
            self._batches_path,
            self._disabled_path,
        ):
            self._repair_jsonl_tail(path)
        self._load_indexes()

    @staticmethod
    def _repair_jsonl_tail(path: Path) -> None:
        """Make a crash-interrupted final line safe for future appends.

        Ignoring an unterminated fragment while loading is not enough: an
        O_APPEND write would concatenate the next object to that fragment and
        turn it into committed middle-file corruption.  At startup, discard
        only an invalid unterminated tail. A valid final object without a
        newline is preserved and receives the missing delimiter.
        """

        if not path.is_file():
            return
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise SearchStoreError(f"cannot read {path.name}: {exc}") from exc
        if not payload or payload.endswith((b"\n", b"\r")):
            return

        line_start = payload.rfind(b"\n") + 1
        final_line = payload[line_start:]
        try:
            value = json.loads(final_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            truncate_to: Optional[int] = line_start
        else:
            if not isinstance(value, dict):
                # A syntactically complete non-object is corruption, not a
                # recoverable partial write. Keep it intact so _read_jsonl can
                # report the precise line instead of silently deleting it.
                return
            truncate_to = None

        try:
            descriptor = os.open(path, os.O_WRONLY)
        except OSError as exc:
            raise SearchStoreError(f"cannot repair {path.name}: {exc}") from exc
        failure: Optional[SearchStoreError] = None
        try:
            try:
                if truncate_to is not None:
                    os.ftruncate(descriptor, truncate_to)
                else:
                    os.lseek(descriptor, 0, os.SEEK_END)
                    if os.write(descriptor, b"\n") != 1:
                        raise OSError("short repair write")
                os.fsync(descriptor)
            except OSError as exc:
                failure = SearchStoreError(f"cannot repair {path.name}: {exc}")
                failure.__cause__ = exc
        finally:
            try:
                os.close(descriptor)
            except OSError as exc:
                if failure is None:
                    failure = SearchStoreError(
                        f"cannot close repaired {path.name}: {exc}"
                    )
                    failure.__cause__ = exc
        if failure is not None:
            raise failure

    @staticmethod
    def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
        """Read JSONL, tolerating only an invalid, unterminated final fragment."""

        if not path.is_file():
            return ()
        records: list[dict[str, Any]] = []
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise SearchStoreError(f"cannot read {path.name}: {exc}") from exc
        lines = payload.splitlines(keepends=True)
        for index, raw_line in enumerate(lines):
            line_number = index + 1
            is_last = index == len(lines) - 1
            terminated = raw_line.endswith((b"\n", b"\r"))
            try:
                text = raw_line.decode("utf-8")
                if not text.strip():
                    continue
                value = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # A process can die between writes. Only the final line, and
                # only when no newline committed it as a complete record, is a
                # recoverable crash fragment. Corruption elsewhere is fatal.
                if is_last and not terminated:
                    break
                raise SearchStoreError(
                    f"invalid JSONL in {path.name}:{line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise SearchStoreError(
                    f"non-object JSONL record in {path.name}:{line_number}"
                )
            records.append(value)
        return records

    def _index_discovery(self, discovery: Mapping[str, Any]) -> None:
        discovery_id = discovery.get("discovery_id")
        hit_id = discovery.get("hit_id")
        if not isinstance(discovery_id, str) or not discovery_id:
            raise SearchStoreError("discovery record is missing discovery_id")
        if not isinstance(hit_id, str) or hit_id not in self._hits_by_id:
            raise SearchStoreError(
                f"discovery {discovery_id} references an unknown canonical hit"
            )
        if discovery_id in self._discoveries_by_id:
            raise SearchStoreError(f"duplicate discovery_id: {discovery_id}")
        stored = dict(discovery)
        self._discoveries_by_id[discovery_id] = stored
        self._discoveries_by_hit_id.setdefault(hit_id, []).append(stored)
        pair_key = stored.get("pair_key")
        if isinstance(pair_key, str) and pair_key:
            self._discoveries_by_pair.setdefault(pair_key, []).append(stored)
        keys = stored.get("dedupe_keys")
        if isinstance(keys, list):
            for key in keys:
                if isinstance(key, str):
                    self._dedupe_to_hit_id.setdefault(key, hit_id)

    def _load_indexes(self) -> None:
        for record in self._read_jsonl(self._requests_path):
            pair_key = record.get("pair_key")
            status = record.get("status")
            if isinstance(pair_key, str) and status in _COMPLETED_PAIR_STATUSES:
                self._pair_keys.add(pair_key)
                self._requests_by_pair[pair_key] = record
        for hit in self._read_jsonl(self._hits_path):
            hit_id = hit.get("hit_id")
            if not isinstance(hit_id, str):
                continue
            self._hits_by_id[hit_id] = hit
            keys = hit.get("dedupe_keys")
            if isinstance(keys, list):
                for key in keys:
                    if isinstance(key, str):
                        self._dedupe_to_hit_id.setdefault(key, hit_id)
        for discovery in self._read_jsonl(self._discoveries_path):
            self._index_discovery(discovery)

        # Compatibility for attempt directories written before discovery
        # provenance existed. These deterministic in-memory occurrences keep
        # old search results readable without mutating the old ledger.
        for hit_id, hit in self._hits_by_id.items():
            if hit_id in self._discoveries_by_hit_id:
                continue
            legacy = dict(hit)
            legacy["discovery_id"] = f"legacy-{hit_id}"
            legacy["hit_id"] = hit_id
            pair_key = _pair_key_for_hit(hit)
            if pair_key is not None:
                legacy["pair_key"] = pair_key
            self._index_discovery(legacy)

        for record in self._read_jsonl(self._disabled_path):
            provider = record.get("provider")
            reason = record.get("reason")
            if isinstance(provider, str) and isinstance(reason, str):
                self._disabled[provider] = reason

    @staticmethod
    def _append(path: Path, value: Mapping[str, Any]) -> None:
        payload = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        except OSError as exc:
            raise SearchStoreError(f"cannot open {path.name} for append: {exc}") from exc

        failure: Optional[SearchStoreError] = None
        try:
            original_size: Optional[int]
            try:
                original_size = os.fstat(descriptor).st_size
            except OSError as exc:
                failure = SearchStoreError(
                    f"cannot inspect {path.name} before append: {exc}"
                )
                failure.__cause__ = exc
                original_size = None
            offset = 0
            while failure is None and offset < len(payload):
                try:
                    written = os.write(descriptor, payload[offset:])
                except OSError as exc:
                    failure = SearchStoreError(f"cannot append to {path.name}: {exc}")
                    failure.__cause__ = exc
                    break
                if written <= 0:
                    failure = SearchStoreError(f"short append to {path.name}")
                    break
                offset += written
            if failure is None:
                try:
                    os.fsync(descriptor)
                except OSError as exc:
                    failure = SearchStoreError(
                        f"cannot sync append to {path.name}: {exc}"
                    )
                    failure.__cause__ = exc
            if failure is not None and original_size is not None:
                # A short write followed by ENOSPC must not turn an incomplete
                # JSON fragment into a committed line when the next append
                # succeeds. Restore the exact pre-call EOF before returning.
                try:
                    os.ftruncate(descriptor, original_size)
                    os.fsync(descriptor)
                except OSError as rollback_exc:
                    failure = SearchStoreError(
                        f"{failure}; cannot roll back {path.name} to "
                        f"{original_size} bytes: {rollback_exc}"
                    )
                    failure.__cause__ = rollback_exc
        finally:
            try:
                os.close(descriptor)
            except OSError as exc:
                if failure is None:
                    failure = SearchStoreError(f"cannot close {path.name}: {exc}")
                    failure.__cause__ = exc
        if failure is not None:
            raise failure

    def has_pair(self, pair_key: str) -> bool:
        with self._lock:
            return pair_key in self._pair_keys

    def existing_request(self, pair_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            value = self._requests_by_pair.get(pair_key)
            return dict(value) if value is not None else None

    def record_request(self, record: Mapping[str, Any]) -> None:
        pair_key = record.get("pair_key")
        if not isinstance(pair_key, str):
            raise SearchStoreError("request record is missing pair_key")
        with self._lock:
            self._append(self._requests_path, record)
            if record.get("status") in _COMPLETED_PAIR_STATUSES:
                self._pair_keys.add(pair_key)
                self._requests_by_pair[pair_key] = dict(record)

    def record_provider_result(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._append(self._providers_path, record)

    def add_hit(self, hit: Mapping[str, Any]) -> tuple[str, bool]:
        """Persist one discovery and return its canonical id/newness.

        The Boolean retains the original service contract: it is true only
        when this call created a canonical hit. A false value still means a
        new discovery record was durably appended.
        """

        keys = canonical_hit_keys(hit)
        if not keys:
            raise SearchStoreError("hit has no stable title, URL, or identifier")
        with self._lock:
            duplicate_id = next(
                (
                    known_id
                    for key in keys
                    if (known_id := self._dedupe_to_hit_id.get(key)) is not None
                ),
                None,
            )
            added = duplicate_id is None
            if duplicate_id is None:
                digest_input = "\u0000".join(keys).encode("utf-8")
                hit_id = "hit-" + hashlib.sha256(digest_input).hexdigest()[:20]
                # A short stable id is normally unique. Resolve a theoretical
                # hash collision without replacing an old canonical record.
                suffix = 1
                base = hit_id
                while hit_id in self._hits_by_id:
                    suffix += 1
                    hit_id = f"{base}-{suffix}"
                canonical = dict(hit)
                canonical["hit_id"] = hit_id
                canonical.pop("discovery_id", None)
                canonical["dedupe_keys"] = list(keys)
                self._append(self._hits_path, canonical)
                # Update canonical indexes immediately: if the following
                # discovery append fails, a retry adds the missing occurrence
                # rather than writing a second canonical record.
                self._hits_by_id[hit_id] = canonical
                for key in keys:
                    self._dedupe_to_hit_id.setdefault(key, hit_id)
            else:
                hit_id = duplicate_id

            discovery = dict(hit)
            discovery["hit_id"] = hit_id
            discovery["discovery_id"] = "discovery-" + uuid.uuid4().hex
            discovery["dedupe_keys"] = list(keys)
            pair_key = _pair_key_for_hit(hit)
            if pair_key is not None:
                discovery["pair_key"] = pair_key
            self._append(self._discoveries_path, discovery)
            self._index_discovery(discovery)
            # A later provider can add a stronger identifier for an existing
            # canonical URL. Persisting keys on the discovery makes that alias
            # survive process restart.
            for key in keys:
                self._dedupe_to_hit_id.setdefault(key, hit_id)
            return hit_id, added

    def disable_provider(self, provider: str, reason: str) -> None:
        with self._lock:
            if provider in self._disabled:
                return
            bounded = reason[:1_000]
            self._append(
                self._disabled_path,
                {"provider": provider, "reason": bounded},
            )
            self._disabled[provider] = bounded

    def disabled_reason(self, provider: str) -> Optional[str]:
        with self._lock:
            return self._disabled.get(provider)

    def disabled_providers(self) -> dict[str, str]:
        with self._lock:
            return dict(self._disabled)

    def record_batch(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._append(self._batches_path, record)

    @staticmethod
    def _public_hit(hit: Mapping[str, Any], *, detailed: bool) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key in _PUBLIC_FIELDS:
            item = hit.get(key)
            if key == "metadata":
                value[key] = _bounded_json(item if isinstance(item, Mapping) else {})
            elif key == "snippet" and not detailed:
                value[key] = _bounded_text(item or "", 600)
            else:
                value[key] = _bounded_text(item, _FIELD_LIMITS.get(key, 1_000))
        if detailed:
            value["raw_item"] = _bounded_json(hit.get("raw_item"))
            value["raw_item_truncated"] = bool(hit.get("raw_item_truncated"))

        # Nested provider metadata can still be wider than anticipated. Keep
        # the identity and source URL even if the preview must be reduced.
        if _json_bytes(value) > MAX_PUBLIC_ITEM_BYTES:
            value["metadata"] = {"_truncated_for_transport": True}
        if _json_bytes(value) > MAX_PUBLIC_ITEM_BYTES:
            value["snippet"] = _bounded_text(value.get("snippet") or "", 300)
            if detailed:
                value["raw_item"] = {"_truncated_for_transport": True}
                value["raw_item_truncated"] = True
        if _json_bytes(value) > MAX_PUBLIC_ITEM_BYTES:
            value["evidence_target"] = _bounded_text(
                value.get("evidence_target"), 300
            )
            value["intent"] = _bounded_text(value.get("intent"), 300)
            value["url"] = _bounded_text(value.get("url"), 2_048)
        if _json_bytes(value) > MAX_PUBLIC_ITEM_BYTES:
            raise SearchStoreError("public search hit exceeds transport budget")
        return value

    def _public_discovery(
        self, discovery: Mapping[str, Any], *, detailed: bool
    ) -> dict[str, Any]:
        hit_id = discovery.get("hit_id")
        canonical = self._hits_by_id.get(str(hit_id))
        if canonical is None:
            raise SearchStoreError(f"unknown canonical hit for discovery: {hit_id}")
        merged = dict(canonical)
        # Occurrence fields preserve the exact provider/query/target and the
        # provider-specific representation that produced this discovery.
        merged.update(discovery)
        return self._public_hit(merged, detailed=detailed)

    def search_results(
        self,
        *,
        cursor: int = 0,
        limit: int = 20,
        provider: Optional[str] = None,
        batch_id: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise SearchStoreError("cursor must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise SearchStoreError("limit must be an integer between 1 and 50")
        with self._lock:
            selected = [
                discovery
                for discovery in self._discoveries_by_id.values()
                if (
                    provider is None
                    or discovery.get("source_provider") == provider
                )
                and (batch_id is None or discovery.get("batch_id") == batch_id)
                and (namespace is None or discovery.get("namespace") == namespace)
            ]
            unique_hit_count = len(
                {
                    str(item.get("hit_id"))
                    for item in selected
                    if item.get("hit_id")
                }
            )
            items: list[dict[str, Any]] = []
            for discovery in selected[cursor : cursor + limit]:
                public = self._public_discovery(discovery, detailed=False)
                trial_count = len(items) + 1
                trial_cursor = cursor + trial_count
                trial = {
                    "items": [*items, public],
                    "cursor": cursor,
                    "next_cursor": (
                        trial_cursor if trial_cursor < len(selected) else None
                    ),
                    "total": len(selected),
                    "unique_hit_count": unique_hit_count,
                }
                if _json_bytes(trial) > MAX_SEARCH_RESULTS_BYTES:
                    break
                items.append(public)
            next_cursor = cursor + len(items)
            result = {
                "items": items,
                "cursor": cursor,
                "next_cursor": next_cursor if next_cursor < len(selected) else None,
                "total": len(selected),
                "unique_hit_count": unique_hit_count,
            }
            if _json_bytes(result) > MAX_SEARCH_RESULTS_BYTES:
                raise SearchStoreError("search result page exceeds transport budget")
            return result

    def get_search_hit(
        self, hit_id: str, *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        """Read a canonical hit or one discovery occurrence in full detail.

        Result pages expose both identifiers. Passing a ``discovery_id`` keeps
        the second provider's raw representation accessible even when several
        discoveries share one canonical ``hit_id``.
        """

        with self._lock:
            selected_discovery = self._discoveries_by_id.get(hit_id)
            if (
                selected_discovery is not None
                and namespace is not None
                and selected_discovery.get("namespace") != namespace
            ):
                raise SearchStoreError(f"unknown search hit: {hit_id}")
            canonical_id = (
                selected_discovery.get("hit_id")
                if selected_discovery is not None
                else hit_id
            )
            hit = self._hits_by_id.get(str(canonical_id))
            if hit is None:
                raise SearchStoreError(f"unknown search hit: {hit_id}")
            if selected_discovery is None and namespace is not None:
                visible = any(
                    item.get("namespace") == namespace
                    for item in self._discoveries_by_hit_id.get(str(canonical_id), [])
                )
                if not visible:
                    raise SearchStoreError(f"unknown search hit: {hit_id}")
            value = (
                self._public_discovery(selected_discovery, detailed=True)
                if selected_discovery is not None
                else self._public_hit(hit, detailed=True)
            )
            value["selected_discovery_id"] = (
                selected_discovery.get("discovery_id")
                if selected_discovery is not None
                else None
            )
            discoveries = [
                item
                for item in self._discoveries_by_hit_id.get(str(canonical_id), [])
                if namespace is None or item.get("namespace") == namespace
            ]
            provenance: list[dict[str, Any]] = []
            for discovery in discoveries[:MAX_PROVENANCE_ITEMS]:
                occurrence = self._public_discovery(discovery, detailed=False)
                occurrence.pop("metadata", None)
                occurrence.pop("snippet", None)
                trial = {
                    **value,
                    "discovery_count": len(discoveries),
                    "provenance": [*provenance, occurrence],
                    "provenance_returned": len(provenance) + 1,
                    "provenance_truncated": len(provenance) + 1 < len(discoveries),
                }
                if _json_bytes(trial) > MAX_SEARCH_HIT_BYTES:
                    break
                provenance.append(occurrence)
            value["discovery_count"] = len(discoveries)
            value["provenance"] = provenance
            value["provenance_returned"] = len(provenance)
            value["provenance_truncated"] = len(provenance) < len(discoveries)
            if _json_bytes(value) > MAX_SEARCH_HIT_BYTES:
                raise SearchStoreError("search hit detail exceeds transport budget")
            return value

    def hits_for_pair(
        self, pair_key: str, *, limit: int = MAX_HITS_FOR_PAIR
    ) -> list[dict[str, Any]]:
        """Return one bounded occurrence per canonical hit for an executed pair.

        This internal replay surface deliberately deduplicates by ``hit_id`` so
        repeated executions or logical targets cannot produce a Cartesian
        expansion when a service associates old results with a new batch.
        """

        if not isinstance(pair_key, str) or not pair_key:
            raise SearchStoreError("pair_key must be a non-empty string")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_HITS_FOR_PAIR
        ):
            raise SearchStoreError(
                f"limit must be an integer between 1 and {MAX_HITS_FOR_PAIR}"
            )
        with self._lock:
            values: list[dict[str, Any]] = []
            seen: set[str] = set()
            for discovery in self._discoveries_by_pair.get(pair_key, []):
                hit_id = discovery.get("hit_id")
                if not isinstance(hit_id, str) or hit_id in seen:
                    continue
                seen.add(hit_id)
                values.append(self._public_discovery(discovery, detailed=True))
                if len(values) >= limit:
                    break
            return values
