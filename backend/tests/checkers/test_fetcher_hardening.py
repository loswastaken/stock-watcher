"""Fetcher hardening: curl_cffi impersonation routing, challenge / waiting-room detection,
Best Buy splash bypass, the fetch recording hook and the browser identity."""
from __future__ import annotations

import asyncio
import sys

import httpx
import pytest
import respx

from app.checkers import fetcher

from .conftest import load


def fx(name: str) -> str:
    return load(f"platforms/{name}")


# ------------------------------------------------------------------ impersonation routing


class _FakeHeaders:
    def __init__(self, items):
        self._items = items

    def multi_items(self):
        return list(self._items)


class _FakeCurlResponse:
    def __init__(self, url, status=200, body=b"", headers=()):
        self.url = url
        self.status_code = status
        self.content = body
        self.headers = _FakeHeaders(headers)


class _Timeout(Exception):
    pass


class _FakeCurlModule:
    """Stands in for ``curl_cffi.requests``."""

    class exceptions:  # noqa: N801
        Timeout = _Timeout

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[tuple[str, dict]] = []
        self.session_kwargs: dict = {}
        mod = self

        class AsyncSession:
            def __init__(self, **kw):
                mod.session_kwargs = kw

            async def get(self, url, headers=None):
                mod.calls.append((url, dict(headers or {})))
                return mod.respond(url)

            async def close(self):
                pass

        self.AsyncSession = AsyncSession


@pytest.fixture
def fake_curl(monkeypatch):
    def install(respond):
        mod = _FakeCurlModule(respond)
        monkeypatch.setattr(fetcher, "_curl_requests", lambda: mod)
        monkeypatch.setenv("STOCKWATCHER_IMPERSONATE", "1")
        return mod

    return install


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.bestbuy.com/site/x/6614313.p", True),
    ("https://bestbuy.com/", True),
    ("https://api.direct.playstation.com/x", True),
    ("https://www.walmart.com/ip/123", True),
    ("https://www.target.com/p/A-1", False),
    ("https://notbestbuy.com/", False),
    ("https://shop.example/", False),
])
def test_wants_impersonation_suffix_match(fake_curl, url, expected):
    fake_curl(lambda u: _FakeCurlResponse(u))
    assert fetcher.wants_impersonation(url) is expected


def test_impersonation_env_switch(fake_curl, monkeypatch):
    fake_curl(lambda u: _FakeCurlResponse(u))
    for off in ("0", "false", "FALSE", "no"):
        monkeypatch.setenv("STOCKWATCHER_IMPERSONATE", off)
        assert not fetcher.wants_impersonation("https://www.bestbuy.com/")
    monkeypatch.setenv("STOCKWATCHER_IMPERSONATE", "1")
    assert fetcher.wants_impersonation("https://www.bestbuy.com/")
    monkeypatch.setattr(fetcher, "_curl_requests", lambda: None)  # curl_cffi not installed
    assert not fetcher.wants_impersonation("https://www.bestbuy.com/")


async def test_http_get_routes_impersonated_host_through_curl(fake_curl):
    url = "https://www.bestbuy.com/site/x/6614313.p?skuId=6614313"
    final = url + "&intl=nosplash"
    mod = fake_curl(lambda u: _FakeCurlResponse(
        final, 200, "<html>Añadir add to cart</html>".encode(),
        [("Content-Type", "text/html; charset=utf-8"), ("Content-Encoding", "gzip"), ("Content-Length", "999"),
         ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]))
    with fetcher.recording() as rec:
        resp = await fetcher.http_get(url, headers={"Referer": "https://www.bestbuy.com/", "Accept": "application/json"})
    assert isinstance(resp, httpx.Response)
    assert resp.status_code == 200 and str(resp.url) == final
    assert resp.text == "<html>Añadir add to cart</html>"
    assert "content-encoding" not in resp.headers  # body is already decoded
    assert resp.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert mod.session_kwargs["impersonate"] == "chrome"
    sent = mod.calls[0][1]
    # curl_cffi supplies the fingerprint headers; ours would contradict its Chrome build
    assert not any(k.lower() in {"user-agent", "sec-ch-ua", "sec-ch-ua-platform", "accept-encoding"} for k in sent)
    assert sent["Referer"] == "https://www.bestbuy.com/" and sent["Accept"] == "application/json"
    assert [k for k in sent if k.lower() == "accept"] == ["Accept"]
    assert rec[0]["via"] == "curl" and rec[0]["status"] == 200 and rec[0]["final_url"] == final


