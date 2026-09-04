"""ACP Agent implementation backed by a Codex App Server connection."""

from __future__ import annotations

import asyncio
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import acp
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    McpServerStdio,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    SetSessionModeResponse,
    SetSessionModelResponse,
    TextContentBlock,
    Usage,
)

from .app_server import CodexAppServerClient, CodexAppServerError
from .mapping import item_update, message_update, usage_from_notification


@dataclass
class _Session:
    session_id: str
    thread_id: str
    cwd: str
    model: Optional[str]
    allow_edits: bool = False
    active_turn_id: Optional[str] = None
    usage: Optional[Usage] = None
    mcp_enabled: bool = False


class CodexAcpBridgeAgent:
    def __init__(self, app_server: CodexAppServerClient, *, model: str | None = None):
        self.app_server = app_server
        self.model = model
        self.connection = None
        self.sessions: dict[str, _Session] = {}
        self._sessions_by_thread: dict[str, _Session] = {}
        self._turn_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}
        self.app_server.notification_handler = self._notification

    def on_connect(self, conn: Any) -> None:
        self.connection = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **_: Any,
    ) -> InitializeResponse:
        del client_capabilities, client_info
        if protocol_version != acp.PROTOCOL_VERSION:
            raise CodexAppServerError(
                "ACP protocol mismatch: "
                f"client={protocol_version}, bridge={acp.PROTOCOL_VERSION}"
            )
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                prompt_capabilities=PromptCapabilities(
                    audio=False, embedded_context=False, image=False
                )
            ),
            agent_info=Implementation(
                name="deepresearch-codex-acp",
                title="DeepResearch Codex ACP Bridge",
                version="0.1.9",
            ),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[Any] | None = None,
        **_: Any,
    ) -> NewSessionResponse:
        try:
            config = self._mcp_config(mcp_servers or [])
        except Exception:
            traceback.print_exc()
            raise
        params: dict[str, Any] = {
            "cwd": cwd,
            "approvalPolicy": "never",
            "ephemeral": True,
            "threadSource": "appServer",
        }
        if self.model:
            params["model"] = self.model
        # App Server reads project config at process/session boundaries, which
        # can race with the CLI creating the attempt workspace.  Send the ACP
        # MCP declaration directly as well; the project config remains a
        # compatibility fallback for Codex versions that ignore this field.
        if config:
            params["config"] = config
        try:
            result = await self.app_server.request("thread/start", params)
        except Exception:
            traceback.print_exc()
            raise
        thread = result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerError("thread/start did not return a thread id")
        session_id = "codex-acp-" + uuid.uuid4().hex
        session = _Session(session_id, thread_id, cwd, self.model, mcp_enabled=bool(config))
        self.sessions[session_id] = session
        self._sessions_by_thread[thread_id] = session
        return NewSessionResponse(session_id=session_id)

    async def set_session_mode(
        self, mode_id: str, session_id: str, **_: Any
    ) -> SetSessionModeResponse:
        session = self._session(session_id)
        if mode_id != "accept_edits":
            raise CodexAppServerError(f"unsupported ACP session mode: {mode_id}")
        session.allow_edits = True
        return SetSessionModeResponse()

    async def set_session_model(
        self, model_id: str, session_id: str, **_: Any
    ) -> SetSessionModelResponse:
        self._session(session_id).model = model_id
        return SetSessionModelResponse()

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **_: Any,
    ) -> PromptResponse:
        session = self._session(session_id)
        text = "\n".join(
            block.text for block in prompt if isinstance(block, TextContentBlock)
        )
        if not text:
            raise CodexAppServerError("Codex ACP bridge accepts text prompts only")
        sandbox_policy: dict[str, Any]
        if session.allow_edits:
            sandbox_policy = {
                "type": "workspaceWrite",
                "writableRoots": [session.cwd],
                # Search MCP may use the local coordinator and configured
                # provider network. Keep ordinary sessions restricted.
                "networkAccess": session.mcp_enabled,
            }
        else:
            sandbox_policy = {
                "type": "readOnly",
                "access": {"type": "fullAccess"},
            }
        params: dict[str, Any] = {
            "threadId": session.thread_id,
            "input": [{"type": "text", "text": text}],
            "cwd": session.cwd,
            "approvalPolicy": "never",
            "sandboxPolicy": sandbox_policy,
            "clientUserMessageId": message_id,
        }
        if session.model:
            params["model"] = session.model
        try:
            result = await self.app_server.request("turn/start", params)
        except Exception:
            traceback.print_exc()
            raise
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, Mapping) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppServerError("turn/start did not return a turn id")
        session.active_turn_id = turn_id
        completed = self._completed_turns.pop(turn_id, None)
        if completed is None:
            waiter = asyncio.get_running_loop().create_future()
            self._turn_waiters[turn_id] = waiter
            try:
                completed = await waiter
            finally:
                self._turn_waiters.pop(turn_id, None)
        session.active_turn_id = None
        turn_value = completed.get("turn")
        status = (
            str(turn_value.get("status"))
            if isinstance(turn_value, Mapping)
            else "failed"
        )
        if status == "completed":
            stop_reason = "end_turn"
        elif status == "interrupted":
            stop_reason = "cancelled"
        else:
            error = turn_value.get("error") if isinstance(turn_value, Mapping) else None
            raise CodexAppServerError(self._error_text(error))
        return PromptResponse(
            stop_reason=stop_reason,
            usage=session.usage,
            user_message_id=message_id,
        )

    async def cancel(self, session_id: str, **_: Any) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.active_turn_id is None:
            return
        await self.app_server.request(
            "turn/interrupt",
            {
                "threadId": session.thread_id,
                "turnId": session.active_turn_id,
            },
        )

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        session = (
            self._sessions_by_thread.get(thread_id)
            if isinstance(thread_id, str)
            else None
        )
        if session is not None and self.connection is not None:
            update = item_update(method, params)
            if update is None and method == "item/agentMessage/delta":
                update = message_update(params)
            if update is not None:
                await self.connection.session_update(
                    session_id=session.session_id, update=update
                )
            if method == "thread/tokenUsage/updated":
                usage = usage_from_notification(params)
                if usage is not None:
                    session.usage = usage
        if method == "turn/completed":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            if isinstance(turn_id, str):
                waiter = self._turn_waiters.get(turn_id)
                if waiter is not None and not waiter.done():
                    waiter.set_result(params)
                else:
                    self._completed_turns[turn_id] = params

    @staticmethod
    def _mcp_config(servers: list[Any]) -> dict[str, Any]:
        configured: dict[str, Any] = {}
        for server in servers:
            if not isinstance(server, McpServerStdio):
                raise CodexAppServerError(
                    "Codex ACP bridge currently supports stdio MCP servers only"
                )
            environment = {
                item.name: item.value for item in (server.env or [])
            }
            value: dict[str, Any] = {
                "command": server.command,
                "args": list(server.args or []),
                "enabled": True,
                "required": True,
                "startup_timeout_sec": 30,
                "tool_timeout_sec": 120,
            }
            if environment:
                value["env"] = environment
            configured[server.name] = value
        return {"mcp_servers": configured} if configured else {}

    def _session(self, session_id: str) -> _Session:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise CodexAppServerError(f"unknown ACP session: {session_id}") from exc

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str):
                return message
        return str(error or "Codex turn failed")

    async def load_session(self, **_: Any) -> None:
        raise CodexAppServerError("session/load is not supported")

    async def list_sessions(self, **_: Any) -> None:
        raise CodexAppServerError("session/list is not supported")

    async def fork_session(self, **_: Any) -> None:
        raise CodexAppServerError("session/fork is not supported")

    async def resume_session(self, **_: Any) -> None:
        raise CodexAppServerError("session/resume is not supported")

    async def close_session(self, session_id: str, **_: Any) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            self._sessions_by_thread.pop(session.thread_id, None)

    async def set_config_option(self, **_: Any) -> None:
        raise CodexAppServerError("session config options are not supported")

    async def authenticate(self, **_: Any) -> None:
        raise CodexAppServerError("ACP authentication is not supported")

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        del params
        raise CodexAppServerError(f"unsupported ACP extension method: {method}")

    async def ext_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        del method, params
