#!/usr/bin/env python3
"""Aggregate the supported keyless academic API providers."""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
from pathlib import Path
from typing import Any, Callable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from arxiv_api_search import search as search_arxiv
from openalex_search import search as search_openalex
from pubmed_search import search as search_pubmed
from search_utils import print_json
from semantic_scholar_search import search as search_semantic_scholar


DEFAULT_SOURCES = ("arxiv", "semantic", "openalex", "pubmed")
PROVIDERS: dict[str, tuple[str, Callable[[str, int], list[dict[str, Any]]]]] = {
    "arxiv": ("arxiv_official", search_arxiv),
    "semantic": ("semantic_scholar_official", search_semantic_scholar),
    "openalex": ("openalex_official", search_openalex),
    "pubmed": ("pubmed_official", search_pubmed),
}


def _normalize_sources(sources: Sequence[str] | str | None) -> list[str]:
    if sources is None:
        return list(DEFAULT_SOURCES)
    values = [sources] if isinstance(sources, str) else list(sources)
    selected: list[str] = []
    for value in values:
        for part in str(value).split(","):
            source = part.strip().lower()
            if not source:
                continue
            if source not in PROVIDERS:
                raise ValueError(
                    "unsupported academic source "
                    f"{source!r}; choose arxiv, semantic, openalex, or pubmed"
                )
            if source not in selected:
                selected.append(source)
    return selected or list(DEFAULT_SOURCES)


def _search_one(source: str, query: str, limit: int) -> dict[str, Any]:
    provider_name, provider = PROVIDERS[source]
    try:
        items = []
        for item in provider(query, limit) or []:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized["source"] = source
            normalized["provider"] = provider_name
            items.append(normalized)
        bounded = items[:limit]
        return {
            "source": source,
            "success": True,
            "provider": provider_name,
            "items": bounded,
            "attempts": [{
                "provider": provider_name,
                "success": True,
                "count": len(bounded),
                "error": None,
            }],
            "error": None,
        }
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        return {
            "source": source,
            "success": False,
            "provider": None,
            "items": [],
            "attempts": [{
                "provider": provider_name,
                "success": False,
                "count": 0,
                "error": error,
            }],
            "error": error,
        }


def search(
    query: str,
    sources: Sequence[str] | str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("query must not be empty")
    clean_sources = _normalize_sources(sources)
    clean_limit = max(0, min(int(limit), 100))

    results_by_source: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(clean_sources), thread_name_prefix="academic-source"
    ) as executor:
        futures = {
            executor.submit(_search_one, source, clean_query, clean_limit): source
            for source in clean_sources
        }
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            results_by_source[source] = future.result()

    source_results = [results_by_source[source] for source in clean_sources]
    items = [item for result in source_results for item in result["items"]]
    errors = [{
        "source": result["source"],
        "error": result["error"],
        "attempts": result["attempts"],
    } for result in source_results if result["error"]]
    success = any(result["success"] for result in source_results)
    return {
        "success": success,
        "query": clean_query,
        "provider": "academic",
        "sources": clean_sources,
        "items": items,
        "source_results": source_results,
        "errors": errors,
        "error": None if success else "all selected academic sources failed",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search the supported keyless academic APIs"
    )
    parser.add_argument("query")
    parser.add_argument("--source", action="append")
    parser.add_argument("--limit", "-n", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        result = search(
            args.query,
            sources=args.source,
            limit=args.limit,
        )
    except Exception as exc:
        result = {
            "success": False,
            "query": args.query,
            "provider": "academic",
            "sources": [],
            "items": [],
            "source_results": [],
            "errors": [],
            "error": str(exc),
        }
    output = dict(result)
    output.pop("items", None)
    print_json(output)
    if not result["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
