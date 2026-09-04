import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest

from deepresearch_cli.config import (
    NodeRegistry,
    RunRequest,
    WorkflowSpec,
    load_workflow_spec,
)
from deepresearch_cli.driver import (
    ExecutionSessionConfig,
    ManifestIntegrityError,
    WorkflowDriver,
    projection_summary,
)
from deepresearch_cli.harness import AgentExecutionResult
from deepresearch_cli.harness.stub import StubHarness
from deepresearch_cli.persistence import RunStore
from deepresearch_cli.progress import TerminalProgressReporter
from deepresearch_cli.web.snapshot import build_run_snapshot


def run(coro):
    return asyncio.run(coro)


def make_driver(tmp_path, harness=None, *, concurrency=4, timeout_seconds=600.0):
    harness = harness or StubHarness()
    run(harness.start())
    store = RunStore(tmp_path / "runs")
    driver = WorkflowDriver(
        store,
        harness,
        ExecutionSessionConfig(
            harness="stub",
            max_concurrency=concurrency,
            node_timeout_seconds=timeout_seconds,
        ),
        output_dir=tmp_path / "output",
    )
    return store, harness, driver, NodeRegistry.load()


def execute_mode(tmp_path, mode, output_format="markdown", harness=None):
    store, harness, driver, registry = make_driver(tmp_path, harness)
    projection = driver.create_run(
        RunRequest("test query", "zh-CN", mode, output_format),
        load_workflow_spec(mode=mode),
        registry,
    )
    projection = run(driver.drive(projection.run_id))
    return store, harness, driver, projection


@pytest.mark.parametrize(
    ("mode", "expected_agent_nodes"),
    [
        ("quick", ["research", "report-writer"]),
        ("normal", ["plan", "research", "report-writer"]),
    ],
)
def test_builtin_markdown_modes_export_the_final_report(
    tmp_path, mode, expected_agent_nodes
):
    store, harness, driver, projection = execute_mode(tmp_path, mode)

    assert projection.status == "completed"
    assert [item.node_type for item in harness.invocations] == expected_agent_nodes
    summary = projection_summary(store, projection, output_dir=driver.output_dir)
    assert "progress" not in summary
    assert summary["result"]["type"] == "report"
    report = Path(summary["result"]["path"])
    assert report == tmp_path / "output" / projection.run_id / "report.md"
    assert report.is_file()
    assert "参考文献" in report.read_text(encoding="utf-8")
    assert summary["result"]["artifact_ref"]["path"].startswith("artifacts/")


def test_heavy_formal_report_persists_profile_for_writers(tmp_path):
    store, harness, driver, registry = make_driver(tmp_path)
    projection = driver.create_run(
        RunRequest("formal market analysis", mode="heavy", report_format="formal_report"),
        load_workflow_spec(mode="heavy"),
        registry,
    )
    projection = run(driver.drive(projection.run_id))

    assert projection.status == "completed"
    assert store.load_manifest(projection.run_id)["context"]["report_format"] == "formal_report"
    outline_artifact = next(
        item for item in projection.artifacts if item.artifact_type == "report-outline"
    )
    outline = json.loads(
        store.validate_artifact_ref(
            projection.run_id, outline_artifact.to_dict()
        ).read_text(encoding="utf-8")
    )
    assert outline["report_profile"] == {
        "format": "formal_report",
        "template_id": "general_research",
    }
    writer = next(item for item in harness.invocations if item.node_type == "report-writer")
    assert writer.agent_context["prompt"]["report_format"] == "formal_report"
    assert writer.agent_context["prompt"]["report_templates_path"].endswith(
        "/resources/report_templates.yaml"
    )


