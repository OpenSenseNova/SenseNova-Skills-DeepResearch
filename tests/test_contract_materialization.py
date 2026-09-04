import importlib.util
import json
from copy import deepcopy
from pathlib import Path

from deepresearch_cli.harness.stub import StubHarness


ROOT = Path(__file__).resolve().parents[1]


def load_outline_validator():
    path = ROOT / "prompts/contracts/validators/validate_outline.py"
    spec = importlib.util.spec_from_file_location("formal_outline_validator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def valid_outline():
    return deepcopy(StubHarness._stub_outline("d1.c1"))


def test_outline_report_profile_matches_request_and_template_catalog():
    validator = load_outline_validator()
    outline = valid_outline()
    outline["report_profile"] = {
        "format": "formal_report",
        "template_id": "market_analysis",
    }

    errors, _ = validator.validate_outline(
        outline,
        expected_report_format="formal_report",
        template_ids={"market_analysis", "general_research"},
    )

    assert errors == []


def test_outline_rejects_missing_formal_template():
    validator = load_outline_validator()
    outline = valid_outline()
    outline["report_profile"] = {"format": "formal_report", "template_id": None}

    errors, _ = validator.validate_outline(
        outline, expected_report_format="formal_report", template_ids={"general_research"}
    )

    assert any(error["rule"] == "U005" for error in errors)


def test_outline_rejects_template_for_brief():
    validator = load_outline_validator()
    outline = valid_outline()
    outline["report_profile"]["format"] = "brief"
    outline["report_profile"]["template_id"] = "general_research"

    errors, _ = validator.validate_outline(
        outline, expected_report_format="brief", template_ids={"general_research"}
    )

    assert any(error["rule"] == "U005" for error in errors)


def claim(claim_id, source_id):
    return {
        "id": claim_id,
        "text": f"Claim {claim_id}",
        "kind": "factual",
        "polarity": "neutral",
        "topic_tag": "test_claim",
        "narrative_role": "primary_support",
        "evidence": [
            {"source_id": source_id, "snippet": "support", "quote_type": "direct"}
        ],
    }


def source(source_id):
    return {
        "id": source_id,
        "url": f"https://example.com/{source_id}",
        "title": source_id,
        "quality": "primary",
    }


def test_outline_rejects_agent_supplied_evidence_subset():
    validator = load_outline_validator()
    outline = valid_outline()
    outline["content_units"][0]["evidence_subset"] = ["d1.c1"]

    errors, _ = validator.validate_outline(outline)

    assert any(
        item["rule"] == "U040" and item.get("fields") == ["evidence_subset"]
        for item in errors
    )


def test_subset_rejects_extra_claims_and_extra_sources():
    validator = load_outline_validator()
    outline = valid_outline()
    subset = {
        "claims": [claim("d1.c1", "d1_s1"), claim("d1.c2", "d1_s2")],
        "writing_context": [],
        "sources": [source("d1_s1"), source("d1_s2"), source("d1_s3")],
    }

    errors = validator.validate_subset(
        subset,
        outline,
        {"d1.c1": {}, "d1.c2": {}},
        {},
        {"d1_s1": source("d1_s1"), "d1_s2": source("d1_s2"), "d1_s3": source("d1_s3")},
        "u1",
    )

    assert any(
        item["rule"] == "U210" and item.get("extra_claim_ids") == ["d1.c2"]
        for item in errors
    )
    assert any(
        item["rule"] == "U211" and item.get("extra") == ["d1_s3"]
        for item in errors
    )


def test_subset_allows_unconsumed_extension_fields():
    validator = load_outline_validator()
    outline = valid_outline()
    subset = {
        "claims": [claim("d1.c1", "d1_s1")],
        "writing_context": [],
        "sources": [source("d1_s1")],
        "invented": True,
    }

    errors = validator.validate_subset(subset, outline, expected_unit_id="u1")

    assert not errors


def test_outline_allows_unconsumed_citation_style():
    validator = load_outline_validator()
    outline = valid_outline()
    outline["style_contract"]["citation_style"] = "inline"

    errors, _ = validator.validate_outline(outline)

    assert not errors


def test_outline_allows_unconsumed_nested_fields():
    validator = load_outline_validator()
    outline = valid_outline()
    outline["content_units"][0]["render_contract"]["invented"] = True

    errors, _ = validator.validate_outline(outline)

    assert not errors


def test_outline_still_requires_writer_render_fields():
    validator = load_outline_validator()
    outline = valid_outline()
    del outline["content_units"][0]["render_contract"]

    errors, _ = validator.validate_outline(outline)

    assert any(item["rule"] == "U050" for item in errors)


def test_outline_requires_machine_readable_citation_and_secondary_contracts():
    validator = load_outline_validator()
    outline = valid_outline()
    render = outline["content_units"][0]["render_contract"]
    del render["citation_policy"]
    del render["secondary_structure"]

    errors, _ = validator.validate_outline(outline)

    assert any(item["rule"] == "U054" for item in errors)
    assert any(item["rule"] == "U055" for item in errors)


def test_outline_rejects_table_citation_fields_outside_schema():
    validator = load_outline_validator()
    outline = valid_outline()
    render = outline["content_units"][0]["render_contract"]
    render["mode"] = "markdown_table"
    render["citation_policy"]["required_fields"] = ["missing"]

    errors, _ = validator.validate_outline(outline)

    assert any(item["rule"] == "U054" and item.get("unknown") == ["missing"] for item in errors)


def test_builtin_runtime_materializes_exact_tasks_and_content_subsets(tmp_path):
    # Import locally to reuse the integration driver's public execution helper.
    from tests.test_driver_integration import execute_mode

    _, harness, _, projection = execute_mode(tmp_path, "heavy")
    assert projection.status == "completed"

    research = next(item for item in harness.invocations if item.node_type == "research")
    task = json.loads(Path(research.agent_context["inputs"]["task"][0]["path"]).read_text())
    plan = json.loads(Path(research.agent_context["inputs"]["plan"][0]["path"]).read_text())
    assert task == plan["dimensions"][0]

    writer = next(item for item in harness.invocations if item.node_type == "report-writer")
    subset = json.loads(Path(writer.agent_context["inputs"]["task"][0]["path"]).read_text())
    assert [item["id"] for item in subset["claims"]] == ["d1.c1"]
    assert [item["id"] for item in subset["sources"]] == ["d1_s1"]
