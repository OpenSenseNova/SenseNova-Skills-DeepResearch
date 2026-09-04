from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from deepresearch_cli.config import Artifact, CompiledWorkflow


class ProjectionError(ValueError):
    pass


@dataclass(frozen=True)
class InstanceState:
    instance_id: str
    step_id: str
    node_id: str
    scope: Mapping[str, str]
    attempt: int
    outcome: Optional[str]
    inputs: Tuple[Artifact, ...]
    artifacts: Tuple[Artifact, ...] = ()
    error: Optional[str] = None


@dataclass(frozen=True)
class RunProjection:
    run_id: str
    status: str
    workflow: CompiledWorkflow
    instances: Mapping[str, InstanceState]
    instance_order: Tuple[str, ...]
    artifacts: Tuple[Artifact, ...]
    last_event_seq: int
    error: Optional[str] = None

    @property
    def completed_steps(self) -> Tuple[str, ...]:
        result = []
        for step in self.workflow.steps:
            values = [item for item in self.instances.values() if item.step_id == step.step_id]
            if values and all(item.outcome == "succeeded" for item in values):
                result.append(step.step_id)
        return tuple(result)


def fold_events(
    run_id: str,
    workflow: CompiledWorkflow,
    events: Sequence[Mapping[str, Any]],
) -> RunProjection:
    instances: Dict[str, InstanceState] = {}
    order: List[str] = []
    artifacts: List[Artifact] = []
    status = "running"
    error = None
    for expected_seq, event in enumerate(events, 1):
        if event.get("seq") != expected_seq:
            raise ProjectionError("journal sequence is not contiguous")
        kind = event.get("type")
        if kind == "step_started":
            instance_id = event["instance_id"]
            previous = instances.get(instance_id)
            attempt = event["attempt"]
            if previous is not None and attempt != previous.attempt + 1:
                raise ProjectionError("step attempt is not contiguous")
            if previous is None:
                order.append(instance_id)
            instances[instance_id] = InstanceState(
                instance_id=instance_id,
                step_id=event["step_id"],
                node_id=event["node_id"],
                scope=event.get("scope", {}),
                attempt=attempt,
                outcome=None,
                inputs=tuple(Artifact.from_dict(item) for item in event.get("inputs", [])),
            )
        elif kind == "step_finished":
            instance_id = event["instance_id"]
            current = instances.get(instance_id)
            if current is None or current.attempt != event["attempt"] or current.outcome is not None:
                raise ProjectionError("step_finished does not match an active attempt")
            published = tuple(Artifact.from_dict(item) for item in event.get("artifact_refs", []))
            instances[instance_id] = InstanceState(
                instance_id=current.instance_id,
                step_id=current.step_id,
                node_id=current.node_id,
                scope=current.scope,
                attempt=current.attempt,
                outcome=event["outcome"],
                inputs=current.inputs,
                artifacts=published,
                error=event.get("error"),
            )
            artifacts.extend(published)
        elif kind == "run_finished":
            status = event["status"]
            error = event.get("error")
        else:
            raise ProjectionError(f"unknown journal event: {kind}")
    return RunProjection(
        run_id=run_id,
        status=status,
        workflow=workflow,
        instances=instances,
        instance_order=tuple(order),
        artifacts=tuple(artifacts),
        last_event_seq=len(events),
        error=error,
    )


def current_state(artifacts: Iterable[Artifact]) -> Mapping[tuple[str, tuple], Artifact]:
    result: Dict[tuple[str, tuple], Artifact] = {}
    for artifact in artifacts:
        if artifact.mode != "state":
            continue
        key = (artifact.artifact_type, tuple(sorted(artifact.scope.items())))
        result[key] = artifact
    return result


def step_instances(projection: RunProjection, step_id: str) -> Tuple[InstanceState, ...]:
    return tuple(
        projection.instances[item]
        for item in projection.instance_order
        if projection.instances[item].step_id == step_id
    )


_HEAVY_STEP_WEIGHTS = {
    "scout": 4.0,
    "plan": 6.0,
    "research": 15.0,
    "review": 5.0,
    "perspective": 4.0,
    "supplement-planner": 3.0,
    "research-2": 10.0,
    "review-2": 4.0,
    "perspective-2": 4.0,
    "report-planner": 5.0,
    "report-writer": 20.0,
    "stitcher": 3.0,
    "final-review-diagnostic": 3.0,
    "final-repair": 2.0,
    "final-review-recheck": 2.0,
}

