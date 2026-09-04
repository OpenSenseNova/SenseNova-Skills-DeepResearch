"""Minimal async client for ``codex app-server`` stdio JSON-RPC."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Any, Optional


class CodexAppServerError(RuntimeError):
    pass


NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class CodexAppServerClient:
    def __init__(
        self,
        command: str,
        *,
        profile: Optional[str] = None,
        notification_handler: Optional[NotificationHandler] = None,
        codex_home: Optional[str] = None,
    ) -> None:
        self.command = command
        self.profile = profile
        self.notification_handler = notification_handler
        self.codex_home = codex_home
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stderr_chunks: list[str] = []
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self.process is not None:
            return
        argv = [self.command]
        if self.profile:
            argv.extend(["--profile", self.profile])
        argv.extend(["app-server", "--listen", "stdio://"])
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **({"CODEX_HOME": self.codex_home} if self.codex_home else {})},
            start_new_session=True,
            limit=16_000_000,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())


    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "deepresearch_cli",
                    "title": "DeepResearch-CLI Codex ACP Bridge",
                    "version": "0.1.9",
                }
            },
        )
        await self.notify("initialized", {})
        return result

    async def request(
        self, method: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        if self.process is None or self.process.returncode is not None:
            raise CodexAppServerError("Codex App Server is not running")
        self._request_id += 1
        request_id = self._request_id
        self._diagnostic(f"request id={request_id} method={method}")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"method": method, "id": request_id, "params": params or {}}
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self, method: str, params: Optional[dict[str, Any]] = None
    ) -> None:
        await self._send({"method": method, "params": params or {}})

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise CodexAppServerError("Codex App Server stdin is unavailable")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            process.stdin.write(payload.encode("utf-8") + b"\n")
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        error: Optional[BaseException] = None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                method = message.get("method")
                if isinstance(message_id, int) and not isinstance(method, str):
                    future = self._pending.get(message_id)
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error_value = message["error"]
                        self._diagnostic(
                            "response_error "
                            + json.dumps(
                                {"id": message_id, "error": error_value},
                                ensure_ascii=False,
                            )[:4000]
                        )
                        detail = (
                            json.dumps(error_value, ensure_ascii=False)
                            if isinstance(error_value, (dict, list))
                            else str(error_value)
                        )
                        stderr = self.stderr[-2000:].strip()
                        if stderr:
                            detail = f"{detail}; app-server stderr: {stderr}"
                        future.set_exception(CodexAppServerError(detail))
                    else:
                        self._diagnostic(f"response_ok id={message_id}")
                        result = message.get("result")
                        future.set_result(result if isinstance(result, dict) else {})
                    continue
                if isinstance(method, str) and isinstance(message_id, int):
                    await self._answer_server_request(message_id, method)
                    continue
                if isinstance(method, str) and self.notification_handler is not None:
                    params = message.get("params")
                    if method in {"error", "turn/completed", "thread/tokenUsage/updated"} or method.startswith("item/"):
                        self._diagnostic(
                            "notification "
                            + json.dumps(
                                {"method": method, "params": params},
                                ensure_ascii=False,
                            )[:6000]
                        )
                    if method == "error":
                        self.stderr_chunks.append(
                            "APP_SERVER_EVENT error: "
                            + json.dumps(params, ensure_ascii=False)
                            + "\n"
                        )
                    await self.notification_handler(
                        method, params if isinstance(params, dict) else {}
                    )
        except BaseException as exc:
            error = exc
            raise
        finally:
            detail = (
                f"{type(error).__name__}: {error}"
                if error is not None
                else "Codex App Server closed its output"
            )
            diagnostics = self.stderr[-4000:].strip()
            if diagnostics:
                detail = f"{detail}: {diagnostics}"
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(CodexAppServerError(detail))

    async def _answer_server_request(self, request_id: int, method: str) -> None:
        if method in {
            "item/mcpToolCall/requestApproval",
            "item/tool/requestApproval",
        }:
            # Search MCP is an attempt-scoped, non-destructive server. Codex
            # must receive an explicit approval response or it leaves the
            # MCP call pending and eventually reports a generic Internal error.
            await self._send({"id": request_id, "result": {"decision": "accept"}})
            return
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            result: dict[str, Any] = {"decision": "decline"}
        else:
            result = {}
        await self._send({"id": request_id, "result": result})

    @staticmethod
    def _diagnostic(message: str) -> None:
        # The bridge stderr is persisted per execution session by the CLI.
        # Keep protocol diagnostics off stdout, which is the ACP wire.
        print("CODEX_APP_SERVER " + message, file=sys.stderr, flush=True)

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            self.stderr_chunks.append(chunk.decode("utf-8", errors="replace"))
            if sum(map(len, self.stderr_chunks)) > 2_000_000:
                self.stderr_chunks.pop(0)

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_chunks)

    async def close(self) -> None:
        process = self.process
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2.0)
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [task for task in (self._reader_task, self._stderr_task) if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.process = None
