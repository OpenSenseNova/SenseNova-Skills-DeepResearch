"""Deterministic HTTP-first page fetch with a bounded Camofox fallback."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import threading
from html.parser import HTMLParser
from typing import Callable, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx


_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
_MAX_CONTENT_CHARS = 60_000
_MAX_REDIRECTS = 5
_BLOCK_TAGS = frozenset(
    {"address", "article", "aside", "blockquote", "br", "div", "footer", "h1",
     "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
     "p", "pre", "section", "table", "td", "th", "tr"}
)
_SKIP_TAGS = frozenset({"canvas", "noscript", "script", "style", "svg", "template"})
_CHALLENGE_PATTERNS = (
    re.compile(r"\bare you a robot\b", re.IGNORECASE),
    re.compile(r"\bjust a moment\b", re.IGNORECASE),
    re.compile(r"\bverify you are human\b", re.IGNORECASE),
    re.compile(r"\bchecking (?:your )?browser\b", re.IGNORECASE),
    re.compile(r"\bplease confirm you are a human\b", re.IGNORECASE),
    re.compile(r"\benable javascript and cookies to continue\b", re.IGNORECASE),
    re.compile(r"\battention required!\s*\|\s*cloudflare\b", re.IGNORECASE),
)


class FetchPolicyError(ValueError):
    pass


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.parts: list[str] = []
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        tag = tag.casefold()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data)
        if self._in_title:
            self.title_parts.append(data)

    def result(self) -> tuple[str, str]:
        title = " ".join(" ".join(self.title_parts).split())
        lines = []
        for line in "".join(self.parts).splitlines():
            compact = " ".join(line.split())
            if compact and (not lines or compact != lines[-1]):
                lines.append(compact)
        return title, "\n".join(lines)


def _default_resolver(hostname: str) -> Sequence[str]:
    return tuple(
        sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            }
        )
    )


def _is_challenge(title: str, text: str) -> bool:
    sample = f"{title}\n{text[:2_500]}"
    return any(pattern.search(sample) for pattern in _CHALLENGE_PATTERNS)


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path or "/", parts.query, ""))


def _public_result_url(url: str) -> str:
    """Drop transient anti-bot challenge tokens from URLs returned as evidence metadata."""
    parts = urlsplit(url)
    query = urlencode(
        [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if not name.casefold().startswith(("__cf_chl_", "cf_chl_"))
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


class WebFetchService:
    def __init__(
        self,
        *,
        camofox_enabled: bool = False,
        camofox_base_url: str = "http://127.0.0.1:9377",
        identity: str = "research",
        resolver: Callable[[str], Sequence[str]] = _default_resolver,
        http_client: Optional[httpx.Client] = None,
        browser_client: Optional[httpx.Client] = None,
    ) -> None:
        self.camofox_enabled = camofox_enabled
        self.camofox_base_url = camofox_base_url.rstrip("/")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        self.user_id = f"deepresearch-fetch-{digest}"
        self.session_key = digest
        self.resolver = resolver
        self.http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "DeepResearchCLI/0.1 (+local research fetch)"},
            trust_env=True,
        )
        self.browser_client = browser_client or httpx.Client(timeout=httpx.Timeout(35.0))
        self._own_http_client = http_client is None
        self._own_browser_client = browser_client is None
        self._fallback_urls: set[str] = set()
        self._fallback_lock = threading.Lock()

    def close(self) -> None:
        if self._own_http_client:
            self.http_client.close()
        if self._own_browser_client:
            self.browser_client.close()

    def _validate_public_url(self, url: str) -> str:
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise FetchPolicyError("only absolute HTTP(S) URLs are allowed")
        if parts.username or parts.password:
            raise FetchPolicyError("URLs containing credentials are not allowed")
        try:
            addresses = self.resolver(parts.hostname)
        except OSError as exc:
            raise FetchPolicyError(f"cannot resolve target hostname: {parts.hostname}") from exc
        if not addresses:
            raise FetchPolicyError(f"target hostname has no addresses: {parts.hostname}")
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global:
                raise FetchPolicyError("local, private, reserved, and link-local targets are blocked")
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))

    def _http_fetch(self, requested_url: str) -> dict[str, object]:
        current = self._validate_public_url(requested_url)
        for redirect_count in range(_MAX_REDIRECTS + 1):
            try:
                with self.http_client.stream("GET", current, follow_redirects=False) as response:
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308} and response.headers.get("location"):
                        if redirect_count == _MAX_REDIRECTS:
                            return {"ok": False, "reason": "too_many_redirects", "status": status, "final_url": current}
                        current = self._validate_public_url(urljoin(current, response.headers["location"]))
                        continue
                    body = bytearray()
                    truncated = False
                    for chunk in response.iter_bytes():
                        remaining = _MAX_DOWNLOAD_BYTES - len(body)
                        if remaining <= 0:
                            truncated = True
                            break
                        body.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True
                            break
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                    encoding = response.encoding or "utf-8"
            except (httpx.HTTPError, OSError) as exc:
                return {"ok": False, "reason": "network_error", "error": f"{type(exc).__name__}: {exc}", "final_url": current}

            if status == 429:
                return {"ok": False, "reason": "rate_limited", "status": status, "final_url": current, "fallback_allowed": False}
            if status == 403:
                return {"ok": False, "reason": "http_403", "status": status, "final_url": current, "fallback_allowed": True}
            if status < 200 or status >= 300:
                return {"ok": False, "reason": "http_error", "status": status, "final_url": current, "fallback_allowed": False}
            if content_type == "application/pdf":
                return {"ok": False, "reason": "pdf_requires_document_reader", "status": status, "final_url": current, "content_type": content_type, "fallback_allowed": False}

            text = bytes(body).decode(encoding, errors="replace")
            if content_type in {"text/html", "application/xhtml+xml", ""} or "<html" in text[:1_000].casefold():
                parser = _VisibleTextParser()
                parser.feed(text)
                title, visible = parser.result()
                if _is_challenge(title, visible):
                    return {"ok": False, "reason": "challenge_page", "status": status, "final_url": current, "title": title, "fallback_allowed": True}
                script_count = len(re.findall(r"<script\b", text, flags=re.IGNORECASE))
                if len(visible) < 240 and script_count:
                    return {"ok": False, "reason": "javascript_shell", "status": status, "final_url": current, "title": title, "fallback_allowed": True}
                content = visible
            elif content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/rss+xml", "application/atom+xml"}:
                title, content = "", text
            else:
                return {"ok": False, "reason": "unsupported_content_type", "status": status, "final_url": current, "content_type": content_type, "fallback_allowed": False}

            content = content[:_MAX_CONTENT_CHARS]
            return {
                "ok": True,
                "retrieval": "http",
                "status": status,
                "final_url": current,
                "content_type": content_type,
                "title": title,
                "content": content,
                "truncated": truncated or len(content) >= _MAX_CONTENT_CHARS,
            }
        raise AssertionError("redirect loop escaped its bound")

    def _camofox_fetch(self, url: str, reason: str) -> dict[str, object]:
        tab_id: Optional[str] = None
        try:
            health = self.browser_client.get(f"{self.camofox_base_url}/health")
            health.raise_for_status()
            if not bool(health.json().get("ok")):
                raise RuntimeError("Camofox health check did not return ok=true")
            created = self.browser_client.post(
                f"{self.camofox_base_url}/tabs",
                json={"url": url, "userId": self.user_id, "sessionKey": self.session_key},
            )
            created.raise_for_status()
            tab_id = str(created.json()["tabId"])
            snapshot = self.browser_client.get(
                f"{self.camofox_base_url}/tabs/{tab_id}/snapshot",
                params={"userId": self.user_id, "includeScreenshot": "false"},
            )
            snapshot.raise_for_status()
            payload = snapshot.json()
            final_url = _public_result_url(
                self._validate_public_url(str(payload.get("url") or url))
            )
            content = str(payload.get("snapshot") or "")
            first_heading = re.search(r'^\s*-?\s*heading "([^"]+)"', content, flags=re.MULTILINE)
            title = first_heading.group(1) if first_heading else ""
            if _is_challenge(title, content):
                return {
                    "ok": False,
                    "retrieval": "camofox",
                    "reason": "interactive_challenge",
                    "final_url": final_url,
                    "fallback": {"attempted": True, "trigger": reason},
                }
            if len(content.strip()) < 80:
                return {
                    "ok": False,
                    "retrieval": "camofox",
                    "reason": "empty_browser_snapshot",
                    "final_url": final_url,
                    "fallback": {"attempted": True, "trigger": reason},
                }
            return {
                "ok": True,
                "retrieval": "camofox",
                "final_url": final_url,
                "title": title,
                "content": content[:_MAX_CONTENT_CHARS],
                "truncated": len(content) > _MAX_CONTENT_CHARS or bool(payload.get("truncated")),
                "fallback": {"attempted": True, "trigger": reason},
            }
        except (httpx.HTTPError, OSError, RuntimeError, KeyError, ValueError) as exc:
            return {
                "ok": False,
                "retrieval": "camofox",
                "reason": "camofox_unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "final_url": url,
                "fallback": {"attempted": True, "trigger": reason},
            }
        finally:
            if tab_id is not None:
                try:
                    self.browser_client.delete(
                        f"{self.camofox_base_url}/tabs/{tab_id}",
                        params={"userId": self.user_id},
                    )
                except (httpx.HTTPError, OSError):
                    pass

    def fetch(self, url: str) -> dict[str, object]:
        requested = self._validate_public_url(url)
        ordinary = self._http_fetch(requested)
        if ordinary.get("ok"):
            return {"requested_url": requested, **ordinary, "fallback": {"attempted": False}}
        if not self.camofox_enabled or not ordinary.get("fallback_allowed", ordinary.get("reason") == "network_error"):
            return {"requested_url": requested, **ordinary, "fallback": {"attempted": False}}
        canonical = _canonical_url(requested)
        with self._fallback_lock:
            if canonical in self._fallback_urls:
                return {
                    "requested_url": requested,
                    **ordinary,
                    "fallback": {"attempted": False, "skipped": "already_attempted_for_url"},
                }
            self._fallback_urls.add(canonical)
        return {
            "requested_url": requested,
            "ordinary_fetch": ordinary,
            **self._camofox_fetch(str(ordinary.get("final_url") or requested), str(ordinary.get("reason") or "fetch_failed")),
        }
