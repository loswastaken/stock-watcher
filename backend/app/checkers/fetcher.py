"""Shared page fetching: a pooled httpx client with browser-like headers plus a lazy,
reused headless Chromium (Playwright) for JS-rendered or bot-protected pages.

Everything here is event-loop aware: the singletons are re-created if they are used
from a different running loop (matters for tests; production has a single loop).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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

# Akamai / PerimeterX-heavy sites that stall or fingerprint plain Python TLS + HTTP/1.1:
# requests to these hosts (suffix match) go through curl_cffi with a Chrome TLS/HTTP2
# fingerprint when it is installed. STOCKWATCHER_IMPERSONATE=0 turns this off.
IMPERSONATE_HOSTS: frozenset[str] = frozenset({
    "bestbuy.com", "costco.com", "homedepot.com", "kohls.com", "macys.com", "lowes.com", "dell.com",
    "lenovo.com", "lg.com", "meijer.com", "bjs.com", "officedepot.com", "qvc.com", "verizon.com",
    "gamestop.com", "direct.playstation.com", "api.direct.playstation.com", "nike.com", "walmart.com",
    "samsclub.com",
})
IMPERSONATE_TARGET = "chrome"
CURL_TIMEOUT = (8.0, 20.0)  # (connect, total) seconds
# Recorded response bodies are capped at this many characters.
RECORD_BODY_LIMIT = 2 * 1024 * 1024


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
    # The site put us in a waiting room (queue-it, Imperva, Shopify throttle, PS Direct queue).
    queued: bool = False


def browser_enabled() -> bool:
    return os.environ.get("ENABLE_BROWSER", "true").strip().lower() not in {"0", "false", "no", "off", ""}


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# --------------------------------------------------------------------------- challenge / signal sniffing

# Markers that only appear on bot-challenge / block pages (checked in the first 60 kB).
_CHALLENGE_MARKERS = [
    re.compile(p, re.I)
    for p in (
        r"<title>\s*just a moment\.{0,3}\s*</title>",
        r"cf-browser-verification|cf_chl_opt|challenge-platform/h/",
        r"<title>\s*attention required!?\s*\|\s*cloudflare",
        r"incapsula incident id",
        r"captcha-delivery\.com|geo\.captcha-delivery",  # DataDome
        r"<title>\s*access denied\s*</title>[\s\S]{0,4000}reference\s*#",  # Akamai
        r"<title>\s*robot or human\?\s*</title>",  # Walmart / Sam's Club (PerimeterX)
        r"<title>\s*pardon our interruption",
        r"/cdn-cgi/challenge-platform/",
        r"sec-if-cpt-container|_sec/cp_challenge",  # Akamai bot manager
        r"<title>\s*(amazon\.com|amazon)\s*</title>[\s\S]{0,5000}(captcha|characters you see)",
        r"opfcaptcha|/errors/validateCaptcha",  # Amazon captcha
        r"checking your browser before you access ebay",
    )
]
# Markers that also occur on normal pages of protected sites (bot-manager script tags,
# review-form captchas, product copy): only trusted on small pages, which challenge pages are.
_SMALL_PAGE = 200_000
_SMALL_PAGE_MARKERS = [
    re.compile(p, re.I)
    for p in (
        # PerimeterX block page. (Its window._pxAppId / sensor script is on every page of a
        # protected site, so that alone is not a challenge.)
        r"px-captcha",
        r"access to this page has been denied",
        r"press\s*(?:&|&amp;)\s*hold\b[^<]{0,80}\bhuman",  # "Press & Hold to confirm you are a human"
        # Imperva block iframe (normal pages load _Incapsula_Resource?SWJIYLWA=... scripts).
        r"_Incapsula_Resource\?(?:CWUDNSAI|[^\"'\s>]*incident_id)",
        r"are you a robot\?|verify you are (a )?human|please verify you are a human",
        r"click the button below to continue shopping",  # Amazon soft block
        r"enter the characters you see below",  # Amazon captcha
        r"/splashui/challenge",  # eBay
        r"/areyouahuman",  # Newegg
    )
]
_CHALLENGE_PATH_RE = re.compile(
    r"^/blocked(?:/|$)|/splashui/challenge|^/areyouahuman|/errors/validatecaptcha", re.I
)


def looks_like_challenge(html: str, url: str | None = None) -> bool:
    """True for bot-challenge / block pages. ``url`` (the final URL after redirects) is
    checked too: Walmart/Sam's ``/blocked``, eBay ``/splashui/challenge``, Newegg ``/areyouahuman``."""
    if url and _CHALLENGE_PATH_RE.search(urlsplit(url).path or ""):
        return True
    html = html or ""
    head = html[:60_000]
    if any(p.search(head) for p in _CHALLENGE_MARKERS):
        return True
    return len(html) <= _SMALL_PAGE and any(p.search(head) for p in _SMALL_PAGE_MARKERS)