_HEAVY_PHASE_LABELS = {
    "scout": "扫描任务",
    "plan": "制定研究计划",
    "research": "第一轮资料研究",
    "review": "第一轮证据审核",
    "perspective": "第一轮观点审查",
    "supplement-planner": "规划补充研究",
    "research-2": "补充资料研究",
    "review-2": "补充证据审核",
    "perspective-2": "补充观点审查",
    "report-planner": "拆分写作任务",
    "report-writer": "分章节写作",
    "stitcher": "合并全文",
    "final-review": "最终审核",
    "final-review-diagnostic": "首次最终审核",
    "final-repair": "解析并定向修复章节",
    "final-review-recheck": "重新合并并复审终稿",
    "render": "生成 Markdown",
    "md-html": "生成 HTML",
    "md-pdf": "生成 PDF",
    "md-docx": "生成 Word",
}


def _heavy_weights(projection: RunProjection) -> Mapping[str, float]:
    step_ids = {step.step_id for step in projection.workflow.steps}
    derived = next(
        (item for item in ("md-html", "md-pdf", "md-docx") if item in step_ids),
        None,
    )
    result = {
        step.step_id: _HEAVY_STEP_WEIGHTS.get(step.step_id, 0.0)
        for step in projection.workflow.steps
    }
    result["render"] = 5.0 if derived else 10.0
    if derived:
        result[derived] = 5.0
    return result


def _expected_units(projection: RunProjection, step) -> int:
    binding = next(
        (
            item
            for item in step.bindings
            if item.mode == "each" and item.source_step_id is not None
        ),
        None,
    )
    if binding is None:
        return 1
    scopes = {
        tuple(sorted(item.scope.items()))
        for item in projection.artifacts
        if item.step_id == binding.source_step_id
        and item.artifact_type == binding.artifact_type
    }
    return len(scopes)


def heavy_progress(projection: RunProjection) -> Optional[Mapping[str, Any]]:
    """Return truthful, event-derived progress for the built-in heavy workflow."""
    if projection.workflow.name != "heavy":
        return None

    weights = _heavy_weights(projection)
    steps = tuple(projection.workflow.steps)
    started_indexes = [
        index
        for index, step in enumerate(steps)
        if step_instances(projection, step.step_id)
    ]
    furthest_started = max(started_indexes, default=-1)
    fractions: Dict[str, float] = {}
    unit_counts: Dict[str, Tuple[int, int]] = {}

    for index, step in enumerate(steps):
        expected = _expected_units(projection, step)
        values = step_instances(projection, step.step_id)
        succeeded = len([item for item in values if item.outcome == "succeeded"])
        if projection.status == "completed":
            fraction = 1.0
        elif expected == 0:
            fraction = 1.0 if index < furthest_started else 0.0
        else:
            fraction = min(1.0, succeeded / expected)
        fractions[step.step_id] = fraction
        unit_counts[step.step_id] = (succeeded, expected)

    total_weight = sum(weights.values()) or 100.0
    completed_weight = sum(
        weights.get(step.step_id, 0.0) * fractions[step.step_id]
        for step in steps
    )
    percent = round(100 * completed_weight / total_weight)
    if projection.status == "completed":
        percent = 100

    current = next(
        (
            step
            for step in steps
            if weights.get(step.step_id, 0.0) > 0
            and fractions[step.step_id] < 1.0
        ),
        None,
    )
    phase_id = (
        "completed"
        if projection.status == "completed"
        else "failed"
        if projection.status == "failed"
        else current.step_id
        if current is not None
        else "starting"
    )
    phase_label = (
        "已完成"
        if phase_id == "completed"
        else "执行失败"
        if phase_id == "failed"
        else "正在启动"
        if phase_id == "starting"
        else _HEAVY_PHASE_LABELS.get(phase_id, phase_id)
    )
    completed_units, total_units = (
        unit_counts.get(current.step_id, (0, 0)) if current is not None else (0, 0)
    )
    active_items = []
    if current is not None:
        for item in step_instances(projection, current.step_id):
            if item.outcome is not None:
                continue
            value = next(
                (
                    item.scope.get(key)
                    for key in (
                        "content-unit-id",
                        "dimension-id",
                        "content_unit_id",
                        "dimension_id",
                    )
                    if item.scope.get(key)
                ),
                None,
            )
            if value:
                active_items.append(str(value))

    writing_completed, writing_total = unit_counts.get("report-writer", (0, 0))
    return {
        "percent": percent,
        "phase": phase_id,
        "phase_label": phase_label,
        "completed_units": completed_units,
        "total_units": total_units,
        "active_items": active_items,
        "writing": {
            "completed": writing_completed,
            "total": writing_total,
        },
    }