@respx.mock
async def test_http_get_other_hosts_and_explicit_client_use_httpx(fake_curl):
    mod = fake_curl(lambda u: _FakeCurlResponse(u, 200, b"curl"))
    respx.get("https://www.target.com/p/A-1").mock(return_value=httpx.Response(200, text="httpx"))
    respx.get("https://www.bestbuy.com/x").mock(return_value=httpx.Response(200, text="httpx-bb"))
    assert (await fetcher.http_get("https://www.target.com/p/A-1")).text == "httpx"
    async with fetcher.make_client() as client:
        assert (await fetcher.http_get("https://www.bestbuy.com/x", client=client)).text == "httpx-bb"
    assert mod.calls == []


@respx.mock
async def test_impersonation_off_uses_httpx_for_bestbuy():
    # conftest sets STOCKWATCHER_IMPERSONATE=0, so respx sees Best Buy requests.
    route = respx.get("https://www.bestbuy.com/x").mock(return_value=httpx.Response(200, text="ok"))
    assert (await fetcher.http_get("https://www.bestbuy.com/x")).text == "ok" and route.called


async def test_curl_errors_become_fetch_errors(fake_curl):
    def timeout(u):
        raise _Timeout("Operation timed out after 20000 milliseconds")

    fake_curl(timeout)
    with fetcher.recording() as rec:
        with pytest.raises(fetcher.FetchError, match="Timed out fetching www.bestbuy.com"):
            await fetcher.http_get("https://www.bestbuy.com/x")
    assert rec[0]["status"] is None and "Timed out" in rec[0]["error"] and rec[0]["via"] == "curl"

    def reset(u):
        raise ConnectionError("curl: (35) Recv failure: Connection reset by peer\nmore")

    fetcher._st().curl = None  # drop the session bound to the first fake
    fake_curl(reset)
    with pytest.raises(fetcher.FetchError, match=r"Network error: ConnectionError: curl: \(35\)"):
        await fetcher.http_get("https://www.costco.com/x")


