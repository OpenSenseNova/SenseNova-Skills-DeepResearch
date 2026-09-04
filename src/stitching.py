from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .node_validators.report_markdown import validate_common


_H1 = re.compile(r"(?m)^#\s+")
_ORDERED_ITEM = re.compile(r"(?m)^\s*\d+\.\s+\S")
_CHECK_ITEM = re.compile(r"(?mi)^\s*-\s+\[(?: |x)\]\s+\S")
_CITATION = re.compile(r"\[\^([^\]\s]+)\]")
_SAFE_CITATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SUBHEADING = re.compile(r"^(#{3,4})\s+(.+?)\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:\d+\.|[-*+]\s+\[(?: |x|X)\]|[-*+])\s+(.+)$")


class StitchContractError(ValueError):
    pass


def read_json(path: str | Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise StitchContractError(f"JSON input must be an object: {path}")
    return value


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _table_headers(text: str) -> list[list[str]]:
    lines = text.splitlines()
    headers: list[list[str]] = []
    for index in range(len(lines) - 1):
        header = lines[index].strip()
        divider = lines[index + 1].strip()
        if "|" not in header or "|" not in divider:
            continue
        cells = [cell.strip() for cell in divider.strip("|").split("|")]
        if not cells or not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        headers.append([cell.strip() for cell in header.strip("|").split("|")])
    return headers


def claim_source_ids(evidence: Mapping[str, Any]) -> dict[str, set[str]]:
    """Return the deterministic claim -> source routing encoded in evidence."""
    result: dict[str, set[str]] = {}
    for claim in evidence.get("claims", []):
        if not isinstance(claim, Mapping) or not isinstance(claim.get("id"), str):
            continue
        result[str(claim["id"])] = {
            str(item["source_id"])
            for item in claim.get("evidence", [])
            if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
        }
    return result


def _plain_cell(value: str) -> str:
    value = _CITATION.sub("", value)
    value = re.sub(r"[*_`~]", "", value)
    return " ".join(value.split()).strip()


def _table_rows(text: str) -> list[tuple[list[str], list[str]]]:
    """Return (header, row) pairs for ordinary Markdown table data rows."""
    lines = text.splitlines()
    rows: list[tuple[list[str], list[str]]] = []
    index = 0
    while index + 1 < len(lines):
        header_line = lines[index].strip()
        divider_line = lines[index + 1].strip()
        divider = [cell.strip() for cell in divider_line.strip("|").split("|")]
        if "|" not in header_line or not divider or not all(
            re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in divider
        ):
            index += 1
            continue
        header = [cell.strip() for cell in header_line.strip("|").split("|")]
        index += 2
        while index < len(lines):
            line = lines[index].strip()
            if not line or "|" not in line or line.startswith("#"):
                break
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) == len(header):
                rows.append((header, cells))
            index += 1
    return rows


