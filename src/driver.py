"""Configuration-first workflow driver."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from deepresearch_cli.config import (
    Artifact,
    CompiledStep,
    CompiledWorkflow,
    NodeRegistry,
    RunRequest,
    WorkflowSpec,
    compile_workflow,
)
from deepresearch_cli.harness import Harness
from deepresearch_cli.node_runner import NodeRunResult, NodeRunner
from deepresearch_cli.persistence import RunStore, sha256_file
from deepresearch_cli.progress import ProgressReporter
from deepresearch_cli.runtime_state import (
    InstanceState,
    RunProjection,
    current_state,
    fold_events,
    heavy_progress,
    step_instances,
)


class DriverError(RuntimeError):
    pass


class ManifestIntegrityError(DriverError):
    pass


@dataclass(frozen=True)
class ExecutionSessionConfig:
    harness: str = "hermes"
    harness_profile: Optional[str] = None
    harness_model: Optional[str] = None
    node_timeout_seconds: Optional[float] = 600.0
    harness_timeout_seconds: Optional[float] = 300.0
    max_concurrency: int = 4
    search_mcp_enabled: bool = True
    search_dir: Optional[Path] = None
    search_provider_python: Optional[str] = None
    search_provider_limit: int = 20
    camofox_fallback_enabled: bool = True
    camofox_home: Optional[Path] = None
    camofox_base_url: Optional[str] = None

    def __post_init__(self) -> None:
        if self.harness not in {
            "hermes", "codex", "claude-code", "openclaw", "stub"
        }:
            raise ValueError("unsupported harness: %s" % self.harness)
        if self.harness_model is not None and not self.harness_model.strip():
            raise ValueError("harness_model must be a non-empty string or None")
        if self.node_timeout_seconds is not None and (
            not math.isfinite(self.node_timeout_seconds) or self.node_timeout_seconds <= 0
        ):
            raise ValueError("node_timeout_seconds must be finite and positive")
        if self.harness_timeout_seconds is not None and (
            not math.isfinite(self.harness_timeout_seconds) or self.harness_timeout_seconds <= 0
        ):
            raise ValueError("harness_timeout_seconds must be finite and positive")
        if isinstance(self.max_concurrency, bool) or not isinstance(self.max_concurrency, int) or self.max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if not isinstance(self.search_mcp_enabled, bool):
            raise ValueError("search_mcp_enabled must be a boolean")
        if self.search_dir is not None and not isinstance(self.search_dir, Path):
            raise ValueError("search_dir must be a Path or None")
        if self.search_provider_python is not None and (
            not isinstance(self.search_provider_python, str) or not self.search_provider_python.strip()
        ):
            raise ValueError("search_provider_python must be a non-empty string or None")
        if isinstance(self.search_provider_limit, bool) or not isinstance(self.search_provider_limit, int) or not 1 <= self.search_provider_limit <= 50:
            raise ValueError("search_provider_limit must be an integer between 1 and 50")
        if not isinstance(self.camofox_fallback_enabled, bool):
            raise ValueError("camofox_fallback_enabled must be a boolean")
        if self.camofox_fallback_enabled and not self.search_mcp_enabled:
            raise ValueError(
                "Camofox code-level fallback requires the DeepResearch search MCP"
            )
        if self.camofox_home is not None and not isinstance(self.camofox_home, Path):
            raise ValueError("camofox_home must be a Path or None")
        for name, value in (
            ("camofox_base_url", self.camofox_base_url),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{name} must be a non-empty string or None")


@dataclass(frozen=True)
class LoadedWorkflow:
    request: RunRequest
    workflow: CompiledWorkflow
    registry: NodeRegistry


class WorkflowDriver:
    def __init__(
        self,
        store: RunStore,
        harness: Harness,
        session_config: ExecutionSessionConfig,
        *,
        output_dir: Optional[Path] = None,
        harness_metadata: Optional[Mapping[str, object]] = None,
        progress_reporter: Optional[ProgressReporter] = None,
    ) -> None:
        self.store = store
        self.harness = harness
        self.session_config = session_config
        self.output_dir = (output_dir or store.root.parent / "output").expanduser().resolve()
        self.harness_metadata = dict(harness_metadata or {})
        self.progress_reporter = progress_reporter

    @staticmethod
    def new_run_id() -> str:
        return "run-" + uuid.uuid4().hex[:20]

    def create_run(
        self,
        request: RunRequest,
        workflow_spec: WorkflowSpec,
        registry: NodeRegistry,
        *,
        run_id: Optional[str] = None,
        _execution_lock_held: bool = False,
    ) -> RunProjection:
        selected = run_id or self.new_run_id()
        if not _execution_lock_held:
            with self.store.execution_session(selected):
                return self.create_run(
                    request, workflow_spec, registry, run_id=selected,
                    _execution_lock_held=True,
                )
        workflow = compile_workflow(
            workflow_spec, registry, output_format=request.output_format
        )
        return self.create_compiled_run(
            request, workflow, registry, run_id=selected,
            _execution_lock_held=True,
        )

    def create_compiled_run(
        self,
        request: RunRequest,
        workflow: CompiledWorkflow,
        registry: NodeRegistry,
        *,
        run_id: Optional[str] = None,
        _execution_lock_held: bool = False,
    ) -> RunProjection:
        selected = run_id or self.new_run_id()
        if not _execution_lock_held:
            with self.store.execution_session(selected):
                return self.create_compiled_run(
                    request, workflow, registry, run_id=selected,
                    _execution_lock_held=True,
                )
        node_snapshot = registry.snapshot_for(step.node_id for step in workflow.steps)
        definitions = {"workflow": workflow.to_dict(), "nodes": node_snapshot}
        manifest = {
            "schema_version": "2",
            "runtime": "config-workflow",
            "run_id": selected,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "context": request.to_dict(),
            "workflow": workflow.to_dict(),
            "nodes": node_snapshot,
            "definition_hash": _fingerprint(definitions),
        }
        self.store.create_run(manifest, run_id=selected)
        return fold_events(selected, workflow, ())

    def load_workflow(self, run_id: str) -> LoadedWorkflow:
        loaded = self.store.load_run(run_id)
        manifest = loaded.manifest
        if manifest.get("schema_version") != "2" or manifest.get("runtime") != "config-workflow":
            raise ManifestIntegrityError("run is not a config-workflow run")
        definitions = {"workflow": manifest.get("workflow"), "nodes": manifest.get("nodes")}
        if _fingerprint(definitions) != manifest.get("definition_hash"):
            raise ManifestIntegrityError("workflow definition snapshot hash mismatch")
        request = RunRequest.from_dict(manifest["context"])
        workflow = CompiledWorkflow.from_dict(manifest["workflow"])
        registry = NodeRegistry.from_snapshot(manifest["nodes"])
        for step in workflow.steps:
            registry.get(step.node_id)
        return LoadedWorkflow(request, workflow, registry)

    def load_projection(self, run_id: str) -> RunProjection:
        loaded_run = self.store.load_run(run_id)
        loaded = self.load_workflow(run_id)
        return fold_events(run_id, loaded.workflow, loaded_run.events)

    async def drive(
        self,
        run_id: str,
        *,
        max_steps: Optional[int] = None,
        _execution_lock_held: bool = False,
    ) -> RunProjection:
        if not _execution_lock_held:
            with self.store.execution_session(run_id):
                return await self.drive(run_id, max_steps=max_steps, _execution_lock_held=True)
        remaining = max_steps
        self._report_workflow_progress(self.load_projection(run_id))
        while True:
            loaded = self.load_workflow(run_id)
            projection = self.load_projection(run_id)
            if projection.status != "running":
                if projection.status == "completed":
                    self._export_results(projection)
                self._report_workflow_progress(projection)
                return projection
            step = self._next_step(projection)
            if step is None:
                self._append_run_finished(run_id, projection, "completed")
                projection = self.load_projection(run_id)
                self._export_results(projection)
                self._report_workflow_progress(projection)
                return projection
            if remaining is not None and remaining <= 0:
                return projection

            scopes = self._expected_scopes(step, projection)
            if not scopes:
                raise DriverError(
                    f"internal scheduling error: selected zero-instance step {step.step_id}"
                )
            existing = {tuple(sorted(item.scope.items())): item for item in step_instances(projection, step.step_id)}
            dispatches = []
            for scope in scopes:
                if remaining is not None and len(dispatches) >= remaining:
                    break
                key = tuple(sorted(scope.items()))
                instance_id = _instance_id(run_id, step.step_id, scope)
                prior = existing.get(key)
                if prior is not None and prior.outcome == "succeeded":
                    continue
                if prior is not None and prior.outcome == "failed":
                    self._append_run_finished(run_id, projection, "failed", prior.error)
                    return self.load_projection(run_id)
                if prior is not None and prior.outcome is None:
                    self._append_finished(run_id, projection, prior, "interrupted", (), "execution session ended before attempt completion")
                    projection = self.load_projection(run_id)
                    prior = projection.instances[instance_id]
                attempt = (prior.attempt + 1) if prior is not None else 1
                inputs = self._select_inputs(step, scope, projection)
                self._append_started(run_id, projection, step, instance_id, scope, attempt, inputs)
                projection = self.load_projection(run_id)
                repair = (
                    {"source_attempt": prior.attempt, "error": prior.error}
                    if prior is not None and prior.outcome == "repairable"
                    else None
                )
                dispatches.append((instance_id, scope, attempt, inputs, repair))

            if not dispatches:
                continue
            self._report_workflow_progress(self.load_projection(run_id))
            if remaining is not None:
                remaining -= len(dispatches)

            node = loaded.registry.get(step.node_id)
            runner = NodeRunner(
                self.store,
                self.harness,
                timeout_seconds=(
                    None
                    if node.kind == "script"
                    else (
                        step.timeout_seconds
                        if step.timeout_seconds is not None
                        else self.session_config.node_timeout_seconds
                    )
                ),
                harness_metadata=self.harness_metadata,
            )
            semaphore = asyncio.Semaphore(self.session_config.max_concurrency)

            async def execute(item):
                instance_id, scope, attempt, inputs, repair = item
                async with semaphore:
                    result = await runner.run(
                        run_id=run_id,
                        request=loaded.request,
                        step_id=step.step_id,
                        instance_id=instance_id,
                        scope=scope,
                        attempt=attempt,
                        node=node,
                        inputs=inputs,
                        repair=repair,
                    )
                return item, result

            tasks = [asyncio.create_task(execute(item)) for item in dispatches]
            results = []
            for completed in asyncio.as_completed(tasks):
                item, result = await completed
                results.append((item, result))
                instance_id, scope, attempt, _, _ = item
                projection = self.load_projection(run_id)
                instance = projection.instances[instance_id]
                self._append_finished(
                    run_id, projection, instance, result.outcome, result.artifacts,
                    result.error, result.diagnostics_ref, result.validation_warnings,
                )
                if self.progress_reporter is not None:
                    for warning in result.validation_warnings:
                        self.progress_reporter.validation_warning(
                            instance.node_id, scope, attempt, warning
                        )
                    self.progress_reporter.node_attempt_finished(
                        instance.node_id, scope, attempt, result.outcome, result.error
                    )
                self._report_workflow_progress(self.load_projection(run_id))
            failed_result = next(
                (result for _, result in results if result.outcome == "failed"),
                None,
            )
            if failed_result is not None:
                projection = self.load_projection(run_id)
                self._append_run_finished(
                    run_id, projection, "failed", failed_result.error
                )
                projection = self.load_projection(run_id)
                self._report_workflow_progress(projection)
                return projection
            if remaining == 0:
                return self.load_projection(run_id)

    def _report_workflow_progress(self, projection: RunProjection) -> None:
        if self.progress_reporter is None:
            return
        progress = heavy_progress(projection)
        callback = getattr(self.progress_reporter, "workflow_progress", None)
        if progress is not None and callback is not None:
            callback(progress)

    async def resume(self, run_id: str, *, max_steps: Optional[int] = None, _execution_lock_held: bool = False) -> RunProjection:
        return await self.drive(run_id, max_steps=max_steps, _execution_lock_held=_execution_lock_held)

    def _next_step(self, projection: RunProjection) -> Optional[CompiledStep]:
        for step in projection.workflow.steps:
            scopes = self._expected_scopes(step, projection)
            if not scopes:
                continue
            values = step_instances(projection, step.step_id)
            expected = {tuple(sorted(item.items())) for item in scopes}
            succeeded = {
                tuple(sorted(item.scope.items()))
                for item in values
                if item.outcome == "succeeded"
            }
            if succeeded == expected:
                continue
            return step
        return None

    @staticmethod
    def _expected_scopes(step: CompiledStep, projection: RunProjection) -> Tuple[Mapping[str, str], ...]:
        driver = next(
            (
                item
                for item in step.bindings
                if item.mode == "each" and item.source_step_id is not None
            ),
            None,
        )
        if driver is None:
            return ({},)
        values = [
            artifact.scope for artifact in projection.artifacts
            if artifact.step_id == driver.source_step_id
            and artifact.artifact_type == driver.artifact_type
        ]
        unique = []
        seen = set()
        for scope in values:
            key = tuple(sorted(scope.items()))
            if key not in seen:
                seen.add(key)
                unique.append(dict(scope))
        return tuple(unique)

    def _select_inputs(
        self, step: CompiledStep, scope: Mapping[str, str], projection: RunProjection
    ) -> Tuple[Artifact, ...]:
        state = current_state(projection.artifacts)
        selected: List[Artifact] = []
        for binding in step.bindings:
            if binding.mode == "each":
                if binding.source_step_id is None:
                    continue
                values = [
                    item for item in projection.artifacts
                    if item.step_id == binding.source_step_id
                    and item.artifact_type == binding.artifact_type
                    and dict(item.scope) == dict(scope)
                ]
            elif binding.mode == "all":
                values = [
                    item for (artifact_type, _), item in state.items()
                    if artifact_type == binding.artifact_type
                ]
            else:
                exact = state.get((binding.artifact_type, tuple(sorted(scope.items()))))
                global_value = state.get((binding.artifact_type, ()))
                values = [exact or global_value] if exact or global_value else []
            if binding.required and not values:
                raise DriverError(
                    f"step {step.step_id} input {binding.port} has no available artifact"
                )
            for item in values:
                if item not in selected:
                    selected.append(item)
        return tuple(sorted(selected, key=lambda item: (item.artifact_type, tuple(sorted(item.scope.items())), item.path)))

    def _append_started(self, run_id, projection, step, instance_id, scope, attempt, inputs):
        self.store.append_event(run_id, {
            "type": "step_started", "seq": projection.last_event_seq + 1,
            "event_id": _event_id(run_id, f"start:{instance_id}:{attempt}"),
            "step_id": step.step_id, "node_id": step.node_id, "instance_id": instance_id,
            "scope": dict(scope), "attempt": attempt,
            "inputs": [item.to_dict() for item in inputs],
        })

    def _append_finished(
        self, run_id, projection, instance, outcome, artifacts, error=None,
        diagnostics_ref=None, warnings=(),
    ):
        self.store.append_event(run_id, {
            "type": "step_finished", "seq": projection.last_event_seq + 1,
            "event_id": _event_id(run_id, f"finish:{instance.instance_id}:{instance.attempt}:{outcome}"),
            "step_id": instance.step_id, "node_id": instance.node_id,
            "instance_id": instance.instance_id, "scope": dict(instance.scope),
            "attempt": instance.attempt, "outcome": outcome,
            "artifact_refs": [item.to_dict() for item in artifacts],
            "diagnostics_ref": diagnostics_ref, "error": error,
            "validation_warnings": list(warnings),
        })

    def _append_run_finished(self, run_id, projection, status, error=None):
        self.store.append_event(run_id, {
            "type": "run_finished", "seq": projection.last_event_seq + 1,
            "event_id": _event_id(run_id, f"run:{status}"),
            "status": status, "error": error,
        })

    def _export_results(self, projection: RunProjection) -> None:
        reports = [item for item in projection.artifacts if item.artifact_type == projection.workflow.result_type]
        if not reports:
            raise ManifestIntegrityError(
                f"completed run has no {projection.workflow.result_type} artifact"
            )
        primary_media = projection.workflow.result_media_type
        primary = next((item for item in reversed(reports) if item.media_type == primary_media), None)
        if primary is None:
            raise ManifestIntegrityError("completed run has no primary report format")
        self._export_artifact(projection.run_id, primary)
        if projection.workflow.output_format in {"html", "pdf", "docx"}:
            markdown = next(
                (
                    item
                    for item in reversed(projection.artifacts)
                    if item.artifact_type == "report"
                    and item.media_type == "text/markdown"
                ),
                None,
            )
            if markdown is None:
                if not projection.workflow.name.startswith("node-"):
                    raise ManifestIntegrityError(
                        f"{projection.workflow.output_format.upper()} result has no Markdown source report"
                    )
            else:
                self._export_artifact(projection.run_id, markdown)

    def _export_artifact(self, run_id: str, artifact: Artifact) -> Path:
        source = self.store.validate_artifact_ref(run_id, artifact.to_dict())
        destination = self.output_dir / run_id / Path(source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_symlink() or not destination.is_file() or sha256_file(destination) != artifact.sha256:
                raise DriverError(f"output already exists with different content: {destination}")
            return destination
        temporary = destination.with_name(f".{destination.name}.exporting-{uuid.uuid4().hex}")
        try:
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != artifact.sha256:
                raise DriverError("exported artifact digest mismatch")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination


def projection_summary(store: RunStore, projection: RunProjection, *, output_dir: Path) -> Mapping[str, Any]:
    result = None
    if projection.status == "completed":
        reports = [item for item in projection.artifacts if item.artifact_type == projection.workflow.result_type]
        media = projection.workflow.result_media_type
        primary = next((item for item in reversed(reports) if item.media_type == media), None)
        if primary is not None:
            result = {
                "type": (
                    "html_report"
                    if media == "text/html"
                    else "pdf_report"
                    if media == "application/pdf"
                    else "docx_report"
                    if media
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    else projection.workflow.result_type
                ),
                "path": str(output_dir / projection.run_id / Path(primary.path).name),
                "artifact_ref": primary.to_dict(),
            }
            if projection.workflow.output_format in {"html", "pdf", "docx"}:
                source = next(
                    (
                        item
                        for item in reversed(projection.artifacts)
                        if item.artifact_type == "report"
                        and item.media_type == "text/markdown"
                    ),
                    None,
                )
                if source is not None:
                    result["source_report_path"] = str(output_dir / projection.run_id / Path(source.path).name)
    finished = [item for item in projection.instances.values() if item.outcome == "succeeded"]
    active = [item for item in projection.instances.values() if item.outcome is None]
    failed = [item for item in projection.instances.values() if item.outcome == "failed"]
    summary = {
        "run_id": projection.run_id,
        "status": projection.status,
        "workflow": projection.workflow.name,
        "output_format": projection.workflow.output_format,
        "completed_steps": list(dict.fromkeys(item.step_id for item in finished)),
        "running_steps": list(dict.fromkeys(item.step_id for item in active)),
        "failed_steps": list(dict.fromkeys(item.step_id for item in failed)),
        "artifact_count": len(projection.artifacts),
        "result": result,
        "error": projection.error,
    }
    progress = heavy_progress(projection)
    if progress is not None:
        summary["progress"] = progress
    return summary


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _instance_id(run_id: str, step_id: str, scope: Mapping[str, str]) -> str:
    semantic = json.dumps([run_id, step_id, sorted(scope.items())], ensure_ascii=False)
    return f"{step_id}-{uuid.uuid5(uuid.NAMESPACE_URL, semantic).hex[:16]}"


def _event_id(run_id: str, semantic: str) -> str:
    return "evt-" + uuid.uuid5(uuid.NAMESPACE_URL, f"deepresearch:{run_id}:{semantic}").hex
