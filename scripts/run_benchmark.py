#!/usr/bin/env python3
"""Run a reproducible query x mode DeepResearch benchmark locally."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
SAFE_ID = re.compile(r"[^a-z0-9-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def absolute(root: Path, value: str) -> Path:
    """Make a path absolute without dereferencing venv executable symlinks."""

    path = Path(value).expanduser()
    return path.absolute() if path.is_absolute() else (root / path).absolute()


def validate_config(config: dict[str, Any], config_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if config.get("version") != 1:
        raise ValueError("benchmark config version must be 1")
    modes = config.get("modes")
    if not isinstance(modes, list) or not modes or any(
        item not in {"quick", "normal", "heavy"} for item in modes
    ):
        raise ValueError("modes must be a non-empty subset of quick, normal, heavy")
    queries_path = resolve(ROOT, str(config.get("queries_file") or ""))
    queries_value = load_yaml(queries_path)
    if queries_value.get("version") != 1 or not isinstance(queries_value.get("queries"), list):
        raise ValueError("queries file must contain version: 1 and a queries list")
    queries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(queries_value["queries"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"query #{index} must be a mapping")
        query_id = str(item.get("id") or "").strip().lower()
        text = str(item.get("query") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", query_id):
            raise ValueError(f"invalid query id: {query_id!r}")
        if query_id in seen:
            raise ValueError(f"duplicate query id: {query_id}")
        if not text:
            raise ValueError(f"query {query_id} is empty")
        seen.add(query_id)
        queries.append({
            "id": query_id,
            "category": str(item.get("category") or "uncategorized"),
            "query": text,
        })
    repetitions = config.get("repetitions", 1)
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    return queries, [str(item) for item in modes]


def run_id(suite: str, query_id: str, mode: str, repetition: int) -> str:
    normalized = SAFE_ID.sub("-", suite.casefold()).strip("-") or "benchmark"
    return f"bench-{normalized[:48]}-{query_id}-{mode}-r{repetition:02d}"


def terminal_status(run_dir: Path) -> str | None:
    journal = run_dir / "journal.jsonl"
    if not journal.is_file():
        return None
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    finished = [item for item in events if item.get("type") == "run_finished"]
    return str(finished[-1].get("status")) if finished else None


def selected(values: Iterable[str], requested: list[str] | None) -> list[str]:
    source = list(values)
    if not requested:
        return source
    wanted = set(requested)
    unknown = wanted.difference(source)
    if unknown:
        raise ValueError("unknown selection: " + ", ".join(sorted(unknown)))
    return [item for item in source if item in wanted]


def build_command(
    config: dict[str, Any], query: dict[str, str], mode: str, repetition: int
) -> tuple[str, list[str]]:
    local = config.get("local") or {}
    execution = config.get("execution") or {}
    paths = config.get("paths") or {}
    suite = str(config.get("suite") or "benchmark")
    selected_run_id = run_id(suite, query["id"], mode, repetition)
    command = [str(item) for item in local.get("command", [".venv/bin/deepresearch"])]
    if not command:
        raise ValueError("local.command must contain an executable")
    executable = Path(command[0]).expanduser()
    if not executable.is_absolute() and len(executable.parts) > 1:
        command[0] = str((ROOT / executable).absolute())
    command.extend([
        query["query"],
        "--run-id", selected_run_id,
        "--mode", mode,
        "--report-format", str(execution.get("report_format", "formal_report")),
        "--output-format", str(execution.get("output_format", "markdown")),
        "--language", str(execution.get("language", "zh-CN")),
        "--harness", str(execution.get("harness", "hermes")),
        "--max-concurrency", str(execution.get("max_concurrency", 4)),
        "--progress", str(execution.get("progress", "off")),
        "--search-provider-limit", str(execution.get("search_provider_limit", 20)),
        "--runs-dir", str(resolve(ROOT, str(paths.get("runs_dir", "runs")))),
        "--output-dir", str(resolve(ROOT, str(paths.get("output_dir", "output")))),
    ])
    harness_command = execution.get("harness_command")
    if harness_command:
        command.extend(["--harness-command", str(harness_command)])
    profile = execution.get("harness_profile")
    if profile:
        command.extend(["--harness-profile", str(profile)])
    timeout = execution.get("node_timeout_seconds")
    if timeout is None:
        command.append("--no-node-timeout")
    else:
        command.extend(["--node-timeout-seconds", str(timeout)])
    if execution.get("search_mcp", True):
        command.extend([
            "--search-dir", str(resolve(ROOT, str(execution.get("search_dir", "search")))),
            "--search-provider-python", str(
                absolute(
                    ROOT,
                    str(execution.get("search_provider_python", ".venv/bin/python")),
                )
            ),
        ])
    else:
        command.append("--no-search-mcp")
    return selected_run_id, command


def build_environment(config: dict[str, Any]) -> dict[str, str]:
    environment = dict(os.environ)
    local = config.get("local") or {}
    hermes_home = str(local.get("hermes_home") or "").strip()
    if not hermes_home:
        raise ValueError("local.hermes_home is required for an isolated benchmark")
    environment["HERMES_HOME"] = str(resolve(ROOT, hermes_home))
    return environment


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("benchmarks/config.yaml"))
    parser.add_argument("--query", action="append", dest="queries", help="run only this query id; repeatable")
    parser.add_argument("--mode", action="append", dest="modes", choices=("quick", "normal", "heavy"))
    parser.add_argument("--dry-run", action="store_true", help="print commands without starting a run")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args(argv)

    config_path = resolve(ROOT, str(args.config))
    config = load_yaml(config_path)
    queries, modes = validate_config(config, config_path)
    run_environment = build_environment(config)
    query_ids = selected((item["id"] for item in queries), args.queries)
    modes = selected(modes, args.modes)
    queries = [item for item in queries if item["id"] in query_ids]
    paths = config.get("paths") or {}
    runs_dir = resolve(ROOT, str(paths.get("runs_dir", "runs")))
    results_dir = resolve(ROOT, str(paths.get("results_dir", "benchmarks/results")))
    logs_dir = results_dir / "logs"
    state_path = results_dir / "runner-state.json"
    results_dir.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "suite": config.get("suite"),
        "config": str(config_path),
        "updated_at": utc_now(),
        "runs": [],
    }
    failures = 0
    total = len(queries) * len(modes) * int(config.get("repetitions", 1))
    position = 0
    for query in queries:
        for repetition in range(1, int(config.get("repetitions", 1)) + 1):
            for mode in modes:
                position += 1
                selected_run_id, command = build_command(config, query, mode, repetition)
                existing = terminal_status(runs_dir / selected_run_id)
                record: dict[str, Any] = {
                    "run_id": selected_run_id,
                    "query_id": query["id"],
                    "category": query["category"],
                    "mode": mode,
                    "repetition": repetition,
                    "command": command,
                    "hermes_home": run_environment["HERMES_HOME"],
                }
                if existing == "completed":
                    record.update({"status": "skipped_completed", "finished_at": utc_now()})
                    state["runs"].append(record)
                    print(f"[{position}/{total}] skip completed {selected_run_id}")
                    continue
                if (runs_dir / selected_run_id).exists():
                    record.update({"status": "blocked_existing", "existing_status": existing})
                    state["runs"].append(record)
                    failures += 1
                    print(f"[{position}/{total}] existing non-completed run blocks {selected_run_id}", file=sys.stderr)
                    if not args.continue_on_error:
                        write_json(state_path, state)
                        return 2
                    continue
                print(f"[{position}/{total}] {selected_run_id}")
                print("  " + shlex.join(command))
                if args.dry_run:
                    record["status"] = "dry_run"
                    state["runs"].append(record)
                    continue
                logs_dir.mkdir(parents=True, exist_ok=True)
                log_path = logs_dir / f"{selected_run_id}.log"
                record.update({"status": "running", "started_at": utc_now(), "log": str(log_path)})
                write_json(state_path, {**state, "runs": [*state["runs"], record]})
                with log_path.open("w", encoding="utf-8") as log:
                    process = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=run_environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    assert process.stdout is not None
                    try:
                        for line in process.stdout:
                            log.write(line)
                            log.flush()
                            print(line, end="")
                    except KeyboardInterrupt:
                        process.terminate()
                        process.wait(timeout=30)
                        raise
                    returncode = process.wait()
                record.update({
                    "status": "completed" if returncode == 0 else "failed",
                    "returncode": returncode,
                    "finished_at": utc_now(),
                })
                state["runs"].append(record)
                state["updated_at"] = utc_now()
                write_json(state_path, state)
                if returncode != 0:
                    failures += 1
                    if not args.continue_on_error:
                        return returncode or 1
    state["updated_at"] = utc_now()
    if not args.dry_run:
        write_json(state_path, state)
    print(f"benchmark runner finished: {total - failures}/{total} commands passed or skipped")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
