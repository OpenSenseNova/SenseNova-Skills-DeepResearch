"""Run-level Harness that owns one disposable backend runtime per attempt."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .protocol import (
    AgentExecutionResult,
    AgentInvocation,
    AttemptRuntime,
    BackendFactory,
    HarnessError,
)


@dataclass(frozen=True)
class _ActiveAttempt:
    runtime: AttemptRuntime
    task: Optional[asyncio.Task[Any]]


class PerAttemptHarness:
    """Coordinate disposable native runtimes without pooling their processes."""

    def __init__(self, factory: BackendFactory, *, run_resource: Optional[object] = None) -> None:
        self.factory = factory
        self.run_resource = run_resource
        self._active: Dict[str, _ActiveAttempt] = {}
        self._state_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    @property
    def active_invocation_ids(self) -> tuple[str, ...]:
        return tuple(self._active)

    async def preflight(self) -> Mapping[str, Any]:
        return await self.factory.preflight()

    async def ensure_timeout(self, seconds: Optional[float]) -> Mapping[str, Any]:
        method = getattr(self.factory, "ensure_timeout", None)
        if method is None:
            return {"harness_timeout": "unsupported"}
        return await method(seconds)

    async def probe(self) -> Mapping[str, Any]:
        if not self._started or self._closed:
            raise HarnessError("per-attempt Harness has not been started")
        return await self.factory.probe()

    async def start(self) -> None:
        async with self._state_lock:
            if self._closed:
                raise HarnessError("per-attempt Harness has already been closed")
            self._started = True

    async def invoke(self, invocation: AgentInvocation) -> AgentExecutionResult:
        async with self._state_lock:
            if not self._started or self._closed:
                raise HarnessError("per-attempt Harness has not been started")
        if self.run_resource is not None and invocation.node_type == "research":
            await self.run_resource.ensure_started(invocation)  # type: ignore[attr-defined]
        async with self._state_lock:
            if not self._started or self._closed:
                raise HarnessError("per-attempt Harness has not been started")
            if invocation.invocation_id in self._active:
                raise HarnessError(
                    "duplicate active invocation id: %s" % invocation.invocation_id
                )
            runtime = self.factory.create(invocation)
            entry = _ActiveAttempt(runtime=runtime, task=asyncio.current_task())
            # Register before process startup so close/cancel can always find it.
            self._active[invocation.invocation_id] = entry

        try:
            await runtime.start()
            return await runtime.invoke(invocation)
        finally:
            try:
                await _shielded_close(runtime)
            finally:
                async with self._state_lock:
                    if self._active.get(invocation.invocation_id) is entry:
                        self._active.pop(invocation.invocation_id, None)

    async def cancel(self, invocation_id: str) -> None:
        async with self._state_lock:
            entry = self._active.get(invocation_id)
        if entry is not None:
            await entry.runtime.cancel(invocation_id)

    async def close(self) -> None:
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            active = tuple(self._active.items())

        if active:
            await asyncio.gather(
                *(
                    entry.runtime.cancel(invocation_id)
                    for invocation_id, entry in active
                ),
                return_exceptions=True,
            )

        current = asyncio.current_task()
        owner_tasks = {
            entry.task
            for _, entry in active
            if entry.task is not None
            and entry.task is not current
            and not entry.task.done()
        }
        for task in owner_tasks:
            task.cancel()
        if owner_tasks:
            await asyncio.gather(*owner_tasks, return_exceptions=True)

        # invoke() normally closes each runtime in its finally block. The
        # second idempotent close covers startup failures and unusual callers.
        if active:
            await asyncio.gather(
                *(_shielded_close(entry.runtime) for _, entry in active),
                return_exceptions=True,
            )
        async with self._state_lock:
            for invocation_id, entry in active:
                if self._active.get(invocation_id) is entry:
                    self._active.pop(invocation_id, None)
        if self.run_resource is not None:
            await self.run_resource.close()  # type: ignore[attr-defined]


async def _shielded_close(runtime: AttemptRuntime) -> None:
    cleanup = asyncio.create_task(runtime.close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await cleanup
        raise
