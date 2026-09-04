from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def load_context() -> Mapping[str, Any]:
    return json.loads(Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8"))


def output_path(context: Mapping[str, Any], port: str) -> Path:
    return Path(context["outputs"][port]["path"])


def structural_error(message: str) -> Mapping[str, Any]:
    return {"rule": "RUNTIME", "severity": "error", "message": message}
