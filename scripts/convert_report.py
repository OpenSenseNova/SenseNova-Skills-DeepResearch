"""Convert one Markdown report to both Word and PDF.

Usage:
    .venv/bin/python scripts/convert_report.py report/report_01.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

from deepresearch_cli.converters.md_docx import convert_markdown_to_docx
from deepresearch_cli.converters.md_pdf import convert_markdown_to_pdf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORD_TEMPLATE = PROJECT_ROOT / "assets/report-templates/word/reference.docx"
PDF_TEMPLATE = PROJECT_ROOT / "assets/report-templates/pdf/report.typ"


def main() -> int:
    parser = argparse.ArgumentParser(description="将 Markdown 同时转换为 Word 和 PDF")
    parser.add_argument("source", type=Path, help="Markdown 文件路径")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="输出目录；默认与 Markdown 文件位于同一目录",
    )
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error(f"找不到 Markdown 文件：{source}")

    output_dir = (
        args.output_dir.expanduser().resolve() if args.output_dir else source.parent
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    docx_path = output_dir / f"{source.stem}.docx"
    pdf_path = output_dir / f"{source.stem}.pdf"

    convert_markdown_to_docx(source, docx_path, WORD_TEMPLATE)
    convert_markdown_to_pdf(source, pdf_path, PDF_TEMPLATE)

    print(f"Word: {docx_path}")
    print(f"PDF:  {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
