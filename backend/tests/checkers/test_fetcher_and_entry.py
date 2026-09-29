"""fetch_html fallback rules and the public entry points in app.checkers."""
from __future__ import annotations

import httpx
import pytest
import respx

import app.checkers as checkers
from app.checkers import apple, fetcher, generic
from app.checkers.base import CheckResult

from .conftest import load

URL = "https://shop.example/products/thing"


def _fake_browser(html: str, calls: list, status: int = 200):
    async def fake(url):
        calls.append(url)
        return fetcher.FetchResult(url=url, status=status, text=html, via_browser=True)

    return fake


# ------------------------------------------------------------------ fetch_html

@respx.mock
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_blocked_status_falls_back_to_browser(monkeypatch, status):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    respx.get(URL).mock(return_value=httpx.Response(status, text="denied"))
    calls: list = []
    monkeypatch.setattr(fetcher, "browser_fetch", _fake_browser(load("button_enabled.html"), calls))
    res = await fetcher.fetch_html(URL)
    assert calls == [URL] and res.via_browser
    # host is remembered: next fetch goes straight to the browser
    route = respx.get(URL + "?2").mock(return_value=httpx.Response(200, text=load("button_enabled.html")))
    await fetcher.fetch_html(URL + "?2")
    assert len(calls) == 2 and not route.called


@respx.mock
@pytest.mark.parametrize("exc", [httpx.ReadTimeout("stalled"), httpx.ConnectError("reset")])
async def test_timeout_or_network_error_falls_back_to_browser(monkeypatch, exc):
    # Akamai-style protection (e.g. Best Buy) stalls plain clients instead of returning 403.
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    respx.get(URL).mock(side_effect=exc)
    calls: list = []
    monkeypatch.setattr(fetcher, "browser_fetch", _fake_browser(load("button_enabled.html"), calls))
    res = await fetcher.fetch_html(URL)
    assert calls == [URL] and res.via_browser


@respx.mock
async def test_timeout_without_browser_still_errors(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "false")
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("stalled"))
    with pytest.raises(fetcher.FetchError, match="Timed out"):
        await fetcher.fetch_html(URL)


@respx.mock
async def test_timeout_and_browser_failure_reports_both(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("stalled"))

    async def fail(url):
        raise fetcher.FetchError("Browser fetch failed: TimeoutError")

    monkeypatch.setattr(fetcher, "browser_fetch", fail)
    with pytest.raises(fetcher.FetchError, match="Timed out.*browser retry also failed"):
        await fetcher.fetch_html(URL)


@respx.mock
async def test_challenge_page_falls_back_to_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    respx.get(URL).mock(return_value=httpx.Response(200, text=load("cloudflare_challenge.html")))
    calls: list = []
    monkeypatch.setattr(fetcher, "browser_fetch", _fake_browser(load("jsonld_graph_instock.html"), calls))
    res = await fetcher.fetch_html(URL)
    assert calls == [URL]
    assert "Aurora" in res.text


@respx.mock
async def test_no_product_signals_falls_back_to_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body><div id='app'></div></body></html>"))
    calls: list = []
    monkeypatch.setattr(fetcher, "browser_fetch", _fake_browser(load("button_enabled.html"), calls))
    r = await generic.check_generic(URL, None)
    assert calls == [URL]
    assert r.status == "in_stock" and r.detail["fetched_via"] == "browser"


@respx.mock
async def test_page_with_signals_does_not_launch_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    respx.get(URL).mock(return_value=httpx.Response(200, text=load("jsonld_graph_instock.html")))
    calls: list = []
    monkeypatch.setattr(fetcher, "browser_fetch", _fake_browser("", calls))
    await fetcher.fetch_html(URL)
    assert calls == []


@respx.mock
async def test_browser_disabled_blocked_raises_fetch_error(monkeypatch):
    respx.get(URL).mock(return_value=httpx.Response(403, text="denied"))

    async def boom(url):
        raise AssertionError("browser must not be used")

    monkeypatch.setattr(fetcher, "browser_fetch", boom)
    with pytest.raises(fetcher.FetchError) as ei:
        await fetcher.fetch_html(URL)
    assert ei.value.status == 403


@respx.mock
async def test_challenge_without_browser_raises(monkeypatch):
    respx.get(URL).mock(return_value=httpx.Response(200, text=load("cloudflare_challenge.html")))
    with pytest.raises(fetcher.FetchError, match="bot protection"):
        await fetcher.fetch_html(URL)


