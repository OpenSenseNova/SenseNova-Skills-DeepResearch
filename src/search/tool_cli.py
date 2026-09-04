"""Command-line bridge to run-scoped DeepResearch search services.

OpenClaw's ACP endpoint intentionally rejects per-session ``mcpServers``.
This small, bounded CLI exposes the same coordinator and deterministic fetch
operations through OpenClaw's native exec tool without changing global
OpenClaw configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .coordinator_client import SearchCoordinatorClient
from .fetch import WebFetchService


def _load_context(path: Path) -> Mapping[str, Any]:
    selected = path.expanduser()
    if selected.is_symlink():
        raise RuntimeError("search context must not be a symbolic link")
    resolved = selected.resolve()
    if not resolved.is_file():
        raise RuntimeError("search context is missing or is not a regular file")
    if os.name != "nt" and resolved.stat().st_mode & 0o077:
        raise RuntimeError("search context permissions must be owner-only")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("unsupported search context schema")
    lease = Path(str(value.get("lease_file") or "")).expanduser().resolve()
    if not lease.is_file():
        raise RuntimeError("search attempt lease is no longer active")
    for key in ("coordinator_url", "coordinator_token", "namespace"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RuntimeError(f"search context is missing {key}")
    return value


def _json_argument(value: str) -> Any:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("--searches must be a JSON array")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deepresearch-search")
    parser.add_argument("--context", required=True, type=Path)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("list-search-sources")
    commands.add_parser("list-search-domains")
    for name in ("start-domain-search", "batch-search"):
        command = commands.add_parser(name)
        command.add_argument("--searches", required=True, type=_json_argument)
    batch = commands.add_parser("get-search-batch")
    batch.add_argument("batch_id")
    results = commands.add_parser("search-results")
    results.add_argument("--cursor", type=int, default=0)
    results.add_argument("--limit", type=int, default=20)
    results.add_argument("--provider")
    results.add_argument("--batch-id")
    hit = commands.add_parser("get-search-hit")
    hit.add_argument("hit_id")
    fetch = commands.add_parser("fetch-url")
    fetch.add_argument("url")
    return parser


def _execute(args: argparse.Namespace, context: Mapping[str, Any]) -> dict[str, Any]:
    operation = args.operation
    if operation == "fetch-url":
        client = SearchCoordinatorClient(
            url=str(context["coordinator_url"]),
            token=str(context["coordinator_token"]),
            namespace=str(context["namespace"]),
            lease_file=Path(str(context["lease_file"])),
        )
        service = WebFetchService(
            camofox_enabled=bool(context.get("camofox_enabled")),
            camofox_base_url=str(
                context.get("camofox_base_url") or "http://127.0.0.1:9377"
            ),
            identity=str(context["namespace"]),
        )
        started = time.monotonic()
        try:
            result = service.fetch(args.url)
            try:
                client.record_fetch(
                    url=args.url,
                    final_url=result.get("final_url") or result.get("requested_url"),
                    status="ok" if result.get("ok") is True else "failed",
                    retrieval=str(result.get("retrieval") or "") or None,
                    elapsed_seconds=time.monotonic() - started,
                    reason=str(result.get("reason") or "") or None,
                )
            except Exception:
                pass
            return result
        except Exception as exc:
            try:
                client.record_fetch(
                    url=args.url,
                    status="failed",
                    elapsed_seconds=time.monotonic() - started,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            raise
        finally:
            service.close()
            client.close()
    client = SearchCoordinatorClient(
        url=str(context["coordinator_url"]),
        token=str(context["coordinator_token"]),
        namespace=str(context["namespace"]),
        lease_file=Path(str(context["lease_file"])),
    )
    try:
        if operation == "list-search-sources":
            return client.list_search_sources()
        if operation == "list-search-domains":
            return client.list_search_domains()
        if operation == "start-domain-search":
            return client.start_domain_search(args.searches)
        if operation == "batch-search":
            return client.batch_search(args.searches)
        if operation == "get-search-batch":
            return client.get_search_batch(args.batch_id)
        if operation == "search-results":
            return client.search_results(
                cursor=args.cursor,
                limit=args.limit,
                provider=args.provider,
                batch_id=args.batch_id,
            )
        if operation == "get-search-hit":
            return client.get_search_hit(args.hit_id)
        raise RuntimeError(f"unsupported search operation: {operation}")
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _execute(args, _load_context(args.context))
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