# --------------------------------------------------------------------------- waiting rooms

QUEUE_STATUS_TEXT = "Waiting room active — drop may be live"
_QUEUE_PAGE_MAX = 150_000
_QUEUE_BUY_RE = re.compile(r"add[\s-]+to[\s-]+(?:cart|bag|basket)|\"@type\"\s*:\s*\"Product\"", re.I)
_QUEUE_IT_RE = re.compile(r"static\.queue-it\.net/script|queue-it\.net|\bqueueit\b|queueit[._-]", re.I)
_QUEUE_WORDING_RE = re.compile(
    r"\bqueue\b|\bin\s+line\b|waiting\s+room|wait\s+time|place\s+in\s+line|number\s+in\s+line", re.I
)
_QUEUE_STRONG_RE = re.compile(
    r"you\s+are\s+(?:now\s+)?in\s+(?:the\s+)?(?:line|queue|waiting\s+room)"
    r"|<title>[^<]{0,80}waiting\s+room"
    r"|/throttle/queue"
    r"|direct-queue\.playstation\.com",
    re.I,
)


def looks_like_queue(html: str, final_url: str | None = None) -> bool:
    """True when the response is a virtual waiting room rather than the product page:
    queue-it (``*.queue-it.net`` or a small queue-it page), Imperva waiting room
    ("You are now in line"), Shopify ``/throttle/queue`` and PlayStation Direct's
    ``direct-queue.playstation.com``."""
    if final_url:
        parts = urlsplit(final_url)
        host = (parts.hostname or "").lower()
        if host == "queue-it.net" or host.endswith(".queue-it.net"):
            return True
        if host.startswith("direct-queue.") and host.endswith("playstation.com"):
            return True
        if (parts.path or "").startswith("/throttle/queue"):
            return True
    html = html or ""
    if not html or len(html) > _QUEUE_PAGE_MAX or _QUEUE_BUY_RE.search(html):
        return False
    if _QUEUE_STRONG_RE.search(html):
        return True
    if _QUEUE_IT_RE.search(html):
        # queue-it's script tag is also embedded on normal pages; require queue wording
        # outside of the queue-it identifiers themselves.
        rest = re.sub(r"queue-?it[\w.-]*", " ", html, flags=re.I)
        return bool(_QUEUE_WORDING_RE.search(rest))
    return False


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
        self.curl: Any = None  # curl_cffi AsyncSession (impersonating Chrome)
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


try:  # HTTP/2 needs the optional h2 package (httpx[http2])
    import h2  # type: ignore  # noqa: F401

    _HTTP2 = True
except ImportError:  # pragma: no cover - depends on environment
    _HTTP2 = False


