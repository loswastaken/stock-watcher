"""The real-browser engine: mode selection, Xvfb lifecycle, persistent profile locks, challenge
waiting, XHR capture, CDP / launch paths (mocked) and browser-first routing. The last tests
drive the real Chromium against a local server (skipped when it can't start)."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.checkers import fetcher

CF_PAGE = "<html><head><title>Just a moment...</title></head><body><div id=cf-chl-widget></div></body></html>"
AKAMAI_PAGE = ("<html><head><title>Access Denied</title></head><body>You don't have permission. "
               "Reference #18.4c1d3e17.1727000000.1a2b3c4d</body></html>")
PX_PAGE = "<html><body><div id=px-captcha></div>Press &amp; Hold to confirm you are a human</body></html>"
PRODUCT = ("<html><head><title>Widget</title></head><body><h1>Widget</h1>"
           "<button>Add to cart</button></body></html>")


# ------------------------------------------------------------------ mode selection


@pytest.mark.parametrize("mode,platform,environ,xvfb,expected", [
    ("headless", "linux", {"DISPLAY": ":0"}, "/usr/bin/Xvfb", ("headless", None)),
    ("auto", "darwin", {}, None, ("headed", None)),
    ("auto", "win32", {}, None, ("headed", None)),
    ("auto", "linux", {"DISPLAY": ":1"}, "/usr/bin/Xvfb", ("headed", None)),
    ("auto", "linux", {}, "/usr/bin/Xvfb", ("xvfb", "/usr/bin/Xvfb")),
    ("auto", "linux", {}, None, ("headless", None)),
    ("headed", "linux", {}, "/usr/bin/Xvfb", ("xvfb", "/usr/bin/Xvfb")),
    ("headed", "linux", {}, None, ("headless", None)),
    ("bogus", "linux", {}, "/usr/bin/Xvfb", ("xvfb", "/usr/bin/Xvfb")),
])
def test_select_mode(mode, platform, environ, xvfb, expected):
    assert fetcher.select_mode(mode, platform=platform, environ=environ, which=lambda _b: xvfb) == expected


def test_select_mode_reads_browser_mode_env(monkeypatch):
    monkeypatch.setenv("BROWSER_MODE", "headless")
    assert fetcher.select_mode()[0] == "headless"
    assert fetcher.select_mode(environ={"BROWSER_MODE": "HEADED", "DISPLAY": ":3"}, platform="linux") == ("headed", None)


def test_browser_info_reports_the_plan(monkeypatch):
    monkeypatch.setattr(fetcher, "_last_info", {})
    monkeypatch.setenv("BROWSER_MODE", "headless")
    monkeypatch.setenv("BROWSER_CHANNEL", "chrome")
    info = fetcher.browser_info()
    assert info["mode"] == "headless" and info["channel"] == "chrome" and info["launched"] is False
    monkeypatch.setenv("BROWSER_CDP_URL", "http://10.0.0.5:9222")
    info = fetcher.browser_info()
    assert info["mode"] == "cdp" and info["cdp"] is True and info["persistent_profile"] is False


# ------------------------------------------------------------------ Xvfb lifecycle


class FakeProc:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.terminated = self.killed = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.exit_code


def _fake_x(files: set[str], procs: list, ready_after: int = 1, dies: set[int] = frozenset()):
    calls: list[list[str]] = []
    polls = {"n": 0}

    def popen(cmd, **kw):
        calls.append(cmd)
        n = int(cmd[1][1:])
        p = FakeProc(exit_code=1 if n in dies else None)
        p.n = n
        procs.append(p)
        return p

    def exists(path):
        if path in files:
            return True
        if path.startswith("/tmp/.X11-unix/X") and procs and procs[-1].poll() is None:
            polls["n"] += 1
            return polls["n"] > ready_after and path == f"/tmp/.X11-unix/X{procs[-1].n}"
        return False

    return popen, exists, calls


def test_xvfb_picks_a_free_display_and_stops():
    procs: list = []
    popen, exists, calls = _fake_x({"/tmp/.X99-lock"}, procs, ready_after=2)
    vd = fetcher.VirtualDisplay("/usr/bin/Xvfb", popen=popen, exists=exists, sleep=lambda s: None)
    assert vd.start() == ":100"
    assert calls[0][:2] == ["/usr/bin/Xvfb", ":100"] and "-nolisten" in calls[0]
    assert vd.running()
    assert vd.start() == ":100" and len(calls) == 1  # reused while running
    proc = vd.proc
    vd.stop()
    assert proc.terminated and not vd.running() and vd.display is None


def test_xvfb_skips_a_display_whose_server_exits():
    procs: list = []
    popen, exists, calls = _fake_x(set(), procs, dies={99})
    vd = fetcher.VirtualDisplay(popen=popen, exists=exists, sleep=lambda s: None)
    assert vd.start() == ":100"
    assert [c[1] for c in calls] == [":99", ":100"]
    assert procs[0].terminated  # the loser is cleaned up


def test_xvfb_gives_up_when_nothing_is_free():
    vd = fetcher.VirtualDisplay(popen=lambda *a, **k: pytest.fail("no free display to try"),
                                exists=lambda p: True, tries=3)
    with pytest.raises(RuntimeError, match="no free display"):
        vd.start()


async def test_launch_falls_back_to_headless_without_a_virtual_display(monkeypatch):
    fake = _FakeEngine()
    _use_engine(monkeypatch, fake)
    monkeypatch.setattr(fetcher, "select_mode", lambda *a, **k: ("xvfb", "/nonexistent/Xvfb"))

    def broken_start(self):
        raise RuntimeError("Xvfb failed")

    monkeypatch.setattr(fetcher.VirtualDisplay, "start", broken_start)
    await fetcher._get_browser()
    assert fake.launches[-1]["headless"] is True and "env" not in fake.launches[-1]
    assert fetcher.browser_info()["mode"] == "headless"


async def test_shutdown_stops_xvfb(monkeypatch):
    stopped = []
    vd = fetcher.VirtualDisplay()
    monkeypatch.setattr(vd, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(fetcher, "_xvfb", vd)
    await fetcher.shutdown()
    assert stopped == [True] and fetcher._xvfb is None


# ------------------------------------------------------------------ persistent profile


def test_profile_dir_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert fetcher.browser_profile_dir(None) == tmp_path / "browser-profile" / "chromium"
    assert fetcher.browser_profile_dir("chrome") == tmp_path / "browser-profile" / "chrome"
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(tmp_path / "p"))
    assert fetcher.browser_profile_dir("chromium") == tmp_path / "p" / "chromium"


def _locked_profile(tmp_path: Path, owner: str) -> Path:
    prof = tmp_path / "prof"
    (prof / "Default").mkdir(parents=True)
    os.symlink(owner, prof / "SingletonLock")  # dangling, like Chromium's
    os.symlink("/tmp/nonexistent-socket", prof / "SingletonSocket")
    (prof / "SingletonCookie").write_text("x")
    (prof / "Default" / "Preferences").write_text(json.dumps({"profile": {"exit_type": "Crashed"}}))
    return prof


def test_stale_lock_from_another_host_is_removed(tmp_path):
    prof = _locked_profile(tmp_path, "old-container-id-4242")
    assert fetcher.clear_stale_profile_locks(prof, hostname="new-container", pid_alive=lambda p: True)
    assert not any(os.path.lexists(prof / n) for n in ("SingletonLock", "SingletonSocket", "SingletonCookie"))
    prefs = json.loads((prof / "Default" / "Preferences").read_text())
    assert prefs["profile"]["exit_type"] == "Normal" and prefs["profile"]["exited_cleanly"] is True


def test_stale_lock_from_a_dead_process_is_removed(tmp_path):
    prof = _locked_profile(tmp_path, "nas-4242")
    assert fetcher.clear_stale_profile_locks(prof, hostname="nas", pid_alive=lambda p: False)
    assert not os.path.lexists(prof / "SingletonLock")


def test_live_lock_is_kept_and_a_temporary_profile_used(tmp_path, monkeypatch):
    prof = _locked_profile(tmp_path, "nas-4242")
    assert not fetcher.clear_stale_profile_locks(prof, hostname="nas", pid_alive=lambda p: p == 4242)
    assert os.path.lexists(prof / "SingletonLock")

    monkeypatch.setattr(fetcher, "browser_profile_dir", lambda ch=None: prof)
    monkeypatch.setattr(fetcher, "clear_stale_profile_locks", lambda p: False)
    path, persistent = fetcher._prepare_profile("chromium")
    assert not persistent and Path(path) != prof and Path(path).is_dir()
    os.rmdir(path)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_pid_alive_ignores_reused_pids():
    assert fetcher._pid_alive(os.getpid()) is False  # alive, but not a Chromium
    assert fetcher._pid_alive(2 ** 22 + 12345) is False


# ------------------------------------------------------------------ challenge waiting


class FakePage:
    def __init__(self, contents, url="https://shop.example/p"):
        self.contents = list(contents)
        self.url = url
        self.reloads = 0
        self.reads = 0

    async def content(self):
        self.reads += 1
        item = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def reload(self, **kw):
        self.reloads += 1

    async def wait_for_load_state(self, *a, **kw):
        return None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


async def test_not_a_challenge_returns_immediately():
    clock = Clock()
    page = FakePage([PRODUCT])
    assert await fetcher.wait_out_challenge(page, sleep=clock.sleep, clock=clock) == PRODUCT
    assert clock.t == 1000.0


async def test_cloudflare_challenge_is_waited_out_without_reload():
    clock = Clock()
    nav = RuntimeError("Execution context was destroyed, most likely because of a navigation")
    page = FakePage([CF_PAGE, CF_PAGE, CF_PAGE, CF_PAGE, CF_PAGE, nav, PRODUCT])
    html = await fetcher.wait_out_challenge(page, wait=20, sleep=clock.sleep, clock=clock)
    assert html == PRODUCT and page.reloads == 0
    assert 1004 <= clock.t <= 1010


async def test_akamai_challenge_gets_one_reload():
    clock = Clock()
    page = FakePage([AKAMAI_PAGE] * 9 + [PRODUCT])
    html = await fetcher.wait_out_challenge(page, wait=20, sleep=clock.sleep, clock=clock)
    assert html == PRODUCT and page.reloads == 1


async def test_challenge_that_never_clears_gives_up_after_the_wait(monkeypatch):
    monkeypatch.setenv("BROWSER_CHALLENGE_WAIT", "7")
    clock = Clock()
    page = FakePage([CF_PAGE])
    html = await fetcher.wait_out_challenge(page, sleep=clock.sleep, clock=clock)
    assert html == CF_PAGE and 1006.9 <= clock.t <= 1007.1


async def test_interactive_captcha_waits_briefly_unless_a_person_can_solve_it():
    clock = Clock()
    assert await fetcher.wait_out_challenge(FakePage([PX_PAGE]), wait=20, sleep=clock.sleep, clock=clock) == PX_PAGE
    assert clock.t - 1000 <= fetcher._INTERACTIVE_WAIT + 0.01
    clock = Clock()
    await fetcher.wait_out_challenge(FakePage([PX_PAGE]), wait=20, interactive=True, sleep=clock.sleep, clock=clock)
    assert clock.t - 1000 >= 19.9


async def test_challenge_wait_respects_the_deadline():
    clock = Clock()
    with fetcher.deadline(8.0):
        await fetcher.wait_out_challenge(FakePage([CF_PAGE]), wait=20, sleep=clock.sleep, clock=clock)
    assert clock.t - 1000 <= 5.1  # 8 s left minus the 3 s reserve


def test_challenge_vendor():
    assert fetcher.challenge_vendor(CF_PAGE) == "cloudflare"
    assert fetcher.challenge_vendor(AKAMAI_PAGE) == "akamai"
    assert fetcher.challenge_vendor(PX_PAGE) == "perimeterx"
    assert fetcher.challenge_vendor('<iframe src="/_Incapsula_Resource?CWUDNSAI=1"></iframe>') == "imperva"
    assert fetcher.challenge_vendor('<script src="https://geo.captcha-delivery.com/c.js"></script>') == "datadome"


def test_check_budget_covers_a_challenge_wait():
    from app import checkers, scheduler

    assert checkers.CHECK_TIMEOUT < scheduler.CHECK_TIMEOUT_SECONDS
    assert (fetcher.TIMEOUT.read + fetcher.BROWSER_NAV_TIMEOUT_MS / 1000 + fetcher.BROWSER_IDLE_TIMEOUT_MS / 1000
            + fetcher.BROWSER_CHALLENGE_WAIT + 10) <= checkers.CHECK_TIMEOUT


# ------------------------------------------------------------------ network capture


class FakeResponse:
    def __init__(self, url, rtype="xhr", ctype="application/json", body=b'{"ok": true}', status=200, length=None):
        self.url, self.status, self._body = url, status, body
        self.request = SimpleNamespace(resource_type=rtype)
        self.headers = {"content-type": ctype}
        if length is not None:
            self.headers["content-length"] = str(length)

    async def all_headers(self):
        return self.headers

    async def body(self):
        return self._body


async def test_capture_collects_matching_xhr_only():
    import asyncio

    out: list[dict] = []
    pending: list = []

    def pred(u):
        if "boom" in u:
            raise ValueError("bad predicate")
        return "redsky" in u

    on_response = fetcher._capture_listener(pred, out, pending, 0.0)
    for r in (
        FakeResponse("https://redsky.target.com/fulfillment?x=1"),
        FakeResponse("https://redsky.target.com/doc", rtype="document"),
        FakeResponse("https://www.target.com/other"),
        FakeResponse("https://redsky.target.com/boom"),
        FakeResponse("https://redsky.target.com/big", length=5 * 1024 * 1024),
        FakeResponse("https://redsky.target.com/img", rtype="fetch", ctype="image/png", body=b"\x89PNG"),
        FakeResponse("https://redsky.target.com/v2", rtype="fetch", ctype="text/plain", body=b"hello", status=206),
    ):
        on_response(r)
    await asyncio.gather(*pending)
    assert [(c["url"], c["status"], c["body"]) for c in out] == [
        ("https://redsky.target.com/fulfillment?x=1", 200, '{"ok": true}'),
        ("https://redsky.target.com/v2", 206, "hello"),
    ]


# ------------------------------------------------------------------ launch / CDP (mocked engine)


class _FakeCtx:
    def __init__(self, browser=None):
        self.browser = browser
        self.closed = False
        self.handlers = {}

    def on(self, ev, fn):
        self.handlers[ev] = fn

    async def add_init_script(self, s):
        self.init_script = s

    async def close(self):
        self.closed = True


class _FakeBrowser:
    version = "141.0.7390.37"

    def __init__(self, contexts=()):
        self.contexts = list(contexts)
        self.closed = False

    def on(self, ev, fn):
        pass

    async def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, fail_channels=(), cdp_contexts=1):
        self.launches: list[dict] = []
        self.cdp: list[str] = []
        self.fail_channels = set(fail_channels)
        self.cdp_browser = _FakeBrowser()
        self.cdp_browser.contexts = [_FakeCtx(self.cdp_browser) for _ in range(cdp_contexts)]
        self.stopped = 0
        engine = self

        class Chromium:
            async def launch_persistent_context(self, user_data_dir, **kw):
                engine.launches.append(dict(kw, user_data_dir=user_data_dir))
                if kw.get("channel") in engine.fail_channels:
                    raise RuntimeError(f"Chromium distribution '{kw['channel']}' is not found")
                return _FakeCtx(_FakeBrowser())

            async def connect_over_cdp(self, endpoint, **kw):
                engine.cdp.append(endpoint)
                return engine.cdp_browser

        class PW:
            chromium = Chromium()

            async def stop(self):
                engine.stopped += 1

        class Starter:
            async def start(self):
                return PW()

        self.factory = lambda: Starter()


def _use_engine(monkeypatch, fake: _FakeEngine, name: str = "patchright"):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    monkeypatch.setattr(fetcher, "engine_names", lambda: [name])
    monkeypatch.setattr(fetcher, "_engine_factory", lambda n: fake.factory)


async def test_launch_headed_on_xvfb_uses_the_persistent_profile(monkeypatch, tmp_path):
    fake = _FakeEngine()
    _use_engine(monkeypatch, fake)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setattr(fetcher, "select_mode", lambda *a, **k: ("xvfb", "/usr/bin/Xvfb"))
    monkeypatch.setattr(fetcher.VirtualDisplay, "start", lambda self: ":123")
    ctx = await fetcher._get_browser()
    assert await fetcher._get_browser() is ctx and len(fake.launches) == 1
    kw = fake.launches[0]
    assert kw["user_data_dir"] == str(tmp_path / "browser-profile" / "chromium")
    assert kw["headless"] is False and kw["no_viewport"] is True and kw["env"]["DISPLAY"] == ":123"
    assert "user_agent" not in kw and "viewport" not in kw  # a headed browser reports itself
    assert "ignore_default_args" not in kw  # patchright handles the automation flags
    info = fetcher.browser_info()
    assert info["engine"] == "patchright" and info["mode"] == "headed-xvfb" and info["persistent_profile"]
    await fetcher.shutdown()
    assert ctx.closed
    assert fetcher.browser_info()["mode"] == "headed-xvfb"  # remembered after shutdown


async def test_launch_headless_playwright_keeps_identity_tweaks(monkeypatch):
    fake = _FakeEngine()
    _use_engine(monkeypatch, fake, name="playwright")
    monkeypatch.setenv("BROWSER_MODE", "headless")
    ctx = await fetcher._get_browser()
    kw = fake.launches[0]
    assert kw["headless"] is True and kw["viewport"] == {"width": 1366, "height": 900} and "env" not in kw
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert kw["ignore_default_args"] == ["--enable-automation"]
    assert "webdriver" in ctx.init_script


async def test_missing_chrome_channel_falls_back_to_bundled(monkeypatch):
    fake = _FakeEngine(fail_channels={"chrome"})
    _use_engine(monkeypatch, fake)
    monkeypatch.setenv("BROWSER_MODE", "headless")
    monkeypatch.setenv("BROWSER_CHANNEL", "chrome")
    await fetcher._get_browser()
    assert [k.get("channel") for k in fake.launches] == ["chrome", None]
    assert fetcher.browser_info()["channel"] == "chromium"


async def test_launch_failure_is_cached_briefly(monkeypatch):
    fake = _FakeEngine(fail_channels={"chromium", None})
    _use_engine(monkeypatch, fake)
    monkeypatch.setenv("BROWSER_MODE", "headless")
    with pytest.raises(fetcher.FetchError, match="Could not start browser"):
        await fetcher._get_browser()
    n = len(fake.launches)
    with pytest.raises(fetcher.FetchError):
        await fetcher._get_browser()
    assert len(fake.launches) == n  # no relaunch storm


async def test_cdp_url_connects_instead_of_launching(monkeypatch):
    fake = _FakeEngine()
    _use_engine(monkeypatch, fake)
    monkeypatch.setenv("BROWSER_CDP_URL", "http://chromium:9223")
    monkeypatch.setattr(fetcher.socket, "gethostbyname", lambda h: "172.18.0.5")
    ctx = await fetcher._get_browser()
    assert fake.launches == [] and fake.cdp == ["http://172.18.0.5:9223"]
    assert ctx is fake.cdp_browser.contexts[0]  # the real Chrome's own cookies
    assert fetcher.browser_info()["mode"] == "cdp"
    await fetcher.shutdown()
    assert fake.cdp_browser.closed and not ctx.closed  # disconnected; the user's Chrome keeps running


def test_cdp_endpoint_resolution(monkeypatch):
    monkeypatch.setattr(fetcher.socket, "gethostbyname", lambda h: "192.168.1.20")
    assert fetcher._cdp_endpoint("http://192.168.1.10:9222") == "http://192.168.1.10:9222"
    assert fetcher._cdp_endpoint("http://localhost:9222") == "http://localhost:9222"
    assert fetcher._cdp_endpoint("chromium:9223") == "http://192.168.1.20:9223"
    assert fetcher._cdp_endpoint("ws://chromium:9223/devtools/browser/x") == "ws://chromium:9223/devtools/browser/x"


# ------------------------------------------------------------------ browser-first routing


@respx.mock
async def test_registry_browser_store_goes_to_the_browser_first(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    calls = []

    async def fake_browser(u):
        calls.append(u)
        return fetcher.FetchResult(url=u, status=200, text=PRODUCT, via_browser=True)

    monkeypatch.setattr(fetcher, "browser_fetch", fake_browser)
    route = respx.get(url__regex=r"https://www\.kohls\.com/.*").mock(return_value=httpx.Response(403))
    res = await fetcher.fetch_html("https://www.kohls.com/product/prd-123/widget.jsp")
    assert res.via_browser and calls and not route.called


@respx.mock
async def test_plain_store_stays_plain(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")

    async def no_browser(u):
        raise AssertionError("browser not needed")

    monkeypatch.setattr(fetcher, "browser_fetch", no_browser)
    respx.get("https://shop.example/p").mock(return_value=httpx.Response(200, text=PRODUCT))
    res = await fetcher.fetch_html("https://shop.example/p")
    assert not res.via_browser


@respx.mock
async def test_store_that_works_plain_skips_browser_first_next_time(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    calls = []

    async def blocked_browser(u):
        calls.append(u)
        return fetcher.FetchResult(url=u, status=403, text=CF_PAGE, via_browser=True)

    monkeypatch.setattr(fetcher, "browser_fetch", blocked_browser)
    url = "https://www.kohls.com/product/prd-123/widget.jsp"
    respx.get(url).mock(return_value=httpx.Response(200, text=PRODUCT))
    assert not (await fetcher.fetch_html(url)).via_browser
    assert not (await fetcher.fetch_html(url)).via_browser
    assert calls == [url]  # only the first check tried the browser


# ------------------------------------------------------------------ real browser


class _ChallengeHandler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        cookie = self.headers.get("Cookie", "")
        type(self).seen.append((self.path, cookie))
        if self.path.startswith("/api/stock.json"):
            return self._send(200, '{"available": true}', "application/json")
        if self.path.startswith("/p"):
            if "clearance=ok" not in cookie:
                # A JS challenge: sets its clearance cookie, then reloads (like Cloudflare's).
                return self._send(403, "<html><head><title>Just a moment...</title></head><body>"
                                       "<script>setTimeout(function(){document.cookie='clearance=ok; max-age=3600; path=/';"
                                       "location.reload();}, 1200);</script></body></html>")
            return self._send(200, "<html><head><title>Widget</title></head><body><h1>Widget</h1>"
                                   "<button>Add to cart</button><script>fetch('/api/stock.json?sku=1')"
                                   ".then(r => r.json());</script></body></html>")
        return self._send(404, "nope")


@pytest.fixture
def challenge_server():
    _ChallengeHandler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ChallengeHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", _ChallengeHandler.seen
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
async def real_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    monkeypatch.setenv("BROWSER_CHALLENGE_WAIT", "15")
    try:
        await fetcher._get_browser()
    except fetcher.FetchError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium unavailable: {e}")
    yield
    await fetcher.shutdown()


async def test_real_browser_waits_out_a_js_challenge_and_keeps_its_cookie(challenge_server, real_browser):
    base, seen = challenge_server
    res = await fetcher.browser_fetch(base + "/p/1", capture=lambda u: "/api/stock.json" in u)
    assert res.status == 200 and "Add to cart" in res.text and not fetcher.looks_like_challenge(res.text)
    assert any(c["url"].endswith("/api/stock.json?sku=1") and json.loads(c["body"]) == {"available": True}
               for c in res.captured)
    # Restart the browser: the persistent profile still has the clearance cookie.
    await fetcher.shutdown()
    seen.clear()
    res2 = await fetcher.browser_fetch(base + "/p/2")
    assert res2.status == 200 and "Add to cart" in res2.text
    assert "clearance=ok" in seen[0][1]


def test_real_xvfb_starts_and_stops():
    import shutil

    binary = shutil.which("Xvfb")
    if not binary:
        pytest.skip("Xvfb not installed")
    vd = fetcher.VirtualDisplay(binary)
    display = vd.start()
    try:
        assert display.startswith(":") and vd.running()
        assert os.path.exists(f"/tmp/.X11-unix/X{display[1:]}")
    finally:
        proc = vd.proc
        vd.stop()
    assert proc.poll() is not None