async def test_fetch_html_through_curl_falls_back_to_browser_on_stall(fake_curl, monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")

    def stall(u):
        raise _Timeout("timed out")

    fake_curl(stall)
    calls = []

    async def fake_browser(url):
        calls.append(url)
        return fetcher.FetchResult(url=url, status=200, text=load("button_enabled.html"), via_browser=True)

    monkeypatch.setattr(fetcher, "browser_fetch", fake_browser)
    res = await fetcher.fetch_html("https://www.bestbuy.com/site/x/6614313.p?skuId=6614313")
    assert res.via_browser and calls == ["https://www.bestbuy.com/site/x/6614313.p?skuId=6614313&intl=nosplash"]


def test_http2_enabled():
    assert fetcher._HTTP2 is True


# ------------------------------------------------------------------ Best Buy splash


@respx.mock
async def test_bestbuy_document_fetch_skips_intl_splash():
    route = respx.get("https://www.bestbuy.com/site/x/6614313.p",
                      params={"skuId": "6614313", "intl": "nosplash"}).mock(
        return_value=httpx.Response(200, text=load("button_enabled.html")))
    res = await fetcher.fetch_html("https://www.bestbuy.com/site/x/6614313.p?skuId=6614313")
    assert route.called and "intl=nosplash" in res.url


def test_prepare_document_url():
    p = fetcher.prepare_document_url
    assert p("https://www.bestbuy.com/product/x/J3Z/sku/6614313") == "https://www.bestbuy.com/product/x/J3Z/sku/6614313?intl=nosplash"
    assert p("https://www.bestbuy.com/a?intl=nosplash") == "https://www.bestbuy.com/a?intl=nosplash"
    assert p("https://www.target.com/p/A-1?x=1") == "https://www.target.com/p/A-1?x=1"


# ------------------------------------------------------------------ challenge markers


@pytest.mark.parametrize("fixture", [
    "amazon_captcha.html", "amazon_continue_shopping.html", "perimeterx_block.html", "ebay_challenge.html",
    "newegg_areyouahuman.html",
])
def test_new_challenge_markers(fixture):
    assert fetcher.looks_like_challenge(fx(fixture))


@pytest.mark.parametrize("url", [
    "https://www.walmart.com/blocked?url=L2lwLzEyMw==&uuid=abc",
    "https://www.samsclub.com/blocked",
    "https://www.ebay.com/splashui/challenge?ap=1&appName=orch",
    "https://www.newegg.com/areyouahuman?referer=%2Fp%2FN82E16814137892",
    "https://www.amazon.com/errors/validateCaptcha?amzn=abc",
])
def test_challenge_final_url(url):
    assert fetcher.looks_like_challenge("<html><body>ok</body></html>", url)


def test_normal_pages_are_not_challenges():
    robot = fx("robot_product.html")
    assert not fetcher.looks_like_challenge(robot, "https://homegoods.example/p/robomop-3000")
    # vendor script tags alone are not challenges, even on small pages
    assert not fetcher.looks_like_challenge(
        '<html><script>window._pxAppId="PX1";</script><script src="/_Incapsula_Resource?SWJIYLWA=719d"></script>'
        '<button>Add to cart</button></html>')
    assert not fetcher.looks_like_challenge("<html>ok</html>", "https://www.homedepot.com/p/blocked-drain-opener/123")
    for name in ("jsonld_graph_instock.html", "button_enabled.html", "shopify_variants.html"):
        assert not fetcher.looks_like_challenge(load(name))


def test_small_page_markers_ignored_on_large_pages():
    big = "<html><body>" + ("<p>filler product copy</p>" * 12_000) + "<p>Are you a robot? Our AI says no.</p></body></html>"
    assert len(big) > 200_000
    assert not fetcher.looks_like_challenge(big)
    assert fetcher.looks_like_challenge("<html><body><p>Are you a robot?</p></body></html>")


@respx.mock
async def test_walmart_blocked_redirect_raises_bot_protection():
    url = "https://www.walmart.com/ip/thing/123"
    respx.get(url).mock(return_value=httpx.Response(307, headers={"Location": "/blocked?url=L2lw"}))
    respx.get("https://www.walmart.com/blocked?url=L2lw").mock(
        return_value=httpx.Response(200, text="<html><h1>Robot or human?</h1><button>Add to cart</button></html>"))
    with pytest.raises(fetcher.FetchError, match="bot protection"):
        await fetcher.fetch_html(url)


# ------------------------------------------------------------------ waiting rooms


@pytest.mark.parametrize(("html", "url"), [
    ("<html>hi</html>", "https://gamestop.queue-it.net/?c=gamestop&e=ps5drop"),
    ("<html>hi</html>", "https://direct-queue.playstation.com/?c=sonyied&e=psdirectps5"),
    ("<html>hi</html>", "https://shop.example/throttle/queue"),
    (fx("queue_it.html"), None),
    (fx("imperva_waiting_room.html"), None),
    ('<html><head><title>Queue</title><script src="//static.queue-it.net/script/queueclient.min.js"></script></head>'
     '<body>Your place in line: 1043</body></html>', None),
])
def test_looks_like_queue(html, url):
    assert fetcher.looks_like_queue(html, url)


def test_queue_not_flagged_on_product_pages():
    # queue-it's script + "waiting room"/"in line" copy on a real product page
    assert not fetcher.looks_like_queue(fx("robot_product.html"), "https://homegoods.example/p/robomop")
    assert not fetcher.looks_like_queue(
        '<html><script src="//static.queue-it.net/script/queueclient.min.js"></script><h1>Blender</h1></html>',
        "https://shop.example/p/blender")
    assert not fetcher.looks_like_queue(load("jsonld_graph_instock.html"), "https://shop.example/p")
    assert not fetcher.looks_like_queue("", None)


@respx.mock
async def test_fetch_html_returns_queued_without_raising(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")

    async def no_browser(url):
        raise AssertionError("a waiting room must not trigger the browser")

    monkeypatch.setattr(fetcher, "browser_fetch", no_browser)
    url = "https://www.gamestop.com/consoles/ps5/20012345.html"
    respx.get(url).mock(return_value=httpx.Response(302, headers={"Location": "https://gamestop.queue-it.net/?c=gs"}))
    respx.get("https://gamestop.queue-it.net/?c=gs").mock(return_value=httpx.Response(503, text="<html>busy</html>"))
    res = await fetcher.fetch_html(url)
    assert res.queued and res.status == 503 and res.url.startswith("https://gamestop.queue-it.net/")


@respx.mock
async def test_browser_queue_result_is_returned(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    url = "https://direct.playstation.com/en-us/buy-consoles/ps5-pro-console"
    respx.get(url).mock(return_value=httpx.Response(403, text="denied"))

    async def fake_browser(u):
        return fetcher.FetchResult(url="https://direct-queue.playstation.com/?c=sonyied", status=200,
                                   text="<html>queue</html>", via_browser=True, queued=True)

    monkeypatch.setattr(fetcher, "browser_fetch", fake_browser)
    res = await fetcher.fetch_html(url)
    assert res.queued and res.via_browser


# ------------------------------------------------------------------ recording hook


@respx.mock
async def test_recording_captures_http_requests(monkeypatch):
    monkeypatch.setattr(fetcher, "RECORD_BODY_LIMIT", 10)
    respx.get("https://a.example/1").mock(return_value=httpx.Response(200, text="0123456789ABCDEF",
                                                                      headers={"X-Test": "yes"}))
    respx.get("https://b.example/2").mock(return_value=httpx.Response(404, text="nope"))
    respx.get("https://c.example/3").mock(side_effect=httpx.ConnectError("refused"))
    await fetcher.http_get("https://a.example/1")  # outside: not recorded
    with fetcher.recording() as rec:
        await fetcher.http_get("https://a.example/1")
        # concurrent tasks spawned inside the block record into the same list
        await asyncio.gather(fetcher.http_get("https://b.example/2"),
                             asyncio.create_task(fetcher.http_get("https://a.example/1")))
        with pytest.raises(fetcher.FetchError):
            await fetcher.http_get("https://c.example/3")
    await fetcher.http_get("https://a.example/1")  # after: not recorded
    assert len(rec) == 4
    first = rec[0]
    assert set(first) >= {"method", "url", "status", "via", "headers", "body", "elapsed_ms"}
    assert first == {**first, "method": "GET", "url": "https://a.example/1", "status": 200, "via": "http",
                     "body": "0123456789"}
    assert first["headers"]["x-test"] == "yes" and isinstance(first["elapsed_ms"], int)
    assert sorted(e["status"] for e in rec[1:3]) == [200, 404]
    assert rec[3]["status"] is None and "refused" in rec[3]["error"]


@respx.mock
async def test_recording_covers_get_json_and_fetch_html():
    from app.checkers.retailers.base import get_json

    respx.get("https://api.example/p.json").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get("https://shop.example/p").mock(return_value=httpx.Response(200, text=load("button_enabled.html")))
    with fetcher.recording() as rec:
        assert await get_json("https://api.example/p.json") == {"ok": True}
        await fetcher.fetch_html("https://shop.example/p")
    assert [e["url"] for e in rec] == ["https://api.example/p.json", "https://shop.example/p"]
    assert rec[0]["body"] == '{"ok":true}' or rec[0]["body"].replace(" ", "") == '{"ok":true}'


def test_recording_nests_and_resets():
    with fetcher.recording() as outer:
        with fetcher.recording() as inner:
            assert fetcher._recording.get() is inner
        assert fetcher._recording.get() is outer
    assert fetcher._recording.get() is None


# ------------------------------------------------------------------ browser identity


def test_browser_identity_matches_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    ua, meta = fetcher.browser_identity("141.0.7390.37")
    assert "X11; Linux x86_64" in ua and "Chrome/141.0.0.0" in ua
    assert "Windows" not in ua and "Headless" not in ua
    assert meta["platform"] == "Linux" and meta["brands"][0]["version"] == "141"
    monkeypatch.setattr(sys, "platform", "darwin")
    ua, meta = fetcher.browser_identity("142.0.1.2")
    assert "Macintosh" in ua and "Chrome/142.0.0.0" in ua and meta["platform"] == "macOS"
    monkeypatch.setattr(sys, "platform", "win32")
    ua, meta = fetcher.browser_identity(None)
    assert "Windows NT 10.0" in ua and meta["platform"] == "Windows"


# ------------------------------------------------------------------ browser recording (real Chromium)


@pytest.fixture
def local_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: dict[str, dict] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            seen[self.path] = {k.lower(): v for k, v in self.headers.items()}
            body = (b'{"ok": true}' if self.path.startswith("/api") else
                    b"<html><head><title>Widget</title></head><body><button>Add to cart</button></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path.startswith("/api") else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    httpd.shutdown()
    httpd.server_close()


async def test_browser_fetches_are_recorded_with_consistent_identity(local_server, monkeypatch):
    base, seen = local_server
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    try:
        await fetcher._get_browser()
    except fetcher.FetchError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium unavailable: {e}")
    with fetcher.recording() as rec:
        res = await fetcher.browser_fetch(base + "/page")
        status, text, _ = await fetcher.browser_fetch_from_page(base + "/page2", base + "/api")
    assert res.via_browser and not res.queued and "Add to cart" in res.text
    assert status == 200 and '"ok"' in text
    assert [(e["url"], e["via"], e["status"]) for e in rec] == [
        (base + "/page", "browser", 200), (base + "/api", "browser", 200)]
    assert "Add to cart" in rec[0]["body"]
    hdrs = seen["/page"]
    if sys.platform.startswith("linux"):
        assert "Linux" in hdrs["user-agent"] and "Windows" not in hdrs["user-agent"]
        assert hdrs.get("sec-ch-ua-platform", '"Linux"') == '"Linux"'
    assert "Headless" not in hdrs["user-agent"]
