from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent

import psutil
import pytest

from deepresearch_cli.search.providers import ProviderRegistry
from deepresearch_cli.search.service import SearchService
from deepresearch_cli.search.store import SearchStore, SearchStoreError
from tests.search_test_utils import write_search_source


def _make_service(
    tmp_path: Path,
    *,
    provider: str,
    source: str,
    provider_timeout: float = 2.0,
    batch_timeout: float = 3.0,
    provider_limit: int = 20,
    provider_env: dict[str, str] | None = None,
    lease_file: Path | None = None,
) -> tuple[SearchService, ProviderRegistry, SearchStore]:
    search_dir = tmp_path / "search"
    source_options = {}
    if provider == "academic":
        source_options = {"result_shape": "academic", "item_limit_multiplier": 2}
    elif provider == "github_repositories":
        source_options = {"optional_env": ["GITHUB_TOKEN"]}
    write_search_source(
        search_dir,
        provider,
        script_text=dedent(source),
        timeout_seconds=provider_timeout,
        required_modules=[],
        **source_options,
    )
    registry = ProviderRegistry(
        search_dir=search_dir,
        python_executable=sys.executable,
    )
    store = SearchStore(tmp_path / "search-store")
    service = SearchService(
        registry=registry,
        store=store,
        batch_timeout_seconds=batch_timeout,
        provider_limit=provider_limit,
        provider_env=provider_env,
        lease_file=lease_file,
    )
    return service, registry, store


def _search(
    provider: str,
    query: str,
    *,
    target: str = "target A",
    intent: str = "find candidate sources",
) -> dict[str, str]:
    return {
        "provider": provider,
        "query": query,
        "evidence_target": target,
        "intent": intent,
    }


def _summary(result: dict, index: int = 0) -> dict:
    summaries = result["provider_summaries"]
    assert summaries
    return summaries[index]


def _wait_for_file(path: Path, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"provider did not create marker: {path}")


