"""Convert the canonical Markdown report to PDF with Pandoc and Typst."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from ._markdown import prepared_markdown_source


def _load_context() -> tuple[Mapping[str, Any], Path]:
    context_value = os.environ.get("DEEPRESEARCH_NODE_CONTEXT")
    if not context_value:
        raise ValueError("DEEPRESEARCH_NODE_CONTEXT is not set")
    context_path = Path(context_value)
    value = json.loads(context_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("node context must be a JSON object")
    return value, context_path


def _input_path(context: Mapping[str, Any], port: str) -> Path:
    values = context.get("inputs", {}).get(port, [])
    if not isinstance(values, list) or not values:
        raise ValueError(f"missing input port: {port}")
    return Path(values[-1]["path"])


def _output_path(context: Mapping[str, Any], port: str) -> Path:
    output = context.get("outputs", {}).get(port)
    if not isinstance(output, dict) or not output.get("path"):
        raise ValueError(f"missing output port: {port}")
    return Path(output["path"])


def _executable(environment_name: str, command: str, install_hint: str) -> str:
    configured = os.environ.get(environment_name)
    executable = configured or shutil.which(command)
    if not executable:
        raise RuntimeError(
            f"{command} is required for PDF export. {install_hint} or set "
            f"{environment_name} to the executable path."
        )
    return executable


def convert_markdown_to_pdf(
    source: Path,
    destination: Path,
    template: Path,
) -> None:
    pandoc = _executable(
        "DEEPRESEARCH_PANDOC", "pandoc", "Install it with `brew install pandoc`"
    )
    typst = _executable(
        "DEEPRESEARCH_TYPST", "typst", "Install it with `brew install typst`"
    )
    selected_template = Path(
        os.environ.get("DEEPRESEARCH_PDF_TEMPLATE", str(template))
    ).expanduser()
    if selected_template.is_symlink() or not selected_template.is_file():
        raise RuntimeError(
            "PDF template does not point to a regular file: "
            f"{selected_template}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with prepared_markdown_source(source) as prepared_source:
        command = [
            pandoc,
            str(prepared_source),
            "--from=gfm",
            "--to=typst",
            "--standalone",
            "--shift-heading-level-by=-1",
            "--toc",
            "--toc-depth=2",
            "--metadata=subtitle:深度研究报告",
            f"--metadata=date:{date.today().year}年{date.today().month}月{date.today().day}日",
            "--metadata=lang:zh-CN",
            f"--pdf-engine={typst}",
            "--template",
            str(selected_template.resolve()),
            f"--resource-path={source.parent}",
            "--output",
            str(destination),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown conversion error").strip()
        raise RuntimeError(f"Pandoc/Typst PDF conversion failed: {detail[-4000:]}")


def main() -> int:
    try:
        context, context_path = _load_context()
        source = _input_path(context, "report")
        destination = _output_path(context, "document")
        template = context_path.parent / "resources" / "report.typ"
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Markdown report not found or unsafe: {source}")
        convert_markdown_to_pdf(source, destination, template)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
