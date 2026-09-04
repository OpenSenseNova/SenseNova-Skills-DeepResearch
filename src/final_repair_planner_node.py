from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping


_VERDICT = re.compile(r"(?im)^VERDICT:\s*(pass|revise)\s*$")
_UNIT_ID = re.compile(r"(?<![A-Za-z0-9])u\d+(?![A-Za-z0-9])")
_REPAIR_TARGET = re.compile(r"(?im)^REPAIR_TARGET:\s*([^|\n]+)(?:\|.*)?$")


def _read_object(path: str) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON input must be an object: {path}")
    return value


def _reset(path: str) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"unsafe repair task directory entry: {child}")
        child.unlink()
    return directory


def main() -> int:
    try:
        context = _read_object(os.environ["DEEPRESEARCH_NODE_CONTEXT"])
        review_output = context.get("outputs", {}).get("review", {})
        review_path = review_output.get("path") or context["inputs"]["review"][-1]["path"]
        outline_path = context["inputs"]["outline"][-1]["path"]
        review = Path(review_path).read_text(encoding="utf-8")
        outline = _read_object(outline_path)
        verdicts = _VERDICT.findall(review)
        if len(verdicts) != 1:
            raise ValueError("FinalReview must contain exactly one VERDICT")

        repair_root = _reset(context["outputs"]["repair-tasks"]["directory"])
        recheck_root = _reset(context["outputs"]["recheck-tasks"]["directory"])
        decision_path = Path(context["outputs"]["decision"]["path"])
        if verdicts[0].casefold() == "pass":
            decision_path.write_text(
                json.dumps({"verdict": "pass", "repair_cycle": 0, "target_units": []}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps({"ok": True, "verdict": "pass", "repair_units": []}))
            return 0

        units = outline.get("content_units")
        if not isinstance(units, list) or not units:
            raise ValueError("outline.content_units must be a non-empty array")
        ordered_ids = [
            str(unit.get("id"))
            for unit in units
            if isinstance(unit, Mapping) and isinstance(unit.get("id"), str)
        ]
        target_specs = [item.strip() for item in _REPAIR_TARGET.findall(review)]
        global_target = any(item.casefold() == "global" for item in target_specs)
        mentioned = {
            unit_id
            for item in target_specs
            for unit_id in _UNIT_ID.findall(item)
        }
        targets = [unit_id for unit_id in ordered_ids if unit_id in mentioned]
        if global_target or not targets:
            targets = ordered_ids

        review_lines = [line.strip() for line in review.splitlines() if line.strip()]
        for unit_id in targets:
            targeted = [
                line for line in review_lines
                if line.startswith("REPAIR_TARGET:")
                and (unit_id in line or "global" in line.casefold())
            ]
            task = {
                "repair_cycle": 1,
                "unit_id": unit_id,
                "targeted_findings": targeted,
                "fallback_to_full_review": not bool(targeted),
            }
            (repair_root / f"{unit_id}.json").write_text(
                json.dumps(task, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        (recheck_root / "once.json").write_text(
            json.dumps(
                {"repair_cycle": 1, "target_units": targets},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        decision_path.write_text(
            json.dumps({"verdict": "revise", "repair_cycle": 1, "target_units": targets}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"ok": True, "verdict": "revise", "repair_units": targets}))
        return 0
    except (KeyError, OSError, UnicodeError, ValueError, TypeError) as exc:
        print(f"final repair planning failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
