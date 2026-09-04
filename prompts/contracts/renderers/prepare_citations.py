#!/usr/bin/env python3
"""引用预处理 v2.2 — 渲染最终报告 (引用编号 + L0 摘要框 + TOC + 参考文献)。

v2.0:从所有 evidence.json 收集 sources,按 URL 去重,将终稿中的
     [^source_id] 替换为 [N],追加 ## 参考文献。
v2.1:新增 --outline 与 --output 参数。当传入 --outline 时,自动启用 L0
     摘要框渲染与 TOC 自动生成 (除非用 --no-l0 / --no-toc 关闭)。
v2.2:对 claim.id 泄漏、孤立引用、脚注定义和预生成参考文献章执行硬校验；
     这些错误必须由 writer/review 修复，渲染器不再猜测或自动放行。

用法:

  # 无 outline
  python3 prepare_citations.py --report report.md \\
    --evidence d1.evidence.json d2.evidence.json ... \
    --output rendered-report.md

  # v2.1 完整渲染模式 (stitched.md → report.md)
  python3 prepare_citations.py --report stitched.md \\
    --evidence d1.evidence.json d2.evidence.json ... \\
    --outline outline.json \\
    --output report.md

输出:
  - 终稿文件 (--output):[^key] → [N] + L0 框 + TOC + 参考文献
  - 同目录写入 citations.json
  - stdout:结构化结果 (含 orphan 警告、TOC 项数、L0 状态)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse, urlunparse


# ─── Citation 数据类 ─────────────────────────────────────────────────────────

@dataclass
class Citation:
    primary_id: str
    url: str
    title: str
    quality: str = "tertiary"
    aliases: set = field(default_factory=set)
    index: int = 0
    source_type: str = ""
    document_ref: str = ""
    knowledge_base_path: str = ""


# ─── URL 标准化 ──────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    """小写 scheme/host,去 path 尾斜杠,丢 fragment。"""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") if parsed.path != "/" else ""
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def _is_safe_local_source(src: dict) -> bool:
    """Defense-in-depth for file links when this renderer is called directly."""

    url = src.get("url")
    root_value = src.get("knowledge_base_path")
    ref_value = src.get("document_ref")
    if not (
        isinstance(url, str)
        and url.startswith("file://")
        and isinstance(root_value, str)
        and root_value
        and isinstance(ref_value, str)
        and ref_value
        and "\x00" not in url
        and "\x00" not in root_value
        and "\x00" not in ref_value
    ):
        return False

    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme.lower() != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        return False

    root = Path(root_value)
    raw_ref_parts = ref_value.split("/")
    ref = PurePosixPath(ref_value)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not root.is_dir()
        or "\\" in ref_value
        or ref.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_ref_parts)
        or ref.as_posix() != ref_value
    ):
        return False

    try:
        resolved_root = root.resolve()
        expected = resolved_root.joinpath(*ref.parts).resolve()
    except (OSError, RuntimeError):
        return False
    try:
        expected.relative_to(resolved_root)
    except ValueError:
        return False
    decoded_path = unquote(parsed.path)
    if "\x00" in decoded_path:
        return False
    decoded_url_path = Path(decoded_path)
    try:
        resolved_url_path = decoded_url_path.resolve()
    except (OSError, RuntimeError):
        return False
    return (
        decoded_url_path.is_absolute()
        and resolved_url_path == expected
        and expected.is_file()
    )


# ─── 收集所有 evidence.json 中的 sources ─────────────────────────────────────

def collect_sources(evidence_paths):
    """
    Returns (pool, alias_map):
      pool[primary_id] = Citation
      alias_map[any_id] = primary_id
    Same-URL sources across files merge to one Citation; first-seen id wins primary.
    Reusing one source id for different normalized URLs is rejected.
    """
    pool = {}
    url_to_primary = {}
    source_id_to_normalized_url = {}
    alias_map = {}

    for path in evidence_paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARN: 跳过 {path}: {e}", file=sys.stderr)
            continue

        sources = data.get("sources", [])
        if isinstance(sources, list):
            for src in sources:
                if not isinstance(src, dict):
                    continue
                sid = src.get("id")
                url = src.get("url")
                if not sid or not url:
                    continue
                title = src.get("title") or sid
                quality = src.get("quality") or "tertiary"
                source_type = src.get("source_type") or ""
                document_ref = src.get("document_ref") or ""
                knowledge_base_path = src.get("knowledge_base_path") or ""
                if (
                    source_type == "local_knowledge_base"
                    and not _is_safe_local_source(src)
                ):
                    print(
                        f"WARN: 跳过不安全的本地知识库 source: {sid}",
                        file=sys.stderr,
                    )
                    continue
                try:
                    norm = _normalize_url(url)
                except Exception:
                    continue

                previous_url = source_id_to_normalized_url.get(sid)
                if previous_url is not None and previous_url != norm:
                    raise ValueError(
                        "source_id %r maps to conflicting normalized URLs: %r and %r"
                        % (sid, previous_url, norm)
                    )
                source_id_to_normalized_url[sid] = norm

                if norm not in url_to_primary:
                    url_to_primary[norm] = sid
                    pool[sid] = Citation(
                        primary_id=sid,
                        url=url,
                        title=title,
                        quality=quality,
                        source_type=source_type,
                        document_ref=document_ref,
                        knowledge_base_path=knowledge_base_path,
                    )
                primary_id = url_to_primary[norm]
                alias_map[sid] = primary_id
                pool[primary_id].aliases.add(sid)

    return pool, alias_map


# ─── [^key] → [N] 替换 ───────────────────────────────────────────────────────

# `[^key]` 引用 (不是定义),key 允许字母数字 - _ .
_FOOTNOTE_REF_RE = re.compile(r"\[\^([\w\-.]+)\](?!:)")
# `[^key]:` 行首定义 (清理掉,LLM 偶尔会写)
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([\w\-.]+)\]:\s*.+$", re.MULTILINE)
# claim.id 形式引用,writer 偶尔会误用 (应该用 source_id)
_CLAIM_ID_REF_RE = re.compile(r"\[\^(d\d+\.c\d+)\]")


def process_citations(text, pool, alias_map):
    """Walk [^key] in order; assign [N] indices via primary_id; replace; strip leftover defs."""
    key_to_index = {}
    ordered_citations = []
    orphans = []
    counter = 0

    for match in _FOOTNOTE_REF_RE.finditer(text):
        key = match.group(1)
        primary_id = alias_map.get(key)
        if primary_id is None:
            if key not in orphans:
                orphans.append(key)
            continue
        if primary_id in key_to_index:
            continue
        counter += 1
        pool[primary_id].index = counter
        key_to_index[primary_id] = counter
        ordered_citations.append(pool[primary_id])

    def replace_ref(m):
        k = m.group(1)
        primary_id = alias_map.get(k)
        if primary_id is None:
            return m.group(0)  # leave orphan as-is for visibility
        idx = key_to_index.get(primary_id)
        return f"[{idx}]" if idx else m.group(0)

    processed = _FOOTNOTE_REF_RE.sub(replace_ref, text)
    processed = _FOOTNOTE_DEF_RE.sub("", processed)
    processed = re.sub(r"\n{3,}", "\n\n", processed).strip()

    return processed, ordered_citations, orphans


# ─── L0 摘要框 ────────────────────────────────────────────────────────────────

# Detect existing L0 box in stitched.md (stitcher may have added one)
_L0_MARKERS = ("> **核心摘要**", "> **Key Findings**", "> **TL;DR**")


def has_l0_box(text: str) -> bool:
    """Return True if the document already carries an L0 callout in its top region."""
    head = text[:2000]
    return any(marker in head for marker in _L0_MARKERS)


def render_l0_box(L0_draft: dict) -> str:
    """Render outline.L0_draft as a top callout (markdown blockquote)."""
    findings = L0_draft.get("key_findings") or []
    lines = ["> **核心摘要**", ">"]
    for f in findings:
        if isinstance(f, str) and f.strip():
            lines.append(f"> {f.strip()}")
    return "\n".join(lines)


def insert_l0_after_h1(text: str, l0_md: str) -> str:
    """Insert L0 callout right after the first H1; fall back to prepend if no H1."""
    lines = text.split("\n")
    h1_idx = None
    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            h1_idx = i
            break
    if h1_idx is None:
        return f"{l0_md}\n\n{text}"

    # Find the first non-empty line after H1
    insert_pos = h1_idx + 1
    while insert_pos < len(lines) and lines[insert_pos].strip() == "":
        insert_pos += 1

    new_lines = lines[:insert_pos] + ["", l0_md, ""] + lines[insert_pos:]
    return "\n".join(new_lines)


# ─── TOC 自动生成 ─────────────────────────────────────────────────────────────

# Match H2 (## title) or H3 (### title), but not H4+
_HEADING_RE = re.compile(r"^(##|###)\s+(.+?)\s*$", re.MULTILINE)
_TOC_PLACEHOLDER = "<!-- TOC will be inserted by render stage -->"

# Headings to skip in TOC (would create circular refs / noise)
_TOC_SKIP_TITLES = {"参考文献", "References", "目录", "Table of Contents", "TOC"}


def slugify(s: str) -> str:
    """GitHub-style slug for anchor links. Keeps CJK characters."""
    # Lowercase
    s = s.strip().lower()
    # Strip leading citation markers like [1] [2]
    s = re.sub(r"^\[\d+\]\s*", "", s)
    # Replace runs of non-word, non-CJK, non-hyphen with single hyphen
    s = re.sub(r"[^\w一-鿿\-]+", "-", s)
    # Collapse multiple hyphens
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "section"


def render_toc(text: str) -> tuple[str, int]:
    """Scan H2/H3 headings, return (toc_markdown, item_count)."""
    items = []
    for match in _HEADING_RE.finditer(text):
        level, title = match.group(1), match.group(2).strip()
        # Strip trailing markdown punctuation
        title_clean = title.rstrip("#").strip()
        if title_clean in _TOC_SKIP_TITLES:
            continue
        anchor = slugify(title_clean)
        indent = "" if level == "##" else "  "
        items.append(f"{indent}- [{title_clean}](#{anchor})")

    toc_body = "\n".join(items) if items else "_(no headings found)_"
    return toc_body, len(items)


def insert_toc(text: str, toc_body: str) -> str:
    """Replace placeholder if present, else insert before the first H2."""
    toc_block = f"## 目录\n\n{toc_body}"
    if _TOC_PLACEHOLDER in text:
        return text.replace(_TOC_PLACEHOLDER, toc_block)
    # No placeholder — insert before the first H2 that isn't itself the L0 callout's H2
    lines = text.split("\n")
    insert_at = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and not line.startswith("### "):
            title = line[3:].strip()
            if title not in _TOC_SKIP_TITLES:
                insert_at = i
                break
    if insert_at is None:
        # No H2 anywhere — append at end
        return f"{text}\n\n{toc_block}"
    new_lines = lines[:insert_at] + [toc_block, ""] + lines[insert_at:]
    return "\n".join(new_lines)


# ─── 渲染参考文献 + citations.json ───────────────────────────────────────────

def render_bibliography(citations):
    return "\n".join(
        f"{c.index}. [{c.title}]({c.url})" for c in citations
    )


def export_citations_json(citations):
    rendered = []
    for citation in citations:
        item = {
            "index": citation.index,
            "id": citation.primary_id,
            "url": citation.url,
            "title": citation.title,
            "quality": citation.quality,
            "aliases": sorted(citation.aliases - {citation.primary_id}),
        }
        # Carry optional local provenance only when it is present.
        if citation.source_type:
            item["source_type"] = citation.source_type
        if citation.document_ref:
            item["document_ref"] = citation.document_ref
        if citation.knowledge_base_path:
            item["knowledge_base_path"] = citation.knowledge_base_path
        rendered.append(item)

    return {
        "schema_version": "2.1",
        "total_citations": len(citations),
        "citations": rendered,
    }


# ─── 主流水线 ────────────────────────────────────────────────────────────────

def render_full(text: str, pool, alias_map, outline_data, enable_l0: bool, enable_toc: bool):
    """
    Full v2.1 pipeline:
      1. [^key] → [N] (always; invalid keys are rejected by main before writing)
      2. Insert L0 callout (if enabled, outline provided, and not already present)
      3. Generate + insert TOC (if enabled)
      4. Append bibliography
    Returns (final_text, ordered_citations, orphans, render_info).
    """
    info = {
        "l0_inserted": False,
        "l0_already_present": False,
        "toc_items": 0,
        "toc_inserted": False,
    }

    processed, ordered_citations, orphans = process_citations(text, pool, alias_map)

    # L0 box
    if enable_l0 and outline_data:
        L0 = outline_data.get("L0_draft") or {}
        if has_l0_box(processed):
            info["l0_already_present"] = True
        elif L0.get("key_findings"):
            l0_md = render_l0_box(L0)
            processed = insert_l0_after_h1(processed, l0_md)
            info["l0_inserted"] = True

    # TOC
    if enable_toc:
        toc_body, toc_count = render_toc(processed)
        if toc_count > 0:
            processed = insert_toc(processed, toc_body)
            info["toc_inserted"] = True
            info["toc_items"] = toc_count

    # Bibliography
    if ordered_citations:
        bibliography = render_bibliography(ordered_citations)
        final = f"{processed}\n\n## 参考文献\n\n{bibliography}\n"
    else:
        final = processed + "\n"

    return final, ordered_citations, orphans, info


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="处理终稿引用 v2.1:[^source_id] → [N] + (L0/TOC) + 参考文献"
    )
    parser.add_argument("--report", required=True,
                        help="输入终稿文件路径 (stitched.md 或 report.md)")
    parser.add_argument("--evidence", nargs="+", required=True,
                        help="所有 evidence.json 文件路径 (≥1)")
    parser.add_argument("--outline",
                        help="outline.json 路径 (可选,启用 L0 摘要框 + TOC)")
    parser.add_argument("--output", required=True,
                        help="输出文件路径")
    parser.add_argument("--no-l0", action="store_true",
                        help="禁用 L0 摘要框渲染 (即使提供了 --outline)")
    parser.add_argument("--no-toc", action="store_true",
                        help="禁用 TOC 自动生成 (即使提供了 --outline)")
    parser.add_argument("--toc", action="store_true",
                        help="根据报告中的 TOC 占位符生成目录，无需 outline")
    args = parser.parse_args()

    report_path = Path(args.report).resolve()
    if not report_path.exists():
        print(json.dumps({"success": False, "error": f"输入文件不存在: {report_path}"},
                         ensure_ascii=False))
        sys.exit(1)

    output_path = Path(args.output).resolve()

    evidence_paths = [Path(p).resolve() for p in args.evidence]
    missing = [str(p) for p in evidence_paths if not p.exists()]
    if missing:
        print(json.dumps({"success": False, "error": f"evidence 文件不存在: {missing}"},
                         ensure_ascii=False))
        sys.exit(1)

    outline_data = None
    if args.outline:
        outline_path = Path(args.outline).resolve()
        if not outline_path.exists():
            print(json.dumps({"success": False, "error": f"outline 文件不存在: {outline_path}"},
                             ensure_ascii=False))
            sys.exit(1)
        try:
            outline_data = json.loads(outline_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(json.dumps({"success": False,
                              "error": f"outline.json 不是有效 JSON: {e}"},
                             ensure_ascii=False))
            sys.exit(1)

    try:
        pool, alias_map = collect_sources(evidence_paths)
    except ValueError as e:
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    if not pool:
        print(json.dumps({"success": False,
                          "error": "未从 evidence.json 收集到任何有效 source (id+url 对)"},
                         ensure_ascii=False))
        sys.exit(1)

    text = report_path.read_text(encoding="utf-8")
    leaked_claim_ids = sorted(set(_CLAIM_ID_REF_RE.findall(text)))
    invalid_structure = []
    if leaked_claim_ids:
        invalid_structure.append(f"claim-id citations: {leaked_claim_ids}")
    if _FOOTNOTE_DEF_RE.search(text):
        invalid_structure.append("footnote definitions")
    if re.search(r"(?im)^##\s+(参考文献|references)\s*$", text):
        invalid_structure.append("pre-rendered references section")
    if invalid_structure:
        print(json.dumps({
            "success": False,
            "error": "render input violates the citation contract: " + "; ".join(invalid_structure),
        }, ensure_ascii=False))
        sys.exit(1)

    # v2.1: 当提供了 --outline 时,默认启用 L0 + TOC (除非显式 --no-*)
    enable_l0 = bool(args.outline) and not args.no_l0
    enable_toc = args.toc or (bool(args.outline) and not args.no_toc)

    final_text, ordered_citations, orphans, render_info = render_full(
        text, pool, alias_map, outline_data, enable_l0, enable_toc
    )

    if orphans:
        print(json.dumps({
            "success": False,
            "error": "citations do not resolve to evidence sources",
            "orphan_citations": orphans,
        }, ensure_ascii=False))
        sys.exit(1)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(final_text, encoding="utf-8")

    # citations.json sits next to the output
    citations_path = output_path.parent / "citations.json"
    citations_data = export_citations_json(ordered_citations)
    citations_path.write_text(
        json.dumps(citations_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = {
        "success": True,
        "schema_version": "2.1",
        "input_report": str(report_path),
        "output_report": str(output_path),
        "citations_path": str(citations_path),
        "total_citations": len(ordered_citations),
        "evidence_files_scanned": [str(p) for p in evidence_paths],
        "sources_in_pool": len(pool),
        "deduped_aliases": sum(len(c.aliases) - 1 for c in pool.values() if len(c.aliases) > 1),
        "render": render_info,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
