from __future__ import annotations

from deepresearch_cli.stitching import claim_source_ids, validate_unit_contract

from .contracts import load_context, output_path
from .report_markdown import emit, input_values, load_text, read_json, source_ids, error


context = load_context()
text, errors = load_text(output_path(context, "draft"))
outline_inputs = input_values(context, "outline")
task_inputs = input_values(context, "task")
try:
    if not outline_inputs or not task_inputs:
        raise ValueError("final repair requires outline and content evidence subset")
    outline = read_json(outline_inputs[-1]["path"])
    task = read_json(task_inputs[-1]["path"])
    unit_id = context.get("scope", {}).get("content-unit-id")
    if not isinstance(unit_id, str):
        raise ValueError("content-unit-id scope is missing")
    unit = next(item for item in outline["content_units"] if item.get("id") == unit_id)
    errors.extend(validate_unit_contract(
        unit,
        text,
        allowed_source_ids=source_ids(context, "task"),
        routed_claim_sources=claim_source_ids(task),
    ))
except (OSError, UnicodeError, ValueError, KeyError, StopIteration, TypeError) as exc:
    errors.append(error(f"cannot validate repaired content unit: {exc}", rule="FILE"))
emit(errors)
