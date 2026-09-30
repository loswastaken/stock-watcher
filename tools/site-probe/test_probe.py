"""Tests for the site probe. No live network: run_check / preview_url are replaced with fakes.

Run from the backend folder:  python -m pytest ../tools/site-probe -q
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import http.client
import json
import threading
import zipfile
from pathlib import Path

import httpx
import pytest

import probe
from app.checkers import fetcher
from app.checkers.base import Availability, CheckResult
from app.checkers.retailers import registry

import app.checkers as checkers


# --------------------------------------------------------------------------- sites.json


def test_sites_json_covers_every_registry_retailer():
    sites = probe.load_sites(probe.DEFAULT_SITES)
    keys = {r.key for r in registry.RETAILERS}
    assert keys - set(sites) == set(), "stores without sample URLs"
    assert set(sites) - keys == set(), "sample URLs for unknown stores"
    for key, entries in sites.items():
        assert entries, key
        for e in entries:
            assert e["url"].startswith("https://"), e
            m = registry.match_retailer(e["url"])
            assert m is not None and m.key == key, f"{e['url']} does not match {key}"


def test_sites_json_is_marked_as_samples():
    raw = json.loads(probe.DEFAULT_SITES.read_text(encoding="utf-8"))
    assert "SAMPLE" in raw["_README"]


def test_select_entries_rejects_unknown_keys():
    sites = probe.load_sites(probe.DEFAULT_SITES)
    assert {e["key"] for e in probe.select_entries(sites, ["target", "bestbuy"])} == {"target", "bestbuy"}
    with pytest.raises(probe.ProbeError):
        probe.select_entries(sites, ["nope"])


# --------------------------------------------------------------------------- scrubbing


def test_scrub_headers_strips_cookies_and_auth_case_insensitively():
    h = probe.scrub_headers({"Set-Cookie": "sid=SECRET1", "cookie": "a=SECRET2", "AUTHORIZATION": "Bearer SECRET3",
                             "Proxy-Authorization": "Basic SECRET4", "content-type": "text/html"})
    assert "SECRET" not in json.dumps(h)
    assert h["content-type"] == "text/html"
    assert h["Set-Cookie"] == probe.STRIPPED
    # list-of-pairs form (multi-valued headers)
    assert "SECRET" not in json.dumps(probe.scrub_headers([("set-cookie", "x=SECRET"), ("set-cookie", "y=SECRET")]))


def test_scrub_url_and_env_secrets(monkeypatch):
    monkeypatch.setenv("BESTBUY_API_KEY", "bbkey1234567")
    url = probe.scrub_url("https://api.bestbuy.com/v1/products/1.json?apiKey=bbkey1234567&show=sku")
    assert "bbkey1234567" not in url and "show=sku" in url
    assert probe.scrub_text("x bbkey1234567 y") == f"x {probe.REDACTED} y"
    # Target's public key param ("key") is kept so recordings stay reproducible
    assert "key=abc" in probe.scrub_url("https://redsky.target.com/x?key=abc&tcin=1")


def test_scrub_entry_strips_request_and_response_headers():
    e = probe.scrub_entry({"url": "https://x.test/?token=zzz", "headers": {"set-cookie": "a"},
                           "request_headers": {"Cookie": "b"}, "body": "hello"})
    assert "body" not in e
    assert e["headers"]["set-cookie"] == probe.STRIPPED
    assert e["request_headers"]["Cookie"] == probe.STRIPPED
    assert "zzz" not in e["url"]


# --------------------------------------------------------------------------- verdicts


@pytest.mark.parametrize(
    "status,text,error,detail,entries,expected",
    [
        ("in_stock", "In stock", None, {}, [], "OK"),
        ("out_of_stock", "Sold out", None, {}, [], "OK"),
        ("unknown", "Waiting room active — drop may be live", None, {"queue": True}, [], "QUEUE"),
        ("error", "Check failed", "Blocked by bot protection on www.bestbuy.com", {}, [], "BLOCKED"),
        ("unknown", "Unknown", None, {}, [{"status": 403, "url": "https://x.test/"}], "BLOCKED"),
        ("unknown", "Unknown", None, {}, [{"status": 200, "url": "https://x.test/",
                                           "body": "<title>Just a moment...</title>"}], "BLOCKED"),
        ("error", "Check failed", "Timed out fetching www.x.test", {}, [], "FAIL"),
        ("unknown", "Unknown", None, {}, [{"status": 200, "body": "<html>fine</html>"}], "FAIL"),
        # stale links are STALE even when a 403 / challenge page was met on the way (2026-09-29: eBay's
        # "Listing not found" after a challenge page and B&H's "different product" were reported BLOCKED)
        ("error", "Listing not found", "Listing not found", {},
         [{"status": 200, "url": "https://www.ebay.com/itm/1", "body": "<title>Just a moment...</title>"}], "STALE"),
        ("error", "Product page not found (HTTP 404)", "Product page not found (HTTP 404) — update the link",
         {"dead_link": True}, [{"status": 403, "url": "https://x.test/"}], "STALE"),
        ("error", "This link now shows a different product (Apple 32GB iPod touch) — update the link", None, {}, [],
         "STALE"),
        ("error", "Product page redirects to the homepage — the link may be stale", None, {}, [], "STALE"),
        ("error", "Walmart redirects this item to a different product (PS5) — update the link", None, {}, [],
         "STALE"),
        # info pages aren't failures, even with api.store.nvidia.com's Akamai 403 in the log
        ("unknown", "Info page — watch the NVIDIA Marketplace listing instead", None, {"info_only": True},
         [{"status": 403, "url": "https://api.store.nvidia.com/partner/v1/feinventory"}], "INFO"),
        ("unknown", "No direct sales on this page", None, {"info_only": True}, [], "INFO"),
    ],
)
def test_classify(status, text, error, detail, entries, expected):
    verdict, reason = probe.classify(status, text, error, detail, entries, fetcher.looks_like_challenge)
    assert verdict == expected
    assert reason


# --------------------------------------------------------------------------- fake checks


FAKE_RESULTS = {
    "target": CheckResult(status="in_stock", status_text="In stock", price="$449.99",
                          available=[Availability(key="stock", label="In stock")],
                          detail={"adapter": "target", "retailer": "target", "seller": "Target", "third_party": False,
                                  "cart_url": None, "signals": ["fulfillment: IN_STOCK"]}),
    "bestbuy": CheckResult(status="error", status_text="Check failed", error="Blocked by bot protection on www.bestbuy.com"),
    "psdirect": CheckResult(status="unknown", status_text="Waiting room active — drop may be live",
                            detail={"adapter": "games", "queue": True}),
    "lego": CheckResult(status="error", status_text="Check failed", error="Timed out fetching www.lego.com"),
    "walmart": CheckResult(status="out_of_stock", status_text="Out of stock", price="$699.00",
                           detail={"adapter": "walmart", "fetched_via": "http"}),
}


_fake_log: contextvars.ContextVar[list | None] = contextvars.ContextVar("fake_log", default=None)


@contextlib.contextmanager
def _fake_recording():
    """Stand-in for fetcher.recording() with the contract's shape (independent of its internals)."""
    log: list = []
    token = _fake_log.set(log)
    try:
        yield log
    finally:
        _fake_log.reset(token)


@pytest.fixture
def fake_backend(monkeypatch):
    """Replace run_check / preview_url / recording; each fake check 'fetches' one page."""
    calls = []
    monkeypatch.setattr(fetcher, "recording", _fake_recording, raising=False)

    async def fake_run_check(kind, url, generic_config, apple_config, retailer_config=None):
        calls.append((url, retailer_config))
        key = registry.match_retailer(url).key
        _fake_log.get().append({"method": "GET", "url": url, "status": 200, "via": "http", "elapsed_ms": 5,
                                "headers": {"Content-Type": "text/html", "Set-Cookie": "session=TOPSECRET"},
                                "body": f"<html>{key} page</html>"})
        res = FAKE_RESULTS[key]
        return CheckResult(**{**res.__dict__, "detail": dict(res.detail)})

    async def fake_preview(url):
        return {"name": "Thing", "image_url": None, "price": "$1.00", "status": "in_stock", "is_apple": False}

    monkeypatch.setattr(checkers, "run_check", fake_run_check)
    monkeypatch.setattr(checkers, "preview_url", fake_preview)
    return calls


URLS = {
    "target": "https://www.target.com/p/x/-/A-94693225",
    "bestbuy": "https://www.bestbuy.com/site/x/6603968.p?skuId=6603968",
    "psdirect": "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console.3009726",
    "lego": "https://www.lego.com/en-us/product/millennium-falcon-75192",
    "walmart": "https://www.walmart.com/ip/x/5113183757",
}


