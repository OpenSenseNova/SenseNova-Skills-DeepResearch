"""Stable source-registry import boundary.

The original :mod:`deepresearch_cli.search.providers` module remains a
compatibility facade while callers migrate to this package.  Source execution
and domain routing no longer need to import result-normalization helpers from
that legacy mixed-responsibility module.
"""

from ..providers import (
    ProviderDefinition,
    ProviderRegistry,
    ProviderRegistryError,
    load_search_environment,
)

__all__ = [
    "ProviderDefinition",
    "ProviderRegistry",
    "ProviderRegistryError",
    "load_search_environment",
]
