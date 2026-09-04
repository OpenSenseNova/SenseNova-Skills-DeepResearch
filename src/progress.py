from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol, TextIO, Tuple

from deepresearch_cli.harness.protocol import AgentInvocation


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-_]|\[[0-?]*[ -/]*[@-~])")
_TERMINAL_TOOL_STATUSES = {"completed", "failed", "cancelled", "rejected"}
_STATUS_MARKERS = {
    "completed": "✓",
    "failed": "✗",
    "cancelled": "!",
    "rejected": "!",
}


class ProgressReporter(Protocol):
    """Synchronous, best-effort view of Harness and node-attempt progress."""

    def invocation_started(
        self, invocation: AgentInvocation, *, backend: str = "Hermes"
    ) -> None: ...

    def acp_event(
        self, invocation: AgentInvocation, event: Mapping[str, Any]
    ) -> None: ...

    def invocation_finished(
        self, invocation: AgentInvocation, status: str, *, backend: str = "Hermes"
    ) -> None: ...

    def validation_warning(
        self,
        node_type: str,
        scope: Mapping[str, Any],
        attempt: int,
        warning: Mapping[str, Any],
    ) -> None: ...

    def node_attempt_finished(
        self,
        node_type: str,
        scope: Mapping[str, Any],
        attempt: int,
        outcome: str,
        error: Optional[str],
    ) -> None: ...

    def workflow_progress(self, progress: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class _ToolDisplay:
    kind: str
    title: str


class TerminalProgressReporter:
    """Render compact ACP progress without exposing tool result bodies."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self._started_at: Dict[str, float] = {}
        self._tools: Dict[Tuple[str, str], _ToolDisplay] = {}
        self._last_workflow_progress: Optional[Tuple[Any, ...]] = None

    def workflow_progress(self, progress: Mapping[str, Any]) -> None:
        active = tuple(str(item) for item in progress.get("active_items", ()))
        signature = (
            progress.get("percent"),
            progress.get("phase"),
            progress.get("completed_units"),
            progress.get("total_units"),
            active,
        )
        if signature == self._last_workflow_progress:
            return
        self._last_workflow_progress = signature
        percent = max(0, min(100, int(progress.get("percent", 0))))
        filled = round(percent * 16 / 100)
        bar = "█" * filled + "░" * (16 - filled)
        phase = self._clean(progress.get("phase_label") or "处理中", limit=80)
        completed = progress.get("completed_units")
        total = progress.get("total_units")
        units = (
            f" {completed}/{total}"
            if isinstance(completed, int) and isinstance(total, int) and total > 1
            else ""
        )
        active_text = (
            " · 正在处理 " + ", ".join(self._clean(item, limit=32) for item in active[:4])
            if active
            else ""
        )
        self._write(f"[heavy] [{bar}] {percent:>3}% · {phase}{units}{active_text}")

    def invocation_started(
        self, invocation: AgentInvocation, *, backend: str = "Hermes"
    ) -> None:
        self._started_at[invocation.invocation_id] = time.monotonic()
        self._drop_invocation_tools(invocation.invocation_id)
        self._write(f"[{self._label(invocation)}] {backend} started")

    def acp_event(
        self, invocation: AgentInvocation, event: Mapping[str, Any]
    ) -> None:
        update_type = event.get("sessionUpdate")
        if update_type == "tool_call":
            tool_call_id = event.get("toolCallId")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                return
            kind = self._clean(event.get("kind") or "tool", limit=32)
            title = self._clean(event.get("title") or kind, limit=180)
            kind_prefix = kind.lower() + ":"
            if title.lower().startswith(kind_prefix):
                title = title[len(kind_prefix) :].lstrip()
            tool = _ToolDisplay(kind=kind, title=title)
            self._tools[(invocation.invocation_id, tool_call_id)] = tool
            self._write(
                f"[{self._label(invocation)}] → {kind} {title}"
                if title != kind
                else f"[{self._label(invocation)}] → {kind}"
            )
            return

        if update_type != "tool_call_update":
            return
        tool_call_id = event.get("toolCallId")
        status = event.get("status")
        if (
            not isinstance(tool_call_id, str)
            or not isinstance(status, str)
            or status not in _TERMINAL_TOOL_STATUSES
        ):
            return
        key = (invocation.invocation_id, tool_call_id)
        tool = self._tools.pop(key, None)
        kind = tool.kind if tool is not None else self._clean(
            event.get("kind") or "tool", limit=32
        )
        title = tool.title if tool is not None else ""
        detail = f" · {title}" if title and title != kind else ""
        marker = _STATUS_MARKERS[status]
        self._write(
            f"[{self._label(invocation)}] {marker} {kind} {status}{detail}"
        )

    def invocation_finished(
        self, invocation: AgentInvocation, status: str, *, backend: str = "Hermes"
    ) -> None:
        started_at = self._started_at.pop(invocation.invocation_id, None)
        elapsed = (
            f" · {time.monotonic() - started_at:.1f}s"
            if started_at is not None
            else ""
        )
        self._drop_invocation_tools(invocation.invocation_id)
        harness_status = "returned" if status == "succeeded" else status
        self._write(
            f"[{self._label(invocation)}] {backend} {harness_status}{elapsed}"
        )

    def validation_warning(
        self,
        node_type: str,
        scope: Mapping[str, Any],
        attempt: int,
        warning: Mapping[str, Any],
    ) -> None:
        label = self._node_label(node_type, scope, attempt)
        rule = self._clean(warning.get("rule") or "validation", limit=32)
        message = self._clean(warning.get("message") or "quality check warning", limit=240)
        self._write(f"[{label}] ! Validation warning {rule} · {message}")

    def node_attempt_finished(
        self,
        node_type: str,
        scope: Mapping[str, Any],
        attempt: int,
        outcome: str,
        error: Optional[str],
    ) -> None:
        label = self._node_label(node_type, scope, attempt)
        if outcome == "succeeded":
            self._write(f"[{label}] Node succeeded")
            return
        if outcome == "repairable":
            status = "Node needs artifact repair"
        elif outcome == "retryable":
            status = "Node timed out; retrying with a fresh session"
        elif outcome == "interrupted":
            status = "Node interrupted"
        else:
            status = "Node failed"
        detail = self._clean(error, limit=240) if error else ""
        self._write(f"[{label}] {status}" + (f" · {detail}" if detail else ""))

    def _drop_invocation_tools(self, invocation_id: str) -> None:
        for key in [key for key in self._tools if key[0] == invocation_id]:
            self._tools.pop(key, None)

    def _label(self, invocation: AgentInvocation) -> str:
        scope = invocation.agent_context.get("scope", invocation.agent_context)
        if not isinstance(scope, Mapping):
            scope = invocation.agent_context
        return self._node_label(
            invocation.node_type,
            scope,
            invocation.attempt,
        )

    def _node_label(
        self, node_type: str, scope: Mapping[str, Any], attempt: int
    ) -> str:
        scope_value = next(
            (
                scope.get(key)
                for key in (
                    "dimension-id",
                    "content-unit-id",
                    "dimension_id",
                    "content_unit_id",
                    "section_id",
                )
                if scope.get(key)
            ),
            None,
        )
        node = self._clean(node_type, limit=64)
        if scope_value is not None:
            node += ":" + self._clean(scope_value, limit=64)
        return f"{node}#{attempt}"

    @staticmethod
    def _clean(value: Any, *, limit: int) -> str:
        text = _ANSI_ESCAPE_RE.sub("", str(value))
        text = " ".join(text.split())
        text = "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _write(self, text: str) -> None:
        print(text, file=self.stream, flush=True)
