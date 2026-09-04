from __future__ import annotations

from .contracts import load_context, output_path
from deepresearch_cli.stitching import claim_source_ids, validate_unit_contract

from .report_markdown import emit, input_values, load_text, read_json, source_ids, validate_common, error


context = load_context()
text, errors = load_text(output_path(context, "draft"))
task_inputs = input_values(context, "task")
outline_inputs = input_values(context, "outline")
allowed_sources = source_ids(context, "task") if task_inputs else source_ids(context, "evidence")

if task_inputs and outline_inputs:
    try:
        outline = read_json(outline_inputs[-1]["path"])
        task = read_json(task_inputs[-1]["path"])
        unit_id = context.get("scope", {}).get("content-unit-id")
        if not isinstance(unit_id, str):
            raise ValueError("content-unit-id scope is missing")
        unit = next(item for item in outline["content_units"] if item.get("id") == unit_id)
        errors.extend(
            validate_unit_contract(
                unit,
                text,
                allowed_source_ids=allowed_sources,
                routed_claim_sources=claim_source_ids(task),
            )
        )
    except (OSError, UnicodeError, ValueError, KeyError, StopIteration, TypeError) as exc:
        errors.append(error(f"cannot load content-unit render contract: {exc}", rule="FILE"))
else:
    errors.extend(validate_common(text, allowed_source_ids=allowed_sources))
    h1_lines = [line for line in text.splitlines() if line.startswith("# ")]
    nonempty = [line for line in text.splitlines() if line.strip()]
    if len(h1_lines) != 1 or not nonempty or nonempty[0] != h1_lines[0]:
        errors.append(error("quick/normal report must start with exactly one H1"))

emit(errors)
