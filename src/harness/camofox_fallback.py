"""Configuration and installation discovery for managed Camofox fallbacks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CAMOFOX_VERSION = "1.14.0"
DEFAULT_CAMOFOX_BASE_URL = "http://127.0.0.1:9377"


def default_camofox_home() -> Path:
    selected = os.environ.get("DEEPRESEARCH_CAMOFOX_HOME")
    return Path(selected).expanduser().resolve() if selected else (
        Path.home() / ".deepresearch-cli" / "camofox"
    ).resolve()


@dataclass(frozen=True)
class CamofoxFallbackSupport:
    enabled: bool = False
    home: Optional[Path] = None
    base_url: Optional[str] = None

    def resolved_home(self) -> Path:
        return (self.home or default_camofox_home()).expanduser().resolve()

    def resolved_command(self) -> Optional[str]:
        candidate = self.resolved_home() / "node_modules" / ".bin" / "camofox-browser"
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None

    def report(self) -> dict[str, object]:
        if not self.enabled:
            return {"camofox_fallback": "disabled"}
        command = self.resolved_command()
        return {
            "camofox_fallback": "configured" if command else "unavailable",
            "camofox_home": str(self.resolved_home()),
            "camofox_server_command": command,
            "camofox_base_url": self.base_url or DEFAULT_CAMOFOX_BASE_URL,
            "camofox_version": CAMOFOX_VERSION,
        }
