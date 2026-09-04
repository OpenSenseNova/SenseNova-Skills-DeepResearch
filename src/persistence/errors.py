"""Persistence-layer exceptions.

The persistence package deliberately exposes a small exception hierarchy so the
CLI/runtime can turn corrupt state, unsafe paths, and ordinary lookup failures
into different user-facing errors without depending on exception message text.
"""


class PersistenceError(Exception):
    """Base class for all run-store failures."""


class RunAlreadyExistsError(PersistenceError):
    """Raised when creating a run would replace an existing run directory."""


class RunNotFoundError(PersistenceError):
    """Raised when a requested run does not exist."""


class RunBusyError(PersistenceError):
    """Raised when another process already owns a Run execution session."""


class PersistenceValidationError(PersistenceError):
    """Raised when persisted or supplied data violates the runtime contract."""


class IntegrityError(PersistenceValidationError):
    """Raised when persisted bytes do not match their recorded integrity data."""


class UnsafePathError(PersistenceValidationError):
    """Raised when a path could escape its owning run or attempt directory."""
