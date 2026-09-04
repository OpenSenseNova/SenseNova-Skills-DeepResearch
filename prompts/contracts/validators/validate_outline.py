#!/usr/bin/env python3
"""Validate the outline fields consumed by materialization, writing and render."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path

import yaml


PARADIGM_VALUES = {
    "panorama", "comparison", "investigation", "timeline", "evaluation", "forecast",
}
CONTENT_UNIT_RENDER_MODE_VALUES = {
    "prose", "markdown_table", "ordered_list", "checklist", "qa",
    "callout", "mermaid", "mixed", "custom",
}
OPENING_SUMMARY_VALUES = {"none", "findings", "recommendation"}
REPORT_FORMAT_VALUES = {"brief", "formal_report"}
CONTENT_UNIT_ID_RE = re.compile(r"^u\d+$")
ELEMENT_ID_RE = re.compile(r"^e\d+$")
CLAIM_ID_RE = re.compile(r"^d\d+\.c\d+$")
WRITING_CONTEXT_ID_RE = re.compile(r"^d\d+\.w\d+$")


def err(rule: str, message: str, **fields: object) -> dict:
    return {"rule": rule, "severity": "error", "message": message, **fields}


def warn(rule: str, message: str, **fields: object) -> dict:
    return {"rule": rule, "severity": "warning", "message": message, **fields}


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def string_array(value: object, location: str, rule: str, errors: list[dict]) -> list[str]:
    if not isinstance(value, list):
        errors.append(err(rule, f"{location} must be an array"))
        return []
    values = [item for item in value if nonempty(item)]
    if len(values) != len(value):
        errors.append(err(rule, f"{location} entries must be non-empty strings"))
    if len(set(values)) != len(values):
        errors.append(err(rule, f"{location} entries must be unique"))
    return values


def validate_outline(
    data: object,
    expected_report_format: str | None = None,
    template_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    errors: list[dict] = []
    warnings: list[dict] = []
    if not isinstance(data, dict):
        return [err("STRUCT", "Root must be a JSON object")], warnings

    retired = sorted({"depth_level", "claim_routing_table"} & set(data))
    if retired:
        errors.append(err("U001", "outline contains retired fields", fields=retired))

    profile = data.get("report_profile")
    if not isinstance(profile, dict):
        errors.append(err("U005", "report_profile must be an object"))
    else:
        report_format = profile.get("format")
        template_id = profile.get("template_id")
        if report_format not in REPORT_FORMAT_VALUES:
            errors.append(err("U005", "report_profile.format is invalid"))
        elif expected_report_format is not None and report_format != expected_report_format:
            errors.append(err(
                "U005", "report_profile.format must match the request",
                expected=expected_report_format, got=report_format,
            ))
        if report_format == "formal_report":
            if not nonempty(template_id):
                errors.append(err(
                    "U005", "formal_report requires a non-empty report_profile.template_id"
                ))
            elif template_ids is not None and template_id not in template_ids:
                errors.append(err(
                    "U005", "report_profile.template_id is not in the template catalog",
                    got=template_id,
                ))
        elif template_id is not None:
            errors.append(err(
                "U005", "brief requires report_profile.template_id=null"
            ))

    paradigm = data.get("paradigm")
    if not isinstance(paradigm, dict) or not nonempty(paradigm.get("main")):
        errors.append(err("U002", "paradigm.main must be a non-empty string"))
    elif paradigm.get("secondary") is not None:
        if not nonempty(paradigm.get("secondary")):
            errors.append(err("U003", "paradigm.secondary must be null or non-empty"))
        elif paradigm.get("secondary") == paradigm.get("main"):
            errors.append(err("U004", "paradigm.main and secondary must differ"))

    if not nonempty(data.get("global_arc")):
        errors.append(err("U006", "global_arc must be a non-empty string"))

    organization = data.get("organization_decision")
    if not isinstance(organization, dict):
        errors.append(err("U010", "organization_decision must be an object"))
        opening_summary = None
    else:
        opening_summary = organization.get("opening_summary")
        if opening_summary not in OPENING_SUMMARY_VALUES:
            errors.append(err("U013", "organization_decision.opening_summary is invalid"))
        for field in ("toc", "numbered_headings"):
            if not isinstance(organization.get(field), bool):
                errors.append(err("U014", f"organization_decision.{field} must be boolean"))

    l0 = data.get("L0_draft")
    l0_refs: set[str] = set()
    if opening_summary == "none" and l0 is not None:
        errors.append(err("U020", "L0_draft must be null when opening_summary=none"))
    elif opening_summary in {"findings", "recommendation"}:
        if not isinstance(l0, dict):
            errors.append(err("U020", "L0_draft must be an object for an opening summary"))
        else:
            if not nonempty(l0.get("headline")):
                errors.append(err("U020", "L0_draft.headline must be non-empty"))
            string_array(l0.get("key_findings"), "L0_draft.key_findings", "U021", errors)
            visual = l0.get("abstract_visual")
            if visual is not None:
                if not isinstance(visual, dict):
                    errors.append(err("U023", "L0_draft.abstract_visual must be an object or null"))
                else:
                    refs = string_array(
                        visual.get("data_refs"), "L0_draft.abstract_visual.data_refs", "U024", errors
                    )
                    for claim_id in refs:
                        if not CLAIM_ID_RE.fullmatch(claim_id):
                            errors.append(err("U024", "visual data_ref must be a claim id", got=claim_id))
                        else:
                            l0_refs.add(claim_id)

    style = data.get("style_contract")
    if not isinstance(style, dict):
        errors.append(err("U030", "style_contract must be an object"))

    units = data.get("content_units")
    if not isinstance(units, list) or not units:
        errors.append(err("U040", "content_units must be a non-empty array"))
        return errors, warnings

    unit_ids: set[str] = set()
    routed_claim_ids: set[str] = set()
    for unit_index, unit in enumerate(units):
        location = f"content_units[{unit_index}]"
        if not isinstance(unit, dict):
            errors.append(err("U040", f"{location} must be an object"))
            continue
        if "evidence_subset" in unit:
            errors.append(err(
                "U040", f"{location}.evidence_subset is runtime-owned and must be omitted",
                fields=["evidence_subset"],
            ))
        unit_id = unit.get("id")
        if not (isinstance(unit_id, str) and CONTENT_UNIT_ID_RE.fullmatch(unit_id)):
            errors.append(err("U041", f"{location}.id must match ^u\\d+$", got=unit_id))
        elif unit_id in unit_ids:
            errors.append(err("U041", f"duplicate content unit id: {unit_id}"))
        else:
            unit_ids.add(unit_id)
        if not nonempty(unit.get("title")):
            errors.append(err("U044", f"{location}.title must be non-empty"))

        render = unit.get("render_contract")
        if not isinstance(render, dict):
            errors.append(err("U050", f"{location}.render_contract must be an object"))
        else:
            if render.get("mode") not in CONTENT_UNIT_RENDER_MODE_VALUES:
                errors.append(err("U050", f"{location}.render_contract.mode is invalid"))
            if not isinstance(render.get("show_heading"), bool):
                errors.append(err("U051", f"{location}.render_contract.show_heading must be boolean"))
            string_array(render.get("schema"), f"{location}.render_contract.schema", "U052", errors)
            if not nonempty(render.get("instructions")):
                errors.append(err("U053", f"{location}.render_contract.instructions must be non-empty"))
            citation_policy = render.get("citation_policy")
            if not isinstance(citation_policy, dict):
                errors.append(err("U054", f"{location}.render_contract.citation_policy must be an object"))
            else:
                if citation_policy.get("scope") not in {"unit", "element"}:
                    errors.append(err("U054", f"{location}.render_contract.citation_policy.scope is invalid"))
                if citation_policy.get("require_each_claim") is not True:
                    errors.append(err("U054", f"{location}.render_contract.citation_policy.require_each_claim must be true"))
                required_fields = string_array(
                    citation_policy.get("required_fields"),
                    f"{location}.render_contract.citation_policy.required_fields",
                    "U054",
                    errors,
                )
                schema = render.get("schema", [])
                if isinstance(schema, list):
                    unknown = sorted(set(required_fields) - set(schema))
                    if unknown:
                        errors.append(err("U054", f"{location}.render_contract.citation_policy.required_fields must be a schema subset", unknown=unknown))
                if render.get("mode") != "markdown_table" and required_fields:
                    errors.append(err("U054", f"{location}.render_contract.citation_policy.required_fields is only valid for markdown_table"))
            secondary = render.get("secondary_structure")
            if not isinstance(secondary, dict):
                errors.append(err("U055", f"{location}.render_contract.secondary_structure must be an object"))
            else:
                allowed = secondary.get("allowed")
                required = secondary.get("required")
                heading_level = secondary.get("heading_level")
                if not isinstance(allowed, bool) or not isinstance(required, bool):
                    errors.append(err("U055", f"{location}.render_contract.secondary_structure allowed/required must be boolean"))
                if required is True and allowed is not True:
                    errors.append(err("U055", f"{location}.render_contract.secondary_structure.required needs allowed=true"))
                if allowed is True and heading_level != 3:
                    errors.append(err("U055", f"{location}.render_contract.secondary_structure.heading_level must be 3 when allowed"))
                if allowed is False and heading_level is not None:
                    errors.append(err("U055", f"{location}.render_contract.secondary_structure.heading_level must be null when forbidden"))

        elements = unit.get("elements")
        if not isinstance(elements, list):
            errors.append(err("U060", f"{location}.elements must be an array"))
            continue
        element_ids: set[str] = set()
        claim_roles: dict[str, str] = {}
        for element_index, element in enumerate(elements):
            element_location = f"{location}.elements[{element_index}]"
            if not isinstance(element, dict):
                errors.append(err("U060", f"{element_location} must be an object"))
                continue
            element_id = element.get("id")
            if not (isinstance(element_id, str) and ELEMENT_ID_RE.fullmatch(element_id)):
                errors.append(err("U061", f"{element_location}.id must match ^e\\d+$"))
            elif element_id in element_ids:
                errors.append(err("U061", f"duplicate element id: {element_id}"))
            else:
                element_ids.add(element_id)

            refs = element.get("evidence_refs", [])
            if not isinstance(refs, list):
                errors.append(err("U064", f"{element_location}.evidence_refs must be an array"))
                refs = []
            for ref_index, ref in enumerate(refs):
                ref_location = f"{element_location}.evidence_refs[{ref_index}]"
                if not isinstance(ref, dict):
                    errors.append(err("U064", f"{ref_location} must be an object"))
                    continue
                claim_id = ref.get("claim_id")
                role = ref.get("role")
                if not (isinstance(claim_id, str) and CLAIM_ID_RE.fullmatch(claim_id)):
                    errors.append(err("U065", f"{ref_location}.claim_id is invalid"))
                    continue
                if not nonempty(role):
                    errors.append(err("U066", f"{ref_location}.role must be non-empty"))
                previous = claim_roles.get(claim_id)
                if previous is not None and previous != role:
                    errors.append(err("U069", f"{location} assigns conflicting roles", claim_id=claim_id))
                else:
                    claim_roles[claim_id] = role
                routed_claim_ids.add(claim_id)

            context_refs = string_array(
                element.get("writing_context_refs", []),
                f"{element_location}.writing_context_refs", "U067", errors,
            )
            for context_id in context_refs:
                if not WRITING_CONTEXT_ID_RE.fullmatch(context_id):
                    errors.append(err("U067", f"{element_location} contains invalid writing context id"))

    missing_visual_refs = l0_refs - routed_claim_ids
    if missing_visual_refs:
        errors.append(err("U025", "visual refs must be routed by content units", missing=sorted(missing_visual_refs)))
    return errors, warnings


def _required_routes(outline_data: dict, unit_id: str) -> tuple[set[str], dict[str, str], set[str]]:
    unit = next(
        (item for item in outline_data.get("content_units", []) if isinstance(item, dict) and item.get("id") == unit_id),
        None,
    )
    if unit is None:
        raise KeyError(unit_id)
    claim_ids: set[str] = set()
    roles: dict[str, str] = {}
    context_ids: set[str] = set()
    for element in unit.get("elements", []):
        if not isinstance(element, dict):
            continue
        for ref in element.get("evidence_refs", []):
            if isinstance(ref, dict) and isinstance(ref.get("claim_id"), str):
                claim_ids.add(ref["claim_id"])
                if isinstance(ref.get("role"), str):
                    roles.setdefault(ref["claim_id"], ref["role"])
        context_ids.update(
            value for value in element.get("writing_context_refs", []) if isinstance(value, str)
        )
    return claim_ids, roles, context_ids


def validate_subset(
    subset_data: object,
    outline_data: dict,
    evidence_index: dict | None = None,
    writing_context_index: dict | None = None,
    source_index: dict | None = None,
    expected_unit_id: str | None = None,
) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(subset_data, dict):
        return [err("STRUCT", "Root must be a JSON object")]
    if not (isinstance(expected_unit_id, str) and CONTENT_UNIT_ID_RE.fullmatch(expected_unit_id)):
        return [err("U202", "expected content unit id is invalid")]
    try:
        required_claims, required_roles, required_contexts = _required_routes(
            outline_data, expected_unit_id
        )
    except KeyError:
        return [err("U203", f"content unit {expected_unit_id!r} not found in outline")]

    claims = subset_data.get("claims")
    contexts = subset_data.get("writing_context")
    sources = subset_data.get("sources")
    if not isinstance(claims, list):
        errors.append(err("U210", "claims must be an array"))
        claims = []
    if not isinstance(contexts, list):
        errors.append(err("U215", "writing_context must be an array"))
        contexts = []
    if not isinstance(sources, list):
        errors.append(err("U211", "sources must be an array"))
        sources = []

    claim_ids: set[str] = set()
    referenced_sources: set[str] = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or not isinstance(claim.get("id"), str):
            errors.append(err("U210", f"claims[{index}] must contain a string id"))
            continue
        claim_id = claim["id"]
        if claim_id in claim_ids:
            errors.append(err("U210", f"duplicate subset claim id: {claim_id}"))
        claim_ids.add(claim_id)
        if claim.get("narrative_role") != required_roles.get(claim_id):
            errors.append(err("U213", f"claim {claim_id} has the wrong narrative_role"))
        if evidence_index is not None:
            formal = evidence_index.get(claim_id)
            copied = dict(claim)
            copied.pop("narrative_role", None)
            if formal is None or copied != formal:
                errors.append(err("U214", f"claim {claim_id} must exactly copy formal evidence"))
        for evidence_item in claim.get("evidence", []):
            if isinstance(evidence_item, dict) and isinstance(evidence_item.get("source_id"), str):
                referenced_sources.add(evidence_item["source_id"])
    if claim_ids != required_claims:
        errors.append(err(
            "U210", "subset claims must exactly equal routed claims",
            missing_claim_ids=sorted(required_claims - claim_ids),
            extra_claim_ids=sorted(claim_ids - required_claims),
        ))

    context_ids: set[str] = set()
    for index, context in enumerate(contexts):
        if not isinstance(context, dict) or not isinstance(context.get("id"), str):
            errors.append(err("U215", f"writing_context[{index}] must contain a string id"))
            continue
        context_id = context["id"]
        if context_id in context_ids:
            errors.append(err("U215", f"duplicate writing context id: {context_id}"))
        context_ids.add(context_id)
        if writing_context_index is not None and context != writing_context_index.get(context_id):
            errors.append(err("U217", f"writing context {context_id} must exactly copy formal evidence"))
        referenced_sources.update(
            value for value in context.get("source_ids", []) if isinstance(value, str)
        )
    if context_ids != required_contexts:
        errors.append(err(
            "U215", "subset writing_context must exactly equal routed contexts",
            missing_context_ids=sorted(required_contexts - context_ids),
            extra_context_ids=sorted(context_ids - required_contexts),
        ))

    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            errors.append(err("U211", f"sources[{index}] must contain a string id"))
            continue
        source_id = source["id"]
        if source_id in source_ids:
            errors.append(err("U211", f"duplicate subset source id: {source_id}"))
        source_ids.add(source_id)
        if source_index is not None and source != source_index.get(source_id):
            errors.append(err("U218", f"source {source_id} must exactly copy formal evidence"))
    if source_ids != referenced_sources:
        errors.append(err(
            "U211", "sources must exactly equal referenced source ids",
            missing=sorted(referenced_sources - source_ids),
            extra=sorted(source_ids - referenced_sources),
        ))
    return errors


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_template_ids(path: Path) -> set[str]:
    catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = catalog.get("templates") if isinstance(catalog, dict) else None
    if not isinstance(templates, list):
        raise ValueError("template catalog must contain templates[]")
    result = {
        item["id"] for item in templates
        if isinstance(item, dict) and nonempty(item.get("id"))
    }
    if len(result) != len(templates):
        raise ValueError("every report template must have a unique non-empty id")
    return result


def _index(evidence_paths: list[Path], field: str) -> dict:
    result: dict = {}
    for path in evidence_paths:
        value = load_json(path)
        for item in value.get(field, []) if isinstance(value, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                result[item["id"]] = item
    return result


def build_evidence_index(paths: list[Path]) -> dict:
    return _index(paths, "claims")


def build_writing_context_index(paths: list[Path]) -> dict:
    return _index(paths, "writing_context")


def build_source_index(paths: list[Path]) -> dict:
    return _index(paths, "sources")


def compute_stats(data: dict) -> dict:
    units = data.get("content_units", [])
    l0 = data.get("L0_draft") or {}
    return {
        "report_profile": data.get("report_profile") or {},
        "paradigm": data.get("paradigm") or {},
        "headline": l0.get("headline"),
        "primary_unit_types": sorted({
            unit.get("type") for unit in units
            if isinstance(unit, dict) and unit.get("role") == "primary" and isinstance(unit.get("type"), str)
        }),
        "content_units_count": len(units),
        "primary_units_count": sum(
            1 for unit in units if isinstance(unit, dict) and unit.get("role") == "primary"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate outline and derived content tasks.")
    parser.add_argument("outline", nargs="?")
    parser.add_argument("--node-context", action="store_true")
    parser.add_argument("--subsets")
    parser.add_argument("--evidence", nargs="*", default=[])
    args = parser.parse_args()
    context = None
    if args.node_context:
        context = json.loads(Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8"))
        args.outline = context["outputs"]["outline"]["path"]
        args.subsets = context["outputs"]["content-tasks"]["directory"]
        args.evidence = [item["path"] for item in context["inputs"].get("evidence", [])]
    if not args.outline:
        parser.error("outline is required unless --node-context is used")

    outline_path = Path(args.outline)
    try:
        outline = load_json(outline_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [err("JSON", str(exc))]}, ensure_ascii=False))
        raise SystemExit(2)

    expected_report_format = None
    template_ids = None
    catalog_error = None
    if context is not None:
        expected_report_format = context.get("run", {}).get("report_format", "formal_report")
        catalog_path = context.get("resources", {}).get("report_templates.yaml")
        try:
            if not catalog_path:
                raise ValueError("report_templates.yaml resource is missing")
            template_ids = load_template_ids(Path(catalog_path))
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
            catalog_error = err("U005", f"invalid report template catalog: {exc}")

    errors, warnings = validate_outline(
        outline, expected_report_format=expected_report_format, template_ids=template_ids
    )
    if catalog_error is not None:
        errors.append(catalog_error)
    if args.subsets:
        subset_root = Path(args.subsets)
        subset_paths = sorted(subset_root.glob("*.evidence_subset.json")) if subset_root.is_dir() else []
        evidence_paths = [Path(value) for value in args.evidence]
        claim_index = build_evidence_index(evidence_paths) if evidence_paths else None
        context_index = build_writing_context_index(evidence_paths) if evidence_paths else None
        source_index = build_source_index(evidence_paths) if evidence_paths else None
        expected_units = {
            item.get("id") for item in outline.get("content_units", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        seen: Counter[str] = Counter()
        suffix = ".evidence_subset.json"
        for subset_path in subset_paths:
            unit_id = subset_path.name[:-len(suffix)] if subset_path.name.endswith(suffix) else ""
            if unit_id not in expected_units:
                warnings.append(warn("U200", "ignoring stale evidence subset", file=str(subset_path)))
                continue
            seen[unit_id] += 1
            try:
                subset = load_json(subset_path)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(err("JSON", str(exc), file=str(subset_path)))
                continue
            for item in validate_subset(
                subset, outline, claim_index, context_index, source_index, unit_id
            ):
                item["file"] = str(subset_path)
                errors.append(item)
        missing = sorted(expected_units - set(seen))
        duplicates = {unit_id: count for unit_id, count in seen.items() if count != 1}
        if missing or duplicates:
            errors.append(err(
                "U200", "each content unit must have exactly one evidence subset",
                missing_content_units=missing, duplicate_content_units=duplicates,
            ))

    payload: dict = {"ok": not errors}
    if errors:
        payload["errors"] = errors
    if warnings:
        payload["warnings"] = warnings
    if not errors:
        payload["stats"] = compute_stats(outline)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
