"""Small, backend-neutral projections for ACP progress events."""

from __future__ import annotations

from typing import Any, Mapping, Optional


_TOOL_FIELDS = (
    "sessionUpdate",
    "toolCallId",
    "kind",
    "title",
    "status",
)


def project_tool_event(event: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Return the bounded progress view shared by terminal and web reporters."""
    if event.get("sessionUpdate") not in {"tool_call", "tool_call_update"}:
        return None
    projected: dict[str, Any] = {}
    limits = {
        "sessionUpdate": 40,
        "toolCallId": 200,
        "kind": 80,
        "title": 1_000,
        "status": 40,
    }
    for key in _TOOL_FIELDS:
        value = event.get(key)
        if isinstance(value, str):
            projected[key] = value[: limits[key]]
    return projected
