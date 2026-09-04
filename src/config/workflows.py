from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .models import WorkflowSpec
from .paths import builtin_config_dir


class WorkflowLoadError(ValueError):
    pass


def builtin_workflow_dir() -> Path:
    return builtin_config_dir() / "workflows"


def load_workflow_spec(*, mode: Optional[str] = None, path: Optional[Path] = None) -> WorkflowSpec:
    if (mode is None) == (path is None):
        raise WorkflowLoadError("exactly one of mode or path is required")
    requested = Path(path) if path is not None else builtin_workflow_dir() / f"{mode}.yaml"
    requested = requested.expanduser()
    if requested.is_symlink() or not requested.is_file():
        raise WorkflowLoadError(f"workflow file not found or unsafe: {requested}")
    source = requested.resolve()
    try:
        value: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowLoadError(f"cannot load workflow {source}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise WorkflowLoadError("workflow YAML must contain an object")
    required = {"version", "name", "steps", "result"}
    allowed = required | {"timeouts"}
    if not required.issubset(value) or set(value) - allowed:
        raise WorkflowLoadError(
            "workflow YAML requires version, name, steps, and result; "
            "timeouts is the only optional field"
        )
    if str(value.get("version")) != "1":
        raise WorkflowLoadError("workflow version must be 1")
    return WorkflowSpec.from_dict(value)