def test_heavy_empty_supplement_batch_skips_only_the_second_cycle(tmp_path):
    store, harness, driver, projection = execute_mode(tmp_path, "heavy")

    assert projection.status == "completed"
    assert [item.node_type for item in harness.invocations] == [
        "scout",
        "plan",
        "research",
        "review",
        "perspective",
        "supplement-planner",
        "report-planner",
        "report-writer",
        "final-review-diagnostic",
    ]
    assert not any(item.step_id == "research-2" for item in projection.instances.values())
    progress = projection_summary(
        store, projection, output_dir=driver.output_dir
    )["progress"]
    assert progress["percent"] == 100
    assert progress["phase"] == "completed"
    assert progress["writing"] == {"completed": 1, "total": 1}


def test_heavy_partial_run_exposes_resumable_progress(tmp_path):
    store, _, driver, registry = make_driver(tmp_path)
    projection = driver.create_run(
        RunRequest("progress test", mode="heavy"),
        load_workflow_spec(mode="heavy"),
        registry,
    )

    partial = run(driver.drive(projection.run_id, max_steps=1))
    progress = projection_summary(
        store, partial, output_dir=driver.output_dir
    )["progress"]

    assert progress["percent"] == 4
    assert progress["phase"] == "plan"
    assert progress["phase_label"] == "制定研究计划"


def test_normal_mode_web_snapshot_exposes_real_workflow_progress(tmp_path):
    store, _, driver, registry = make_driver(tmp_path)
    projection = driver.create_run(
        RunRequest("normal web progress", mode="normal"),
        load_workflow_spec(mode="normal"),
        registry,
    )

    partial = run(driver.drive(projection.run_id, max_steps=1))
    snapshot = build_run_snapshot(
        store, partial.run_id, output_dir=driver.output_dir
    )

    assert snapshot["mode"] == "normal"
    assert snapshot["progress"] == {
        "percent": 25,
        "phase": "research",
        "phase_label": "收集与整理证据",
    }
    assert [item["id"] for item in snapshot["pipeline"]] == [
        "planning", "researching", "writing", "delivery"
    ]
    assert snapshot["pipeline"][0]["status"] == "done"
    assert all(item["id"] != "finalizing" for item in snapshot["pipeline"])


def test_tool_progress_is_persisted_for_fetched_conversion_metrics(tmp_path):
    class FetchingStub(StubHarness):
        async def invoke(self, invocation):
            result = await super().invoke(invocation)
            if invocation.node_type != "research":
                return result
            return AgentExecutionResult(
                **{
                    **result.__dict__,
                    "events": [
                        {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "fetch-1",
                            "kind": "fetch",
                            "title": "fetch https://example.com/stub",
                            "content": [{"body": "must not be persisted"}],
                        },
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "fetch-1",
                            "status": "completed",
                            "content": [{"body": "must not be persisted"}],
                        },
                    ],
                }
            )

    store, _, driver, projection = execute_mode(
        tmp_path, "quick", harness=FetchingStub()
    )
    event_files = list(
        (store.run_dir(projection.run_id) / "attempts").glob(
            "*/attempt-*/acp-events.jsonl"
        )
    )
    persisted = "".join(path.read_text(encoding="utf-8") for path in event_files)
    assert "must not be persisted" not in persisted
    snapshot = build_run_snapshot(
        store, projection.run_id, output_dir=driver.output_dir
    )
    assert snapshot["search"]["funnel"]["fetched"] == 1
    assert snapshot["search"]["funnel"]["evidence"] == 1
    assert snapshot["search"]["funnel"]["rates"]["evidence"] == 100.0


@pytest.mark.parametrize(("mode", "shows_heavy_progress"), [("quick", False), ("heavy", True)])
def test_live_workflow_progress_is_only_rendered_for_heavy(
    tmp_path, mode, shows_heavy_progress
):
    stream = io.StringIO()
    store, _, driver, registry = make_driver(tmp_path)
    driver.progress_reporter = TerminalProgressReporter(stream=stream)
    projection = driver.create_run(
        RunRequest("live progress", mode=mode),
        load_workflow_spec(mode=mode),
        registry,
    )

    completed = run(driver.drive(projection.run_id))

    assert completed.status == "completed"
    assert ("[heavy]" in stream.getvalue()) is shows_heavy_progress
    if shows_heavy_progress:
        assert "100% · 已完成" in stream.getvalue()


