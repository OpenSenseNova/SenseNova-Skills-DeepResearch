from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


_CITATION = re.compile(r"\[\^([^\]\s]+)\]")
_CLAIM_OR_CONTEXT_ID = re.compile(r"^d\d+\.[cw]\d+$")
_FOOTNOTE_DEFINITION = re.compile(r"(?m)^\[\^[^\]]+\]:")
_REFERENCE_HEADING = re.compile(r"(?im)^##\s+(参考文献|references)\s*$")


def error(message: str, *, rule: str = "REPORT", **fields: object) -> dict:
    return {"rule": rule, "severity": "error", "message": message, **fields}


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def input_values(context: Mapping[str, Any], port: str) -> list[Mapping[str, Any]]:
    values = context.get("inputs", {}).get(port, [])
    return values if isinstance(values, list) else []


def source_ids(context: Mapping[str, Any], *ports: str) -> set[str]:
    result: set[str] = set()
    for port in ports:
        for item in input_values(context, port):
            try:
                value = read_json(item["path"])
            except (OSError, UnicodeError, ValueError, KeyError):
                continue
            for source in value.get("sources", []) if isinstance(value, dict) else []:
                if isinstance(source, dict) and isinstance(source.get("id"), str):
                    result.add(source["id"])
    return result


def validate_common(text: str, *, allowed_source_ids: set[str] | None) -> list[dict]:
    errors: list[dict] = []
    if not text.strip():
        errors.append(error("report Markdown is empty"))
        return errors
    if _FOOTNOTE_DEFINITION.search(text):
        errors.append(error("report Markdown must not define footnotes; render owns definitions"))
    if _REFERENCE_HEADING.search(text):
        errors.append(error("report Markdown must not contain a references section; render owns it"))
    keys = _CITATION.findall(text)
    leaked = sorted({key for key in keys if _CLAIM_OR_CONTEXT_ID.fullmatch(key)})
    if leaked:
        errors.append(error("internal claim or writing-context ids cannot be citations", keys=leaked))
    if allowed_source_ids is not None:
        orphaned = sorted({key for key in keys if key not in allowed_source_ids})
        if orphaned:
            errors.append(error("citations must reference sources in the routed evidence", keys=orphaned))
    return errors


def load_text(path: str | Path) -> tuple[str, list[dict]]:
    try:
        return Path(path).read_text(encoding="utf-8"), []
    except (OSError, UnicodeError) as exc:
        return "", [error(str(exc), rule="FILE")]


def emit(errors: list[dict]) -> None:
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)
