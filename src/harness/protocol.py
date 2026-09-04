from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentInvocation:
    # Harness control envelope. These fields are never rendered implicitly.
    invocation_id: str
    run_id: str
    node_instance_id: str
    node_type: str
    attempt: int
    workspace: Path
    input_artifact_refs: List[Mapping[str, Any]]
    resolved_input_artifacts: List[Mapping[str, Any]]
    timeout_seconds: Optional[float]
    # Model-visible projection. Only prompt is sent to the selected backend;
    # agent_context is
    # retained as an inspectable record of the fields rendered into it. A
    # repair attempt may additionally contain the reserved ``repair`` object.
    agent_context: Mapping[str, Any]
    prompt: str
    # Agent nodes may edit only their attempt staging cwd to write typed
    # business Artifacts.
    allow_workspace_edits: bool = False


@dataclass(frozen=True)
class AgentExecutionResult:
    status: str
    response_text: str = ""
    native_process_id: Optional[int] = None
    native_process_instance_id: Optional[str] = None
    native_process_stderr_path: Optional[str] = None
    native_session_id: Optional[str] = None
    stop_reason: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    stderr: str = ""
    usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    failure_kind: Optional[str] = None


class Harness(Protocol):
    async def preflight(self) -> Mapping[str, Any]: ...

    async def start(self) -> None: ...

    async def invoke(self, invocation: AgentInvocation) -> AgentExecutionResult: ...

    async def cancel(self, invocation_id: str) -> None: ...

    async def close(self) -> None: ...


class AttemptRuntime(Protocol):
    """One disposable native agent process serving exactly one attempt."""

    async def start(self) -> None: ...

    async def invoke(self, invocation: AgentInvocation) -> AgentExecutionResult: ...

    async def cancel(self, invocation_id: str) -> None: ...

    async def close(self) -> None: ...


class BackendFactory(Protocol):
    """Reusable backend configuration that creates disposable runtimes."""

    async def preflight(self) -> Mapping[str, Any]: ...

    async def ensure_timeout(self, seconds: Optional[float]) -> Mapping[str, Any]: ...

    async def probe(self) -> Mapping[str, Any]: ...

    def create(self, invocation: AgentInvocation) -> AttemptRuntime: ...
