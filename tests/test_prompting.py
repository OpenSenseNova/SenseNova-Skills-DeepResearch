import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from deepresearch_cli.config import NodeRegistry
from deepresearch_cli.harness.stub import StubHarness
from deepresearch_cli.prompting import PromptBundle


PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


def _load_contract_validator(name):
    path = PROMPT_DIR / "contracts" / "validators" / f"validate_{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}_validator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_prompt_resources_are_loaded_into_self_contained_node_specs():
    bundle = PromptBundle.load(PROMPT_DIR)
    registry = NodeRegistry.load()

    assert bundle.bundle_id == "deepresearch-basic"
    assert len(bundle.node_prompts) == 11
    assert registry.get("research").prompt == bundle.node_prompts["Research"]
    assert "prepare_citations.py" in registry.get("render").resources
    assert registry.get("research").validator == ()
    assert registry.get("research").validators == (
        ("python", "resource:validate_evidence.py", "--node-context"),
        ("python", "resource:validate_supplement_plan.py", "--node-context"),
    )
    assert {
        "evidence.schema.md",
        "supplement_plan.schema.md",
        "validate_evidence.py",
        "validate_supplement_plan.py",
    } <= set(registry.get("research").resources)
    assert {"plan.schema.md", "validate_plan.py"} <= set(
        registry.get("plan").resources
    )
    assert registry.get("report-writer").validator == (
        "python", "-m", "deepresearch_cli.node_validators.report_writer"
    )
    assert "report_templates.yaml" in registry.get("report-planner").resources
    assert "report_templates.yaml" in registry.get("report-writer").resources
    assert registry.get("stitcher").validator == (
        "python", "-m", "deepresearch_cli.node_validators.stitcher"
    )
    assert registry.get("final-repair").prompt == bundle.node_prompts["FinalRepair"]


def test_run_snapshot_contains_native_node_prompts_without_skill_fields():
    snapshot = NodeRegistry.load().snapshot_for(["research", "md-html"])
    research, html = snapshot

    assert "# Research Agent" in research["prompt"]
    assert research["validator"] == []
    assert research["validators"] == [
        ["python", "resource:validate_evidence.py", "--node-context"],
        ["python", "resource:validate_supplement_plan.py", "--node-context"],
    ]
    assert "# Report HTML" in html["prompt"]
    assert "# Reference: Design Contract" in html["prompt"]
    assert "references/" not in html["prompt"]
    assert "skill" not in html
    assert "skill_references" not in html


def test_research_prompt_requires_native_declared_output_writes():
    research = NodeRegistry.load().get("research").prompt

    assert "`write_file`" in research
    assert "不得通过 terminal、shell 重定向、heredoc" in research
    assert "terminal 只用于读取、抓取或运行校验" in research


def test_report_writer_prompt_matches_mode_specific_h1_validator_contract():
    prompt = NodeRegistry.load().get("report-writer").prompt

    assert "第一条非空行必须是全文唯一的 H1" in prompt
    assert "`quick_synthesis` 以全文唯一的 H1 开头" in prompt
    assert "`write_unit` 不含 H1" in prompt
    assert "文件内不出现 H1" not in prompt


def test_report_prompts_prioritize_explicit_user_structure_over_templates():
    registry = NodeRegistry.load()
    planner = registry.get("report-planner").prompt
    writer = registry.get("report-writer").prompt

    assert "最高优先级硬合同" in planner
    assert "模板与用户结构发生冲突时无条件服从用户结构" in planner
    assert "Template 只用于参考和覆盖检查" in writer
    assert "Template 与用户要求冲突时服从用户要求" in writer


