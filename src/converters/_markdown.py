"""Shared Markdown preprocessing for document converters."""

from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_TOC_TITLES = {"目录", "table of contents", "toc"}
_CITATIONS_AFTER_PUNCTUATION = re.compile(
    r"([。！？；，,!?;])([ \t]*(?:\[\d+\])+)",
)


def move_citations_before_punctuation(markdown: str) -> str:
    """Put numeric source markers before sentence punctuation (e.g. ``[1]。``)."""
    lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        marker = _FENCE.match(line.rstrip("\r\n"))
        if marker:
            kind, length = marker.group(1)[0], len(marker.group(1))
            if fence is None:
                fence = (kind, length)
            elif kind == fence[0] and length >= fence[1]:
                fence = None
            lines.append(line)
            continue
        lines.append(
            line
            if fence is not None
            else _CITATIONS_AFTER_PUNCTUATION.sub(r"\2\1", line)
        )
    return "".join(lines)


def strip_static_toc(markdown: str) -> str:
    """Remove authored TOC sections; native PDF/DOCX TOCs are generated later."""
    lines = markdown.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    fence: tuple[str, int] | None = None
    index = 0

    while index < len(lines):
        plain = lines[index].rstrip("\r\n")
        fence_match = _FENCE.match(plain)
        if fence_match:
            marker = fence_match.group(1)
            marker_kind = marker[0]
            if fence is None:
                fence = (marker_kind, len(marker))
            elif marker_kind == fence[0] and len(marker) >= fence[1]:
                fence = None
            index += 1
            continue

        heading = _HEADING.match(plain) if fence is None else None
        if (
            heading is None
            or len(heading.group(1)) == 1
            or heading.group(2).strip().casefold() not in _TOC_TITLES
        ):
            index += 1
            continue

        level = len(heading.group(1))
        end = index + 1
        nested_fence: tuple[str, int] | None = None
        while end < len(lines):
            candidate = lines[end].rstrip("\r\n")
            candidate_fence = _FENCE.match(candidate)
            if candidate_fence:
                marker = candidate_fence.group(1)
                marker_kind = marker[0]
                if nested_fence is None:
                    nested_fence = (marker_kind, len(marker))
                elif marker_kind == nested_fence[0] and len(marker) >= nested_fence[1]:
                    nested_fence = None
                end += 1
                continue
            next_heading = _HEADING.match(candidate) if nested_fence is None else None
            if next_heading is not None and len(next_heading.group(1)) <= level:
                break
            end += 1
        ranges.append((index, end))
        index = end

    if not ranges:
        return markdown

    kept: list[str] = []
    cursor = 0
    for start, end in ranges:
        kept.extend(lines[cursor:start])
        cursor = end
    kept.extend(lines[cursor:])

    return "".join(kept)


@contextmanager
def prepared_markdown_source(source: Path) -> Iterator[Path]:
    """Yield a temporary TOC-free source only when preprocessing is needed."""
    original = source.read_text(encoding="utf-8")
    prepared = move_citations_before_punctuation(strip_static_toc(original))
    if prepared == original:
        yield source
        return

    with tempfile.TemporaryDirectory(prefix="deepresearch-document-") as directory:
        temporary_source = Path(directory) / source.name
        temporary_source.write_text(prepared, encoding="utf-8")
        yield temporary_source
