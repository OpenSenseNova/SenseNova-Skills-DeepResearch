from __future__ import annotations

import json
import re
from pathlib import Path

from .contracts import load_context, output_path


def diagnostic(message: str, *, rule: str = "M002", **fields: object) -> dict:
    return {"rule": rule, "severity": "error", "message": message, **fields}


context = load_context()
path = output_path(context, "perspective")
errors: list[dict] = []
try:
    text = path.read_text(encoding="utf-8")
except (OSError, UnicodeError) as exc:
    text = ""
    errors.append(diagnostic(str(exc), rule="FILE"))

dimension_id = context.get("scope", {}).get("dimension-id")
expected_h1 = f"# Perspective Summary: {dimension_id}"
lines = text.splitlines()
nonempty = [line.strip() for line in lines if line.strip()]
if not nonempty or nonempty[0] != expected_h1:
    errors.append(diagnostic("perspective must start with the exact dimension H1", expected=expected_h1))

required_h2 = ["## Lens Reviews", "## 维度内补研需求", "## 写回摘要"]
positions = []
for heading in required_h2:
    matches = [index for index, line in enumerate(lines) if line.strip() == heading]
    if len(matches) != 1:
        errors.append(diagnostic("required heading must appear exactly once", heading=heading, count=len(matches)))
    elif matches:
        positions.append(matches[0])
if positions != sorted(positions):
    errors.append(diagnostic("perspective level-2 headings are out of order"))
unexpected_h2 = [line.strip() for line in lines if line.startswith("## ") and line.strip() not in required_h2]
if unexpected_h2:
    errors.append(diagnostic("perspective contains undocumented level-2 headings", headings=unexpected_h2))

lenses: list[dict] = []
plan_inputs = context.get("inputs", {}).get("plan", [])
if plan_inputs:
    try:
        plan = json.loads(Path(plan_inputs[-1]["path"]).read_text(encoding="utf-8"))
        dimension = next(
            item for item in plan.get("dimensions", [])
            if isinstance(item, dict) and item.get("id") == dimension_id
        )
        lenses = dimension.get("lenses", []) if isinstance(dimension.get("lenses"), list) else []
    except (OSError, UnicodeError, ValueError, KeyError, StopIteration) as exc:
        errors.append(diagnostic(f"cannot load plan lens contract: {exc}", rule="FILE"))

if not lenses:
    if "当前维度没有已声明 lens" not in text:
        errors.append(diagnostic("lenses: [] must be stated explicitly"))
else:
    required_h4 = [
        "#### Lens 定位",
        "#### 写作补充边界（非正文主张）",
        "#### 需要补研后才能使用",
        "#### 探索性搜索线索",
    ]
    lens_headings = []
    lens_heading_pattern = re.compile(r"^###\s+(l[1-9]\d*)\b.*$")
    headings_by_id: dict[str, list[int]] = {}
    for line_index, line in enumerate(lines):
        match = lens_heading_pattern.match(line.strip())
        if match:
            headings_by_id.setdefault(match.group(1), []).append(line_index)

    for index, lens in enumerate(lenses, start=1):
        lens_id = f"l{index}"
        expected = f"### {lens_id}: {lens.get('axis')}:{lens.get('value')}"
        matches = headings_by_id.get(lens_id, [])
        if len(matches) != 1:
            errors.append(diagnostic(
                "lens id must appear in exactly one level-3 heading",
                lens_id=lens_id,
                example=expected,
                count=len(matches),
            ))
            continue
        lens_headings.append(matches[0])
        end = next((i for i in range(matches[0] + 1, len(lines)) if re.match(r"^#{2,3}\s", lines[i])), len(lines))
        section = lines[matches[0] + 1:end]
        section_positions = []
        for heading in required_h4:
            found = [i for i, line in enumerate(section) if line.strip() == heading]
            if len(found) != 1:
                errors.append(diagnostic("lens subsection must appear exactly once", lens=expected, heading=heading, count=len(found)))
            elif found:
                section_positions.append(found[0])
        if section_positions != sorted(section_positions):
            errors.append(diagnostic("lens subsections are out of order", lens=expected))
    if lens_headings != sorted(lens_headings):
        errors.append(diagnostic("lens sections are out of plan order"))

    expected_lens_ids = {f"l{index}" for index in range(1, len(lenses) + 1)}
    unexpected_lens_ids = sorted(set(headings_by_id) - expected_lens_ids)
    if unexpected_lens_ids:
        errors.append(diagnostic(
            "perspective contains lens ids not declared by the plan",
            lens_ids=unexpected_lens_ids,
        ))

print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
raise SystemExit(0 if not errors else 1)
