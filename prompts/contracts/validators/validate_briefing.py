#!/usr/bin/env python3
"""Validate only the briefing fields consumed by downstream planning."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


TOP_LEVEL_TYPES = {
    "user_confirmations_needed": dict,
    "task_interpretation": dict,
    "context_entities": list,
    "terminology": list,
    "subdomain_partitions": dict,
    "knowledge_topology": dict,
    "information_landscape": dict,
    "critical_unknowns": list,
    "candidate_lenses": list,
    "coverage_boundary": dict,
    "hypotheses_to_test": list,
    "risk_flags": list,
}
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def err(rule: str, message: str, **fields: object) -> dict:
    return {"rule": rule, "severity": "error", "message": message, **fields}


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def require_array(value: object, location: str, rule: str, errors: list[dict]) -> list:
    if not isinstance(value, list):
        errors.append(err(rule, f"{location} must be an array"))
        return []
    return value


def validate_confirmations(value: dict, errors: list[dict]) -> None:
    seen_questions: set[str] = set()
    for tier in ("blocking", "high_value", "optional"):
        items = require_array(
            value.get(tier), f"user_confirmations_needed.{tier}", "B010", errors
        )
        for index, question in enumerate(items):
            location = f"user_confirmations_needed.{tier}[{index}]"
            if not isinstance(question, dict):
                errors.append(err("B010", f"{location} must be an object"))
                continue
            question_id = question.get("id")
            if not (isinstance(question_id, str) and ID_RE.fullmatch(question_id)):
                errors.append(err("B011", f"{location}.id must be a stable identifier"))
            elif question_id in seen_questions:
                errors.append(err("B025", f"duplicate confirmation id: {question_id}"))
            else:
                seen_questions.add(question_id)
            if not nonempty(question.get("question")):
                errors.append(err("B012", f"{location}.question must be non-empty"))

            options = require_array(
                question.get("options"), f"{location}.options", "B015", errors
            )
            option_ids: set[str] = set()
            for option_index, option in enumerate(options):
                option_location = f"{location}.options[{option_index}]"
                if not isinstance(option, dict):
                    errors.append(err("B016", f"{option_location} must be an object"))
                    continue
                option_id = option.get("id")
                if not (isinstance(option_id, str) and ID_RE.fullmatch(option_id)):
                    errors.append(err("B017", f"{option_location}.id must be stable"))
                elif option_id in option_ids:
                    errors.append(err("B019", f"duplicate option id: {option_id}"))
                else:
                    option_ids.add(option_id)
                if not nonempty(option.get("label")):
                    errors.append(err("B018", f"{option_location}.label must be non-empty"))

            default_value = question.get("default_if_unanswered")
            if tier == "blocking":
                if default_value is not None:
                    errors.append(err("B020", f"{location}.default_if_unanswered must be null"))
            elif not isinstance(default_value, dict):
                errors.append(err("B021", f"{location}.default_if_unanswered must be an object"))
            elif default_value.get("option_id") not in option_ids:
                errors.append(err("B022", f"{location}.default option must reference options[].id"))


def validate_named_objects(
    value: object,
    *,
    location: str,
    name_field: str,
    rule: str,
    errors: list[dict],
) -> None:
    items = require_array(value, location, rule, errors)
    seen: set[str] = set()
    for index, item in enumerate(items):
        item_location = f"{location}[{index}]"
        if not isinstance(item, dict):
            errors.append(err(rule, f"{item_location} must be an object"))
            continue
        name = item.get(name_field)
        if not nonempty(name):
            errors.append(err(rule, f"{item_location}.{name_field} must be non-empty"))
        elif name in seen:
            errors.append(err(rule, f"{location} contains duplicate {name_field}", value=name))
        else:
            seen.add(name)


def validate(data: object) -> list[dict]:
    if not isinstance(data, dict):
        return [err("STRUCT", "Root must be a JSON object")]

    errors: list[dict] = []
    missing = sorted(set(TOP_LEVEL_TYPES) - set(data))
    if missing:
        errors.append(err("B001", "briefing is missing consumed top-level fields", missing=missing))
    for field, expected_type in TOP_LEVEL_TYPES.items():
        if field in data and not isinstance(data[field], expected_type):
            errors.append(err("B002", f"{field} must be {expected_type.__name__}"))
    if errors:
        return errors

    validate_confirmations(data["user_confirmations_needed"], errors)
    if not nonempty(data["task_interpretation"].get("user_goal")):
        errors.append(err("B031", "task_interpretation.user_goal must be non-empty"))

    validate_named_objects(
        data["context_entities"], location="context_entities", name_field="name",
        rule="B040", errors=errors,
    )
    validate_named_objects(
        data["terminology"], location="terminology", name_field="term",
        rule="B050", errors=errors,
    )
    validate_named_objects(
        data["subdomain_partitions"].get("subdomains"),
        location="subdomain_partitions.subdomains", name_field="name",
        rule="B062", errors=errors,
    )

    topology = data["knowledge_topology"]
    for field in ("consensus", "disputes", "blanks"):
        require_array(topology.get(field), f"knowledge_topology.{field}", "B070", errors)

    landscape = data["information_landscape"]
    for field in (
        "primary_source_categories", "secondary_source_categories",
        "data_source_categories", "expert_or_industry_sources",
        "weak_or_risky_sources", "high_value_urls", "search_terms", "access_barriers",
    ):
        require_array(landscape.get(field), f"information_landscape.{field}", "B090", errors)
    if not isinstance(landscape.get("time_sensitivity"), dict):
        errors.append(err("B101", "information_landscape.time_sensitivity must be an object"))

    validate_named_objects(
        data["candidate_lenses"], location="candidate_lenses", name_field="lens",
        rule="B120", errors=errors,
    )
    validate_named_objects(
        data["critical_unknowns"], location="critical_unknowns", name_field="unknown",
        rule="B110", errors=errors,
    )
    validate_named_objects(
        data["hypotheses_to_test"], location="hypotheses_to_test", name_field="claim",
        rule="B140", errors=errors,
    )
    validate_named_objects(
        data["risk_flags"], location="risk_flags", name_field="risk",
        rule="B150", errors=errors,
    )
    return errors


def build_stats(data: dict) -> dict:
    confirmations = data.get("user_confirmations_needed", {})
    partitions = data.get("subdomain_partitions", {})
    return {
        "blocking_confirmations": len(confirmations.get("blocking", [])),
        "high_value_confirmations": len(confirmations.get("high_value", [])),
        "optional_confirmations": len(confirmations.get("optional", [])),
        "context_entities": len(data.get("context_entities", [])),
        "subdomains": len(partitions.get("subdomains", [])),
        "candidate_lenses": len(data.get("candidate_lenses", [])),
        "risk_flags": len(data.get("risk_flags", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a briefing.json file.")
    parser.add_argument("path", nargs="?", help="path to briefing.json")
    parser.add_argument("--node-context", action="store_true")
    args = parser.parse_args()

    if args.node_context:
        context = json.loads(
            Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8")
        )
        args.path = context["outputs"]["briefing"]["path"]
    if not args.path:
        parser.error("path is required unless --node-context is used")

    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"ok": False, "errors": [err("FILE", f"File not found: {path}")]}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [err("JSON", f"Invalid briefing JSON: {exc}")]}, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    errors = validate(data)
    payload = {"ok": not errors, "errors": errors}
    if not errors:
        payload["stats"] = build_stats(data)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
