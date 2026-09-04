#!/usr/bin/env python3
"""Dependency-light arXiv Atom API search used by the Search MCP."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from typing import Any

from search_utils import build_parser, get_client, make_item, make_result, print_json


API_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def _text(entry: ET.Element, name: str) -> str:
    return " ".join((entry.findtext(ATOM + name) or "").split())


def _identifier(value: str) -> str:
    clean = value.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", clean)


def search(query: str, limit: int) -> list[dict[str, Any]]:
    wanted = max(0, min(int(limit), 100))
    if not query.strip() or wanted == 0:
        return []
    with get_client(timeout=30, headers={"User-Agent": "deepresearch-cli/0.1"}) as client:
        response = client.get(
            API_URL,
            params={
                "search_query": f'all:"{query.strip()}"',
                "start": 0,
                "max_results": wanted,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        response.raise_for_status()
    root = ET.fromstring(response.content)
    items: list[dict[str, Any]] = []
    for entry in root.findall(ATOM + "entry")[:wanted]:
        identifier_url = _text(entry, "id")
        arxiv_id = _identifier(identifier_url)
        links = {
            str(link.attrib.get("title") or link.attrib.get("rel") or ""):
            str(link.attrib.get("href") or "")
            for link in entry.findall(ATOM + "link")
        }
        authors = [
            " ".join((author.findtext(ATOM + "name") or "").split())
            for author in entry.findall(ATOM + "author")
            if (author.findtext(ATOM + "name") or "").strip()
        ]
        categories = [
            str(category.attrib.get("term"))
            for category in entry.findall(ATOM + "category")
            if category.attrib.get("term")
        ]
        doi = entry.findtext(ARXIV + "doi")
        items.append(
            make_item(
                title=_text(entry, "title"),
                url=identifier_url or f"https://arxiv.org/abs/{arxiv_id}",
                snippet=_text(entry, "summary"),
                arxiv_id=arxiv_id,
                doi=doi.strip() if doi else None,
                authors=authors,
                publication_date=_text(entry, "published") or None,
                updated_at=_text(entry, "updated") or None,
                categories=categories,
                pdf_url=links.get("pdf") or f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            )
        )
    return items


def main() -> None:
    parser = build_parser("Search the official arXiv Atom API")
    args = parser.parse_args()
    try:
        print_json(make_result(True, args.query, "arxiv", search(args.query, args.limit)))
    except Exception as exc:
        print_json(make_result(False, args.query, "arxiv", [], str(exc)))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
