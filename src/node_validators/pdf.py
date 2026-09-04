"""Perform dependency-free structural checks on a generated PDF report."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def main() -> int:
    try:
        context = json.loads(
            Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8")
        )
        document = Path(context["outputs"]["document"]["path"])
        if document.is_symlink() or not document.is_file():
            raise ValueError("report.pdf is missing or unsafe")
        data = document.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise ValueError("report.pdf has no PDF header")
        if b"%%EOF" not in data[-2048:]:
            raise ValueError("report.pdf has no final EOF marker")
        if not re.search(rb"/Type\s*/Page(?:\s|/|>>)", data):
            raise ValueError("report.pdf contains no page objects")
        return 0
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