def test_html_format_runs_html_node_and_exports_both_files(tmp_path):
    store, harness, driver, projection = execute_mode(tmp_path, "quick", "html")

    assert projection.status == "completed"
    assert harness.invocations[-1].node_type == "md-html"
    assert "# Report HTML" in harness.invocations[-1].prompt
    summary = projection_summary(store, projection, output_dir=driver.output_dir)
    assert summary["result"]["type"] == "html_report"
    assert Path(summary["result"]["path"]).is_file()
    assert Path(summary["result"]["source_report_path"]).is_file()


def test_docx_format_runs_converter_and_exports_source(monkeypatch, tmp_path):
    fake_pandoc = tmp_path / "fake-pandoc"
    fake_pandoc.write_text(
        f"""#!{sys.executable}
import sys
import zipfile

destination = sys.argv[sys.argv.index("--output") + 1]
document = ('<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            'Report</w:t></w:r></w:p></w:body></w:document>')
with zipfile.ZipFile(destination, "w") as archive:
    archive.writestr("[Content_Types].xml", "<Types/>")
    archive.writestr("_rels/.rels", "<Relationships/>")
    archive.writestr("word/document.xml", document)
""",
        encoding="utf-8",
    )
    fake_pandoc.chmod(0o755)
    monkeypatch.setenv("DEEPRESEARCH_PANDOC", str(fake_pandoc))

    store, _, driver, projection = execute_mode(tmp_path, "quick", "docx")

    assert projection.status == "completed"
    summary = projection_summary(store, projection, output_dir=driver.output_dir)
    assert summary["result"]["type"] == "docx_report"
    assert Path(summary["result"]["path"]).name == "report.docx"
    assert Path(summary["result"]["path"]).is_file()
    assert Path(summary["result"]["source_report_path"]).is_file()


def test_pdf_format_runs_markdown_pdf_node_and_exports_source(
    monkeypatch, tmp_path
):
    fake_pandoc = tmp_path / "fake-pandoc"
    fake_pandoc.write_text(
        f"""#!{sys.executable}
import sys
from pathlib import Path

destination = Path(sys.argv[sys.argv.index("--output") + 1])
destination.write_bytes(
    b"%PDF-1.4\\n1 0 obj\\n<< /Type /Page >>\\nendobj\\n%%EOF\\n"
)
""",
        encoding="utf-8",
    )
    fake_pandoc.chmod(0o755)
    monkeypatch.setenv("DEEPRESEARCH_PANDOC", str(fake_pandoc))
    monkeypatch.setenv("DEEPRESEARCH_TYPST", str(fake_pandoc))

    store, harness, driver, projection = execute_mode(tmp_path, "quick", "pdf")

    assert projection.status == "completed"
    assert [item.node_type for item in harness.invocations] == [
        "research",
        "report-writer",
    ]
    summary = projection_summary(store, projection, output_dir=driver.output_dir)
    assert summary["result"]["type"] == "pdf_report"
    assert Path(summary["result"]["path"]).name == "report.pdf"
    assert Path(summary["result"]["path"]).is_file()
    assert Path(summary["result"]["source_report_path"]).is_file()


def test_resume_continues_without_reinvoking_completed_steps(tmp_path):
    store, harness, driver, registry = make_driver(tmp_path)
    projection = driver.create_run(
        RunRequest("resume test", mode="normal"),
        load_workflow_spec(mode="normal"),
        registry,
    )
    partial = run(driver.drive(projection.run_id, max_steps=1))

    assert partial.status == "running"
    assert [item.node_type for item in harness.invocations] == ["plan"]
    completed = run(driver.resume(projection.run_id))
    assert completed.status == "completed"
    assert [item.node_type for item in harness.invocations] == [
        "plan",
        "research",
        "report-writer",
    ]
    assert store.load_manifest(projection.run_id)["runtime"] == "config-workflow"


