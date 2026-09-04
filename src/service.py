"""Execution-session lifecycle for configuration-first CLI commands."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from deepresearch_cli.config import (
    Artifact,
    CompiledStep,
    CompiledWorkflow,
    InputBinding,
    NodeRegistry,
    RunRequest,
    load_workflow_spec,
)
from deepresearch_cli.driver import ExecutionSessionConfig, WorkflowDriver
from deepresearch_cli.harness import PerAttemptHarness
from deepresearch_cli.harness.registry import build_backend_factory
from deepresearch_cli.harness.hermes_acp import resolve_hermes_profile_home
from deepresearch_cli.harness.search_coordinator import SearchCoordinatorManager
from deepresearch_cli.persistence import RunStore
from deepresearch_cli.progress import ProgressReporter
from deepresearch_cli.runtime_state import RunProjection


@dataclass
class WorkflowService:
    runs_dir: Path
    config: ExecutionSessionConfig
    output_dir: Optional[Path] = None
    node_dirs: Sequence[Path] = field(default_factory=tuple)
    harness_command: Optional[str] = None
    # Backward-compatible constructor hook used by existing embedders/tests.
    hermes_command: Optional[str] = None
    progress_reporter: Optional[ProgressReporter] = None

    def __post_init__(self) -> None:
        self.runs_dir = self.runs_dir.expanduser().resolve()
        self.output_dir = (self.output_dir or Path("./output")).expanduser().resolve()
        self.node_dirs = tuple(Path(item).expanduser().resolve() for item in self.node_dirs)
        self.store = RunStore(self.runs_dir)

    def registry(self) -> NodeRegistry:
        return NodeRegistry.load(self.node_dirs)

    def _build_harness(self, run_id: Optional[str] = None) -> PerAttemptHarness:
        coordinator = None
        if self.config.search_mcp_enabled and run_id is not None:
            profile_env_file = None
            if self.config.harness == "hermes":
                candidate = resolve_hermes_profile_home(
                    self.config.harness_profile
                ) / ".env"
                if candidate.is_file():
                    profile_env_file = candidate
            coordinator = SearchCoordinatorManager(
                runs_dir=self.runs_dir,
                run_id=run_id,
                search_dir=self.config.search_dir,
                provider_python=self.config.search_provider_python,
                provider_limit=self.config.search_provider_limit,
                max_workers=max(8, self.config.max_concurrency),
                profile_env_file=profile_env_file,
            )
        factory = build_backend_factory(
            self.config.harness,
            workspace=self.runs_dir,
            command=self.harness_command or self.hermes_command,
            profile=self.config.harness_profile,
            model=self.config.harness_model,
            progress_reporter=self.progress_reporter,
            search_mcp_enabled=self.config.search_mcp_enabled,
            search_dir=self.config.search_dir,
            search_provider_python=self.config.search_provider_python,
            search_provider_limit=self.config.search_provider_limit,
            search_coordinator=coordinator,
            camofox_fallback_enabled=self.config.camofox_fallback_enabled,
            camofox_home=self.config.camofox_home,
            camofox_base_url=self.config.camofox_base_url,
        )
        return PerAttemptHarness(factory, run_resource=coordinator)

    def _harness(self) -> PerAttemptHarness:
        """Backward-compatible non-run-scoped harness hook used by embedders."""

        return self._build_harness()

    def _harness_for_run(self, run_id: str) -> PerAttemptHarness:
        # Existing embedders/tests replace the no-argument hook on an instance.
        # Respect that customization; production instances receive P3 state.
        if "_harness" in self.__dict__:
            return self._harness()
        return self._build_harness(run_id)

    async def _ensure_harness_timeout(self, harness) -> Mapping[str, object]:
        method = getattr(harness, "ensure_timeout", None)
        if method is None:
            return {"harness_timeout": "unsupported"}
        return await method(self.config.harness_timeout_seconds)

    async def doctor(self) -> Mapping[str, object]:
        # Load every built-in node before a paid model invocation starts.
        registry = self.registry()
        harness = self._harness()
        timeout_report = await self._ensure_harness_timeout(harness)
        report = {**timeout_report, **dict(await harness.preflight())}
        try:
            await harness.start()
            probe = await harness.probe()
        finally:
            await _shielded_close(harness)
        return {**dict(report), **dict(probe), "node_count": len(registry.list())}

    async def run(
        self,
        request: RunRequest,
        *,
        workflow_path: Optional[Path] = None,
        max_steps: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> RunProjection:
        workflow = load_workflow_spec(
            path=workflow_path,
            mode=None if workflow_path is not None else request.mode,
        )
        registry = self.registry()
        selected = run_id or WorkflowDriver.new_run_id()
        with self.store.execution_session(selected):
            harness = self._harness_for_run(selected)
            timeout_report = await self._ensure_harness_timeout(harness)
            preflight = {**timeout_report, **dict(await harness.preflight())}
            await harness.start()
            try:
                await harness.probe()
                driver = self._driver(harness, preflight)
                projection = driver.create_run(
                    request, workflow, registry, run_id=selected,
                    _execution_lock_held=True,
                )
                return await driver.drive(
                    projection.run_id, max_steps=max_steps,
                    _execution_lock_held=True,
                )
            finally:
                await _shielded_close(harness)

    async def resume(self, run_id: str, *, max_steps: Optional[int] = None) -> RunProjection:
        with self.store.execution_session(run_id):
            inert = self._driver(_StatusOnlyHarness())
            current = inert.load_projection(run_id)
            if current.status != "running":
                if current.status == "completed":
                    inert._export_results(current)
                return current
            harness = self._harness_for_run(run_id)
            timeout_report = await self._ensure_harness_timeout(harness)
            preflight = {**timeout_report, **dict(await harness.preflight())}
            await harness.start()
            try:
                await harness.probe()
                return await self._driver(harness, preflight).resume(
                    run_id, max_steps=max_steps, _execution_lock_held=True
                )
            finally:
                await _shielded_close(harness)

    async def run_node(
        self,
        node_id: str,
        request: RunRequest,
        *,
        inputs: Mapping[str, Path],
    ) -> RunProjection:
        registry = self.registry()
        node = registry.get(node_id)
        unknown = set(inputs) - {item.name for item in node.inputs}
        if unknown:
            raise ValueError("unknown input ports: " + ", ".join(sorted(unknown)))
        missing = [
            port.name
            for port in node.inputs
            if port.required and port.name not in inputs
        ]
        if missing:
            raise ValueError("missing required input ports: " + ", ".join(missing))
        normalized_inputs = {}
        for port_name, source in inputs.items():
            requested = Path(source).expanduser()
            if requested.is_symlink() or not requested.is_file():
                raise ValueError(f"input file not found or unsafe: {requested}")
            normalized_inputs[port_name] = requested.resolve()
        inputs = normalized_inputs
        bindings = tuple(
            InputBinding(
                item.name,
                "all" if item.mode == "all" else "one",
                None,
                item.artifact_type,
                item.required,
            )
            for item in node.inputs
        )
        primary_media = next(
            (item.media_type for item in node.outputs if item.primary),
            node.outputs[0].media_type,
        )
        output_format = {
            "text/html": "html",
            "application/pdf": "pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        }.get(primary_media, "markdown")
        request = RunRequest(
            request.query, request.language, request.mode, output_format,
            request.report_format,
        )
        workflow = CompiledWorkflow(
            name=f"node-{node_id}",
            steps=(CompiledStep(node_id, node_id, bindings),),
            result_type=next((item.artifact_type for item in node.outputs if item.primary), node.outputs[0].artifact_type),
            result_media_type=next((item.media_type for item in node.outputs if item.primary), node.outputs[0].media_type),
            output_format=output_format,
        )
        selected = WorkflowDriver.new_run_id()
        with self.store.execution_session(selected):
            if node.kind == "script":
                driver = self._driver(_StatusOnlyHarness())
                projection = driver.create_compiled_run(
                    request,
                    workflow,
                    registry,
                    run_id=selected,
                    _execution_lock_held=True,
                )
                projection = self._import_node_inputs(projection, node, inputs)
                return await driver.drive(selected, _execution_lock_held=True)
            harness = self._harness_for_run(selected)
            timeout_report = await self._ensure_harness_timeout(harness)
            preflight = {**timeout_report, **dict(await harness.preflight())}
            await harness.start()
            try:
                await harness.probe()
                driver = self._driver(harness, preflight)
                projection = driver.create_compiled_run(
                    request, workflow, registry, run_id=selected,
                    _execution_lock_held=True,
                )
                projection = self._import_node_inputs(projection, node, inputs)
                return await driver.drive(selected, _execution_lock_held=True)
            finally:
                await _shielded_close(harness)

    def _import_node_inputs(self, projection, node, inputs):
        if not inputs:
            return projection
        instance_id = "inputs-" + uuid.uuid4().hex[:16]
        attempt = 1
        self.store.append_event(projection.run_id, {
            "type": "step_started", "seq": projection.last_event_seq + 1,
            "event_id": "evt-" + uuid.uuid4().hex,
            "step_id": "inputs", "node_id": "input", "instance_id": instance_id,
            "scope": {}, "attempt": attempt, "inputs": [],
        })
        layout = self.store.prepare_attempt(projection.run_id, instance_id, attempt)
        metadata = []
        for port_name, source in inputs.items():
            port = node.input(port_name)
            destination = layout.staging_dir / f"{port_name}-{source.name}"
            shutil.copyfile(source, destination)
            metadata.append((destination.name, port))
        candidate = self.store.freeze_staging_candidate(projection.run_id, instance_id, attempt)
        self.store.seal_candidate(projection.run_id, instance_id, attempt, candidate)
        snapshot = self.store.snapshot_candidate(projection.run_id, instance_id, attempt, candidate)
        published = self.store.publish_artifacts(projection.run_id, instance_id, attempt, candidate, snapshot)
        artifacts = []
        for relative, port in metadata:
            base = next(item for item in published if item["path"].endswith("/" + relative))
            artifacts.append(Artifact(
                port=port.name, artifact_type=port.artifact_type,
                media_type=port.media_types[0], path=base["path"], sha256=base["sha256"],
                scope={}, mode="state", step_id="inputs", instance_id=instance_id,
            ))
        projection = self._driver(_StatusOnlyHarness()).load_projection(projection.run_id)
        self.store.append_event(projection.run_id, {
            "type": "step_finished", "seq": projection.last_event_seq + 1,
            "event_id": "evt-" + uuid.uuid4().hex,
            "step_id": "inputs", "node_id": "input", "instance_id": instance_id,
            "scope": {}, "attempt": attempt, "outcome": "succeeded",
            "artifact_refs": [item.to_dict() for item in artifacts],
            "diagnostics_ref": layout.diagnostics_ref, "error": None,
            "validation_warnings": [],
        })
        return self._driver(_StatusOnlyHarness()).load_projection(projection.run_id)

    def status(self, run_id: str) -> RunProjection:
        return self._driver(_StatusOnlyHarness()).load_projection(run_id)

    def _driver(self, harness, metadata=None) -> WorkflowDriver:
        return WorkflowDriver(
            self.store,
            harness,
            self.config,
            output_dir=self.output_dir,
            harness_metadata=metadata,
            progress_reporter=self.progress_reporter,
        )


class _StatusOnlyHarness:
    async def preflight(self):
        raise RuntimeError("status must not preflight a Harness")
    async def start(self):
        raise RuntimeError("status must not start a Harness")
    async def invoke(self, invocation):
        raise RuntimeError("status must not invoke a Harness")
    async def cancel(self, invocation_id):
        raise RuntimeError("status must not cancel a Harness")
    async def close(self):
        return None


async def _shielded_close(harness) -> None:
    cleanup = asyncio.create_task(harness.close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await cleanup
        raise
