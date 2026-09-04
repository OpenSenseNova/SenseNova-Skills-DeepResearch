#!/usr/bin/env python3
"""Summarize persisted DeepResearch benchmark runs without model calls."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from deepresearch_cli.search.metrics import build_search_metrics


ROOT = Path(__file__).resolve().parents[1]
VERDICT = re.compile(r"\bVERDICT\s*:\s*(pass|revise|reject)\b", re.IGNORECASE)


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return values
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(float(item) for item in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    start, end = ordered[0]
    total = 0.0
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += (end - start).total_seconds()
            start, end = next_start, next_end
    return total + (end - start).total_seconds()


def evidence_urls(run_dir: Path) -> set[str]:
    urls: set[str] = set()
    for path in run_dir.glob("artifacts/*/attempt-*/*.json"):
        value = read_json(path)
        stack: list[Any] = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key == "url" and isinstance(nested, str) and nested.startswith(("http://", "https://")):
                        urls.add(nested)
                    else:
                        stack.append(nested)
            elif isinstance(item, list):
                stack.extend(item)
    return urls


def final_verdict(run_dir: Path) -> str | None:
    paths = list(run_dir.glob("artifacts/*/attempt-*/*final*review*.md"))
    # A non-pass final review is not promoted to artifacts, so inspect the
    # validated candidate (or staging fallback) for failed Heavy runs too.
    paths.extend(run_dir.glob("attempts/*final-review*/attempt-*/candidate/final-review.md"))
    paths.extend(run_dir.glob("attempts/*final-review*/attempt-*/staging/final-review.md"))
    # The diagnostic may be ``revise`` while the bounded recheck is ``pass``.
    # Prefer the terminal recheck over the earlier diagnostic.
    ordered = sorted(
        set(paths),
        key=lambda path: (0 if "final-review-recheck" in path.as_posix() else 1, path.as_posix()),
    )
    for path in ordered:
        try:
            match = VERDICT.search(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if match:
            return match.group(1).lower()
    return None


def tool_metrics(run_dir: Path, instance_steps: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    starts: dict[tuple[str, str], tuple[str, str]] = {}
    final_status: dict[tuple[str, str], str] = {}
    for path in run_dir.glob("attempts/*/attempt-*/acp-events.jsonl"):
        instance_id = path.parent.parent.name
        for event in read_jsonl(path):
            call_id = event.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                continue
            key = (str(path), call_id)
            update = event.get("sessionUpdate")
            if update == "tool_call":
                starts[key] = (str(event.get("kind") or "other"), instance_id)
            elif update == "tool_call_update" and event.get("status"):
                final_status[key] = str(event["status"])
    by_kind: Counter[str] = Counter()
    failed_by_kind: Counter[str] = Counter()
    by_step: Counter[str] = Counter()
    failed = completed = 0
    for key, (kind, instance_id) in starts.items():
        by_kind[kind] += 1
        by_step[instance_steps.get(instance_id, "unknown")] += 1
        status = final_status.get(key)
        if status == "failed":
            failed += 1
            failed_by_kind[kind] += 1
        elif status == "completed":
            completed += 1
    rows = [
        {"kind": kind, "calls": count, "failed": failed_by_kind[kind]}
        for kind, count in sorted(by_kind.items())
    ]
    return {
        "calls": sum(by_kind.values()),
        "completed": completed,
        "failed": failed,
        "unfinished": sum(by_kind.values()) - completed - failed,
        "by_step": dict(sorted(by_step.items())),
    }, rows


def usage_metrics(run_dir: Path) -> dict[str, int]:
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "cached_input_tokens": ("cached_read_tokens", "cachedInputTokens", "cache_read_tokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "reasoning_tokens": ("thought_tokens", "reasoningOutputTokens", "reasoning_tokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    totals = {key: 0 for key in aliases}
    for path in run_dir.glob("attempts/*/attempt-*/harness.json"):
        value = read_json(path)
        usage = value.get("usage")
        if not isinstance(usage, dict):
            continue
        for target, names in aliases.items():
            for name in names:
                amount = usage.get(name)
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    totals[target] += int(amount)
                    break
    return totals


def summarize_run(run_dir: Path, query_id: str, category: str, mode: str, repetition: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = read_json(run_dir / "run.json")
    events = read_jsonl(run_dir / "journal.jsonl")
    starts: dict[tuple[str, int], dict[str, Any]] = {}
    intervals_by_step: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    attempts_by_step: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    instance_steps: dict[str, str] = {}
    node_by_step: dict[str, str] = {}
    run_finished: dict[str, Any] | None = None
    warnings = 0
    forced_finalizes = 0
    for event in events:
        event_type = event.get("type")
        if event_type == "step_started":
            key = (str(event.get("instance_id")), int(event.get("attempt") or 1))
            starts[key] = event
            step = str(event.get("step_id") or event.get("node_id") or "unknown")
            instance_steps[key[0]] = step
            node_by_step[step] = str(event.get("node_id") or "")
        elif event_type == "step_finished":
            key = (str(event.get("instance_id")), int(event.get("attempt") or 1))
            started = starts.get(key)
            started_at = parse_time(started.get("recorded_at")) if started else None
            finished_at = parse_time(event.get("recorded_at"))
            step = str(event.get("step_id") or event.get("node_id") or "unknown")
            node_by_step[step] = str(event.get("node_id") or "")
            attempts_by_step[step] += 1
            outcomes[str(event.get("outcome") or "unknown")] += 1
            event_warnings = event.get("validation_warnings") or []
            warnings += len(event_warnings)
            forced_finalizes += sum(
                isinstance(item, dict)
                and item.get("rule") == "RUNTIME_FORCED_FINALIZE"
                for item in event_warnings
            )
            if started_at and finished_at and finished_at >= started_at:
                intervals_by_step[step].append((started_at, finished_at))
        elif event_type == "run_finished":
            run_finished = event

    nodes = {
        str(item.get("id")): str(item.get("kind"))
        for item in manifest.get("nodes", [])
        if isinstance(item, dict)
    }
    finalizer_invocations = len(
        list(run_dir.glob("attempts/*/attempt-*/finalizer-invocation.json"))
    )
    agent_attempts = finalizer_invocations + sum(
        1
        for event in events
        if event.get("type") == "step_started" and nodes.get(str(event.get("node_id"))) == "agent"
    )
    stages: list[dict[str, Any]] = []
    all_intervals: list[tuple[datetime, datetime]] = []
    tools, tool_rows = tool_metrics(run_dir, instance_steps)
    for step, intervals in sorted(intervals_by_step.items(), key=lambda item: min(value[0] for value in item[1])):
        active = union_seconds(intervals)
        cumulative = sum((end - start).total_seconds() for start, end in intervals)
        if nodes.get(node_by_step.get(step, "")) == "agent":
            all_intervals.extend(intervals)
        stages.append({
            "run_id": run_dir.name,
            "query_id": query_id,
            "mode": mode,
            "step": step,
            "attempts": attempts_by_step[step],
            "active_seconds": round(active, 3),
            "cumulative_agent_seconds": round(cumulative, 3),
            "parallelism": round(cumulative / active, 3) if active else None,
            "tool_calls": int(tools["by_step"].get(step, 0)),
        })
    created = parse_time(manifest.get("created_at"))
    finished = parse_time(run_finished.get("recorded_at")) if run_finished else None
    urls = evidence_urls(run_dir)
    search = build_search_metrics(run_dir, evidence_urls=urls)
    used_sources = [
        item for item in search.get("sources", [])
        if int(item.get("calls") or 0) or int(item.get("cache_reused") or 0)
    ]
    summary = {
        "run_id": run_dir.name,
        "query_id": query_id,
        "category": category,
        "mode": mode,
        "repetition": repetition,
        "status": str(run_finished.get("status")) if run_finished else "incomplete",
        "wall_seconds": round((finished - created).total_seconds(), 3) if created and finished else None,
        "agent_active_seconds": round(union_seconds(all_intervals), 3),
        "agent_cumulative_seconds": round(sum(
            item["cumulative_agent_seconds"]
            for item in stages
            if nodes.get(node_by_step.get(item["step"], "")) == "agent"
        ), 3),
        "agent_attempts": agent_attempts,
        "step_attempts": sum(attempts_by_step.values()),
        "succeeded_attempts": outcomes["succeeded"],
        "failed_attempts": outcomes["failed"],
        "retryable_attempts": outcomes["retryable"],
        "validation_warnings": warnings,
        "research_forced_finalizes": forced_finalizes,
        "research_finalizer_invocations": finalizer_invocations,
        "tool_calls": tools["calls"],
        "tool_failures": tools["failed"],
        "search_provider_count": len(used_sources),
        "search_api_calls": int(search.get("api_calls") or 0),
        "search_raw": int(search.get("funnel", {}).get("raw") or 0),
        "search_unique": int(search.get("funnel", {}).get("unique") or 0),
        "search_fetched": int(search.get("funnel", {}).get("fetched") or 0),
        "evidence_source_urls": len(urls),
        "final_verdict": final_verdict(run_dir),
        **usage_metrics(run_dir),
    }
    for row in tool_rows:
        row.update({"run_id": run_dir.name, "query_id": query_id, "mode": mode})
    return summary, stages, tool_rows


def expected_runs(
    config: dict[str, Any], query_ids: set[str] | None = None
) -> list[tuple[str, str, str, int]]:
    query_path = resolve(str(config.get("queries_file")))
    query_config = load_yaml(query_path)
    suite = re.sub(r"[^a-z0-9-]+", "-", str(config.get("suite") or "benchmark").casefold()).strip("-")
    values = []
    for query in query_config.get("queries", []):
        if query_ids is not None and str(query["id"]) not in query_ids:
            continue
        for repetition in range(1, int(config.get("repetitions", 1)) + 1):
            for mode in config.get("modes", []):
                values.append((
                    f"bench-{suite[:48]}-{query['id']}-{mode}-r{repetition:02d}",
                    str(query["id"]),
                    str(query.get("category") or "uncategorized"),
                    int(repetition),
                ))
    return values


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fmt_minutes(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) / 60:.2f}"


def aggregate_modes(runs: list[dict[str, Any]], modes: list[str]) -> list[dict[str, Any]]:
    values = []
    for mode in modes:
        selected = [item for item in runs if item["mode"] == mode]
        walls = [float(item["wall_seconds"]) for item in selected if item["wall_seconds"] is not None]
        values.append({
            "mode": mode,
            "runs": len(selected),
            "completed": sum(item["status"] == "completed" for item in selected),
            "median_wall_seconds": statistics.median(walls) if walls else None,
            "p90_wall_seconds": percentile(walls, 0.9),
            "mean_agents": statistics.fmean(item["agent_attempts"] for item in selected) if selected else None,
            "mean_tools": statistics.fmean(item["tool_calls"] for item in selected) if selected else None,
            "mean_search_providers": statistics.fmean(item["search_provider_count"] for item in selected) if selected else None,
            "mean_evidence_urls": statistics.fmean(item["evidence_source_urls"] for item in selected) if selected else None,
            "mean_forced_finalizes": statistics.fmean(item["research_forced_finalizes"] for item in selected) if selected else None,
        })
    return values


def render_report(
    config: dict[str, Any],
    runs: list[dict[str, Any]],
    stages: list[dict[str, Any]],
    missing: list[str],
    expected_run_count: int,
) -> str:
    modes = [str(item) for item in config.get("modes", [])]
    aggregate = aggregate_modes(runs, modes)
    lines = [
        f"# DeepResearch-CLI Benchmark：{config.get('suite')}",
        "",
        "## 测试摘要",
        "",
        f"- 测试矩阵：{expected_run_count} 个预期 Run。",
        f"- 已发现：{len(runs)} 个 Run；缺失：{len(missing)} 个。",
        "- `completed` 只代表工作流完成；内容质量以 FinalReview verdict 单独判断。",
        "",
        "| 模式 | 完成/发现 | 中位耗时（分） | P90（分） | 平均 Agent | 平均强制收口 | 平均工具调用 | 平均搜索来源 | 平均证据 URL |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in aggregate:
        lines.append(
            f"| {item['mode']} | {item['completed']}/{item['runs']} | "
            f"{fmt_minutes(item['median_wall_seconds'])} | {fmt_minutes(item['p90_wall_seconds'])} | "
            f"{item['mean_agents']:.1f} | {item['mean_forced_finalizes']:.1f} | {item['mean_tools']:.1f} | "
            f"{item['mean_search_providers']:.1f} | {item['mean_evidence_urls']:.1f} |"
            if item["runs"] else f"| {item['mode']} | 0/0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A |"
        )
    lines.extend([
        "",
        "## 单 Run 结果",
        "",
        "| Query | 模式 | 状态 | 耗时（分） | Agent | 重试/强制收口 | 工具/失败 | 搜索来源/API | Raw/Unique/Fetched | 证据 URL | FinalReview |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for item in sorted(runs, key=lambda value: (value["query_id"], modes.index(value["mode"]), value["repetition"])):
        lines.append(
            f"| {item['query_id']} | {item['mode']} | {item['status']} | {fmt_minutes(item['wall_seconds'])} | "
            f"{item['agent_attempts']} | {item['retryable_attempts']}/{item['research_forced_finalizes']} | "
            f"{item['tool_calls']}/{item['tool_failures']} | "
            f"{item['search_provider_count']}/{item['search_api_calls']} | "
            f"{item['search_raw']}/{item['search_unique']}/{item['search_fetched']} | "
            f"{item['evidence_source_urls']} | {item['final_verdict'] or 'N/A'} |"
        )
    lines.extend([
        "",
        "## 各阶段统计",
        "",
        "阶段在某模式不存在时为 N/A，不按 0 秒处理。Active 是并发区间并集，累计时间是实例耗时之和。",
        "",
        "| 模式 | 阶段 | Run 数 | 中位 Active（分） | 中位累计 Agent（分） | 平均实例数 | 平均工具调用 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in stages:
        grouped[(item["mode"], item["step"])].append(item)
    for mode in modes:
        for (selected_mode, step), values in grouped.items():
            if selected_mode != mode:
                continue
            lines.append(
                f"| {mode} | {step} | {len(values)} | "
                f"{statistics.median(item['active_seconds'] for item in values) / 60:.2f} | "
                f"{statistics.median(item['cumulative_agent_seconds'] for item in values) / 60:.2f} | "
                f"{statistics.fmean(item['attempts'] for item in values):.1f} | "
                f"{statistics.fmean(item['tool_calls'] for item in values):.1f} |"
            )
    if missing:
        lines.extend(["", "## 缺失 Run", ""] + [f"- `{item}`" for item in missing])
    lines.extend([
        "",
        "## 统计口径",
        "",
        "- 总耗时：`run.json.created_at` 到 `run_finished.recorded_at`。",
        "- Agent 数：Node Spec 中 `kind=agent` 的实际 attempt 数；Script Node 不计入。",
        "- ResearchFinalizer：独立 Finalizer invocation 计入 Agent 数；强制收口次数来自 `RUNTIME_FORCED_FINALIZE` warning。",
        "- 工具调用：ACP 投影中唯一 `toolCallId` 的 `tool_call` 数；失败取最终 `status=failed`。",
        "- 搜索来源：本 Run 的 Search SQLite 中实际调用或缓存复用的 distinct provider。",
        "- Evidence URL：正式 Artifact JSON 中出现的唯一 HTTP(S) URL。",
        "- Token：仅汇总存在于 `harness.json.usage` 的值；缺失不推测。",
        "",
        "原始明细见同目录下 `summary.json`、`runs.csv`、`stages.csv` 和 `tools.csv`。",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("benchmarks/config.yaml"))
    parser.add_argument("--runs-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--query",
        action="append",
        help="only summarize this query id; repeat to select multiple queries",
    )
    parser.add_argument("--strict", action="store_true", help="fail when any expected run is missing")
    args = parser.parse_args(argv)
    config = load_yaml(resolve(str(args.config)))
    paths = config.get("paths") or {}
    runs_dir = resolve(str(args.runs_dir or paths.get("runs_dir", "runs")))
    output_dir = resolve(str(args.output_dir or paths.get("results_dir", "benchmarks/results")))
    output_dir.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    missing: list[str] = []
    selected_queries = set(args.query) if args.query else None
    expected = expected_runs(config, selected_queries)
    unknown_queries = selected_queries - {item[1] for item in expected} if selected_queries else set()
    if unknown_queries:
        parser.error(f"unknown query ids: {', '.join(sorted(unknown_queries))}")
    for selected_run_id, query_id, category, repetition in expected:
        run_dir = runs_dir / selected_run_id
        if not (run_dir / "run.json").is_file():
            missing.append(selected_run_id)
            continue
        mode = selected_run_id.rsplit("-r", 1)[0].rsplit("-", 1)[-1]
        summary, stage_rows, tool_rows = summarize_run(
            run_dir, query_id, category, mode, repetition
        )
        runs.append(summary)
        stages.extend(stage_rows)
        tools.extend(tool_rows)
    payload = {
        "schema_version": 1,
        "suite": config.get("suite"),
        "expected_run_count": len(expected),
        "observed_run_count": len(runs),
        "missing_runs": missing,
        "mode_summary": aggregate_modes(runs, [str(item) for item in config.get("modes", [])]),
        "runs": runs,
        "stages": stages,
        "tools": tools,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(output_dir / "runs.csv", runs)
    write_csv(output_dir / "stages.csv", stages)
    write_csv(output_dir / "tools.csv", tools)
    (output_dir / "report.md").write_text(
        render_report(config, runs, stages, missing, len(expected)), encoding="utf-8"
    )
    print(f"wrote benchmark report to {output_dir / 'report.md'}")
    if args.strict and missing:
        print(f"missing {len(missing)} expected runs")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
