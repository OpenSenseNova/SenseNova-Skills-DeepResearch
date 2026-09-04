"""Read-only search observability projection for the Web console."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(r"https?://[^\s<>'\"\]\[)]+", re.IGNORECASE)
_FETCH_HINT = re.compile(r"\b(fetch|browser|open url|read url|网页|抓取)\b", re.IGNORECASE)


def _normalized_url(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator * 100.0 / denominator, 1)


def _fetched_attempts(run_dir: Path) -> tuple[int, int]:
    identities: set[str] = set()
    newest_ns = 0
    attempts = run_dir / "attempts"
    if not attempts.is_dir():
        return 0, newest_ns
    for path in attempts.glob("*/attempt-*/acp-events.jsonl"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            newest_ns = max(newest_ns, path.stat().st_mtime_ns)
            starts: dict[str, tuple[str, str]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if not isinstance(value, dict):
                    continue
                call_id = value.get("toolCallId")
                if not isinstance(call_id, str) or not call_id:
                    continue
                if value.get("sessionUpdate") == "tool_call":
                    starts[call_id] = (
                        str(value.get("kind") or ""),
                        str(value.get("title") or ""),
                    )
                    continue
                if (
                    value.get("sessionUpdate") != "tool_call_update"
                    or value.get("status") != "completed"
                ):
                    continue
                kind, title = starts.get(
                    call_id,
                    (str(value.get("kind") or ""), str(value.get("title") or "")),
                )
                match = _URL.search(title)
                looks_like_fetch = kind.casefold() in {"fetch", "browser"} or bool(
                    _FETCH_HINT.search(f"{kind} {title}")
                )
                if not looks_like_fetch:
                    continue
                url = _normalized_url(match.group(0)) if match else None
                identities.add(url or f"{path}:{call_id}")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return len(identities), newest_ns


def _fetch_telemetry(events: Iterable[sqlite3.Row]) -> tuple[dict[str, int], int]:
    """Return fetch counts from explicit bridge telemetry.

    Fetches are executed through the native ``execute`` tool for some
    harnesses, so ACP event titles are not a reliable source of truth.
    """
    successful: set[str] = set()
    attempts = failed = http = camofox = 0
    for row in events:
        if str(row["event_type"] or "") != "fetch_finished":
            continue
        attempts += 1
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        status = str(row["status"] or "")
        retrieval = str(payload.get("retrieval") or "")
        if status != "ok":
            failed += 1
            continue
        value = _normalized_url(payload.get("final_url") or payload.get("url"))
        if value:
            successful.add(value)
        if retrieval == "http":
            http += 1
        elif retrieval == "camofox":
            camofox += 1
    return {
        "attempts": attempts,
        "succeeded": len(successful),
        "failed": failed,
        "http": http,
        "camofox": camofox,
    }, len(successful)


def empty_search_metrics(*, evidence_count: int = 0) -> dict[str, Any]:
    return {
        "version": "0:0:0",
        "status": "idle",
        "domains": [],
        "sources": [],
        "api_calls": 0,
        "cache_reused": 0,
        "fetch": {"attempts": 0, "succeeded": 0, "failed": 0, "http": 0, "camofox": 0},
        "funnel": {
            "raw": 0,
            "unique": 0,
            "fetched": 0,
            "evidence": evidence_count,
            "rates": {
                "deduplicated": None,
                "fetched": None,
                "evidence": None,
            },
        },
    }


def build_search_metrics(
    run_dir: Path, *, evidence_urls: Iterable[str] = ()
) -> dict[str, Any]:
    evidence = {
        normalized
        for value in evidence_urls
        if (normalized := _normalized_url(value)) is not None
    }
    fetched, fetched_version = _fetched_attempts(run_dir)
    database = run_dir / "search" / "search.sqlite3"
    if not database.is_file() or database.is_symlink():
        value = empty_search_metrics(evidence_count=len(evidence))
        value["funnel"]["fetched"] = fetched
        fetched = max(fetched, len(evidence))
        value["funnel"]["fetched"] = fetched
        value["funnel"]["rates"]["evidence"] = _rate(len(evidence), fetched)
        value["version"] = f"0:{fetched_version}:{len(evidence)}"
        return value

    try:
        connection = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=0.25
        )
        connection.row_factory = sqlite3.Row
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        events = (
            connection.execute("SELECT * FROM telemetry ORDER BY id").fetchall()
            if "telemetry" in tables
            else []
        )
        provider_records = (
            [json.loads(row["payload"]) for row in connection.execute(
                "SELECT payload FROM provider_results ORDER BY id"
            )]
            if "provider_results" in tables
            else []
        )
        unique = (
            int(connection.execute("SELECT COUNT(*) FROM hits").fetchone()[0])
            if "hits" in tables
            else 0
        )
        discovery_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(discoveries)")
        }
        if "domain" in discovery_columns:
            unique_by_domain = {
                str(row["domain"]): int(row["total"])
                for row in connection.execute(
                    "SELECT domain, COUNT(DISTINCT hit_id) AS total FROM discoveries "
                    "WHERE domain IS NOT NULL GROUP BY domain"
                )
            }
        else:
            ids_by_domain: dict[str, set[str]] = defaultdict(set)
            for row in connection.execute("SELECT hit_id,payload FROM discoveries"):
                payload = json.loads(row["payload"])
                domain = payload.get("domain") if isinstance(payload, dict) else None
                if domain:
                    ids_by_domain[str(domain)].add(str(row["hit_id"]))
            unique_by_domain = {
                domain: len(hit_ids) for domain, hit_ids in ids_by_domain.items()
            }
    except (OSError, sqlite3.Error):
        value = empty_search_metrics(evidence_count=len(evidence))
        value["funnel"]["fetched"] = fetched
        fetched = max(fetched, len(evidence))
        value["funnel"]["fetched"] = fetched
        value["funnel"]["rates"]["evidence"] = _rate(len(evidence), fetched)
        value["version"] = f"0:{fetched_version}:{len(evidence)}"
        return value
    finally:
        if "connection" in locals():
            connection.close()

    fetch_stats, explicit_fetched = _fetch_telemetry(events)
    if fetch_stats["attempts"]:
        fetched = explicit_fetched
        fetched_version = max(fetched_version, int(events[-1]["id"]) if events else 0)
    # Evidence sources are completed reads even when a native Harness or
    # cache path did not emit explicit fetch telemetry. Include them in the
    # fetched stage so the funnel remains monotonic.
    fetched = max(fetched, len(evidence))

    sources: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "cache_reused": 0,
            "raw": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "total_seconds": 0.0,
            "max_seconds": 0.0,
        }
    )
    domains: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "operations": set(),
            "planned": 0,
            "completed": 0,
            "running": 0,
            "failed": 0,
            "raw": 0,
            "api_calls": 0,
            "cache_reused": 0,
            "finished_statuses": [],
        }
    )
    active_sources: dict[tuple[str, str, str], tuple[str, str]] = {}
    raw = api_calls = cache_reused = 0
    observed_execution_keys: set[tuple[str, str]] = set()
    last_id = 0
    for row in events:
        last_id = max(last_id, int(row["id"]))
        event_type = str(row["event_type"])
        domain = str(row["domain"] or "")
        provider = str(row["provider"] or "")
        operation = str(row["operation"] or "")
        key = (
            str(row["batch_id"] or ""),
            str(row["namespace"] or ""),
            str(row["pair_key"] or ""),
        )
        if domain and operation:
            domains[domain]["operations"].add(operation)
        if event_type == "domain_planned" and domain:
            try:
                payload = json.loads(row["payload"])
                planned = len(payload.get("planned_sources") or [])
            except (TypeError, json.JSONDecodeError):
                planned = 0
            domains[domain]["planned"] += planned
        elif event_type == "domain_finished" and domain:
            domains[domain]["finished_statuses"].append(str(row["status"] or ""))
        elif event_type == "source_started":
            active_sources[key] = (provider, domain)
        elif event_type in {"source_finished", "source_reused"}:
            active_sources.pop(key, None)
            raw_count = int(row["raw_count"] or 0)
            reused = int(row["cache_reused"] or 0)
            if event_type == "source_finished":
                raw += raw_count
            cache_reused += reused
            if provider:
                item = sources[provider]
                item["raw"] += raw_count if event_type == "source_finished" else 0
                item["cache_reused"] += reused
                status = str(row["status"] or "")
                if event_type == "source_reused":
                    item["succeeded"] += 1
                elif status in {"ok", "empty"}:
                    item["succeeded"] += 1
                else:
                    item["failed"] += 1
                if event_type == "source_finished" and int(row["process_started"] or 0):
                    elapsed = float(row["elapsed_seconds"] or 0.0)
                    item["calls"] += 1
                    item["total_seconds"] += elapsed
                    item["max_seconds"] = max(item["max_seconds"], elapsed)
                    api_calls += 1
                    observed_execution_keys.add((key[0], key[2]))
            if domain:
                item = domains[domain]
                item["completed"] += 1
                item["raw"] += raw_count if event_type == "source_finished" else 0
                item["cache_reused"] += reused
                if event_type == "source_finished" and int(row["process_started"] or 0):
                    item["api_calls"] += 1
                if str(row["status"] or "") not in {"ok", "empty", "reused_completed"}:
                    item["failed"] += 1

    # P3 databases and a tiny commit window in live P4 runs may contain a
    # completed provider result without telemetry. Backfill only executions
    # not already counted above.
    for record in provider_records:
        if not isinstance(record, Mapping) or record.get("process_started") is not True:
            continue
        execution_key = (
            str(record.get("batch_id") or ""),
            str(record.get("pair_key") or ""),
        )
        if execution_key in observed_execution_keys:
            continue
        observed_execution_keys.add(execution_key)
        provider = str(record.get("provider") or "")
        domain = str(record.get("domain") or "")
        operation = str(record.get("operation") or "")
        elapsed = float(record.get("elapsed_seconds") or 0.0)
        raw_count = len(record.get("hits") or [])
        status = str(record.get("status") or "")
        raw += raw_count
        api_calls += 1
        if provider:
            item = sources[provider]
            item["calls"] += 1
            item["raw"] += raw_count
            item["total_seconds"] += elapsed
            item["max_seconds"] = max(item["max_seconds"], elapsed)
            if status in {"ok", "empty"}:
                item["succeeded"] += 1
            else:
                item["failed"] += 1
        if domain:
            item = domains[domain]
            if operation:
                item["operations"].add(operation)
            item["completed"] += 1
            item["raw"] += raw_count
            item["api_calls"] += 1
            if status not in {"ok", "empty"}:
                item["failed"] += 1

    for provider, domain in active_sources.values():
        if provider:
            sources[provider]["running"] += 1
        if domain:
            domains[domain]["running"] += 1

    source_items = []
    for provider, item in sorted(sources.items()):
        calls = int(item["calls"])
        source_items.append({
            "provider": provider,
            **{key: item[key] for key in ("calls", "cache_reused", "raw", "running", "succeeded", "failed")},
            "total_seconds": round(float(item["total_seconds"]), 3),
            "average_seconds": round(float(item["total_seconds"]) / calls, 3) if calls else None,
            "max_seconds": round(float(item["max_seconds"]), 3) if calls else None,
        })
    domain_items = []
    for domain, item in sorted(domains.items()):
        planned = int(item["planned"])
        completed = int(item["completed"])
        statuses = item.pop("finished_statuses")
        status = (
            "active" if item["running"] or (planned and not statuses)
            else "failed" if statuses and all(value == "failed" for value in statuses)
            else "done" if statuses or (planned and completed >= planned)
            else "queued"
        )
        domain_items.append({
            "domain": domain,
            "operations": sorted(item["operations"]),
            "status": status,
            "planned": planned,
            "completed": completed,
            "running": int(item["running"]),
            "failed": int(item["failed"]),
            "progress_percent": 100 if status == "done" else round(min(completed, planned) * 100 / planned) if planned else 0,
            "raw": int(item["raw"]),
            "unique": unique_by_domain.get(domain, 0),
            "api_calls": int(item["api_calls"]),
            "cache_reused": int(item["cache_reused"]),
        })

    return {
        "version": f"{last_id}:{fetched_version}:{len(evidence)}",
        "status": "active" if any(item["status"] == "active" for item in domain_items) else "ready" if events or provider_records or unique else "idle",
        "domains": domain_items,
        "sources": source_items,
        "api_calls": api_calls,
        "cache_reused": cache_reused,
        "fetch": fetch_stats,
        "funnel": {
            "raw": raw,
            "unique": unique,
            "fetched": fetched,
            "evidence": len(evidence),
            "rates": {
                "deduplicated": _rate(unique, raw),
                "fetched": _rate(fetched, unique),
                "evidence": _rate(len(evidence), fetched),
            },
        },
    }
