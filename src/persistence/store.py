"""File-backed persistence for a configuration-first workflow run.

The store has three authoritative inputs only:

* an immutable ``run.json`` manifest;
* an append-only ``journal.jsonl`` event stream;
* immutable files below ``artifacts/`` that are referenced by the journal.

Attempt diagnostics and staging files are intentionally kept outside that
authoritative set.  They are useful for inspection, but merely writing a file
there can never make a node successful.
"""

from __future__ import annotations

import errno
import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Union

from .errors import (
    IntegrityError,
    PersistenceError,
    PersistenceValidationError,
    RunAlreadyExistsError,
    RunBusyError,
    RunNotFoundError,
    UnsafePathError,
)


JsonObject = Dict[str, Any]
PathLike = Union[str, os.PathLike]

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_MEMBER_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_TYPES = {"step_started", "step_finished", "run_finished"}
_ATTEMPT_OUTCOMES = {"succeeded", "failed", "repairable", "retryable", "interrupted"}


@dataclass(frozen=True)
class AttemptLayout:
    """Absolute paths owned by one node attempt."""

    attempt_dir: Path
    staging_dir: Path
    candidate_dir: Path
    invocation_path: Path
    acp_events_path: Path
    stderr_path: Path
    raw_response_path: Path
    harness_path: Path

    @property
    def diagnostics_ref(self) -> str:
        """The diagnostics directory path relative to its run directory."""

        # attempt_dir = <run>/attempts/<instance>/attempt-<n>
        return PurePosixPath(*self.attempt_dir.parts[-3:]).as_posix()


@dataclass(frozen=True)
class LoadedRun:
    """Validated persisted inputs used to rebuild a workflow projection."""

    run_dir: Path
    manifest: JsonObject
    events: List[JsonObject]


