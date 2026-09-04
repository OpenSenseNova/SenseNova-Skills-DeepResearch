#!/usr/bin/env python3
"""Keyless general-web search through the DDGS DuckDuckGo backend."""

from __future__ import annotations

import sys

from search_utils import build_parser, make_item, make_result, print_json


def search(
    query: str,
    limit: int,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    timelimit: str | None = None,
) -> list[dict]:
    """Search the public web without requiring an API key."""
    from ddgs import DDGS

    results = DDGS(timeout=15).text(
        query=query,
        region=region,
        safesearch=safesearch,
        timelimit=timelimit,
        max_results=max(1, min(limit, 50)),
        backend="duckduckgo",
    )

    items: list[dict] = []
    for result in results or []:
        url = str(result.get("href") or result.get("url") or "").strip()
        title = str(result.get("title") or url).strip()
        if not url:
            continue
        items.append(
            make_item(
                title=title,
                url=url,
                snippet=str(result.get("body") or result.get("snippet") or "").strip(),
            )
        )
        if len(items) >= limit:
            break
    return items


def main() -> None:
    parser = build_parser("Search the general web with DuckDuckGo")
    parser.add_argument("--region", default="wt-wt")
    parser.add_argument(
        "--safesearch",
        choices=("on", "moderate", "off"),
        default="moderate",
    )
    parser.add_argument("--timelimit", choices=("d", "w", "m", "y"))
    args = parser.parse_args()

    try:
        items = search(
            args.query,
            args.limit,
            region=args.region,
            safesearch=args.safesearch,
            timelimit=args.timelimit,
        )
        print_json(make_result(True, args.query, "duckduckgo", items))
    except Exception as exc:
        print_json(make_result(False, args.query, "duckduckgo", [], str(exc)))
        sys.exit(1)


if __name__ == "__main__":
    main()
