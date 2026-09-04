"""Build browser-friendly state exclusively from persisted run data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from deepresearch_cli.config import CompiledWorkflow
from deepresearch_cli.driver import projection_summary
from deepresearch_cli.persistence import RunStore
from deepresearch_cli.runtime_state import fold_events
from deepresearch_cli.search.metrics import build_search_metrics


_PIPELINE = (
    ("planning", "研究规划", {"scout", "plan"}),
    ("researching", "资料研究", {"research", "review", "perspective", "supplement-planner", "research-2", "review-2", "perspective-2"}),
    ("writing", "报告写作", {"report-planner", "report-writer"}),
    ("finalizing", "合并与审核", {"stitcher", "final-review", "final-review-diagnostic", "final-repair", "final-review-recheck"}),
    ("delivery", "格式交付", {"render", "md-html", "md-pdf", "md-docx"}),
)

_STEP_LABELS = {
    "scout": "扫描研究任务",
    "plan": "制定研究计划",
    "research": "收集与整理证据",
    "review": "审核研究证据",
    "perspective": "检查研究视角",
    "supplement-planner": "规划补充研究",
    "research-2": "执行补充研究",
    "review-2": "审核补充证据",
    "perspective-2": "复核补充视角",
    "report-planner": "规划报告结构",
    "report-writer": "撰写报告",
    "stitcher": "合并报告章节",
    "final-review": "最终审核",
    "final-review-diagnostic": "首次最终审核",
    "final-repair": "解析并定向修复章节",
    "final-review-recheck": "重新合并并复审终稿",
    "render": "整理 Markdown",
    "md-html": "生成 HTML",
    "md-pdf": "生成 PDF",
    "md-docx": "生成 Word",
}


def _generic_web_progress(projection) -> dict[str, Any]:
    """Build truthful progress for Quick/Normal and custom browser workflows."""
    steps = tuple(projection.workflow.steps)
    if not steps:
        percent = 100 if projection.status == "completed" else 0
        return {
            "percent": percent,
            "phase": "done" if percent == 100 else "starting",
            "phase_label": "已完成" if percent == 100 else "正在启动研究",
        }

    fractions: list[float] = []
    active_step = None
    next_step = None
    for step in steps:
        instances = [
            item for item in projection.instances.values()
            if item.step_id == step.step_id
        ]
        succeeded = sum(item.outcome == "succeeded" for item in instances)
        if projection.status == "completed" or (
            instances and succeeded == len(instances)
        ):
            fraction = 1.0
        elif instances:
            fraction = succeeded / len(instances)
        else:
            fraction = 0.0
        fractions.append(fraction)
        if active_step is None and any(item.outcome is None for item in instances):
            active_step = step.step_id
        if next_step is None and fraction < 1.0:
            next_step = step.step_id

    phase = "done" if projection.status == "completed" else active_step or next_step or "starting"
    return {
        "percent": round(sum(fractions) * 100 / len(fractions)),
        "phase": phase,
        "phase_label": (
            "已完成" if phase == "done"
            else _STEP_LABELS.get(phase, "正在处理研究任务")
        ),
    }


def _json_artifact(store: RunStore, run_id: str, artifact) -> Mapping[str, Any] | None:
    if artifact.media_type != "application/json":
        return None
    try:
        path = store.validate_artifact_ref(run_id, artifact.to_dict())
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _scope_id(scope: Mapping[str, str]) -> str | None:
    for key in ("dimension-id", "dimension_id", "content-unit-id", "content_unit_id"):
        if scope.get(key):
            return str(scope[key])
    return None


def build_run_snapshot(store: RunStore, run_id: str, *, output_dir: Path) -> dict[str, Any]:
    loaded = store.load_run(run_id)
    workflow = CompiledWorkflow.from_dict(loaded.manifest["workflow"])
    projection = fold_events(run_id, workflow, loaded.events)
    summary = dict(projection_summary(store, projection, output_dir=output_dir))
    context = dict(loaded.manifest.get("context", {}))

    plan: Mapping[str, Any] = {}
    outline: Mapping[str, Any] = {}
    evidence: dict[str, Mapping[str, Any]] = {}
    for artifact in projection.artifacts:
        value = _json_artifact(store, run_id, artifact)
        if value is None:
            continue
        if artifact.artifact_type == "research-plan":
            plan = value
        elif artifact.artifact_type == "report-outline":
            outline = value
        elif artifact.artifact_type == "evidence":
            dimension_id = str(value.get("dimension_id") or _scope_id(artifact.scope) or "")
            if dimension_id:
                evidence[dimension_id] = value

    all_sources: dict[str, Mapping[str, Any]] = {}
    claim_count = counter_count = 0
    for evidence_item in evidence.values():
        claims = (
            evidence_item.get("claims", [])
            if isinstance(evidence_item.get("claims", []), list)
            else []
        )
        sources = (
            evidence_item.get("sources", [])
            if isinstance(evidence_item.get("sources", []), list)
            else []
        )
        claim_count += len(claims)
        counter_count += sum(
            1
            for claim in claims
            if isinstance(claim, dict) and claim.get("polarity") == "refute"
        )
        for source in sources:
            if isinstance(source, dict):
                key = str(source.get("url") or source.get("id") or len(all_sources))
                all_sources[key] = source

    dimensions = []
    for item in plan.get("dimensions", []) if isinstance(plan.get("dimensions"), list) else []:
        if not isinstance(item, dict):
            continue
        dimension_id = str(item.get("id", ""))
        evidence_item = evidence.get(dimension_id, {})
        claims = evidence_item.get("claims", []) if isinstance(evidence_item.get("claims", []), list) else []
        sources = evidence_item.get("sources", []) if isinstance(evidence_item.get("sources", []), list) else []
        related = [instance for instance in projection.instances.values() if _scope_id(instance.scope) == dimension_id]
        active = any(instance.outcome is None for instance in related)
        failed = any(instance.outcome == "failed" for instance in related)
        status = "failed" if failed else "active" if active else "done" if evidence_item else "queued"
        dimensions.append({
            "id": dimension_id,
            "name": item.get("name", dimension_id),
            "description": item.get("description", ""),
            "questions": item.get("key_questions", []),
            "lenses": item.get("lenses", []),
            "status": status,
            "claims": len(claims),
            "sources": len(sources),
            "headline": evidence_item.get("headline", ""),
        })

    sections = []
    units = outline.get("content_units", []) if isinstance(outline.get("content_units"), list) else []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id", ""))
        related = [instance for instance in projection.instances.values() if _scope_id(instance.scope) == unit_id and instance.step_id == "report-writer"]
        status = "active" if any(x.outcome is None for x in related) else "done" if any(x.outcome == "succeeded" for x in related) else "queued"
        sections.append({"id": unit_id, "title": unit.get("title", unit_id), "type": unit.get("type", "narrative"), "status": status})

    completed = set(projection.completed_steps)
    running = {
        item.step_id for item in projection.instances.values()
        if item.outcome is None
    }
    failed = {
        item.step_id for item in projection.instances.values()
        if item.outcome == "failed"
    }
    workflow_steps = {step.step_id for step in workflow.steps}
    pipeline = []
    for phase_id, label, steps in _PIPELINE:
        present = steps & workflow_steps
        if not present:
            continue
        phase_status = "failed" if present & failed else "active" if present & running else "done" if present and present <= completed else "queued"
        if phase_status == "queued" and any(step in completed for step in steps):
            phase_status = "partial"
        pipeline.append({"id": phase_id, "label": label, "status": phase_status})
    if projection.status == "completed":
        for item in pipeline:
            item["status"] = "done"

    web_progress = summary.get("progress") or _generic_web_progress(projection)

    activity = []
    for event in reversed(loaded.events[-40:]):
        kind = event.get("type")
        if kind == "step_started":
            message = f"开始：{event.get('step_id')}"
        elif kind == "step_finished":
            message = f"{event.get('step_id')} · {event.get('outcome')}"
        else:
            message = f"运行{event.get('status', '')}"
        activity.append({"seq": event.get("seq"), "time": event.get("recorded_at"), "message": message, "scope": _scope_id(event.get("scope", {}))})

    quality = {"primary": 0, "secondary": 0, "tertiary": 0}
    for source in all_sources.values():
        level = source.get("quality")
        if level in quality:
            quality[level] += 1

    result = summary.get("result")
    if isinstance(result, dict):
        filename = Path(result["path"]).name
        result = {"type": result["type"], "filename": filename, "url": f"/api/runs/{run_id}/files/{filename}"}
        source_path = summary["result"].get("source_report_path")
        if source_path:
            source_name = Path(source_path).name
            result["source"] = {"filename": source_name, "url": f"/api/runs/{run_id}/files/{source_name}"}

    search_metrics = build_search_metrics(
        loaded.run_dir,
        evidence_urls=(
            str(source.get("url"))
            for source in all_sources.values()
            if source.get("url")
        ),
    )

    return {
        **summary,
        "progress": web_progress,
        "result": result,
        "query": context.get("query", ""),
        "language": context.get("language", "zh-CN"),
        "mode": context.get("mode", workflow.name),
        "report_format": context.get("report_format", "formal_report"),
        "created_at": loaded.manifest.get("created_at"),
        "last_event_seq": projection.last_event_seq,
        "pipeline": pipeline,
        "dimensions": dimensions,
        "sections": sections,
        "metrics": {"sources": len(all_sources), "claims": claim_count, "counter_claims": counter_count, "quality": quality},
        "search": search_metrics,
        "activity": activity,
    }
