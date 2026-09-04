import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from deepresearch_cli.search.providers import ProviderRegistry
from deepresearch_cli.search.service import SearchService
from deepresearch_cli.search.store import SearchStore
from tests.search_test_utils import write_search_source


def _write_fake_provider(
    search_dir: Path, provider: str, *, call_log: Path | None = None
) -> Path:
    return write_search_source(
        search_dir,
        provider,
        script_text=f"""\
import argparse
import json
import os

parser = argparse.ArgumentParser()
parser.add_argument("query")
parser.add_argument("--limit", type=int, default=5)
args, _ = parser.parse_known_args()
provider = {provider!r}

call_log = {str(call_log) if call_log is not None else None!r}
if call_log:
    with open(call_log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({{"provider": provider, "query": args.query}}) + "\\n")

if args.query == "shared result":
    items = [
        {{
            "title": provider + " copy",
            "url": "https://example.test/shared",
            "snippet": provider + " found the same canonical page",
        }}
    ]
elif args.query == "paged result":
    items = [
        {{
            "title": f"Result {{index}}",
            "url": f"https://example.test/page/{{index}}",
            "snippet": f"full detail for result {{index}} " + ("x" * 800),
        }}
        for index in range(5)
    ]
else:
    items = [
        {{
            "title": provider + " result for " + args.query,
            "url": "https://example.test/" + provider + "/" + args.query.replace(" ", "-"),
            "snippet": "result from " + provider,
        }}
    ]

print(json.dumps({{
    "success": True,
    "query": args.query,
    "provider": provider,
    "items": items[:args.limit],
    "error": None,
}}))
""",
    )


def _make_service(tmp_path: Path, monkeypatch):
    search_dir = tmp_path / "search"
    call_log = tmp_path / "calls.jsonl"
    _write_fake_provider(search_dir, "hackernews", call_log=call_log)
    _write_fake_provider(search_dir, "reddit", call_log=call_log)
    registry = ProviderRegistry(
        search_dir=search_dir,
        python_executable=sys.executable,
    )
    store = SearchStore(tmp_path / "search-store")
    service = SearchService(registry=registry, store=store)
    return service, call_log


def _search(provider: str, query: str) -> dict[str, str]:
    return {
        "provider": provider,
        "query": query,
        "evidence_target": "test evidence target",
        "intent": "verify the multi-source search contract",
    }


