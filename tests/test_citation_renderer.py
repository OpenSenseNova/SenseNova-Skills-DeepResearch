import json
import subprocess
import sys
from pathlib import Path


RENDERER = (
    Path(__file__).resolve().parents[1]
    / "prompts"
    / "contracts"
    / "renderers"
    / "prepare_citations.py"
)


def _write_evidence(path: Path, *, source_id: str, url: str) -> None:
    path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": source_id,
                        "url": url,
                        "title": source_id,
                        "quality": "primary",
                    }
                ],
                "claims": [],
            }
        ),
        encoding="utf-8",
    )


def _render(tmp_path: Path, sources: list[tuple[str, str]]):
    report = tmp_path / "draft.md"
    report.write_text(
        "# Report\n\n" + " ".join(f"[^{source_id}]" for source_id, _ in sources),
        encoding="utf-8",
    )
    evidence_paths = []
    for index, (source_id, url) in enumerate(sources, start=1):
        evidence = tmp_path / f"d{index}.evidence.json"
        _write_evidence(evidence, source_id=source_id, url=url)
        evidence_paths.append(evidence)
    output = tmp_path / "report.md"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(RENDERER),
            "--report",
            str(report),
            "--evidence",
            *(str(path) for path in evidence_paths),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, output


def test_renderer_rejects_one_source_id_for_different_normalized_urls(tmp_path):
    completed, output = _render(
        tmp_path,
        [
            ("official_source", "https://example.com/first"),
            ("official_source", "https://example.com/second"),
        ],
    )

    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert result["success"] is False
    assert "source_id 'official_source' maps to conflicting normalized URLs" in result[
        "error"
    ]
    assert "https://example.com/first" in result["error"]
    assert "https://example.com/second" in result["error"]
    assert not output.exists()
    assert not (tmp_path / "citations.json").exists()


def test_renderer_allows_same_source_id_for_same_normalized_url(tmp_path):
    completed, output = _render(
        tmp_path,
        [
            ("official_source", "HTTPS://EXAMPLE.COM/source/"),
            ("official_source", "https://example.com/source#section"),
        ],
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["success"] is True
    assert result["sources_in_pool"] == 1
    citations = json.loads((tmp_path / "citations.json").read_text(encoding="utf-8"))
    assert citations["total_citations"] == 1
    assert citations["citations"][0]["id"] == "official_source"
    assert output.is_file()


def test_renderer_merges_different_source_ids_for_same_normalized_url(tmp_path):
    completed, output = _render(
        tmp_path,
        [
            ("d1_official_source", "https://example.com/source/"),
            ("d2_official_source", "HTTPS://EXAMPLE.COM/source#section"),
        ],
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["success"] is True
    assert result["sources_in_pool"] == 1
    assert result["deduped_aliases"] == 1
    citations = json.loads((tmp_path / "citations.json").read_text(encoding="utf-8"))
    assert citations["total_citations"] == 1
    assert citations["citations"][0]["id"] == "d1_official_source"
    assert citations["citations"][0]["aliases"] == ["d2_official_source"]
    assert output.is_file()


def test_renderer_keeps_namespaced_source_ids_for_different_urls(tmp_path):
    completed, output = _render(
        tmp_path,
        [
            ("d1_official_source", "https://example.com/first"),
            ("d2_official_source", "https://example.com/second"),
        ],
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["success"] is True
    assert result["sources_in_pool"] == 2
    citations = json.loads((tmp_path / "citations.json").read_text(encoding="utf-8"))
    assert citations["total_citations"] == 2
    assert [item["id"] for item in citations["citations"]] == [
        "d1_official_source",
        "d2_official_source",
    ]
    assert output.is_file()


def test_renderer_rejects_orphan_citation_without_writing_output(tmp_path):
    completed, output = _render(
        tmp_path,
        [("missing_from_report", "https://example.com/source")],
    )
    (tmp_path / "draft.md").write_text("# Report\n\nUnknown.[^orphan]", encoding="utf-8")
    output.unlink()
    (tmp_path / "citations.json").unlink()
    completed = subprocess.run(completed.args, capture_output=True, text=True, check=False)

    assert completed.returncode == 1
    assert json.loads(completed.stdout)["orphan_citations"] == ["orphan"]
    assert not output.exists()
    assert not (tmp_path / "citations.json").exists()


def test_renderer_rejects_claim_id_citation_without_repair(tmp_path):
    completed, output = _render(
        tmp_path,
        [("d1_s1", "https://example.com/source")],
    )
    (tmp_path / "draft.md").write_text("# Report\n\nClaim.[^d1.c1]", encoding="utf-8")
    output.unlink()
    (tmp_path / "citations.json").unlink()
    completed = subprocess.run(completed.args, capture_output=True, text=True, check=False)

    assert completed.returncode == 1
    assert "claim-id citations" in json.loads(completed.stdout)["error"]
    assert not output.exists()
    assert not (tmp_path / "citations.json").exists()
