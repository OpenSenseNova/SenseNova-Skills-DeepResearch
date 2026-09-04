"""Stable data contracts shared by the search service and MCP adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


MAX_BATCH_SIZE = 64
MAX_QUERY_CHARS = 500
MAX_CONTEXT_CHARS = 1_000
MAX_DOMAIN_BATCH_SIZE = 16


class SearchContractError(ValueError):
    """Raised before any provider process is started."""


@dataclass(frozen=True)
class SearchRequest:
    provider: str
    query: str
    evidence_target: str
    intent: str
    domain: str | None = None
    operation: str | None = None
    namespace: str | None = None

    @property
    def pair_key(self) -> str:
        normalized = re.sub(r"\s+", " ", self.query).strip().casefold()
        return f"{self.provider}\u0000{normalized}"

    @property
    def logical_key(self) -> str:
        """Identify one research purpose without changing execution reuse.

        A provider/query pair is the unit of external execution.  The same
        execution may, however, serve more than one evidence target.  Keeping
        those logical requests distinct prevents deduplication from erasing
        provenance while still allowing the provider process to run once.
        """

        values = (
            self.provider,
            self.query,
            self.evidence_target,
            self.intent,
            self.domain or "",
            self.operation or "",
            self.namespace or "",
        )
        return "\u0000".join(
            re.sub(r"\s+", " ", value).strip().casefold() for value in values
        )

    def to_dict(self) -> dict[str, str]:
        value = {
            "provider": self.provider,
            "query": self.query,
            "evidence_target": self.evidence_target,
            "intent": self.intent,
        }
        if self.domain is not None:
            value["domain"] = self.domain
        if self.operation is not None:
            value["operation"] = self.operation
        if self.namespace is not None:
            value["namespace"] = self.namespace
        return value


@dataclass(frozen=True)
class DomainSearchRequest:
    domain: str
    operation: str | None
    query: str
    evidence_target: str
    intent: str
    source_policy: str = "all_relevant"
    source_queries: Mapping[str, str] | None = None
    namespace: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "domain": self.domain,
            "operation": self.operation,
            "query": self.query,
            "evidence_target": self.evidence_target,
            "intent": self.intent,
            "source_policy": self.source_policy,
            "source_queries": dict(self.source_queries or {}),
            "namespace": self.namespace,
        }


def _required_text(
    value: object,
    *,
    field: str,
    index: int,
    max_chars: int,
) -> str:
    if not isinstance(value, str):
        raise SearchContractError(f"searches[{index}].{field} must be a string")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        raise SearchContractError(f"searches[{index}].{field} cannot be empty")
    if len(normalized) > max_chars:
        raise SearchContractError(
            f"searches[{index}].{field} exceeds {max_chars} characters"
        )
    return normalized


def validate_search_requests(
    searches: object,
    *,
    provider_names: Iterable[str],
    max_batch_size: int = MAX_BATCH_SIZE,
) -> list[SearchRequest]:
    """Validate strictly and remove exact duplicates within one tool call.

    Invalid entries are never silently discarded: one malformed provider/query
    pair rejects the complete call so the agent can repair its plan.
    """

    if not isinstance(searches, Sequence) or isinstance(
        searches, (str, bytes, bytearray)
    ):
        raise SearchContractError("searches must be a JSON array")
    if not searches:
        raise SearchContractError("searches cannot be empty")
    if len(searches) > max_batch_size:
        raise SearchContractError(
            f"one batch can contain at most {max_batch_size} provider/query pairs"
        )

    supported = frozenset(provider_names)
    requests: list[SearchRequest] = []
    seen_requests: set[str] = set()
    for index, raw in enumerate(searches):
        if isinstance(raw, SearchRequest):
            raw = raw.to_dict()
        if isinstance(raw, Mapping):
            provider = _required_text(
                raw.get("provider"),
                field="provider",
                index=index,
                max_chars=80,
            )
            if provider not in supported:
                names = ", ".join(sorted(supported))
                raise SearchContractError(
                    f"searches[{index}].provider is unsupported: {provider}; "
                    f"choose one of: {names}"
                )
            request = SearchRequest(
                provider=provider,
                query=_required_text(
                    raw.get("query"),
                    field="query",
                    index=index,
                    max_chars=MAX_QUERY_CHARS,
                ),
                evidence_target=_required_text(
                    raw.get("evidence_target"),
                    field="evidence_target",
                    index=index,
                    max_chars=MAX_CONTEXT_CHARS,
                ),
                intent=_required_text(
                    raw.get("intent"),
                    field="intent",
                    index=index,
                    max_chars=MAX_CONTEXT_CHARS,
                ),
                domain=(
                    _required_text(
                        raw.get("domain"),
                        field="domain",
                        index=index,
                        max_chars=64,
                    )
                    if raw.get("domain") is not None
                    else None
                ),
                operation=(
                    _required_text(
                        raw.get("operation"),
                        field="operation",
                        index=index,
                        max_chars=64,
                    )
                    if raw.get("operation") is not None
                    else None
                ),
                namespace=(
                    _required_text(
                        raw.get("namespace"),
                        field="namespace",
                        index=index,
                        max_chars=300,
                    )
                    if raw.get("namespace") is not None
                    else None
                ),
            )
        else:
            raise SearchContractError(f"searches[{index}] must be an object")
        if request.provider not in supported:
            raise SearchContractError(f"unsupported provider: {request.provider}")
        # Only an exactly repeated logical request is redundant.  Two entries
        # may intentionally use the same provider/query for different evidence
        # targets; the service groups their external execution but records a
        # discovery occurrence for every target.
        if request.logical_key in seen_requests:
            continue
        seen_requests.add(request.logical_key)
        requests.append(request)
    return requests


def validate_domain_search_requests(
    searches: object,
    *,
    domain_names: Iterable[str],
    max_batch_size: int = MAX_DOMAIN_BATCH_SIZE,
) -> list[DomainSearchRequest]:
    """Validate domain-level requests before they fan out into source jobs."""

    if not isinstance(searches, Sequence) or isinstance(
        searches, (str, bytes, bytearray)
    ):
        raise SearchContractError("searches must be a JSON array")
    if not searches:
        raise SearchContractError("searches cannot be empty")
    if len(searches) > max_batch_size:
        raise SearchContractError(
            f"one domain batch can contain at most {max_batch_size} searches"
        )
    supported = frozenset(domain_names)
    output: list[DomainSearchRequest] = []
    seen: set[tuple[object, ...]] = set()
    for index, raw in enumerate(searches):
        if not isinstance(raw, Mapping):
            raise SearchContractError(f"searches[{index}] must be an object")
        domain = _required_text(
            raw.get("domain"), field="domain", index=index, max_chars=64
        )
        if domain not in supported:
            choices = ", ".join(sorted(supported)) or "none configured"
            raise SearchContractError(
                f"searches[{index}].domain is unsupported: {domain}; choose one of: {choices}"
            )
        operation_value = raw.get("operation")
        if operation_value is None:
            operation = None
        else:
            operation = _required_text(
                operation_value, field="operation", index=index, max_chars=64
            )
        policy = str(raw.get("source_policy") or "all_relevant").strip()
        if policy != "all_relevant":
            raise SearchContractError(
                f"searches[{index}].source_policy must be all_relevant"
            )
        raw_source_queries = raw.get("source_queries")
        if raw_source_queries is not None and not isinstance(raw_source_queries, Mapping):
            raise SearchContractError(
                f"searches[{index}].source_queries must be an object"
            )
        request = DomainSearchRequest(
            domain=domain,
            operation=operation,
            query=_required_text(
                raw.get("query"), field="query", index=index, max_chars=MAX_QUERY_CHARS
            ),
            evidence_target=_required_text(
                raw.get("evidence_target"),
                field="evidence_target",
                index=index,
                max_chars=MAX_CONTEXT_CHARS,
            ),
            intent=_required_text(
                raw.get("intent"), field="intent", index=index, max_chars=MAX_CONTEXT_CHARS
            ),
            source_policy=policy,
            source_queries=(
                {
                    str(source): _required_text(
                        query,
                        field=f"source_queries.{source}",
                        index=index,
                        max_chars=MAX_QUERY_CHARS,
                    )
                    for source, query in (raw_source_queries or {}).items()
                }
                if raw_source_queries is not None
                else None
            ),
            namespace=(
                _required_text(
                    raw.get("namespace"),
                    field="namespace",
                    index=index,
                    max_chars=300,
                )
                if raw.get("namespace") is not None
                else None
            ),
        )
        key = (
            request.domain,
            request.operation,
            request.query.casefold(),
            request.evidence_target.casefold(),
            request.intent.casefold(),
            tuple(sorted((request.source_queries or {}).items())),
            request.namespace,
        )
        if key not in seen:
            seen.add(key)
            output.append(request)
    return output


def json_safe(value: Any, *, max_string_chars: int = 4_000) -> Any:
    """Project arbitrary provider values into bounded JSON-compatible data."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value
        return value[:max_string_chars] + "\u2026"
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(item, max_string_chars=max_string_chars)
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            json_safe(item, max_string_chars=max_string_chars)
            for item in list(value)[:80]
        ]
    return str(value)[:max_string_chars]
