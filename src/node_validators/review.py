from .contracts import load_context, output_path
from .markdown_review import emit, validate_markdown


context = load_context()
verdict, errors = validate_markdown(
    output_path(context, "review"),
    min_level2_headings=2,
)
emit(errors, verdict=verdict)
