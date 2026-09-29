"""Shared page fetching: a pooled httpx client with browser-like headers plus a lazy,
reused headless Chromium (Playwright) for JS-rendered or bot-protected pages.

Everything here is event-loop aware: the singletons are re-created if they are used
from a different running loop (matters for tests; production has a single loop).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("stockwatcher.checkers.fetcher")

CHROME_MAJOR = "141"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
SEC_CH_UA = f'"Google Chrome";v="{CHROME_MAJOR}", "Not?A_Brand";v="8", "Chromium";v="{CHROME_MAJOR}"'

try:  # httpx only decodes brotli when a brotli module is importable
    import brotli  # type: ignore  # noqa: F401

    _ACCEPT_ENCODING = "gzip, deflate, br"
except ImportError:  # pragma: no cover - depends on environment
    try:
        import brotlicffi  # type: ignore  # noqa: F401

        _ACCEPT_ENCODING = "gzip, deflate, br"
    except ImportError:
        _ACCEPT_ENCODING = "gzip, deflate"

DOCUMENT_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": _ACCEPT_ENCODING,
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = httpx.Timeout(15.0, connect=8.0)
PER_HOST_CONCURRENCY = 2
# Minimum gap between request *starts* to the same host (seconds). Tests set this to 0.
HOST_MIN_GAP = 0.4
BROWSER_CONCURRENCY = 2
BROWSER_NAV_TIMEOUT_MS = 30_000
BROWSER_IDLE_TIMEOUT_MS = 8_000
# Hosts that recently needed the browser go straight to it for this long.
BROWSER_HOST_TTL = 30 * 60

RETRY_WITH_BROWSER_STATUSES = {403, 429, 503}


class FetchError(Exception):
    """A site problem (HTTP error, blocked, network failure) with a user-facing message."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    via_browser: bool = False


def browser_enabled() -> bool:
    return os.environ.get("ENABLE_BROWSER", "true").strip().lower() not in {"0", "false", "no", "off", ""}


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# --------------------------------------------------------------------------- challenge / signal sniffing

_CHALLENGE_MARKERS = [
    re.compile(p, re.I)
    for p in (
        r"<title>\s*just a moment\.{0,3}\s*</title>",
        r"cf-browser-verification|cf_chl_opt|challenge-platform/h/",
        r"<title>\s*attention required!?\s*\|\s*cloudflare",
        r"_Incapsula_Resource|incapsula incident id",
        r"px-captcha|perimeterx|_pxhd|window\._pxAppId",
        r"captcha-delivery\.com|geo\.captcha-delivery",  # DataDome
        r"<title>\s*access denied\s*</title>[\s\S]{0,4000}reference\s*#",  # Akamai
        r"<title>\s*robot or human\?\s*</title>",
        r"<title>\s*pardon our interruption",
        r"are you a robot\?|verify you are (a )?human|please verify you are a human",
        r"/cdn-cgi/challenge-platform/",
        r"sec-if-cpt-container|_sec/cp_challenge",  # Akamai bot manager
        r"<title>\s*(amazon\.com|amazon)\s*</title>[\s\S]{0,5000}(captcha|characters you see)",
    )
]


def looks_like_challenge(html: str) -> bool:
    head = html[:60_000]
    return any(p.search(head) for p in _CHALLENGE_MARKERS)


_PRODUCT_SIGNAL_RE = re.compile(
    r"application/ld\+json|schema\.org/(?:Product|Offer)|itemprop=[\"']?(?:availability|offers|price)"
    r"|product:availability|og:availability|og:type[\"'][^>]{0,40}product|product:price"
    r"|add[\s-]+to[\s-]+(?:cart|bag|basket|trolley)|buy[\s-]+(?:it[\s-]+)?now|pre-?order"
    r"|sold[\s-]+out|out[\s-]+of[\s-]+stock|notify[\s-]+me|currently[\s-]+unavailable",
    re.I,
)


def has_product_signals(html: str) -> bool:
    return bool(_PRODUCT_SIGNAL_RE.search(html))


# --------------------------------------------------------------------------- loop-bound state


class _State:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: httpx.AsyncClient | None = None
        self.host_sems: dict[str, asyncio.Semaphore] = {}
        self.host_locks: dict[str, asyncio.Lock] = {}
        self.host_last: dict[str, float] = {}
        # browser
        self.pw: Any = None
        self.browser: Any = None
        self.browser_lock: asyncio.Lock | None = None
        self.browser_sem: asyncio.Semaphore | None = None


_state = _State()
_browser_hosts: dict[str, float] = {}


