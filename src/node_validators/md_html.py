import json, os, pathlib, sys
context = json.loads(pathlib.Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text())
report = pathlib.Path(context["outputs"]["report"]["path"])
if not report.is_file():
    print("report.html is missing")
    sys.exit(1)
text = report.read_text(encoding="utf-8").lower()
if "<html" not in text or "</html>" not in text:
    print("report.html is not a complete HTML document")
    sys.exit(1)
