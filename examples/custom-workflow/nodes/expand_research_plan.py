from __future__ import annotations

import copy
import json
import os
from pathlib import Path


TARGET_TASKS = 8


def _load_context() -> dict:
    context_path = Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"])
    return json.loads(context_path.read_text(encoding="utf-8"))


def _expanded_dimension(source: dict, index: int, original_count: int) -> dict:
    item = copy.deepcopy(source)
    item["id"] = f"d{index + 1}"
    if index < original_count:
        return item

    pass_number = index // original_count + 1
    suffix = f"独立交叉验证 {pass_number}"
    item["name"] = f"{item.get('name', '研究维度')}（{suffix}）"
    description = str(item.get("description", "")).strip()
    item["description"] = (
        f"{description} 使用不同来源执行{suffix}，用于检查原研究维度的结论稳定性。"
    ).strip()
    questions = list(item.get("key_questions") or [])
    questions.append(
        "使用与原研究任务不同的独立来源交叉验证关键事实，并明确记录一致点、冲突点和缺失信息。"
    )
    item["key_questions"] = questions
    item["focus"] = f"{item.get('focus', '')}；{suffix}".strip("；")
    ownership = dict(item.get("scope_ownership") or {})
    owned = list(ownership.get("owns") or [item["name"]])
    ownership["owns"] = [f"{value} [{item['id']}]" for value in owned]
    ownership["overlap_policy"] = (
        "这是显式交叉验证任务；允许与原维度主题重叠，但必须使用独立来源并单独交付证据。"
    )
    item["scope_ownership"] = ownership
    return item


def main() -> None:
    context = _load_context()
    plan_inputs = context["inputs"]["plan"]
    if len(plan_inputs) != 1:
        raise ValueError("expand-research-plan requires exactly one plan artifact")
    plan = json.loads(Path(plan_inputs[0]["path"]).read_text(encoding="utf-8"))
    original = plan.get("dimensions")
    if not isinstance(original, list) or not original:
        raise ValueError("plan.dimensions must be a non-empty list")

    expanded = [
        _expanded_dimension(original[index % len(original)], index, len(original))
        for index in range(TARGET_TASKS)
    ]
    plan["dimensions"] = expanded

    plan_path = Path(context["outputs"]["plan"]["path"])
    task_dir = Path(context["outputs"]["research-tasks"]["directory"])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for dimension in expanded:
        (task_dir / f"{dimension['id']}.json").write_text(
            json.dumps(dimension, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({"input_tasks": len(original), "output_tasks": len(expanded)}))


if __name__ == "__main__":
    main()
