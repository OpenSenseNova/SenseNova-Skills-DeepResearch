from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deepresearch_cli.harness import (
    AgentExecutionResult,
    AgentInvocation,
    HarnessError,
    PerAttemptHarness,
)


def _invocation(tmp_path: Path, invocation_id: str) -> AgentInvocation:
    workspace = tmp_path / invocation_id / "staging"
    workspace.mkdir(parents=True)
    return AgentInvocation(
        invocation_id=invocation_id,
        run_id="run-test",
        node_instance_id=invocation_id,
        node_type="Research",
        attempt=1,
        workspace=workspace,
        input_artifact_refs=[],
        resolved_input_artifacts=[],
        timeout_seconds=30,
        agent_context={"query": "test", "language": "en"},
        prompt="test",
    )


class FakeAttemptRuntime:
    def __init__(self, factory, invocation_id: str, native_id: int) -> None:
        self.factory = factory
        self.invocation_id = invocation_id
        self.native_id = native_id
        self.started = False
        self.invoke_entered = asyncio.Event()
        self.cancelled_ids = []
        self.close_count = 0

    async def start(self) -> None:
        self.factory.start_entered.add(self.invocation_id)
        self.factory.start_event.set()
        if self.invocation_id in self.factory.blocked_starts:
            await self.factory.release_start.wait()
        if self.invocation_id in self.factory.failed_starts:
            raise RuntimeError("injected startup failure")
        self.started = True
        self.factory.active += 1
        self.factory.peak_active = max(
            self.factory.peak_active, self.factory.active
        )
        if self.factory.active >= self.factory.release_at_active:
            self.factory.release_invoke.set()

    async def invoke(self, invocation: AgentInvocation) -> AgentExecutionResult:
        assert self.started
        assert invocation.invocation_id == self.invocation_id
        self.invoke_entered.set()
        await self.factory.release_invoke.wait()
        return AgentExecutionResult(
            status="succeeded",
            response_text="done",
            native_process_id=self.native_id,
            native_process_instance_id=f"process-{self.native_id}",
            native_session_id=f"session-{self.native_id}",
        )

    async def cancel(self, invocation_id: str) -> None:
        self.cancelled_ids.append(invocation_id)

    async def close(self) -> None:
        if self.close_count:
            return
        self.close_count = 1
        if self.started:
            self.factory.active -= 1


class FakeBackendFactory:
    def __init__(self, *, release_at_active: int = 1) -> None:
        self.release_at_active = release_at_active
        self.created = []
        self.active = 0
        self.peak_active = 0
        self.start_entered = set()
        self.start_event = asyncio.Event()
        self.release_start = asyncio.Event()
        self.release_invoke = asyncio.Event()
        self.blocked_starts = set()
        self.failed_starts = set()

    async def preflight(self):
        return {"harness": "fake", "ok": True}

    async def probe(self):
        return {"acp_initialize": "ok"}

    def create(self, invocation):
        runtime = FakeAttemptRuntime(
            self, invocation.invocation_id, 10_000 + len(self.created)
        )
        self.created.append(runtime)
        return runtime


def test_each_invoke_gets_a_distinct_runtime_and_process(tmp_path):
    async def exercise():
        factory = FakeBackendFactory(release_at_active=2)
        harness = PerAttemptHarness(factory)
        await harness.start()

        first, second = await asyncio.gather(
            harness.invoke(_invocation(tmp_path, "inv-1")),
            harness.invoke(_invocation(tmp_path, "inv-2")),
        )

        assert first.native_process_id != second.native_process_id
        assert first.native_process_instance_id != second.native_process_instance_id
        assert first.native_session_id != second.native_session_id
        assert factory.peak_active == 2
        assert [runtime.close_count for runtime in factory.created] == [1, 1]
        assert harness.active_invocation_ids == ()
        await harness.close()

    asyncio.run(exercise())


def test_per_attempt_harness_has_no_hidden_four_process_cap(tmp_path):
    async def exercise():
        factory = FakeBackendFactory(release_at_active=8)
        harness = PerAttemptHarness(factory)
        await harness.start()

        results = await asyncio.gather(
            *(
                harness.invoke(_invocation(tmp_path, f"inv-{index}"))
                for index in range(8)
            )
        )

        assert factory.peak_active == 8
        assert len({result.native_process_id for result in results}) == 8
        await harness.close()

    asyncio.run(exercise())


def test_runtime_is_registered_before_start_and_cancel_is_precise(tmp_path):
    async def exercise():
        factory = FakeBackendFactory()
        factory.blocked_starts.add("inv-blocked")
        harness = PerAttemptHarness(factory)
        await harness.start()
        task = asyncio.create_task(
            harness.invoke(_invocation(tmp_path, "inv-blocked"))
        )
        await factory.start_event.wait()

        assert harness.active_invocation_ids == ("inv-blocked",)
        await harness.cancel("inv-missing")
        await harness.cancel("inv-blocked")
        assert factory.created[0].cancelled_ids == ["inv-blocked"]

        factory.release_start.set()
        factory.release_invoke.set()
        assert (await task).status == "succeeded"
        await harness.close()

    asyncio.run(exercise())


def test_startup_failure_closes_runtime_and_clears_registration(tmp_path):
    async def exercise():
        factory = FakeBackendFactory()
        factory.failed_starts.add("inv-fail")
        harness = PerAttemptHarness(factory)
        await harness.start()

        with pytest.raises(RuntimeError, match="startup failure"):
            await harness.invoke(_invocation(tmp_path, "inv-fail"))

        assert factory.created[0].close_count == 1
        assert harness.active_invocation_ids == ()
        await harness.close()

    asyncio.run(exercise())


def test_close_cancels_and_reaps_all_active_runtimes(tmp_path):
    async def exercise():
        factory = FakeBackendFactory(release_at_active=99)
        harness = PerAttemptHarness(factory)
        await harness.start()
        tasks = [
            asyncio.create_task(harness.invoke(_invocation(tmp_path, "inv-a"))),
            asyncio.create_task(harness.invoke(_invocation(tmp_path, "inv-b"))),
        ]
        while len(factory.created) < 2 or not all(
            runtime.invoke_entered.is_set() for runtime in factory.created
        ):
            await asyncio.sleep(0)

        await harness.close()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert [runtime.cancelled_ids for runtime in factory.created] == [
            ["inv-a"],
            ["inv-b"],
        ]
        assert [runtime.close_count for runtime in factory.created] == [1, 1]
        assert factory.active == 0
        assert harness.active_invocation_ids == ()
        await harness.close()
        with pytest.raises(HarnessError, match="already been closed"):
            await harness.start()

    asyncio.run(exercise())


def test_preflight_and_probe_delegate_to_backend_factory(tmp_path):
    async def exercise():
        factory = FakeBackendFactory()
        harness = PerAttemptHarness(factory)
        assert await harness.preflight() == {"harness": "fake", "ok": True}
        with pytest.raises(HarnessError, match="has not been started"):
            await harness.probe()
        await harness.start()
        assert await harness.probe() == {"acp_initialize": "ok"}
        await harness.close()

    asyncio.run(exercise())
