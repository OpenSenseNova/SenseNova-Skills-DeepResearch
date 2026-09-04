#!/usr/bin/env python3
"""Validate a generated or completed supplement_plan.json.

The default planned-state gate checks that every supplement item is pending and
has no resolution note.  ``--expected-state completed`` is the supplement
Research return gate: it binds the returned work-order item IDs to the planned
work order and checks the updated evidence boundary.  It deliberately does not
freeze work-order wording or compare old/new Evidence contents; those are Agent
responsibilities and a legitimate claim correction may remove old material.

Usage:
    python3 validate_supplement_plan.py supplement_plan.json \
        --plan plan.json

Exit code:
    0 - pass
    1 - schema or contract errors
    2 - file not found or invalid JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path


DIM_ID_RE = re.compile(r"^d[1-9]\d*$")
SUPPLEMENT_TYPE_VALUES = {"coverage", "claim_fix", "both"}
SUPPLEMENT_STATUS_VALUES = {
    "pending",
    "resolved",
    "partial",
    "no_data",
    "out_of_scope",
}
EXPECTED_STATE_VALUES = {"planned", "completed"}
DEFERRED_REASON_VALUES = {
    "writing_context_only",
    "low_value",
    "not_actionable",
    "out_of_scope",
    "already_covered",
    "unavailable",
}
TOP_LEVEL_KEYS = {
    "dimension_id",
    "supplement_items",
    "deferred_items",
}
SUPPLEMENT_ITEM_KEYS = {
    "id",
    "type",
    "gap",
    "question",
    "rationale",
    "suggested_sources",
    "candidate_leads",
    "source_refs",
    "review_refs",
    "impact_if_skipped",
    "status",
    "resolution_note",
}
DEFERRED_ITEM_KEYS = {
    "id",
    "reason",
    "item",
    "source_refs",
    "writing_context_use",
}


def err(rule: str, message: str, **fields: object) -> dict:
    return {"rule": rule, "severity": "error", "message": message, **fields}


def is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_required_keys(
    value: object,
    expected: set[str],
    *,
    location: str,
    rule: str,
    retired: set[str] | None = None,
) -> tuple[list[dict], dict | None]:
    if not isinstance(value, dict):
        return [err(rule, f"{location} must be an object")], None

    errors: list[dict] = []
    # Extension fields are harmless to the task materializer and Research
    # consumer. Only required consumed fields are enforced.
    missing = sorted(expected - set(value))
    if missing:
        errors.append(err(rule, f"{location} is missing required fields", missing=missing))
    return errors, value


def validate_string_array(
    value: object,
    *,
    location: str,
    rule: str,
    min_items: int = 0,
) -> tuple[list[dict], list[str]]:
    if not isinstance(value, list):
        return [err(rule, f"{location} must be an array")], []

    errors: list[dict] = []
    if len(value) < min_items:
        errors.append(
            err(
                rule,
                f"{location} must contain at least {min_items} item(s)",
                length=len(value),
            )
        )

    strings: list[str] = []
    for index, item in enumerate(value):
        if not is_nonempty_string(item):
            errors.append(
                err(rule, f"{location}[{index}] must be a non-empty string", got=item)
            )
        else:
            strings.append(item)

    duplicates = sorted(item for item, count in Counter(strings).items() if count > 1)
    if duplicates:
        errors.append(
            err(rule, f"{location} must not contain duplicates", duplicates=duplicates)
        )
    return errors, strings


def validate(
    data: object,
    plan_data: object | None = None,
    *,
    expected_state: str = "planned",
) -> list[dict]:
    errors: list[dict] = []
    if expected_state not in EXPECTED_STATE_VALUES:
        return [
            err(
                "SP000",
                f"expected_state must be one of {sorted(EXPECTED_STATE_VALUES)}",
                got=expected_state,
            )
        ]
    root_errors, root = validate_required_keys(
        data,
        TOP_LEVEL_KEYS,
        location="root",
        rule="SP001",
        retired={"meta"},
    )
    errors.extend(root_errors)
    if root is None:
        return errors
    if "meta" in root:
        errors.append(err("SP002", "meta is retired; runtime already owns provenance metadata"))

    dimension_id = root.get("dimension_id")
    valid_dimension_id = (
        isinstance(dimension_id, str) and DIM_ID_RE.fullmatch(dimension_id) is not None
    )
    if not valid_dimension_id:
        errors.append(
            err("SP005", "dimension_id must match ^d[1-9]\\d*$", got=dimension_id)
        )
        dimension_id = None

    if plan_data is not None:
        if not isinstance(plan_data, dict):
            errors.append(err("SP007", "plan root must be an object"))
        else:
            dimensions = plan_data.get("dimensions")
            if not isinstance(dimensions, list):
                errors.append(err("SP007", "plan.dimensions must be an array"))
            elif dimension_id is not None:
                planned_dimension = next(
                    (
                        item
                        for item in dimensions
                        if isinstance(item, dict) and item.get("id") == dimension_id
                    ),
                    None,
                )
                if planned_dimension is None:
                    errors.append(
                        err("SP007", f"dimension {dimension_id!r} is not present in plan")
                    )

    supplement_items = root.get("supplement_items")
    if not isinstance(supplement_items, list):
        errors.append(err("SP009", "supplement_items must be an array"))
        supplement_items = []

    seen_ids: list[str] = []
    for index, item in enumerate(supplement_items):
        location = f"supplement_items[{index}]"
        item_errors, item_obj = validate_required_keys(
            item,
            SUPPLEMENT_ITEM_KEYS,
            location=location,
            rule="SP010",
        )
        errors.extend(item_errors)
        if item_obj is None:
            continue

        item_id = item_obj.get("id")
        expected_id_re = (
            re.compile(rf"^{re.escape(dimension_id)}-s[1-9]\d*$")
            if dimension_id is not None
            else None
        )
        if not (
            isinstance(item_id, str)
            and expected_id_re is not None
            and expected_id_re.fullmatch(item_id)
        ):
            errors.append(
                err(
                    "SP011",
                    f"{location}.id must match <dimension_id>-sN",
                    got=item_id,
                )
            )
        else:
            seen_ids.append(item_id)

        item_type = item_obj.get("type")
        if item_type not in SUPPLEMENT_TYPE_VALUES:
            errors.append(
                err(
                    "SP012",
                    f"{location}.type must be one of {sorted(SUPPLEMENT_TYPE_VALUES)}",
                    got=item_type,
                )
            )

        for field in (
            "gap",
            "question",
            "rationale",
            "impact_if_skipped",
        ):
            if not is_nonempty_string(item_obj.get(field)):
                errors.append(
                    err("SP013", f"{location}.{field} must be a non-empty string")
                )

        for field, min_items in (
            ("suggested_sources", 0),
            ("candidate_leads", 0),
            ("source_refs", 0),
            ("review_refs", 0),
        ):
            array_errors, _ = validate_string_array(
                item_obj.get(field),
                location=f"{location}.{field}",
                rule="SP014",
                min_items=min_items,
            )
            errors.extend(array_errors)

        if item_type in {"claim_fix", "both"}:
            review_refs = item_obj.get("review_refs")
            if isinstance(review_refs, list) and not review_refs:
                errors.append(
                    err(
                        "SP015",
                        f"{location}.review_refs must be non-empty for type={item_type}",
                    )
                )

        status = item_obj.get("status")
        if status not in SUPPLEMENT_STATUS_VALUES:
            errors.append(
                err(
                    "SP016",
                    f"{location}.status must be one of "
                    f"{sorted(SUPPLEMENT_STATUS_VALUES)}",
                    got=status,
                )
            )
        elif expected_state == "planned" and status != "pending":
            errors.append(
                err(
                    "SP018",
                    f"{location}.status must be pending in a newly generated plan",
                    got=status,
                )
            )

        resolution_note = item_obj.get("resolution_note")
        if not isinstance(resolution_note, str):
            errors.append(err("SP017", f"{location}.resolution_note must be a string"))
        elif expected_state == "planned" and resolution_note:
            errors.append(
                err(
                    "SP018",
                    f"{location}.resolution_note must be empty in a newly generated plan",
                )
            )
        elif (
            expected_state == "completed"
            and status in SUPPLEMENT_STATUS_VALUES - {"pending"}
            and not resolution_note.strip()
        ):
            errors.append(
                err(
                    "SP019",
                    f"{location}.resolution_note must be non-empty after execution",
                )
            )
        if expected_state == "completed" and status == "pending":
            errors.append(
                err(
                    "SP019",
                    f"{location}.status must be completed after supplement Research",
                    got=status,
                )
            )

    deferred_items = root.get("deferred_items")
    if not isinstance(deferred_items, list):
        errors.append(err("SP020", "deferred_items must be an array"))
        deferred_items = []

    for index, item in enumerate(deferred_items):
        location = f"deferred_items[{index}]"
        item_errors, item_obj = validate_required_keys(
            item,
            DEFERRED_ITEM_KEYS,
            location=location,
            rule="SP021",
        )
        errors.extend(item_errors)
        if item_obj is None:
            continue

        item_id = item_obj.get("id")
        expected_id_re = (
            re.compile(rf"^{re.escape(dimension_id)}-d[1-9]\d*$")
            if dimension_id is not None
            else None
        )
        if not (
            isinstance(item_id, str)
            and expected_id_re is not None
            and expected_id_re.fullmatch(item_id)
        ):
            errors.append(
                err(
                    "SP022",
                    f"{location}.id must match <dimension_id>-dN",
                    got=item_id,
                )
            )
        else:
            seen_ids.append(item_id)

        reason = item_obj.get("reason")
        if reason not in DEFERRED_REASON_VALUES:
            errors.append(
                err(
                    "SP023",
                    f"{location}.reason must be one of {sorted(DEFERRED_REASON_VALUES)}",
                    got=reason,
                )
            )
        if not is_nonempty_string(item_obj.get("item")):
            errors.append(err("SP024", f"{location}.item must be a non-empty string"))
        source_errors, _ = validate_string_array(
            item_obj.get("source_refs"),
            location=f"{location}.source_refs",
            rule="SP025",
            min_items=0,
        )
        errors.extend(source_errors)
        writing_context_use = item_obj.get("writing_context_use")
        if not isinstance(writing_context_use, str):
            errors.append(
                err("SP026", f"{location}.writing_context_use must be a string")
            )
        elif reason == "writing_context_only" and not writing_context_use.strip():
            errors.append(
                err(
                    "SP026",
                    f"{location}.writing_context_use must be non-empty "
                    "for reason=writing_context_only",
                )
            )

    duplicates = sorted(item_id for item_id, count in Counter(seen_ids).items() if count > 1)
    if duplicates:
        errors.append(err("SP027", "item ids must be unique", duplicates=duplicates))

    return errors


def validate_completed_execution(
    completed: object,
    input_plan: object,
    evidence: object,
) -> list[dict]:
    """Validate the minimal lifecycle binding and updated-evidence boundary."""

    errors: list[dict] = []
    if not all(
        isinstance(value, dict)
        for value in (completed, input_plan, evidence)
    ):
        return [
            err(
                "SP030",
                "completed execution inputs must all be JSON objects",
            )
        ]

    completed_obj = completed
    input_obj = input_plan
    evidence_obj = evidence

    if completed_obj.get("dimension_id") != input_obj.get("dimension_id"):
        errors.append(
            err(
                "SP031",
                "completed plan dimension_id must match its planned work order",
                expected=input_obj.get("dimension_id"),
                got=completed_obj.get("dimension_id"),
            )
        )

    input_items = input_obj.get("supplement_items")
    completed_items = completed_obj.get("supplement_items")
    if isinstance(input_items, list) and isinstance(completed_items, list):
        input_ids = [
            item.get("id") if isinstance(item, dict) else None
            for item in input_items
        ]
        completed_ids = [
            item.get("id") if isinstance(item, dict) else None
            for item in completed_items
        ]
        same_ids = (
            len(completed_ids) == len(input_ids)
            and all(item_id in completed_ids for item_id in input_ids)
            and all(item_id in input_ids for item_id in completed_ids)
        )
        if not same_ids:
            errors.append(
                err(
                    "SP032",
                    "completed plan must account for every planned supplement item id",
                    expected=input_ids,
                    got=completed_ids,
                )
            )

    dimension_id = completed_obj.get("dimension_id")
    if evidence_obj.get("dimension_id") != dimension_id:
        errors.append(
            err(
                "SP034",
                "updated evidence dimension_id must match completed supplement plan",
                expected=dimension_id,
                got=evidence_obj.get("dimension_id"),
            )
        )
    errors.extend(_status_boundary_errors(completed_items, evidence_obj))
    return errors


def _status_boundary_errors(
    completed_items: object, evidence: dict
) -> list[dict]:
    if not isinstance(completed_items, list):
        return []
    required_kinds = {
        "partial": "unresolved_gap",
        "no_data": "availability_gap",
        "out_of_scope": "scope_boundary",
    }
    writing_context = [
        item
        for item in evidence.get("writing_context", [])
        if isinstance(item, dict)
    ]
    errors = []
    for item in completed_items:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        required_kind = required_kinds.get(status)
        if required_kind is None:
            continue
        if not any(context.get("kind") == required_kind for context in writing_context):
            errors.append(
                err(
                    "SP037",
                    f"status={status} requires updated evidence writing_context "
                    f"kind={required_kind}",
                    item_id=item.get("id"),
                )
            )
    return errors


def build_stats(data: dict) -> dict:
    supplement_items = [
        item for item in data.get("supplement_items", []) if isinstance(item, dict)
    ]
    return {
        "dimension_id": data.get("dimension_id"),
        "supplement_items": len(supplement_items),
        "deferred_items": len(
            [item for item in data.get("deferred_items", []) if isinstance(item, dict)]
        ),
        "status_distribution": {
            status: sum(1 for item in supplement_items if item.get("status") == status)
            for status in sorted(SUPPLEMENT_STATUS_VALUES)
        },
    }


def load_json(path: Path, label: str) -> tuple[object | None, dict | None]:
    if not path.exists():
        return None, err("FILE", f"File not found: {path}", input=label)
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError) as exc:
        return None, err("FILE", f"Could not read {path}: {exc}", input=label)
    except json.JSONDecodeError as exc:
        return None, err(
            "JSON",
            f"Invalid {label} JSON: {exc.msg} at line {exc.lineno} col {exc.colno}",
            input=label,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a supplement_plan.json file.")
    parser.add_argument("path", nargs="?", help="path to supplement_plan.json")
    parser.add_argument(
        "--node-context",
        action="store_true",
        help="resolve planned or completed supplement outputs from Node Context",
    )
    parser.add_argument(
        "--plan",
        required=False,
        dest="plan_path",
        help="path to plan.json; validates dimension id and name binding",
    )
    parser.add_argument(
        "--expected-state",
        choices=sorted(EXPECTED_STATE_VALUES),
        default="planned",
        help="planned for a new work order; completed after supplement Research",
    )
    parser.add_argument(
        "--input-plan",
        help="planned work order whose item IDs must be completed",
    )
    parser.add_argument(
        "--evidence",
        help="updated evidence.json; required for expected-state completed",
    )
    args = parser.parse_args()

    context = None
    if args.node_context:
        context_path = Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"])
        context = json.loads(context_path.read_text(encoding="utf-8"))
        plan_inputs = context["inputs"].get("plan", [])
        args.plan_path = plan_inputs[-1]["path"] if plan_inputs else None
        if "supplement-plan" in context["outputs"]:
            args.path = context["outputs"]["supplement-plan"]["path"]
            args.expected_state = "planned"
        else:
            input_plans = context["inputs"].get("supplement-plan", [])
            if not input_plans:
                print(json.dumps({
                    "ok": True,
                    "errors": [],
                    "stats": {"state": "not_applicable"},
                }, ensure_ascii=False, indent=2))
                return
            args.path = context["outputs"]["completed-supplement-plan"]["path"]
            args.expected_state = "completed"
            args.input_plan = input_plans[-1]["path"]
            args.evidence = context["outputs"]["evidence"]["path"]
    if not args.path:
        parser.error("path is required unless --node-context is used")
    if not args.plan_path:
        parser.error("--plan is required")

    data, load_error = load_json(Path(args.path), "supplement plan")
    if load_error:
        print(json.dumps({"ok": False, "errors": [load_error]},
                         ensure_ascii=False, indent=2))
        sys.exit(2)

    plan_data, load_error = load_json(Path(args.plan_path), "plan")
    if load_error:
        print(json.dumps({"ok": False, "errors": [load_error]},
                         ensure_ascii=False, indent=2))
        sys.exit(2)

    errors = validate(
        data,
        plan_data=plan_data,
        expected_state=args.expected_state,
    )
    if context is not None and "research-tasks" in context["outputs"]:
        task_root = Path(context["outputs"]["research-tasks"]["directory"])
        task_paths = sorted(task_root.glob("*.json")) if task_root.exists() else []
        expected_task = None
        if isinstance(data, dict) and data.get("supplement_items"):
            expected_task = {
                "dimension_id": data.get("dimension_id"),
                "supplement_items": data.get("supplement_items"),
            }
        expected_names = (
            {f"{data.get('dimension_id')}.json"} if expected_task is not None else set()
        )
        actual_names = {path.name for path in task_paths}
        if actual_names != expected_names:
            errors.append(err(
                "SP028",
                "derived research task files must match supplement_items state",
                expected=sorted(expected_names),
                got=sorted(actual_names),
            ))
        elif expected_task is not None:
            try:
                task_data = json.loads(task_paths[0].read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(err("SP028", f"invalid derived research task: {exc}"))
            else:
                if task_data != expected_task:
                    errors.append(err(
                        "SP028",
                        "derived research task must exactly match supplement plan items",
                    ))
    if args.expected_state == "completed":
        missing = [
            flag
            for flag, value in (
                ("--input-plan", args.input_plan),
                ("--evidence", args.evidence),
            )
            if not value
        ]
        if missing:
            errors.append(
                err(
                    "SP030",
                    "completed validation requires execution inputs",
                    missing=missing,
                )
            )
        else:
            input_plan, input_error = load_json(
                Path(args.input_plan), "input supplement plan"
            )
            evidence, evidence_error = load_json(
                Path(args.evidence), "updated evidence"
            )
            for load_error in (input_error, evidence_error):
                if load_error:
                    errors.append(load_error)
            if not any((input_error, evidence_error)):
                input_errors = validate(
                    input_plan,
                    plan_data=plan_data,
                    expected_state="planned",
                )
                errors.extend(
                    {
                        **item,
                        "message": "input supplement plan: " + item["message"],
                    }
                    for item in input_errors
                )
                errors.extend(
                    validate_completed_execution(
                        data,
                        input_plan,
                        evidence,
                    )
                )
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        sys.exit(1)

    print(
        json.dumps(
            {"ok": True, "errors": [], "stats": build_stats(data)},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