def sha256_file(path: PathLike, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the design-level ``sha256:<hex>`` digest for a regular file."""

    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _plain_json(value: Any, *, label: str) -> Any:
    """Validate JSON serializability and detach caller-owned mutable objects."""

    if not isinstance(value, Mapping):
        converter = getattr(value, "to_dict", None)
        if converter is None or not callable(converter):
            raise PersistenceValidationError(
                f"{label} must be a mapping or expose to_dict()"
            )
        value = converter()
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return json.loads(encoded, parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as exc:
        raise PersistenceValidationError(f"{label} is not valid JSON: {exc}") from exc


def _validate_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise PersistenceValidationError(
            f"{label} must match {_SAFE_ID.pattern!r}; got {value!r}"
        )
    if value in {".", ".."}:
        raise PersistenceValidationError(f"{label} cannot be {value!r}")
    return value


def _validate_member_key(value: Any, *, label: str) -> str:
    """Validate a routing key without applying filesystem ID length limits."""

    if not isinstance(value, str) or not _SAFE_MEMBER_KEY.fullmatch(value):
        raise PersistenceValidationError(
            f"{label} must match {_SAFE_MEMBER_KEY.pattern!r}; got {value!r}"
        )
    if value in {".", ".."}:
        raise PersistenceValidationError(f"{label} cannot be {value!r}")
    return value


def _validate_attempt(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PersistenceValidationError("attempt must be a positive integer")
    return value


def _safe_posix_relative(value: Any, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise UnsafePathError(f"{label} must be a non-empty relative path")
    if "\x00" in value or "\\" in value:
        raise UnsafePathError(f"{label} contains an unsafe path character")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or not candidate.parts:
        raise UnsafePathError(f"{label} must be relative")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise UnsafePathError(f"{label} cannot contain '.' or '..' segments")
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after an atomic namespace change."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some supported filesystems do not allow fsync on directories.
        pass
    finally:
        os.close(descriptor)


@contextmanager
def _portable_file_lock(
    path: Path,
    *,
    exclusive: bool,
    blocking: bool,
    busy_message: str,
) -> Iterator[None]:
    """Hold one advisory lock file without replacing or unlinking its inode.

    POSIX uses ``flock`` and Windows uses a one-byte ``msvcrt`` range lock.  Lock
    files are deliberately permanent: removing a lock path while another process
    still owns its old inode would allow a second, unrelated lock to be created.
    """

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, 0o600)
    acquired = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafePathError(f"lock path is not a regular file: {path}")

        if os.name == "posix":
            import fcntl

            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(descriptor, operation)
                acquired = True
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise RunBusyError(busy_message) from exc
                raise
        elif os.name == "nt":  # pragma: no cover - exercised on Windows CI/users.
            import msvcrt

            if metadata.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
            while True:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, mode, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if not blocking:
                        raise RunBusyError(busy_message) from exc
                    time.sleep(0.05)
        else:  # pragma: no cover - CPython release targets are POSIX/Windows.
            raise PersistenceError(
                "cross-process Run locking is unsupported on os.name=%r" % os.name
            )

        yield
    finally:
        if acquired:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif os.name == "nt":  # pragma: no cover - Windows only.
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


class RunStore:
    """Own run directories and validate all paths crossing the store boundary.

    A Run has one cross-process execution owner at a time.  Journal commits are
    serialized separately so status readers can take stable snapshots while the
    owner is waiting on a long-running node invocation.
    """

    def __init__(self, runs_root: PathLike):
        requested_root = Path(runs_root).expanduser()
        requested_root.mkdir(parents=True, exist_ok=True)
        if not requested_root.is_dir():
            raise PersistenceValidationError(
                f"runs root is not a directory: {requested_root}"
            )
        self.root = requested_root.resolve(strict=True)
        self._lock = threading.RLock()
        locks_root = self.root / ".locks"
        if locks_root.is_symlink():
            raise UnsafePathError("runs lock directory cannot be a symlink")
        locks_root.mkdir(mode=0o700, exist_ok=True)
        if locks_root.is_symlink() or not locks_root.is_dir():
            raise UnsafePathError("runs lock path is not a real directory")
        self._locks_root = locks_root.resolve(strict=True)
        if self._locks_root.parent != self.root:
            raise UnsafePathError("runs lock directory escapes runs root")

    @contextmanager
    def execution_session(self, run_id: str) -> Iterator[None]:
        """Fail fast unless this process owns the Run's mutable drive session.

        The lock is intentionally broader than a journal append.  A caller holds
        it across load/reconcile, every node attempt, publication, and commit
        so two CLI processes can never both execute the same READY instance.
        """

        safe_id = _validate_identifier(run_id, label="run_id")
        path = self._locks_root / f"{safe_id}.execution.lock"
        with _portable_file_lock(
            path,
            exclusive=True,
            blocking=False,
            busy_message=(
                f"run {safe_id} is already being driven by another process"
            ),
        ):
            yield

    @contextmanager
    def _state_file_lock(self, run_id: str, *, exclusive: bool) -> Iterator[None]:
        """Serialize journal commits while allowing stable concurrent status reads."""

        safe_id = _validate_identifier(run_id, label="run_id")
        path = self._locks_root / f"{safe_id}.state.lock"
        with _portable_file_lock(
            path,
            exclusive=exclusive,
            blocking=True,
            busy_message=f"run {safe_id} state is currently being committed",
        ):
            yield

    def run_dir(self, run_id: str, *, must_exist: bool = True) -> Path:
        """Return a safe, non-symlink run directory path."""

        safe_id = _validate_identifier(run_id, label="run_id")
        candidate = self.root / safe_id
        if not must_exist:
            return candidate
        if not candidate.exists():
            raise RunNotFoundError(f"run does not exist: {safe_id}")
        if candidate.is_symlink() or not candidate.is_dir():
            raise UnsafePathError(f"run path is not a real directory: {safe_id}")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != self.root:
            raise UnsafePathError(f"run directory escapes runs root: {safe_id}")
        return resolved

    def create_run(
        self, manifest: Any, *, run_id: Optional[str] = None
    ) -> Path:
        """Create a complete run skeleton without replacing an existing run.

        ``run.json`` is written before the temporary directory is renamed into
        place.  Once published, this class exposes no API that can modify it.
        """

        plain = _plain_json(manifest, label="manifest")
        if not isinstance(plain, dict):
            raise PersistenceValidationError("manifest must be a JSON object")
        manifest_run_id = plain.get("run_id")
        selected_id = run_id if run_id is not None else manifest_run_id
        safe_id = _validate_identifier(selected_id, label="run_id")
        if manifest_run_id != safe_id:
            raise PersistenceValidationError(
                "manifest run_id must exactly match the requested run_id"
            )
        self._validate_manifest(plain, expected_run_id=safe_id)

        destination = self.run_dir(safe_id, must_exist=False)
        with self._lock:
            if destination.exists() or destination.is_symlink():
                raise RunAlreadyExistsError(f"run already exists: {safe_id}")
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{safe_id}.creating-", dir=str(self.root))
            )
            try:
                (temporary / "artifacts").mkdir()
                (temporary / "attempts").mkdir()
                self._atomic_write_json(
                    temporary / "run.json", plain, pretty=True, replace=False
                )
                os.chmod(temporary / "run.json", 0o444)
                self._write_new_bytes(temporary / "journal.jsonl", b"")
                _fsync_directory(temporary)

                # os.rename is atomic on the same filesystem.  The existence
                # check above is sufficient under the documented single-driver
                # contract; a concurrent creator still cannot alter files
                # inside our private temporary directory.
                os.rename(str(temporary), str(destination))
                _fsync_directory(self.root)
            except FileExistsError as exc:
                raise RunAlreadyExistsError(f"run already exists: {safe_id}") from exc
            finally:
                if temporary.exists():
                    shutil.rmtree(str(temporary))
        return destination

    def load_manifest(self, run_id: str) -> JsonObject:
        run_directory = self.run_dir(run_id)
        manifest = self._load_json_object(run_directory / "run.json", "run.json")
        self._validate_manifest(manifest, expected_run_id=run_id)
        return manifest

    def load_journal(self, run_id: str) -> List[JsonObject]:
        """Load a complete, newline-terminated, contiguous event stream."""

        with self._state_file_lock(run_id, exclusive=False):
            return self._load_journal_unlocked(run_id)

    def _load_journal_unlocked(self, run_id: str) -> List[JsonObject]:
        journal_path = self.run_dir(run_id) / "journal.jsonl"
        self._assert_regular_file(journal_path, label="journal.jsonl")
        payload = journal_path.read_bytes()
        if payload and not payload.endswith(b"\n"):
            raise IntegrityError("journal.jsonl has a torn final record")

        events: List[JsonObject] = []
        event_ids = set()
        for line_number, raw_line in enumerate(payload.splitlines(), start=1):
            if not raw_line.strip():
                raise IntegrityError(
                    f"journal.jsonl contains a blank record at line {line_number}"
                )
            try:
                event = json.loads(
                    raw_line.decode("utf-8"), parse_constant=_reject_json_constant
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise IntegrityError(
                    f"journal.jsonl line {line_number} is invalid JSON: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise IntegrityError(
                    f"journal.jsonl line {line_number} is not an object"
                )
            try:
                self._validate_event(event, expected_seq=line_number)
            except PersistenceValidationError as exc:
                raise IntegrityError(
                    f"journal.jsonl line {line_number} is invalid: {exc}"
                ) from exc
            event_id = event["event_id"]
            if event_id in event_ids:
                raise IntegrityError(f"duplicate event_id in journal: {event_id}")
            event_ids.add(event_id)
            events.append(event)
        try:
            self._validate_event_sequence(events)
        except PersistenceValidationError as exc:
            raise IntegrityError(f"journal event sequence is invalid: {exc}") from exc
        return events

    def append_event(self, run_id: str, event: Any) -> JsonObject:
        """Append one validated event and assign missing sequence metadata."""

        plain = _plain_json(event, label="event")
        if not isinstance(plain, dict):
            raise PersistenceValidationError("event must be a JSON object")
        with self._lock, self._state_file_lock(run_id, exclusive=True):
            self.load_manifest(run_id)
            existing = self._load_journal_unlocked(run_id)
            # Refuse to extend a run whose previously committed file facts no
            # longer validate.  This avoids recording more state on top of a
            # corrupt or externally edited artifact set.
            self._validate_persisted_references(run_id, existing)
            expected_seq = len(existing) + 1
            if plain.get("seq") is None:
                plain["seq"] = expected_seq
            if plain.get("event_id") is None:
                plain["event_id"] = "evt-" + uuid.uuid4().hex
            if plain.get("recorded_at") is None:
                plain["recorded_at"] = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
            self._validate_event(plain, expected_seq=expected_seq)
            self._validate_event_sequence(existing + [plain])

            known_ids = {item["event_id"] for item in existing}
            if plain["event_id"] in known_ids:
                raise PersistenceValidationError(
                    f"event_id already exists: {plain['event_id']}"
                )
            self._validate_persisted_references(run_id, existing + [plain])

            encoded = (
                json.dumps(
                    plain,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            journal_path = self.run_dir(run_id) / "journal.jsonl"
            journal_flags = os.O_WRONLY | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                journal_flags |= os.O_NOFOLLOW
            descriptor = os.open(str(journal_path), journal_flags)
            try:
                # One os.write keeps each event append indivisible from the
                # perspective of other writers using O_APPEND.
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    raise IntegrityError("short write while appending journal event")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return plain

    @staticmethod
    def _validate_event_sequence(events: Iterable[Mapping[str, Any]]) -> None:
        instances: Dict[str, tuple[int, bool, Optional[str]]] = {}
        terminal = False
        for event in events:
            if terminal:
                raise PersistenceValidationError(
                    "no event may follow run_finished"
                )
            event_type = event["type"]
            if event_type == "run_finished":
                if any(active for _, active, _ in instances.values()):
                    raise PersistenceValidationError(
                        "run_finished cannot leave an active step attempt"
                    )
                terminal = True
                continue
            instance_id = event["instance_id"]
            attempt = event["attempt"]
            previous = instances.get(instance_id)
            if event_type == "step_started":
                if previous is None:
                    expected_attempt = 1
                else:
                    prior_attempt, active, prior_outcome = previous
                    if active:
                        raise PersistenceValidationError(
                            f"step {instance_id} already has an active attempt"
                        )
                    if prior_outcome not in {"repairable", "retryable", "interrupted"}:
                        raise PersistenceValidationError(
                            f"step {instance_id} cannot retry after {prior_outcome}"
                        )
                    expected_attempt = prior_attempt + 1
                if attempt != expected_attempt:
                    raise PersistenceValidationError(
                        f"step {instance_id} expected attempt {expected_attempt}, got {attempt}"
                    )
                instances[instance_id] = (attempt, True, None)
                continue
            if previous is None or not previous[1] or previous[0] != attempt:
                raise PersistenceValidationError(
                    f"step_finished does not match an active attempt for {instance_id}"
                )
            instances[instance_id] = (attempt, False, str(event["outcome"]))

    def prepare_attempt(
        self, run_id: str, node_instance_id: str, attempt: int
    ) -> AttemptLayout:
        """Create (or reopen) a safe diagnostics/staging directory."""

        run_directory = self.run_dir(run_id)
        safe_instance = _validate_identifier(
            node_instance_id, label="node_instance_id"
        )
        safe_attempt = _validate_attempt(attempt)
        with self._lock:
            attempt_dir = self._ensure_directory_chain(
                run_directory,
                ("attempts", safe_instance, f"attempt-{safe_attempt}"),
            )
            staging_dir = self._ensure_directory_chain(attempt_dir, ("staging",))
        return AttemptLayout(
            attempt_dir=attempt_dir,
            staging_dir=staging_dir,
            candidate_dir=attempt_dir / "candidate",
            invocation_path=attempt_dir / "invocation.json",
            acp_events_path=attempt_dir / "acp-events.jsonl",
            stderr_path=attempt_dir / "stderr.log",
            raw_response_path=attempt_dir / "raw-response.txt",
            harness_path=attempt_dir / "harness.json",
        )

    def staging_dir(
        self, run_id: str, node_instance_id: str, attempt: int
    ) -> Path:
        return self.prepare_attempt(run_id, node_instance_id, attempt).staging_dir

    def write_attempt_json(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        relative_path: str,
        value: Any,
        *,
        replace: bool = False,
    ) -> str:
        plain = _plain_json(value, label="attempt diagnostic")
        payload = (
            json.dumps(
                plain,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        return self._write_attempt_bytes(
            run_id,
            node_instance_id,
            attempt,
            relative_path,
            payload,
            replace=replace,
        )

    def write_attempt_text(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        relative_path: str,
        text: Union[str, bytes],
        *,
        replace: bool = False,
    ) -> str:
        payload = text.encode("utf-8") if isinstance(text, str) else text
        if not isinstance(payload, bytes):
            raise PersistenceValidationError("attempt text must be str or bytes")
        return self._write_attempt_bytes(
            run_id,
            node_instance_id,
            attempt,
            relative_path,
            payload,
            replace=replace,
        )

    def append_attempt_text(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        relative_path: str,
        text: Union[str, bytes],
    ) -> str:
        payload = text.encode("utf-8") if isinstance(text, str) else text
        if not isinstance(payload, bytes):
            raise PersistenceValidationError("attempt text must be str or bytes")
        destination, reference = self._attempt_diagnostic_path(
            run_id, node_instance_id, attempt, relative_path
        )
        with self._lock:
            diagnostic_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                diagnostic_flags |= os.O_NOFOLLOW
            descriptor = os.open(
                str(destination), diagnostic_flags, 0o600
            )
            try:
                view = memoryview(payload)
                while view:
                    count = os.write(descriptor, view)
                    if count <= 0:
                        raise IntegrityError("short write in attempt diagnostic")
                    view = view[count:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return reference

    def append_attempt_jsonl(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        relative_path: str,
        value: Any,
    ) -> str:
        plain = _plain_json(value, label="attempt JSONL record")
        encoded = (
            json.dumps(
                plain,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return self.append_attempt_text(
            run_id,
            node_instance_id,
            attempt,
            relative_path,
            encoded,
        )

    def freeze_staging_candidate(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        *,
        exclude_top_level: Iterable[str] = ("_contracts",),
    ) -> Path:
        """Copy the staging tree into a private, stable attempt candidate.

        The Harness only receives the staging directory.  The candidate is made
        after the Harness returns and is never exposed in the invocation.  The
        reserved ``_contracts`` tree is excluded so trusted code can recreate it
        from the snapshotted Prompt Bundle before validation.
        """

        layout = self.prepare_attempt(run_id, node_instance_id, attempt)
        excluded = set(exclude_top_level)
        for name in excluded:
            relative = _safe_posix_relative(
                name, label="excluded candidate directory"
            )
            if len(relative.parts) != 1:
                raise UnsafePathError(
                    "excluded candidate entry must be one top-level name"
                )
        with self._lock:
            if layout.candidate_dir.exists() or layout.candidate_dir.is_symlink():
                raise IntegrityError(
                    "attempt publication candidate already exists: "
                    + layout.candidate_dir.as_posix()
                )
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=".candidate-", dir=str(layout.attempt_dir)
                )
            )
            try:
                self._copy_tree_from_directory(
                    layout.staging_dir, temporary, excluded
                )
                _fsync_directory(temporary)
                os.rename(str(temporary), str(layout.candidate_dir))
                _fsync_directory(layout.attempt_dir)
            finally:
                if temporary.exists():
                    shutil.rmtree(str(temporary))
        return layout.candidate_dir

    def seal_candidate(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        candidate_root: PathLike,
    ) -> None:
        """Make a private candidate tree read-only before typed validation."""

        candidate = self._validated_candidate_root(
            run_id, node_instance_id, attempt, candidate_root
        )
        self._seal_tree(candidate)

    def snapshot_candidate(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        candidate_root: PathLike,
        *,
        exclude_top_level: Iterable[str] = ("_contracts",),
    ) -> Dict[str, str]:
        """Hash every regular business file in a sealed candidate tree."""

        candidate = self._validated_candidate_root(
            run_id, node_instance_id, attempt, candidate_root
        )
        excluded = set(exclude_top_level)
        return self._snapshot_tree(candidate, excluded)

    def publish_artifacts(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        publication_root: PathLike,
        validated_files: Mapping[str, str],
    ) -> List[JsonObject]:
        """Publish one validated attempt as an atomic directory transaction.

        ``validated_files`` binds typed validation to exact candidate bytes.  All
        sources are opened relative to one ``O_NOFOLLOW`` root fd, copied into a
        hidden attempt directory, and checked against those digests.  Only after
        every file succeeds is the whole directory atomically renamed to
        ``artifacts/<instance>/attempt-<n>``.  Failures leave both staging and the
        candidate intact and never expose a partial formal attempt directory.
        """

        if not isinstance(validated_files, Mapping) or not validated_files:
            raise PersistenceValidationError(
                "validated_files must be a non-empty relative-path to sha256 mapping"
            )
        candidate = self._validated_candidate_root(
            run_id, node_instance_id, attempt, publication_root
        )
        safe_instance = _validate_identifier(
            node_instance_id, label="node_instance_id"
        )
        safe_attempt = _validate_attempt(attempt)
        normalized: Dict[PurePosixPath, str] = {}
        for raw_path, digest in validated_files.items():
            relative = _safe_posix_relative(raw_path, label="validated file path")
            if relative.parts[0] == "_contracts":
                raise UnsafePathError("contract resources cannot be published")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise PersistenceValidationError(
                    "validated file digest must use sha256:<64 lowercase hex>"
                )
            if relative in normalized:
                raise PersistenceValidationError(
                    "duplicate validated file: " + relative.as_posix()
                )
            normalized[relative] = digest

        run_directory = self.run_dir(run_id)
        instance_root = self._ensure_directory_chain(
            run_directory / "artifacts", (safe_instance,)
        )
        final_root = instance_root / f"attempt-{safe_attempt}"
        references: List[JsonObject] = []
        with self._lock:
            if final_root.exists() or final_root.is_symlink():
                raise IntegrityError(
                    "artifact attempt directory already exists: "
                    + final_root.relative_to(run_directory).as_posix()
                )
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".attempt-{safe_attempt}.publishing-",
                    dir=str(instance_root),
                )
            )
            root_fd = self._open_directory_fd(candidate)
            try:
                for relative, expected_digest in normalized.items():
                    source_fd = self._open_regular_beneath(root_fd, relative)
                    destination_parent = self._ensure_directory_chain(
                        temporary, relative.parts[:-1]
                    )
                    destination = destination_parent / relative.parts[-1]
                    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        destination_flags |= os.O_NOFOLLOW
                    destination_fd = os.open(
                        str(destination), destination_flags, 0o400
                    )
                    try:
                        actual_digest = self._copy_fd_and_hash(
                            source_fd, destination_fd
                        )
                        os.fsync(destination_fd)
                    finally:
                        os.close(destination_fd)
                        os.close(source_fd)
                    if actual_digest != expected_digest:
                        raise IntegrityError(
                            "validated candidate changed before publication: "
                            f"{relative.as_posix()} expected {expected_digest}, "
                            f"got {actual_digest}"
                        )
                    relative_to_run = PurePosixPath(
                        "artifacts",
                        safe_instance,
                        f"attempt-{safe_attempt}",
                        *relative.parts,
                    ).as_posix()
                    media_type = mimetypes.guess_type(relative.name)[0]
                    references.append(
                        {
                            "path": relative_to_run,
                            "sha256": actual_digest,
                            "media_type": media_type
                            or "application/octet-stream",
                        }
                    )
                # macOS requires the source directory itself to remain writable
                # for this rename. Seal every descendant first and leave only
                # the hidden root writable until the namespace move.
                self._seal_tree(temporary, seal_root=False)
                _fsync_directory(temporary)
                if final_root.exists() or final_root.is_symlink():
                    raise IntegrityError(
                        "artifact attempt directory appeared during publication: "
                        + final_root.relative_to(run_directory).as_posix()
                    )
                os.rename(str(temporary), str(final_root))
                # The rename is the publication commit point.  Post-commit
                # permission/durability hardening is best effort and must never
                # turn a visible, complete attempt into a reported failure.
                try:
                    os.chmod(final_root, 0o555, follow_symlinks=False)
                except OSError:
                    pass
                try:
                    _fsync_directory(instance_root)
                except OSError:
                    pass
            except Exception:
                if temporary.exists():
                    self._remove_private_tree(temporary)
                raise
            finally:
                os.close(root_fd)
                # The digest-bound copy is now either formally published or the
                # operation failed. Candidate is diagnostics from this point on,
                # so restore ordinary owner permissions to keep attempts prunable.
                try:
                    self._make_private_tree_writable(candidate)
                except OSError:
                    pass
        return references

    def publish_artifact(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        publication_root: PathLike,
        relative_path: str,
        validated_sha256: str,
    ) -> JsonObject:
        return self.publish_artifacts(
            run_id,
            node_instance_id,
            attempt,
            publication_root,
            {relative_path: validated_sha256},
        )[0]

    def validate_artifact_ref(self, run_id: str, artifact_ref: Any) -> Path:
        """Resolve one ArtifactRef and verify namespace, type, and sha256."""

        plain = _plain_json(artifact_ref, label="artifact_ref")
        if not isinstance(plain, dict):
            raise PersistenceValidationError("artifact_ref must be an object")
        relative, expected_hash = self._validate_artifact_ref_shape(plain)
        resolved = self.resolve_run_relative(
            run_id, relative.as_posix(), must_exist=True, require_file=True
        )
        actual_hash = sha256_file(resolved)
        if actual_hash != expected_hash:
            raise IntegrityError(
                f"artifact hash mismatch for {relative.as_posix()}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        return resolved

    def resolve_run_relative(
        self,
        run_id: str,
        relative_path: str,
        *,
        must_exist: bool = True,
        require_file: bool = False,
    ) -> Path:
        """Resolve a persisted POSIX path without following an escaping symlink."""

        relative = _safe_posix_relative(relative_path, label="run-relative path")
        run_directory = self.run_dir(run_id)
        candidate = run_directory.joinpath(*relative.parts)
        if must_exist:
            if not candidate.exists():
                raise IntegrityError(
                    f"referenced run path does not exist: {relative.as_posix()}"
                )
            if candidate.is_symlink():
                raise UnsafePathError(
                    f"referenced run path is a symlink: {relative.as_posix()}"
                )
            resolved = candidate.resolve(strict=True)
            if not _is_relative_to(resolved, run_directory):
                raise UnsafePathError(
                    f"referenced run path escapes run directory: {relative.as_posix()}"
                )
            if require_file and not resolved.is_file():
                raise IntegrityError(
                    f"referenced run path is not a file: {relative.as_posix()}"
                )
            return resolved
        parent = candidate.parent
        existing = parent
        while not existing.exists() and existing != run_directory:
            existing = existing.parent
        resolved_existing = existing.resolve(strict=True)
        if not _is_relative_to(resolved_existing, run_directory):
            raise UnsafePathError(
                f"run-relative path escapes run directory: {relative.as_posix()}"
            )
        return candidate

    def load_run(self, run_id: str, *, validate_artifacts: bool = True) -> LoadedRun:
        """Load authoritative state and validate every committed file reference."""

        with self._state_file_lock(run_id, exclusive=False):
            manifest = self.load_manifest(run_id)
            events = self._load_journal_unlocked(run_id)
            if validate_artifacts:
                self._validate_persisted_references(run_id, events)
            return LoadedRun(self.run_dir(run_id), manifest, events)

    def _validate_manifest(
        self, manifest: Mapping[str, Any], *, expected_run_id: str
    ) -> None:
        if manifest.get("schema_version") != "2":
            raise PersistenceValidationError(
                "unsupported manifest.schema_version: %r"
                % manifest.get("schema_version")
            )
        if manifest.get("runtime") != "config-workflow":
            raise PersistenceValidationError(
                "run was not created by the config-workflow runtime"
            )
        if manifest.get("run_id") != expected_run_id:
            raise PersistenceValidationError(
                "run.json run_id does not match its directory"
            )
        if "context" not in manifest or not isinstance(manifest["context"], dict):
            raise PersistenceValidationError("manifest.context must be an object")

    def _validate_event(self, event: Mapping[str, Any], *, expected_seq: int) -> None:
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq != expected_seq:
            raise PersistenceValidationError(
                f"event seq must be contiguous; expected {expected_seq}, got {seq!r}"
            )
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise PersistenceValidationError("event_id must be a non-empty string")
        event_type = event.get("type")
        if event_type not in _EVENT_TYPES:
            raise PersistenceValidationError(
                f"event type must be one of {sorted(_EVENT_TYPES)}"
            )
        recorded_at = event.get("recorded_at")
        if recorded_at is not None:
            if not isinstance(recorded_at, str) or not recorded_at.endswith("Z"):
                raise PersistenceValidationError(
                    "event.recorded_at must be a UTC ISO-8601 string"
                )
            try:
                datetime.fromisoformat(recorded_at[:-1] + "+00:00")
            except ValueError as exc:
                raise PersistenceValidationError(
                    "event.recorded_at must be a UTC ISO-8601 string"
                ) from exc

        if event_type == "run_finished":
            if event.get("status") not in {"completed", "failed"}:
                raise PersistenceValidationError("run_finished status is invalid")
            return

        node_instance_id = event.get("instance_id")
        _validate_identifier(node_instance_id, label="event.node_instance_id")
        _validate_identifier(event.get("step_id"), label="event.step_id")
        _validate_identifier(event.get("node_id"), label="event.node_id")
        _validate_attempt(event.get("attempt"))
        scope = event.get("scope", {})
        if not isinstance(scope, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in scope.items()
        ):
            raise PersistenceValidationError("event.scope must map non-empty strings")
        if event_type == "step_started":
            inputs = event.get("inputs", [])
            if not isinstance(inputs, list):
                raise PersistenceValidationError("step_started.inputs must be a list")
            for artifact_ref in inputs:
                self._validate_artifact_ref_shape(artifact_ref)
            return
        if event_type == "step_finished":
            if event.get("outcome") not in _ATTEMPT_OUTCOMES:
                raise PersistenceValidationError(
                    f"attempt outcome must be one of {sorted(_ATTEMPT_OUTCOMES)}"
                )
            artifact_refs = event.get("artifact_refs")
            if not isinstance(artifact_refs, list):
                raise PersistenceValidationError(
                    "finished step artifacts must be a list"
                )
            for artifact_ref in artifact_refs:
                if not isinstance(artifact_ref, dict):
                    raise PersistenceValidationError(
                        "finished attempt artifact ref must be an object"
                    )
                relative, _ = self._validate_artifact_ref_shape(artifact_ref)
                expected_prefix = (
                    "artifacts",
                    node_instance_id,
                    f"attempt-{event['attempt']}",
                )
                if (
                    len(relative.parts) < 4
                    or relative.parts[:3] != expected_prefix
                ):
                    raise IntegrityError(
                        "finished attempt artifact belongs to a different "
                        "instance/attempt: "
                        + relative.as_posix()
                    )
            if event["outcome"] != "succeeded" and artifact_refs:
                raise PersistenceValidationError(
                    "non-succeeded attempt cannot publish artifacts"
                )
            diagnostics_ref = event.get("diagnostics_ref")
            if diagnostics_ref is not None and not isinstance(diagnostics_ref, str):
                raise PersistenceValidationError(
                    "diagnostics_ref must be null or a relative path string"
                )
            if diagnostics_ref is not None:
                diagnostics = _safe_posix_relative(
                    diagnostics_ref, label="diagnostics_ref"
                )
                if not diagnostics.parts or diagnostics.parts[0] != "attempts":
                    raise UnsafePathError("diagnostics_ref must be below attempts/")
            error = event.get("error")
            if error is not None and not isinstance(error, str):
                raise PersistenceValidationError("attempt error must be null or string")
            if event["outcome"] == "repairable" and not (
                isinstance(error, str) and error.strip()
            ):
                raise PersistenceValidationError(
                    "repairable attempt requires a non-empty error"
                )
            warnings = event.get("validation_warnings", [])
            if not isinstance(warnings, list):
                raise PersistenceValidationError("validation_warnings must be a list")

    @staticmethod
    def _validate_artifact_ref_shape(
        artifact_ref: Mapping[str, Any]
    ) -> tuple:
        expected_keys = {
            "port", "type", "media_type", "path", "sha256", "scope",
            "mode", "step_id", "instance_id",
        }
        if set(artifact_ref) != expected_keys:
            raise PersistenceValidationError(
                "artifact_ref does not match the config-workflow Artifact contract"
            )
        relative = _safe_posix_relative(
            artifact_ref["path"], label="artifact_ref.path"
        )
        if not relative.parts or relative.parts[0] != "artifacts":
            raise UnsafePathError("artifact_ref.path must be below artifacts/")
        expected_hash = artifact_ref["sha256"]
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise PersistenceValidationError(
                "artifact_ref.sha256 must use sha256:<64 lowercase hex>"
            )
        if (
            not isinstance(artifact_ref["media_type"], str)
            or not artifact_ref["media_type"]
        ):
            raise PersistenceValidationError(
                "artifact_ref.media_type must be a non-empty string"
            )
        for key in ("port", "type", "mode", "step_id", "instance_id"):
            if not isinstance(artifact_ref[key], str) or not artifact_ref[key]:
                raise PersistenceValidationError(f"artifact_ref.{key} must be text")
        if artifact_ref["mode"] not in {"state", "batch"}:
            raise PersistenceValidationError("artifact_ref.mode is invalid")
        scope = artifact_ref["scope"]
        if not isinstance(scope, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in scope.items()
        ):
            raise PersistenceValidationError("artifact_ref.scope must map strings")
        return relative, expected_hash

    def _validate_persisted_references(
        self, run_id: str, events: Iterable[Mapping[str, Any]]
    ) -> None:
        """Validate committed outputs and scheduled inputs in journal order."""

        committed: Dict[str, str] = {}
        for event in events:
            event_type = event.get("type")
            if event_type == "step_started":
                for artifact_ref in event.get("inputs", []):
                    path = artifact_ref["path"]
                    digest = artifact_ref["sha256"]
                    if committed.get(path) != digest:
                        raise IntegrityError(
                            "step input does not reference an earlier committed artifact: "
                            + path
                        )
                    self.validate_artifact_ref(run_id, artifact_ref)
                continue
            if event_type != "step_finished":
                continue
            for artifact_ref in event.get("artifact_refs", []):
                path = artifact_ref["path"]
                digest = artifact_ref["sha256"]
                if path in committed and committed[path] != digest:
                    raise IntegrityError(
                        f"artifact path is recorded with conflicting hashes: {path}"
                    )
                self.validate_artifact_ref(run_id, artifact_ref)
                committed[path] = digest
            diagnostics_ref = event.get("diagnostics_ref")
            # attempts/ contains optional diagnostics only. It may be pruned
            # without changing authoritative Workflow State, so load/status/
            # resume must not require the referenced directory to exist.

    def _attempt_diagnostic_path(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        relative_path: str,
    ) -> tuple:
        relative = _safe_posix_relative(relative_path, label="diagnostic path")
        if len(relative.parts) != 1:
            raise UnsafePathError(
                "diagnostic paths must be direct children of the attempt"
            )
        if relative.parts[0] == "staging":
            raise UnsafePathError(
                "diagnostic writers cannot write through the staging namespace"
            )
        layout = self.prepare_attempt(run_id, node_instance_id, attempt)
        destination = layout.attempt_dir.joinpath(*relative.parts)
        if destination.is_symlink():
            raise UnsafePathError("diagnostic destination cannot be a symlink")
        run_directory = self.run_dir(run_id)
        reference = destination.relative_to(run_directory).as_posix()
        return destination, reference

    def _write_attempt_bytes(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        relative_path: str,
        payload: bytes,
        *,
        replace: bool,
    ) -> str:
        destination, reference = self._attempt_diagnostic_path(
            run_id, node_instance_id, attempt, relative_path
        )
        with self._lock:
            if replace:
                self._atomic_write_bytes(destination, payload, replace=True)
            else:
                self._write_new_bytes(destination, payload)
        return reference

    def _validated_candidate_root(
        self,
        run_id: str,
        node_instance_id: str,
        attempt: int,
        candidate_root: PathLike,
    ) -> Path:
        layout = self.prepare_attempt(run_id, node_instance_id, attempt)
        supplied = Path(candidate_root)
        if supplied.is_symlink() or not supplied.is_dir():
            raise UnsafePathError("publication root is not a real candidate directory")
        resolved = supplied.resolve(strict=True)
        expected = layout.candidate_dir
        if expected.is_symlink() or not expected.is_dir():
            raise UnsafePathError("attempt candidate is missing or unsafe")
        if resolved != expected.resolve(strict=True):
            raise UnsafePathError(
                "publication root must be this attempt's private candidate"
            )
        return resolved

    @classmethod
    def _copy_tree_from_directory(
        cls, source_root: Path, destination_root: Path, excluded_top: set
    ) -> None:
        """Copy a tree using no-follow descriptors, rejecting special files."""

        if source_root.is_symlink() or not source_root.is_dir():
            raise UnsafePathError("staging root is missing or unsafe")
        source_resolved = source_root.resolve(strict=True)
        for current, directory_names, file_names, directory_fd in os.fwalk(
            str(source_resolved), topdown=True, follow_symlinks=False
        ):
            relative = Path(current).relative_to(source_resolved)
            depth = len(relative.parts)
            if depth == 0:
                directory_names[:] = [
                    name for name in directory_names if name not in excluded_top
                ]
                file_names = [
                    name for name in file_names if name not in excluded_top
                ]
            destination = cls._ensure_directory_chain(
                destination_root, relative.parts
            )
            safe_directories = []
            for name in directory_names:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise UnsafePathError(
                        f"staging contains a non-directory tree entry: {name}"
                    )
                safe_directories.append(name)
                cls._ensure_directory_chain(destination, (name,))
            directory_names[:] = safe_directories
            for name in sorted(file_names):
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafePathError(
                        f"staging contains a symlink or special file: {name}"
                    )
                source_flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    source_flags |= os.O_NOFOLLOW
                if hasattr(os, "O_NONBLOCK"):
                    source_flags |= os.O_NONBLOCK
                source_fd = os.open(name, source_flags, dir_fd=directory_fd)
                opened = os.fstat(source_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    os.close(source_fd)
                    raise UnsafePathError(
                        f"staging file changed type or identity while freezing: {name}"
                    )
                destination_path = destination / name
                destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    destination_flags |= os.O_NOFOLLOW
                destination_fd = os.open(
                    str(destination_path), destination_flags, 0o600
                )
                try:
                    cls._copy_fd_and_hash(source_fd, destination_fd)
                    os.fsync(destination_fd)
                finally:
                    os.close(destination_fd)
                    os.close(source_fd)

    @classmethod
    def _snapshot_tree(cls, root: Path, excluded_top: set) -> Dict[str, str]:
        root_fd = cls._open_directory_fd(root)
        result: Dict[str, str] = {}
        try:
            cls._snapshot_directory_fd(
                root_fd, PurePosixPath(), excluded_top, result
            )
        finally:
            os.close(root_fd)
        return dict(sorted(result.items()))

    @classmethod
    def _snapshot_directory_fd(
        cls,
        directory_fd: int,
        prefix: PurePosixPath,
        excluded_top: set,
        result: Dict[str, str],
    ) -> None:
        for name in sorted(os.listdir(directory_fd)):
            if not prefix.parts and name in excluded_top:
                continue
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = prefix / name
            if stat.S_ISDIR(metadata.st_mode):
                flags = os.O_RDONLY
                if hasattr(os, "O_DIRECTORY"):
                    flags |= os.O_DIRECTORY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                opened = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    os.close(child_fd)
                    raise UnsafePathError(
                        "candidate directory changed identity while snapshotting: "
                        + relative.as_posix()
                    )
                try:
                    cls._snapshot_directory_fd(
                        child_fd, relative, excluded_top, result
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafePathError(
                    "candidate contains a symlink or special file: "
                    + relative.as_posix()
                )
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            opened = os.fstat(file_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                os.close(file_fd)
                raise UnsafePathError(
                    "candidate file changed type or identity while snapshotting: "
                    + relative.as_posix()
                )
            try:
                result[relative.as_posix()] = cls._hash_fd(file_fd)
            finally:
                os.close(file_fd)

    @staticmethod
    def _seal_tree(root: Path, *, seal_root: bool = True) -> None:
        if root.is_symlink() or not root.is_dir():
            raise UnsafePathError("cannot seal an unsafe directory tree")
        directories = []
        for current, directory_names, file_names in os.walk(
            str(root), topdown=True, followlinks=False
        ):
            current_path = Path(current)
            directories.append(current_path)
            for name in directory_names:
                child = current_path / name
                if child.is_symlink() or not child.is_dir():
                    raise UnsafePathError(
                        "cannot seal a tree containing directory symlinks"
                    )
            for name in file_names:
                child = current_path / name
                if child.is_symlink() or not child.is_file():
                    raise UnsafePathError(
                        "cannot seal a tree containing symlinks or special files"
                    )
                os.chmod(child, 0o444, follow_symlinks=False)
        for directory in reversed(directories):
            _fsync_directory(directory)
            if seal_root or directory != root:
                os.chmod(directory, 0o555, follow_symlinks=False)

    @staticmethod
    def _remove_private_tree(root: Path) -> None:
        """Make a private temporary tree writable, then remove it."""

        RunStore._make_private_tree_writable(root)
        shutil.rmtree(str(root))

    @staticmethod
    def _make_private_tree_writable(root: Path) -> None:
        for current, directory_names, file_names in os.walk(
            str(root), topdown=False, followlinks=False
        ):
            current_path = Path(current)
            for name in file_names:
                path = current_path / name
                if not path.is_symlink():
                    try:
                        os.chmod(path, 0o600, follow_symlinks=False)
                    except OSError:
                        pass
            for name in directory_names:
                path = current_path / name
                if not path.is_symlink():
                    try:
                        os.chmod(path, 0o700, follow_symlinks=False)
                    except OSError:
                        pass
            try:
                os.chmod(current_path, 0o700, follow_symlinks=False)
            except OSError:
                pass

    @staticmethod
    def _open_directory_fd(path: Path) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise UnsafePathError("publication root fd is not a directory")
        return descriptor

    @classmethod
    def _open_regular_beneath(
        cls, root_fd: int, relative: PurePosixPath
    ) -> int:
        current_fd = os.dup(root_fd)
        try:
            for part in relative.parts[:-1]:
                flags = os.O_RDONLY
                if hasattr(os, "O_DIRECTORY"):
                    flags |= os.O_DIRECTORY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                next_fd = os.open(part, flags, dir_fd=current_fd)
                metadata = os.fstat(next_fd)
                if not stat.S_ISDIR(metadata.st_mode):
                    os.close(next_fd)
                    raise UnsafePathError(
                        "validated path traverses a non-directory component"
                    )
                os.close(current_fd)
                current_fd = next_fd
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            result = os.open(relative.parts[-1], flags, dir_fd=current_fd)
            metadata = os.fstat(result)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(result)
                raise UnsafePathError("validated publication source is not a file")
            return result
        finally:
            os.close(current_fd)

    @staticmethod
    def _hash_fd(source_fd: int) -> str:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    @classmethod
    def _copy_fd_and_hash(cls, source_fd: int, destination_fd: int) -> str:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise IntegrityError("short write while publishing artifact")
                view = view[written:]
        return "sha256:" + digest.hexdigest()

    @staticmethod
    def _assert_regular_file(path: Path, *, label: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise IntegrityError(f"{label} is missing or not a regular file")

    @staticmethod
    def _ensure_directory_chain(root: Path, parts: Iterable[str]) -> Path:
        """Create child directories one by one without traversing symlinks."""

        if root.is_symlink() or not root.is_dir():
            raise UnsafePathError(f"directory root is missing or unsafe: {root}")
        resolved_root = root.resolve(strict=True)
        current = root
        for part in parts:
            if not part or part in {".", ".."} or "/" in part or "\\" in part:
                raise UnsafePathError(f"unsafe directory segment: {part!r}")
            candidate = current / part
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if candidate.is_symlink() or not candidate.is_dir():
                raise UnsafePathError(f"directory component is unsafe: {candidate}")
            resolved = candidate.resolve(strict=True)
            if not _is_relative_to(resolved, resolved_root):
                raise UnsafePathError(f"directory component escapes root: {candidate}")
            current = candidate
        return current

    def _load_json_object(self, path: Path, label: str) -> JsonObject:
        self._assert_regular_file(path, label=label)
        try:
            value = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise IntegrityError(f"{label} is invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise IntegrityError(f"{label} must contain a JSON object")
        return value

    def _atomic_write_json(
        self, path: Path, value: Any, *, pretty: bool, replace: bool
    ) -> None:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self._atomic_write_bytes(path, payload, replace=replace)

    @staticmethod
    def _write_new_bytes(path: Path, payload: bytes) -> None:
        descriptor = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            view = memoryview(payload)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise IntegrityError(f"short write while creating {path.name}")
                view = view[count:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(path.parent)

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes, *, replace: bool) -> None:
        if not replace and (path.exists() or path.is_symlink()):
            raise FileExistsError(str(path))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if not replace and (path.exists() or path.is_symlink()):
                raise FileExistsError(str(path))
            os.replace(str(temporary), str(path))
            _fsync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()
