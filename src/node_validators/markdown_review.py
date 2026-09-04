from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable


_VERDICT = re.compile(r"(?im)^VERDICT:\s*(pass|revise)\s*$")


def validate_markdown(
    path: Path,
    *,
    required_headings: Iterable[str] = (),
    min_level2_headings: int = 0,
) -> tuple[str | None, list[dict]]:
    errors = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, [{"rule": "FILE", "severity": "error", "message": str(exc)}]
    if not text.strip():
        errors.append({"rule": "M001", "severity": "error", "message": "Markdown output is empty"})
    positions = []
    for heading in required_headings:
        matches = list(re.finditer(rf"(?m)^{re.escape(heading)}\s*$", text))
        if len(matches) != 1:
            errors.append({
                "rule": "M002", "severity": "error",
                "message": f"required heading must appear exactly once: {heading}",
                "count": len(matches),
            })
        elif matches:
            positions.append(matches[0].start())
    if positions != sorted(positions):
        errors.append({"rule": "M002", "severity": "error", "message": "required headings are out of order"})
    level2_count = len(re.findall(r"(?m)^##\s+\S", text))
    if level2_count < min_level2_headings:
        errors.append({
            "rule": "M002",
            "severity": "error",
            "message": f"expected at least {min_level2_headings} level-2 headings, got {level2_count}",
        })
    verdicts = list(_VERDICT.finditer(text))
    if len(verdicts) > 1:
        errors.append({"rule": "M003", "severity": "error", "message": "VERDICT must appear exactly once"})
    return (verdicts[0].group(1).lower() if len(verdicts) == 1 else None), errors


def emit(errors: list[dict], *, verdict: str | None = None) -> None:
    if verdict is None:
        errors.append({"rule": "M003", "severity": "error", "message": "missing VERDICT: pass / revise"})
    print(json.dumps({"ok": not errors, "errors": errors, "stats": {"verdict": verdict}}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)
