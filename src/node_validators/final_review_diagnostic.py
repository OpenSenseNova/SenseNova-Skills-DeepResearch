import re

from .contracts import load_context, output_path, structural_error
from .markdown_review import emit, validate_markdown
from .report_markdown import input_values, load_text, source_ids, validate_common


context = load_context()
verdict, errors = validate_markdown(
    output_path(context, "review"),
    min_level2_headings=2,
)
if verdict == "revise":
    try:
        review_text = output_path(context, "review").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        review_text = ""
    targets = re.findall(r"(?im)^REPAIR_TARGET:\s*([^|\n]+)(?:\|.*)?$", review_text)
    if not targets:
        errors.append(structural_error(
            "VERDICT: revise requires at least one REPAIR_TARGET: uN | issue or REPAIR_TARGET: global | issue line"
        ))
    elif any(
        value.strip().casefold() != "global"
        and not re.fullmatch(r"u\d+(?:\s*,\s*u\d+)*", value.strip())
        for value in targets
    ):
        errors.append(structural_error("REPAIR_TARGET must be global or a comma-separated list of unit ids"))
report_inputs = input_values(context, "report")
if not report_inputs:
    errors.append(structural_error("final review is missing the stitched report input"))
else:
    report_text, report_errors = load_text(report_inputs[-1]["path"])
    errors.extend(report_errors)
    errors.extend(validate_common(
        report_text,
        allowed_source_ids=source_ids(context, "evidence"),
    ))
emit(errors, verdict=verdict)
