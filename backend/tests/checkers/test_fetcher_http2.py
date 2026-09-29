"""Akamai's HTTP/2 stream resets (curl 92 / net::ERR_HTTP2_PROTOCOL_ERROR): one retry over HTTP/1.1
(curl_cffi ``http_version``, httpx without h2, a second browser with ``--disable-http2``), remembered per
host; and raw transport errors shown to people as short messages."""
from __future__ import annotations

import httpx
import pytest
import respx

from app import checkers
from app.checkers import fetcher

from .test_browser_engine import _FakeEngine, _use_engine
from .test_fetcher_hardening import _FakeCurlResponse

RESET = ("curl: (92) HTTP/2 stream 1 reset by server (error 0x2 INTERNAL_ERROR). See "
         "https://curl.se/libcurl/c/libcurl-errors.html first for more details.")
BB = "https://www.bestbuy.com/product/canon-powershot-g7-x-mark-iii-20-1-megapixel-digital-camera-black/J7C86S93T6"


class _CurlH2Module:
    """``curl_cffi.requests`` stand-in whose HTTP/2 requests get reset."""

    class exceptions:  # noqa: N801
        Timeout = TimeoutError

    def __init__(self, h1_ok: bool = True):
        self.calls: list[dict] = []
        mod = self

        class AsyncSession:
            def __init__(self, **kw):
                pass

            async def get(self, url, headers=None, **kw):
                mod.calls.append(kw)
                if "http_version" not in kw or not h1_ok:
                    raise ConnectionError(RESET)
                return _FakeCurlResponse(url, 200, b"<html>ok</html>")

            async def close(self):
                pass

        self.AsyncSession = AsyncSession


@pytest.fixture
def curl_h2(monkeypatch):
    def install(**kw):
        mod = _CurlH2Module(**kw)
        monkeypatch.setattr(fetcher, "_curl_requests", lambda: mod)
        monkeypatch.setenv("STOCKWATCHER_IMPERSONATE", "1")
        return mod
    return install


def test_http2_reset_detection():
    assert fetcher.is_http2_reset(RESET)
    assert fetcher.is_http2_reset("Browser fetch failed: Error: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://x/")
    assert fetcher.is_http2_reset("RemoteProtocolError: <StreamReset stream_id:1, error_code:ErrorCodes.INTERNAL_ERROR>")
    assert not fetcher.is_http2_reset("curl: (35) Recv failure: Connection reset by peer")
    assert not fetcher.is_http2_reset("HTTP 403 from www.bestbuy.com")


async def test_curl_http2_reset_retries_once_over_http11_and_remembers_host(curl_h2):
    mod = curl_h2()
    resp = await fetcher.http_get("https://www.bestbuy.com/x")
    assert resp.status_code == 200
    assert [("http_version" in c) for c in mod.calls] == [False, True]
    assert int(mod.calls[1]["http_version"]) == 2  # CURL_HTTP_VERSION_1_1
    # the host now goes straight to HTTP/1.1
    await fetcher.http_get("https://www.bestbuy.com/y")
    assert [("http_version" in c) for c in mod.calls] == [False, True, True]


async def test_curl_http11_retry_failing_raises_the_reset(curl_h2):
    mod = curl_h2(h1_ok=False)
    with pytest.raises(fetcher.FetchError, match=r"curl: \(92\)"):
        await fetcher.http_get("https://www.bestbuy.com/x")
    assert len(mod.calls) == 2  # exactly one retry
    assert "www.bestbuy.com" not in fetcher._h1_hosts


@respx.mock
async def test_httpx_stream_reset_retries_without_http2(monkeypatch):
    monkeypatch.setattr(fetcher, "_HTTP2", True)
    seen: list[str] = []

    def side(request):
        client = "h1" if fetcher._st().client_h1 is not None else "h2"
        seen.append(client)
        if client == "h2":
            raise httpx.RemoteProtocolError("<StreamReset stream_id:1, error_code:ErrorCodes.INTERNAL_ERROR>")
        return httpx.Response(200, text="ok")

    respx.get("https://www.homedepot.com/p/1").mock(side_effect=side)
    assert (await fetcher.http_get("https://www.homedepot.com/p/1")).text == "ok"
    assert seen == ["h2", "h1"] and "www.homedepot.com" in fetcher._h1_hosts


