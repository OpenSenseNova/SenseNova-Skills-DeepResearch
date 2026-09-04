"""Compatibility adapter for the repository's existing provider scripts."""

from __future__ import annotations

from typing import Callable, Mapping

from ..contracts import SearchRequest
from ..registry.sources import ProviderDefinition, ProviderRegistry
from .base import PreparedSourceInvocation


class SubprocessSourceAdapter:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        environment_factory: Callable[[ProviderDefinition], Mapping[str, str]],
    ) -> None:
        self.registry = registry
        self.environment_factory = environment_factory

    def prepare(self, request: SearchRequest, *, limit: int) -> PreparedSourceInvocation:
        definition, command = self.registry.command(request, limit=limit)
        return PreparedSourceInvocation(
            definition=definition,
            command=tuple(command),
            cwd=self.registry.script_path(definition).parent,
            environment=dict(self.environment_factory(definition)),
        )