def test_agent_context_includes_resources_and_derived_prompt_view(tmp_path):
    _, harness, _, projection = execute_mode(tmp_path, "normal")
    research = next(item for item in harness.invocations if item.node_type == "research")

    assert set(research.agent_context) == {
        "run", "step", "scope", "inputs", "outputs", "resources", "prompt"
    }
    assert set(research.agent_context["run"]) == {
        "id",
        "query",
        "language",
        "mode",
        "output_format",
        "report_format",
    }
    assert research.agent_context["inputs"]["task"][0]["type"] == "research-task"
    assert research.agent_context["outputs"]["evidence"]["path"].endswith(
        "/evidence.json"
    )
    assert research.agent_context["prompt"]["schema_path"].endswith(
        "/resources/evidence.schema.md"
    )
    assert research.agent_context["prompt"]["mode"] == "initial"
    assert research.agent_context["prompt"]["output_path"].endswith("/evidence.json")
    assert "checkpoint_path" not in research.agent_context["prompt"]
    assert "search_materials_path" not in research.agent_context["prompt"]
    assert research.timeout_seconds == 1800.0
    assert set(research.agent_context["resources"]) == {
        "evidence.schema.md",
        "supplement_plan.schema.md",
    }
    assert projection.status == "completed"


def test_custom_workflow_without_step_timeout_uses_session_fallback(tmp_path):
    store, harness, driver, registry = make_driver(tmp_path, timeout_seconds=77)
    projection = driver.create_run(
        RunRequest("fallback timeout", mode="normal"),
        WorkflowSpec(
            name="fallback-timeout",
            steps=("research", "report-writer", "render"),
            result="report",
        ),
        registry,
    )

    projection = run(driver.drive(projection.run_id))

    assert projection.status == "completed"
    assert [item.timeout_seconds for item in harness.invocations] == [77, 77]


def test_agent_context_hides_runtime_validator_and_materializer_resources(tmp_path):
    _, harness, _, projection = execute_mode(tmp_path, "heavy")
    expected_resources = {
        "scout": {"briefing.schema.md"},
        "plan": {"plan.schema.md"},
        "research": {"evidence.schema.md", "supplement_plan.schema.md"},
        "supplement-planner": {"supplement_plan.schema.md"},
        "report-planner": {"outline.schema.md", "report_templates.yaml"},
    }

    for node_type, expected in expected_resources.items():
        invocations = [
            item for item in harness.invocations if item.node_type == node_type
        ]
        assert invocations
        for invocation in invocations:
            assert set(invocation.agent_context["resources"]) == expected

    assert projection.status == "completed"


def test_failed_node_makes_the_run_terminal(tmp_path):
    harness = StubHarness(fail_node="research")
    _, _, _, projection = execute_mode(tmp_path, "quick", harness=harness)

    assert projection.status == "failed"
    assert "injected failure" in projection.error
    assert not (tmp_path / "output" / projection.run_id).exists()


class TimeoutHarness(StubHarness):
    def __init__(self, *, always_timeout=False, timeout_dimension=None):
        super().__init__()
        self.always_timeout = always_timeout
        self.timeout_dimension = timeout_dimension

    async def invoke(self, invocation):
        if invocation.node_type == "research":
            dimension = invocation.agent_context["scope"].get("dimension-id", "d1")
            prior_timeouts = [
                item
                for item in self.invocations
                if item.node_type == "research"
                and item.agent_context["scope"].get("dimension-id", "d1") == dimension
            ]
            selected = self.timeout_dimension is None or dimension == self.timeout_dimension
            if selected and (self.always_timeout or not prior_timeouts):
                self.invocations.append(invocation)
                return AgentExecutionResult(
                    status="cancelled",
                    stop_reason="cancelled",
                    error=f"Stub invocation timed out after {invocation.timeout_seconds}s",
                    failure_kind="timeout",
                )
        result = await super().invoke(invocation)
        if invocation.node_type == "plan":
            path = Path(invocation.agent_context["outputs"]["plan"]["path"])
            plan = json.loads(path.read_text(encoding="utf-8"))
            if self.timeout_dimension == "d2":
                second = json.loads(json.dumps(plan["dimensions"][0]))
                second.update({"id": "d2", "name": "Second stub dimension"})
                second["scope_ownership"].update(
                    {"owns": ["second stub fact"], "overlap_policy": "Only d2 owns it"}
                )
                plan["dimensions"].append(second)
                path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return result


