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
    assert "**5 checks:** 2 OK, 1 FAIL, 1 BLOCKED, 1 QUEUE" in text
    row = next(line for line in text.splitlines() if line.startswith("| target |"))
    assert "$449.99" in row and "**OK**" in row and "| target |" in row
    assert "Blocked by bot protection" in text
    report = json.loads(js.read_text())
    assert report["counts"] == {"OK": 2, "FAIL": 1, "BLOCKED": 1, "QUEUE": 1}
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