@respx.mock
async def test_browser_fallback_failure_surfaces_http_error(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    respx.get(URL).mock(return_value=httpx.Response(503, text="busy"))

    async def fail(url):
        raise fetcher.FetchError("Could not start browser: nope")

    monkeypatch.setattr(fetcher, "browser_fetch", fail)
    with pytest.raises(fetcher.FetchError, match="HTTP 503"):
        await fetcher.fetch_html(URL)


@respx.mock
async def test_network_error_becomes_fetch_error():
    respx.get(URL).mock(side_effect=httpx.ConnectError("dns fail"))
    with pytest.raises(fetcher.FetchError, match="Network error"):
        await fetcher.fetch_html(URL)


def test_challenge_and_signal_sniffers():
    assert fetcher.looks_like_challenge(load("cloudflare_challenge.html"))
    assert not fetcher.looks_like_challenge(load("jsonld_graph_instock.html"))
    assert fetcher.has_product_signals(load("button_disabled.html"))
    assert not fetcher.has_product_signals(load("no_signals.html"))


# ------------------------------------------------------------------ run_check

@respx.mock
async def test_run_check_generic():
    respx.get(URL).mock(return_value=httpx.Response(200, text=load("button_disabled.html")))
    r = await checkers.run_check("generic", URL, {"mode": "auto"}, None)
    assert isinstance(r, CheckResult) and r.status == "out_of_stock"


@respx.mock
async def test_run_check_wraps_site_errors():
    respx.get(URL).mock(return_value=httpx.Response(500, text="oops"))
    r = await checkers.run_check("generic", URL, None, None)
    assert r.status == "error" and r.status_text == "Check failed"
    assert "500" in r.error


@respx.mock
@pytest.mark.parametrize("code", [404, 410])
async def test_run_check_names_dead_product_links(code):
    # a 404/410 from the product's own site is a stale link, not a generic failure
    respx.get(URL).mock(return_value=httpx.Response(code, text="not found"))
    r = await checkers.run_check("generic", URL, None, None)
    assert r.status == "error" and r.status_text == f"Product page not found (HTTP {code})"
    assert "update the link" in r.error and r.detail["dead_link"] is True


async def test_run_check_wraps_unexpected_exceptions(monkeypatch):
    async def crash(url, cfg):
        raise ValueError("kaboom")

    monkeypatch.setattr(generic, "check_generic", crash)
    r = await checkers.run_check("generic", URL, None, None)
    assert r.status == "error" and "kaboom" in r.error


async def test_run_check_dispatches_apple(monkeypatch):
    seen = []

    async def fake_apple(url, cfg):
        seen.append((url, cfg))
        return CheckResult(status="out_of_stock", status_text="Not available nearby")

    monkeypatch.setattr(apple, "check_apple", fake_apple)
    r = await checkers.run_check("apple", "https://www.apple.com/shop/buy-iphone", None, {"parts": [], "zip": "1"})
    assert seen and r.status == "out_of_stock"


async def test_run_check_timeout(monkeypatch):
    import asyncio

    async def slow(url, cfg):
        await asyncio.sleep(5)

    monkeypatch.setattr(generic, "check_generic", slow)
    monkeypatch.setattr(checkers, "CHECK_TIMEOUT", 0.05)
    r = await checkers.run_check("generic", URL, None, None)
    assert r.status == "error" and "timed out" in r.error


# ------------------------------------------------------------------ preview / resolve

@respx.mock
async def test_preview_generic():
    respx.get(URL).mock(return_value=httpx.Response(200, text=load("jsonld_graph_instock.html")))
    p = await checkers.preview_url(URL)
    assert p == {"name": "Aurora X1 Espresso Machine",
                 "image_url": "https://cdn.brewco.example/products/aurora-x1_1200x.jpg?v=1712",
                 "price": "$1,099.00", "status": "in_stock", "is_apple": False, "retailer": None,
                 "status_text": "In stock", "seller": None, "third_party": None, "adapter": "generic",
                 "fetched_via": "http", "cart_url": None, "signals": p["signals"], "queued": False, "blocked": False}
    assert p["signals"] and all(isinstance(x, str) for x in p["signals"]) and len(p["signals"]) <= 8


@respx.mock
async def test_preview_error_does_not_raise():
    respx.get(URL).mock(return_value=httpx.Response(500))
    p = await checkers.preview_url(URL)
    assert p["status"] == "error" and p["is_apple"] is False and "500" in p["error"]


@respx.mock
async def test_preview_apple_uses_resolve():
    respx.get("https://www.apple.com/shop/buy-iphone").mock(return_value=httpx.Response(200, text="ok"))
    url = "https://www.apple.com/shop/product/MXP63AM/A/airpods-4"
    respx.get(url).mock(return_value=httpx.Response(200, text=load("apple_airpods.html")))
    p = await checkers.preview_url(url)
    assert p == {"name": "AirPods 4", "image_url": "https://www.apple.com/is/airpods-4-select-202409?wid=1200",
                 "price": "$129.00", "status": "unknown", "is_apple": True, "retailer": None}


async def test_resolve_apple_never_raises(monkeypatch):
    async def crash(url):
        raise RuntimeError("x")

    monkeypatch.setattr(apple, "resolve_apple", crash)
    info = await checkers.resolve_apple("https://www.apple.com/shop/product/MXP63AM/A/airpods-4")
    assert info["variants"][0]["part_number"] == "MXP63AM/A"


async def test_shutdown_is_idempotent():
    await checkers.shutdown()
    await checkers.shutdown()
