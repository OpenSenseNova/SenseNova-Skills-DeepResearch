"""Run-scoped SQLite persistence for the shared search coordinator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from .results import canonical_hit_keys
from .store import (
    MAX_HITS_FOR_PAIR,
    MAX_PROVENANCE_ITEMS,
    MAX_SEARCH_HIT_BYTES,
    MAX_SEARCH_RESULTS_BYTES,
    SearchStore,
    SearchStoreError,
    _json_bytes,
    _pair_key_for_hit,
)


_COMPLETED = frozenset({"ok", "empty"})


class SQLiteSearchStore:
    """Thread-safe WAL ledger shared by every Research attempt in one run."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=30.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_pair ON requests(pair_key, id);
                CREATE TABLE IF NOT EXISTS provider_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hits (
                    hit_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dedupe_keys (
                    dedupe_key TEXT PRIMARY KEY,
                    hit_id TEXT NOT NULL REFERENCES hits(hit_id)
                );
                CREATE TABLE IF NOT EXISTS discoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discovery_id TEXT UNIQUE NOT NULL,
                    hit_id TEXT NOT NULL REFERENCES hits(hit_id),
                    pair_key TEXT,
                    batch_id TEXT,
                    provider TEXT,
                    namespace TEXT,
                    domain TEXT,
                    operation TEXT,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS discoveries_page
                    ON discoveries(namespace, batch_id, provider, id);
                CREATE INDEX IF NOT EXISTS discoveries_hit ON discoveries(hit_id, id);
                CREATE INDEX IF NOT EXISTS discoveries_pair ON discoveries(pair_key, id);
                CREATE TABLE IF NOT EXISTS disabled (
                    provider TEXT PRIMARY KEY,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    batch_id TEXT,
                    namespace TEXT,
                    domain TEXT,
                    operation TEXT,
                    provider TEXT,
                    pair_key TEXT,
                    status TEXT,
                    elapsed_seconds REAL,
                    raw_count INTEGER NOT NULL DEFAULT 0,
                    cache_reused INTEGER NOT NULL DEFAULT 0,
                    process_started INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS telemetry_domain
                    ON telemetry(domain, batch_id, id);
                CREATE INDEX IF NOT EXISTS telemetry_provider
                    ON telemetry(provider, id);
                """
            )
            discovery_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(discoveries)")
            }
            for column in ("domain", "operation"):
                if column not in discovery_columns:
                    self._connection.execute(
                        f"ALTER TABLE discoveries ADD COLUMN {column} TEXT"
                    )
            self._connection.commit()

    @staticmethod
    def _dump(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> dict[str, Any]:
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise SearchStoreError("SQLite search payload is not an object")
        return loaded

    def has_pair(self, pair_key: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM requests WHERE pair_key=? AND status IN ('ok','empty') LIMIT 1",
                (pair_key,),
            ).fetchone()
            return row is not None

    def existing_request(self, pair_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM requests WHERE pair_key=? AND status IN ('ok','empty') "
                "ORDER BY id DESC LIMIT 1",
                (pair_key,),
            ).fetchone()
            return self._load(row["payload"]) if row is not None else None

    def record_request(self, record: Mapping[str, Any]) -> None:
        pair_key = record.get("pair_key")
        if not isinstance(pair_key, str):
            raise SearchStoreError("request record is missing pair_key")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO requests(pair_key,status,payload) VALUES(?,?,?)",
                (pair_key, str(record.get("status") or ""), self._dump(record)),
            )

    def record_provider_result(self, record: Mapping[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO provider_results(payload) VALUES(?)", (self._dump(record),)
            )

    def add_hit(self, hit: Mapping[str, Any]) -> tuple[str, bool]:
        keys = canonical_hit_keys(hit)
        if not keys:
            raise SearchStoreError("hit has no stable title, URL, or identifier")
        with self._lock, self._connection:
            placeholders = ",".join("?" for _ in keys)
            row = self._connection.execute(
                f"SELECT hit_id FROM dedupe_keys WHERE dedupe_key IN ({placeholders}) LIMIT 1",
                tuple(keys),
            ).fetchone()
            added = row is None
            if row is None:
                base = "hit-" + hashlib.sha256(
                    "\u0000".join(keys).encode("utf-8")
                ).hexdigest()[:20]
                hit_id = base
                suffix = 1
                while self._connection.execute(
                    "SELECT 1 FROM hits WHERE hit_id=?", (hit_id,)
                ).fetchone() is not None:
                    suffix += 1
                    hit_id = f"{base}-{suffix}"
                canonical = dict(hit)
                canonical.pop("discovery_id", None)
                canonical["hit_id"] = hit_id
                canonical["dedupe_keys"] = list(keys)
                self._connection.execute(
                    "INSERT INTO hits(hit_id,payload) VALUES(?,?)",
                    (hit_id, self._dump(canonical)),
                )
            else:
                hit_id = str(row["hit_id"])
            for key in keys:
                self._connection.execute(
                    "INSERT OR IGNORE INTO dedupe_keys(dedupe_key,hit_id) VALUES(?,?)",
                    (key, hit_id),
                )
            discovery = dict(hit)
            discovery_id = "discovery-" + uuid.uuid4().hex
            discovery["hit_id"] = hit_id
            discovery["discovery_id"] = discovery_id
            discovery["dedupe_keys"] = list(keys)
            pair_key = _pair_key_for_hit(hit)
            if pair_key is not None:
                discovery["pair_key"] = pair_key
            self._connection.execute(
                "INSERT INTO discoveries(discovery_id,hit_id,pair_key,batch_id,provider,namespace,domain,operation,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    discovery_id,
                    hit_id,
                    pair_key,
                    discovery.get("batch_id"),
                    discovery.get("source_provider"),
                    discovery.get("namespace"),
                    discovery.get("domain"),
                    discovery.get("operation"),
                    self._dump(discovery),
                ),
            )
            return hit_id, added

    def disable_provider(self, provider: str, reason: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO disabled(provider,reason) VALUES(?,?)",
                (provider, reason[:1_000]),
            )

    def disabled_reason(self, provider: str) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT reason FROM disabled WHERE provider=?", (provider,)
            ).fetchone()
            return str(row["reason"]) if row is not None else None

    def disabled_providers(self) -> dict[str, str]:
        with self._lock:
            return {
                str(row["provider"]): str(row["reason"])
                for row in self._connection.execute(
                    "SELECT provider,reason FROM disabled ORDER BY provider"
                )
            }

    def record_batch(self, record: Mapping[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO batches(batch_id,payload) VALUES(?,?)",
                (record.get("batch_id"), self._dump(record)),
            )

    def record_telemetry(self, record: Mapping[str, Any]) -> None:
        event_type = record.get("event_type")
        recorded_at = record.get("recorded_at")
        if not isinstance(event_type, str) or not event_type:
            raise SearchStoreError("telemetry record is missing event_type")
        if not isinstance(recorded_at, str) or not recorded_at:
            raise SearchStoreError("telemetry record is missing recorded_at")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO telemetry(recorded_at,event_type,batch_id,namespace,domain,"
                "operation,provider,pair_key,status,elapsed_seconds,raw_count,cache_reused,"
                "process_started,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    recorded_at,
                    event_type,
                    record.get("batch_id"),
                    record.get("namespace"),
                    record.get("domain"),
                    record.get("operation"),
                    record.get("provider"),
                    record.get("pair_key"),
                    record.get("status"),
                    record.get("elapsed_seconds"),
                    int(record.get("raw_count") or 0),
                    int(record.get("cache_reused") or 0),
                    int(bool(record.get("process_started"))),
                    self._dump(record),
                ),
            )

    def _canonical(self, hit_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload FROM hits WHERE hit_id=?", (hit_id,)
        ).fetchone()
        if row is None:
            raise SearchStoreError(f"unknown search hit: {hit_id}")
        return self._load(row["payload"])

    def _merged(self, discovery: Mapping[str, Any], *, detailed: bool) -> dict[str, Any]:
        merged = self._canonical(str(discovery["hit_id"]))
        merged.update(discovery)
        return SearchStore._public_hit(merged, detailed=detailed)

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
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (("provider", provider), ("batch_id", batch_id), ("namespace", namespace)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM discoveries" + where + " ORDER BY id",
                tuple(values),
            ).fetchall()
            discoveries = [self._load(row["payload"]) for row in rows]
            unique_count = len({item["hit_id"] for item in discoveries})
            items: list[dict[str, Any]] = []
            for discovery in discoveries[cursor : cursor + limit]:
                public = self._merged(discovery, detailed=False)
                trial_next = cursor + len(items) + 1
                trial = {
                    "items": [*items, public],
                    "cursor": cursor,
                    "next_cursor": trial_next if trial_next < len(discoveries) else None,
                    "total": len(discoveries),
                    "unique_hit_count": unique_count,
                }
                if _json_bytes(trial) > MAX_SEARCH_RESULTS_BYTES:
                    break
                items.append(public)
            next_cursor = cursor + len(items)
            return {
                "items": items,
                "cursor": cursor,
                "next_cursor": next_cursor if next_cursor < len(discoveries) else None,
                "total": len(discoveries),
                "unique_hit_count": unique_count,
            }

    def get_search_hit(
        self, hit_id: str, *, namespace: Optional[str] = None
    ) -> dict[str, Any]:
        with self._lock:
            selected = self._connection.execute(
                "SELECT payload FROM discoveries WHERE discovery_id=?", (hit_id,)
            ).fetchone()
            discovery = self._load(selected["payload"]) if selected is not None else None
            if discovery is not None and namespace is not None and discovery.get("namespace") != namespace:
                raise SearchStoreError(f"unknown search hit: {hit_id}")
            canonical_id = str(discovery["hit_id"]) if discovery is not None else hit_id
            canonical = self._canonical(canonical_id)
            query = "SELECT payload FROM discoveries WHERE hit_id=?"
            values: list[Any] = [canonical_id]
            if namespace is not None:
                query += " AND namespace=?"
                values.append(namespace)
            query += " ORDER BY id"
            rows = self._connection.execute(query, tuple(values)).fetchall()
            discoveries = [self._load(row["payload"]) for row in rows]
            if namespace is not None and not discoveries:
                raise SearchStoreError(f"unknown search hit: {hit_id}")
            value = (
                self._merged(discovery, detailed=True)
                if discovery is not None
                else SearchStore._public_hit(canonical, detailed=True)
            )
            value["selected_discovery_id"] = discovery.get("discovery_id") if discovery else None
            provenance: list[dict[str, Any]] = []
            for item in discoveries[:MAX_PROVENANCE_ITEMS]:
                occurrence = self._merged(item, detailed=False)
                occurrence.pop("metadata", None)
                occurrence.pop("snippet", None)
                trial = {**value, "provenance": [*provenance, occurrence]}
                if _json_bytes(trial) > MAX_SEARCH_HIT_BYTES:
                    break
                provenance.append(occurrence)
            value.update(
                discovery_count=len(discoveries),
                provenance=provenance,
                provenance_returned=len(provenance),
                provenance_truncated=len(provenance) < len(discoveries),
            )
            return value

    def hits_for_pair(
        self, pair_key: str, *, limit: int = MAX_HITS_FOR_PAIR
    ) -> list[dict[str, Any]]:
        if not isinstance(pair_key, str) or not pair_key:
            raise SearchStoreError("pair_key must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HITS_FOR_PAIR:
            raise SearchStoreError(f"limit must be between 1 and {MAX_HITS_FOR_PAIR}")
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM discoveries WHERE pair_key=? ORDER BY id",
                (pair_key,),
            ).fetchall()
            output: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                discovery = self._load(row["payload"])
                canonical_id = str(discovery["hit_id"])
                if canonical_id in seen:
                    continue
                seen.add(canonical_id)
                output.append(self._merged(discovery, detailed=True))
                if len(output) >= limit:
                    break
            return output

    def close(self) -> None:
        with self._lock:
            self._connection.close()