def make_client(**kwargs: Any) -> httpx.AsyncClient:
    """A new AsyncClient with our defaults (callers own and must close it)."""
    opts: dict[str, Any] = dict(
        headers=DOCUMENT_HEADERS,
        timeout=TIMEOUT,
        follow_redirects=True,
        http2=_HTTP2,
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


# --------------------------------------------------------------------------- fetch recording

_recording: contextvars.ContextVar[list | None] = contextvars.ContextVar("stockwatcher_fetch_recording", default=None)


@contextlib.contextmanager
def recording() -> Iterator[list[dict]]:
    """Record every fetch made inside the block (in this task and tasks it spawns).

    Yields a list that receives one dict per request::

        {"method", "url", "final_url", "status", "via": "http"|"curl"|"browser",
         "headers": {...response headers}, "body": str (<= 2 MB), "elapsed_ms"}

    Failed requests are recorded with ``status`` None and an ``error`` message.
    """
    entries: list[dict] = []
    token = _recording.set(entries)
    try:
        yield entries
    finally:
        _recording.reset(token)


def _record(url: str, *, via: str, t0: float, status: int | None = None, final_url: str | None = None,
            headers: Any = None, body: str | None = None, error: str | None = None, method: str = "GET") -> None:
    entries = _recording.get()
    if entries is None:
        return
    try:
        hdrs = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    except Exception:  # noqa: BLE001 - recording must never break a fetch
        hdrs = {}
    entry: dict[str, Any] = {
        "method": method,
        "url": url,
        "final_url": final_url or url,
        "status": status,
        "via": via,
        "headers": hdrs,
        "body": (body or "")[:RECORD_BODY_LIMIT],
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
    if error:
        entry["error"] = error
    entries.append(entry)


def _response_text(resp: httpx.Response) -> str:
    try:
        return resp.text if resp.content else ""
    except Exception:  # noqa: BLE001 - undecodable body
        return ""


# --------------------------------------------------------------------------- impersonation (curl_cffi)

_curl_mod: Any = None
_curl_checked = False


def _curl_requests() -> Any:
    """The ``curl_cffi.requests`` module, or None when curl_cffi isn't installed."""
    global _curl_mod, _curl_checked
    if not _curl_checked:
        _curl_checked = True
        try:
            from curl_cffi import requests as curl_requests  # type: ignore

            _curl_mod = curl_requests
        except Exception:  # noqa: BLE001 - ImportError or a broken native lib
            _curl_mod = None
    return _curl_mod


def impersonation_enabled() -> bool:
    if os.environ.get("STOCKWATCHER_IMPERSONATE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return _curl_requests() is not None


def _host_matches(host: str, hosts: frozenset[str] | set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in hosts)


def wants_impersonation(url: str) -> bool:
    """Whether ``http_get(url)`` (without an explicit client) goes through curl_cffi."""
    return _host_matches(host_of(url), IMPERSONATE_HOSTS) and impersonation_enabled()


# Headers that make up the browser fingerprint: curl_cffi supplies ones matching its TLS
# fingerprint, so ours (claiming a different Chrome build) are dropped unless a caller
# explicitly set a different value.
_FP_HEADERS = {"user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
_DEFAULT_LOWER = {k.lower(): v for k, v in DOCUMENT_HEADERS.items()}


def _curl_headers(extra: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    lower_key: dict[str, str] = {}
    for src in (DOCUMENT_HEADERS, extra or {}):
        for k, v in src.items():
            lk = k.lower()
            if v is None or lk == "accept-encoding":  # curl negotiates + decodes itself
                continue
            if lk in _FP_HEADERS and v == _DEFAULT_LOWER.get(lk):
                continue
            if lk in lower_key:
                merged.pop(lower_key[lk], None)
            lower_key[lk] = k
            merged[k] = v
    return merged


def _get_curl_session() -> Any:
    st = _st()
    if st.curl is None:
        cr = _curl_requests()
        st.curl = cr.AsyncSession(impersonate=IMPERSONATE_TARGET, timeout=CURL_TIMEOUT, allow_redirects=True,
                                  max_redirects=10, max_clients=10)
    return st.curl


_STRIP_RESPONSE_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def _curl_to_httpx(r: Any, url: str) -> httpx.Response:
    """Wrap a curl_cffi response as an ``httpx.Response`` (body already decoded)."""
    try:
        items = list(r.headers.multi_items())
    except Exception:  # noqa: BLE001
        items = list(dict(r.headers or {}).items())
    items = [(k, v) for k, v in items if k and k.lower() not in _STRIP_RESPONSE_HEADERS and v is not None]
    final = str(getattr(r, "url", None) or url)
    return httpx.Response(int(r.status_code), headers=items, content=bytes(r.content or b""),
                          request=httpx.Request("GET", final))


async def _curl_get(url: str, headers: dict[str, str] | None) -> httpx.Response:
    session = _get_curl_session()
    cr = _curl_requests()
    timeout_exc: tuple[type, ...] = tuple(
        t for t in (getattr(getattr(cr, "exceptions", None), "Timeout", None),) if isinstance(t, type)
    )
    try:
        r = await session.get(url, headers=_curl_headers(headers))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - curl_cffi raises its own RequestException tree
        msg = (str(e).splitlines() or [""])[0][:200]
        if (timeout_exc and isinstance(e, timeout_exc)) or "timed out" in msg.lower() or "timeout" in type(e).__name__.lower():
            raise FetchError(f"Timed out fetching {host_of(url)}") from e
        raise FetchError(f"Network error: {type(e).__name__}: {msg}".rstrip(": ")) from e
    return _curl_to_httpx(r, url)


# --------------------------------------------------------------------------- plain HTTP


async def http_get(url: str, *, headers: dict[str, str] | None = None, client: httpx.AsyncClient | None = None) -> httpx.Response:
    """GET ``url`` politely (per-host slot + gap). Hosts in ``IMPERSONATE_HOSTS`` go through
    curl_cffi impersonating Chrome (unless a ``client`` is given or impersonation is off);
    the result is always an ``httpx.Response``. Raises FetchError on network failures."""
    via = "curl" if client is None and wants_impersonation(url) else "http"
    t0 = time.monotonic()
    async with host_slot(url):
        try:
            if via == "curl":
                resp = await _curl_get(url, headers)
            else:
                try:
                    resp = await (client or get_client()).get(url, headers=headers)
                except httpx.TimeoutException as e:
                    raise FetchError(f"Timed out fetching {host_of(url)}") from e
                except httpx.HTTPError as e:
                    raise FetchError(f"Network error: {type(e).__name__}: {e}".rstrip(": ")) from e
        except FetchError as e:
            _record(url, via=via, t0=t0, error=str(e))
            raise
    if _recording.get() is not None:
        _record(url, via=via, t0=t0, status=resp.status_code, final_url=str(resp.url), headers=resp.headers,
                body=_response_text(resp))
    return resp


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


def prepare_document_url(url: str) -> str:
    """Site tweaks for document fetches: Best Buy's international splash is skipped with
    ``intl=nosplash``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if host == "bestbuy.com" or host.endswith(".bestbuy.com"):
        q = parse_qsl(parts.query, keep_blank_values=True)
        if not any(k == "intl" for k, _ in q):
            q.append(("intl", "nosplash"))
            return urlunsplit(parts._replace(query=urlencode(q)))
    return url


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
    the browser. Raises ``FetchError`` for unrecoverable site problems. A virtual
    waiting room is not an error: the result comes back with ``queued=True``.
    """
    url = prepare_document_url(url)
    use_browser = browser_enabled()
    if use_browser and (render_js or _host_prefers_browser(url)):
        try:
            bres = await browser_fetch(url)
            if bres.queued:
                return bres
            if not looks_like_challenge(bres.text, bres.url):
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
        if bres.queued:
            return bres
        if bres.status < 400 and not looks_like_challenge(bres.text, bres.url):
            _mark_browser_host(url)
            return bres
        if looks_like_challenge(bres.text, bres.url):
            raise FetchError(f"Blocked by bot protection on {host_of(url)}", status=bres.status) from e
        raise FetchError(f"HTTP {bres.status} from {host_of(url)}", status=bres.status) from e
    text = _response_text(resp)
    final_url = str(resp.url)
    result = FetchResult(url=final_url, status=resp.status_code, text=text, headers=dict(resp.headers))
    if looks_like_queue(text, final_url):
        result.queued = True
        return result

    challenged = looks_like_challenge(text, final_url)
    reason: str | None = None
    if resp.status_code in RETRY_WITH_BROWSER_STATUSES:
        reason = f"HTTP {resp.status_code}"
    elif 200 <= resp.status_code < 300 and challenged:
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
        if bres is not None and bres.queued:
            return bres
        if bres is not None and bres.status < 400 and not looks_like_challenge(bres.text, bres.url):
            if reason != "no product signals":
                _mark_browser_host(url)  # skip the doomed plain request for a while
            return bres

    if challenged:
        raise FetchError(f"Blocked by bot protection on {host_of(url)}", status=resp.status_code)
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} from {host_of(url)}", status=resp.status_code)
    return result


# --------------------------------------------------------------------------- Playwright browser

_BLOCKED_RESOURCES = {"image", "font", "media"}
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""


_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
]


def _browser_channel() -> str | None:
    """Playwright channel: "chromium" runs the full Chromium build in the new headless mode
    (same fingerprint as headful Chrome); falls back to the headless shell if unavailable."""
    ch = os.environ.get("STOCKWATCHER_BROWSER_CHANNEL", "chromium").strip()
    return ch or None


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
            channel = _browser_channel()
            browser = None
            if channel:
                try:
                    browser = await st.pw.chromium.launch(headless=True, channel=channel, args=_LAUNCH_ARGS)
                except Exception as e:  # noqa: BLE001 - e.g. only the headless shell is installed
                    log.info("browser channel %r unavailable (%s); using the default headless build",
                             channel, (str(e).splitlines() or [""])[0])
            if browser is None:
                browser = await st.pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            st.browser = browser
        except Exception as e:
            raise FetchError(f"Could not start browser: {e}") from e
        return st.browser


def _ua_platform() -> tuple[str, str, str]:
    """(UA platform token, navigator.platform, sec-ch-ua-platform) for the real OS, using
    Chrome's reduced user-agent strings — claiming Windows from Linux is an easy tell."""
    if sys.platform == "darwin":
        return "Macintosh; Intel Mac OS X 10_15_7", "MacIntel", "macOS"
    if sys.platform.startswith("win"):
        return "Windows NT 10.0; Win64; x64", "Win32", "Windows"
    import platform as _platform

    machine = _platform.machine().lower()
    nav = "Linux aarch64" if machine in {"aarch64", "arm64"} else "Linux x86_64"
    return "X11; Linux x86_64", nav, "Linux"


def browser_identity(version: str | None) -> tuple[str, dict]:
    """(user agent, CDP userAgentMetadata) consistent with the real platform and the
    running Chromium's major version (without the "HeadlessChrome" token)."""
    major = (version or CHROME_MAJOR).split(".")[0] or CHROME_MAJOR
    token, _, ch_platform = _ua_platform()
    ua = f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    brands = [{"brand": "Google Chrome", "version": major}, {"brand": "Not?A_Brand", "version": "8"},
              {"brand": "Chromium", "version": major}]
    full = version or f"{major}.0.0.0"
    meta = {
        "brands": brands,
        "fullVersionList": [dict(b, version=full if b["brand"] != "Not?A_Brand" else "8.0.0.0") for b in brands],
        "fullVersion": full,
        "platform": ch_platform,
        "platformVersion": "",
        "architecture": "arm" if "aarch64" in _ua_platform()[1] else "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }
    return ua, meta


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
        version = None
        with contextlib.suppress(Exception):
            version = browser.version
        ua, ua_meta = browser_identity(version if isinstance(version, str) else None)
        context = await browser.new_context(
            user_agent=ua,
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
            # Client hints (sec-ch-ua*) that agree with the user agent and the real platform.
            with contextlib.suppress(Exception):
                cdp = await context.new_cdp_session(page)
                await cdp.send("Emulation.setUserAgentOverride", {
                    "userAgent": ua, "acceptLanguage": "en-US,en",
                    "platform": _ua_platform()[1], "userAgentMetadata": ua_meta,
                })
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
        if looks_like_queue(html, page.url) or not looks_like_challenge(html, page.url):
            return
        await asyncio.sleep(2.5)


def _browser_error(e: Exception) -> FetchError:
    first = str(e).splitlines()[0] if str(e) else ""
    return FetchError(f"Browser fetch failed: {type(e).__name__}: {first}")


async def browser_fetch(url: str) -> FetchResult:
    t0 = time.monotonic()
    try:
        async with browser_page() as page:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_NAV_TIMEOUT_MS)
            await _settle(page)
            html = await page.content()
            status = resp.status if resp is not None else 200
            final_url = page.url
            # After a solved challenge the final document is fine even if the first response was 403/503.
            if status >= 400 and not looks_like_challenge(html, final_url) and has_product_signals(html):
                status = 200
            headers: dict = {}
            with contextlib.suppress(Exception):
                headers = dict(resp.headers) if resp is not None else {}
            result = FetchResult(url=final_url, status=status, text=html, headers=headers, via_browser=True,
                                 queued=looks_like_queue(html, final_url))
            _record(url, via="browser", t0=t0, status=status, final_url=final_url, headers=headers, body=html)
            return result
    except FetchError as e:
        _record(url, via="browser", t0=t0, error=str(e))
        raise
    except Exception as e:
        err = _browser_error(e)
        _record(url, via="browser", t0=t0, error=str(err))
        raise err from e


async def browser_fetch_from_page(page_url: str, target_url: str, accept: str = "application/json") -> tuple[int, str, list[dict]]:
    """Open ``page_url`` in the browser, then fetch ``target_url`` from inside that page
    (same-origin XHR with the page's cookies). Returns (status, body, cookies)."""
    script = """
    async ([u, accept]) => {
        const r = await fetch(u, {credentials: 'include', headers: {'Accept': accept, 'x-skip-redirect': 'true'}});
        const headers = {};
        r.headers.forEach((v, k) => { headers[k] = v; });
        return {status: r.status, text: await r.text(), url: r.url, headers};
    }
    """
    t0 = time.monotonic()
    try:
        async with browser_page() as page:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=BROWSER_NAV_TIMEOUT_MS)
            await _settle(page)
            res = await page.evaluate(script, [target_url, accept])
            cookies = await page.context.cookies()
            status, text = int(res.get("status") or 0), str(res.get("text") or "")
            _record(target_url, via="browser", t0=t0, status=status, final_url=str(res.get("url") or target_url),
                    headers=res.get("headers") or {}, body=text)
            return status, text, cookies
    except FetchError as e:
        _record(target_url, via="browser", t0=t0, error=str(e))
        raise
    except Exception as e:
        err = _browser_error(e)
        _record(target_url, via="browser", t0=t0, error=str(err))
        raise err from e


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
        if st.curl is not None:
            with contextlib.suppress(Exception):
                await st.curl.close()
        if st.browser is not None:
            with contextlib.suppress(Exception):
                await st.browser.close()
        if st.pw is not None:
            with contextlib.suppress(Exception):
                await st.pw.stop()
    _state = _State()
    _browser_hosts.clear()
