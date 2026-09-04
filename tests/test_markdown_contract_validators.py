from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_validator(module: str, context: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DEEPRESEARCH_NODE_CONTEXT"] = str(context)
    return subprocess.run(
        [sys.executable, "-m", module],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def _report_writer_context(
    tmp_path: Path,
    draft_text: str,
    *,
    write_unit: bool = False,
) -> Path:
    draft = tmp_path / "draft.md"
    draft.write_text(draft_text, encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"sources": []}), encoding="utf-8")
    inputs: dict[str, list[dict[str, str]]] = {
        "evidence": [{"path": str(evidence)}]
    }
    context_value: dict[str, object] = {
        "scope": {},
        "inputs": inputs,
        "outputs": {"draft": {"path": str(draft)}},
    }
    if write_unit:
        task = tmp_path / "task.json"
        task.write_text(json.dumps({"sources": []}), encoding="utf-8")
        outline = tmp_path / "outline.json"
        outline.write_text(
            json.dumps(
                {
                    "content_units": [
                        {
                            "id": "u1",
                            "title": "Unit title",
                            "render_contract": {
                                "show_heading": True,
                                "mode": "prose",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        inputs["task"] = [{"path": str(task)}]
        inputs["outline"] = [{"path": str(outline)}]
        context_value["scope"] = {"content-unit-id": "u1"}
    context = tmp_path / "context.json"
    context.write_text(json.dumps(context_value), encoding="utf-8")
    return context


def test_report_writer_quick_synthesis_requires_one_leading_h1(tmp_path: Path) -> None:
    valid = _report_writer_context(tmp_path, "# Report title\n\n## Summary\n\nBody.\n")
    completed = _run_validator(
        "deepresearch_cli.node_validators.report_writer", valid
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    invalid = _report_writer_context(tmp_path, "## Summary\n\nBody.\n")
    completed = _run_validator(
        "deepresearch_cli.node_validators.report_writer", invalid
    )
    assert completed.returncode == 1
    assert "must start with exactly one H1" in completed.stdout


def test_report_writer_content_unit_forbids_h1(tmp_path: Path) -> None:
    context = _report_writer_context(
        tmp_path,
        "# Report title\n\n## Unit title\n\nBody.\n",
        write_unit=True,
    )

    completed = _run_validator(
        "deepresearch_cli.node_validators.report_writer", context
    )

    assert completed.returncode == 1
    assert "content-unit draft must not contain an H1" in completed.stdout


def test_report_writer_reports_exact_unit_element_and_claim_for_missing_citation(
    tmp_path: Path,
) -> None:
    context = _report_writer_context(
        tmp_path,
        "## Unit title\n\n- [x] Required fact is present.\n",
        write_unit=True,
    )
    context_value = json.loads(context.read_text(encoding="utf-8"))
    task_path = Path(context_value["inputs"]["task"][0]["path"])
    task_path.write_text(
        json.dumps(
            {
                "claims": [
                    {
                        "id": "d1.c1",
                        "evidence": [{"source_id": "d1_s1"}],
                    }
                ],
                "sources": [{"id": "d1_s1"}],
            }
        ),
        encoding="utf-8",
    )
    outline_path = Path(context_value["inputs"]["outline"][0]["path"])
    outline = json.loads(outline_path.read_text(encoding="utf-8"))
    unit = outline["content_units"][0]
    unit["render_contract"].update(
        {
            "mode": "checklist",
            "citation_policy": {
                "scope": "element",
                "require_each_claim": True,
                "required_fields": [],
            },
            "secondary_structure": {
                "allowed": False,
                "required": False,
                "heading_level": None,
            },
        }
    )
    unit["elements"] = [
        {
            "id": "e1",
            "label": "Required fact",
            "evidence_refs": [{"claim_id": "d1.c1", "role": "primary_support"}],
        }
    ]
    outline_path.write_text(json.dumps(outline), encoding="utf-8")

    completed = _run_validator("deepresearch_cli.node_validators.report_writer", context)
    payload = json.loads(completed.stdout)

    assert completed.returncode == 1
    issue = next(item for item in payload["errors"] if item["rule"] == "UNIT_CITATION_COVERAGE")
    assert issue["unit_id"] == "u1"
    assert issue["element_id"] == "e1"
    assert issue["claim_id"] == "d1.c1"
    assert issue["expected_source_ids"] == ["d1_s1"]


def test_review_validator_accepts_noncanonical_section_headings(tmp_path: Path) -> None:
    review = tmp_path / "review.md"
    review.write_text(
        "# Evidence audit\n\n"
        "## Result\n\nVERDICT: pass\n\n"
        "## Findings\n\nNo blocking issue.\n",
        encoding="utf-8",
    )
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps({"outputs": {"review": {"path": str(review)}}}),
        encoding="utf-8",
    )

    completed = _run_validator("deepresearch_cli.node_validators.review", context)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_review_validator_still_requires_a_verdict(tmp_path: Path) -> None:
    review = tmp_path / "review.md"
    review.write_text("# Evidence audit\n\nNo verdict was emitted.\n", encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps({"outputs": {"review": {"path": str(review)}}}),
        encoding="utf-8",
    )

    completed = _run_validator("deepresearch_cli.node_validators.review", context)

    assert completed.returncode == 1
    assert "missing VERDICT" in completed.stdout


def test_review_validator_still_requires_basic_section_structure(tmp_path: Path) -> None:
    review = tmp_path / "review.md"
    review.write_text(
        "# Evidence audit\n\n## Result\n\nVERDICT: pass\n",
        encoding="utf-8",
    )
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps({"outputs": {"review": {"path": str(review)}}}),
        encoding="utf-8",
    )

    completed = _run_validator("deepresearch_cli.node_validators.review", context)

    assert completed.returncode == 1
    assert "expected at least 2 level-2 headings" in completed.stdout


def _perspective_context(tmp_path: Path, perspective_text: str) -> Path:
    perspective = tmp_path / "perspective.md"
    perspective.write_text(perspective_text, encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "dimensions": [
                    {
                        "id": "d1",
                        "lenses": [
                            {
                                "axis": "Evidence quality",
                                "value": "Primary versus secondary",
                            },
                            {
                                "axis": "Timing",
                                "value": "Current versus historical",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "scope": {"dimension-id": "d1"},
                "inputs": {"plan": [{"path": str(plan)}]},
                "outputs": {"perspective": {"path": str(perspective)}},
            }
        ),
        encoding="utf-8",
    )
    return context


def test_perspective_validator_matches_lens_ids_not_exact_titles(tmp_path: Path) -> None:
    lens_sections = "\n".join(
        f"""### {lens_id}：自由改写的展示标题

#### Lens 定位

内容。

#### 写作补充边界（非正文主张）

内容。

#### 需要补研后才能使用

内容。

#### 探索性搜索线索

内容。
"""
        for lens_id in ("l1", "l2")
    )
    context = _perspective_context(
        tmp_path,
        "# Perspective Summary: d1\n\n"
        "## Lens Reviews\n\n"
        f"{lens_sections}\n"
        "## 维度内补研需求\n\n无。\n\n"
        "## 写回摘要\n\n完成。\n",
    )

    completed = _run_validator("deepresearch_cli.node_validators.perspective", context)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_perspective_validator_still_requires_each_lens_id(tmp_path: Path) -> None:
    context = _perspective_context(
        tmp_path,
        "# Perspective Summary: d1\n\n"
        "## Lens Reviews\n\n"
        "## 维度内补研需求\n\n无。\n\n"
        "## 写回摘要\n\n完成。\n",
    )

    completed = _run_validator("deepresearch_cli.node_validators.perspective", context)

    assert completed.returncode == 1
    assert "lens id must appear" in completed.stdout