def _st() -> _State:
    global _state
    loop = asyncio.get_running_loop()
    if _state.loop is not loop:
        # Different loop (e.g. new test) — drop old loop-bound objects without awaiting them.
        _state = _State()
        _state.loop = loop
    return _state


def make_client(**kwargs: Any) -> httpx.AsyncClient:
    """A new AsyncClient with our defaults (callers own and must close it)."""
    opts: dict[str, Any] = dict(
        headers=DOCUMENT_HEADERS,
        timeout=TIMEOUT,
        follow_redirects=True,
        http2=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=60),
    )
    opts.update(kwargs)
    return httpx.AsyncClient(**opts)


def get_client() -> httpx.AsyncClient:
    st = _st()
    if st.client is None or st.client.is_closed:
        st.client = make_client()
    return st.client


@contextlib.asynccontextmanager
async def host_slot(url: str):
    """Per-host concurrency limit + politeness gap between request starts."""
    st = _st()
    host = host_of(url)
    sem = st.host_sems.setdefault(host, asyncio.Semaphore(PER_HOST_CONCURRENCY))
    lock = st.host_locks.setdefault(host, asyncio.Lock())
    async with sem:
        if HOST_MIN_GAP > 0:
            async with lock:
                wait = st.host_last.get(host, 0.0) + HOST_MIN_GAP - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                st.host_last[host] = time.monotonic()
        yield


# --------------------------------------------------------------------------- plain HTTP


async def http_get(url: str, *, headers: dict[str, str] | None = None, client: httpx.AsyncClient | None = None) -> httpx.Response:
    client = client or get_client()
    async with host_slot(url):
        try:
            return await client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            raise FetchError(f"Timed out fetching {host_of(url)}") from e
        except httpx.HTTPError as e:
            raise FetchError(f"Network error: {type(e).__name__}: {e}".rstrip(": ")) from e


def _mark_browser_host(url: str) -> None:
    _browser_hosts[host_of(url)] = time.monotonic() + BROWSER_HOST_TTL


def _host_prefers_browser(url: str) -> bool:
    exp = _browser_hosts.get(host_of(url))
    if exp is None:
        return False
    if exp < time.monotonic():
        _browser_hosts.pop(host_of(url), None)
        return False
    return True


async def fetch_html(
    url: str,
    render_js: bool = False,
    *,
    needs: Callable[[str], bool] | None = has_product_signals,
) -> FetchResult:
    """Fetch a page's HTML.

    Uses plain HTTP first; falls back to headless Chromium when the response is
    403/429/503, looks like a bot challenge, or ``needs(html)`` is False (content lacks
    the signals the caller needs — typical for JS-rendered pages). ``render_js`` forces
    the browser. Raises ``FetchError`` for unrecoverable site problems.
    """
    use_browser = browser_enabled()
    if use_browser and (render_js or _host_prefers_browser(url)):
        try:
            bres = await browser_fetch(url)
            if not looks_like_challenge(bres.text):
                return bres
            if render_js:
                raise FetchError(f"Blocked by bot protection on {host_of(url)}", status=bres.status)
        except FetchError:
            if render_js:
                raise
        # host preference was only a hint; try plain HTTP below

    try:
        resp = await http_get(url)
    except FetchError as e:
        # Bot protection often stalls or drops plain clients instead of answering 403
        # (Best Buy and other Akamai sites): let the real browser try before giving up.
        if not use_browser:
            raise
        log.info("fetch %s: %s -> retrying with browser", url, e)
        try:
            bres = await browser_fetch(url)
        except FetchError as be:
            raise FetchError(f"{e} (browser retry also failed: {be})", status=be.status) from be
        if bres.status < 400 and not looks_like_challenge(bres.text):
            _mark_browser_host(url)
            return bres
        if looks_like_challenge(bres.text):
            raise FetchError(f"Blocked by bot protection on {host_of(url)}", status=bres.status) from e
        raise FetchError(f"HTTP {bres.status} from {host_of(url)}", status=bres.status) from e
    text = resp.text if resp.content else ""
    result = FetchResult(url=str(resp.url), status=resp.status_code, text=text, headers=dict(resp.headers))

    reason: str | None = None
    if resp.status_code in RETRY_WITH_BROWSER_STATUSES:
        reason = f"HTTP {resp.status_code}"
    elif 200 <= resp.status_code < 300 and looks_like_challenge(text):
        reason = "bot challenge"
    elif 200 <= resp.status_code < 300 and needs is not None and not needs(text):
        reason = "no product signals"

    if reason and use_browser:
        log.info("fetch %s: %s -> retrying with browser", url, reason)
        try:
            bres = await browser_fetch(url)
        except FetchError as e:
            log.info("browser fallback failed for %s: %s", url, e)
            bres = None
        if bres is not None and bres.status < 400 and not looks_like_challenge(bres.text):
            if reason != "no product signals":
                _mark_browser_host(url)  # skip the doomed plain request for a while
            return bres

    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} from {host_of(url)}", status=resp.status_code)
    if looks_like_challenge(text):
        raise FetchError(f"Blocked by bot protection on {host_of(url)}", status=resp.status_code)
    return result


