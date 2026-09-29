"""Regression tests for the fetch-layer review fixes: curl_cffi time budget + check/preview
deadlines, waiting-room false positives, Cloudflare's passive jsd script, and XHR-style
headers (None removes a default header) on both transports."""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

import app.checkers as checkers
from app.checkers import fetcher, generic
from app.checkers import retailers as retailers_pkg
from app.checkers.retailers.base import get_json

from .conftest import load


class _Headers:
    def __init__(self, items):
        self._items = items

    def multi_items(self):
        return list(self._items)


class _Resp:
    def __init__(self, url, status=200, body=b"{}", headers=(("Content-Type", "application/json"),)):
        self.url, self.status_code, self.content, self.headers = url, status, body, _Headers(headers)


class _FakeCurl:
    """Stands in for ``curl_cffi.requests`` and records session + per-request kwargs."""

    class exceptions:  # noqa: N801
        Timeout = TimeoutError

    def __init__(self):
        self.session_kwargs: dict = {}
        self.calls: list[tuple[str, dict, dict]] = []
        mod = self

        class AsyncSession:
            def __init__(self, **kw):
                mod.session_kwargs = kw

            async def get(self, url, headers=None, **kw):
                mod.calls.append((url, dict(headers or {}), kw))
                return _Resp(url)

            async def close(self):
                pass

        self.AsyncSession = AsyncSession


@pytest.fixture
def fake_curl(monkeypatch):
    mod = _FakeCurl()
    monkeypatch.setattr(fetcher, "_curl_requests", lambda: mod)
    monkeypatch.setenv("STOCKWATCHER_IMPERSONATE", "1")
    return mod


# ------------------------------------------------------------------ 2. time budgets


def test_curl_timeout_total_matches_httpx():
    # curl_cffi: (connect, read) -> CONNECTTIMEOUT=connect, TIMEOUT=connect+read
    connect, read = fetcher.CURL_TIMEOUT
    assert connect == fetcher.TIMEOUT.connect == 8.0
    assert connect + read == fetcher.TIMEOUT.read == 15.0


async def test_curl_session_gets_the_15s_budget(fake_curl):
    await fetcher.http_get("https://www.bestbuy.com/x")
    connect, read = fake_curl.session_kwargs["timeout"]
    assert connect == 8.0 and connect + read == 15.0
    assert fake_curl.calls[0][2] == {}  # no per-request override without a deadline


async def test_deadline_clamps_curl_and_httpx_timeouts(fake_curl):
    with fetcher.deadline(5.0):
        await fetcher.http_get("https://www.bestbuy.com/x")
    connect, read = fake_curl.calls[0][2]["timeout"]
    assert connect <= 5.0 and 4.0 < connect + read <= 5.1

    with respx.mock:
        route = respx.get("https://shop.example/p").mock(return_value=httpx.Response(200, text="ok"))
        with fetcher.deadline(4.0):
            await fetcher.http_get("https://shop.example/p")
        t = route.calls[0].request.extensions["timeout"]
        assert 3.0 < t["read"] <= 4.0 and t["connect"] <= 4.0


async def test_no_request_when_the_budget_is_used_up():
    with fetcher.deadline(0.2):
        with pytest.raises(fetcher.FetchError, match="time budget"):
            await fetcher.http_get("https://shop.example/p")


def test_browser_navigation_timeout_follows_the_deadline():
    assert fetcher._nav_timeout_ms("https://shop.example/p") == fetcher.BROWSER_NAV_TIMEOUT_MS
    with fetcher.deadline(20.0):
        assert 17_000 <= fetcher._nav_timeout_ms("https://shop.example/p") <= 18_000
    with fetcher.deadline(3.0):
        with pytest.raises(fetcher.FetchError, match="no time left"):
            fetcher._nav_timeout_ms("https://shop.example/p")


