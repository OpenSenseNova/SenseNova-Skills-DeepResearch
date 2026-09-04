"""Own background workflow tasks started by the local web console."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from deepresearch_cli.config import RunRequest
from deepresearch_cli.driver import ExecutionSessionConfig, WorkflowDriver
from deepresearch_cli.service import WorkflowService


class WebRunManager:
    def __init__(self, runs_dir: Path, output_dir: Path, node_dirs: Sequence[Path] = ()) -> None:
        self.runs_dir = runs_dir.expanduser().resolve()
        self.output_dir = output_dir.expanduser().resolve()
        self.node_dirs = tuple(node_dirs)
        self.tasks: dict[str, asyncio.Task] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    def _config(self, value: Mapping[str, Any]) -> ExecutionSessionConfig:
        timeout = value.get("node_timeout_seconds", 600)
        return ExecutionSessionConfig(
            harness=str(value.get("harness", "hermes")),
            harness_profile=value.get("harness_profile") or None,
            harness_model=value.get("harness_model") or None,
            node_timeout_seconds=None if timeout is None else float(timeout),
            max_concurrency=int(value.get("max_concurrency", 4)),
            search_mcp_enabled=bool(value.get("search_mcp", True)),
            search_dir=Path(value["search_dir"]).expanduser() if value.get("search_dir") else None,
            search_provider_python=value.get("search_provider_python") or None,
            search_provider_limit=int(value.get("search_provider_limit", 20)),
            camofox_fallback_enabled=(
                bool(value.get("camofox_fallback", True))
                and bool(value.get("search_mcp", True))
            ),
            camofox_home=Path(value["camofox_home"]).expanduser() if value.get("camofox_home") else None,
            camofox_base_url=value.get("camofox_base_url") or None,
        )

    def _service(self, value: Mapping[str, Any]) -> WorkflowService:
        return WorkflowService(
            self.runs_dir,
            self._config(value),
            output_dir=self.output_dir,
            node_dirs=self.node_dirs,
            harness_command=value.get("harness_command") or None,
        )

    def start(self, value: Mapping[str, Any]) -> str:
        report_format = value.get("report_format")
        if report_format not in {"brief", "formal_report"}:
            raise ValueError(
                "report_format must be selected: brief or formal_report"
            )
        request = RunRequest(
            query=value.get("query"),
            language=value.get("language", "zh-CN"),
            mode=value.get("mode", "heavy"),
            output_format=value.get("output_format", "markdown"),
            report_format=report_format,
        )
        if request.mode not in {"quick", "normal", "heavy"}:
            raise ValueError("mode must be quick, normal, or heavy")
        run_id = WorkflowDriver.new_run_id()
        self.pending[run_id] = {
            "run_id": run_id, "status": "starting", "query": request.query,
            "mode": request.mode, "output_format": request.output_format,
            "report_format": request.report_format,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "last_event_seq": 0, "progress": {"percent": 0, "phase": "starting", "phase_label": "正在连接运行环境"},
        }
        task = asyncio.create_task(self._run(run_id, request, dict(value)))
        self.tasks[run_id] = task
        task.add_done_callback(lambda finished, selected=run_id: self._settled(selected, finished))
        return run_id

    async def _run(self, run_id: str, request: RunRequest, value: Mapping[str, Any]) -> None:
        try:
            await self._service(value).run(request, run_id=run_id)
        except Exception as exc:
            if not (self.runs_dir / run_id / "run.json").exists():
                self.pending[run_id].update(status="failed", error=f"{type(exc).__name__}: {exc}")

    def _settled(self, run_id: str, task: asyncio.Task) -> None:
        self.tasks.pop(run_id, None)
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            pass

    def pending_snapshot(self, run_id: str) -> dict[str, Any] | None:
        value = self.pending.get(run_id)
        return dict(value) if value else None
