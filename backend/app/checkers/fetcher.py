"""Shared page fetching: a pooled httpx client with browser-like headers plus a lazy,
reused real Chromium (patchright / Playwright; headed on a virtual display when possible,
with a persistent profile) for JS-rendered or bot-protected pages.

Everything here is event-loop aware: the singletons are re-created if they are used
from a different running loop (matters for tests; production has a single loop).
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import importlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
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
# curl_cffi reads a (connect, read) tuple as CONNECTTIMEOUT=connect, TIMEOUT=connect+read, so
# the second element is what's left of the total budget after connecting. Same budget as
# ``TIMEOUT`` for httpx: connect 8 s, 15 s for the whole request.
CURL_CONNECT_TIMEOUT = 8.0
CURL_TOTAL_TIMEOUT = 15.0
CURL_TIMEOUT = (CURL_CONNECT_TIMEOUT, CURL_TOTAL_TIMEOUT - CURL_CONNECT_TIMEOUT)
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
    # browser_fetch(capture=...): the page's own XHR/fetch responses, [{"url", "status", "body"}].
    captured: list[dict] = field(default_factory=list)


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
        r"cf-browser-verification|cf_chl_opt|__cf_chl_(?:f_|rt_)?tk=",  # Cloudflare interstitial
        r"<title>\s*attention required!?\s*\|\s*cloudflare",
        r"incapsula incident id",
        r"captcha-delivery\.com|geo\.captcha-delivery",  # DataDome
        r"<title>\s*access denied\s*</title>[\s\S]{0,4000}reference\s*#",  # Akamai
        r"<title>\s*robot or human\?\s*</title>",  # Walmart / Sam's Club (PerimeterX)
        r"<title>\s*pardon our interruption",
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
        # Cloudflare challenge assets. Normal pages of CF sites load the passive bot-detection
        # script ``/cdn-cgi/challenge-platform/[h/<x>/]scripts/jsd/main.js``: never a challenge.
        r"/cdn-cgi/challenge-platform/(?!(?:h/\w+/)?scripts/jsd/)",
        r"\bcf-chl-",
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
# A waiting room is never a product page: any of these in the response means it isn't one.
_QUEUE_BUY_RE = re.compile(
    r"add[\s-]+to[\s-]+(?:cart|bag|basket)"
    r"|[\"']@type[\"']\s*:\s*\[?\s*[\"'](?:Product|ProductGroup)[\"']"
    r"|itemtype\s*=\s*[\"']?https?://schema\.org/Product\b",
    re.I,
)
# Checked against the visible text only (i18n bundles in scripts mention these everywhere).
_QUEUE_PRODUCT_TEXT_RE = re.compile(r"sold[\s-]*out|out\s+of\s+stock", re.I)
_QUEUE_IT_RE = re.compile(r"static\.queue-it\.net/script|queue-it\.net|\bqueueit\b|queueit[._-]", re.I)
_QUEUE_WORDING_RE = re.compile(
    r"\bqueue\b|\bin\s+line\b|waiting\s+room|wait\s+time|place\s+in\s+line|number\s+in\s+line", re.I
)
# Waiting-room copy (matched against the visible text).
_QUEUE_STRONG_TEXT_RE = re.compile(
    r"you\s+are\s+(?:now\s+)?in\s+(?:the\s+)?(?:line|queue|waiting\s+room)", re.I
)
_QUEUE_TITLE_RE = re.compile(r"<title[^>]*>[^<]{0,80}waiting\s+room", re.I)
# Waiting-room URLs (redirect targets / meta refresh / form actions anywhere in the markup).
_QUEUE_STRONG_URL_RE = re.compile(r"/throttle/queue|direct-queue\.playstation\.com", re.I)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*>[\s\S]*?</\1\s*>", re.I)
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_TAG_RE = re.compile(r"<[^>]+>")


def _visible_text(html: str) -> str:
    """Rough visible text: script/style bodies, comments and tags removed."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _COMMENT_RE.sub(" ", text)
    return _TAG_RE.sub(" ", text)


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
    text = _visible_text(html)
    if _QUEUE_PRODUCT_TEXT_RE.search(text):
        return False
    if _QUEUE_STRONG_URL_RE.search(html) or _QUEUE_TITLE_RE.search(html) or _QUEUE_STRONG_TEXT_RE.search(text):
        return True
    if _QUEUE_IT_RE.search(html):
        # queue-it's connector script is also embedded on normal pages: require queue
        # wording in the page's visible text (not in script bodies or queue-it identifiers).
        rest = re.sub(r"queue-?it[\w.-]*", " ", text, flags=re.I)
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
        self.browser: Any = None  # Browser (None for some persistent contexts)
        self.context: Any = None  # the shared BrowserContext every page is opened in
        self.version: str | None = None
        self.info: dict[str, Any] = {}
        self.closed = False
        self.temp_profile: str | None = None
        self.launch_error: FetchError | None = None
        self.launch_failed_at = 0.0
        self.warmed: set[str] = set()  # hosts whose homepage was visited this session
        self.browser_lock: asyncio.Lock | None = None
        self.browser_sem: asyncio.Semaphore | None = None


