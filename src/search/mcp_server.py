"""stdio MCP server exposing directed multi-source search to Research agents."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from .registry import DomainRegistry, ProviderRegistry, load_search_environment
from .coordinator_client import SearchCoordinatorClient
from .fetch import WebFetchService
from .service import SearchService
from .store import SearchStore


class SearchInput(BaseModel):
    provider: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]{0,63}$",
            description=(
                "One source name returned by list_search_sources for this evidence target."
            ),
        ),
    ]
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description="A query rewritten specifically for the selected provider.",
        ),
    ]
    evidence_target: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1_000,
            description="The fact, object, field, or gap this search should discover.",
        ),
    ]
    intent: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1_000,
            description="Why this provider/query pair is appropriate and how its hits will be used.",
        ),
    ]


class DomainSearchInput(BaseModel):
    domain: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]{0,63}$",
            description="One domain returned by list_search_domains.",
        ),
    ]
    operation: Annotated[
        Optional[str],
        Field(
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]{0,63}$",
            description="A domain operation; omit to use its declared default.",
        ),
    ] = None
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description="A concise query suitable for every source selected by the operation.",
        ),
    ]
    evidence_target: Annotated[
        str,
        Field(min_length=1, max_length=1_000),
    ]
    intent: Annotated[
        str,
        Field(min_length=1, max_length=1_000),
    ]
    source_policy: Annotated[
        str,
        Field(pattern=r"^all_relevant$"),
    ] = "all_relevant"
    source_queries: Optional[dict[str, Annotated[str, Field(min_length=1, max_length=500)]]] = Field(
        default=None,
        description=(
            "Optional source-specific query overrides. Keys must belong to the selected "
            "operation; unspecified sources receive query."
        ),
    )


def _required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return Path(value).expanduser().resolve()


def _positive_int(name: str, default: int, *, maximum: int) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if not 1 <= value <= maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}")
    return value


def _positive_float(name: str, default: float, *, maximum: float) -> float:
    raw = os.environ.get(name)
    value = default if raw is None else float(raw)
    if not 0 < value <= maximum:
        raise RuntimeError(f"{name} must be greater than 0 and at most {maximum}")
    return value


@lru_cache(maxsize=1)
def _service() -> object:
    coordinator_url = os.environ.get("DEEPRESEARCH_SEARCH_COORDINATOR_URL")
    if coordinator_url:
        token = os.environ.get("DEEPRESEARCH_SEARCH_COORDINATOR_TOKEN")
        namespace = os.environ.get("DEEPRESEARCH_SEARCH_NAMESPACE")
        if not token or not namespace:
            raise RuntimeError("search coordinator token and namespace are required")
        lease_value = os.environ.get("DEEPRESEARCH_SEARCH_LEASE_FILE")
        return SearchCoordinatorClient(
            url=coordinator_url,
            token=token,
            namespace=namespace,
            lease_file=(
                Path(lease_value).expanduser().resolve() if lease_value else None
            ),
        )
    search_dir = _required_path("DEEPRESEARCH_SEARCH_DIR")
    store_dir = _required_path("DEEPRESEARCH_SEARCH_STORE_DIR")
    python_executable = os.environ.get(
        "DEEPRESEARCH_SEARCH_PROVIDER_PYTHON", sys.executable
    )


    profile_env = os.environ.get("DEEPRESEARCH_SEARCH_ENV_FILE")
    environment = load_search_environment(
        search_dir,
        profile_env_file=(
            Path(profile_env) if profile_env else None
        ),
    )
    registry = ProviderRegistry(
        search_dir=search_dir,
        python_executable=python_executable,
        environment=environment,
    )
    domains = DomainRegistry(
        search_dir=search_dir,
        source_registry=registry,
    )
    return SearchService(
        registry=registry,
        domain_registry=domains,
        store=SearchStore(store_dir),
        max_workers=_positive_int(
            "DEEPRESEARCH_SEARCH_MAX_WORKERS", 8, maximum=32
        ),
        provider_limit=_positive_int(
            "DEEPRESEARCH_SEARCH_PROVIDER_LIMIT", 20, maximum=50
        ),
        batch_timeout_seconds=_positive_float(
            "DEEPRESEARCH_SEARCH_BATCH_TIMEOUT_SECONDS", 120.0, maximum=600.0
        ),
        provider_env={
            name: environment[name]
            for name in registry.environment_names
            if environment.get(name)
        },
        lease_file=(
            Path(value).expanduser().resolve()
            if (value := os.environ.get("DEEPRESEARCH_SEARCH_LEASE_FILE"))
            else None
        ),
    )


@lru_cache(maxsize=1)
def _fetch_service() -> WebFetchService:
    return WebFetchService(
        camofox_enabled=os.environ.get("DEEPRESEARCH_CAMOFOX_FALLBACK") == "1",
        camofox_base_url=os.environ.get(
            "DEEPRESEARCH_CAMOFOX_BASE_URL", "http://127.0.0.1:9377"
        ),
        identity=os.environ.get("DEEPRESEARCH_FETCH_IDENTITY", "research"),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def create_server() -> FastMCP:
    server = FastMCP(
        "DeepResearch Multi-Source Search",
        instructions=(
            "Search discovery for one Research attempt. Prefer domain tools: select every "
            "domain relevant to the evidence target and one declared operation per domain; "
            "the service fans out only to sources configured for those operations. Page "
            "through all persisted discoveries returned by each source call, "
            "then call fetch_url for selected HTML source URLs before treating candidates "
            "as evidence. fetch_url always tries ordinary HTTP first and performs at most "
            "one code-controlled Camofox fallback when configured."
        ),
        log_level="WARNING",
    )

    @server.tool(
        structured_output=False,
        description=(
            "List source routes, availability, query-writing guidance, result semantics, "
            "and credential requirements. Call this before the first search when source "
            "selection is uncertain."
        )
    )
    def list_search_sources() -> str:
        return _json(_service().list_search_sources())

    @server.tool(
        structured_output=False,
        description=(
            "List configured research domains, their operations, relevant sources, "
            "availability, and dispatch policies. Prefer this catalog over manually "
            "broadcasting a query to source providers."
        ),
    )
    def list_search_domains() -> str:
        return _json(_service().list_search_domains())

    @server.tool(
        structured_output=False,
        description=(
            "Start one or more domain searches. Each entry selects a domain and operation; "
            "the service expands it to all relevant configured sources and runs them under "
            "global, domain, and source concurrency limits. Returns immediately with a "
            "batch_id; poll get_search_batch, then page search_results by that batch_id."
        ),
    )
    def start_domain_search(searches: list[DomainSearchInput]) -> str:
        return _json(
            _service().start_domain_search(
                [search.model_dump(mode="json") for search in searches]
            )
        )

    @server.tool(
        structured_output=False,
        description=(
            "Read the current status of a batch created by start_domain_search. Completed "
            "and partial-success responses include bounded source summaries; use "
            "search_results for candidate pages."
        ),
    )
    def get_search_batch(
        batch_id: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> str:
        return _json(_service().get_search_batch(batch_id))

    @server.tool(
        structured_output=False,
        description=(
            "Execute only the provider/query pairs selected by the Research agent. "
            "Different pairs run concurrently, heterogeneous outputs are normalized and "
            "deduplicated, and every discovery retained from those bounded provider calls "
            "remains available through pagination/detail. "
            "Each entry requires provider, a provider-specific query, evidence_target, "
            "and intent. One call accepts at most 64 pairs; this is a safety bound, not a "
            "limit on the complete research process."
        )
    )
    def batch_search(searches: list[SearchInput]) -> str:
        return _json(
            _service().batch_search(
                [search.model_dump(mode="json") for search in searches]
            )
        )

    @server.tool(
        structured_output=False,
        description=(
            "Read normalized search hits page by page. Use next_cursor until it is null; "
            "optionally filter by batch_id or source provider. Snippets are discovery "
            "material only, so fetch/read selected URLs before writing evidence."
        )
    )
    def search_results(
        cursor: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        provider: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> str:
        return _json(
            _service().search_results(
                cursor=cursor,
                limit=limit,
                provider=provider,
                batch_id=batch_id,
            )
        )

    @server.tool(
        structured_output=False,
        description=(
            "Read stored source-specific fields using a hit_id or discovery_id from "
            "search_results. A discovery_id selects that provider's exact occurrence; "
            "a hit_id selects the canonical candidate and lists its provenance. "
            "This is still a search record rather than source content; fetch/read its URL "
            "before using it as evidence."
        )
    )
    def get_search_hit(hit_id: Annotated[str, Field(min_length=1, max_length=200)]) -> str:
        return _json(_service().get_search_hit(hit_id))

    @server.tool(
        structured_output=False,
        description=(
            "Fetch one selected public HTTP(S) source URL through the deterministic "
            "DeepResearch reader. It always attempts ordinary HTTP first. For 403, an "
            "explicit anti-bot challenge, a JavaScript-only shell, or a transport failure, "
            "the service may perform one read-only Camofox create/snapshot/close fallback. "
            "It never retries 429, clicks, types, logs in, imports cookies, or solves "
            "CAPTCHA. Use this instead of raw browser tools for HTML evidence pages."
        ),
    )
    def fetch_url(
        url: Annotated[
            str,
            Field(
                min_length=8,
                max_length=4_096,
                description="A public absolute HTTP(S) URL returned by search.",
            ),
        ],
    ) -> str:
        return _json(_fetch_service().fetch(url))

    return server


def _start_lease_watchdog(service: object) -> Optional[threading.Thread]:
    """End the per-Research MCP process when its attempt lease disappears.

    Hermes ACP v0.19 has no session-close operation and retains registered MCP
    processes until the whole Hermes process exits. The lease is owned by the
    CLI invocation, so it also provides a deterministic lifetime boundary for
    an idle MCP server after that Research node has finished.
    """

    # Codex App Server owns the stdio child lifecycle.  Its session startup
    # can briefly outlive the CLI-side lease bookkeeping; terminating the
    # process from this watchdog during MCP initialization makes Codex report
    # "connection closed: initialize response".  Codex will terminate the
    # child when its session ends, and the CLI still removes the lease.
    if os.environ.get("DEEPRESEARCH_SEARCH_DISABLE_WATCHDOG") == "1":
        return None
    lease_file = getattr(service, "lease_file", None)
    if lease_file is None:
        return None

    def watch() -> None:
        while lease_file.is_file():
            time.sleep(0.1)
        service.close()  # type: ignore[attr-defined]
        # SystemExit only stops this daemon thread. Exit the stdio server
        # process itself so Hermes releases its pipes and process memory.
        os._exit(0)

    watchdog = threading.Thread(
        target=watch,
        name="deepresearch-search-lease-watchdog",
        daemon=True,
    )
    watchdog.start()
    return watchdog


def main() -> None:
    # Constructing the server must keep stdout clean: stdio is the MCP wire.
    try:
        service = _service()
    except BaseException as exc:
        log_path = os.environ.get("DEEPRESEARCH_SEARCH_STARTUP_LOG")
        if log_path:
            try:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                Path(log_path).write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            except OSError:
                pass
        raise

    def terminate(signum: int, _frame: object) -> None:
        del signum
        service.close()
        raise SystemExit(0)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, terminate)
    _start_lease_watchdog(service)
    try:
        create_server().run(transport="stdio")
    finally:
        _fetch_service().close()
        service.close()


if __name__ == "__main__":
    main()