def test_timed_out_agent_retries_once_with_a_clean_attempt(tmp_path):
    harness = TimeoutHarness()
    store, _, _, projection = execute_mode(tmp_path, "quick", harness=harness)

    assert projection.status == "completed"
    research = [item for item in harness.invocations if item.node_type == "research"]
    assert [item.attempt for item in research] == [1, 2]
    assert research[0].invocation_id != research[1].invocation_id
    assert "# Repair" not in research[1].prompt
    outcomes = [
        event["outcome"]
        for event in store.load_run(projection.run_id).events
        if event["type"] == "step_finished" and event["node_id"] == "research"
    ]
    assert outcomes == ["retryable", "succeeded"]


def test_agent_fails_after_two_consecutive_timeouts(tmp_path):
    harness = TimeoutHarness(always_timeout=True)
    store, _, _, projection = execute_mode(tmp_path, "quick", harness=harness)

    assert projection.status == "failed"
    research = [item for item in harness.invocations if item.node_type == "research"]
    assert [item.attempt for item in research] == [1, 2]
    outcomes = [
        event["outcome"]
        for event in store.load_run(projection.run_id).events
        if event["type"] == "step_finished" and event["node_id"] == "research"
    ]
    assert outcomes == ["retryable", "failed"]


def test_timeout_retry_preserves_successful_parallel_scope(tmp_path):
    harness = TimeoutHarness(timeout_dimension="d2")
    _, _, _, projection = execute_mode(tmp_path, "normal", harness=harness)

    assert projection.status == "completed"
    research = [item for item in harness.invocations if item.node_type == "research"]
    attempts_by_dimension = {}
    for invocation in research:
        dimension = invocation.agent_context["scope"]["dimension-id"]
        attempts_by_dimension.setdefault(dimension, []).append(invocation.attempt)
    assert attempts_by_dimension == {"d1": [1], "d2": [1, 2]}


class InvalidEvidenceHarness(StubHarness):
    @classmethod
    def _materialize_stub_outputs(cls, invocation):
        super()._materialize_stub_outputs(invocation)
        if invocation.node_type == "research":
            path = Path(invocation.agent_context["outputs"]["evidence"]["path"])
            value = json.loads(path.read_text(encoding="utf-8"))
            value.pop("headline", None)
            cls._write_json(path, value)


def test_runtime_rejects_evidence_that_fails_the_formal_contract(tmp_path):
    _, harness, _, projection = execute_mode(
        tmp_path, "quick", harness=InvalidEvidenceHarness()
    )

    assert projection.status == "failed"
    assert "V003" in projection.error
    assert len([item for item in harness.invocations if item.node_type == "research"]) == 2


class ReviseFinalReviewHarness(StubHarness):
    @classmethod
    def _materialize_stub_outputs(cls, invocation):
        super()._materialize_stub_outputs(invocation)
        if invocation.node_type == "final-review-diagnostic":
            path = Path(invocation.agent_context["outputs"]["review"]["path"])
            path.write_text(
                "## 审查结论\n\nVERDICT: revise\n\n## 问题清单\n\nREPAIR_TARGET: u1 | 需修改。\n\n"
                "## 审查说明\n\n报告未通过。\n",
                encoding="utf-8",
            )
        elif invocation.node_type == "final-repair":
            path = Path(invocation.agent_context["outputs"]["draft"]["path"])
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "is supported", "is supported after targeted repair"
                ),
                encoding="utf-8",
            )


