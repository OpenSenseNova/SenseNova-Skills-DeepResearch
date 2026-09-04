from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from deepresearch_cli.config import Artifact, NodeSpec, RunRequest
from deepresearch_cli.harness import AgentExecutionResult, AgentInvocation, Harness
from deepresearch_cli.harness.acp.events import project_tool_event
from deepresearch_cli.persistence import RunStore


_BINARY_RESOURCE_PREFIX = "deepresearch-resource-base64-v1:"
_NODE_TIMEOUT_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class NodeRunResult:
    outcome: str
    artifacts: Tuple[Artifact, ...] = ()
    error: Optional[str] = None
    diagnostics_ref: Optional[str] = None
    validation_warnings: Tuple[Mapping[str, Any], ...] = ()


class NodeRunner:
    def __init__(
        self,
        store: RunStore,
        harness: Harness,
        *,
        timeout_seconds: Optional[float],
        harness_metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.store = store
        self.harness = harness
        self.timeout_seconds = timeout_seconds
        self.harness_metadata = dict(harness_metadata or {})

    async def run(
        self,
        *,
        run_id: str,
        request: RunRequest,
        step_id: str,
        instance_id: str,
        scope: Mapping[str, str],
        attempt: int,
        node: NodeSpec,
        inputs: Sequence[Artifact],
        repair: Optional[Mapping[str, Any]] = None,
    ) -> NodeRunResult:
        layout = self.store.prepare_attempt(run_id, instance_id, attempt)
        can_repair = node.kind == "agent" and repair is None
        try:
            runtime_dir = layout.attempt_dir / "execution-runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            execution_resources = self._materialize_resources(node, runtime_dir)
            agent_resources = {
                name: path
                for name, path in execution_resources.items()
                if name not in self._command_resource_names(node)
            }
            context = self._build_context(
                run_id,
                request,
                step_id,
                instance_id,
                scope,
                attempt,
                node,
                inputs,
                layout.staging_dir,
                agent_resources,
            )
            context_path = runtime_dir / "context.json"
            context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            prepared_snapshot: dict[str, tuple[int, str]] = {}
            if node.preparer:
                prepared = await self._run_command(
                    node.preparer, execution_resources, context_path, layout.staging_dir
                )
                self.store.write_attempt_text(
                    run_id, instance_id, attempt, "preparer.stdout.log", prepared.stdout
                )
                self.store.write_attempt_text(
                    run_id, instance_id, attempt, "preparer.stderr.log", prepared.stderr
                )
                if prepared.returncode != 0:
                    return NodeRunResult(
                        "failed",
                        error=(prepared.stderr or prepared.stdout or "node preparer failed")[-4000:],
                        diagnostics_ref=layout.diagnostics_ref,
                    )
                prepared_snapshot = self._prepared_output_snapshot(node, context)
            if node.kind == "agent":
                execution = await self._run_agent(
                    run_id, step_id, instance_id, attempt, node, inputs, scope,
                    layout.staging_dir, context, repair,
                )
                if execution.status != "succeeded":
                    retry_timeout = (
                        execution.failure_kind == "timeout"
                        and attempt < _NODE_TIMEOUT_MAX_ATTEMPTS
                    )
                    return NodeRunResult(
                        "retryable" if retry_timeout else "failed",
                        error=execution.error or f"agent returned {execution.status}",
                        diagnostics_ref=layout.diagnostics_ref,
                    )
                runtime_warnings = []
                if prepared_snapshot != self._prepared_output_snapshot(node, context):
                    return NodeRunResult(
                        "failed",
                        error="agent modified a trusted prepared output",
                        diagnostics_ref=layout.diagnostics_ref,
                    )
            else:
                runtime_warnings = []
                completed = await self._run_command(
                    node.command, execution_resources, context_path, layout.staging_dir
                )
                self.store.write_attempt_text(run_id, instance_id, attempt, "stdout.log", completed.stdout)
                self.store.write_attempt_text(run_id, instance_id, attempt, "stderr.log", completed.stderr)
                if completed.returncode != 0:
                    return NodeRunResult(
                        "failed",
                        error=(completed.stderr or completed.stdout or "script failed")[-4000:],
                        diagnostics_ref=layout.diagnostics_ref,
                    )

            if node.materializer:
                materialized = await self._run_command(
                    node.materializer,
                    execution_resources,
                    context_path,
                    layout.staging_dir,
                )
                self.store.write_attempt_text(
                    run_id, instance_id, attempt,
                    "materializer.stdout.log", materialized.stdout,
                )
                self.store.write_attempt_text(
                    run_id, instance_id, attempt,
                    "materializer.stderr.log", materialized.stderr,
                )
                if materialized.returncode != 0:
                    repairable = (
                        can_repair
                        and node.repair_on_validation_failure
                    )
                    return NodeRunResult(
                        "repairable" if repairable else "failed",
                        error=(
                            materialized.stderr
                            or materialized.stdout
                            or "node materializer failed"
                        )[-4000:],
                        diagnostics_ref=layout.diagnostics_ref,
                        validation_warnings=tuple(runtime_warnings),
                    )

            candidate = self.store.freeze_staging_candidate(
                run_id, instance_id, attempt, exclude_top_level=("_runtime",)
            )
            candidate_context = self._replace_root(context, layout.staging_dir, candidate)
            validator_context_path = layout.attempt_dir / "validator-context.json"
            validator_context_path.write_text(
                json.dumps(candidate_context, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.store.seal_candidate(run_id, instance_id, attempt, candidate)
            before = self.store.snapshot_candidate(run_id, instance_id, attempt, candidate)
            try:
                matched = self._match_outputs(node, candidate, scope)
            except ValueError as exc:
                repairable = can_repair
                return NodeRunResult(
                    "repairable" if repairable else "failed",
                    error=str(exc),
                    diagnostics_ref=layout.diagnostics_ref,
                    validation_warnings=tuple(runtime_warnings),
                )
            validation_warnings_list: List[Mapping[str, Any]] = list(runtime_warnings)
            validation_commands = node.validators or ((node.validator,) if node.validator else ())
            for validator_index, validator_command in enumerate(validation_commands, start=1):
                validation_resources = self._materialize_resources(
                    node,
                    layout.attempt_dir / f"validation-runtime-{uuid.uuid4().hex}",
                )
                validated = await self._run_command(
                    validator_command,
                    validation_resources,
                    validator_context_path,
                    candidate,
                )
                log_suffix = (
                    "validator"
                    if len(validation_commands) == 1
                    else f"validator-{validator_index}"
                )
                self.store.write_attempt_text(
                    run_id, instance_id, attempt, f"{log_suffix}.stdout.log", validated.stdout
                )
                self.store.write_attempt_text(
                    run_id,
                    instance_id,
                    attempt,
                    f"{log_suffix}.stderr.log",
                    validated.stderr,
                )
                if validated.returncode != 0:
                    repairable = (
                        can_repair
                        and node.repair_on_validation_failure
                    )
                    return NodeRunResult(
                        "repairable" if repairable else "failed",
                        error=(validated.stderr or validated.stdout or "validator rejected output")[-4000:],
                        diagnostics_ref=layout.diagnostics_ref,
                        validation_warnings=tuple(runtime_warnings),
                    )
                validation_warnings_list.extend(self._validator_warnings(validated.stdout))
            validation_warnings = tuple(validation_warnings_list)
            after = self.store.snapshot_candidate(run_id, instance_id, attempt, candidate)
            if before != after:
                raise ValueError("node validator modified the sealed publication candidate")
            validated_files = {relative: after[relative] for relative, _, _ in matched}
            published = self.store.publish_artifacts(
                run_id, instance_id, attempt, candidate, validated_files
            )
            artifacts: List[Artifact] = []
            for relative, output, artifact_scope in matched:
                base = next(
                    item for item in published if item["path"].endswith("/" + relative)
                )
                artifacts.append(
                    Artifact(
                        port=output.name,
                        artifact_type=output.artifact_type,
                        media_type=output.media_type,
                        path=base["path"],
                        sha256=base["sha256"],
                        scope=artifact_scope,
                        mode=output.mode,
                        step_id=step_id,
                        instance_id=instance_id,
                    )
                )
            return NodeRunResult(
                "succeeded",
                tuple(artifacts),
                diagnostics_ref=layout.diagnostics_ref,
                validation_warnings=validation_warnings,
            )
        except Exception as exc:
            return NodeRunResult(
                "failed", error=f"{type(exc).__name__}: {exc}", diagnostics_ref=layout.diagnostics_ref
            )

    def _build_context(
        self,
        run_id: str,
        request: RunRequest,
        step_id: str,
        instance_id: str,
        scope: Mapping[str, str],
        attempt: int,
        node: NodeSpec,
        inputs: Sequence[Artifact],
        staging: Path,
        resources: Mapping[str, Path],
    ) -> Dict[str, Any]:
        grouped: Dict[str, List[Mapping[str, Any]]] = {item.name: [] for item in node.inputs}
        for artifact in inputs:
            path = self.store.validate_artifact_ref(run_id, artifact.to_dict())
            target_ports = [
                port for port in node.inputs
                if port.artifact_type == artifact.artifact_type and artifact.media_type in port.media_types
            ]
            for port in target_ports:
                grouped[port.name].append({**artifact.to_dict(), "path": str(path)})
        outputs: Dict[str, Mapping[str, Any]] = {}
        for output in node.outputs:
            if output.mode == "batch":
                directory = (staging / Path(output.path).parent).resolve()
                directory.mkdir(parents=True, exist_ok=True)
                outputs[output.name] = {
                    **output.to_dict(),
                    "pattern": str((staging / output.path).resolve()),
                    "directory": str(directory),
                    "prepared": output.name in node.prepared_outputs,
                }
            else:
                path = (staging / output.path).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                outputs[output.name] = {
                    **output.to_dict(),
                    "path": str(path),
                    "prepared": output.name in node.prepared_outputs,
                }
        context = {
            "run": {"id": run_id, **request.to_dict()},
            "step": {"id": step_id, "node": node.node_id, "instance": instance_id, "attempt": attempt},
            "scope": dict(scope),
            "inputs": grouped,
            "outputs": outputs,
            "resources": {name: str(path) for name, path in resources.items()},
        }
        context["prompt"] = self._build_prompt_view(context, node.node_id)
        return context

    @staticmethod
    def _build_prompt_view(context: Mapping[str, Any], node_id: str) -> Dict[str, Any]:
        """Expose stable prompt aliases without replacing the authoritative context."""
        run = context["run"]
        scope = context["scope"]
        inputs = context["inputs"]
        outputs = context["outputs"]
        resources = context["resources"]
        view: Dict[str, Any] = {
            "query": run["query"],
            "language": run["language"],
            "mode": run["mode"],
            "output_format": run["output_format"],
            "report_format": run["report_format"],
            "dimension_id": scope.get("dimension-id"),
            "content_unit_id": scope.get("content-unit-id"),
        }
        for port, values in inputs.items():
            normalized = port.replace("-", "_")
            paths = [item["path"] for item in values]
            view[f"{normalized}_paths"] = paths
            view[f"{normalized}_path"] = paths[-1] if paths else None
        for port, value in outputs.items():
            normalized = port.replace("-", "_")
            if "path" in value:
                view[f"{normalized}_output_path"] = value["path"]
            if "directory" in value:
                view[f"{normalized}_output_directory"] = value["directory"]
                view[f"{normalized}_output_pattern"] = value["pattern"]
        preferred_output = {
            "scout": "briefing",
            "plan": "plan",
            "research": "evidence",
            "review": "review",
            "perspective": "perspective",
            "supplement-planner": "supplement-plan",
            "report-planner": "outline",
            "report-writer": "draft",
            "stitcher": "stitched",
            "final-review": "review",
            "final-review-diagnostic": "review",
            "final-review-recheck": "review",
            "final-repair": "draft",
            "md-html": "report",
        }.get(node_id)
        if preferred_output and "path" in outputs.get(preferred_output, {}):
            view["output_path"] = outputs[preferred_output]["path"]
        schema_resources = {
            name.removesuffix(".md").replace(".", "_"): path
            for name, path in resources.items()
            if name.endswith("schema.md")
        }
        for name, path in schema_resources.items():
            view[f"{name}_path"] = path
        preferred_schema = {
            "scout": "briefing_schema",
            "plan": "plan_schema",
            "research": "evidence_schema",
            "perspective": "perspective_feedback_schema",
            "supplement-planner": "supplement_plan_schema",
            "report-planner": "outline_schema",
        }.get(node_id)
        view["schema_path"] = schema_resources.get(preferred_schema) if preferred_schema else None
        if node_id == "research":
            view["research_mode"] = run["mode"]
            view["mode"] = (
                "supplement"
                if view.get("supplement_plan_path")
                else ("quick" if run["mode"] == "quick" else "initial")
            )
            view["dimension_id"] = view.get("dimension_id") or "d1"
            view["research_phase"] = "supplement" if view["mode"] == "supplement" else "initial"
            view["research_round"] = 2 if view["mode"] == "supplement" else 1
            view["existing_evidence_path"] = view.get("evidence_path")
            view["supplement_plan_output_path"] = view.get(
                "completed_supplement_plan_output_path"
            )
        elif node_id == "report-planner":
            view["outline_path"] = view.get("outline_output_path")
            view["content_units_dir"] = view.get("content_tasks_output_directory")
            view["report_templates_path"] = resources.get("report_templates.yaml")
        elif node_id == "report-writer":
            view["subset_path"] = view.get("task_path")
            view["write_mode"] = "write_unit" if view.get("task_path") else "quick_synthesis"
            view["report_templates_path"] = resources.get("report_templates.yaml")
        elif node_id == "final-repair":
            view["subset_path"] = view.get("task_path")
            view["original_draft_path"] = view.get("draft_path")
        elif node_id == "stitcher":
            view["content_unit_paths"] = view.get("drafts_paths", [])
        elif node_id in {"final-review", "final-review-diagnostic", "final-review-recheck"}:
            view["stitched_path"] = view.get("stitched_output_path") or view.get("report_path")
            effective_units = {
                item.get("scope", {}).get("content-unit-id"): item.get("path")
                for item in inputs.get("drafts", [])
                if item.get("scope", {}).get("content-unit-id") and item.get("path")
            }
            effective_units.update({
                item.get("scope", {}).get("content-unit-id"): item.get("path")
                for item in inputs.get("repairs", [])
                if item.get("scope", {}).get("content-unit-id") and item.get("path")
            })
            view["content_unit_paths"] = [effective_units[key] for key in sorted(effective_units)]
            view["supplement_plan_paths"] = view.get("supplement_plans_paths", [])
        return view

    @staticmethod
    def _agent_contract(
        context: Mapping[str, Any], repair: Optional[Mapping[str, Any]] = None
    ) -> str:
        contract = (
            "\n\n# Runtime Node Contract\n"
            "The JSON below is the complete authoritative execution context. "
            "Read only declared inputs and write every required output to its declared path. "
            "Outputs marked prepared=true were generated by trusted Runtime code before this "
            "invocation; treat them as read-only inputs and do not modify them. "
            "Create or replace declared outputs with the harness's native file write/edit tool; "
            "never use terminal commands, shell redirection, heredocs, inline scripts, or generated "
            "helper scripts to write declared outputs. Terminal tools may inspect inputs or run "
            "validators, but must not create or replace a declared output. "
            "A batch output is zero or more files matching its pattern; do not invent runtime metadata. "
            "The prompt object contains stable aliases such as query, schema_path, input paths, "
            "and output_path; it is derived from run/inputs/outputs/resources.\n"
            "<node_context_json>\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
            + "\n</node_context_json>"
        )
        if repair:
            contract += (
                "\n\n# Repair\n"
                + json.dumps(repair, ensure_ascii=False, indent=2)
                + "\nThis is a fresh attempt workspace. Recreate every required declared output "
                "in this attempt even when the prior attempt produced only intermediate files or "
                "a candidate. Use the native file write/edit tool for those outputs."
            )
        return contract

    async def _run_agent(
        self,
        run_id: str,
        step_id: str,
        instance_id: str,
        attempt: int,
        node: NodeSpec,
        inputs: Sequence[Artifact],
        scope: Mapping[str, str],
        staging: Path,
        context: Mapping[str, Any],
        repair: Optional[Mapping[str, Any]],
    ) -> AgentExecutionResult:
        prompt = str(node.prompt) + self._agent_contract(context, repair)
        invocation_id = f"inv-{instance_id}-a{attempt}-{uuid.uuid4().hex[:10]}"
        invocation = AgentInvocation(
            invocation_id=invocation_id,
            run_id=run_id,
            node_instance_id=instance_id,
            node_type=node.node_id,
            attempt=attempt,
            workspace=staging,
            input_artifact_refs=[item.to_dict() for item in inputs],
            resolved_input_artifacts=[item for values in context["inputs"].values() for item in values],
            timeout_seconds=self.timeout_seconds,
            agent_context=context,
            prompt=prompt,
            allow_workspace_edits=True,
        )
        self.store.write_attempt_json(
            run_id, instance_id, attempt, "invocation.json",
            {
                "control": {
                    "step_id": step_id,
                    "node_id": node.node_id,
                    "invocation_id": invocation_id,
                    "timeout_seconds": self.timeout_seconds,
                },
                "agent_context": context,
                "prompt": prompt,
            },
        )
        return await self._invoke_and_record(
            invocation,
            run_id=run_id,
            instance_id=instance_id,
            attempt=attempt,
        )

    async def _invoke_and_record(
        self,
        invocation: AgentInvocation,
        *,
        run_id: str,
        instance_id: str,
        attempt: int,
    ) -> AgentExecutionResult:
        result = await self.harness.invoke(invocation)
        # Persist only the bounded tool-progress projection. Agent messages,
        # tool bodies and result payloads stay out of diagnostics and Web.
        for sequence, event in enumerate(result.events[:2_000], start=1):
            projection = project_tool_event(event)
            if projection is not None:
                self.store.append_attempt_jsonl(
                    run_id,
                    instance_id,
                    attempt,
                    "acp-events.jsonl",
                    {"sequence": sequence, **projection},
                )
        self.store.write_attempt_text(
            run_id, instance_id, attempt, "raw-response.txt", result.response_text
        )
        self.store.write_attempt_text(
            run_id, instance_id, attempt, "stderr.log", result.stderr
        )
        return result

    def _materialize_resources(self, node: NodeSpec, runtime_dir: Path) -> Mapping[str, Path]:
        result = {}
        root = runtime_dir / "resources"
        if runtime_dir.is_symlink():
            raise ValueError(f"runtime resource directory is unsafe: {runtime_dir}")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if root.exists() or root.is_symlink():
            raise ValueError(f"runtime resources already exist: {root}")
        root.mkdir()
        for name, content in node.resources.items():
            path = root / Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if content.startswith(_BINARY_RESOURCE_PREFIX):
                try:
                    data = base64.b64decode(
                        content[len(_BINARY_RESOURCE_PREFIX) :], validate=True
                    )
                except (ValueError, base64.binascii.Error) as exc:
                    raise ValueError(f"invalid binary node resource: {name}") from exc
                path.write_bytes(data)
            else:
                path.write_text(content, encoding="utf-8")
            result[name] = path
        return result

    @staticmethod
    def _command_resource_names(node: NodeSpec) -> set[str]:
        """Return resources used only by trusted Runtime commands.

        Validators and materializers must be present in the immutable NodeSpec
        and materialized for execution, but exposing their source paths in an
        Agent's Node Context only increases context and invites needless reads.
        """

        commands = [node.command, node.preparer, node.materializer, node.validator, *node.validators]
        hidden = {
            item.split(":", 1)[1]
            for command in commands
            for item in command
            if item.startswith("resource:")
        }
        return hidden

    @staticmethod
    def _prepared_output_snapshot(
        node: NodeSpec, context: Mapping[str, Any]
    ) -> dict[str, tuple[int, str]]:
        import hashlib

        snapshot: dict[str, tuple[int, str]] = {}
        for name in node.prepared_outputs:
            value = context["outputs"][name]
            if "path" not in value:
                raise ValueError("prepared batch outputs are not supported")
            path = Path(value["path"])
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"preparer did not create declared output: {name}")
            data = path.read_bytes()
            snapshot[name] = (len(data), hashlib.sha256(data).hexdigest())
        return snapshot

    async def _run_command(
        self,
        command: Sequence[str],
        resources: Mapping[str, Path],
        context_path: Path,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        resolved = []
        for index, item in enumerate(command):
            if item.startswith("resource:"):
                resolved.append(str(resources[item.split(":", 1)[1]]))
            elif index == 0 and item == "python":
                resolved.append(sys.executable)
            else:
                resolved.append(item)
        env = dict(os.environ)
        env["DEEPRESEARCH_NODE_CONTEXT"] = str(context_path)
        env["DEEPRESEARCH_NODE_RESOURCES"] = json.dumps(
            {name: str(path) for name, path in resources.items()}, ensure_ascii=False
        )
        return await asyncio.to_thread(
            subprocess.run,
            resolved,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )

    @staticmethod
    def _validator_warnings(stdout: str) -> Tuple[Mapping[str, Any], ...]:
        try:
            value = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return ()
        warnings = value.get("warnings", []) if isinstance(value, Mapping) else []
        if not isinstance(warnings, list):
            return ()
        return tuple(item for item in warnings if isinstance(item, Mapping))

    @staticmethod
    def _replace_root(value: Mapping[str, Any], old: Path, new: Path) -> Dict[str, Any]:
        result = json.loads(json.dumps(value, ensure_ascii=False))
        old_root = str(old.resolve())
        new_root = str(new.resolve())
        for output in result.get("outputs", {}).values():
            if not isinstance(output, dict):
                continue
            for key in ("path", "pattern", "directory"):
                current = output.get(key)
                if isinstance(current, str) and (
                    current == old_root or current.startswith(old_root + os.sep)
                ):
                    output[key] = new_root + current[len(old_root):]
        return result

    @staticmethod
    def _match_outputs(
        node: NodeSpec, candidate: Path, scope: Mapping[str, str]
    ) -> List[tuple[str, Any, Mapping[str, str]]]:
        result = []
        for output in node.outputs:
            if output.mode == "state":
                path = candidate / output.path
                if path.is_symlink() or not path.is_file():
                    if output.required:
                        raise ValueError(f"required output is missing: {output.path}")
                    continue
                if path.stat().st_size == 0:
                    raise ValueError(f"output is empty: {output.path}")
                result.append((output.path, output, dict(scope)))
                continue
            matches = sorted(candidate.glob(output.path))
            if output.required and not matches:
                raise ValueError(f"required batch output is empty: {output.path}")
            seen = set()
            for path in matches:
                if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                    raise ValueError(f"batch output is unsafe or empty: {path}")
                relative = path.relative_to(candidate).as_posix()
                key = path.name.split(".", 1)[0]
                if key in seen or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", key):
                    raise ValueError(f"invalid or duplicate batch scope key: {key}")
                seen.add(key)
                item_scope = dict(scope)
                item_scope[str(output.scope_key)] = key
                result.append((relative, output, item_scope))
        return result
