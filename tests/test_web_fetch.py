import httpx
import pytest

from deepresearch_cli.search.fetch import FetchPolicyError, WebFetchService


PUBLIC_IPS = lambda _hostname: ("93.184.216.34",)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _service(http_handler, browser_handler=None, *, enabled=True):
    return WebFetchService(
        camofox_enabled=enabled,
        identity="test-attempt",
        resolver=PUBLIC_IPS,
        http_client=_client(http_handler),
        browser_client=_client(
            browser_handler
            or (lambda request: httpx.Response(500, request=request))
        ),
    )


def test_ordinary_html_success_never_starts_camofox():
    browser_calls = []

    def browser(request):
        browser_calls.append(request)
        return httpx.Response(500, request=request)

    service = _service(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><title>Source</title><article>" + ("useful evidence " * 30) + "</article></html>",
            request=request,
        ),
        browser,
    )

    result = service.fetch("https://example.com/source")

    assert result["ok"] is True
    assert result["retrieval"] == "http"
    assert result["fallback"] == {"attempted": False}
    assert browser_calls == []


def test_403_runs_one_read_only_camofox_fallback_and_closes_tab():
    browser_methods = []

    def browser(request):
        browser_methods.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True}, request=request)
        if request.method == "POST":
            return httpx.Response(200, json={"tabId": "tab-1"}, request=request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "url": "https://example.com/source",
                    "snapshot": '- heading "Rendered source"\n- paragraph: ' + ("evidence " * 30),
                    "truncated": False,
                },
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    service = _service(
        lambda request: httpx.Response(403, text="Forbidden", request=request),
        browser,
    )

    result = service.fetch("https://example.com/source")

    assert result["ok"] is True
    assert result["retrieval"] == "camofox"
    assert result["fallback"] == {"attempted": True, "trigger": "http_403"}
    assert browser_methods == [
        ("GET", "/health"),
        ("POST", "/tabs"),
        ("GET", "/tabs/tab-1/snapshot"),
        ("DELETE", "/tabs/tab-1"),
    ]


def test_javascript_shell_triggers_fallback():
    def browser(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True}, request=request)
        if request.method == "POST":
            return httpx.Response(200, json={"tabId": "tab-js"}, request=request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"url": "https://example.com/article", "snapshot": '- heading "Article"\n' + ("body " * 30)},
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    service = _service(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Redirecting</title><script>location.href='/article'</script></html>",
            request=request,
        ),
        browser,
    )

    result = service.fetch("https://example.com/redirect")

    assert result["retrieval"] == "camofox"
    assert result["ordinary_fetch"]["reason"] == "javascript_shell"


def test_captcha_is_reported_and_tab_is_still_closed():
    deleted = []

    def browser(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True}, request=request)
        if request.method == "POST":
            return httpx.Response(200, json={"tabId": "tab-captcha"}, request=request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"url": "https://example.com/challenge?__cf_chl_rt_tk=secret&source=doi", "snapshot": '- heading "Are you a robot?"\n- text: Please confirm you are a human'},
                request=request,
            )
        deleted.append(request.url.path)
        return httpx.Response(200, json={"ok": True}, request=request)

    service = _service(
        lambda request: httpx.Response(403, request=request), browser
    )

    result = service.fetch("https://example.com/challenge")

    assert result["ok"] is False
    assert result["reason"] == "interactive_challenge"
    assert result["final_url"] == "https://example.com/challenge?source=doi"
    assert deleted == ["/tabs/tab-captcha"]


def test_429_never_uses_camofox():
    browser_calls = []
    service = _service(
        lambda request: httpx.Response(429, request=request),
        lambda request: browser_calls.append(request),
    )

    result = service.fetch("https://example.com/rate-limited")

    assert result["reason"] == "rate_limited"
    assert result["fallback"] == {"attempted": False}
    assert browser_calls == []


def test_same_url_never_falls_back_twice():
    posts = []

    def browser(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True}, request=request)
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(500, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    service = _service(
        lambda request: httpx.Response(403, request=request), browser
    )

    first = service.fetch("https://example.com/once")
    second = service.fetch("https://example.com/once#fragment")

    assert first["fallback"]["attempted"] is True
    assert second["fallback"] == {
        "attempted": False,
        "skipped": "already_attempted_for_url",
    }
    assert len(posts) == 1


def test_private_network_targets_are_rejected():
    service = WebFetchService(
        resolver=lambda _hostname: ("127.0.0.1",),
        http_client=_client(lambda request: httpx.Response(200, request=request)),
        browser_client=_client(lambda request: httpx.Response(200, request=request)),
    )

    with pytest.raises(FetchPolicyError, match="private"):
        service.fetch("http://localhost/admin")
