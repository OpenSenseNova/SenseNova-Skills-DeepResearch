import copy

import pytest

from deepresearch_cli.harness.stub import StubHarness
from deepresearch_cli.stitching import (
    StitchContractError,
    assemble_report,
)


def outline(*, show_heading=True, mode="checklist"):
    value = copy.deepcopy(StubHarness._stub_outline("d1.c1"))
    unit = value["content_units"][0]
    unit["render_contract"]["show_heading"] = show_heading
    unit["render_contract"]["mode"] = mode
    return value


def test_deterministic_stitch_preserves_citations_and_contract_heading():
    value = outline()
    draft = "## Stub check\n\n- [x] Stub fact: Supported.[^d1_s1]\n"

    result = assemble_report(
        query="Check the deterministic result.",
        language="en",
        outline=value,
        drafts={"u1": draft},
    )
    assert result.startswith("# Check the deterministic result\n")
    assert "## Stub check" in result
    assert result.count("[^d1_s1]") == 1
    assert "STITCH_UNIT" not in result


def test_deterministic_stitch_rejects_draft_unit_mismatch():
    with pytest.raises(StitchContractError, match="draft/unit mismatch"):
        assemble_report(
            query="Test",
            language="en",
            outline=outline(),
            drafts={"u2": "## Stub check\n\n- [x] Result"},
        )


def test_deterministic_stitch_checks_markdown_table_schema():
    value = outline(mode="markdown_table")
    value["content_units"][0]["render_contract"]["schema"] = ["Name", "Value"]
    invalid = "## Stub check\n\n| Name |\n| --- |\n| Stub |\n"

    with pytest.raises(StitchContractError, match="contracted schema"):
        assemble_report(
            query="Test",
            language="en",
            outline=value,
            drafts={"u1": invalid},
        )


def test_deterministic_stitch_rejects_orphaned_source_citation():
    with pytest.raises(StitchContractError, match="routed evidence"):
        assemble_report(
            query="Test",
            language="en",
            outline=outline(),
            drafts={"u1": "## Stub check\n\n- [x] Result.[^missing_source]"},
            allowed_source_ids={"d1_s1"},
        )


def test_hidden_heading_units_are_concatenated_without_model_polish():
    value = outline(show_heading=False)
    value["content_units"].append(
        {
            **copy.deepcopy(value["content_units"][0]),
            "id": "u2",
            "title": "Hidden boundary",
            "render_contract": {
                **copy.deepcopy(value["content_units"][0]["render_contract"]),
                "show_heading": False,
            },
        }
    )
    result = assemble_report(
        query="Test",
        language="en",
        outline=value,
        drafts={
            "u1": "- [x] Stub fact: First.[^d1_s1]",
            "u2": "- [x] Stub fact: Second.[^d1_s2]",
        },
    )

    assert "- [x] Stub fact: First.[^d1_s1]\n\n- [x] Stub fact: Second" in result
    assert result.count("[^d1_s1]") == 1
    assert result.count("[^d1_s2]") == 1


def test_deterministic_stitch_reports_unit_element_and_claim_for_missing_citation():
    value = outline()
    draft = "## Stub check\n\n- [x] Stub fact is supported.\n"

    with pytest.raises(
        StitchContractError,
        match=r"UNIT_CITATION_COVERAGE unit=u1 element=e1 claim=d1\.c1",
    ):
        assemble_report(
            query="Test",
            language="en",
            outline=value,
            drafts={"u1": draft},
            allowed_source_ids={"d1_s1"},
            routed_claim_sources={"d1.c1": {"d1_s1"}},
        )


def test_element_citation_cannot_be_borrowed_from_another_element():
    value = outline()
    unit = value["content_units"][0]
    unit["elements"].append(
        {
            "id": "e2",
            "label": "Second fact",
            "purpose": "Check the second fact",
            "evidence_refs": [{"claim_id": "d1.c2", "role": "primary_support"}],
            "writing_context_refs": [],
        }
    )
    draft = (
        "## Stub check\n\n"
        "- [x] Stub fact is supported.[^d1_s1][^d1_s2]\n"
        "- [x] Second fact is supported.\n"
    )

    with pytest.raises(StitchContractError, match=r"element=e2 claim=d1\.c2"):
        assemble_report(
            query="Test",
            language="en",
            outline=value,
            drafts={"u1": draft},
            allowed_source_ids={"d1_s1", "d1_s2"},
            routed_claim_sources={"d1.c1": {"d1_s1"}, "d1.c2": {"d1_s2"}},
        )


def test_markdown_table_required_field_needs_inline_citation():
    value = outline(mode="markdown_table")
    render = value["content_units"][0]["render_contract"]
    render["schema"] = ["Item", "Value"]
    render["citation_policy"]["required_fields"] = ["Value"]
    draft = (
        "## Stub check\n\n"
        "| Item | Value |\n| --- | --- |\n"
        "| Stub fact | Supported |\n"
    )

    with pytest.raises(StitchContractError, match="UNIT_TABLE_CELL_CITATION"):
        assemble_report(
            query="Test",
            language="en",
            outline=value,
            drafts={"u1": draft},
            routed_claim_sources={"d1.c1": {"d1_s1"}},
        )


def test_secondary_structure_rejects_uncontracted_agent_heading():
    value = outline()
    draft = "## Stub check\n\n- [x] Stub fact.[^d1_s1]\n\n### Agent summary\n\nExtra.\n"

    with pytest.raises(StitchContractError, match="UNIT_SECONDARY_STRUCTURE"):
        assemble_report(
            query="Test",
            language="en",
            outline=value,
            drafts={"u1": draft},
        )
