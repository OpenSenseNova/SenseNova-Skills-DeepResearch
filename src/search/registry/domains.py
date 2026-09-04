"""Declarative many-to-many domain-to-source routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import yaml

from .sources import ProviderRegistry


_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_POLICIES = frozenset({"all_relevant"})
_DOMAIN_KEYS = frozenset(
    {
        "version",
        "name",
        "description",
        "default_operation",
        "operations",
        "max_parallel",
        "minimum_successful_sources",
        "source_policy",
        "total_result_limit",
    }
)
_OPERATION_KEYS = frozenset({"description", "sources"})


class DomainRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class DomainOperation:
    name: str
    description: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class DomainDefinition:
    name: str
    source_file: Path
    description: str
    default_operation: str
    operations: Mapping[str, DomainOperation]
    max_parallel: int = 8
    minimum_successful_sources: int = 1
    source_policy: str = "all_relevant"
    total_result_limit: int = 100

    def operation(self, name: Optional[str] = None) -> DomainOperation:
        selected = name or self.default_operation
        try:
            return self.operations[selected]
        except KeyError as exc:
            raise DomainRegistryError(
                f"domain {self.name} does not support operation {selected!r}; "
                f"choose one of: {', '.join(self.operations)}"
            ) from exc


def _text(value: Any, *, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainRegistryError(f"search domain {path.name} {field} must be text")
    return value.strip()


def _positive_int(value: Any, *, field: str, path: Path, default: int, maximum: int) -> int:
    selected = default if value is None else value
    if isinstance(selected, bool) or not isinstance(selected, int) or not 1 <= selected <= maximum:
        raise DomainRegistryError(
            f"search domain {path.name} {field} must be between 1 and {maximum}"
        )
    return selected


def _load_domain(path: Path, *, source_names: frozenset[str]) -> DomainDefinition:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DomainRegistryError(f"cannot load search domain {path.name}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DomainRegistryError(f"search domain {path.name} must contain an object")
    value = dict(raw)
    unknown = set(value) - _DOMAIN_KEYS
    if unknown:
        raise DomainRegistryError(
            f"search domain {path.name} has unknown keys: {', '.join(sorted(unknown))}"
        )
    if str(value.get("version")) != "1":
        raise DomainRegistryError(f"search domain {path.name} requires version: 1")
    name = _text(value.get("name"), field="name", path=path)
    if not _NAME.fullmatch(name) or path.stem != name:
        raise DomainRegistryError(
            f"search domain filename {path.name} must match a snake-case name"
        )
    raw_operations = value.get("operations")
    if not isinstance(raw_operations, Mapping) or not raw_operations:
        raise DomainRegistryError(f"search domain {path.name} operations must be an object")
    operations: dict[str, DomainOperation] = {}
    for operation_name, raw_operation in raw_operations.items():
        if not isinstance(operation_name, str) or not _NAME.fullmatch(operation_name):
            raise DomainRegistryError(f"search domain {path.name} has invalid operation name")
        if not isinstance(raw_operation, Mapping):
            raise DomainRegistryError(
                f"search domain {path.name} operation {operation_name} must be an object"
            )
        operation_value = dict(raw_operation)
        extra = set(operation_value) - _OPERATION_KEYS
        if extra:
            raise DomainRegistryError(
                f"search domain {path.name} operation {operation_name} has unknown keys: "
                + ", ".join(sorted(extra))
            )
        raw_sources = operation_value.get("sources")
        if (
            not isinstance(raw_sources, Sequence)
            or isinstance(raw_sources, (str, bytes))
            or not raw_sources
            or any(not isinstance(item, str) or not item for item in raw_sources)
        ):
            raise DomainRegistryError(
                f"search domain {path.name} operation {operation_name} sources must be a non-empty string array"
            )
        sources = tuple(dict.fromkeys(raw_sources))
        missing = set(sources) - source_names
        if missing:
            raise DomainRegistryError(
                f"search domain {path.name} operation {operation_name} references unknown sources: "
                + ", ".join(sorted(missing))
            )
        operations[operation_name] = DomainOperation(
            name=operation_name,
            description=_text(
                operation_value.get("description"),
                field=f"operations.{operation_name}.description",
                path=path,
            ),
            sources=sources,
        )
    default_operation = str(value.get("default_operation") or next(iter(operations)))
    if default_operation not in operations:
        raise DomainRegistryError(
            f"search domain {path.name} default_operation is not declared"
        )
    policy = str(value.get("source_policy") or "all_relevant")
    if policy not in _POLICIES:
        raise DomainRegistryError(
            f"search domain {path.name} source_policy must be one of: {', '.join(sorted(_POLICIES))}"
        )
    minimum = _positive_int(
        value.get("minimum_successful_sources"),
        field="minimum_successful_sources",
        path=path,
        default=1,
        maximum=64,
    )
    if any(minimum > len(operation.sources) for operation in operations.values()):
        raise DomainRegistryError(
            f"search domain {path.name} minimum_successful_sources exceeds an operation source count"
        )
    return DomainDefinition(
        name=name,
        source_file=path.resolve(),
        description=_text(value.get("description"), field="description", path=path),
        default_operation=default_operation,
        operations=operations,
        max_parallel=_positive_int(
            value.get("max_parallel"), field="max_parallel", path=path, default=8, maximum=32
        ),
        minimum_successful_sources=minimum,
        source_policy=policy,
        total_result_limit=_positive_int(
            value.get("total_result_limit"),
            field="total_result_limit",
            path=path,
            default=100,
            maximum=1000,
        ),
    )


class DomainRegistry:
    """Load optional domain profiles without weakening source compatibility."""

    def __init__(self, *, search_dir: Path, source_registry: ProviderRegistry) -> None:
        self.search_dir = search_dir.expanduser().resolve()
        domains_dir = self.search_dir / "domains"
        definitions: dict[str, DomainDefinition] = {}
        if domains_dir.is_dir() and not domains_dir.is_symlink():
            source_names = frozenset(source_registry.names)
            for path in sorted(domains_dir.glob("*.yaml")):
                if path.is_symlink() or not path.is_file():
                    raise DomainRegistryError(f"search domain file is unsafe: {path}")
                definition = _load_domain(path, source_names=source_names)
                if definition.name in definitions:
                    raise DomainRegistryError(f"duplicate search domain: {definition.name}")
                definitions[definition.name] = definition
        self._definitions = definitions

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    @property
    def definitions(self) -> tuple[DomainDefinition, ...]:
        return tuple(self._definitions.values())

    def definition(self, name: str) -> DomainDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            choices = ", ".join(self.names) or "none configured"
            raise DomainRegistryError(
                f"unsupported search domain: {name}; choose one of: {choices}"
            ) from exc

    def resolve(self, name: str, operation: Optional[str] = None) -> tuple[DomainDefinition, DomainOperation]:
        definition = self.definition(name)
        return definition, definition.operation(operation)

    def list_domains(
        self,
        *,
        source_registry: ProviderRegistry,
        availability: Optional[Callable[[str], tuple[bool, Optional[str]]]] = None,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for domain in self.definitions:
            operations: list[dict[str, Any]] = []
            for operation in domain.operations.values():
                available: list[str] = []
                unavailable: list[dict[str, str]] = []
                for source in operation.sources:
                    definition = source_registry.definition(source)
                    ok, reason = (
                        availability(source)
                        if availability is not None
                        else source_registry.availability(definition)
                    )
                    if ok:
                        available.append(source)
                    else:
                        unavailable.append({"source": source, "reason": reason or "unavailable"})
                operations.append(
                    {
                        "name": operation.name,
                        "description": operation.description,
                        "sources": list(operation.sources),
                        "available_sources": available,
                        "unavailable_sources": unavailable,
                    }
                )
            output.append(
                {
                    "domain": domain.name,
                    "description": domain.description,
                    "default_operation": domain.default_operation,
                    "source_policy": domain.source_policy,
                    "max_parallel": domain.max_parallel,
                    "minimum_successful_sources": domain.minimum_successful_sources,
                    "total_result_limit": domain.total_result_limit,
                    "operations": operations,
                }
            )
        return output
