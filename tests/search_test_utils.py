from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def write_search_source(
    search_dir: Path,
    name: str,
    *,
    script_text: str = "print('{}')\n",
    script_path: Path | None = None,
    args: list[str] | None = None,
    **overrides: Any,
) -> Path:
    sources_dir = search_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    selected_script = script_path or (search_dir / "scripts" / f"{name}.py")
    selected_script.parent.mkdir(parents=True, exist_ok=True)
    selected_script.write_text(script_text, encoding="utf-8")
    try:
        script_value = str(selected_script.resolve().relative_to(search_dir.resolve()))
    except ValueError:
        script_value = str(selected_script.resolve())
    value: dict[str, Any] = {
        "version": 1,
        "name": name,
        "script": script_value,
        "args": args if args is not None else ["{query}", "--limit", "{limit}"],
        "capability": f"{name} test search",
        "query_style": "Use a provider-specific test query.",
        "result_semantics": "Returns deterministic test discoveries.",
    }
    value.update(overrides)
    (sources_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return selected_script
