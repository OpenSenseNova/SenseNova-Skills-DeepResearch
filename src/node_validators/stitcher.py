from __future__ import annotations

from .contracts import load_context, output_path
from .report_markdown import emit, error, input_values, load_text, read_json, validate_common


context = load_context()
text, errors = load_text(output_path(context, "stitched"))
errors.extend(validate_common(text, allowed_source_ids=None))
lines = text.splitlines()
h1_lines = [line for line in lines if line.startswith("# ")]
nonempty = [line for line in lines if line.strip()]
if len(h1_lines) != 1 or not nonempty or nonempty[0] != h1_lines[0]:
    errors.append(error("stitched report must start with exactly one H1"))

outline_inputs = input_values(context, "outline")
if outline_inputs:
    try:
        outline = read_json(outline_inputs[-1]["path"])
        expected = [
            f"## {unit['title']}"
            for unit in outline["content_units"]
            if unit.get("render_contract", {}).get("show_heading") is True
        ]
        positions = []
        for heading in expected:
            matches = [index for index, line in enumerate(lines) if line.strip() == heading]
            if len(matches) != 1:
                errors.append(error("contracted unit heading must appear exactly once", heading=heading, count=len(matches)))
            elif matches:
                positions.append(matches[0])
        if positions != sorted(positions):
            errors.append(error("content-unit headings are out of outline order"))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        errors.append(error(f"cannot load outline contract: {exc}", rule="FILE"))

emit(errors)
