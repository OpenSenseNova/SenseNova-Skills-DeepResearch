"""Resolve the built-in user-configurable search registry."""

from __future__ import annotations

import os
import shutil
import sysconfig
from pathlib import Path


def builtin_search_dir() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    source_search = project_root / "search"
    if (project_root / "pyproject.toml").is_file() and source_search.is_dir():
        return source_search
    return (
        Path(sysconfig.get_path("data"))
        / "share"
        / "deepresearch-cli"
        / "search"
    )


def user_search_config_dir() -> Path:
    """Stable per-user Search configuration shared by every harness."""
    configured = os.environ.get("DEEPRESEARCH_SEARCH_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".deepresearch-cli" / "search").resolve()


def user_search_env_file() -> Path:
    return user_search_config_dir() / ".env"


def initialize_user_search_environment(*, overwrite: bool = False) -> Path:
    """Create the user Search .env from the packaged example."""
    destination = user_search_env_file()
    if destination.exists() and not overwrite:
        return destination
    source = builtin_search_dir() / ".env.example"
    if not source.is_file():
        raise FileNotFoundError(f"packaged Search environment example is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination
