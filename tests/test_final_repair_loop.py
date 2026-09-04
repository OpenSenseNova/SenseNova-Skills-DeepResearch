from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_planner(tmp_path: Path, review_text: str) -> subprocess.CompletedProcess[str]:
    review = tmp_path / "review.md"
    review.write_text(review_text, encoding="utf-8")
    outline = tmp_path / "outline.json"
    outline.write_text(
        json.dumps({"content_units": [{"id": "u1"}, {"id": "u2"}]}),
        encoding="utf-8",
    )
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "inputs": {
                    "review": [{"path": str(review)}],
                    "outline": [{"path": str(outline)}],
                },
                "outputs": {
                    "decision": {"path": str(tmp_path / "decision.json")},
                    "repair-tasks": {"directory": str(tmp_path / "repair-tasks")},
                    "recheck-tasks": {"directory": str(tmp_path / "recheck-tasks")},
                },
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["DEEPRESEARCH_NODE_CONTEXT"] = str(context)
    return subprocess.run(
        [sys.executable, "-m", "deepresearch_cli.final_repair_planner_node"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def test_repair_planner_targets_only_explicit_unit_markers(tmp_path: Path) -> None:
    completed = _run_planner(
        tmp_path,
        "## Result\n\nVERDICT: revise\n\n"
        "## Issues\n\nREPAIR_TARGET: u2 | Fix the second unit.\n",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (tmp_path / "repair-tasks" / "u1.json").exists()
    task = json.loads((tmp_path / "repair-tasks" / "u2.json").read_text())
    assert task["unit_id"] == "u2"
    assert task["targeted_findings"] == [
        "REPAIR_TARGET: u2 | Fix the second unit."
    ]
    assert (tmp_path / "recheck-tasks" / "once.json").is_file()


def test_repair_planner_pass_creates_no_agent_or_recheck_tasks(tmp_path: Path) -> None:
    completed = _run_planner(
        tmp_path,
        "## Result\n\nVERDICT: pass\n\n## Issues\n\nNone.\n",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert list((tmp_path / "repair-tasks").iterdir()) == []
    assert list((tmp_path / "recheck-tasks").iterdir()) == []
    decision = json.loads((tmp_path / "decision.json").read_text())
    assert decision == {"verdict": "pass", "repair_cycle": 0, "target_units": []}