def test_final_review_revise_runs_one_targeted_repair_and_recheck(tmp_path):
    store, harness, driver, projection = execute_mode(
        tmp_path, "heavy", harness=ReviseFinalReviewHarness()
    )

    assert projection.status == "completed"
    assert len([item for item in harness.invocations if item.node_type == "final-review-diagnostic"]) == 1
    repairs = [item for item in harness.invocations if item.node_type == "final-repair"]
    assert len(repairs) == 1
    assert repairs[0].agent_context["scope"]["content-unit-id"] == "u1"
    assert len([item for item in harness.invocations if item.node_type == "final-review-recheck"]) == 1
    assert (driver.output_dir / projection.run_id).exists()
    assert "after targeted repair" in (
        driver.output_dir / projection.run_id / "report.md"
    ).read_text(encoding="utf-8")


class PersistentReviseFinalReviewHarness(ReviseFinalReviewHarness):
    @classmethod
    def _materialize_stub_outputs(cls, invocation):
        super()._materialize_stub_outputs(invocation)
        if invocation.node_type == "final-review-recheck":
            path = Path(invocation.agent_context["outputs"]["review"]["path"])
            path.write_text(
                "## 审查结论\n\nVERDICT: revise\n\n## 问题清单\n\nREPAIR_TARGET: u1 | 仍未修复。\n\n"
                "## 审查说明\n\n第二次审核未通过。\n",
                encoding="utf-8",
            )


def test_second_final_review_revise_is_terminal_after_one_repair(tmp_path):
    _, harness, driver, projection = execute_mode(
        tmp_path, "heavy", harness=PersistentReviseFinalReviewHarness()
    )

    assert projection.status == "failed"
    assert "final review verdict is revise" in projection.error
    assert len([item for item in harness.invocations if item.node_type == "final-repair"]) == 1
    assert len([item for item in harness.invocations if item.node_type == "final-review-recheck"]) == 1
    assert not (driver.output_dir / projection.run_id).exists()


class MutatingPreparedReportHarness(ReviseFinalReviewHarness):
    @classmethod
    def _materialize_stub_outputs(cls, invocation):
        super()._materialize_stub_outputs(invocation)
        if invocation.node_type == "final-review-recheck":
            Path(invocation.agent_context["outputs"]["stitched"]["path"]).write_text(
                "# untrusted rewrite\n", encoding="utf-8"
            )


def test_final_review_recheck_cannot_modify_prepared_stitched_report(tmp_path):
    _, _, driver, projection = execute_mode(
        tmp_path, "heavy", harness=MutatingPreparedReportHarness()
    )

    assert projection.status == "failed"
    assert "modified a trusted prepared output" in projection.error
    assert not (driver.output_dir / projection.run_id).exists()


class FiveDimensionHarness(StubHarness):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.release = asyncio.Event()

    @staticmethod
    def _stub_plan(mode):
        base = dict(StubHarness._stub_plan(mode))
        template = base["dimensions"][0]
        base["dimensions"] = []
        for index in range(1, 6):
            item = json.loads(json.dumps(template))
            item["id"] = f"d{index}"
            item["name"] = f"Dimension {index}"
            item["scope_ownership"]["owns"] = [f"stub fact {index}"]
            item["scope_ownership"]["overlap_policy"] = (
                f"Only d{index} owns stub fact {index}"
            )
            base["dimensions"].append(item)
        return base

    async def invoke(self, invocation):
        if invocation.node_type != "research":
            return await super().invoke(invocation)
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 5:
            self.release.set()
        try:
            await asyncio.wait_for(self.release.wait(), timeout=2)
            return await super().invoke(invocation)
        finally:
            self.active -= 1


def test_each_port_fans_out_and_respects_configured_concurrency(tmp_path):
    harness = FiveDimensionHarness()
    store, _, driver, registry = make_driver(tmp_path, harness, concurrency=5)
    projection = driver.create_run(
        RunRequest("fan out", mode="normal"),
        load_workflow_spec(mode="normal"),
        registry,
    )

    partial = run(driver.drive(projection.run_id, max_steps=6))

    assert partial.status == "running"
    assert harness.peak == 5
    assert len([item for item in harness.invocations if item.node_type == "research"]) == 5


