"""Declarative launch configuration for one ACP agent process."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class AcpLaunchSpec:
    backend: str
    command: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    process_prefix: str = "acp-process"
