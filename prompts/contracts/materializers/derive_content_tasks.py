#!/usr/bin/env python3
"""Derive exact content-unit evidence subsets from outline references and evidence."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path


UNIT_ID_RE = re.compile(r"^u\d+$")


def load_object(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def reset_directory(path: str) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"derived output directory contains an unsafe entry: {child}")
        child.unlink()
    return directory


def append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def main() -> None:
    context = json.loads(
        Path(os.environ["DEEPRESEARCH_NODE_CONTEXT"]).read_text(encoding="utf-8")
    )
    outline_path = context["outputs"]["outline"]["path"]
    task_root = reset_directory(context["outputs"]["content-tasks"]["directory"])
    outline = load_object(outline_path)

    claims: dict[str, dict] = {}
    contexts: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    source_order: list[str] = []
    for item in context["inputs"].get("evidence", []):
        evidence = load_object(item["path"])
        for claim in evidence.get("claims", []):
            if not isinstance(claim, dict) or not isinstance(claim.get("id"), str):
                raise ValueError("evidence claims must contain string ids")
            if claim["id"] in claims:
                raise ValueError(f"duplicate evidence claim id: {claim['id']}")
            claims[claim["id"]] = claim
        for writing_context in evidence.get("writing_context", []):
            if not isinstance(writing_context, dict) or not isinstance(
                writing_context.get("id"), str
            ):
                raise ValueError("writing_context entries must contain string ids")
            if writing_context["id"] in contexts:
                raise ValueError(f"duplicate writing context id: {writing_context['id']}")
            contexts[writing_context["id"]] = writing_context
        for source in evidence.get("sources", []):
            if not isinstance(source, dict) or not isinstance(source.get("id"), str):
                raise ValueError("evidence sources must contain string ids")
            source_id = source["id"]
            if source_id in sources and sources[source_id] != source:
                raise ValueError(f"conflicting evidence source id: {source_id}")
            if source_id not in sources:
                sources[source_id] = source
                source_order.append(source_id)

    units = outline.get("content_units")
    if not isinstance(units, list) or not units:
        raise ValueError("outline.content_units must be a non-empty array")
    seen_units = set()
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("each content unit must be an object")
        unit_id = unit.get("id")
        if not isinstance(unit_id, str) or not UNIT_ID_RE.fullmatch(unit_id):
            raise ValueError(f"invalid content unit id: {unit_id!r}")
        if unit_id in seen_units:
            raise ValueError(f"duplicate content unit id: {unit_id}")
        seen_units.add(unit_id)
        if "evidence_subset" in unit:
            raise ValueError(
                f"content unit {unit_id} must omit runtime-owned evidence_subset"
            )

        claim_ids: list[str] = []
        context_ids: list[str] = []
        roles: dict[str, str] = {}
        elements = unit.get("elements")
        if not isinstance(elements, list):
            raise ValueError(f"content unit {unit_id} elements must be an array")
        for element in elements:
            if not isinstance(element, dict):
                raise ValueError(f"content unit {unit_id} contains a non-object element")
            for ref in element.get("evidence_refs", []):
                if not isinstance(ref, dict):
                    raise ValueError(f"content unit {unit_id} contains an invalid evidence ref")
                claim_id = ref.get("claim_id")
                role = ref.get("role")
                if claim_id not in claims:
                    raise ValueError(f"unknown claim id in {unit_id}: {claim_id!r}")
                previous_role = roles.get(claim_id)
                if previous_role is not None and previous_role != role:
                    raise ValueError(
                        f"claim {claim_id} has conflicting roles in {unit_id}: "
                        f"{previous_role!r} and {role!r}"
                    )
                roles[claim_id] = role
                append_unique(claim_ids, claim_id)
            for context_id in element.get("writing_context_refs", []):
                if context_id not in contexts:
                    raise ValueError(
                        f"unknown writing context id in {unit_id}: {context_id!r}"
                    )
                append_unique(context_ids, context_id)

        subset_claims = []
        referenced_sources: list[str] = []
        for claim_id in claim_ids:
            claim = copy.deepcopy(claims[claim_id])
            claim["narrative_role"] = roles[claim_id]
            subset_claims.append(claim)
            for evidence_item in claim.get("evidence", []):
                if not isinstance(evidence_item, dict):
                    raise ValueError(f"claim {claim_id} contains invalid evidence")
                source_id = evidence_item.get("source_id")
                if source_id not in sources:
                    raise ValueError(f"claim {claim_id} references unknown source {source_id!r}")
                append_unique(referenced_sources, source_id)

        subset_contexts = []
        for context_id in context_ids:
            writing_context = copy.deepcopy(contexts[context_id])
            subset_contexts.append(writing_context)
            for source_id in writing_context.get("source_ids", []):
                if source_id not in sources:
                    raise ValueError(
                        f"writing context {context_id} references unknown source {source_id!r}"
                    )
                append_unique(referenced_sources, source_id)

        ordered_source_ids = [sid for sid in source_order if sid in referenced_sources]
        subset = {
            "claims": subset_claims,
            "writing_context": subset_contexts,
            "sources": [copy.deepcopy(sources[sid]) for sid in ordered_source_ids],
        }
        (task_root / f"{unit_id}.evidence_subset.json").write_text(
            json.dumps(subset, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(json.dumps({"ok": True, "content_tasks": len(units)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"content task materialization failed: {exc}", file=sys.stderr)
        sys.exit(1)
