"""Source execution boundary independent of domain routing and result parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from ..contracts import SearchRequest
from ..registry.sources import ProviderDefinition


@dataclass(frozen=True)
class PreparedSourceInvocation:
    definition: ProviderDefinition
    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]


class SourceAdapter(Protocol):
    def prepare(self, request: SearchRequest, *, limit: int) -> PreparedSourceInvocation:
        """Prepare one bounded source invocation without executing it."""
