#!/usr/bin/env python3
"""Derive batch research tasks from the validated semantic state candidate."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


DIMENSION_ID_RE = re.compile(r"^d[1-9]\d*$")


def load_json(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def reset_json_directory(path: str) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"derived output directory contains an unsafe entry: {child}")
        child.unlink()
    return directory


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    context = json.loads(
        Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8")
    )
    outputs = context["outputs"]
    task_root = reset_json_directory(outputs["research-tasks"]["directory"])

    if "plan" in outputs:
        plan = load_json(outputs["plan"]["path"])
        dimensions = plan.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions:
            raise ValueError("plan.dimensions must be a non-empty array before deriving tasks")
        seen = set()
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                raise ValueError("each plan dimension must be an object")
            dimension_id = dimension.get("id")
            if not isinstance(dimension_id, str) or not DIMENSION_ID_RE.fullmatch(dimension_id):
                raise ValueError(f"invalid plan dimension id: {dimension_id!r}")
            if dimension_id in seen:
                raise ValueError(f"duplicate plan dimension id: {dimension_id}")
            seen.add(dimension_id)
            write_json(task_root / f"{dimension_id}.json", dimension)
    elif "supplement-plan" in outputs:
        plan = load_json(outputs["supplement-plan"]["path"])
        dimension_id = plan.get("dimension_id")
        items = plan.get("supplement_items")
        if not isinstance(dimension_id, str) or not DIMENSION_ID_RE.fullmatch(dimension_id):
            raise ValueError(f"invalid supplement dimension id: {dimension_id!r}")
        if not isinstance(items, list):
            raise ValueError("supplement_items must be an array before deriving tasks")
        if items:
            write_json(
                task_root / f"{dimension_id}.json",
                {
                    "dimension_id": dimension_id,
                    "supplement_items": items,
                },
            )
    else:
        raise ValueError("research task materializer requires plan or supplement-plan output")

    print(json.dumps({"ok": True, "tasks": len(list(task_root.glob("*.json")))}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"research task materialization failed: {exc}", file=sys.stderr)
        sys.exit(1)
