from pathlib import Path

from starlette.testclient import TestClient

from deepresearch_cli.config import CompiledWorkflow
from deepresearch_cli.persistence import RunStore
from deepresearch_cli.web.app import create_app
from deepresearch_cli.web.snapshot import build_run_snapshot


class _FakeManager:
    def __init__(self):
        self.started = []

    def start(self, value):
        self.started.append(dict(value))
        return "run-direct-start"

    def pending_snapshot(self, _run_id):
        return None


def _persisted_run(root: Path) -> tuple[RunStore, str]:
    store = RunStore(root / "runs")
    workflow = CompiledWorkflow(
        name="heavy", steps=(), result_type="report",
        result_media_type="text/markdown", output_format="markdown",
    )
    run_id = "run-web-test"
    store.create_run({
        "schema_version": "2", "runtime": "config-workflow", "run_id": run_id,
        "created_at": "2026-01-01T00:00:00Z",
        "context": {"query": "测试真实进度", "language": "zh-CN", "mode": "heavy", "output_format": "markdown"},
        "workflow": workflow.to_dict(), "nodes": {}, "definition_hash": "unused",
    }, run_id=run_id)
    return store, run_id


def test_snapshot_is_derived_from_persisted_run(tmp_path):
    store, run_id = _persisted_run(tmp_path)
    value = build_run_snapshot(store, run_id, output_dir=tmp_path / "output")
    assert value["query"] == "测试真实进度"
    assert value["report_format"] == "formal_report"
    assert value["last_event_seq"] == 0
    assert value["metrics"] == {
        "sources": 0, "claims": 0, "counter_claims": 0,
        "quality": {"primary": 0, "secondary": 0, "tertiary": 0},
    }
    assert value["pipeline"] == []
    assert value["progress"]["percent"] == 0
    assert value["progress"]["phase"] == "starting"
    assert value["search"]["status"] == "idle"


def test_web_serves_console_and_snapshot(tmp_path):
    _, run_id = _persisted_run(tmp_path)
    app = create_app(runs_dir=tmp_path / "runs", output_dir=tmp_path / "output")
    with TestClient(app) as client:
        page = client.get(f"/runs/{run_id}")
        assert page.status_code == 200
        assert "DeepResearch 控制台" in page.text
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200
        assert response.json()["query"] == "测试真实进度"


def test_web_root_redirects_to_latest_running_persisted_run(tmp_path):
    _, run_id = _persisted_run(tmp_path)
    app = create_app(runs_dir=tmp_path / "runs", output_dir=tmp_path / "output")
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"/runs/{run_id}"
    assert response.headers["cache-control"] == "no-store"


def test_web_current_run_endpoint_exposes_running_run(tmp_path):
    _, run_id = _persisted_run(tmp_path)
    app = create_app(runs_dir=tmp_path / "runs", output_dir=tmp_path / "output")
    with TestClient(app) as client:
        response = client.get("/api/runs/current")

    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "url": f"/runs/{run_id}"}


def test_web_explicit_new_page_does_not_redirect_to_running_run(tmp_path):
    _persisted_run(tmp_path)
    app = create_app(runs_dir=tmp_path / "runs", output_dir=tmp_path / "output")
    with TestClient(app) as client:
        response = client.get("/?new=1", follow_redirects=False)

    assert response.status_code == 200
    assert "DeepResearch 控制台" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_web_assets_expose_research_workbench_layout(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", output_dir=tmp_path / "output")
    with TestClient(app) as client:
        script = client.get("/assets/app.js")
        styles = client.get("/assets/styles.css")

    assert script.status_code == 200
    assert styles.status_code == 200
    assert script.headers["cache-control"] == "no-cache"
    assert styles.headers["cache-control"] == "no-cache"
    assert "SenseNova Workbench" in script.text
    assert "报告形式" in script.text
    assert 'name="report_format" required' in script.text
    assert "请选择报告形式" in script.text
    assert 'value="brief"' in script.text
    assert 'value="formal_report"' in script.text
    assert "standard_report" not in script.text
    assert "研究概览" in script.text
    assert "最终报告" in script.text
    assert "检索进度与转化" in script.text
    assert "Source 耗时与调用统计" in script.text
    assert "/api/runs/current" in script.text
    assert "setInterval(followCurrentRun, 1000)" in script.text
    assert 'href="/?new=1"' in script.text
    assert "所有统计均来自当前运行的持久化产物" not in script.text
    assert "缓存复用" not in script.text
    assert "<dt>缓存</dt>" not in script.text
    assert "运行当前未连接执行器" not in script.text
    assert "恢复此运行" not in script.text
    assert "/resume" not in script.text
    assert ".dashboard-shell" in styles.text
    assert ".status-sidebar" in styles.text
    assert ".domain-progress" in styles.text
    assert ".resume-callout" not in styles.text


def test_web_rejects_unknown_run(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", output_dir=tmp_path / "output")
    with TestClient(app) as client:
        response = client.get("/api/runs/run-missing")
        assert response.status_code == 404


def test_web_result_download_rejects_run_id_directory_traversal(tmp_path):
    _, run_id = _persisted_run(tmp_path)
    output_dir = tmp_path / "output"
    run_output = output_dir / run_id
    run_output.mkdir(parents=True)
    (run_output / "report.md").write_text("safe", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("must not be served", encoding="utf-8")
    app = create_app(runs_dir=tmp_path / "runs", output_dir=output_dir)

    with TestClient(app) as client:
        valid = client.get(f"/api/runs/{run_id}/files/report.md")
        traversal = client.get("/api/runs/%2E%2E/files/secret.txt")

    assert valid.status_code == 200
    assert valid.text == "safe"
    assert traversal.status_code == 400
    assert "must not be served" not in traversal.text


def test_web_result_download_rejects_symlinks(tmp_path):
    _, run_id = _persisted_run(tmp_path)
    output_dir = tmp_path / "output"
    run_output = output_dir / run_id
    run_output.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be served", encoding="utf-8")
    link = run_output / "report.md"
    try:
        link.symlink_to(secret)
    except OSError:
        return
    app = create_app(runs_dir=tmp_path / "runs", output_dir=output_dir)

    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/files/report.md")

    assert response.status_code == 404
    assert "must not be served" not in response.text


def test_web_does_not_offer_ambiguous_resume_endpoint(tmp_path):
    _, run_id = _persisted_run(tmp_path)
    app = create_app(runs_dir=tmp_path / "runs", output_dir=tmp_path / "output")
    with TestClient(app) as client:
        response = client.post(f"/api/runs/{run_id}/resume", json={})
    assert response.status_code == 404


def test_web_requires_explicit_report_format(tmp_path):
    manager = _FakeManager()
    app = create_app(
        runs_dir=tmp_path / "runs", output_dir=tmp_path / "output", manager=manager
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={"query": "测试", "mode": "normal", "output_format": "pdf"},
        )

    assert response.status_code == 400
    assert "report_format must be selected" in response.json()["error"]
    assert manager.started == []


def test_startup_request_starts_run_and_redirects_root(tmp_path):
    manager = _FakeManager()
    request = {
        "query": "直接开始研究", "mode": "heavy",
        "language": "zh-CN", "output_format": "pdf",
        "report_format": "formal_report",
    }
    app = create_app(
        runs_dir=tmp_path / "runs", output_dir=tmp_path / "output",
        manager=manager, startup_request=request,
    )
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
    assert manager.started == [request]
    assert response.status_code == 307
    assert response.headers["location"] == "/runs/run-direct-start"