def test_probe_one_saves_scrubbed_bundle(tmp_path, fake_backend):
    s = asyncio.run(probe.probe_one(URLS["target"], {"zip": "60601"}, out_root=tmp_path))
    assert s["retailer"] == "target" and s["adapter"] == "target" and s["verdict"] == "OK"
    assert s["price"] == "$449.99" and s["seller"] == "Target" and s["recording"] == "fetcher.recording"
    assert s["requests"] == 1 and s["preview"]["name"] == "Thing"
    d = Path(s["bundle_path"])
    assert d.parent == tmp_path and d.name.endswith("-target")
    doc = json.loads((d / "result.json").read_text())
    assert doc["summary"]["status"] == "in_stock" and doc["result"]["price"] == "$449.99"
    index = json.loads((d / "requests" / "index.json").read_text())
    assert len(index) == 1  # the fake preview does not fetch
    assert index[0]["phase"] == "check" and index[0]["status"] == 200
    assert (d / index[0]["body_file"]).read_text() == "<html>target page</html>"
    assert index[0]["body_file"].endswith(".html")
    assert "TOPSECRET" not in json.dumps(index)
    assert fake_backend == [(URLS["target"], {"zip": "60601"})]


def test_probe_one_without_recording_hook_falls_back_to_httpx(tmp_path, monkeypatch, capsys):
    monkeypatch.delattr(fetcher, "recording", raising=False)
    monkeypatch.setattr(probe, "_warned_no_hook", False)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/json", "set-cookie": "k=SECRETV"},
                              json={"ok": True})

    async def fake_run_check(kind, url, generic_config, apple_config, retailer_config=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await c.get("https://api.example.test/stock")
        return CheckResult(status="in_stock", status_text="In stock", detail={"adapter": "generic"})

    monkeypatch.setattr(checkers, "run_check", fake_run_check)
    s = asyncio.run(probe.probe_one("https://shop.example.test/p/1", preview=False, out_root=tmp_path))
    assert s["recording"] == "httpx-fallback" and s["requests"] == 1
    assert "fetcher.recording() hook" in capsys.readouterr().err
    index = json.loads((Path(s["bundle_path"]) / "requests" / "index.json").read_text())
    assert index[0]["url"] == "https://api.example.test/stock" and index[0]["body_file"].endswith(".json")
    assert "SECRETV" not in json.dumps(index)


def test_sweep_report_from_fake_results(tmp_path, fake_backend):
    entries = [{"key": k, "url": u, "retailer_config": {}, "note": None} for k, u in URLS.items()]
    entries[0]["retailer_config"] = {"fulfillment": "pickup"}
    seen = []
    results = asyncio.run(probe.run_entries(entries, concurrency=3, base_config={"zip": "60601"}, out_root=tmp_path,
                                            on_result=lambda i, s: seen.append(i)))
    assert sorted(seen) == list(range(len(URLS)))
    by_store = {r["store"]: r for r in results}
    assert {k: r["verdict"] for k, r in by_store.items()} == {
        "target": "OK", "bestbuy": "BLOCKED", "psdirect": "QUEUE", "lego": "FAIL", "walmart": "OK"}
    # per-entry retailer_config wins over / merges with the CLI defaults
    assert (URLS["target"], {"zip": "60601", "fulfillment": "pickup"}) in fake_backend

    md, js = probe.write_report(results, tmp_path)
    text = md.read_text()
    assert "| Store | URL | Status | Adapter | Price | Verdict | Error |" in text
    assert "**5 checks:** 2 OK, 0 INFO, 0 STALE, 1 FAIL, 1 BLOCKED, 1 QUEUE" in text
    row = next(line for line in text.splitlines() if line.startswith("| target |"))
    assert "$449.99" in row and "**OK**" in row and "| target |" in row
    assert "Blocked by bot protection" in text
    report = json.loads(js.read_text())
    assert report["counts"] == {"OK": 2, "INFO": 0, "STALE": 0, "FAIL": 1, "BLOCKED": 1, "QUEUE": 1}
    assert len(report["results"]) == 5
    assert all("bundle_path" not in r for r in report["results"])  # no local paths in the shared report


def test_run_entries_survives_a_crashing_probe(tmp_path, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("kaput")

    monkeypatch.setattr(checkers, "run_check", boom)
    res = asyncio.run(probe.run_entries([{"key": "lego", "url": URLS["lego"]}], out_root=tmp_path))
    assert res[0]["verdict"] == "FAIL" and "kaput" in res[0]["error"]


# --------------------------------------------------------------------------- bundle + fixture


def _make_output(tmp_path: Path, fake_backend) -> Path:
    out = tmp_path / "probe-output"
    entries = [{"key": k, "url": URLS[k]} for k in ("target", "walmart")]
    results = asyncio.run(probe.run_entries(entries, out_root=out))
    probe.write_report(results, out)
    return out


def test_bundle_creates_scrubbed_zip(tmp_path, fake_backend):
    out = _make_output(tmp_path, fake_backend)
    # a recording written by an older probe, with raw cookies, must still be scrubbed on the way out
    old = out / "20250101-000000-target" / "requests"
    old.mkdir(parents=True)
    (old / "index.json").write_text(json.dumps([{"url": "https://x.test/", "headers": {"Set-Cookie": "sid=OLDSECRET"},
                                                 "request_headers": {"Authorization": "Bearer OLDTOKEN"}}]))
    zpath, included, skipped = probe.make_bundle(out, tmp_path, cap_mb=5, date=probe.dt.date(2026, 9, 29))
    assert zpath.name == "probe-results-2026-09-29.zip" and not skipped
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        assert "probe-output/report.md" in names and "probe-output/report.json" in names and "MANIFEST.txt" in names
        assert any(n.endswith("-target/result.json") for n in names)
        assert any(n.endswith(".html") for n in names)
        blob = b"".join(zf.read(n) for n in names)
    for secret in (b"TOPSECRET", b"OLDSECRET", b"OLDTOKEN"):
        assert secret not in blob


def test_bundle_respects_size_cap(tmp_path, fake_backend):
    out = _make_output(tmp_path, fake_backend)
    big = out / "20250101-000000-lego" / "requests"
    big.mkdir(parents=True)
    import os

    (big / "001-lego.html").write_bytes(os.urandom(400_000))  # incompressible
    zpath, included, skipped = probe.make_bundle(out, tmp_path, cap_mb=0.2)
    assert "20250101-000000-lego/requests/001-lego.html" in skipped
    assert "report.md" in included
    with zipfile.ZipFile(zpath) as zf:
        assert "001-lego.html" in zf.read("MANIFEST.txt").decode()
    assert zpath.stat().st_size < 0.25 * 1024 * 1024


def test_bundle_empty_output_is_a_clear_error(tmp_path):
    with pytest.raises(probe.ProbeError):
        probe.make_bundle(tmp_path / "nothing", tmp_path)


def test_fixture_from_folder_and_zip(tmp_path, fake_backend):
    out = _make_output(tmp_path, fake_backend)
    target_dir = next(p for p in out.iterdir() if p.name.endswith("-target"))
    dest_root = tmp_path / "fixtures" / "live"
    dest = probe.make_fixture(target_dir, "target_in_stock", dest_root)
    assert dest == dest_root / "target"
    idx = json.loads((dest / "index.json").read_text())
    fx = idx["fixtures"]["target_in_stock"]
    assert fx["source_url"] == URLS["target"] and fx["observed"]["status"] == "in_stock"
    assert fx["files"][0]["file"] == "target_in_stock__01.html"
    assert (dest / "target_in_stock__01.html").read_text() == "<html>target page</html>"
    with pytest.raises(probe.ProbeError):
        probe.make_fixture(target_dir, "target_in_stock", dest_root)  # exists
    probe.make_fixture(target_dir, "target_in_stock", dest_root, force=True)
    with pytest.raises(probe.ProbeError):
        probe.make_fixture(target_dir, "Bad Name", dest_root)

    zpath, _, _ = probe.make_bundle(out, tmp_path)
    with pytest.raises(probe.ProbeError, match="--entry"):
        probe.make_fixture(zpath, "walmart_oos", dest_root)  # two checks in the zip
    walmart_dir = next(p for p in out.iterdir() if p.name.endswith("-walmart")).name
    dest = probe.make_fixture(zpath, "walmart_oos", dest_root, entry=walmart_dir)
    assert json.loads((dest / "index.json").read_text())["fixtures"]["walmart_oos"]["observed"]["status"] == "out_of_stock"


# --------------------------------------------------------------------------- local web UI


def test_serve_ui_and_api(tmp_path, fake_backend):
    httpd, srv = probe.start_server(0, tmp_path / "out", probe.DEFAULT_SITES, 2)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    assert httpd.server_address[0] == "127.0.0.1"

    def req(method, path, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    try:
        st, page = req("GET", "/")
        assert st == 200 and b"<title>Site Probe</title>" in page
        st, meta = req("GET", "/api/meta")
        meta = json.loads(meta)
        assert len(meta["retailers"]) == len(registry.RETAILERS)
        assert all(r["samples"] for r in meta["retailers"])
        assert req("GET", "/", host="evil.example:80")[0] == 403  # DNS-rebinding guard

        st, job = req("POST", "/api/run", {"urls": [URLS["target"]], "keys": [], "options": {"zip": "60601"}})
        assert st == 200
        job = json.loads(job)
        for _ in range(100):
            st, state = req("GET", f"/api/job?id={job['id']}")
            state = json.loads(state)
            if state["state"] == "done":
                break
            threading.Event().wait(0.05)
        assert state["results"][0]["verdict"] == "OK"
        assert (tmp_path / "out" / "report.md").is_file()
        st, z = req("GET", "/api/bundle")
        assert st == 200 and z[:2] == b"PK"
        assert req("POST", "/api/run", {"urls": ["file:///etc/passwd"]})[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
        with contextlib.suppress(Exception):
            srv.close()


# --------------------------------------------------------------------------- browser options


@pytest.fixture
def clean_browser_env(monkeypatch):
    for k in ("ENABLE_BROWSER", "BROWSER_MODE", "BROWSER_CHANNEL", "BROWSER_CDP_URL", "BROWSER_PROFILE_DIR"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _parse(*argv):
    return probe.build_parser().parse_args(list(argv))


def test_browser_flags_map_to_backend_env(clean_browser_env):
    import os

    a = _parse("sweep", "--headless", "--chrome", "--cdp", "http://127.0.0.1:9222")
    probe.apply_browser_options(a, platform="linux")
    assert os.environ["BROWSER_MODE"] == "headless" and os.environ["BROWSER_CHANNEL"] == "chrome"
    assert os.environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"
    assert os.environ["BROWSER_PROFILE_DIR"] == str(probe.DEFAULT_PROFILE)
    with pytest.raises(SystemExit):
        _parse("check", "https://x.test/p", "--headed", "--headless")  # mutually exclusive


def test_macos_defaults_to_installed_chrome(clean_browser_env):
    import os

    clean_browser_env.setattr(fetcher, "chrome_installed", lambda: True)
    probe.apply_browser_options(_parse("check", "https://x.test/p"), platform="darwin")
    assert os.environ["BROWSER_CHANNEL"] == "chrome" and "BROWSER_MODE" not in os.environ  # auto = headed on macOS
    clean_browser_env.delenv("BROWSER_CHANNEL")
    probe.apply_browser_options(_parse("check", "https://x.test/p", "--no-chrome", "--headed"), platform="darwin")
    assert os.environ["BROWSER_CHANNEL"] == "chromium" and os.environ["BROWSER_MODE"] == "headed"
    clean_browser_env.delenv("BROWSER_CHANNEL")
    clean_browser_env.setattr(fetcher, "chrome_installed", lambda: False)
    probe.apply_browser_options(_parse("serve"), platform="darwin")
    assert "BROWSER_CHANNEL" not in os.environ


def test_engine_and_mode_are_recorded(tmp_path, fake_backend, monkeypatch):
    info = {"engine": "patchright", "mode": "headed-xvfb", "channel": "chromium", "version": "141.0.7390.37",
            "persistent_profile": True, "cdp": False, "launched": True}
    monkeypatch.setattr(fetcher, "browser_info", lambda: dict(info))
    s = asyncio.run(probe.probe_one(URLS["target"], out_root=tmp_path, preview=False))
    doc = json.loads((Path(s["bundle_path"]) / "result.json").read_text())
    assert doc["environment"]["browser"]["engine"] == "patchright"
    assert doc["environment"]["browser"]["mode"] == "headed-xvfb"
    md, _ = probe.write_report([s], tmp_path)
    assert "Browser engine: patchright, headed-xvfb, chromium 141.0.7390.37, persistent profile." in md.read_text()
    assert probe.describe_browser({"engine": "patchright", "mode": "cdp", "version": "142.0.1", "launched": True}) \
        == "patchright, cdp, Chrome 142.0.1"
    assert probe.describe_browser({}) == "unknown (older backend)"


# --------------------------------------------------------------------------- discover


import gzip


class FakeNet:
    """Offline stand-in for LiveNet: ``files`` maps URL -> bytes/str for get(), ``pages`` URL -> HTML."""

    def __init__(self, files=None, pages=None):
        self.files = {k: (v.encode() if isinstance(v, str) else v) for k, v in (files or {}).items()}
        self.pages = pages or {}
        self.gets: list[str] = []
        self.paged: list[str] = []

    async def get(self, url):
        self.gets.append(url)
        return (200, self.files[url]) if url in self.files else (404, b"")

    async def page(self, url):
        self.paged.append(url)
        if url not in self.pages:
            raise RuntimeError("HTTP 403")
        return self.pages[url]


def _urlset(*locs, lastmods=None):
    body = "".join(
        f"<url><loc>{loc}</loc>" + (f"<lastmod>{lastmods[i]}</lastmod>" if lastmods else "") + "</url>"
        for i, loc in enumerate(locs))
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def _index(*locs):
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'


def test_robots_sitemap_lines():
    txt = ("User-agent: *\nDisallow: /cart\nsitemap: https://www.x.test/a.xml\n  SITEMAP:https://www.x.test/b.xml.gz  \n"
           "Sitemap: /relative.xml\nSitemap: https://www.x.test/a.xml\n# Sitemap: https://no.test/c.xml\n")
    assert probe.parse_robots_sitemaps(txt, "https://www.x.test") == [
        "https://www.x.test/a.xml", "https://www.x.test/b.xml.gz", "https://www.x.test/relative.xml"]
    assert probe.parse_robots_sitemaps("User-agent: *") == []


def test_parse_sitemap_index_urlset_gzip_and_cdata():
    idx = probe.parse_sitemap(_index("https://s.test/sitemap-products-1.xml.gz", "https://s.test/blog.xml"))
    assert idx.kind == "index" and [e[0] for e in idx.entries] == [
        "https://s.test/sitemap-products-1.xml.gz", "https://s.test/blog.xml"]
    xml = _urlset("https://s.test/p/a?x=1&amp;y=2", "<![CDATA[https://s.test/p/b]]>", lastmods=["2026-09-01", "2026-09-02"])
    for data in (xml, xml.encode(), gzip.compress(xml.encode())):  # str, bytes, .xml.gz bytes
        sm = probe.parse_sitemap(data)
        assert sm.kind == "urlset"
        assert sm.entries == [("https://s.test/p/a?x=1&y=2", "2026-09-01"), ("https://s.test/p/b", "2026-09-02")]
    assert probe.parse_sitemap("https://a.test/1\nhttps://a.test/2\n").entries[1][0] == "https://a.test/2"
    assert probe.parse_sitemap(b"\x1f\x8bnot really gzip").entries == []


def test_sitemap_rank_prefers_product_sitemaps():
    urls = ["https://s.test/sitemap-blog.xml", "https://s.test/sitemap.xml", "https://s.test/sitemap-products-2.xml",
            "https://s.test/sitemap-en-us-pdp.xml", "https://s.test/sitemap-fr-fr-pdp.xml"]
    ranked = sorted(urls, key=probe.sitemap_rank, reverse=True)
    assert ranked[0] == "https://s.test/sitemap-en-us-pdp.xml" and ranked[-1] == "https://s.test/sitemap-blog.xml"
    assert probe.sitemap_rank(urls[3]) > probe.sitemap_rank(urls[4])


@pytest.mark.parametrize("key,url,expected", [
    ("target", "https://www.target.com/p/nintendo-switch-2/-/A-94693225#lnk=x", "https://www.target.com/p/nintendo-switch-2/-/A-94693225"),
    ("target", "https://www.target.com/c/video-games/-/N-5xtg5", None),
    ("bestbuy", "https://www.bestbuy.com/site/some-thing/6603968.p?skuId=6603968&intl=nosplash",
     "https://www.bestbuy.com/site/some-thing/6603968.p?skuId=6603968&intl=nosplash"),
    ("bestbuy", "https://www.bestbuy.com/product/x/JJGCQ8WQ7K/sku/6614313", "https://www.bestbuy.com/product/x/JJGCQ8WQ7K/sku/6614313"),
    ("bestbuy", "https://www.bestbuy.com/site/promo/deals.c?id=abcat", None),
    ("walmart", "https://www.walmart.com/ip/Nintendo-Switch-2/15949610846?athbdg=L1600", "https://www.walmart.com/ip/Nintendo-Switch-2/15949610846"),
    ("walmart", "https://www.walmart.com/browse/electronics/3944", None),
    ("samsclub", "https://www.samsclub.com/ip/Thing/prod123456", None),
    ("samsclub", "https://www.samsclub.com/ip/Thing/16634389868", "https://www.samsclub.com/ip/Thing/16634389868"),
    ("amazon", "https://www.amazon.com/PlayStation-5/dp/B0DGY63Z2H/ref=sr_1_1", "https://www.amazon.com/PlayStation-5/dp/B0DGY63Z2H/ref=sr_1_1"),
    ("amazon", "https://www.amazon.com/dp/short", None),
    ("jazwares", "https://shop.jazwares.com/products/squishmallows-chip?variant=1", "https://shop.jazwares.com/products/squishmallows-chip"),
    ("jazwares", "https://shop.jazwares.com/collections/all", None),
    ("newegg", "https://www.newegg.com/p/N82E16819113877", "https://www.newegg.com/p/N82E16819113877"),
    ("newegg", "https://www.newegg.com/msi-rtx/p/N82E16814137917", "https://www.newegg.com/msi-rtx/p/N82E16814137917"),
    ("newegg", "https://www.newegg.com/p/pl?d=gpu", None),
    ("microcenter", "https://www.microcenter.com/product/687907/amd-ryzen", "https://www.microcenter.com/product/687907/amd-ryzen"),
    ("microcenter", "https://www.microcenter.com/category/4294967292/cpus", None),
    ("lego", "https://www.lego.com/en-us/product/millennium-falcon-75192", "https://www.lego.com/en-us/product/millennium-falcon-75192"),
    ("lego", "https://www.lego.com/en-us/categories/star-wars", None),
    ("homedepot", "https://www.homedepot.com/p/RYOBI-Drill-PCL206K1/315143462", "https://www.homedepot.com/p/RYOBI-Drill-PCL206K1/315143462"),
    ("gamestop", "https://www.gamestop.com/video-games/switch/products/mario/11223344.html",
     "https://www.gamestop.com/video-games/switch/products/mario/11223344.html"),
    ("costco", "https://www.costco.com/x.product.4000362974.html", "https://www.costco.com/x.product.4000362974.html"),
    ("nextwarehouse", "https://www.nextwarehouse.com/item/?p_num=1234567&n=PNY", "https://www.nextwarehouse.com/item/?p_num=1234567&n=PNY"),
    ("target", "https://www.walmart.com/ip/x/15949610846", None),  # another store's URL
    ("target", "https://www.target.com/p/x/-/A-94693225.jpg", None),  # asset
    ("target", "https://www.target.com/cart", None),
    # 2026-09-29 discover gaps
    ("bhphoto", "https://www.bhphotovideo.com/c/product/758628-REG/Shure_SE215_CL.html",
     "https://www.bhphotovideo.com/c/product/758628-REG/Shure_SE215_CL.html"),
    ("bhphoto", "https://www.bhphotovideo.com/c/product/1809440-REG/apple_ipod_touch.html/specs", None),
    ("homedepot", "https://www.homedepot.com/p/315143462", "https://www.homedepot.com/p/315143462"),
    ("homedepot", "https://www.homedepot.com/p/reviews/RYOBI/315143462/1", None),
    ("psdirect", "https://direct.playstation.com/en-us/buy-accessories/dualsense-edge-wireless-controller.1000036452",
     "https://direct.playstation.com/en-us/buy-accessories/dualsense-edge-wireless-controller.1000036452"),
    ("psdirect", "https://direct.playstation.com/en-us/games/ghost-of-yotei.1000048912",
     "https://direct.playstation.com/en-us/games/ghost-of-yotei.1000048912"),
    ("psdirect", "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console-2-tb",
     "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console-2-tb"),
    ("psdirect", "https://direct.playstation.com/en-us/accessories/headsets", None),
    ("asus", "https://shop.asus.com/us/90nr0ib1-m00m10-rog-strix-g16-2025-g615.html",
     "https://shop.asus.com/us/90nr0ib1-m00m10-rog-strix-g16-2025-g615.html"),
    ("asus", "https://shop.asus.com/us/rog/90mb1ir0-m0aay0-rog-strix-b850-f-gaming-wifi.html",
     "https://shop.asus.com/us/rog/90mb1ir0-m0aay0-rog-strix-b850-f-gaming-wifi.html"),
    ("asus", "https://shop.asus.com/us/id-me-page.html", None),
    ("evga", "https://www.evga.com/products/product.aspx?pn=220-G7-1000-X1",
     "https://www.evga.com/products/product.aspx?pn=220-G7-1000-X1"),
    ("evga", "https://www.evga.com/products/product.aspx.pn=220-G7-1000-X1.html",
     "https://www.evga.com/products/product.aspx.pn=220-G7-1000-X1.html"),
    ("evga", "https://www.evga.com/products/productlist.aspx.type=10.html", None),
    ("nextwarehouse", "https://www.nextwarehouse.com/item/?2573476_g10e", "https://www.nextwarehouse.com/item/?2573476_g10e"),
    ("nvidia", "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5090/",
     "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5090/"),
    ("nvidia", "https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5080/", None),  # info page
    ("nvidia", "https://marketplace.nvidia.com/en-sg/consumer/graphics-cards/nvidia-geforce-rtx-5090/", None),
    ("microcenter", "https://www.microcenter.com/product/427343/2-year-accidental-damage-protection-plan", None),
    ("stockx", "https://stockx.com/nintendo-switch-2-console-us-version", "https://stockx.com/nintendo-switch-2-console-us-version"),
    ("stockx", "https://stockx.com/category/electronics", None),
    ("stockx", "https://stockx.com/brands/pop-mart", None),
    ("ebay", "https://www.ebay.com/itm/387123456789?_skw=switch&hash=item5a", "https://www.ebay.com/itm/387123456789"),
    ("ebay", "https://www.ebay.com/itm/Nintendo-Switch-2/387123456789", "https://www.ebay.com/itm/Nintendo-Switch-2/387123456789"),
    ("ebay", "https://www.ebay.com/sch/i.html?_nkw=switch", None),
])
def test_normalize_candidate_per_retailer(key, url, expected):
    assert probe.normalize_candidate(url, key) == expected


def test_every_registry_store_has_a_pattern_and_samples_match_it():
    keys = {r.key for r in registry.RETAILERS}
    assert len(keys) == 59
    assert keys - set(probe.PRODUCT_PATTERNS) - set(probe.DISCOVER_SKIP) == set(), "stores without a product pattern"
    assert set(probe.PRODUCT_PATTERNS) - keys == set()
    assert probe.DISCOVER_SKIP == {}  # eBay (Buy-It-Now search) and StockX (category pages) are discovered too
    assert set(probe.DISCOVER_SEEDS) <= keys and set(probe.DISCOVER_BASES) <= keys
    # a store the table does not know falls back to generic heuristics
    assert probe.product_pattern("nope-store").search("/products/thing") and probe.product_pattern("x").search("/p/abc")
    for key, entries in probe.load_sites(probe.DEFAULT_SITES).items():
        if key in probe.DISCOVER_SKIP:
            continue
        for e in entries:
            assert probe.normalize_candidate(e["url"], key), f"{key}: sample {e['url']} does not match its own pattern"


def test_select_candidates_prefers_recent_and_is_deterministic():
    pool = [(f"https://s.test/p/{i}", f"2026-01-{i:02d}") for i in range(1, 20)] + [("https://s.test/p/x", None)]
    got = probe.select_candidates(pool, 3, "k")
    # a page-linked URL (no lastmod: the store's menus show it today) always gets a share of the tries
    assert len(got) == 3 and "https://s.test/p/x" in got
    assert all(int(u.rsplit("/", 1)[1]) >= 8 for u in got if u != "https://s.test/p/x")  # only from the recent 12
    assert got == probe.select_candidates(pool, 3, "k")
    assert "https://s.test/p/x" not in probe.select_candidates(pool, 1, "k")
    # a big sitemap never crowds page links out; few page links leave the rest to the sitemap
    many = [(f"https://s.test/p/{i}", "2026-01-01") for i in range(80)] + [(f"https://s.test/q/{i}", None) for i in range(5)]
    got = probe.select_candidates(many, 6, "k")
    assert len(got) == 6 and sum("/q/" in u for u in got) == 2
    assert probe.select_candidates([("https://s.test/p/a", None)] * 3, 5) == ["https://s.test/p/a"]


def _run(coro):
    return asyncio.run(coro)


def test_collect_from_sitemaps_follows_index_gz_and_prefers_product_sitemaps():
    base = "https://www.target.com"
    prod = "https://www.target.com/sitemap-products-1.xml.gz"
    blog = "https://www.target.com/sitemap-blog.xml"
    net = FakeNet({
        f"{base}/robots.txt": f"User-agent: *\nSitemap: {blog}\nSitemap: {base}/sitemap_index.xml\n",
        f"{base}/sitemap_index.xml": _index(blog, prod),
        blog: _urlset(f"{base}/blog/post-1"),
        prod: gzip.compress(_urlset(f"{base}/p/a/-/A-11111111", f"{base}/p/b/-/A-22222222", f"{base}/c/games/-/N-1",
                                    lastmods=["2026-09-01", "2026-09-02", "2026-09-03"]).encode()),
    })
    pool, notes = {}, []
    n = _run(probe.collect_from_sitemaps(net, probe.Budget(), base, "target", pool, notes))
    assert n == 2 and pool == {f"{base}/p/a/-/A-11111111": "2026-09-01", f"{base}/p/b/-/A-22222222": "2026-09-02"}
    assert net.gets.index(prod) < net.gets.index(blog) or blog not in net.gets  # product sitemap first
    assert net.gets[0] == f"{base}/robots.txt"


def test_collect_from_sitemaps_tries_default_locations_and_respects_budget():
    base = "https://www.lego.com"
    net = FakeNet({f"{base}/sitemap.xml": _urlset(*[f"{base}/en-us/product/set-{75190 + i}" for i in range(5)])})
    pool, notes = {}, []
    assert _run(probe.collect_from_sitemaps(net, probe.Budget(), base, "lego", pool, notes)) == 5
    assert net.gets[:2] == [f"{base}/robots.txt", f"{base}/sitemap.xml"]
    # nothing reachable: says so, and the request budget is honoured
    net2, notes2 = FakeNet(), []
    b = probe.Budget(max_requests=2)
    assert _run(probe.collect_from_sitemaps(net2, b, base, "lego", {}, notes2)) == 0
    assert len(net2.gets) == 2 and b.requests == 2 and notes2
    # byte cap: a big first file stops further requests
    big = FakeNet({f"{base}/robots.txt": f"Sitemap: {base}/a.xml\nSitemap: {base}/b.xml\n",
                   f"{base}/a.xml": _urlset(f"{base}/en-us/product/x-11111"), f"{base}/b.xml": _urlset(f"{base}/en-us/product/y-22222")})
    _run(probe.collect_from_sitemaps(big, probe.Budget(max_bytes=10), base, "lego", {}, []))
    assert len(big.gets) == 1


def test_collect_from_shopify_and_homepage_fallback():
    base = "https://shop.jazwares.com"
    net = FakeNet({f"{base}/products.json?limit=60": json.dumps(
        {"products": [{"handle": "squish-a", "updated_at": "2026-09-01"}, {"handle": "squish-b"}]})})
    pool = {}
    assert _run(probe.collect_from_shopify(net, probe.Budget(), base, "jazwares", pool)) == 2
    assert pool[f"{base}/products/squish-a"] == "2026-09-01"
    # homepage: <a href>, relative links, embedded JSON paths, off-site and non-product links ignored
    home = ('<a href="/products/one?variant=9">1</a><a href="https://evil.test/products/x">x</a>'
            '<a href="/collections/all">c</a><script>{"u":"\\/products\\/two-thing"}</script>')
    net2, notes, pool2 = FakeNet(pages={f"{base}/": home}), [], {}
    assert _run(probe.collect_from_pages(net2, probe.Budget(), base, "jazwares", pool2, notes)) == 2
    assert set(pool2) == {f"{base}/products/one", f"{base}/products/two-thing"}
    # no products on the homepage: opens a listing page it links to
    net3 = FakeNet(pages={f"{base}/": '<a href="/collections/all">all</a>', f"{base}/collections/all": '<a href="/products/deep">d</a>'})
    pool3 = {}
    assert _run(probe.collect_from_pages(net3, probe.Budget(), base, "jazwares", pool3, [])) == 1
    assert net3.paged == [f"{base}/", f"{base}/collections/all"]
    # unreachable homepage: a note, no crash
    notes4 = []
    assert _run(probe.collect_from_pages(FakeNet(), probe.Budget(), base, "jazwares", {}, notes4)) == 0 and notes4


def _summary(status, verdict="OK"):
    return {"status": status, "verdict": verdict, "price": "$1", "verdict_reason": verdict}


def test_choose_verified_prefers_definite_and_mixes_stock():
    v = [("u1", _summary("error", "FAIL")), ("u2", _summary("in_stock")), ("u3", _summary("in_stock")),
         ("u4", _summary("unknown", "BLOCKED")), ("u5", _summary("out_of_stock"))]
    assert [c["url"] for c in probe.choose_verified(v, 2)] == ["u2", "u5"]
    assert [c["url"] for c in probe.choose_verified(v, 3)] == ["u2", "u5", "u3"]
    assert [c["url"] for c in probe.choose_verified(v, 1)] == ["u2"]
    only_out = [("a", _summary("out_of_stock")), ("b", _summary("out_of_stock"))]
    assert [c["url"] for c in probe.choose_verified(only_out, 2)] == ["a", "b"]
    # nothing definite: BLOCKED / QUEUE stand in (flagged); FAIL never does
    soft = [("f", _summary("error", "FAIL")), ("b", _summary("error", "BLOCKED")), ("q", _summary("unknown", "QUEUE"))]
    got = probe.choose_verified(soft, 2)
    assert [(c["url"], c["definite"]) for c in got] == [("b", False), ("q", False)]
    assert probe.choose_verified([("f", _summary("error", "FAIL"))], 2) == []


@pytest.fixture
def fake_checks(monkeypatch):
    """checkers.run_check replaced: the answer depends on the product id in the URL."""
    calls = []

    async def fake_run_check(kind, url, generic_config, apple_config, retailer_config=None):
        calls.append((url, retailer_config))
        pid = int(url.rsplit("A-", 1)[1]) if "A-" in url else 0
        if pid % 10 == 0:
            return CheckResult(status="error", status_text="Check failed", error="HTTP 404 from www.target.com")
        if pid % 10 == 1:
            return CheckResult(status="in_stock", status_text="In stock", price="$10")
        if pid % 10 == 2:
            return CheckResult(status="out_of_stock", status_text="Out of stock")
        return CheckResult(status="unknown", status_text="Unknown")

    monkeypatch.setattr(checkers, "run_check", fake_run_check)
    return calls


def _target_net(ids):
    base = "https://www.target.com"
    return FakeNet({f"{base}/robots.txt": f"Sitemap: {base}/sitemap-products.xml\n",
                    f"{base}/sitemap-products.xml": _urlset(*[f"{base}/p/thing/-/A-{i}" for i in ids])})


def test_discover_store_verifies_and_prefers_definite_answers(fake_checks):
    target = registry.retailer_by_key("target")
    ids = [10000010, 10000020, 10000031, 10000043, 10000052]  # dead x2, in, unknown, out
    res = _run(probe.discover_store(target, [{"url": "https://www.target.com/p/x/-/A-94693225"}], _target_net(ids),
                                    per_store=2, max_tries=8, base_config={"zip": "60601"}))
    assert res.source == "sitemap" and res.found == 5 and 0 < res.tried <= 5
    assert len(res.chosen) == 2 and all(c["definite"] for c in res.chosen)
    assert {c["status"] for c in res.chosen} == {"in_stock", "out_of_stock"}
    assert all(rc == {"zip": "60601"} for _, rc in fake_checks)  # base config reaches the check
    assert res.tried == len(fake_checks)


def test_discover_store_all_dead_and_skipped_and_homepage_fallback(fake_checks):
    target = registry.retailer_by_key("target")
    res = _run(probe.discover_store(target, None, _target_net([10000010, 10000020, 10000030]), per_store=2))
    assert res.chosen == [] and res.notes and res.tried == 3
    # no sitemap at all: falls back to the homepage
    home = FakeNet(pages={"https://www.target.com/": '<a href="/p/x/-/A-10000051">x</a><a href="/p/y/-/A-10000062">y</a>'})
    res = _run(probe.discover_store(target, None, home, per_store=2))
    assert res.source == "homepage" and len(res.chosen) == 2


def test_discover_store_time_cap_and_crashing_check():
    target = registry.retailer_by_key("target")

    async def slow(url, rc):
        await asyncio.sleep(60)

    async def boom(url, rc):
        raise RuntimeError("kaput")

    net = _target_net([10000011, 10000021])
    res = _run(probe.discover_store(target, None, net, time_cap=0.05, check=slow, min_check_time=0.05))
    assert res.chosen == []  # nothing verified in time, and it returned promptly
    res = _run(probe.discover_store(target, None, _target_net([10000011]), check=boom))
    assert res.chosen == [] and res.tried == 1


def test_discover_all_runs_two_stores_at_a_time(monkeypatch):
    active, peak = [0], [0]

    async def fake_store(retailer, samples, net, **kw):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.02)
        active[0] -= 1
        return probe.StoreResult(key=retailer.key, name=retailer.name)

    monkeypatch.setattr(probe, "discover_store", fake_store)
    got = []
    res = _run(probe.discover_all(["target", "walmart", "lego", "newegg", "bestbuy"], {"sites": {}}, net=FakeNet(),
                                  on_result=got.append))
    assert peak[0] == 2 and [r.key for r in res] == ["target", "walmart", "lego", "newegg", "bestbuy"] and len(got) == 5
    with pytest.raises(probe.ProbeError):
        _run(probe.discover_all(["nope"], {}, net=FakeNet()))


SITES_DOC = {
    "_README": "SAMPLE product URLs ... keep me",
    "sites": {
        "target": ["https://www.target.com/p/dead/-/A-1",
                   {"url": "https://www.target.com/p/dead2/-/A-2",
                    "retailer_config": {"fulfillment": "pickup", "zip": "60601", "radius_miles": 25}, "note": "store pickup near ZIP"}],
        "bestbuy": [{"url": "https://www.bestbuy.com/site/x/1.p?skuId=1", "retailer_config": {"fulfillment": "delivery"}, "note": "n"},
                    "https://www.bestbuy.com/site/y/2.p?skuId=2"],
        "walmart": ["https://www.walmart.com/ip/dead/123456"],
        "ebay": ["https://www.ebay.com/itm/387123456789"],
        "stockx": ["https://stockx.com/nintendo-switch-2-console-us-version"],
    },
}


def _chosen(*urls):
    return [{"url": u, "status": "in_stock", "verdict": "OK", "price": None, "definite": True, "reason": ""} for u in urls]


def test_merge_sites_preserves_config_comments_and_skipped_stores():
    results = [
        probe.StoreResult(key="target", chosen=_chosen("https://www.target.com/p/a/-/A-11111111", "https://www.target.com/p/b/-/A-22222222")),
        probe.StoreResult(key="bestbuy", chosen=_chosen("https://www.bestbuy.com/site/n/9.p?skuId=9")),
        probe.StoreResult(key="walmart", notes=["nothing found"]),  # nothing found: keeps the old sample
        probe.StoreResult(key="ebay", skipped="expire"),
        probe.StoreResult(key="stockx", skipped="walled"),
    ]
    out = probe.merge_sites(SITES_DOC, results, when=__import__("datetime").date(2026, 9, 29))
    assert out["_README"] == SITES_DOC["_README"] and out["_comment"].startswith("Discovered 2026-09-29")
    s = out["sites"]
    assert s["target"] == [
        {"url": "https://www.target.com/p/a/-/A-11111111",
         "retailer_config": {"fulfillment": "pickup", "zip": "60601", "radius_miles": 25}, "note": "store pickup near ZIP"},
        "https://www.target.com/p/b/-/A-22222222"]
    assert s["bestbuy"] == [{"url": "https://www.bestbuy.com/site/n/9.p?skuId=9",
                             "retailer_config": {"fulfillment": "delivery"}, "note": "n"}]
    assert s["walmart"] == SITES_DOC["sites"]["walmart"]
    assert s["ebay"] == SITES_DOC["sites"]["ebay"] and s["stockx"] == SITES_DOC["sites"]["stockx"]
    assert SITES_DOC["sites"]["target"][0] == "https://www.target.com/p/dead/-/A-1"  # input untouched
    # more config entries than discovered URLs: the config still survives
    two_cfg = {"sites": {"microcenter": [{"url": "https://www.microcenter.com/product/1/a", "retailer_config": {"store_id": "151"}},
                                         {"url": "https://www.microcenter.com/product/2/b", "retailer_config": {"store_id": "045"}}]}}
    m = probe.merge_sites(two_cfg, [probe.StoreResult(key="microcenter", chosen=_chosen("https://www.microcenter.com/product/9/z"))])
    assert [e["retailer_config"]["store_id"] for e in m["sites"]["microcenter"]] == ["151", "045"]


def test_dump_sites_round_trips_and_is_loadable(tmp_path):
    out = probe.merge_sites(SITES_DOC, [probe.StoreResult(key="target", chosen=_chosen("https://www.target.com/p/a/-/A-11111111"))])
    text = probe.dump_sites(out)
    assert json.loads(text) == out
    p = tmp_path / "sites.json"
    p.write_text(text, encoding="utf-8")
    loaded = probe.load_sites(p)
    assert loaded["target"][0]["retailer_config"]["zip"] == "60601" and "_comment" not in loaded


def _patch_live(monkeypatch, net):
    monkeypatch.setattr(probe, "LiveNet", lambda *a, **k: net)

    async def no_shutdown():
        pass

    monkeypatch.setattr(probe, "_shutdown_backend", no_shutdown)


def test_cli_discover_writes_discovered_json_or_sites_in_place(tmp_path, clean_browser_env, fake_checks, capsys):
    _patch_live(clean_browser_env, _target_net([10000031, 10000042]))
    sites = tmp_path / "sites.json"
    sites.write_text(json.dumps(SITES_DOC), encoding="utf-8")
    out = tmp_path / "out"
    assert probe.main(["discover", "--only", "target", "--sites", str(sites), "--out", str(out)]) == 0
    assert json.loads(sites.read_text()) == SITES_DOC  # not touched without --write
    disc = json.loads((out / "discovered.json").read_text())
    assert [e if isinstance(e, str) else e["url"] for e in disc["sites"]["target"]] == [
        "https://www.target.com/p/thing/-/A-10000031", "https://www.target.com/p/thing/-/A-10000042"]
    assert "target" in capsys.readouterr().out
    assert probe.main(["discover", "--only", "target", "--sites", str(sites), "--out", str(out), "--write"]) == 0
    written = json.loads(sites.read_text())
    assert written["_README"] == SITES_DOC["_README"] and written["_comment"]
    assert written["sites"]["target"][0]["retailer_config"]["fulfillment"] == "pickup"
    assert written["sites"]["ebay"] == SITES_DOC["sites"]["ebay"] and written["sites"]["walmart"] == SITES_DOC["sites"]["walmart"]


def test_cli_sweep_discover_checks_the_discovered_urls(tmp_path, clean_browser_env, fake_checks, capsys):
    _patch_live(clean_browser_env, _target_net([10000031, 10000042]))
    sites = tmp_path / "sites.json"
    sites.write_text(json.dumps(SITES_DOC), encoding="utf-8")
    out = tmp_path / "out"
    assert probe.main(["sweep", "--discover", "--only", "target", "--sites", str(sites), "--out", str(out)]) == 0
    assert json.loads(sites.read_text()) == SITES_DOC  # sites.json untouched
    assert (out / "discovered.json").is_file()
    report = json.loads((out / "report.json").read_text())
    urls = {r["url"] for r in report["results"]}
    assert urls == {"https://www.target.com/p/thing/-/A-10000031", "https://www.target.com/p/thing/-/A-10000042"}
    # the sweep ran with the pickup config carried over to the first discovered URL
    assert any(rc and rc.get("fulfillment") == "pickup" for _, rc in fake_checks)
    assert "OK" in capsys.readouterr().out


def test_livenet_plain_first_then_browser_and_soft_404(monkeypatch):
    calls = []

    def resp(status, body, url="https://s.test/x"):
        return httpx.Response(status, content=body, request=httpx.Request("GET", url))

    async def fake_http_get(url, **kw):
        calls.append(("http", url))
        if "robots" in url:
            return resp(200, b"User-agent: *\nSitemap: https://s.test/sm.xml\n")
        if url.endswith("soft.xml"):
            return resp(200, b"<!doctype html><html><body>Not found</body></html>")
        if url.endswith("missing.xml"):
            return resp(404, b"")
        return resp(403, b"Access Denied")

    async def fake_browser(page_url, target_url, accept="application/json"):
        calls.append(("browser", page_url, target_url))
        return 200, "<urlset><url><loc>https://s.test/p/1</loc></url></urlset>", []

    monkeypatch.setattr(fetcher, "http_get", fake_http_get)
    monkeypatch.setattr(fetcher, "browser_fetch_from_page", fake_browser)
    monkeypatch.setattr(fetcher, "browser_enabled", lambda: True)
    net = probe.LiveNet(gap=0)
    assert _run(net.get("https://s.test/robots.txt"))[0] == 200
    assert _run(net.get("https://s.test/soft.xml")) == (404, b"")
    assert _run(net.get("https://s.test/missing.xml")) == (404, b"")
    status, body = _run(net.get("https://s.test/blocked.xml"))  # 403 -> the real browser, from the store's origin
    assert status == 200 and b"<loc>https://s.test/p/1</loc>" in body
    assert ("browser", "https://s.test/", "https://s.test/blocked.xml") in calls
    n = len(calls)
    _run(net.get("https://s.test/again.xml"))  # the host is known to need the browser: no plain attempt
    assert calls[n][0] == "browser"
    # a store the registry marks as bot-walled answering a sitemap with an HTML page (Akamai's "Oops!!
    # Something went wrong" at Home Depot): the wall, not a soft 404 — the browser gets a turn
    status, body = _run(net.get("https://www.homedepot.com/soft.xml"))
    assert status == 200 and ("browser", "https://www.homedepot.com/", "https://www.homedepot.com/soft.xml") in calls


def test_discover_ebay_from_buy_it_now_search(fake_checks):
    # eBay listings expire: each run picks live Buy-It-Now /itm/ links from a search page (no sitemaps read)
    search = probe.DISCOVER_SEEDS["ebay"][0]
    net = FakeNet(pages={search: '<a href="https://www.ebay.com/itm/123456789011?hash=x">a</a>'
                                 '<a href="/itm/Switch-2/123456789022">b</a><a href="/sch/i.html?_nkw=x&_pgn=2">next</a>'})
    ebay = registry.retailer_by_key("ebay")
    res = _run(probe.discover_store(ebay, [{"url": "https://www.ebay.com/itm/387123456789"}], net, per_store=2,
                                    check=lambda u, rc: _fake_itm(u)))
    assert net.gets == [] and net.paged[0] == search
    assert res.source == "pages" and res.found == 2
    assert sorted(c["url"] for c in res.chosen) == ["https://www.ebay.com/itm/123456789011",
                                                     "https://www.ebay.com/itm/Switch-2/123456789022"]


async def _fake_itm(url):
    return {"status": "in_stock" if url.endswith("11") else "out_of_stock", "verdict": "OK", "price": "$1"}


def test_discover_uses_stale_sample_pages_and_html_sitemaps():
    # the stale B&H sample page (an open-box iPod now) still links to live products; its /specs sub-page doesn't count
    stale = "https://www.bhphotovideo.com/c/product/1809440-REG/fujifilm_x100vi.html"
    page = ('<a href="/c/product/1809440-REG/apple_ipod_touch.html/specs">s</a>'
            '<a href="/c/product/758628-REG/Shure_SE215_CL.html">1</a>'
            '<a href="https://www.bhphotovideo.com/c/product/1735751-REG/belkin_boostcharge.html">2</a>')
    net, pool, notes = FakeNet(pages={stale: page}), {}, []
    n = _run(probe.collect_from_pages(net, probe.Budget(), "https://www.bhphotovideo.com", "bhphoto", pool, notes,
                                      [stale], want=2))
    assert n == 2 and net.paged == [stale]  # enough found: the homepage isn't opened
    # a homepage with no products: its HTML sitemap is opened before other listing pages
    base = "https://www.nextwarehouse.com"
    net = FakeNet(pages={f"{base}/": '<a href="/categoryList.cfm">c</a><a href="/sitemap.cfm">s</a>',
                         f"{base}/sitemap.cfm": '<a href="/item/?2573476_g10e">x</a>'})
    pool = {}
    assert _run(probe.collect_from_pages(net, probe.Budget(), base, "nextwarehouse", pool, [])) == 1
    assert net.paged[:2] == [f"{base}/", f"{base}/sitemap.cfm"]


def test_discover_opens_listing_pages_one_level_below_the_seeds():
    # 2026-09-29: NextWarehouse's seed pages (sitemap.cfm, categoryList.cfm) list categories, not products, so
    # discovery found nothing; their category pages (.cfm?... query links) are opened next, before the homepage
    base = "https://www.nextwarehouse.com"
    net = FakeNet(pages={
        f"{base}/sitemap.cfm": '<a href="/aboutus.cfm">about</a><a href="/cat.cfm?c=517">Video Cards</a>'
                               '<a href="/cat.cfm?c=12">Memory</a>',
        f"{base}/cat.cfm?c=517": '<a href="/item/?p_num=4400123&n=PNY">x</a><a href="/item/?2573476_g10e">y</a>',
    })
    pool, notes = {}, []
    n = _run(probe.collect_from_pages(net, probe.Budget(max_requests=4), base, "nextwarehouse", pool, notes,
                                      [f"{base}/sitemap.cfm"]))
    assert n == 2 and net.paged[:2] == [f"{base}/sitemap.cfm", f"{base}/cat.cfm?c=517"]
    assert f"{base}/aboutus.cfm" not in net.paged
    assert set(pool) == {f"{base}/item/?p_num=4400123&n=PNY", f"{base}/item/?2573476_g10e"}


def test_discover_slow_sitemaps_leave_time_for_the_stale_sample_pages():
    # 2026-09-29: B&H / PS Direct kept their dead samples — sitemaps through the browser (a page load per file)
    # used up the whole pool phase, so the pages their stale samples / menus link to were never opened
    stale = "https://www.bhphotovideo.com/c/product/1809440-REG/fujifilm_16821474_x100vi_digital_camera_silver.html"
    live = ("https://www.bhphotovideo.com/c/product/49510-REG/Sony_MDR_7506_MDR_7506_Headphone.html",
            "https://www.bhphotovideo.com/c/product/758628-REG/Shure_SE215_CL_SE215_Sound_Isolating_In_Ear_Stereo.html")

    class SlowSitemaps(FakeNet):
        async def get(self, url):
            self.gets.append(url)
            await asyncio.sleep(0.5)
            return 404, b""

    net = SlowSitemaps(pages={stale: "".join(f'<a href="{u}">p</a>' for u in live)})

    async def check(url, rc):
        return {"status": "in_stock" if "49510" in url else "out_of_stock", "verdict": "OK", "price": "$1"}

    bh = registry.retailer_by_key("bhphoto")
    res = _run(probe.discover_store(bh, [{"url": stale}], net, time_cap=2.0, check=check, min_check_time=0.1))
    assert net.paged and net.paged[0] == stale and "sitemaps: time share used up" in res.notes
    assert sorted(c["url"] for c in res.chosen) == sorted(live)


def test_discover_prefers_items_that_can_come_back():
    v = [("ipod", {"status": "out_of_stock", "verdict": "OK", "verdict_reason": "No longer available"}),
         ("in", _summary("in_stock")), ("out", {"status": "out_of_stock", "verdict": "OK", "verdict_reason": "Sold out"})]
    assert [c["url"] for c in probe.choose_verified(v, 2)] == ["in", "out"]
    assert [c["url"] for c in probe.choose_verified(v[:1], 2)] == ["ipod"]  # still better than nothing
    assert not probe._enough([v[0], v[1]], 2)


def test_amd_discovery_looks_in_its_store():
    amd = registry.retailer_by_key("amd")
    assert probe.store_bases(amd, [{"url": "https://www.amd.com/en/direct-buy/5335621300/us"}]) == \
        ["https://shop-us-en.amd.com"]
    # the store's homepage (recorded 2026-09-29, where direct-buy links land) links its HTML sitemap
    assert "https://shop-us-en.amd.com/sitemap.php" in probe.DISCOVER_SEEDS["amd"]
    ok = "https://shop-us-en.amd.com/amd-ryzen-7-9800x3d-processor/"
    assert probe.normalize_candidate(ok, "amd") == ok
    for u in ("https://shop-us-en.amd.com/adaptive-embedded-computing/", "https://shop-us-en.amd.com/terms-of-purchase/",
              "https://shop-us-en.amd.com/amd-game-bundle-with-onimusha-way-of-the-sword/",
              "https://shop-us-en.amd.com/processors/", "https://shop-us-en.amd.com/cart.php"):
        assert probe.normalize_candidate(u, "amd") is None, u


def test_retired_store_is_info_and_not_discovered():
    cx = registry.retailer_by_key("consutronix")
    assert cx.retired
    assert probe.retired_verdict(cx, "STALE", "Product page not found (HTTP 404)")[0] == "INFO"
    assert probe.retired_verdict(cx, "OK", "In stock") == ("OK", "In stock")
    assert probe.retired_verdict(registry.retailer_by_key("target"), "STALE", "x") == ("STALE", "x")
    res = _run(probe.discover_store(cx, None, FakeNet()))
    assert res.skipped and not res.chosen


def test_retired_store_sweep_reports_info(monkeypatch, tmp_path):
    async def fake_run_check(kind, url, generic_config, apple_config, retailer_config=None):
        return CheckResult(status="error", status_text="Product page not found (HTTP 404)",
                           error="Product page not found (HTTP 404) — update the link")

    monkeypatch.setattr(checkers, "run_check", fake_run_check)
    s = _run(probe.probe_one("https://consutronix.com/products/gigabyte-geforce-rtx-5080-windforce-oc-sff-16g",
                             preview=False, out_root=None))
    assert s["verdict"] == "INFO" and s["verdict_reason"].startswith("Store no longer sells online")


def test_local_sites_take_fixed_shipped_samples(tmp_path, monkeypatch):
    # the 2026-09-29 19:53 sweep read sites.local.json (its URLs aren't sites.json's), which still carried the dead
    # B&H / AMD / PS Direct samples discovery found nothing for; a shipped fix must reach it
    shipped = json.loads(probe.DEFAULT_SITES.read_text())
    legacy_local = {"_README": "x", "_comment": "Discovered 2026-09-29 by `probe.py discover` for 50 store(s): ...", "sites": {
        "bhphoto": ["https://www.bhphotovideo.com/c/product/1809440-REG/fujifilm_16821474_x100vi_digital_camera_silver.html"],
        "asus": ["https://eshop.asus.com/us/rog-strix-scar-18-2026-gaming-laptop.html"],  # discovered
        "psdirect": ["https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console.3009726"],
    }}
    got = probe.load_sites_data(probe.overlay_shipped(legacy_local, shipped))
    assert got["asus"][0]["url"] == "https://eshop.asus.com/us/rog-strix-scar-18-2026-gaming-laptop.html"
    assert got["bhphoto"][0]["url"] == shipped["sites"]["bhphoto"][0]["url"] != legacy_local["sites"]["bhphoto"][0]
    assert got["psdirect"][0]["url"].endswith("playstation5-pro-console-2-tb")
    assert "target" in got and "lego" in got  # stores the local file lacks come from sites.json
    # with the new bookkeeping: a store no discovery found takes the shipped entries, discovered ones stay
    merged = probe.merge_sites(legacy_local, [probe.StoreResult(key="asus", chosen=[
        {"url": "https://eshop.asus.com/us/x-1.html", "status": "in_stock", "verdict": "OK", "definite": True}])])
    assert merged["_discovered"] == ["asus"]
    merged["sites"]["lego"] = ["https://www.lego.com/en-us/product/old-12345"]
    again = probe.load_sites_data(probe.overlay_shipped(merged, shipped))
    assert again["asus"][0]["url"] == "https://eshop.asus.com/us/x-1.html"
    assert again["lego"][0]["url"] == probe.load_sites_data(shipped)["lego"][0]["url"]
    # sweep / discover read the overlay when the local file is the one in use
    local = tmp_path / "sites.local.json"
    local.write_text(json.dumps(legacy_local))
    monkeypatch.setattr(probe, "LOCAL_SITES", local)
    assert probe.sites_doc_for(local)["sites"]["bhphoto"] == shipped["sites"]["bhphoto"]
    assert probe.sites_doc_for(tmp_path / "other.json")["sites"] == {}
    with pytest.raises(probe.ProbeError):
        probe.sites_doc_for(tmp_path / "missing.json", must_exist=True)


def test_discover_bases_and_locale_sitemaps():
    nv = registry.retailer_by_key("nvidia")
    # the www.nvidia.com info-page samples don't send discovery there
    assert probe.store_bases(nv, [{"url": "https://www.nvidia.com/en-sg/geforce/graphics-cards/50-series/rtx-5050/"}]) \
        == ["https://marketplace.nvidia.com"]
    samples = [{"url": "https://shop.asus.com/us/90mb1ir0-m0aay0-rog-strix-b850-f-gaming-wifi.html"},
               {"url": "https://www.lego.com/en-us/product/orchid-10311"}]
    assert probe.locale_sitemaps("https://shop.asus.com", samples) == ["https://shop.asus.com/us/sitemap.xml"]
    assert probe.locale_sitemaps("https://www.lego.com", samples) == ["https://www.lego.com/en-us/sitemap.xml"]
    base = "https://shop.asus.com"
    net = FakeNet({f"{base}/us/sitemap.xml": _urlset(f"{base}/us/90nr0ib1-m00m10-rog-strix-g16-2025-g615.html",
                                                     f"{base}/us/deals.html")})
    pool = {}
    assert _run(probe.collect_from_sitemaps(net, probe.Budget(), base, "asus", pool, [],
                                            extra_seeds=probe.locale_sitemaps(base, samples))) == 1


def test_discover_write_goes_to_gitignored_local_file(tmp_path, monkeypatch):
    local = tmp_path / "sites.local.json"
    monkeypatch.setattr(probe, "LOCAL_SITES", local)
    shipped = tmp_path / "sites.json"
    monkeypatch.setattr(probe, "DEFAULT_SITES", shipped)
    assert probe.effective_sites(None) == shipped  # no local file yet -> shipped samples
    local.write_text("{}", encoding="utf-8")
    assert probe.effective_sites(None) == local  # discover --write output wins once it exists
    assert probe.effective_sites(tmp_path / "x.json") == tmp_path / "x.json"  # explicit --sites always wins
    assert "sites.local.json" in (Path(probe.__file__).parent / ".gitignore").read_text()


def test_report_names_the_sample_file(tmp_path):
    s = {"url": "https://www.target.com/p/-/A-1", "store": "target", "status": "in_stock", "verdict": "OK"}
    md, js = probe.write_report([s], tmp_path, sites=probe.LOCAL_SITES)
    assert "Sample URLs from: sites.local.json (from `discover --write`)." in md.read_text()
    assert json.loads(js.read_text())["sites"] == "sites.local.json (from `discover --write`)"
    md, _ = probe.write_report([s], tmp_path, sites=probe.DEFAULT_SITES)
    assert "Sample URLs from: sites.json (shipped samples)." in md.read_text()


def test_livenet_gzipped_sitemap_through_the_browser_stays_binary(monkeypatch):
    # a .xml.gz read as text in the page came back mangled (B&H's walled sitemaps): the browser returns base64
    import base64 as b64

    xml = _urlset("https://www.bhphotovideo.com/c/product/49510-REG/Sony_MDR_7506_MDR_7506_Headphone.html")
    seen = {}

    async def fake_http_get(url, **kw):
        return httpx.Response(403, content=b"Access Denied", request=httpx.Request("GET", url))

    async def fake_browser(page_url, target_url, accept="application/json", *, binary=False):
        seen[target_url] = binary
        return 200, (b64.b64encode(gzip.compress(xml.encode())).decode() if binary else xml), []

    monkeypatch.setattr(fetcher, "http_get", fake_http_get)
    monkeypatch.setattr(fetcher, "browser_fetch_from_page", fake_browser)
    monkeypatch.setattr(fetcher, "browser_enabled", lambda: True)
    net = probe.LiveNet(gap=0)
    status, data = _run(net.get("https://www.bhphotovideo.com/sitemap/products-1.xml.gz"))
    assert status == 200 and seen["https://www.bhphotovideo.com/sitemap/products-1.xml.gz"] is True
    assert probe.parse_sitemap(data).entries[0][0].endswith("Sony_MDR_7506_MDR_7506_Headphone.html")
    status, data = _run(net.get("https://www.bhphotovideo.com/sitemap.xml"))
    assert seen["https://www.bhphotovideo.com/sitemap.xml"] is False and b"<loc>" in data
