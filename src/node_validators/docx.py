"""Validate that a converter produced a non-empty WordprocessingML document."""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


_REQUIRED_MEMBERS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
}


def main() -> int:
    try:
        context = json.loads(
            Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8")
        )
        document = Path(context["outputs"]["document"]["path"])
        if document.is_symlink() or not document.is_file():
            raise ValueError("report.docx is missing or unsafe")
        with zipfile.ZipFile(document) as archive:
            missing = _REQUIRED_MEMBERS - set(archive.namelist())
            if missing:
                raise ValueError(
                    "report.docx is missing required OOXML members: "
                    + ", ".join(sorted(missing))
                )
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        text = "".join(root.itertext()).strip()
        if not text:
            raise ValueError("report.docx contains no readable text")
        return 0
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