class RepairHarness(StubHarness):
    async def invoke(self, invocation):
        self.invocations.append(invocation)
        assert not list(invocation.workspace.parent.rglob("validate.py"))
        path = Path(invocation.agent_context["outputs"]["report"]["path"])
        path.write_text("bad" if invocation.attempt == 1 else "good", encoding="utf-8")
        return AgentExecutionResult(status="succeeded")


def test_custom_agent_validator_gets_one_bounded_repair_attempt(tmp_path):
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "custom-report.md").write_text("Write a good report.", encoding="utf-8")
    (nodes / "custom-report-validate.py").write_text(
        "import json, os, pathlib, sys\n"
        "c=json.loads(pathlib.Path(os.environ['DEEPRESEARCH_NODE_CONTEXT']).read_text())\n"
        "sys.exit(0 if pathlib.Path(c['outputs']['report']['path']).read_text()=='good' else 1)\n",
        encoding="utf-8",
    )
    (nodes / "custom-report.yaml").write_text(
        "version: 1\nid: custom-report\nkind: agent\nprompt: custom-report.md\ninputs: {}\n"
        "outputs:\n  report:\n    path: report.md\n    type: report\n"
        "    media_type: text/markdown\n    primary: true\n"
        "validator: [python, ./custom-report-validate.py]\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "custom.yaml"
    workflow.write_text(
        "version: 1\nname: custom\nsteps: [custom-report]\nresult: report\n",
        encoding="utf-8",
    )
    harness = RepairHarness()
    store, _, driver, _ = make_driver(tmp_path, harness)
    registry = NodeRegistry.load([nodes])
    projection = driver.create_run(
        RunRequest("repair", mode="custom"),
        load_workflow_spec(path=workflow),
        registry,
    )
    projection = run(driver.drive(projection.run_id))

    assert projection.status == "completed"
    assert [item.attempt for item in harness.invocations] == [1, 2]
    assert "never use terminal commands" in harness.invocations[0].prompt
    assert "This is a fresh attempt workspace" in harness.invocations[1].prompt
    assert "Recreate every required declared output" in harness.invocations[1].prompt
    assert (tmp_path / "output" / projection.run_id / "report.md").read_text() == "good"


def test_custom_script_node_uses_the_same_context_and_artifact_publication(tmp_path):
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "script-report.py").write_text(
        "import json, os, pathlib\n"
        "c=json.loads(pathlib.Path(os.environ['DEEPRESEARCH_NODE_CONTEXT']).read_text())\n"
        "pathlib.Path(c['outputs']['report']['path']).write_text('# Script report\\n')\n",
        encoding="utf-8",
    )
    (nodes / "script-report.yaml").write_text(
        "version: 1\nid: script-report\nkind: script\ncommand: [python, ./script-report.py]\n"
        "inputs: {}\noutputs:\n  report:\n    path: report.md\n    type: report\n"
        "    media_type: text/markdown\n    primary: true\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "script.yaml"
    workflow.write_text(
        "version: 1\nname: script\nsteps: [script-report]\nresult: report\n",
        encoding="utf-8",
    )
    store, harness, driver, _ = make_driver(tmp_path)
    projection = driver.create_run(
        RunRequest("script", mode="script"),
        load_workflow_spec(path=workflow),
        NodeRegistry.load([nodes]),
    )

    projection = run(driver.drive(projection.run_id))

    assert projection.status == "completed"
    assert harness.invocations == []
    assert (tmp_path / "output" / projection.run_id / "report.md").read_text() == "# Script report\n"


def test_manifest_snapshot_tampering_is_rejected(tmp_path):
    store, _, driver, projection = execute_mode(tmp_path, "quick")
    manifest_path = store.run_dir(projection.run_id) / "run.json"
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow"]["name"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ManifestIntegrityError, match="hash mismatch"):
        driver.load_projection(projection.run_id)
