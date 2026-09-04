"""Small authenticated client for the run-scoped search coordinator."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


class SearchCoordinatorClient:
    def __init__(
        self,
        *,
        url: str,
        token: str,
        namespace: str,
        timeout_seconds: float = 190.0,
        lease_file: Optional[Path] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.timeout_seconds = timeout_seconds
        self.lease_file = lease_file

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        payload = json.dumps(
            {"method": method, "params": params, "namespace": self.namespace},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url + "/rpc",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"search coordinator HTTP {exc.code}: {detail[:1000]}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"search coordinator request failed: {exc}") from exc
        if not isinstance(value, dict) or value.get("ok") is not True:
            error = value.get("error") if isinstance(value, dict) else value
            raise RuntimeError(f"search coordinator rejected {method}: {error}")
        result = value.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"search coordinator returned invalid {method} result")
        return result

    def list_search_sources(self) -> dict[str, Any]:
        return self._call("list_search_sources")

    def list_search_domains(self) -> dict[str, Any]:
        return self._call("list_search_domains")

    def batch_search(self, searches: object) -> dict[str, Any]:
        return self._call("batch_search", searches=searches)

    def domain_search(self, searches: object) -> dict[str, Any]:
        return self._call("domain_search", searches=searches)

    def start_domain_search(self, searches: object) -> dict[str, Any]:
        return self._call("start_domain_search", searches=searches)

    def get_search_batch(self, batch_id: str) -> dict[str, Any]:
        return self._call("get_search_batch", batch_id=batch_id)

    def search_results(
        self,
        *,
        cursor: int = 0,
        limit: int = 20,
        provider: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._call(
            "search_results",
            cursor=cursor,
            limit=limit,
            provider=provider,
            batch_id=batch_id,
        )

    def get_search_hit(self, hit_id: str) -> dict[str, Any]:
        return self._call("get_search_hit", hit_id=hit_id)

    def record_fetch(
        self,
        *,
        url: str,
        final_url: Optional[str] = None,
        status: str,
        retrieval: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._call(
            "record_fetch",
            url=url,
            final_url=final_url,
            status=status,
            retrieval=retrieval,
            elapsed_seconds=elapsed_seconds,
            reason=reason,
        )

    def close(self) -> None:
        return None