def test_agent_visible_contracts_retain_exact_validator_boundaries():
    registry = NodeRegistry.load()

    review_prompt = registry.get("review").prompt
    for heading in (
        "## 审查结论",
        "## 问题清单",
        "## 核验记录",
        "## 审查说明",
    ):
        assert heading in review_prompt
    assert "它们都是二级标题" in review_prompt

    perspective = registry.get("perspective")
    perspective_schema = perspective.resources["perspective_feedback.schema.md"]
    assert "第 1 项是 `l1`，第 2 项是 `l2`" in perspective_schema
    assert "### l{N}: {axis}:{value}" in perspective_schema
    assert "不得使用 dimension 前缀" in perspective.prompt

    outline_schema = registry.get("report-planner").resources["outline.schema.md"]
    outline_validator = _load_contract_validator("outline")
    for value in outline_validator.PARADIGM_VALUES:
        assert value in outline_schema
    assert "合理的扩展值不会阻断下游" in outline_schema


def _validate_evidence(tmp_path, value):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    validator = PROMPT_DIR / "contracts" / "validators" / "validate_evidence.py"
    completed = subprocess.run(
        [sys.executable, "-I", str(validator), str(evidence), "--expected-mode", "quick"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, json.loads(completed.stdout)


def test_evidence_contract_keeps_structural_failures_strict(tmp_path):
    evidence = copy.deepcopy(StubHarness._stub_evidence("d1", "quick"))
    evidence.pop("headline")

    completed, payload = _validate_evidence(tmp_path, evidence)

    assert completed.returncode != 0
    assert payload["ok"] is False
    assert any(item["rule"] == "V003" for item in payload["errors"])


def test_evidence_contract_keeps_quality_findings_non_blocking(tmp_path):
    evidence = copy.deepcopy(StubHarness._stub_evidence("d1", "quick"))
    evidence["sources"][0]["quality"] = "tertiary"

    completed, payload = _validate_evidence(tmp_path, evidence)

    assert completed.returncode == 0
    assert payload["ok"] is True
    assert any(item["rule"] == "V040" for item in payload["warnings"])


def test_evidence_contract_accepts_nonsequential_ids_and_extension_metadata(tmp_path):
    evidence = copy.deepcopy(StubHarness._stub_evidence("d1", "quick"))
    evidence["sources"][0]["id"] = "d1_official_source"
    evidence["claims"][0]["evidence"][0]["source_id"] = "d1_official_source"
    evidence["claims"][0]["evidence"][0]["snapshot_ref"] = "internal-cache-key"

    completed, payload = _validate_evidence(tmp_path, evidence)

    assert completed.returncode == 0
    assert payload["ok"] is True


def test_evidence_contract_still_rejects_a_broken_source_reference(tmp_path):
    evidence = copy.deepcopy(StubHarness._stub_evidence("d1", "quick"))
    evidence["claims"][0]["evidence"][0]["source_id"] = "d1_missing_source"

    completed, payload = _validate_evidence(tmp_path, evidence)

    assert completed.returncode == 1
    assert any(item["rule"] == "V031" for item in payload["errors"])


def test_briefing_contract_accepts_empty_discovery_collections():
    briefing = copy.deepcopy(StubHarness._stub_briefing())
    briefing["context_entities"] = []
    briefing["subdomain_partitions"]["subdomains"] = []
    briefing["knowledge_topology"]["consensus"] = []
    briefing["knowledge_topology"]["disputes"] = []
    briefing["candidate_lenses"] = []
    briefing["hypotheses_to_test"] = []
    briefing["risk_flags"] = []

    assert not _load_contract_validator("briefing").validate(briefing)


def test_json_contracts_allow_unconsumed_extension_fields():
    briefing = copy.deepcopy(StubHarness._stub_briefing())
    briefing["invented"] = True
    assert not _load_contract_validator("briefing").validate(briefing)

    plan = copy.deepcopy(StubHarness._stub_plan("heavy"))
    plan["invented"] = True
    assert not _load_contract_validator("plan").validate(plan)

    evidence = copy.deepcopy(StubHarness._stub_evidence("d1", "quick"))
    evidence["invented"] = True
    assert not _load_contract_validator("evidence").validate(
        evidence, expected_mode="quick"
    )

    supplement = {
        "dimension_id": "d1",
        "supplement_items": [],
        "deferred_items": [],
        "invented": True,
    }
    assert not _load_contract_validator("supplement_plan").validate(supplement)
