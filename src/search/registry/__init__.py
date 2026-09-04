"""Configuration registries for search sources and research domains."""

from .domains import (
    DomainDefinition,
    DomainOperation,
    DomainRegistry,
    DomainRegistryError,
)
from .sources import (
    ProviderDefinition,
    ProviderRegistry,
    ProviderRegistryError,
    load_search_environment,
)

__all__ = [
    "DomainDefinition",
    "DomainOperation",
    "DomainRegistry",
    "DomainRegistryError",
    "ProviderDefinition",
    "ProviderRegistry",
    "ProviderRegistryError",
    "load_search_environment",
]
