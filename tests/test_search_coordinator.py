from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from deepresearch_cli.harness.protocol import AgentInvocation
from deepresearch_cli.harness.search_coordinator import SearchCoordinatorManager
from deepresearch_cli.harness.search_mcp import SearchMcpSupport
from deepresearch_cli.search.coordinator_client import SearchCoordinatorClient
from deepresearch_cli.search.metrics import build_search_metrics
from tests.search_test_utils import write_search_source


def _invocation(runs_dir: Path, identity: str) -> AgentInvocation:
    workspace = runs_dir / "run-test" / "attempts" / identity / "staging"
    workspace.mkdir(parents=True, exist_ok=True)
    return AgentInvocation(
        invocation_id=identity,
        run_id="run-test",
        node_instance_id=identity,
        node_type="research",
        attempt=1,
        workspace=workspace,
        input_artifact_refs=[],
        resolved_input_artifacts=[],
        timeout_seconds=30,
        agent_context={},
        prompt="test",
    )


def _search(query: str) -> list[dict[str, str]]:
    return [{
        "provider": "fake",
        "query": query,
        "evidence_target": "target",
        "intent": "test run-wide reuse",
    }]


def test_coordinator_coalesces_clients_isolates_namespaces_and_reopens(tmp_path: Path) -> None:
    async def exercise() -> None:
        runs_dir = tmp_path / "runs"
        search_dir = tmp_path / "registry"
        call_log = tmp_path / "calls.jsonl"
        write_search_source(
            search_dir,
            "fake",
            script_text=f"""\
import argparse, json, time
p = argparse.ArgumentParser()
p.add_argument('query')
p.add_argument('--limit')
a = p.parse_args()
time.sleep(0.2)
with open({str(call_log)!r}, 'a', encoding='utf-8') as f:
    f.write(a.query + '\\n')
print(json.dumps({{'items': [{{'title': a.query, 'url': 'https://example.test/' + a.query}}]}}))
""",
            required_modules=[],
        )
        manager = SearchCoordinatorManager(
            runs_dir=runs_dir,
            run_id="run-test",
            search_dir=search_dir,
            provider_python=sys.executable,
        )
        await manager.ensure_started(_invocation(runs_dir, "d1-a1"))
        assert manager.url and manager.token
        first_url, first_token = manager.credentials("d1-a1")
        second_url, second_token = manager.credentials("d2-a1")
        first = SearchCoordinatorClient(url=first_url, token=first_token, namespace="d1-a1")
        second = SearchCoordinatorClient(url=second_url, token=second_token, namespace="d2-a1")
        one, two = await asyncio.gather(
            asyncio.to_thread(first.batch_search, _search("shared")),
            asyncio.to_thread(second.batch_search, _search("shared")),
        )
        assert sorted([one["executed_provider_count"], two["executed_provider_count"]]) == [0, 1]
        assert call_log.read_text(encoding="utf-8").splitlines() == ["shared"]
        assert first.search_results()["total"] == 1
        assert second.search_results()["total"] == 1
        escaped = SearchCoordinatorClient(
            url=first_url, token=first_token, namespace="d2-a1"
        )
        try:
            escaped.search_results()
        except RuntimeError as exc:
            assert "HTTP 401" in str(exc)
        else:
            raise AssertionError("attempt credential accessed another namespace")
        metrics = build_search_metrics(runs_dir / "run-test")
        assert metrics["api_calls"] == 1
        assert metrics["cache_reused"] == 1
        assert metrics["funnel"]["raw"] == 1
        assert metrics["funnel"]["unique"] == 1
        await manager.close()

        resumed = SearchCoordinatorManager(
            runs_dir=runs_dir,
            run_id="run-test",
            search_dir=search_dir,
            provider_python=sys.executable,
        )
        await resumed.ensure_started(_invocation(runs_dir, "d3-a1"))
        assert resumed.url and resumed.token
        third_url, third_token = resumed.credentials("d3-a1")
        third = SearchCoordinatorClient(url=third_url, token=third_token, namespace="d3-a1")
        reused = await asyncio.to_thread(third.batch_search, _search("shared"))
        assert reused["executed_provider_count"] == 0
        assert third.search_results()["total"] == 1
        assert call_log.read_text(encoding="utf-8").splitlines() == ["shared"]
        await resumed.close()
        assert (runs_dir / "run-test" / "search" / "search.sqlite3").is_file()

    asyncio.run(exercise())


def test_proxy_launch_receives_only_coordinator_capability(tmp_path: Path) -> None:
    class Endpoint:
        url = "http://127.0.0.1:12345"

        @classmethod
        def credentials(cls, namespace: str) -> tuple[str, str]:
            return cls.url, "scoped-" + namespace

    spec = SearchMcpSupport(coordinator=Endpoint()).build(
        identity="run/d1/attempt-1",
        store_dir=tmp_path / "attempt" / "search",
    )
    assert spec.env["DEEPRESEARCH_SEARCH_NAMESPACE"] == "run/d1/attempt-1"
    assert spec.env["DEEPRESEARCH_SEARCH_COORDINATOR_URL"] == Endpoint.url
    assert spec.env["DEEPRESEARCH_SEARCH_COORDINATOR_TOKEN"] == "scoped-run/d1/attempt-1"
    assert "DEEPRESEARCH_SEARCH_DIR" not in spec.env
    assert "DEEPRESEARCH_SEARCH_PROVIDER_PYTHON" not in spec.env
    assert "DEEPRESEARCH_SEARCH_ENV_FILE" not in spec.env
