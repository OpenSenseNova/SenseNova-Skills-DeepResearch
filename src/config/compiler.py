from __future__ import annotations

from collections import Counter

from .models import CompiledStep, CompiledWorkflow, InputBinding, NodeSpec, WorkflowSpec
from .registry import NodeRegistry


class WorkflowCompileError(ValueError):
    pass


_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def compile_workflow(
    workflow: WorkflowSpec,
    registry: NodeRegistry,
    *,
    output_format: str,
) -> CompiledWorkflow:
    node_ids = list(workflow.steps)
    if output_format in {"html", "pdf", "docx"} and workflow.result != "report":
        raise WorkflowCompileError(
            f"{output_format} output currently requires workflow result: report"
        )
    if output_format == "html":
        if not node_ids or node_ids[-1] != "md-html":
            node_ids.append("md-html")
        result_type = workflow.result
        expected_media = "text/html"
    elif output_format == "pdf":
        if not node_ids or node_ids[-1] != "md-pdf":
            node_ids.append("md-pdf")
        result_type = "report-pdf"
        expected_media = "application/pdf"
    elif output_format == "docx":
        if not node_ids or node_ids[-1] != "md-docx":
            node_ids.append("md-docx")
        result_type = "report-docx"
        expected_media = _DOCX_MEDIA_TYPE
    elif output_format == "markdown":
        result_type = workflow.result
        expected_media = "text/markdown"
    else:
        raise WorkflowCompileError(f"unsupported output format: {output_format}")

    counts: Counter[str] = Counter()
    compiled = []
    # Agent nodes may be conditionally appended by output format. Allow their
    # Node IDs in ``timeouts`` even when this compilation does not select them;
    # deterministic script nodes intentionally do not accept timeouts.
    valid_timeout_keys: set[str] = {
        item.node_id for item in registry.list() if item.kind == "agent"
    }
    prior: list[tuple[str, NodeSpec]] = []
    for node_id in node_ids:
        try:
            node = registry.get(node_id)
        except KeyError as exc:
            raise WorkflowCompileError(f"workflow references unknown node: {node_id}") from exc
        counts[node_id] += 1
        step_id = node_id if counts[node_id] == 1 else f"{node_id}-{counts[node_id]}"
        bindings = []
        for port in node.inputs:
            candidates = [
                (candidate_id, output)
                for candidate_id, candidate in prior
                for output in candidate.outputs
                if port.accepts(output)
                and (port.mode == "each" or output.mode == "state")
            ]
            source_step_id = candidates[-1][0] if candidates else None
            if source_step_id is None and port.required:
                raise WorkflowCompileError(
                    f"step {step_id} input {port.name} has no compatible producer"
                )
            bindings.append(
                InputBinding(
                    port=port.name,
                    mode=port.mode,
                    source_step_id=(source_step_id if port.mode == "each" else None),
                    artifact_type=port.artifact_type,
                    required=port.required,
                )
            )
        # Timeouts belong to node types, not individual repeated steps. This
        # keeps every invocation of the same node consistent within a mode.
        timeout_seconds = workflow.timeouts.get(node_id)
        if timeout_seconds is not None and node.kind == "script":
            raise WorkflowCompileError(
                f"workflow timeout {step_id} targets deterministic script node {node_id}; "
                "timeouts are supported only for agent nodes"
            )
        compiled.append(
            CompiledStep(
                step_id,
                node_id,
                tuple(bindings),
                timeout_seconds=timeout_seconds,
            )
        )
        prior.append((step_id, node))

    unknown_timeout_keys = set(workflow.timeouts) - valid_timeout_keys
    if unknown_timeout_keys:
        raise WorkflowCompileError(
            "workflow timeouts reference unknown agent nodes: "
            + ", ".join(sorted(unknown_timeout_keys))
        )

    result_candidates = [
        (step.step_id, output)
        for step in compiled
        for output in registry.get(step.node_id).outputs
        if output.artifact_type == result_type and output.primary
    ]
    if not result_candidates:
        raise WorkflowCompileError(
            f"workflow has no primary output of type {result_type}"
        )
    if result_candidates[-1][1].media_type != expected_media:
        raise WorkflowCompileError(
            f"primary result is {result_candidates[-1][1].media_type}, expected {expected_media}"
        )
    return CompiledWorkflow(
        name=workflow.name,
        steps=tuple(compiled),
        result_type=result_type,
        result_media_type=expected_media,
        output_format=output_format,
    )