def _process_is_running(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_process_stopped(pid: int, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_is_running(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"provider descendant is still running: {pid}")


def test_same_pair_runs_once_but_records_each_logical_target(tmp_path: Path) -> None:
    call_count = tmp_path / "call-count.txt"
    source = f"""
        import json
        from pathlib import Path

        counter = Path({str(call_count)!r})
        counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
        print(json.dumps({{
            "success": True,
            "items": [{{
                "title": "Shared result",
                "url": "https://example.test/shared",
                "snippet": "one external result",
            }}],
        }}))
    """
    service, _, store = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
    )

    result = service.batch_search(
        [
            _search("hackernews", "same query", target="target A"),
            _search("hackernews", "same query", target="target B"),
        ]
    )
    page = service.search_results(batch_id=result["batch_id"])

    assert call_count.read_text(encoding="utf-8") == "1"
    assert result["executed_provider_count"] == 1
    assert len(result["provider_summaries"]) == 2
    assert page["total"] == 2
    assert {item["evidence_target"] for item in page["items"]} == {
        "target A",
        "target B",
    }
    assert len({item["hit_id"] for item in page["items"]}) == 1
    requests = [
        json.loads(line)
        for line in (store.root / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {item["evidence_target"] for item in requests} == {
        "target A",
        "target B",
    }
    assert len({item["logical_key"] for item in requests}) == 2


@pytest.mark.parametrize("first_outcome", ["failed", "timed_out"])
def test_failed_or_timed_out_pair_can_be_retried(
    tmp_path: Path,
    first_outcome: str,
) -> None:
    call_count = tmp_path / "retry-count.txt"
    first_action = (
        'print(json.dumps({"success": False, "error": "transient failure", "items": []})); '
        "raise SystemExit(2)"
        if first_outcome == "failed"
        else "time.sleep(30)"
    )
    source = f"""
        import json
        import time
        from pathlib import Path

        counter = Path({str(call_count)!r})
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if count == 1:
            {first_action}
        print(json.dumps({{
            "success": True,
            "items": [{{
                "title": "Retry succeeded",
                "url": "https://example.test/retry",
            }}],
        }}))
    """
    service, _, store = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
        provider_timeout=0.2 if first_outcome == "timed_out" else 2.0,
        batch_timeout=2.0,
    )
    request = _search("hackernews", "retry this pair")

    first = service.batch_search([request])
    second = service.batch_search([request])

    assert _summary(first)["status"] == first_outcome
    assert _summary(second)["status"] == "ok"
    assert second["executed_provider_count"] == 1
    assert call_count.read_text(encoding="utf-8") == "2"
    assert store.has_pair("hackernews\0retry this pair") is True


def test_academic_subsource_429_is_partial_and_preserves_warning(
    tmp_path: Path,
) -> None:
    source = """
        import json

        print(json.dumps({
            "success": True,
            "source_results": [
                {
                    "source": "arxiv",
                    "provider": "openalex",
                    "success": True,
                    "items": [{
                        "title": "Healthy paper",
                        "url": "https://openalex.org/W123",
                    }],
                    "attempts": [],
                    "error": None,
                },
                {
                    "source": "semantic",
                    "provider": None,
                    "success": False,
                    "items": [],
                    "attempts": [{
                        "provider": "semantic_scholar_official",
                        "success": False,
                        "error": "HTTP 429",
                    }],
                    "error": "all semantic providers failed",
                },
            ],
        }))
    """
    service, _, _ = _make_service(
        tmp_path,
        provider="academic",
        source=source,
    )

    result = service.batch_search([_search("academic", "graph agents")])
    summary = _summary(result)

    assert summary["status"] == "partial"
    assert summary["hit_count"] == 1
    assert summary["warnings"] == [
        {
            "code": "provider_source_failed",
            "source": "semantic",
            "provider": "",
            "error": "all semantic providers failed",
            "attempt_count": 1,
        }
    ]
    assert result["disabled_providers"] == {}


def test_batch_response_stays_bounded_for_maximal_failure_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = _make_service(
        tmp_path,
        provider="hackernews",
        source='print("unused")',
    )
    warning = {
        "code": "provider_source_failed",
        "source": "s" * 500,
        "provider": "p" * 500,
        "error": "e" * 2_000,
        "attempts": [{"error": "retry"}] * 20,
    }

    def fake_execute(request, *, batch_id, deadline):
        del deadline
        return {
            "batch_id": batch_id,
            **request.to_dict(),
            "pair_key": request.pair_key,
            "status": "failed",
            "elapsed_seconds": 0.0,
            "timed_out": False,
            "returncode": 2,
            "stderr": "",
            "parse_error": None,
            "error": "failure " + "x" * 2_000,
            "warnings": [warning] * 5,
            "payload": {},
            "payload_truncated": False,
            "hits": [],
            "process_started": True,
            "recorded_at": "2026-08-12T00:00:00Z",
        }

    monkeypatch.setattr(service, "_execute", fake_execute)
    searches = [
        _search(
            "hackernews",
            f"query-{index}-" + "q" * 470,
            target="t" * 900,
            intent="i" * 900,
        )
        for index in range(64)
    ]

    result = service.batch_search(searches)

    assert len(result["provider_summaries"]) == 64
    assert result["provider_summaries_compacted_for_transport"] is True
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 192 * 1024


def test_large_provider_payload_is_bounded_in_diagnostics(tmp_path: Path) -> None:
    source = """
        import json
        print(json.dumps({
            "success": True,
            "items": [{
                "title": "Bounded payload",
                "url": "https://example.test/bounded-payload",
            }],
            "diagnostics": {
                f"field-{index}": "x" * 20000 for index in range(40)
            },
        }))
    """
    service, _, store = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
    )

    service.batch_search([_search("hackernews", "large diagnostics")])

    [record] = [
        json.loads(line)
        for line in (store.root / "provider-results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert record["payload_truncated"] is True
    assert record["payload"]["_truncated_for_storage"] is True
    assert len(json.dumps(record["payload"], ensure_ascii=False).encode("utf-8")) <= 512 * 1024


def test_provider_cannot_return_more_than_the_configured_result_limit(
    tmp_path: Path,
) -> None:
    source = """
        import json
        print(json.dumps({
            "success": True,
            "items": [
                {
                    "title": f"Candidate {index}",
                    "url": f"https://example.test/unbounded/{index}",
                }
                for index in range(5000)
            ],
        }))
    """
    service, _, store = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
        provider_limit=1,
    )

    result = service.batch_search([_search("hackernews", "bounded items")])

    assert _summary(result)["hit_count"] == 1
    assert service.search_results(batch_id=result["batch_id"])["total"] == 1
    assert len((store.root / "hits.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_add_hit_storage_failure_is_not_reported_as_a_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """
        import json
        print(json.dumps({
            "success": True,
            "items": [{
                "title": "A result that must be stored",
                "url": "https://example.test/storage",
            }],
        }))
    """
    service, _, store = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
    )

    def disk_failure(_hit):
        raise SearchStoreError("cannot append discoveries.jsonl: disk full")

    monkeypatch.setattr(store, "add_hit", disk_failure)

    with pytest.raises(SearchStoreError, match="disk full"):
        service.batch_search([_search("hackernews", "storage failure")])


def test_provider_environment_contains_only_route_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = tmp_path / "provider-env.json"
    source = f"""
        import json
        import os
        from pathlib import Path

        names = ["GITHUB_TOKEN", "HF_TOKEN", "DEEPRESEARCH_SEARCH_ENV_FILE"]
        Path({str(captured)!r}).write_text(json.dumps({{
            name: os.environ.get(name) for name in names
        }}))
        print(json.dumps({{
            "success": True,
            "items": [{{
                "title": "Repository",
                "url": "https://github.com/example/repository",
            }}],
        }}))
    """
    values = {
        "GITHUB_TOKEN": "github-route-secret",
        "HF_TOKEN": "unrelated-route-secret",
        "DEEPRESEARCH_SEARCH_ENV_FILE": "/private/profile/.env",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    service, _, _ = _make_service(
        tmp_path,
        provider="github_repositories",
        source=source,
        provider_env=values,
    )

    result = service.batch_search(
        [_search("github_repositories", "graph runtime")]
    )

    assert _summary(result)["status"] == "ok"
    assert json.loads(captured.read_text(encoding="utf-8")) == {
        "GITHUB_TOKEN": "github-route-secret",
        "HF_TOKEN": None,
        "DEEPRESEARCH_SEARCH_ENV_FILE": None,
    }


def test_lowercase_proxy_credentials_are_redacted_from_provider_diagnostics(
    tmp_path: Path,
) -> None:
    proxy = "http://proxy-user:proxy-password@proxy.example.test:8080"
    source = """
        import json
        import os
        import sys

        proxy = os.environ["http_proxy"]
        print("provider proxy=" + proxy, file=sys.stderr)
        print(json.dumps({
            "success": False,
            "error": "request through " + proxy + " failed",
            "items": [],
        }))
        raise SystemExit(2)
    """
    service, _, store = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
        provider_env={"http_proxy": proxy},
    )

    result = service.batch_search([_search("hackernews", "proxy failure")])

    assert _summary(result)["status"] == "failed"
    persisted = (store.root / "provider-results.jsonl").read_text(encoding="utf-8")
    assert proxy not in persisted
    assert "[REDACTED]" in persisted


def test_timeout_kills_detached_child_that_inherits_provider_output(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "detached-child.pid"
    source = f"""
        import subprocess
        import sys
        import time
        from pathlib import Path

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        Path({str(child_pid_file)!r}).write_text(str(child.pid))
        time.sleep(60)
    """
    service, _, _ = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
        provider_timeout=0.3,
        batch_timeout=2.0,
    )

    started = time.monotonic()
    result = service.batch_search([_search("hackernews", "detached child")])
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert _summary(result)["status"] == "timed_out"
    assert child_pid_file.is_file()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    _wait_process_stopped(child_pid)


def test_successful_provider_cannot_leave_a_detached_child(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "successful-detached-child.pid"
    source = f"""
        import json
        import subprocess
        import sys
        from pathlib import Path

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        Path({str(child_pid_file)!r}).write_text(str(child.pid))
        print(json.dumps({{
            "success": True,
            "items": [{{
                "title": "Provider completed",
                "url": "https://example.test/provider-completed",
            }}],
        }}))
    """
    service, _, _ = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
    )

    result = service.batch_search([_search("hackernews", "child cleanup")])

    assert _summary(result)["status"] == "ok"
    assert child_pid_file.is_file()
    _wait_process_stopped(int(child_pid_file.read_text(encoding="utf-8")))


def test_deleting_lease_cancels_running_provider(tmp_path: Path) -> None:
    provider_pid_file = tmp_path / "leased-provider.pid"
    lease_file = tmp_path / "research-attempt.lease"
    lease_file.write_text("active", encoding="utf-8")
    source = f"""
        import os
        import time
        from pathlib import Path

        Path({str(provider_pid_file)!r}).write_text(str(os.getpid()))
        time.sleep(60)
    """
    service, _, _ = _make_service(
        tmp_path,
        provider="hackernews",
        source=source,
        provider_timeout=10.0,
        batch_timeout=10.0,
        lease_file=lease_file,
    )
    outcome: dict[str, object] = {}

    def run_search() -> None:
        try:
            outcome["result"] = service.batch_search(
                [_search("hackernews", "lease cancellation")]
            )
        except BaseException as exc:  # make thread failures visible in the test
            outcome["error"] = exc

    worker = threading.Thread(target=run_search, daemon=True)
    worker.start()
    _wait_for_file(provider_pid_file)

    cancelled_at = time.monotonic()
    lease_file.unlink()
    worker.join(timeout=3.0)

    assert not worker.is_alive()
    assert "error" not in outcome
    assert time.monotonic() - cancelled_at < 3.0
    result = outcome["result"]
    assert isinstance(result, dict)
    summary = _summary(result)
    assert summary["status"] == "timed_out"
    assert "lease ended" in summary["error"]
    provider_pid = int(provider_pid_file.read_text(encoding="utf-8"))
    _wait_process_stopped(provider_pid)
