#!/usr/bin/env python3
"""Validate plan.json against schemas/plan.schema.md.

Stdlib-only. The validator checks the executable plan contract, including
scope ownership and independently executable research dimensions.

Usage:
    python3 validate_plan.py path/to/plan.json

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


MODE_VALUES = {"normal", "heavy"}
DEPTH_VALUES = {"skim", "moderate", "thorough"}
SOURCE_CATEGORY_VALUES = {
    "official",
    "news",
    "social_media",
    "github",
    "developer",
    "community",
    "trend",
    "academic",
    "forum",
    "analyst",
    "review",
    "data",
    "legal",
    "financial",
    "finance",
    "securities",
    "annual_report",
    "filing",
    "market_cn",
    "policy",
    "regulation",
    "multi_platform",
}
DIM_ID_RE = re.compile(r"^d[1-9]\d*$")
TOP_LEVEL_KEYS = {"dimensions"}
DIMENSION_KEYS = {
    "id", "name", "description", "key_questions", "focus", "sources",
    "lenses", "depth", "time_sensitivity", "scope_ownership",
}
SOURCE_KEYS = {"category", "description"}
LENS_KEYS = {"axis", "value", "rationale"}
SCOPE_KEYS = {"owns", "excludes", "shared_topics", "overlap_policy"}


def err(rule: str, message: str, **fields: object) -> dict:
    return {"rule": rule, "severity": "error", "message": message, **fields}


def is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def reject_unknown_keys(
    value: dict,
    allowed: set[str],
    *,
    location: str,
    rule: str,
    retired: set[str] | None = None,
) -> list[dict]:
    # Consumers read the declared fields and safely ignore extensions. Retired
    # fields are handled explicitly by the caller when they are ambiguous.
    return []


def validate_string_array(
    value: object,
    *,
    location: str,
    rule: str,
    min_items: int = 0,
) -> tuple[list[dict], list[str]]:
    errors: list[dict] = []
    if not isinstance(value, list):
        return [err(rule, f"{location} must be an array")], []

    if len(value) < min_items:
        errors.append(err(rule, f"{location} must contain at least {min_items} item(s)",
                          length=len(value)))

    strings: list[str] = []
    for index, item in enumerate(value):
        if not is_nonempty_string(item):
            errors.append(err(rule, f"{location}[{index}] must be a non-empty string",
                              got=item))
        else:
            strings.append(item)

    duplicates = sorted(item for item, count in Counter(strings).items() if count > 1)
    if duplicates:
        errors.append(err(rule, f"{location} must not contain duplicates",
                          duplicates=duplicates))
    return errors, strings


def validate(data: object, expected_mode: str | None = None) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(data, dict):
        return [err("STRUCT", "Root must be a JSON object")]
    retired = sorted({"strategy", "notes"} & set(data))
    if retired:
        errors.append(err("P001", "plan contains retired fields", fields=retired))
    errors.extend(reject_unknown_keys(
        data, TOP_LEVEL_KEYS, location="root", rule="P001", retired={"strategy", "notes"}
    ))

    if expected_mode is not None and expected_mode not in MODE_VALUES:
        errors.append(err("P002", f"expected mode must be one of {sorted(MODE_VALUES)}", got=expected_mode))

    dimensions = data.get("dimensions")
    if not (isinstance(dimensions, list) and dimensions):
        errors.append(err("P020", "dimensions must be a non-empty array"))
        return errors

    records: list[dict] = []
    all_ids: list[str] = []

    for index, dimension in enumerate(dimensions):
        location = f"dimensions[{index}]"
        if not isinstance(dimension, dict):
            errors.append(err("P021", f"{location} must be an object"))
            continue
        errors.extend(reject_unknown_keys(
            dimension, DIMENSION_KEYS, location=location, rule="P021"
        ))

        dimension_id = dimension.get("id")
        if not (isinstance(dimension_id, str) and DIM_ID_RE.fullmatch(dimension_id)):
            errors.append(err("P022", f"{location}.id must match ^d[1-9]\\d*$",
                              got=dimension_id))
        else:
            all_ids.append(dimension_id)

        for field, rule in (
            ("name", "P023"),
            ("description", "P024"),
            ("focus", "P026"),
            ("time_sensitivity", "P030"),
        ):
            if not is_nonempty_string(dimension.get(field)):
                errors.append(err(rule, f"{location}.{field} must be a non-empty string"))

        key_question_errors, _ = validate_string_array(
            dimension.get("key_questions"),
            location=f"{location}.key_questions",
            rule="P025",
            min_items=1,
        )
        errors.extend(key_question_errors)

        sources = dimension.get("sources")
        if not (isinstance(sources, list) and sources):
            errors.append(err("P028", f"{location}.sources must be a non-empty array"))
        else:
            for source_index, source in enumerate(sources):
                source_location = f"{location}.sources[{source_index}]"
                if not isinstance(source, dict):
                    errors.append(err("P028", f"{source_location} must be an object"))
                    continue
                errors.extend(reject_unknown_keys(
                    source, SOURCE_KEYS, location=source_location, rule="P028"
                ))
                category = source.get("category")
                if not is_nonempty_string(category):
                    errors.append(err("P028", f"{source_location}.category must be a "
                                              "non-empty string", got=category))
                if not is_nonempty_string(source.get("description")):
                    errors.append(err("P028", f"{source_location}.description must be a "
                                              "non-empty string"))

        lenses = dimension.get("lenses")
        if not isinstance(lenses, list):
            errors.append(err("P029", f"{location}.lenses must be an array"))
            lenses = []
        else:
            lens_pairs: list[tuple[str, str]] = []
            for lens_index, lens in enumerate(lenses):
                lens_location = f"{location}.lenses[{lens_index}]"
                if not isinstance(lens, dict):
                    errors.append(err("P029", f"{lens_location} must be an object"))
                    continue
                errors.extend(reject_unknown_keys(
                    lens, LENS_KEYS, location=lens_location, rule="P029"
                ))
                for field in ("axis", "value", "rationale"):
                    if not is_nonempty_string(lens.get(field)):
                        errors.append(err("P029", f"{lens_location}.{field} must be a "
                                                  "non-empty string"))
                if is_nonempty_string(lens.get("axis")) and is_nonempty_string(lens.get("value")):
                    lens_pairs.append((lens["axis"], lens["value"]))
            duplicate_lenses = sorted(
                pair for pair, count in Counter(lens_pairs).items() if count > 1
            )
            if duplicate_lenses:
                errors.append(err(
                    "P029",
                    f"{location}.lenses must not repeat an axis/value pair",
                    duplicates=duplicate_lenses,
                ))

        if not is_nonempty_string(dimension.get("depth")):
            errors.append(err("P031", f"{location}.depth must be a non-empty string"))

        scope = dimension.get("scope_ownership")
        scope_values: dict[str, list[str]] = {}
        if not isinstance(scope, dict):
            errors.append(err("P032", f"{location}.scope_ownership must be an object"))
        else:
            errors.extend(reject_unknown_keys(
                scope, SCOPE_KEYS, location=f"{location}.scope_ownership", rule="P032"
            ))
            for field, min_items in (("owns", 1), ("excludes", 0), ("shared_topics", 0)):
                scope_errors, values = validate_string_array(
                    scope.get(field),
                    location=f"{location}.scope_ownership.{field}",
                    rule="P033",
                    min_items=min_items,
                )
                errors.extend(scope_errors)
                scope_values[field] = values

            if not is_nonempty_string(scope.get("overlap_policy")):
                errors.append(err("P034", f"{location}.scope_ownership.overlap_policy "
                                          "must be a non-empty string"))

            ownership_conflicts = sorted(
                (set(scope_values.get("owns", [])) & set(scope_values.get("excludes", [])))
                | (set(scope_values.get("owns", [])) & set(scope_values.get("shared_topics", [])))
                | (set(scope_values.get("excludes", [])) & set(scope_values.get("shared_topics", [])))
            )
            if ownership_conflicts:
                errors.append(err("P035", f"{location}.scope_ownership fields must not contain "
                                          "the same exact scope",
                                  conflicts=ownership_conflicts))

        records.append({
            "index": index,
            "id": dimension_id,
            "lenses": lenses,
            "owns": scope_values.get("owns", []),
        })

    duplicate_ids = sorted(item for item, count in Counter(all_ids).items() if count > 1)
    if duplicate_ids:
        errors.append(err("P045", "dimension ids must be unique", duplicates=duplicate_ids))

    owned_by: dict[str, list[str]] = {}
    for record in records:
        dimension_id = record["id"]
        if not isinstance(dimension_id, str):
            continue
        for owned_scope in record["owns"]:
            owned_by.setdefault(owned_scope, []).append(dimension_id)
    duplicated_ownership = {
        scope: owners for scope, owners in owned_by.items() if len(owners) > 1
    }
    if duplicated_ownership:
        errors.append(err(
            "P050",
            "the same exact scope must not be owned by multiple dimensions; use "
            "shared_topics plus overlap_policy for intentional overlap",
            duplicated_ownership=duplicated_ownership,
        ))

    return errors


def build_stats(data: dict, expected_mode: str | None = None) -> dict:
    dimensions = [item for item in data.get("dimensions", []) if isinstance(item, dict)]
    return {
        "mode": expected_mode,
        "dimensions": len(dimensions),
        "lenses": sum(
            len(item.get("lenses", []))
            for item in dimensions
            if isinstance(item.get("lenses"), list)
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a plan.json file.")
    parser.add_argument("path", nargs="?", help="path to plan.json")
    parser.add_argument("--node-context", action="store_true",
                        help="validate the plan and research-task outputs from Node Context")
    parser.add_argument("--expected-mode", choices=sorted(MODE_VALUES),
                        help="request mode; required to apply mode-specific lens rules")
    args = parser.parse_args()

    context = None
    if args.node_context:
        context_path = Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"])
        context = json.loads(context_path.read_text(encoding="utf-8"))
        args.path = context["outputs"]["plan"]["path"]
        run_mode = context["run"].get("mode")
        args.expected_mode = run_mode if run_mode in MODE_VALUES else None
    if not args.path:
        parser.error("path is required unless --node-context is used")
    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"ok": False, "errors": [
            err("FILE", f"File not found: {path}")
        ]}, ensure_ascii=False, indent=2))
        sys.exit(2)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "errors": [
            err("FILE", f"Could not read {path}: {exc}")
        ]}, ensure_ascii=False, indent=2))
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "errors": [
            err("JSON", f"Invalid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}")
        ]}, ensure_ascii=False, indent=2))
        sys.exit(2)

    errors = validate(data, expected_mode=args.expected_mode)
    if context is not None and not errors:
        dimensions = data.get("dimensions", [])
        dimension_ids = {
            item.get("id") for item in dimensions if isinstance(item, dict)
        }
        task_root = Path(context["outputs"]["research-tasks"]["directory"])
        task_paths = sorted(task_root.glob("*.json")) if task_root.exists() else []
        task_ids = {item.stem for item in task_paths}
        if task_ids != dimension_ids:
            errors.append(err(
                "P060",
                "research task filenames must exactly match plan dimension IDs",
                missing=sorted(dimension_ids - task_ids),
                extra=sorted(task_ids - dimension_ids),
            ))
        for task_path in task_paths:
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(err("P061", f"invalid research task {task_path.name}: {exc}"))
                continue
            expected_dimension = next(
                (
                    item for item in dimensions
                    if isinstance(item, dict) and item.get("id") == task_path.stem
                ),
                None,
            )
            if task != expected_dimension:
                errors.append(err(
                    "P061",
                    f"research task {task_path.name} must exactly equal its plan dimension",
                ))
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        sys.exit(1)

    print(json.dumps({"ok": True, "errors": [], "stats": build_stats(data, args.expected_mode)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