_state = _State()
_browser_hosts: dict[str, float] = {}
# Registry "browser" stores whose plain request worked after the browser-first attempt
# didn't: they skip browser-first for BROWSER_HOST_TTL.
_plain_hosts: dict[str, float] = {}


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
def recording(*, reuse: bool = False) -> Iterator[list[dict]]:
    """Record every fetch made inside the block (in this task and tasks it spawns).
    With ``reuse=True`` an enclosing recording (if any) is shared instead of hidden.

    Yields a list that receives one dict per request::

        {"method", "url", "final_url", "status", "via": "http"|"curl"|"browser",
         "headers": {...response headers}, "body": str (<= 2 MB), "elapsed_ms"}

    Failed requests are recorded with ``status`` None and an ``error`` message.
    """
    current = _recording.get()
    if reuse and current is not None:
        yield current
        return
    entries: list[dict] = []
    token = _recording.set(entries)
    try:
        yield entries
    finally:
        _recording.reset(token)


# --------------------------------------------------------------------------- time budget

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("stockwatcher_fetch_deadline", default=None)


@contextlib.contextmanager
def deadline(seconds: float | None) -> Iterator[None]:
    """Fetches inside the block (this task and tasks it spawns) shrink their timeouts so
    they end within ``seconds``: a check/preview with an outer time cap gets a (possibly
    shorter) browser attempt that returns a page instead of being cancelled mid-way.
    Nested blocks keep the earlier deadline."""
    if seconds is None:
        yield
        return
    new = time.monotonic() + max(0.0, seconds)
    cur = _deadline.get()
    token = _deadline.set(new if cur is None else min(cur, new))
    try:
        yield
    finally:
        _deadline.reset(token)


def time_left() -> float | None:
    """Seconds until the current ``deadline`` (None when there is none)."""
    d = _deadline.get()
    return None if d is None else d - time.monotonic()


_MIN_REQUEST_TIME = 1.0  # don't start a plain request with less time than this left
_MIN_BROWSER_TIME = 6.0  # ...or a browser navigation


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


def _curl_headers(extra: dict[str, str | None] | None) -> dict[str, str | None]:
    """Headers for curl_cffi. A None value in ``extra`` removes that header: it is sent to
    curl as None ("Name:"), which also drops curl_cffi's own impersonation default."""
    merged: dict[str, str | None] = {}
    lower_key: dict[str, str] = {}
    for src in (DOCUMENT_HEADERS, extra or {}):
        for k, v in src.items():
            lk = k.lower()
            if lk == "accept-encoding":  # curl negotiates + decodes itself
                continue
            if v is None:
                if lk in lower_key:
                    merged.pop(lower_key.pop(lk), None)
                merged[k] = None
                continue
            if lk in _FP_HEADERS and v == _DEFAULT_LOWER.get(lk):
                continue
            if lk in lower_key:
                merged.pop(lower_key[lk], None)
            else:  # drop an earlier removal marker for the same header
                for mk in [mk for mk in merged if mk.lower() == lk]:
                    merged.pop(mk)
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


