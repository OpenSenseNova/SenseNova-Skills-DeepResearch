"""Recording ACP client shared by Hermes and bridged agent backends."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, DefaultDict, Dict, List, Mapping, Optional

from acp.connection import StreamDirection, StreamEvent
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from ..protocol import HarnessError
from .events import project_tool_event


class RecordingAcpClient:
    def __init__(
        self,
        *,
        raw_observer_enabled: bool = False,
        event_observer: Optional[
            Callable[[str, Mapping[str, Any]], None]
        ] = None,
    ) -> None:
        self.connection = None
        self.events: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.messages: DefaultDict[str, List[str]] = defaultdict(list)
        self._raw_observer_enabled = raw_observer_enabled
        self._event_observer = event_observer

    def on_connect(self, conn: Any) -> None:
        self.connection = conn

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        if self._raw_observer_enabled:
            return
        if hasattr(update, "model_dump"):
            event = update.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(update, dict):
            event = dict(update)
        else:
            event = {"repr": repr(update)}
        self._record_update(session_id, event)

    def observe_stream(self, stream_event: StreamEvent) -> None:
        """Record incoming session updates synchronously in wire order."""
        if stream_event.direction != StreamDirection.INCOMING:
            return
        message = stream_event.message
        if message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        update = params.get("update")
        if not isinstance(session_id, str) or not isinstance(update, dict):
            return
        if not isinstance(update.get("sessionUpdate"), str):
            return
        self._record_update(session_id, dict(update))

    def _record_update(self, session_id: str, event: Dict[str, Any]) -> None:
        self.events[session_id].append(event)
        if event.get("sessionUpdate") == "agent_message_chunk":
            content = event.get("content")
            if (
                isinstance(content, dict)
                and content.get("type") == "text"
                and isinstance(content.get("text"), str)
            ):
                self.messages[session_id].append(content["text"])
        projection = project_tool_event(event)
        if self._event_observer is not None and projection is not None:
            try:
                self._event_observer(session_id, projection)
            except Exception:
                pass

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,
        tool_call: Any,
        **_: Any,
    ) -> RequestPermissionResponse:
        del session_id, tool_call
        reject_once = next(
            (option for option in options if option.kind == "reject_once"), None
        )
        if reject_once is not None:
            return RequestPermissionResponse(
                outcome=AllowedOutcome(
                    outcome="selected", option_id=reject_once.option_id
                )
            )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def write_text_file(self, **_: Any) -> None:
        raise HarnessError("ACP client-side file writes are disabled")

    async def read_text_file(self, **_: Any) -> None:
        raise HarnessError("ACP client-side file reads are not advertised")

    async def create_terminal(self, **_: Any) -> None:
        raise HarnessError("ACP client-side terminals are disabled")

    async def terminal_output(self, **_: Any) -> None:
        raise HarnessError("ACP client-side terminals are disabled")

    async def release_terminal(self, **_: Any) -> None:
        raise HarnessError("ACP client-side terminals are disabled")

    async def wait_for_terminal_exit(self, **_: Any) -> None:
        raise HarnessError("ACP client-side terminals are disabled")

    async def kill_terminal(self, **_: Any) -> None:
        raise HarnessError("ACP client-side terminals are disabled")

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        del params
        raise HarnessError(f"unsupported ACP extension method: {method}")

    async def ext_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        del method, params

    def reset_session(self, session_id: str) -> None:
        self.events[session_id].clear()
        self.messages[session_id].clear()