async def test_browser_http2_protocol_error_retries_in_http11_browser(monkeypatch):
    calls: list[tuple[str, bool]] = []

    async def once(url, capture, *, h1):
        calls.append((url, h1))
        if not h1:
            raise fetcher.FetchError(f"Browser fetch failed: Error: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at {url}")
        return fetcher.FetchResult(url=url, status=200, text="<html>page</html>", via_browser=True)

    monkeypatch.setattr(fetcher, "_browser_fetch_once", once)
    res = await fetcher.browser_fetch(BB)
    assert res.text == "<html>page</html>" and calls == [(BB, False), (BB, True)]
    await fetcher.browser_fetch(BB + "?x=1")  # remembered: straight to the HTTP/1.1 browser
    assert calls[-1] == (BB + "?x=1", True) and len(calls) == 3

    async def always_h2_error(url, capture, *, h1):
        calls.append((url, h1))
        raise fetcher.FetchError("Browser fetch failed: Error: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR")

    monkeypatch.setattr(fetcher, "_browser_fetch_once", always_h2_error)
    with pytest.raises(fetcher.FetchError, match="ERR_HTTP2_PROTOCOL_ERROR"):
        await fetcher.browser_fetch("https://www.homedepot.com/p/1")
    assert calls[-2:] == [("https://www.homedepot.com/p/1", False), ("https://www.homedepot.com/p/1", True)]


async def test_other_browser_errors_are_not_retried(monkeypatch):
    calls: list[bool] = []

    async def once(url, capture, *, h1):
        calls.append(h1)
        raise fetcher.FetchError("Browser fetch failed: TimeoutError: Timeout 30000ms exceeded")

    monkeypatch.setattr(fetcher, "_browser_fetch_once", once)
    with pytest.raises(fetcher.FetchError):
        await fetcher.browser_fetch(BB)
    assert calls == [False]


async def test_http11_browser_is_a_separate_lazy_context(monkeypatch, tmp_path):
    fake = _FakeEngine()
    _use_engine(monkeypatch, fake)
    monkeypatch.setenv("BROWSER_MODE", "headless")
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(tmp_path))
    main = await fetcher._get_browser()
    assert len(fake.launches) == 1 and "--disable-http2" not in fake.launches[0]["args"]
    h1 = await fetcher._get_browser(h1=True)
    assert h1 is not main and len(fake.launches) == 2
    kw = fake.launches[1]
    assert "--disable-http2" in kw["args"] and kw["user_data_dir"] == str(tmp_path / "chromium-h1")
    assert await fetcher._get_browser(h1=True) is h1 and len(fake.launches) == 2
    await fetcher.shutdown()
    assert main.closed and h1.closed


@pytest.mark.parametrize("raw,short", [
    ("Network error: HTTPError: Failed to perform, curl: (92) HTTP/2 stream 1 reset by server (error 0x2 "
     "INTERNAL_ERROR) (browser retry also failed: Browser fetch failed: Error: Page.goto: "
     "net::ERR_HTTP2_PROTOCOL_ERROR at " + BB + "?intl=nosplash)",
     "Best Buy refused the connection (bot protection) — retrying later"),
    ("Timed out fetching www.bestbuy.com", "Best Buy didn't respond in time — retrying later"),
    ("Network error: ConnectError: [Errno -2] Name or service not known",
     "Couldn't find www.bestbuy.com (DNS lookup failed) — check the link"),
    ("HTTP 403 from www.bestbuy.com", None),
    ("Blocked by bot protection on www.bestbuy.com", None),
])
def test_friendly_transport_errors(raw, short):
    assert checkers.friendly_error(raw, BB) == short


async def test_run_check_and_preview_show_short_message_keep_raw(monkeypatch):
    raw = ("Network error: HTTPError: Failed to perform, curl: (92) HTTP/2 stream 1 reset by server (error 0x2 "
           "INTERNAL_ERROR) (browser retry also failed: Browser fetch failed: Error: Page.goto: "
           "net::ERR_HTTP2_PROTOCOL_ERROR at " + BB + "?intl=nosplash)")

    async def fail(*a, **k):
        raise fetcher.FetchError(raw)

    monkeypatch.setattr(fetcher, "fetch_html", fail)
    res = await checkers.run_check("site", BB, None, None)
    assert res.status == "error" and res.status_text == res.error == \
        "Best Buy refused the connection (bot protection) — retrying later"
    assert res.detail["raw_error"] == raw
    p = await checkers.preview_url(BB)
    assert p["error"] == "Best Buy refused the connection (bot protection) — retrying later"
    assert p["error_detail"] == raw


async def test_preview_blocked_and_via_keys(monkeypatch):
    async def blocked(*a, **k):
        raise fetcher.FetchError("Blocked by bot protection on www.adorama.com (DataDome)")

    monkeypatch.setattr(fetcher, "fetch_html", blocked)
    p = await checkers.preview_url("https://www.adorama.com/ifjx100vs.html")
    assert p["status"] == "error" and p["blocked"] is True and p["queued"] is False and p["signals"] == []
