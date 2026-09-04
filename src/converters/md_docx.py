"""Convert the canonical Markdown report to an editable DOCX with Pandoc."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from ._markdown import prepared_markdown_source


_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"
ET.register_namespace("w", _WORD_NS)


def _load_context() -> tuple[Mapping[str, Any], Path]:
    context_path = os.environ.get("DEEPRESEARCH_NODE_CONTEXT")
    if not context_path:
        raise ValueError("DEEPRESEARCH_NODE_CONTEXT is not set")
    path = Path(context_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("node context must be a JSON object")
    return value, path


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


def _set_page_break_before(paragraph: ET.Element) -> None:
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        properties = ET.Element(f"{_W}pPr")
        paragraph.insert(0, properties)
    if properties.find(f"{_W}pageBreakBefore") is None:
        ET.SubElement(properties, f"{_W}pageBreakBefore")


def _set_body_paragraph_spacing(paragraph: ET.Element) -> None:
    """Make body paragraphs flow continuously while keeping readable line spacing."""
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        properties = ET.Element(f"{_W}pPr")
        paragraph.insert(0, properties)
    spacing = properties.find(f"{_W}spacing")
    if spacing is None:
        spacing = ET.SubElement(properties, f"{_W}spacing")
    # Keep a modest paragraph gap while using tighter in-paragraph line spacing.
    spacing.set(f"{_W}before", "0")
    spacing.set(f"{_W}after", "120")
    spacing.set(f"{_W}line", "285")
    spacing.set(f"{_W}lineRule", "auto")


def _shade_cell(cell: ET.Element, fill: str) -> None:
    properties = cell.find(f"{_W}tcPr")
    if properties is None:
        properties = ET.Element(f"{_W}tcPr")
        cell.insert(0, properties)
    shading = properties.find(f"{_W}shd")
    if shading is None:
        shading = ET.SubElement(properties, f"{_W}shd")
    shading.set(f"{_W}val", "clear")
    shading.set(f"{_W}fill", fill)


def _style_table_text(cell: ET.Element, *, header: bool) -> None:
    for run in cell.iter(f"{_W}r"):
        properties = run.find(f"{_W}rPr")
        if properties is None:
            properties = ET.Element(f"{_W}rPr")
            run.insert(0, properties)
        size = properties.find(f"{_W}sz")
        if size is None:
            size = ET.SubElement(properties, f"{_W}sz")
        size.set(f"{_W}val", "16")
        size_cs = properties.find(f"{_W}szCs")
        if size_cs is None:
            size_cs = ET.SubElement(properties, f"{_W}szCs")
        size_cs.set(f"{_W}val", "16")
        if header:
            if properties.find(f"{_W}b") is None:
                ET.SubElement(properties, f"{_W}b")
            color = properties.find(f"{_W}color")
            if color is None:
                color = ET.SubElement(properties, f"{_W}color")
            color.set(f"{_W}val", "FFFFFF")


def _page_break_paragraph() -> ET.Element:
    paragraph = ET.Element(f"{_W}p")
    run = ET.SubElement(paragraph, f"{_W}r")
    ET.SubElement(run, f"{_W}br", {f"{_W}type": "page"})
    return paragraph


def _cover_paragraph(
    text: str,
    *,
    before: str,
    after: str,
    size: str,
    color: str,
    bold: bool = False,
    top_border: bool = False,
) -> ET.Element:
    paragraph = ET.Element(f"{_W}p")
    properties = ET.SubElement(paragraph, f"{_W}pPr")
    ET.SubElement(
        properties,
        f"{_W}spacing",
        {f"{_W}before": before, f"{_W}after": after},
    )
    if top_border:
        borders = ET.SubElement(properties, f"{_W}pBdr")
        ET.SubElement(
            borders,
            f"{_W}top",
            {
                f"{_W}val": "single",
                f"{_W}sz": "8",
                f"{_W}space": "8",
                f"{_W}color": "D8E0E8",
            },
        )
    run = ET.SubElement(paragraph, f"{_W}r")
    run_properties = ET.SubElement(run, f"{_W}rPr")
    ET.SubElement(run_properties, f"{_W}color", {f"{_W}val": color})
    ET.SubElement(run_properties, f"{_W}sz", {f"{_W}val": size})
    ET.SubElement(run_properties, f"{_W}szCs", {f"{_W}val": size})
    if bold:
        ET.SubElement(run_properties, f"{_W}b")
    ET.SubElement(run, f"{_W}t").text = text
    return paragraph


def _cover_title_table(paragraph: ET.Element) -> ET.Element:
    """Place the Pandoc title in a restrained consulting-style cover panel."""
    paragraph_properties = paragraph.find(f"{_W}pPr")
    if paragraph_properties is None:
        paragraph_properties = ET.Element(f"{_W}pPr")
        paragraph.insert(0, paragraph_properties)
    alignment = paragraph_properties.find(f"{_W}jc")
    if alignment is None:
        alignment = ET.SubElement(paragraph_properties, f"{_W}jc")
    alignment.set(f"{_W}val", "left")
    # Add balanced vertical padding so the title sits in the visual middle of
    # the cover panel instead of hugging its top edge.
    spacing = paragraph_properties.find(f"{_W}spacing")
    if spacing is None:
        spacing = ET.SubElement(paragraph_properties, f"{_W}spacing")
    spacing.set(f"{_W}before", "420")
    spacing.set(f"{_W}after", "420")
    for run in paragraph.findall(f"{_W}r"):
        properties = run.find(f"{_W}rPr")
        if properties is None:
            properties = ET.Element(f"{_W}rPr")
            run.insert(0, properties)
        color = properties.find(f"{_W}color")
        if color is None:
            color = ET.SubElement(properties, f"{_W}color")
        color.set(f"{_W}val", "FFFFFF")
        for tag in ("sz", "szCs"):
            size = properties.find(f"{_W}{tag}")
            if size is None:
                size = ET.SubElement(properties, f"{_W}{tag}")
            size.set(f"{_W}val", "48")
        if properties.find(f"{_W}b") is None:
            ET.SubElement(properties, f"{_W}b")

    table = ET.Element(f"{_W}tbl")
    table_properties = ET.SubElement(table, f"{_W}tblPr")
    ET.SubElement(
        table_properties, f"{_W}tblCaption", {f"{_W}val": "DeepResearchCover"}
    )
    ET.SubElement(
        table_properties, f"{_W}tblW", {f"{_W}w": "0", f"{_W}type": "auto"}
    )
    borders = ET.SubElement(table_properties, f"{_W}tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        ET.SubElement(borders, f"{_W}{side}", {f"{_W}val": "nil"})
    row = ET.SubElement(table, f"{_W}tr")
    cell = ET.SubElement(row, f"{_W}tc")
    cell_properties = ET.SubElement(cell, f"{_W}tcPr")
    ET.SubElement(
        cell_properties, f"{_W}tcW", {f"{_W}w": "0", f"{_W}type": "auto"}
    )
    ET.SubElement(
        cell_properties,
        f"{_W}shd",
        {f"{_W}val": "clear", f"{_W}fill": "17324D"},
    )
    margins = ET.SubElement(cell_properties, f"{_W}tcMar")
    for side, width in (("top", "420"), ("left", "420"), ("bottom", "420"), ("right", "420")):
        ET.SubElement(
            margins,
            f"{_W}{side}",
            {f"{_W}w": width, f"{_W}type": "dxa"},
        )
    cell.append(paragraph)
    return table


def _polish_docx(path: Path) -> None:
    """Apply layout details that reference.docx alone cannot guarantee."""
    with zipfile.ZipFile(path, "r") as archive:
        document_xml = archive.read("word/document.xml")
        entries = [(item, archive.read(item.filename)) for item in archive.infolist()]

    root = ET.fromstring(document_xml)
    body = root.find(f"{_W}body")
    if body is None:
        raise RuntimeError("Generated DOCX has no document body")

    children = list(body)
    title_index = next(
        (
            index
            for index, child in enumerate(children)
            if child.tag == f"{_W}p"
            and child.find(f"{_W}pPr/{_W}pStyle") is not None
            and child.find(f"{_W}pPr/{_W}pStyle").get(f"{_W}val") == "Title"
        ),
        None,
    )
    if title_index is not None:
        title_paragraph = children[title_index]
        body.remove(title_paragraph)
        body.insert(title_index, _cover_title_table(title_paragraph))
        body.insert(
            title_index,
            _cover_paragraph(
                "DEEP RESEARCH  /  INSIGHT REPORT",
                before="780",
                after="2300",
                size="19",
                color="0E7490",
                bold=True,
            ),
        )
        # Word collapses spacing on a paragraph immediately followed by a
        # table. Use a dedicated spacer paragraph so the title panel visibly
        # sits lower on the cover, matching the PDF composition.
        body.insert(
            title_index + 1,
            _cover_paragraph(
                "",
                before="0",
                after="900",
                size="2",
                color="FFFFFF",
            ),
        )
        children = list(body)
        date_index = next(
            (
                index
                for index, child in enumerate(children)
                if child.tag == f"{_W}p"
                and child.find(f"{_W}pPr/{_W}pStyle") is not None
                and child.find(f"{_W}pPr/{_W}pStyle").get(f"{_W}val") == "Date"
            ),
            None,
        )
        if date_index is not None:
            body.insert(
                date_index + 1,
                _cover_paragraph(
                    "独立研究  ·  证据可追溯  ·  结论有边界",
                    before="1800",
                    after="0",
                    size="18",
                    color="667085",
                    top_border=True,
                ),
            )

        # Enlarge and lower the original cover metadata beneath the title panel.
        for paragraph in body.findall(f"{_W}p"):
            style = paragraph.find(f"{_W}pPr/{_W}pStyle")
            style_name = style.get(f"{_W}val") if style is not None else ""
            if style_name not in {"Subtitle", "Date"}:
                continue
            properties = paragraph.find(f"{_W}pPr")
            if properties is None:
                properties = ET.Element(f"{_W}pPr")
                paragraph.insert(0, properties)
            spacing = properties.find(f"{_W}spacing")
            if spacing is None:
                spacing = ET.SubElement(properties, f"{_W}spacing")
            spacing.set(f"{_W}before", "280")
            for run in paragraph.findall(f"{_W}r"):
                run_properties = run.find(f"{_W}rPr")
                if run_properties is None:
                    run_properties = ET.Element(f"{_W}rPr")
                    run.insert(0, run_properties)
                for tag in ("sz", "szCs"):
                    size = run_properties.find(f"{_W}{tag}")
                    if size is None:
                        size = ET.SubElement(run_properties, f"{_W}{tag}")
                    size.set(f"{_W}val", "24" if style_name == "Subtitle" else "20")

    for index, child in enumerate(list(body)):
        gallery = child.find(f".//{_W}docPartGallery")
        if gallery is not None and gallery.get(f"{_W}val") == "Table of Contents":
            for text in child.iter(f"{_W}t"):
                if text.text == "Table of Contents":
                    text.text = "目录"
            body.insert(index, _page_break_paragraph())
            toc_position = list(body).index(child)
            body.insert(toc_position + 1, _page_break_paragraph())
            break

    # Pandoc inherits sizeable after-spacing from some reference styles. Normalize
    # ordinary body paragraphs so adjacent paragraphs touch naturally; headings and
    # the generated TOC retain their own hierarchy spacing.
    body_styles = {"Title", "Date", "Heading1", "Heading2", "Heading3", "Heading4", "Heading5", "Heading6"}
    for paragraph in body.findall(f"{_W}p"):
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        style_name = style.get(f"{_W}val") if style is not None else ""
        if style_name == "Heading1":
            properties = paragraph.find(f"{_W}pPr")
            if properties is None:
                properties = ET.Element(f"{_W}pPr")
                paragraph.insert(0, properties)
            for tag in ("pageBreakBefore", "keepNext", "keepLines"):
                node = properties.find(f"{_W}{tag}")
                if node is not None:
                    properties.remove(node)
            ET.SubElement(properties, f"{_W}keepNext")
            continue
        if style_name not in body_styles and not paragraph.find(f".//{_W}docPartGallery") is not None:
            _set_body_paragraph_spacing(paragraph)

    # Restore the intentional cover spacer after generic body normalization.
    body_children = list(body)
    for index, child in enumerate(body_children[:-1]):
        if child.tag == f"{_W}p" and not "".join(child.itertext()).strip():
            following = body_children[index + 1]
            if following.tag == f"{_W}tbl" and following.find(f"{_W}tblPr/{_W}tblCaption") is not None:
                spacing = child.find(f"{_W}pPr/{_W}spacing")
                if spacing is not None:
                    spacing.set(f"{_W}after", "0")
                    # Fixed-height spacer: Word otherwise collapses an empty
                    # paragraph before a table and pulls the title panel up.
                    spacing.set(f"{_W}line", "1800")
                    spacing.set(f"{_W}lineRule", "exact")
                break

    for table in body.iter(f"{_W}tbl"):
        caption = table.find(f"{_W}tblPr/{_W}tblCaption")
        if caption is not None and caption.get(f"{_W}val") == "DeepResearchCover":
            continue
        rows = table.findall(f"{_W}tr")
        for row_index, row in enumerate(rows):
            for cell in row.findall(f"{_W}tc"):
                if row_index == 0:
                    _shade_cell(cell, "17324D")
                elif row_index % 2 == 0:
                    _shade_cell(cell, "F3F7FB")
                _style_table_text(cell, header=row_index == 0)

    updated_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    styles_xml: bytes | None = None
    for item, content in entries:
        if item.filename == "word/styles.xml":
            styles = ET.fromstring(content)
            # The reference template marks Heading 1 as page-break-before,
            # which creates large white areas when a section is short. Keep
            # headings flowing with the preceding text instead.
            for style in styles.findall(f"{_W}style"):
                if style.get(f"{_W}styleId") == "Heading1":
                    properties = style.find(f"{_W}pPr")
                    if properties is not None:
                        for tag in ("pageBreakBefore", "keepNext", "keepLines"):
                            node = properties.find(f"{_W}{tag}")
                            if node is not None:
                                properties.remove(node)
                        ET.SubElement(properties, f"{_W}keepNext")
                    break
            styles_xml = ET.tostring(styles, encoding="utf-8", xml_declaration=True)
    settings_xml: bytes | None = None
    for item, content in entries:
        if item.filename == "word/settings.xml":
            settings = ET.fromstring(content)
            update = settings.find(f"{_W}updateFields")
            if update is None:
                update = ET.SubElement(settings, f"{_W}updateFields")
            update.set(f"{_W}val", "true")
            settings_xml = ET.tostring(
                settings, encoding="utf-8", xml_declaration=True
            )
            break
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}-", suffix=".docx", dir=path.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w") as output:
            for item, content in entries:
                output.writestr(
                    item,
                    updated_xml
                    if item.filename == "word/document.xml"
                    else styles_xml
                    if item.filename == "word/styles.xml" and styles_xml is not None
                    else settings_xml
                    if item.filename == "word/settings.xml" and settings_xml is not None
                    else content,
                )
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def convert_markdown_to_docx(
    source: Path,
    destination: Path,
    default_reference: Path,
) -> None:
    configured = os.environ.get("DEEPRESEARCH_PANDOC")
    pandoc = configured or shutil.which("pandoc")
    if not pandoc:
        raise RuntimeError(
            "Pandoc is required for DOCX export. Install it with `brew install pandoc` "
            "or set DEEPRESEARCH_PANDOC to the executable path."
        )

    reference_path = Path(
        os.environ.get("DEEPRESEARCH_DOCX_REFERENCE", str(default_reference))
    ).expanduser()
    if reference_path.is_symlink() or not reference_path.is_file():
        raise RuntimeError(
            "Word reference template does not point to a regular file: "
            f"{reference_path}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with prepared_markdown_source(source) as prepared_source:
        command = [
            pandoc,
            str(prepared_source),
            "--from=gfm",
            "--to=docx",
            "--standalone",
            "--shift-heading-level-by=-1",
            "--toc",
            "--toc-depth=1",
            "--metadata=subtitle:深度研究报告",
            f"--metadata=date:{date.today().year}年{date.today().month}月{date.today().day}日",
            "--metadata=lang:zh-CN",
            f"--resource-path={source.parent}",
            "--output",
            str(destination),
            "--reference-doc",
            str(reference_path.resolve()),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown Pandoc error").strip()
        raise RuntimeError(f"Pandoc DOCX conversion failed: {detail[-4000:]}")
    _polish_docx(destination)


def main() -> int:
    try:
        context, context_path = _load_context()
        source = _input_path(context, "report")
        destination = _output_path(context, "document")
        reference = context_path.parent / "resources" / "reference.docx"
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Markdown report not found or unsafe: {source}")
        convert_markdown_to_docx(source, destination, reference)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
