from .contracts import load_context, output_path, structural_error
from .markdown_review import emit, validate_markdown
from .report_markdown import input_values, load_text, source_ids, validate_common


context = load_context()
verdict, errors = validate_markdown(
    output_path(context, "review"),
    min_level2_headings=2,
)
if verdict == "revise":
    errors.append(structural_error("final review verdict is revise; the report must be repaired before render"))
report_inputs = input_values(context, "report")
prepared_report = context.get("outputs", {}).get("stitched", {}).get("path")
if not report_inputs and not prepared_report:
    errors.append(structural_error("final review is missing the stitched report input"))
else:
    report_path = prepared_report or report_inputs[-1]["path"]
    report_text, report_errors = load_text(report_path)
    errors.extend(report_errors)
    errors.extend(validate_common(
        report_text,
        allowed_source_ids=source_ids(context, "evidence"),
    ))
emit(errors, verdict=verdict)
