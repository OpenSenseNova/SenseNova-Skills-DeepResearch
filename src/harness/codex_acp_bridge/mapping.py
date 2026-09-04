"""Translate Codex App Server notifications into typed ACP updates."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from acp.schema import AgentMessageChunk, TextContentBlock, ToolCallProgress, ToolCallStart, Usage


_KINDS = {
    "commandExecution": "execute",
    "command_execution": "execute",
    "fileChange": "edit",
    "file_change": "edit",
    "mcpToolCall": "other",
    "mcp_tool_call": "other",
    "webSearch": "search",
    "web_search": "search",
    "reasoning": "think",
}


def _title(item: Mapping[str, Any], kind: str) -> str:
    value = (
        item.get("command")
        or item.get("tool")
        or item.get("server")
        or item.get("query")
        or item.get("path")
        or item.get("name")
        or kind
    )
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return str(value)


def item_update(
    method: str, params: Mapping[str, Any]
) -> Optional[ToolCallStart | ToolCallProgress]:
    if method not in {"item/started", "item/completed"}:
        return None
    item = params.get("item")
    if not isinstance(item, Mapping):
        return None
    item_type = str(item.get("type") or "other")
    if item_type in {"agentMessage", "agent_message", "userMessage", "reasoning"}:
        return None
    identifier = str(item.get("id") or "")
    if not identifier:
        return None
    kind = _KINDS.get(item_type, "other")
    title = _title(item, kind)
    if method == "item/started":
        return ToolCallStart(
            session_update="tool_call",
            tool_call_id=identifier,
            kind=kind,
            title=title,
            status="in_progress",
        )
    raw_status = str(item.get("status") or "completed")
    status = "failed" if raw_status in {"failed", "declined", "cancelled"} else "completed"
    return ToolCallProgress(
        session_update="tool_call_update",
        tool_call_id=identifier,
        kind=kind,
        title=title,
        status=status,
    )


def message_update(params: Mapping[str, Any]) -> Optional[AgentMessageChunk]:
    delta = params.get("delta")
    if not isinstance(delta, str) or not delta:
        return None
    return AgentMessageChunk(
        session_update="agent_message_chunk",
        content=TextContentBlock(type="text", text=delta),
    )


def usage_from_notification(params: Mapping[str, Any]) -> Optional[Usage]:
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, Mapping):
        return None
    last = token_usage.get("last")
    if not isinstance(last, Mapping):
        return None
    input_tokens = max(0, int(last.get("inputTokens") or 0))
    output_tokens = max(0, int(last.get("outputTokens") or 0))
    cached_tokens = max(0, int(last.get("cachedInputTokens") or 0))
    thought_tokens = max(0, int(last.get("reasoningOutputTokens") or 0))
    total_tokens = max(
        0,
        int(last.get("totalTokens") or input_tokens + output_tokens),
    )
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_read_tokens=cached_tokens,
        thought_tokens=thought_tokens,
    )
