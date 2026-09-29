"""Real headless-Chromium path against a local HTTP server (skipped if Chromium can't start)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.checkers import fetcher, generic

SPA = """<!doctype html><html><head><title>JS Widget - Local Shop</title></head>
<body><div id="app">Loading…</div>
<script>
setTimeout(function () {
  var t = 'Add to ' + 'cart';
  document.getElementById('app').innerHTML = '<main><h1>JS Widget</h1><button id="buy">' + t + '</button></main>';
  var s = document.createElement('script');
  s.type = 'application/' + 'ld+json';
  s.textContent = JSON.stringify({"@context": "https://schema.org", "@type": "Product", "name": "JS Widget",
    "offers": {"@type": "Offer", "price": "42.00", "priceCurrency": "USD", "availability": "https://schema.org/" + "In" + "Stock"}});
  document.head.appendChild(s);
}, 300);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api.json"):
            body = json.dumps({"ok": True, "cookie": self.headers.get("Cookie", "")}).encode()
            ctype = "application/json"
        else:
            body = SPA.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if self.path.startswith("/spa"):
            self.send_header("Set-Cookie", "sess=abc; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
async def browser_on(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    try:
        await fetcher._get_browser()
    except fetcher.FetchError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium unavailable: {e}")
    yield
    await fetcher.shutdown()


async def test_js_rendered_page_uses_browser_fallback(server, browser_on):
    url = server + "/spa"
    r = await generic.check_generic(url, {"mode": "auto"})
    assert r.detail["fetched_via"] == "browser"
    assert r.status == "in_stock"
    assert r.detail["matched"] == "json-ld: InStock"
    assert r.price == "$42.00"
    assert r.title == "JS Widget"


async def test_render_js_selector_mode(server, browser_on):
    r = await generic.check_generic(server + "/spa", {"mode": "selector", "selector": "#buy", "render_js": True})
    assert r.status == "in_stock"


async def test_fetch_from_page_context_shares_cookies(server, browser_on):
    status, text, cookies = await fetcher.browser_fetch_from_page(server + "/spa", server + "/api.json")
    assert status == 200
    data = json.loads(text)
    assert data["ok"] is True and "sess=abc" in data["cookie"]
    assert any(c["name"] == "sess" for c in cookies)


async def test_browser_is_reused(server, browser_on):
    b1 = await fetcher._get_browser()
    await fetcher.browser_fetch(server + "/spa")
    b2 = await fetcher._get_browser()
    assert b1 is b2