def _heading_sections(text: str) -> tuple[dict[str, list[str]], list[tuple[int, str]]]:
    """Parse H3/H4 sections without treating fenced-code content as headings."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    fenced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _SUBHEADING.match(line.strip())
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    sections: dict[str, list[str]] = {}
    public = [(level, title) for _, level, title in headings]
    for position, (start, _, title) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        sections.setdefault(title, []).append("\n".join(lines[start:end]))
    return sections, public


def _primary_element_region(unit: Mapping[str, Any], text: str, label: str) -> str:
    mode = unit.get("render_contract", {}).get("mode")
    matches: list[str] = []
    if mode == "markdown_table":
        for header, cells in _table_rows(text):
            if any(_plain_cell(cell) == label for cell in cells):
                matches.append("| " + " | ".join(cells) + " |")
    elif mode in {"ordered_list", "checklist"}:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = _LIST_ITEM.match(line)
            if not match or not _plain_cell(match.group(1)).startswith(label):
                continue
            block = [line]
            for continuation in lines[index + 1:]:
                if _LIST_ITEM.match(continuation) or continuation.startswith("#"):
                    break
                block.append(continuation)
            matches.append("\n".join(block))
    return "\n".join(matches)


def _element_regions(unit: Mapping[str, Any], text: str) -> dict[str, str]:
    elements = [item for item in unit.get("elements", []) if isinstance(item, Mapping)]
    sections, _ = _heading_sections(text)
    result: dict[str, str] = {}
    for element in elements:
        element_id = str(element.get("id") or "")
        label = str(element.get("label") or "")
        parts = []
        primary = _primary_element_region(unit, text, label)
        if primary:
            parts.append(primary)
        parts.extend(sections.get(label, []))
        if not parts and len(elements) == 1:
            parts.append(text)
        result[element_id] = "\n".join(parts)
    return result


def _contract_error(message: str, *, rule: str, unit_id: str, **fields: object) -> dict:
    return {
        "rule": rule,
        "severity": "error",
        "message": message,
        "unit_id": unit_id,
        **fields,
    }


def validate_unit_contract(
    unit: Mapping[str, Any],
    text: str,
    *,
    allowed_source_ids: set[str] | None = None,
    routed_claim_sources: Mapping[str, set[str]] | None = None,
) -> list[dict]:
    """Validate one draft with stable, machine-readable unit diagnostics."""
    unit_id = str(unit.get("id") or "<unknown>")
    errors: list[dict] = []
    if not text.strip():
        return [_contract_error("draft is empty", rule="UNIT_EMPTY", unit_id=unit_id)]
    if _H1.search(text):
        errors.append(_contract_error("content-unit draft must not contain an H1", rule="UNIT_H1", unit_id=unit_id))
    for item in validate_common(text, allowed_source_ids=allowed_source_ids):
        errors.append({**item, "unit_id": unit_id})

    unsafe = sorted({key for key in _CITATION.findall(text) if not _SAFE_CITATION.fullmatch(key)})
    if unsafe:
        errors.append(_contract_error("unsafe citation keys", rule="UNIT_CITATION_KEY", unit_id=unit_id, keys=unsafe))

    render = unit.get("render_contract")
    if not isinstance(render, Mapping):
        return errors + [_contract_error("render_contract is missing", rule="UNIT_RENDER", unit_id=unit_id)]
    heading = f"## {unit.get('title')}"
    lines = _nonempty_lines(text)
    count = sum(line.strip() == heading for line in text.splitlines())
    if render.get("show_heading") is True and (not lines or lines[0] != heading or count != 1):
        errors.append(_contract_error("draft must start with exactly one contracted unit heading", rule="UNIT_HEADING", unit_id=unit_id, expected=heading, count=count))
    elif render.get("show_heading") is False and count:
        errors.append(_contract_error("draft must omit the contracted unit heading", rule="UNIT_HEADING", unit_id=unit_id, heading=heading))

    try:
        _validate_render_contract(unit, text)
    except StitchContractError as exc:
        errors.append(_contract_error(str(exc), rule="UNIT_RENDER", unit_id=unit_id))

    mode = render.get("mode")
    element_labels = [
        str(item.get("label") or "")
        for item in unit.get("elements", [])
        if isinstance(item, Mapping)
    ]
    if mode == "markdown_table":
        actual_labels: list[str] = []
        for _, cells in _table_rows(text):
            exact = [label for label in element_labels if any(_plain_cell(cell) == label for cell in cells)]
            actual_labels.extend(exact)
        if actual_labels != element_labels:
            errors.append(_contract_error("table must contain exactly one row per element in contracted order", rule="UNIT_ELEMENT_ORDER", unit_id=unit_id, expected=element_labels, actual=actual_labels))
    elif mode in {"ordered_list", "checklist"}:
        actual_labels = []
        for line in text.splitlines():
            match = _LIST_ITEM.match(line)
            if not match:
                continue
            item_text = _plain_cell(match.group(1))
            actual_labels.extend(label for label in element_labels if item_text.startswith(label))
        if actual_labels != element_labels:
            errors.append(_contract_error("list must contain exactly one item per element in contracted order", rule="UNIT_ELEMENT_ORDER", unit_id=unit_id, expected=element_labels, actual=actual_labels))

    secondary = render.get("secondary_structure")
    sections, headings = _heading_sections(text)
    elements = [item for item in unit.get("elements", []) if isinstance(item, Mapping)]
    labels = element_labels
    if not isinstance(secondary, Mapping):
        errors.append(_contract_error("secondary_structure contract is missing", rule="UNIT_CONTRACT", unit_id=unit_id))
    else:
        allowed = secondary.get("allowed") is True
        required = secondary.get("required") is True
        level = secondary.get("heading_level")
        if not allowed and headings:
            errors.append(_contract_error("secondary H3/H4 structure is forbidden", rule="UNIT_SECONDARY_STRUCTURE", unit_id=unit_id, headings=[title for _, title in headings]))
        if allowed:
            unexpected = [title for heading_level, title in headings if heading_level != level or title not in labels]
            if unexpected:
                errors.append(_contract_error("secondary headings must exactly match contracted element labels and level", rule="UNIT_SECONDARY_STRUCTURE", unit_id=unit_id, headings=unexpected, expected_level=level))
            if required:
                missing = [label for label in labels if len(sections.get(label, [])) != 1]
                actual = [title for heading_level, title in headings if heading_level == level]
                if missing or actual != labels:
                    errors.append(_contract_error("required element sections are missing, duplicated, or out of order", rule="UNIT_SECONDARY_STRUCTURE", unit_id=unit_id, expected=labels, actual=actual, missing=missing))

    citation_policy = render.get("citation_policy")
    if not isinstance(citation_policy, Mapping):
        errors.append(_contract_error("citation_policy contract is missing", rule="UNIT_CONTRACT", unit_id=unit_id))
    elif routed_claim_sources is not None:
        require_each = citation_policy.get("require_each_claim") is True
        scope = citation_policy.get("scope")
        regions = _element_regions(unit, text)
        for element in elements:
            element_id = str(element.get("id") or "<unknown>")
            region = text if scope == "unit" else regions.get(element_id, "")
            refs = [ref for ref in element.get("evidence_refs", []) if isinstance(ref, Mapping)]
            if refs and not region:
                errors.append(_contract_error("cannot locate the contracted element region", rule="UNIT_ELEMENT_REGION", unit_id=unit_id, element_id=element_id, label=element.get("label")))
                continue
            cited = set(_CITATION.findall(region))
            if require_each:
                for ref in refs:
                    claim_id = str(ref.get("claim_id") or "")
                    expected_sources = set(routed_claim_sources.get(claim_id, set()))
                    if not expected_sources:
                        errors.append(_contract_error("routed claim has no source mapping in the evidence subset", rule="UNIT_CLAIM_SOURCE", unit_id=unit_id, element_id=element_id, claim_id=claim_id))
                    elif cited.isdisjoint(expected_sources):
                        errors.append(_contract_error("routed claim is not cited in its contracted element", rule="UNIT_CITATION_COVERAGE", unit_id=unit_id, element_id=element_id, claim_id=claim_id, expected_source_ids=sorted(expected_sources), cited_source_ids=sorted(cited)))

        required_fields = citation_policy.get("required_fields", [])
        if render.get("mode") == "markdown_table" and isinstance(required_fields, list):
            for element in elements:
                label = str(element.get("label") or "")
                element_id = str(element.get("id") or "<unknown>")
                matching_rows = [(header, cells) for header, cells in _table_rows(text) if any(_plain_cell(cell) == label for cell in cells)]
                for field in required_fields:
                    for header, cells in matching_rows:
                        if field in header:
                            cell = cells[header.index(field)]
                            if not _CITATION.search(cell):
                                errors.append(_contract_error("required table cell has no inline citation", rule="UNIT_TABLE_CELL_CITATION", unit_id=unit_id, element_id=element_id, field=field))
    return errors


def _validate_render_contract(unit: Mapping[str, Any], text: str) -> None:
    render = unit.get("render_contract")
    if not isinstance(render, Mapping):
        raise StitchContractError(f"unit {unit.get('id')} has no render_contract")
    mode = render.get("mode")
    unit_id = unit.get("id")
    if mode == "markdown_table":
        headers = _table_headers(text)
        if not headers:
            raise StitchContractError(f"unit {unit_id} requires a Markdown table")
        schema = [str(item).strip() for item in render.get("schema", [])]
        if schema and not any(all(field in header for field in schema) for header in headers):
            raise StitchContractError(
                f"unit {unit_id} table header does not contain contracted schema: {schema}"
            )
    elif mode == "ordered_list" and not _ORDERED_ITEM.search(text):
        raise StitchContractError(f"unit {unit_id} requires an ordered list")
    elif mode == "checklist" and not _CHECK_ITEM.search(text):
        raise StitchContractError(f"unit {unit_id} requires a checklist")
    elif mode == "callout" and not any(line.startswith(">") for line in text.splitlines()):
        raise StitchContractError(f"unit {unit_id} requires a Markdown callout")
    elif mode == "mermaid" and "```mermaid" not in text:
        raise StitchContractError(f"unit {unit_id} requires a Mermaid block")
    elif mode == "qa":
        missing = [
            str(item.get("label"))
            for item in unit.get("elements", [])
            if isinstance(item, Mapping) and str(item.get("label")) not in text
        ]
        if missing:
            raise StitchContractError(f"unit {unit_id} is missing QA labels: {missing}")


def validate_unit(
    unit: Mapping[str, Any],
    text: str,
    *,
    allowed_source_ids: set[str] | None = None,
    routed_claim_sources: Mapping[str, set[str]] | None = None,
) -> None:
    unit_id = str(unit.get("id") or "<unknown>")
    errors = validate_unit_contract(
        unit,
        text,
        allowed_source_ids=allowed_source_ids,
        routed_claim_sources=routed_claim_sources,
    )
    if errors:
        details = "; ".join(
            f"{item['rule']} unit={item.get('unit_id')}"
            + (f" element={item.get('element_id')}" if item.get("element_id") else "")
            + (f" claim={item.get('claim_id')}" if item.get("claim_id") else "")
            + f": {item['message']}"
            for item in errors
        )
        raise StitchContractError(details)


def _report_title(query: str) -> str:
    title = " ".join(query.split()).strip().rstrip("。.!?！？")
    if not title:
        raise StitchContractError("run query cannot produce an empty report title")
    return title


def _render_l0(outline: Mapping[str, Any], language: str) -> list[str]:
    decision = outline.get("organization_decision", {})
    opening = decision.get("opening_summary") if isinstance(decision, Mapping) else None
    value = outline.get("L0_draft")
    if opening == "none":
        if value is not None:
            raise StitchContractError("opening_summary=none requires L0_draft=null")
        return []
    if opening not in {"findings", "recommendation"} or not isinstance(value, Mapping):
        raise StitchContractError("opening summary requires a valid L0_draft")
    headline = value.get("headline")
    findings = value.get("key_findings")
    if not isinstance(headline, str) or not headline.strip():
        raise StitchContractError("L0_draft.headline must be non-empty")
    if not isinstance(findings, list) or not findings or not all(
        isinstance(item, str) and item.strip() for item in findings
    ):
        raise StitchContractError("L0_draft.key_findings must be non-empty strings")
    zh = language.casefold().startswith("zh")
    label = (
        "建议摘要" if opening == "recommendation" else "主要发现"
    ) if zh else ("Recommendation" if opening == "recommendation" else "Key Findings")
    return [f"## {label}", "", f"**{headline.strip()}**", "", *[f"- {item.strip()}" for item in findings]]


def assemble_report(
    *,
    query: str,
    language: str,
    outline: Mapping[str, Any],
    drafts: Mapping[str, str],
    allowed_source_ids: set[str] | None = None,
    routed_claim_sources: Mapping[str, set[str]] | None = None,
) -> str:
    units = outline.get("content_units")
    if not isinstance(units, list) or not units:
        raise StitchContractError("outline.content_units must be a non-empty list")
    ids = [str(unit.get("id")) for unit in units if isinstance(unit, Mapping)]
    if len(ids) != len(units) or len(ids) != len(set(ids)):
        raise StitchContractError("outline content unit ids must be unique")
    if set(drafts) != set(ids):
        raise StitchContractError(
            f"draft/unit mismatch: expected {ids}, received {sorted(drafts)}"
        )

    sections = [f"# {_report_title(query)}", ""]
    l0 = _render_l0(outline, language)
    if l0:
        sections.extend(l0 + [""])
    decision = outline.get("organization_decision", {})
    if isinstance(decision, Mapping) and decision.get("toc") is True:
        sections.extend(["<!-- TOC will be inserted by render stage -->", ""])

    input_citations = []
    for index, unit in enumerate(units):
        unit_id = ids[index]
        text = drafts[unit_id].strip()
        validate_unit(
            unit,
            text,
            allowed_source_ids=allowed_source_ids,
            routed_claim_sources=routed_claim_sources,
        )
        input_citations.extend(_CITATION.findall(text))
        sections.extend([text, ""])
    result = "\n".join(sections).rstrip() + "\n"
    if sorted(_CITATION.findall(result)) != sorted(input_citations):
        raise StitchContractError("stitch assembly changed citation keys")
    return result
