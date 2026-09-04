"""Built-in script capability for deterministic Markdown citation rendering."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _one(context, port):
    values = context["inputs"].get(port, [])
    return Path(values[-1]["path"]) if values else None


def main() -> int:
    context_path = Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"])
    context = json.loads(context_path.read_text(encoding="utf-8"))
    draft = _one(context, "stitched") or _one(context, "draft")
    if draft is None:
        print("render requires a stitched report or report draft", file=sys.stderr)
        return 2
    evidence = [Path(item["path"]) for item in context["inputs"].get("evidence", [])]
    if not evidence:
        print("render requires evidence", file=sys.stderr)
        return 2
    report = Path(context["outputs"]["report"]["path"])
    helper = context_path.parent / "resources" / "prepare_citations.py"
    command = [
        sys.executable,
        "-I",
        str(helper),
        "--report",
        str(draft),
        "--evidence",
        *(str(item) for item in evidence),
        "--output",
        str(report),
    ]
    try:
        draft_text = draft.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read render input: {exc}", file=sys.stderr)
        return 2
    if "<!-- TOC will be inserted by render stage -->" in draft_text:
        command.append("--toc")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        print(completed.stderr or completed.stdout, file=sys.stderr)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