@respx.mock
async def test_fetch_html_makes_at_most_one_browser_attempt(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    url = "https://shop.example/p/1"
    calls = []

    async def challenged_browser(u):
        calls.append(u)
        return fetcher.FetchResult(url=u, status=403, via_browser=True,
                                   text="<html><head><title>Just a moment...</title></head></html>")

    monkeypatch.setattr(fetcher, "browser_fetch", challenged_browser)
    fetcher._mark_browser_host(url)
    respx.get(url).mock(return_value=httpx.Response(403, text="<html>denied</html>"))
    with pytest.raises(fetcher.FetchError):
        await fetcher.fetch_html(url)
    assert calls == [url]  # the preferred-host attempt; no second ~45 s browser run


def test_budgets_fit_inside_the_outer_timeouts():
    from app import scheduler
    from app.routers import items

    assert checkers.CHECK_TIMEOUT < scheduler.CHECK_TIMEOUT_SECONDS
    assert checkers.PREVIEW_TIMEOUT < items.PREVIEW_TIMEOUT
    # a plain request + one browser attempt + an adapter API call fit in a check
    assert fetcher.TIMEOUT.read + fetcher.BROWSER_NAV_TIMEOUT_MS / 1000 + fetcher.BROWSER_IDLE_TIMEOUT_MS / 1000 \
        + 10 + fetcher.TIMEOUT.read <= checkers.CHECK_TIMEOUT


async def test_run_check_sets_a_fetch_deadline(monkeypatch):
    seen = []

    async def fake_generic(url, cfg):
        seen.append(fetcher.time_left())
        return generic.analyze(load("jsonld_graph_instock.html"), url)

    monkeypatch.setattr(generic, "check_generic", fake_generic)
    r = await checkers.run_check("generic", "https://shop.example/p", None, None)
    assert r.status == "in_stock"
    assert seen[0] is not None and checkers.CHECK_TIMEOUT - 10 < seen[0] < checkers.CHECK_TIMEOUT
    assert fetcher.time_left() is None


@respx.mock
async def test_preview_timeout_keeps_partial_info(monkeypatch):
    url = "https://www.target.com/p/aurora-x1/-/A-12345678"
    respx.get(url).mock(return_value=httpx.Response(200, text=load("jsonld_graph_instock.html"),
                                                    headers={"Content-Type": "text/html"}))

    async def no_adapter(u, g, r):
        return None

    async def slow_generic(u, cfg):
        await fetcher.http_get(u)  # the page arrived...
        await asyncio.sleep(5)  # ...then the browser fallback hangs

    monkeypatch.setattr(retailers_pkg, "run_adapter", no_adapter)
    monkeypatch.setattr(generic, "check_generic", slow_generic)
    monkeypatch.setattr(checkers, "PREVIEW_TIMEOUT", 0.5)
    monkeypatch.setattr(checkers, "_DEADLINE_MARGIN", 0.0)
    monkeypatch.setattr(fetcher, "_MIN_REQUEST_TIME", 0.0)
    p = await checkers.preview_url(url)
    assert p["status"] == "error" and "timed out" in p["error"].lower() and p["error"] != "TimeoutError"
    assert p["retailer"] and p["retailer"]["key"] == "target"
    assert p["name"] == "Aurora X1 Espresso Machine" and p["price"] == "$1,099.00"
    assert p["image_url"].startswith("https://cdn.brewco.example/")


async def test_preview_timeout_without_a_page_still_names_the_retailer(monkeypatch):
    async def hang(u, g, r):
        await asyncio.sleep(5)

    monkeypatch.setattr(retailers_pkg, "run_adapter", hang)
    monkeypatch.setattr(checkers, "PREVIEW_TIMEOUT", 0.2)
    p = await checkers.preview_url("https://www.target.com/p/x/-/A-1")
    assert p["status"] == "error" and "timed out" in p["error"].lower()
    assert p["retailer"]["key"] == "target" and p["name"] is None


# ------------------------------------------------------------------ 3. waiting-room false positives

_QIT = ('<script src="//static.queue-it.net/script/queueclient.min.js"></script>'
        '<script data-queueit-c="acme" src="//static.queue-it.net/script/queueconfigloader.min.js"></script>')


@pytest.mark.parametrize("html", [
    # connector + JS tokens mentioning a queue: not a waiting room
    f"<html><head><title>Blender</title>{_QIT}<script>window.__tasks = {{queue: []}}; var waitTime = 0;"
    "/* you are in line */</script><style>.queue {{}}</style></head><body><h1>Blender</h1></body></html>",
    # sold-out copy is a product page even with queue wording
    f"<html><head>{_QIT}</head><body><h1>Sneaker X</h1><p>Sold out. Get in line for the next restock.</p></body></html>",
    f"<html><head>{_QIT}</head><body><h1>Sneaker X</h1><p>Out of stock &mdash; join the queue.</p></body></html>",
    # ProductGroup JSON-LD / microdata Product
    f"<html><head>{_QIT}<script type=\"application/ld+json\">{{\"@type\": \"ProductGroup\", \"name\": \"Sneaker X\"}}"
    "</script></head><body><p>Limited release: join the queue at 10am.</p></body></html>",
    f"<html><head>{_QIT}</head><body><div itemscope itemtype=\"https://schema.org/Product\"><h1>Sneaker X</h1>"
    "<p>Limited release: wait time may vary.</p></div></body></html>",
])
def test_queue_it_connector_on_product_pages_is_not_a_queue(html):
    assert not fetcher.looks_like_queue(html, "https://shop.example/p/x")


def test_real_waiting_rooms_still_detected():
    assert fetcher.looks_like_queue(load("platforms/queue_it.html"), "https://www.gamestop.com/p")
    assert fetcher.looks_like_queue(load("platforms/imperva_waiting_room.html"), "https://shop.example/p")
    assert fetcher.looks_like_queue(f"<html><head><title>Please wait</title>{_QIT}</head>"
                                    "<body><p>Your number in line: 5210</p></body></html>")
    assert fetcher.looks_like_queue('<html><meta http-equiv="refresh" content="0;url=/throttle/queue"></html>')


# ------------------------------------------------------------------ 4. Cloudflare passive jsd script

_JSD = ("<script>(function(){var a=document.createElement('script');"
        "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';document.head.appendChild(a);})();</script>")
_JSD_H = ("<script>(function(){var a=document.createElement('script');"
          "a.src='/cdn-cgi/challenge-platform/h/b/scripts/jsd/e0c90b6a3ed1/main.js';})();</script>")


@pytest.mark.parametrize("jsd", [_JSD, _JSD_H])
def test_cloudflare_jsd_script_is_not_a_challenge(jsd):
    page = f"<html><head><title>Widget</title></head><body><h1>Widget</h1><button>Add to cart</button>{jsd}</body></html>"
    assert not fetcher.looks_like_challenge(page)


def test_cloudflare_interstitials_still_detected():
    assert fetcher.looks_like_challenge("<html><head><title>Just a moment...</title></head><body></body></html>")
    chl = ('<html><head><title>shop.example</title></head><body><div id="cf-chl-widget-a1b2c"></div>'
           '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=8a1"></script></body></html>')
    assert fetcher.looks_like_challenge(chl)
    assert fetcher.looks_like_challenge(
        "<html><script>window._cf_chl_opt={cvId:'3'};</script>" + " " * 300_000 + "</html>")


def test_challenge_platform_path_alone_is_size_gated():
    big = ('<html><body><h1>Widget</h1><script src="/cdn-cgi/challenge-platform/h/g/orchestrate/x.js"></script>'
           + "<p>spec</p>" * 30_000 + "</body></html>")
    assert len(big) > fetcher._SMALL_PAGE
    assert not fetcher.looks_like_challenge(big)


# ------------------------------------------------------------------ 5. XHR-style headers for get_json

_NAV_ONLY = {"upgrade-insecure-requests", "sec-fetch-user"}


@respx.mock
async def test_get_json_drops_navigation_headers_over_httpx():
    route = respx.get("https://api.shop.example/v1/p").mock(return_value=httpx.Response(200, json={"ok": 1}))
    assert await get_json("https://api.shop.example/v1/p", headers={"Referer": "https://shop.example/"}) == {"ok": 1}
    sent = route.calls[0].request.headers
    assert not _NAV_ONLY & {k.lower() for k in sent.keys()}
    assert sent["accept"].startswith("application/json") and sent["sec-fetch-mode"] == "cors"
    assert sent["referer"] == "https://shop.example/" and sent["user-agent"] == fetcher.USER_AGENT


@respx.mock
async def test_http_get_none_header_removes_default_even_after_redirect():
    respx.get("https://shop.example/a").mock(return_value=httpx.Response(302, headers={"Location": "/b"}))
    final = respx.get("https://shop.example/b").mock(return_value=httpx.Response(200, text="ok"))
    await fetcher.http_get("https://shop.example/a", headers={"Upgrade-Insecure-Requests": None, "X-Test": "1"})
    sent = final.calls[0].request.headers
    assert "upgrade-insecure-requests" not in sent and sent["x-test"] == "1"
    assert sent["sec-fetch-user"] == "?1"  # other defaults stay


async def test_get_json_drops_navigation_headers_over_curl(fake_curl):
    await get_json("https://www.bestbuy.com/api/x")
    sent = fake_curl.calls[0][1]
    # None tells curl_cffi to send "Name:" -- which also removes its own Chrome default
    nav = {k.lower(): v for k, v in sent.items() if k.lower() in _NAV_ONLY}
    assert nav == {"upgrade-insecure-requests": None, "sec-fetch-user": None}
    assert sent["Accept"].startswith("application/json") and sent["Sec-Fetch-Dest"] == "empty"


def test_curl_headers_override_after_removal():
    h = fetcher._curl_headers({"sec-fetch-user": None, "Referer": "r"})
    assert h["sec-fetch-user"] is None and "Sec-Fetch-User" not in h and h["Referer"] == "r"
    assert h["Upgrade-Insecure-Requests"] == "1"
