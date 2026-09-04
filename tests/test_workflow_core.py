from pathlib import Path

import pytest

from deepresearch_cli.config import (
    CompiledWorkflow,
    NodeRegistry,
    WorkflowSpec,
    WorkflowCompileError,
    compile_workflow,
    load_workflow_spec,
)
from deepresearch_cli.config.registry import NodeRegistryError
from deepresearch_cli.config.registry import builtin_node_dir
from deepresearch_cli.config.workflows import (
    WorkflowLoadError,
    builtin_workflow_dir,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_builtin_configuration_has_one_root_directory():
    assert builtin_node_dir() == PROJECT_ROOT / "config" / "nodes"
    assert builtin_workflow_dir() == PROJECT_ROOT / "config" / "workflows"


def test_builtin_registry_exposes_capabilities_as_nodes():
    registry = NodeRegistry.load()

    assert {item.node_id for item in registry.list()} == {
        "final-repair",
        "final-review",
        "final-review-diagnostic",
        "final-review-recheck",
        "md-html",
        "md-docx",
        "md-pdf",
        "perspective",
        "plan",
        "render",
        "report-planner",
        "report-writer",
        "research",
        "review",
        "scout",
        "stitcher",
        "supplement-planner",
    }
    html = registry.get("md-html")
    assert html.kind == "agent"
    assert html.prompt and "先写" in html.prompt
    assert html.output("report").media_type == "text/html"
    assert registry.get("md-docx").kind == "script"
    assert registry.get("md-docx").output("document").media_type.endswith(
        "wordprocessingml.document"
    )
    assert registry.get("md-pdf").output("document").media_type == "application/pdf"
    assert "reference.docx" in registry.get("md-docx").resources
    assert "report.typ" in registry.get("md-pdf").resources


def test_quick_workflow_is_a_plain_ordered_yaml_list():
    registry = NodeRegistry.load()
    spec = load_workflow_spec(mode="quick")
    compiled = compile_workflow(spec, registry, output_format="markdown")

    assert list(spec.steps) == ["research", "report-writer", "render"]
    assert [item.step_id for item in compiled.steps] == [
        "research",
        "report-writer",
        "render",
    ]
    assert compiled.result_type == "report"
    assert compiled.result_media_type == "text/markdown"
    assert {item.step_id: item.timeout_seconds for item in compiled.steps} == {
        "research": 1800.0,
        "report-writer": 300.0,
        "render": None,
    }


def test_repeating_yaml_nodes_builds_the_second_research_cycle():
    registry = NodeRegistry.load()
    compiled = compile_workflow(
        load_workflow_spec(mode="heavy"), registry, output_format="markdown"
    )

    ids = [item.step_id for item in compiled.steps]
    assert ids == [
        "scout",
        "plan",
        "research",
        "review",
        "perspective",
        "supplement-planner",
        "research-2",
        "review-2",
        "perspective-2",
        "report-planner",
        "report-writer",
        "stitcher",
        "final-review-diagnostic",
        "final-repair",
        "final-review-recheck",
        "render",
    ]
    research_2 = next(item for item in compiled.steps if item.step_id == "research-2")
    research_1 = next(item for item in compiled.steps if item.step_id == "research")
    assert research_1.timeout_seconds == 1800.0
    assert research_2.timeout_seconds == 1800.0
    task = next(item for item in research_2.bindings if item.port == "task")
    assert task.mode == "each"
    assert task.source_step_id == "supplement-planner"
    perspective_2 = next(
        item for item in compiled.steps if item.step_id == "perspective-2"
    )
    evidence = next(item for item in perspective_2.bindings if item.port == "evidence")
    assert evidence.mode == "each"
    assert evidence.source_step_id == "research-2"


def test_custom_workflow_can_call_deterministic_stitcher_directly():
    workflow = WorkflowSpec(
        name="legacy-custom",
        steps=(
            "plan",
            "research",
            "report-planner",
            "report-writer",
            "stitcher",
            "final-review",
            "render",
        ),
        result="report",
    )

    compiled = compile_workflow(workflow, NodeRegistry.load(), output_format="markdown")

    stitcher = next(item for item in compiled.steps if item.step_id == "stitcher")
    assert next(item for item in stitcher.bindings if item.port == "drafts").required is True
    assert next(item for item in stitcher.bindings if item.port == "evidence").required is True


def test_html_format_appends_the_builtin_html_node_once():
    registry = NodeRegistry.load()
    compiled = compile_workflow(
        load_workflow_spec(mode="quick"), registry, output_format="html"
    )
    assert [item.step_id for item in compiled.steps][-2:] == ["render", "md-html"]
    assert compiled.result_media_type == "text/html"
    assert compiled.steps[-1].timeout_seconds == 900.0


def test_pdf_format_appends_markdown_pdf_node():
    compiled = compile_workflow(
        load_workflow_spec(mode="quick"), NodeRegistry.load(), output_format="pdf"
    )

    assert [item.step_id for item in compiled.steps][-2:] == ["render", "md-pdf"]
    pdf_input = compiled.steps[-1].bindings[0]
    assert pdf_input.artifact_type == "report"
    assert compiled.result_type == "report-pdf"
    assert compiled.result_media_type == "application/pdf"
    assert compiled.steps[-1].timeout_seconds is None


def test_docx_format_appends_docx_node():
    compiled = compile_workflow(
        load_workflow_spec(mode="quick"), NodeRegistry.load(), output_format="docx"
    )

    assert [item.step_id for item in compiled.steps][-2:] == ["render", "md-docx"]
    assert compiled.result_type == "report-docx"
    assert compiled.result_media_type.endswith("wordprocessingml.document")
    assert compiled.steps[-1].timeout_seconds is None


def test_explicit_trailing_html_node_is_not_appended_twice(tmp_path):
    workflow = tmp_path / "html.yaml"
    workflow.write_text(
        "version: 1\nname: html\n"
        "steps: [research, report-writer, render, md-html]\nresult: report\n",
        encoding="utf-8",
    )

    compiled = compile_workflow(
        load_workflow_spec(path=workflow), NodeRegistry.load(), output_format="html"
    )

    assert [item.step_id for item in compiled.steps].count("md-html") == 1


def test_custom_workflow_accepts_only_the_optional_timeouts_field(tmp_path):
    path = tmp_path / "workflow.yaml"
    path.write_text(
        "version: 1\nname: custom\nsteps: [research, report-writer, render]\n"
        "timeouts: {research: 42}\nresult: report\n",
        encoding="utf-8",
    )
    loaded = load_workflow_spec(path=path)
    assert loaded.name == "custom"
    assert loaded.timeouts == {"research": 42.0}

    path.write_text(path.read_text(encoding="utf-8") + "policy: hidden-language\n")
    with pytest.raises(WorkflowLoadError, match="only optional field"):
        load_workflow_spec(path=path)


@pytest.mark.parametrize("timeout", [0, -1, True, "slow", float("inf")])
def test_workflow_rejects_invalid_step_timeouts(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        WorkflowSpec(
            name="invalid-timeout",
            steps=("research", "report-writer", "render"),
            result="report",
            timeouts={"research": timeout},
        )


def test_compiler_rejects_timeout_for_an_unknown_node():
    workflow = WorkflowSpec(
        name="unknown-timeout",
        steps=("research", "report-writer", "render"),
        result="report",
        timeouts={"missing-node": 30},
    )

    with pytest.raises(WorkflowCompileError, match="unknown agent nodes: missing-node"):
        compile_workflow(workflow, NodeRegistry.load(), output_format="markdown")


def test_compiler_rejects_per_occurrence_timeout_override():
    workflow = WorkflowSpec(
        name="repeated-timeout",
        steps=("research", "research", "report-writer", "render"),
        result="report",
        timeouts={"research": 30, "research-2": 20},
    )

    with pytest.raises(WorkflowCompileError, match="unknown agent nodes: research-2"):
        compile_workflow(workflow, NodeRegistry.load(), output_format="markdown")


def test_compiler_rejects_timeout_for_a_deterministic_script_node():
    workflow = WorkflowSpec(
        name="script-timeout",
        steps=("research", "report-writer", "render"),
        result="report",
        timeouts={"render": 30},
    )

    with pytest.raises(WorkflowCompileError, match="deterministic script node render"):
        compile_workflow(workflow, NodeRegistry.load(), output_format="markdown")


def test_compiled_workflow_round_trip_preserves_step_timeouts():
    compiled = compile_workflow(
        load_workflow_spec(mode="normal"),
        NodeRegistry.load(),
        output_format="markdown",
    )

    restored = CompiledWorkflow.from_dict(compiled.to_dict())

    assert [item.timeout_seconds for item in restored.steps] == [
        600.0,
        1800.0,
        420.0,
        None,
    ]


def test_node_config_rejects_unknown_configuration_language(tmp_path):
    (tmp_path / "sample.md").write_text("Write the output.", encoding="utf-8")
    (tmp_path / "sample.yaml").write_text(
        "version: 1\nid: sample\nkind: agent\nprompt: sample.md\ninputs: {}\n"
        "outputs:\n  report:\n    path: report.md\n    type: report\n"
        "    media_type: text/markdown\n    primary: true\ntransition: magic\n",
        encoding="utf-8",
    )
    with pytest.raises(NodeRegistryError, match="unknown keys: transition"):
        NodeRegistry.load([tmp_path])


def test_node_config_rejects_removed_skill_field(tmp_path):
    (tmp_path / "legacy.yaml").write_text(
        "version: 1\nid: legacy\nkind: agent\nskill: old-skill\ninputs: {}\n"
        "outputs:\n  report:\n    path: report.md\n    type: report\n"
        "    media_type: text/markdown\n    primary: true\n",
        encoding="utf-8",
    )
    with pytest.raises(NodeRegistryError, match="unknown keys: skill"):
        NodeRegistry.load([tmp_path])


def test_node_config_rejects_validator_and_validators_together(tmp_path):
    (tmp_path / "sample.md").write_text("Write the output.", encoding="utf-8")
    (tmp_path / "sample.yaml").write_text(
        "version: 1\nid: sample\nkind: agent\nprompt: sample.md\ninputs: {}\n"
        "outputs:\n  report:\n    path: report.md\n    type: report\n"
        "    media_type: text/markdown\n    primary: true\n"
        "validator: [python, -c, pass]\n"
        "validators:\n  - [python, -c, pass]\n",
        encoding="utf-8",
    )

    with pytest.raises(NodeRegistryError, match="cannot define both validator and validators"):
        NodeRegistry.load([tmp_path])


def test_compiler_rejects_a_required_input_without_a_producer(tmp_path):
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "consumer.md").write_text("Write output.", encoding="utf-8")
    (nodes / "consumer.yaml").write_text(
        "version: 1\nid: consumer\nkind: agent\nprompt: consumer.md\n"
        "inputs:\n  source:\n    type: missing\n    media_type: text/plain\n    mode: one\n"
        "outputs:\n  report:\n    path: report.md\n    type: report\n"
        "    media_type: text/markdown\n    primary: true\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "version: 1\nname: broken\nsteps: [consumer]\nresult: report\n",
        encoding="utf-8",
    )
    registry = NodeRegistry.load([nodes])
    with pytest.raises(WorkflowCompileError, match="no compatible producer"):
        compile_workflow(
            load_workflow_spec(path=workflow), registry, output_format="markdown"
        )
