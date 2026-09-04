"""Execution adapters for heterogeneous search sources."""

from .base import PreparedSourceInvocation, SourceAdapter
from .subprocess import SubprocessSourceAdapter

__all__ = ["PreparedSourceInvocation", "SourceAdapter", "SubprocessSourceAdapter"]
