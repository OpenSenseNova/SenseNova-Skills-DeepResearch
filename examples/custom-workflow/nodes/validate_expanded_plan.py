from __future__ import annotations

import json
import os
import sys
from pathlib import Path


TARGET_TASKS = 8


def main() -> int:
    context = json.loads(
        Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8")
    )
    try:
        plan_path = Path(context["outputs"]["plan"]["path"])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        dimensions = plan["dimensions"]
        identifiers = [item["id"] for item in dimensions]
        task_dir = Path(context["outputs"]["research-tasks"]["directory"])
        tasks = sorted(task_dir.glob("*.json"))
        assert len(dimensions) == TARGET_TASKS
        assert identifiers == [f"d{index}" for index in range(1, TARGET_TASKS + 1)]
        assert [path.stem for path in tasks] == identifiers
        for path in tasks:
            task = json.loads(path.read_text(encoding="utf-8"))
            assert task["id"] == path.stem
            assert task.get("key_questions")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "task_count": TARGET_TASKS,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
