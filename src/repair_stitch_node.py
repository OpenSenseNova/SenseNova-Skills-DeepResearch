from __future__ import annotations

import os
import sys
from pathlib import Path

from .stitching import StitchContractError, assemble_report, claim_source_ids, read_json


def _drafts(items: list[dict]) -> dict[str, str]:
    return {
        str(item.get("scope", {}).get("content-unit-id")): Path(item["path"]).read_text(encoding="utf-8")
        for item in items
    }


def main() -> int:
    try:
        context = read_json(os.environ["DEEPRESEARCH_NODE_CONTEXT"])
        outline = read_json(context["inputs"]["outline"][-1]["path"])
        drafts = _drafts(context["inputs"].get("drafts", []))
        repairs = _drafts(context["inputs"].get("repairs", []))
        unknown = sorted(set(repairs) - set(drafts))
        if unknown:
            raise StitchContractError(f"repairs target unknown content units: {unknown}")
        drafts.update(repairs)

        allowed_sources: set[str] = set()
        routed_claim_sources: dict[str, set[str]] = {}
        for item in context["inputs"].get("evidence", []):
            evidence = read_json(item["path"])
            allowed_sources.update(
                str(source["id"])
                for source in evidence.get("sources", [])
                if isinstance(source, dict) and source.get("id")
            )
            for claim_id, ids in claim_source_ids(evidence).items():
                routed_claim_sources.setdefault(claim_id, set()).update(ids)

        result = assemble_report(
            query=str(context["run"]["query"]),
            language=str(context["run"]["language"]),
            outline=outline,
            drafts=drafts,
            allowed_source_ids=allowed_sources,
            routed_claim_sources=routed_claim_sources,
        )
        output = Path(context["outputs"]["stitched"]["path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result, encoding="utf-8")
        return 0
    except (KeyError, OSError, UnicodeError, ValueError, TypeError, StitchContractError) as exc:
        print(f"repair stitch failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
