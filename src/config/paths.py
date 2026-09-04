"""Resolve the built-in configuration in source and installed layouts."""

from __future__ import annotations

import sysconfig
from pathlib import Path


def builtin_config_dir() -> Path:
    """Return the single built-in config root for this installation."""

    project_root = Path(__file__).resolve().parents[2]
    source_config = project_root / "config"
    if (project_root / "pyproject.toml").is_file() and source_config.is_dir():
        return source_config

    return (
        Path(sysconfig.get_path("data"))
        / "share"
        / "deepresearch-cli"
        / "config"
    )


def builtin_asset_dir() -> Path:
    """Return the built-in static asset root for this installation."""

    project_root = Path(__file__).resolve().parents[2]
    source_assets = project_root / "assets"
    if (project_root / "pyproject.toml").is_file() and source_assets.is_dir():
        return source_assets

    return (
        Path(sysconfig.get_path("data"))
        / "share"
        / "deepresearch-cli"
        / "assets"
    )


def builtin_docs_dir() -> Path:
    """Return the built-in documentation root for this installation."""

    project_root = Path(__file__).resolve().parents[2]
    source_docs = project_root / "docs"
    if (project_root / "pyproject.toml").is_file() and source_docs.is_dir():
        return source_docs

    return (
        Path(sysconfig.get_path("data"))
        / "share"
        / "deepresearch-cli"
        / "docs"
    )