def _read_calls(call_log: Path) -> list[dict[str, str]]:
    if not call_log.exists():
        return []
    return [
        json.loads(line)
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _hits(value):
    if isinstance(value, list):
        return value
    for key in ("hits", "results", "items"):
        if isinstance(value.get(key), list):
            return value[key]
    raise AssertionError(f"search response has no hit list: {value!r}")


def _batch_id(value):
    batch_id = value.get("batch_id") or value.get("batchId")
    assert isinstance(batch_id, str) and batch_id
    return batch_id


def _next_cursor(value):
    if "next_cursor" in value:
        return value["next_cursor"]
    return value.get("nextCursor")


def _hit_id(value):
    hit_id = value.get("hit_id") or value.get("id")
    assert isinstance(hit_id, str) and hit_id
    return hit_id


def _detail_hit(value):
    if isinstance(value.get("hit"), dict):
        return value["hit"]
    return value


def test_batch_search_executes_only_selected_providers(tmp_path, monkeypatch):
    service, call_log = _make_service(tmp_path, monkeypatch)

    result = service.batch_search([_search("hackernews", "selected query")])

    assert _batch_id(result)
    assert _read_calls(call_log) == [
        {"provider": "hackernews", "query": "selected query"}
    ]
    hits = _hits(service.search_results())
    assert len(hits) == 1
    assert hits[0]["provider"] == "hackernews"


def test_batch_search_deduplicates_the_same_url_across_providers(
    tmp_path, monkeypatch
):
    service, call_log = _make_service(tmp_path, monkeypatch)

    service.batch_search(
        [
            _search("hackernews", "shared result"),
            _search("reddit", "shared result"),
        ]
    )

    assert {call["provider"] for call in _read_calls(call_log)} == {
        "hackernews",
        "reddit",
    }
    hits = _hits(service.search_results())
    assert len(hits) == 2
    assert {hit["url"] for hit in hits} == {"https://example.test/shared"}
    assert len({hit["hit_id"] for hit in hits}) == 1
    assert {hit["source_provider"] for hit in hits} == {"hackernews", "reddit"}


def test_repeated_provider_query_is_not_executed_again_across_batches(
    tmp_path, monkeypatch
):
    service, call_log = _make_service(tmp_path, monkeypatch)
    search = _search("hackernews", "one execution only")

    first = service.batch_search([search])
    second = service.batch_search([search])

    assert _batch_id(first) != _batch_id(second)
    assert _read_calls(call_log) == [
        {"provider": "hackernews", "query": "one execution only"}
    ]
    second_page = service.search_results(batch_id=_batch_id(second))
    assert len(_hits(second_page)) == 1
    all_hits = _hits(service.search_results())
    assert len(all_hits) == 2
    assert len({hit["hit_id"] for hit in all_hits}) == 1


def test_search_results_are_paginated_and_each_hit_remains_readable(
    tmp_path, monkeypatch
):
    service, _ = _make_service(tmp_path, monkeypatch)
    batch = service.batch_search([_search("hackernews", "paged result")])
    batch_id = _batch_id(batch)

    first_page = service.search_results(batch_id=batch_id, cursor=0, limit=2)
    first_hits = _hits(first_page)
    assert len(first_hits) == 2
    assert _next_cursor(first_page) is not None

    second_page = service.search_results(
        batch_id=batch_id,
        cursor=_next_cursor(first_page),
        limit=2,
    )
    second_hits = _hits(second_page)
    assert len(second_hits) == 2
    assert {_hit_id(hit) for hit in first_hits}.isdisjoint(
        {_hit_id(hit) for hit in second_hits}
    )

    detail = _detail_hit(service.get_search_hit(_hit_id(first_hits[0])))
    assert detail["url"].startswith("https://example.test/page/")
    assert detail["snippet"].startswith("full detail for result")
    assert len(detail["snippet"]) > 500


def _tool_payload(result):
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    texts = [
        content.text
        for content in result.content
        if getattr(content, "type", None) == "text"
    ]
    assert texts, f"MCP tool returned no text or structured content: {result!r}"
    return json.loads("".join(texts))


def test_stdio_mcp_initializes_lists_tools_and_calls_batch_search(
    tmp_path, monkeypatch
):
    search_dir = tmp_path / "search"
    call_log = tmp_path / "stdio-calls.jsonl"
    _write_fake_provider(search_dir, "hackernews", call_log=call_log)
    _write_fake_provider(search_dir, "reddit", call_log=call_log)

    async def exercise():
        environment = dict(os.environ)
        environment.update(
            {
                "DEEPRESEARCH_SEARCH_STORE_DIR": str(tmp_path / "stdio-store"),
                "DEEPRESEARCH_SEARCH_DIR": str(search_dir),
                "DEEPRESEARCH_SEARCH_PROVIDER_PYTHON": sys.executable,
            }
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "deepresearch_cli.search.mcp_server"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name

                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                assert {
                    "list_search_domains",
                    "start_domain_search",
                    "get_search_batch",
                    "list_search_sources",
                    "batch_search",
                    "search_results",
                    "get_search_hit",
                    "fetch_url",
                }.issubset(tool_names)

                sources_result = await session.call_tool(
                    "list_search_sources", arguments={}
                )
                assert not sources_result.isError
                sources = _tool_payload(sources_result)
                assert "hackernews" in json.dumps(sources)

                batch_result = await session.call_tool(
                    "batch_search",
                    arguments={
                        "searches": [
                            _search("hackernews", "paged result")
                        ]
                    },
                )
                assert not batch_result.isError
                batch = _tool_payload(batch_result)
                batch_id = _batch_id(batch)

                page_result = await session.call_tool(
                    "search_results",
                    arguments={"batch_id": batch_id, "cursor": 0, "limit": 2},
                )
                assert not page_result.isError
                page = _tool_payload(page_result)
                assert len(_hits(page)) == 2
                assert _next_cursor(page) == 2

                discovery_id = _hits(page)[0]["discovery_id"]
                detail_result = await session.call_tool(
                    "get_search_hit", arguments={"hit_id": discovery_id}
                )
                assert not detail_result.isError
                detail = _tool_payload(detail_result)
                assert detail["selected_discovery_id"] == discovery_id
                assert detail["source_provider"] == "hackernews"

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    assert _read_calls(call_log) == [
        {"provider": "hackernews", "query": "paged result"}
    ]


def test_stdio_mcp_exits_when_research_lease_is_removed(tmp_path):
    search_dir = tmp_path / "search"
    _write_fake_provider(search_dir, "hackernews")
    lease_file = tmp_path / "research.lease"
    lease_file.write_text("active", encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "DEEPRESEARCH_SEARCH_STORE_DIR": str(tmp_path / "lease-store"),
            "DEEPRESEARCH_SEARCH_DIR": str(search_dir),
            "DEEPRESEARCH_SEARCH_PROVIDER_PYTHON": sys.executable,
            "DEEPRESEARCH_SEARCH_LEASE_FILE": str(lease_file),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "deepresearch_cli.search.mcp_server"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.3)
        assert process.poll() is None
        lease_file.unlink()
        assert process.wait(timeout=3.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2.0)