async def _curl_get(url: str, headers: dict[str, str | None] | None) -> httpx.Response:
    session = _get_curl_session()
    cr = _curl_requests()
    timeout_exc: tuple[type, ...] = tuple(
        t for t in (getattr(getattr(cr, "exceptions", None), "Timeout", None),) if isinstance(t, type)
    )
    kwargs: dict[str, Any] = {}
    left = time_left()
    if left is not None and left < CURL_TOTAL_TIMEOUT:
        connect = min(CURL_CONNECT_TIMEOUT, left)
        kwargs["timeout"] = (connect, max(left - connect, 0.1))  # curl: total = connect + read
    try:
        r = await session.get(url, headers=_curl_headers(headers), **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - curl_cffi raises its own RequestException tree
        msg = (str(e).splitlines() or [""])[0][:200]
        if (timeout_exc and isinstance(e, timeout_exc)) or "timed out" in msg.lower() or "timeout" in type(e).__name__.lower():
            raise FetchError(f"Timed out fetching {host_of(url)}") from e
        raise FetchError(f"Network error: {type(e).__name__}: {msg}".rstrip(": ")) from e
    return _curl_to_httpx(r, url)


# --------------------------------------------------------------------------- plain HTTP


def _httpx_request(client: httpx.AsyncClient, url: str, headers: dict[str, str | None] | None) -> httpx.Request:
    """A GET request with the client's default headers merged with ``headers``; a None
    value removes that header (httpx itself would re-add the client default)."""
    headers = headers or {}
    extra: dict[str, Any] = {}
    left = time_left()
    if left is not None and left < (TIMEOUT.read or 0):
        extra["timeout"] = httpx.Timeout(left, connect=min(TIMEOUT.connect or left, left))
    req = client.build_request("GET", url, headers={k: v for k, v in headers.items() if v is not None}, **extra)
    for k, v in headers.items():
        if v is None:
            req.headers.pop(k, None)
    return req


async def http_get(url: str, *, headers: dict[str, str | None] | None = None,
                   client: httpx.AsyncClient | None = None) -> httpx.Response:
    """GET ``url`` politely (per-host slot + gap). Hosts in ``IMPERSONATE_HOSTS`` go through
    curl_cffi impersonating Chrome (unless a ``client`` is given or impersonation is off);
    the result is always an ``httpx.Response``. Raises FetchError on network failures.
    A None header value removes that default header (e.g. ``Upgrade-Insecure-Requests``)."""
    via = "curl" if client is None and wants_impersonation(url) else "http"
    t0 = time.monotonic()
    async with host_slot(url):
        try:
            left = time_left()
            if left is not None and left < _MIN_REQUEST_TIME:
                raise FetchError(f"Timed out fetching {host_of(url)} (time budget used up)")
            if via == "curl":
                resp = await _curl_get(url, headers)
            else:
                try:
                    cl = client or get_client()
                    resp = await cl.send(_httpx_request(cl, url, headers))
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
    _plain_hosts.pop(host_of(url), None)


def _host_prefers_browser(url: str) -> bool:
    exp = _browser_hosts.get(host_of(url))
    if exp is None:
        return False
    if exp < time.monotonic():
        _browser_hosts.pop(host_of(url), None)
        return False
    return True


def _retailer_needs_browser(url: str) -> bool:
    """The registry marks the store as needing a real browser (``browser=True``)."""
    try:
        from .retailers.registry import match_retailer

        r = match_retailer(url)
    except Exception:  # noqa: BLE001 - registry trouble must not break fetching
        return False
    return bool(r is not None and getattr(r, "browser", False))


def prefers_browser(url: str) -> bool:
    """Go straight to the browser (with its persisted cookies) instead of a plain request that
    would be blocked: the host blocked plain HTTP recently, or is a known hard store (unless
    plain HTTP recently worked for it where the browser didn't)."""
    if _host_prefers_browser(url):
        return True
    exp = _plain_hosts.get(host_of(url))
    if exp is not None:
        if exp >= time.monotonic():
            return False
        _plain_hosts.pop(host_of(url), None)
    return _retailer_needs_browser(url)


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

    Uses plain HTTP first; falls back to the real browser when the response is
    403/429/503, looks like a bot challenge, or ``needs(html)`` is False (content lacks
    the signals the caller needs — typical for JS-rendered pages). ``render_js`` forces
    the browser; hosts that blocked plain HTTP recently and stores the registry marks
    ``browser=True`` try the browser first. Raises ``FetchError`` for unrecoverable site
    problems. A virtual waiting room is not an error: the result comes back with ``queued=True``.
    """
    url = prepare_document_url(url)
    use_browser = browser_enabled()
    browser_first = use_browser and not render_js and prefers_browser(url)
    if use_browser and (render_js or browser_first):
        # The browser already had its go: another attempt after the plain request would
        # only double the worst case (~2 x 45 s) and blow the check's time budget.
        use_browser = False
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
    if browser_first and not _host_prefers_browser(url) and (needs is None or needs(text)):
        _plain_hosts[host_of(url)] = time.monotonic() + BROWSER_HOST_TTL
    return result


# --------------------------------------------------------------------------- real browser
#
# One Chromium per process, driven by patchright (a Playwright fork without the CDP /
# automation leaks bot managers look for) when it is installed, else Playwright:
#
# * mode (BROWSER_MODE=auto|headed|headless): headed when there is a display (DISPLAY set,
#   macOS, Windows); on a headless Linux box (Docker) headed on a private Xvfb display we
#   start ourselves; headless only when neither is possible. A headed browser reports its
#   real user agent and client hints, so nothing is overridden there.
# * one persistent profile ($DATA_DIR/browser-profile/<channel>) shared by every fetch, so
#   the cookies a site hands out after its bot check (cf_clearance, _abck, ...) survive
#   between checks and restarts; each fetch gets its own page (tab).
# * BROWSER_CDP_URL attaches to a real Chrome the user runs instead (their Mac's Chrome, a
#   linuxserver/chromium container where a captcha can be solved by hand).
# * BROWSER_CHANNEL=chrome launches the installed Google Chrome instead of bundled Chromium.

_BLOCKED_RESOURCES = {"image", "font", "media"}
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

_BASE_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--lang=en-US",
    "--disk-cache-size=67108864",  # the profile persists: keep its cache bounded (64 MB)
]
# Plain Playwright only (patchright already hides these).
_PLAYWRIGHT_ARGS = ["--disable-blink-features=AutomationControlled"]
_LAUNCH_ARGS = _BASE_ARGS + _PLAYWRIGHT_ARGS  # kept for compatibility
_WINDOW = (1440, 900)
_XVFB_SCREEN = "1920x1080x24"

BROWSER_CHALLENGE_WAIT = 20.0  # default for BROWSER_CHALLENGE_WAIT (seconds)
_CHALLENGE_POLL = 1.0
_RELOAD_AFTER = 3.0  # seconds into a challenge before the one reload (Akamai / Imperva)
_INTERACTIVE_WAIT = 5.0  # captcha-style blocks can't clear on their own: don't wait long
_CAPTURE_LIMIT = 2 * 1024 * 1024
_CAPTURE_TYPES = ("json", "text", "javascript", "xml")
_LAUNCH_RETRY_AFTER = 60.0  # after a failed launch, fail fast for this long

MODES = ("auto", "headed", "headless")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(_env(name, str(default))))
    except ValueError:
        return default


def browser_challenge_wait() -> float:
    """Seconds a bot challenge gets to clear on its own (BROWSER_CHALLENGE_WAIT, default 20)."""
    return _env_float("BROWSER_CHALLENGE_WAIT", BROWSER_CHALLENGE_WAIT)


def browser_concurrency() -> int:
    try:
        return max(1, int(_env("BROWSER_CONCURRENCY", str(BROWSER_CONCURRENCY))))
    except ValueError:
        return BROWSER_CONCURRENCY


def browser_cdp_url() -> str | None:
    return _env("BROWSER_CDP_URL") or None


def _browser_channel() -> str | None:
    """Channel to launch: BROWSER_CHANNEL (e.g. "chrome" = installed Google Chrome), else the
    full bundled "chromium" build (the same browser headed and in new-headless mode)."""
    ch = _env("BROWSER_CHANNEL") or _env("STOCKWATCHER_BROWSER_CHANNEL", "chromium")
    return ch or None


def engine_names() -> list[str]:
    """Importable automation libraries in preference order. BROWSER_ENGINE=playwright or
    =patchright forces one."""
    want = _env("BROWSER_ENGINE", "auto").lower()
    names = [want] if want in ("patchright", "playwright") else ["patchright", "playwright"]
    return [n for n in names if importlib.util.find_spec(n) is not None]


def _engine_factory(name: str) -> Callable[[], Any]:
    return importlib.import_module(f"{name}.async_api").async_playwright


def _has_display(platform: str, environ: Any) -> bool:
    return platform == "darwin" or platform.startswith("win") or bool(environ.get("DISPLAY"))


def select_mode(mode: str | None = None, *, platform: str | None = None, environ: Any = None,
                which: Callable[[str], str | None] = shutil.which) -> tuple[str, str | None]:
    """How to run the browser: ("headed", None) on an existing display, ("xvfb", <Xvfb path>)
    for headed on a virtual display we start, or ("headless", None)."""
    environ = os.environ if environ is None else environ
    mode = (mode if mode is not None else environ.get("BROWSER_MODE", "auto") or "auto").strip().lower()
    if mode not in MODES:
        log.warning("BROWSER_MODE=%r is not one of %s; using auto", mode, "|".join(MODES))
        mode = "auto"
    if mode == "headless":
        return "headless", None
    if _has_display(platform or sys.platform, environ):
        return "headed", None
    xvfb = which("Xvfb")
    if xvfb:
        return "xvfb", xvfb
    if mode == "headed":
        log.warning("BROWSER_MODE=headed but there is no display and no Xvfb; running headless")
    return "headless", None


def chrome_installed() -> bool:
    """Whether Google Chrome (the "chrome" channel) is installed in its standard location."""
    if sys.platform == "darwin":
        paths = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    elif sys.platform.startswith("win"):
        paths = [os.path.join(os.environ.get(v, ""), "Google", "Chrome", "Application", "chrome.exe")
                 for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(v)]
    else:
        paths = ["/opt/google/chrome/chrome"]
    return any(os.path.exists(p) for p in paths)


# --------------------------------------------------------------------------- virtual display


class VirtualDisplay:
    """An Xvfb server on the first free display number (from :99), started on first use and
    stopped by ``shutdown()`` (or at exit)."""

    def __init__(self, binary: str = "Xvfb", *, screen: str = _XVFB_SCREEN, first: int = 99, tries: int = 20,
                 popen: Callable[..., Any] = subprocess.Popen, exists: Callable[[str], bool] = os.path.lexists,
                 sleep: Callable[[float], Any] = time.sleep, ready_timeout: float = 5.0) -> None:
        self.binary, self.screen, self.first, self.tries = binary, screen, first, tries
        self._popen, self._exists, self._sleep, self.ready_timeout = popen, exists, sleep, ready_timeout
        self.proc: Any = None
        self.display: str | None = None
        self._atexit = False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _free(self, n: int) -> bool:
        return not self._exists(f"/tmp/.X{n}-lock") and not self._exists(f"/tmp/.X11-unix/X{n}")

    def start(self) -> str:
        if self.running():
            assert self.display
            return self.display
        self.proc = self.display = None
        for n in range(self.first, self.first + self.tries):
            if not self._free(n):
                continue
            proc = self._popen([self.binary, f":{n}", "-screen", "0", self.screen, "-nolisten", "tcp"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
            end = time.monotonic() + self.ready_timeout
            while time.monotonic() < end:
                if proc.poll() is not None:
                    break  # lost a race for this display number: try the next one
                if self._exists(f"/tmp/.X11-unix/X{n}"):
                    self.proc, self.display = proc, f":{n}"
                    if not self._atexit:
                        atexit.register(self.stop)
                        self._atexit = True
                    log.info("started Xvfb on display :%d", n)
                    return self.display
                self._sleep(0.05)
            self._kill(proc)
        raise RuntimeError(f"could not start {self.binary} (no free display in :{self.first}..:{self.first + self.tries - 1})")

    @staticmethod
    def _kill(proc: Any) -> None:
        with contextlib.suppress(Exception):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def stop(self) -> None:
        if self.proc is not None:
            self._kill(self.proc)
            log.info("stopped Xvfb on display %s", self.display)
        self.proc = self.display = None


_xvfb: VirtualDisplay | None = None


# --------------------------------------------------------------------------- persistent profile

_PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")


def browser_profile_dir(channel: str | None = None) -> Path:
    """Where the persistent profile lives: BROWSER_PROFILE_DIR, else $DATA_DIR/browser-profile;
    one sub-folder per browser build (Chrome and bundled Chromium can't share a profile)."""
    base = _env("BROWSER_PROFILE_DIR") or os.path.join(_env("DATA_DIR", "/data"), "browser-profile")
    return Path(base) / re.sub(r"[^\w.-]+", "_", channel or "chromium")


def _pid_alive(pid: int) -> bool:
    """A live Chromium process with this pid (a reused pid of something else doesn't count)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"chrom" in fh.read().lower()
    except OSError:
        return True  # no /proc (macOS): assume it's still the browser


def clear_stale_profile_locks(profile: Path, *, hostname: str | None = None,
                              pid_alive: Callable[[int], bool] = _pid_alive) -> bool:
    """Remove the Singleton* lock files a crashed or killed browser left in ``profile`` (a
    recreated container has a new hostname, so Chromium itself would refuse the profile as
    "in use on another computer"). Returns False, leaving them alone, when a live browser on
    this host still holds the profile."""
    lock = profile / "SingletonLock"
    target = None
    with contextlib.suppress(OSError):
        target = os.readlink(lock)  # "<hostname>-<pid>"
    if target:
        host, _, pid = target.rpartition("-")
        if host == (hostname or socket.gethostname()) and pid.isdigit() and pid_alive(int(pid)):
            return False
    removed = []
    for name in _PROFILE_LOCKS:
        p = profile / name
        if os.path.lexists(p):
            with contextlib.suppress(OSError):
                os.unlink(p)
                removed.append(name)
    if removed:
        log.info("removed stale browser profile locks in %s: %s", profile, ", ".join(removed))
        _mark_clean_exit(profile)
    return True


def _mark_clean_exit(profile: Path) -> None:
    """Stop Chromium showing "restore pages?" after a crash (best effort)."""
    prefs = profile / "Default" / "Preferences"
    with contextlib.suppress(Exception):
        data = json.loads(prefs.read_text(encoding="utf-8"))
        prof = data.setdefault("profile", {})
        if prof.get("exit_type") != "Normal" or prof.get("exited_cleanly") is not True:
            prof["exit_type"], prof["exited_cleanly"] = "Normal", True
            prefs.write_text(json.dumps(data), encoding="utf-8")


def _prepare_profile(channel: str | None) -> tuple[str, bool]:
    """(user data dir, persistent?). Falls back to a throwaway profile when the persistent
    one can't be created or is held by another running browser."""
    profile = browser_profile_dir(channel)
    try:
        profile.mkdir(parents=True, exist_ok=True)
        if clear_stale_profile_locks(profile):
            return str(profile), True
        log.warning("browser profile %s is in use by another browser; using a temporary profile", profile)
    except OSError as e:
        log.warning("browser profile %s unusable (%s); using a temporary profile", profile, e)
    return tempfile.mkdtemp(prefix="stockwatcher-browser-"), False


# --------------------------------------------------------------------------- launch / connect

_last_info: dict[str, Any] = {}


def browser_info() -> dict[str, Any]:
    """Which engine / mode the browser runs (or would run) with, for logs and the site probe:
    ``{"engine", "mode", "channel", "version", "persistent_profile", "cdp"}``. ``mode`` is
    "headed", "headed-xvfb", "headless" or "cdp". Values from the last launch are kept
    after ``shutdown()``."""
    engines = engine_names()
    if browser_cdp_url():
        mode = "cdp"
    else:
        m = select_mode()[0]
        mode = "headed-xvfb" if m == "xvfb" else m
    info: dict[str, Any] = {"engine": engines[0] if engines else None, "mode": mode,
                            "channel": None if mode == "cdp" else _browser_channel(), "version": None,
                            "persistent_profile": mode != "cdp", "cdp": bool(browser_cdp_url()),
                            "launched": False}
    info.update(_last_info)
    return info


def _first_line(e: BaseException) -> str:
    return (str(e).splitlines() or [""])[0][:300]


def _cdp_endpoint(url: str) -> str:
    """Chrome's DevTools HTTP endpoint only answers when the Host header is an IP address or
    localhost: resolve a name like ``http://chromium:9223`` to its IP first."""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host or host == "localhost":
        return url
    try:
        ipaddress.ip_address(host)
        return url
    except ValueError:
        pass
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return url
    netloc = ip + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


async def _connect_cdp(st: _State, url: str) -> None:
    engines = engine_names()
    if not engines:
        raise FetchError("Browser fallback unavailable (neither patchright nor playwright is installed)")
    endpoint = await asyncio.to_thread(_cdp_endpoint, url)
    last: Exception | None = None
    for name in engines:
        pw = await _engine_factory(name)().start()
        try:
            browser = await pw.chromium.connect_over_cdp(endpoint, timeout=15_000)
        except Exception as e:  # noqa: BLE001
            last = e
            with contextlib.suppress(Exception):
                await pw.stop()
            continue
        # The Chrome's own (default) context: its cookies are what a hand-solved captcha left.
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        st.pw, st.browser, st.context = pw, browser, ctx
        st.info = {"engine": name, "mode": "cdp", "channel": None, "version": _version(browser),
                   "persistent_profile": False, "cdp": True, "launched": True}
        browser.on("disconnected", lambda *_: _mark_closed(st))
        return
    raise FetchError(f"Could not connect to the browser at {url}: {_first_line(last) if last else 'unknown error'}")


def _version(browser: Any) -> str | None:
    with contextlib.suppress(Exception):
        v = browser.version
        if isinstance(v, str):
            return v
    return None


def _mark_closed(st: _State) -> None:
    st.closed = True


def _context_alive(st: _State) -> bool:
    return st.context is not None and not st.closed


async def _launch(st: _State) -> None:
    global _xvfb
    engines = engine_names()
    if not engines:
        raise FetchError("Browser fallback unavailable (neither patchright nor playwright is installed)")
    mode, xvfb_bin = select_mode()
    env: dict[str, str] | None = None
    if mode == "xvfb":
        try:
            if _xvfb is None or _xvfb.binary != xvfb_bin:
                _xvfb = VirtualDisplay(xvfb_bin or "Xvfb")
            display = await asyncio.to_thread(_xvfb.start)
            env = {**os.environ, "DISPLAY": display}
            mode = "headed-xvfb"
        except Exception as e:  # noqa: BLE001
            log.warning("virtual display unavailable (%s); running the browser headless", _first_line(e))
            mode = "headless"
    headless = mode == "headless"
    channel = _browser_channel()
    user_data_dir, persistent = _prepare_profile(channel)
    st.temp_profile = None if persistent else user_data_dir

    last: Exception | None = None
    for name in engines:
        pw = await _engine_factory(name)().start()
        args = list(_BASE_ARGS) + ([] if name == "patchright" else _PLAYWRIGHT_ARGS)
        opts: dict[str, Any] = {"headless": headless, "args": args, "accept_downloads": False}
        if name != "patchright":
            opts["ignore_default_args"] = ["--enable-automation"]
        if env is not None:
            opts["env"] = env
        if headless:
            opts.update(viewport={"width": 1366, "height": 900}, locale="en-US",
                        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        else:  # a real window: its size is the viewport, like a person's browser
            opts.update(no_viewport=True)
            args.append(f"--window-size={_WINDOW[0]},{_WINDOW[1]}")
        ctx = None
        used_channel = channel
        for ch in ([channel, None] if channel else [None]):
            try:
                ctx = await pw.chromium.launch_persistent_context(user_data_dir, channel=ch, **opts) if ch else \
                    await pw.chromium.launch_persistent_context(user_data_dir, **opts)
                used_channel = ch
                break
            except Exception as e:  # noqa: BLE001 - e.g. Chrome not installed, browser build missing
                last = e
                if ch:
                    log.info("browser channel %r unavailable with %s (%s); trying the bundled build", ch, name,
                             _first_line(e))
                # A failed launch can leave its lock behind.
                with contextlib.suppress(Exception):
                    clear_stale_profile_locks(Path(user_data_dir))
        if ctx is None:
            log.info("%s could not start a browser: %s", name, _first_line(last) if last else "")
            with contextlib.suppress(Exception):
                await pw.stop()
            continue
        if name != "patchright" and headless:
            with contextlib.suppress(Exception):
                await ctx.add_init_script(_STEALTH_JS)
        st.pw, st.context, st.browser = pw, ctx, getattr(ctx, "browser", None)
        version = _version(st.browser) if st.browser is not None else None
        st.version = version
        st.info = {"engine": name, "mode": mode, "channel": used_channel or "chromium", "version": version,
                   "persistent_profile": persistent, "cdp": False, "launched": True}
        ctx.on("close", lambda *_: _mark_closed(st))
        log.info("browser started: %s", st.info)
        return
    raise FetchError(f"Could not start browser: {_first_line(last) if last else 'unknown error'}")


async def _get_browser() -> Any:
    """The shared browser context (launched or connected on first use, relaunched if it died)."""
    st = _st()
    if st.browser_lock is None:
        st.browser_lock = asyncio.Lock()
    async with st.browser_lock:
        if _context_alive(st):
            return st.context
        if st.launch_error is not None and time.monotonic() - st.launch_failed_at < _LAUNCH_RETRY_AFTER:
            raise st.launch_error
        await _close_browser(st)
        st.closed = False
        try:
            cdp = browser_cdp_url()
            if cdp:
                await _connect_cdp(st, cdp)
            else:
                await _launch(st)
        except FetchError as e:
            st.launch_error, st.launch_failed_at = e, time.monotonic()
            raise
        except Exception as e:  # noqa: BLE001
            err = FetchError(f"Could not start browser: {_first_line(e)}")
            st.launch_error, st.launch_failed_at = err, time.monotonic()
            raise err from e
        st.launch_error = None
        _last_info.clear()
        _last_info.update(st.info)
        return st.context


async def _close_browser(st: _State) -> None:
    """Close what this state launched (a CDP-connected Chrome is only disconnected)."""
    if st.info.get("mode") == "cdp":
        if st.browser is not None:
            with contextlib.suppress(Exception):
                await st.browser.close()  # disconnects; the user's Chrome keeps running
    elif st.context is not None:
        with contextlib.suppress(Exception):
            await st.context.close()
    if st.pw is not None:
        with contextlib.suppress(Exception):
            await st.pw.stop()
    if st.temp_profile:
        shutil.rmtree(st.temp_profile, ignore_errors=True)
    st.pw = st.browser = st.context = st.temp_profile = None


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
    running Chromium's major version (without the "HeadlessChrome" token). Only used in
    headless mode: a headed browser reports itself."""
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


async def _block_route(route: Any) -> None:
    try:
        if route.request.resource_type in _BLOCKED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:  # noqa: BLE001 - page closed mid-request
        pass


@contextlib.asynccontextmanager
async def browser_page(block_resources: bool = True):
    """Yield a new page (tab) in the shared browser context; closed afterwards. Cookies
    persist in the context's profile across fetches."""
    if not browser_enabled():
        raise FetchError("Browser fallback disabled (ENABLE_BROWSER=false)")
    st = _st()
    if st.browser_sem is None:
        st.browser_sem = asyncio.Semaphore(browser_concurrency())
    async with st.browser_sem:
        ctx = await _get_browser()
        page = await ctx.new_page()
        try:
            mode = st.info.get("mode")
            if block_resources and mode != "cdp":
                await page.route("**/*", _block_route)
            if mode == "headless":
                # Headless Chromium says "HeadlessChrome": present the matching regular Chrome,
                # with client hints (sec-ch-ua*) that agree with it and the real platform.
                ua, ua_meta = browser_identity(st.version)
                with contextlib.suppress(Exception):
                    cdp = await ctx.new_cdp_session(page)
                    await cdp.send("Emulation.setUserAgentOverride", {
                        "userAgent": ua, "acceptLanguage": "en-US,en",
                        "platform": _ua_platform()[1], "userAgentMetadata": ua_meta,
                    })
            yield page
        finally:
            with contextlib.suppress(Exception):
                await page.close()


def _clamp_ms(default_ms: int, reserve: float = 0.0) -> int:
    """``default_ms`` shrunk to what's left of the current ``deadline`` minus ``reserve`` s."""
    left = time_left()
    if left is None:
        return default_ms
    return max(0, min(default_ms, int((left - reserve) * 1000)))


def _nav_timeout_ms(url: str) -> int:
    ms = _clamp_ms(BROWSER_NAV_TIMEOUT_MS, reserve=2.0)
    if ms < _MIN_BROWSER_TIME * 1000:
        raise FetchError(f"Timed out fetching {host_of(url)} (no time left for the browser)")
    return ms


async def _settle(page: Any, idle_ms: int = BROWSER_IDLE_TIMEOUT_MS) -> None:
    idle_ms = _clamp_ms(idle_ms, reserve=2.0)
    if idle_ms > 0:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=idle_ms)


# --------------------------------------------------------------------------- challenges


def challenge_vendor(html: str) -> str:
    """Rough bot-manager family of a challenge page (decides how to wait it out)."""
    h = (html or "")[:60_000].lower()
    if "cf_chl" in h or "cf-chl" in h or "/cdn-cgi/challenge-platform/" in h or "just a moment" in h \
            or ("attention required" in h and "cloudflare" in h):
        return "cloudflare"
    if "incapsula" in h:
        return "imperva"
    if "captcha-delivery.com" in h:
        return "datadome"
    if "px-captcha" in h or "perimeterx" in h or "press &amp; hold" in h or "press & hold" in h:
        return "perimeterx"
    if "sec-if-cpt-container" in h or "_sec/cp_challenge" in h or ("access denied" in h and "reference #" in h):
        return "akamai"
    return "other"


# Challenges that clear by themselves after a reload once the sensor script has run.
_RELOAD_VENDORS = {"akamai", "imperva", "other"}
# Challenges that need a person (press & hold, captcha): only worth waiting for when a person
# can see the browser (BROWSER_CDP_URL).
_INTERACTIVE_VENDORS = {"perimeterx", "datadome"}


async def _content(page: Any) -> str | None:
    try:
        return await page.content()
    except Exception:  # noqa: BLE001 - navigating (the challenge redirected) or page gone
        return None


def _is_blocked(html: str, url: str | None) -> bool:
    return not looks_like_queue(html, url) and looks_like_challenge(html, url)


async def wait_out_challenge(page: Any, *, wait: float | None = None, interactive: bool = False,
                             sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                             clock: Callable[[], float] = time.monotonic) -> str:
    """If ``page`` shows a bot challenge, keep it open and poll until it clears (Cloudflare's
    JS challenge navigates by itself; Akamai / Imperva get one reload after their sensor has
    run), for up to ``wait`` seconds (BROWSER_CHALLENGE_WAIT) within the current deadline.
    Returns the page's HTML at the end (still the challenge when it never cleared)."""
    html = await _content(page) or ""
    if not _is_blocked(html, page.url):
        return html
    vendor = challenge_vendor(html)
    budget = browser_challenge_wait() if wait is None else wait
    if vendor in _INTERACTIVE_VENDORS and not interactive:
        budget = min(budget, _INTERACTIVE_WAIT)
    left = time_left()
    if left is not None:
        budget = min(budget, left - 3.0)
    start = clock()
    end = start + budget
    reloaded = False
    log.info("bot challenge (%s) on %s; waiting up to %.0f s", vendor, host_of(page.url or ""), max(budget, 0))
    while clock() < end:
        await sleep(min(_CHALLENGE_POLL, max(0.0, end - clock())))
        cur = await _content(page)
        if cur is None:
            continue
        html = cur
        if not _is_blocked(html, page.url):
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=max(1, _clamp_ms(10_000, reserve=2.0)))
            await _settle(page, idle_ms=3_000)
            final = await _content(page)
            log.info("bot challenge on %s cleared after %.1f s", host_of(page.url or ""), clock() - start)
            return final or html
        if (vendor in _RELOAD_VENDORS and not reloaded and clock() - start >= _RELOAD_AFTER
                and end - clock() > 6.0):
            reloaded = True
            with contextlib.suppress(Exception):
                await page.reload(wait_until="domcontentloaded", timeout=max(1, _clamp_ms(15_000, reserve=3.0)))
    return html


def _browser_error(e: Exception) -> FetchError:
    first = str(e).splitlines()[0] if str(e) else ""
    return FetchError(f"Browser fetch failed: {type(e).__name__}: {first}")


class _MainDocument:
    """Tracks the page's latest main-frame document response (the real page after a
    challenge navigated away from the interstitial)."""

    def __init__(self, page: Any, first: Any = None) -> None:
        self.page, self.resp = page, first

    def __call__(self, resp: Any) -> None:
        with contextlib.suppress(Exception):
            req = resp.request
            if req.is_navigation_request() and req.frame == self.page.main_frame:
                self.resp = resp


async def _read_captured(resp: Any, out: list[dict], t0: float) -> None:
    try:
        headers = await resp.all_headers() if hasattr(resp, "all_headers") else dict(resp.headers)
    except Exception:  # noqa: BLE001
        headers = dict(getattr(resp, "headers", {}) or {})
    ctype = str(headers.get("content-type", "")).lower()
    if ctype and not any(t in ctype for t in _CAPTURE_TYPES):
        return
    with contextlib.suppress(ValueError, TypeError):
        if int(headers.get("content-length") or 0) > _CAPTURE_LIMIT:
            return
    try:
        body = await resp.body()
    except Exception:  # noqa: BLE001 - redirect, page closed, body evicted
        return
    if len(body) > _CAPTURE_LIMIT:
        return
    text = body.decode("utf-8", "replace")
    out.append({"url": resp.url, "status": resp.status, "body": text})
    _record(resp.url, via="browser", t0=t0, status=resp.status, headers=headers, body=text)


def _capture_listener(capture: Callable[[str], bool], out: list[dict], pending: list[asyncio.Future],
                      t0: float) -> Callable[[Any], None]:
    """A page "response" handler collecting XHR/fetch responses whose URL ``capture`` accepts."""

    def on_response(resp: Any) -> None:
        try:
            if resp.request.resource_type not in ("xhr", "fetch") or not capture(resp.url):
                return
        except Exception:  # noqa: BLE001 - a broken predicate must not break the fetch
            return
        pending.append(asyncio.ensure_future(_read_captured(resp, out, t0)))

    return on_response


def _home_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/", "", ""))


async def _open(page: Any, url: str, *, interactive: bool) -> tuple[Any, str]:
    """Navigate, let the page settle and wait out a challenge. Returns (response, html)."""
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=_nav_timeout_ms(url))
    await _settle(page)
    html = await wait_out_challenge(page, interactive=interactive)
    return resp, html