# --------------------------------------------------------------------------- Playwright browser

_BLOCKED_RESOURCES = {"image", "font", "media"}
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""


async def _get_browser() -> Any:
    st = _st()
    if st.browser_lock is None:
        st.browser_lock = asyncio.Lock()
    async with st.browser_lock:
        if st.browser is not None and st.browser.is_connected():
            return st.browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover
            raise FetchError("Browser fallback unavailable (playwright not installed)") from e
        try:
            if st.pw is None:
                st.pw = await async_playwright().start()
            st.browser = await st.pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
        except Exception as e:
            raise FetchError(f"Could not start browser: {e}") from e
        return st.browser


@contextlib.asynccontextmanager
async def browser_page(block_resources: bool = True):
    """Yield a fresh page in its own context (cookies isolated per fetch)."""
    if not browser_enabled():
        raise FetchError("Browser fallback disabled (ENABLE_BROWSER=false)")
    st = _st()
    if st.browser_sem is None:
        st.browser_sem = asyncio.Semaphore(BROWSER_CONCURRENCY)
    async with st.browser_sem:
        browser = await _get_browser()
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            await context.add_init_script(_STEALTH_JS)
            if block_resources:

                async def _route(route: Any) -> None:
                    if route.request.resource_type in _BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", _route)
            page = await context.new_page()
            yield page
        finally:
            with contextlib.suppress(Exception):
                await context.close()


async def _settle(page: Any) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=BROWSER_IDLE_TIMEOUT_MS)
    # Give JS challenges (Cloudflare etc.) a chance to resolve and redirect.
    for _ in range(4):
        try:
            html = await page.content()
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if not looks_like_challenge(html):
            return
        await asyncio.sleep(2.5)


async def browser_fetch(url: str) -> FetchResult:
    try:
        async with browser_page() as page:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_NAV_TIMEOUT_MS)
            await _settle(page)
            html = await page.content()
            status = resp.status if resp is not None else 200
            # After a solved challenge the final document is fine even if the first response was 403/503.
            if status >= 400 and not looks_like_challenge(html) and has_product_signals(html):
                status = 200
            return FetchResult(url=page.url, status=status, text=html, via_browser=True)
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"Browser fetch failed: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}") from e


async def browser_fetch_from_page(page_url: str, target_url: str, accept: str = "application/json") -> tuple[int, str, list[dict]]:
    """Open ``page_url`` in the browser, then fetch ``target_url`` from inside that page
    (same-origin XHR with the page's cookies). Returns (status, body, cookies)."""
    script = """
    async ([u, accept]) => {
        const r = await fetch(u, {credentials: 'include', headers: {'Accept': accept, 'x-skip-redirect': 'true'}});
        return {status: r.status, text: await r.text()};
    }
    """
    try:
        async with browser_page() as page:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=BROWSER_NAV_TIMEOUT_MS)
            await _settle(page)
            res = await page.evaluate(script, [target_url, accept])
            cookies = await page.context.cookies()
            return int(res.get("status") or 0), str(res.get("text") or ""), cookies
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"Browser fetch failed: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}") from e


# --------------------------------------------------------------------------- shutdown

_shutdown_hooks: list[Callable[[], Awaitable[None]]] = []


def register_shutdown(fn: Callable[[], Awaitable[None]]) -> None:
    if fn not in _shutdown_hooks:
        _shutdown_hooks.append(fn)


async def shutdown() -> None:
    global _state
    st = _state
    for fn in list(_shutdown_hooks):
        with contextlib.suppress(Exception):
            await fn()
    same_loop = False
    with contextlib.suppress(RuntimeError):
        same_loop = st.loop is asyncio.get_running_loop()
    if same_loop:
        if st.client is not None:
            with contextlib.suppress(Exception):
                await st.client.aclose()
        if st.browser is not None:
            with contextlib.suppress(Exception):
                await st.browser.close()
        if st.pw is not None:
            with contextlib.suppress(Exception):
                await st.pw.stop()
    _state = _State()
    _browser_hosts.clear()