async def browser_fetch(url: str, *, capture: Callable[[str], bool] | None = None) -> FetchResult:
    """Load ``url`` in the real browser and return the rendered HTML. A bot challenge gets
    ``BROWSER_CHALLENGE_WAIT`` s to clear; if it doesn't, the site's homepage is visited once
    per host per session (to collect its cookies) and the page tried again, time permitting.

    ``capture`` (a URL predicate) collects the page's own XHR/fetch responses (JSON/text up to
    2 MB) into ``FetchResult.captured`` as ``{"url", "status", "body"}``."""
    t0 = time.monotonic()
    st = _st()
    captured: list[dict] = []
    pending: list[asyncio.Future] = []
    try:
        async with browser_page() as page:
            interactive = st.info.get("mode") == "cdp"
            if capture is not None:
                page.on("response", _capture_listener(capture, captured, pending, t0))
            main = _MainDocument(page)
            page.on("response", main)
            resp, html = await _open(page, url, interactive=interactive)
            resp = main.resp or resp
            final_url = page.url
            host = host_of(url)
            left = time_left()
            if (_is_blocked(html, final_url) and host not in st.warmed
                    and (left is None or left > 2 * _MIN_BROWSER_TIME + 4)):
                st.warmed.add(host)
                home = _home_url(url)
                if home.rstrip("/") != url.rstrip("/"):
                    log.info("still blocked on %s: warming up on %s and retrying", host, home)
                    try:
                        await _open(page, home, interactive=interactive)
                        main.resp = None
                        resp2, html2 = await _open(page, url, interactive=interactive)
                        resp, html, final_url = main.resp or resp2, html2, page.url
                    except Exception as e:  # noqa: BLE001 - keep the first attempt's page
                        log.info("warm-up for %s failed: %s", host, _first_line(e))
            doc = resp
            status = doc.status if doc is not None else 200
            # After a solved challenge the final document is fine even if the first response was 403/503.
            if status >= 400 and not looks_like_challenge(html, final_url) and has_product_signals(html):
                status = 200
            headers: dict = {}
            with contextlib.suppress(Exception):
                headers = dict(doc.headers) if doc is not None else {}
            if pending:
                with contextlib.suppress(Exception):
                    await asyncio.wait(pending, timeout=max(0.5, min(5.0, (time_left() or 5.0) - 1.0)))
            result = FetchResult(url=final_url, status=status, text=html, headers=headers, via_browser=True,
                                 queued=looks_like_queue(html, final_url), captured=list(captured))
            _record(url, via="browser", t0=t0, status=status, final_url=final_url, headers=headers, body=html)
            return result
    except FetchError as e:
        _record(url, via="browser", t0=t0, error=str(e))
        raise
    except Exception as e:
        err = _browser_error(e)
        _record(url, via="browser", t0=t0, error=str(err))
        raise err from e
    finally:
        for f in pending:
            f.cancel()


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
    st = _st()
    try:
        async with browser_page() as page:
            await _open(page, page_url, interactive=st.info.get("mode") == "cdp")
            res = await page.evaluate(script, [target_url, accept])
            cookies = await page.context.cookies([page_url, target_url])
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
    global _state, _xvfb
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
        await _close_browser(st)
    if _xvfb is not None:
        await asyncio.to_thread(_xvfb.stop)
        _xvfb = None
    _state = _State()
    _browser_hosts.clear()
    _plain_hosts.clear()
