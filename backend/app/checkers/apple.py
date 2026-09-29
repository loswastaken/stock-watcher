"""Apple Store support: in-store pickup near a ZIP + 2-hour (courier) delivery.

Availability comes from the Apple Store's own fulfillment endpoint that powers the
"Check availability" dialog on product pages:

    GET https://www.apple.com/shop/fulfillment-messages
        ?fae=true&pl=true&mts.0=regular&mts.1=compact
        &parts.0=MG8H4LL/A[&parts.1=...]&searchNearby=true&location=95014

Response (abridged, fields we use)::

    {"body": {"content": {
        "pickupMessage": {"stores": [{
            "storeNumber": "R014", "storeName": "Valley Fair", "city": "Santa Clara",
            "storedistance": 3.21, "storeDistanceWithUnit": "3.21 mi",
            "partsAvailability": {"MG8H4LL/A": {
                "pickupDisplay": "available|unavailable|ineligible",
                "pickupSearchQuote": "Available<br/>Today",
                "messageTypes": {"regular": {"storePickupQuote": "Today at Valley Fair",
                                              "storePickupProductTitle": "iPhone ..."}},
                "buyability": {"isBuyable": true}}}}]},
        "deliveryMessage": {"MG8H4LL/A": {
            "regular": {"deliveryOptions": [{"displayName": "...", "date": "...", "shippingCost": "..."}]},
            "compact": {"quote": "..."}}}}}}

Parsing is deliberately defensive — Apple changes field names without notice.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

import httpx
from bs4 import BeautifulSoup

from . import fetcher
from .base import Availability, CheckResult
from .fetcher import FetchError
from .util import absolutize, clean_text, extract_balanced, format_price, loads_lenient, parse_amount, walk

log = logging.getLogger("stockwatcher.checkers.apple")

APPLE_ORIGIN = "https://www.apple.com"
# Country path prefix: "" for the US store, "/ca", "/uk", ... (internal knob; UI is US-only for now).
STORE_PREFIX = ""
FULFILLMENT_PATH = "/shop/fulfillment-messages"
WARMUP_PATH = "/shop/buy-iphone"
JAR_TTL = 30 * 60
RESPONSE_CACHE_TTL = 30.0
MAX_PARTS_PER_REQUEST = 8
BLOCK_STATUSES = {403, 429, 503, 541}

# e.g. MG8H4LL/A, MX2E3AM/A, MYW33B/A, MU8F2ZM/A (5 alnum + 1-2 letter region + /A)
PART_RE = re.compile(r"^[A-Z0-9]{5}[A-Z]{1,2}/A$")
_PART_IN_TEXT_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{5}[A-Z]{1,2}/A)(?![A-Z0-9])")


class AppleError(Exception):
    pass


def is_apple_url(url: str) -> bool:
    host = fetcher.host_of(url)
    return host == "apple.com" or host.endswith(".apple.com")


def is_part_number(s: Any) -> bool:
    return isinstance(s, str) and bool(PART_RE.match(s)) and any(c.isdigit() for c in s[:5])


def normalize_part(s: Any) -> str | None:
    if not isinstance(s, str):
        return None
    p = unquote(s).strip().upper()
    return p if is_part_number(p) else None


def normalize_zip(z: Any) -> str:
    s = str(z or "").strip()
    m = re.fullmatch(r"(\d{5})(?:-\d{4})?", s)
    return m.group(1) if m else s


# --------------------------------------------------------------------------- parsing helpers


def _get_ci(d: Any, key: str) -> Any:
    if not isinstance(d, dict):
        return None
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == low:
            return v
    return None


def _dig(d: Any, *path: str) -> Any:
    cur = d
    for p in path:
        cur = _get_ci(cur, p)
        if cur is None:
            return None
    return cur


def _distance_miles(store: dict) -> float | None:
    for k in ("storedistance", "storeDistance", "distance"):
        v = _get_ci(store, k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            unit_text = str(_get_ci(store, "storeDistanceWithUnit") or "")
            return round(float(v) * (0.621371 if re.search(r"\bkm\b", unit_text, re.I) else 1.0), 1)
        if isinstance(v, str) and parse_amount(v) is not None:
            return round(float(parse_amount(v)), 1)
    for k in ("storeDistanceWithUnit", "storeDistanceVoText"):
        v = _get_ci(store, k)
        if isinstance(v, str):
            m = re.search(r"([\d.,]+)\s*(mi|miles?|km|kilomet)", v, re.I)
            if m:
                d = parse_amount(m.group(1))
                if d is not None:
                    f = float(d) * (0.621371 if m.group(2).lower().startswith("k") else 1.0)
                    return round(f, 1)
    return None


def _fmt_dist(d: float | None) -> str | None:
    if d is None:
        return None
    return f"{d:.1f}".rstrip("0").rstrip(".") if d < 100 else f"{d:.0f}"


def _pickup_quote(info: dict) -> str | None:
    for path in (
        ("pickupSearchQuote",),
        ("messageTypes", "regular", "storePickupQuote2_0"),
        ("messageTypes", "regular", "storePickupQuote"),
        ("messageTypes", "compact", "storePickupQuote"),
        ("storePickupQuote",),
    ):
        v = _dig(info, *path)
        if isinstance(v, str) and clean_text(v):
            return clean_text(v)
    return None


_AVAILABLE_QUOTE_RE = re.compile(r"\b(available|today|tomorrow|in stock)\b", re.I)
_UNAVAILABLE_QUOTE_RE = re.compile(r"unavailable|not available|ineligible|currently unavailable|sold out", re.I)


def _pickup_available(info: dict, quote: str | None) -> tuple[bool, str]:
    display = str(_get_ci(info, "pickupDisplay") or "").strip().lower()
    if display:
        return display == "available", display
    # No pickupDisplay: infer from eligibility flags + quote text.
    eligible = _get_ci(info, "storePickEligible")
    if eligible is False:
        return False, "ineligible"
    if quote and _AVAILABLE_QUOTE_RE.search(quote) and not _UNAVAILABLE_QUOTE_RE.search(quote):
        return True, "available"
    return False, "unavailable" if quote else "unknown"


_TODAY_QUOTE_RE = re.compile(r"\b(today|now)\b", re.I)
_FUTURE_QUOTE_RE = re.compile(
    r"\btomorrow\b|\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\d",
    re.I,
)


def _pickup_is_today(quote: str | None) -> bool:
    """True unless the quote names a later day ("Tomorrow", "Available Oct 3").

    An available store with no quote, or a bare "Available", is treated as same-day.
    """
    if not quote or _TODAY_QUOTE_RE.search(quote):
        return True
    return not _FUTURE_QUOTE_RE.search(quote)


def _pickup_title(info: dict) -> str | None:
    for path in (("messageTypes", "regular", "storePickupProductTitle"),
                 ("messageTypes", "compact", "storePickupProductTitle"),
                 ("storePickupProductTitle",)):
        v = _dig(info, *path)
        if isinstance(v, str) and clean_text(v):
            return clean_text(v)
    return None


# ---- 2-hour / courier delivery detection

TWO_HOUR_RE = re.compile(
    r"courier"
    r"|\b(?:within|in)\s+(?:about\s+)?(?:2|two)[\s-]*(?:hours?|hrs?|h)\b"
    r"|\b(?:2|two)[\s-]*(?:hours?|hrs?|h)\s+(?:delivery|deliver\w*|courier)"
    r"|\btoday,?\s+(?:2|two)[\s-]*(?:hours?|hrs?)\b"
    r"|\bdeliver\w*\s+(?:today\s+)?(?:with)?in\s+(?:2|two)[\s-]*(?:hours?|hrs?)\b",
    re.I,
)
_TWO_HOUR_NEG_RE = re.compile(
    r"\bnot\s+available\b|\bunavailable\b|\bisn'?t\s+available\b|\bnot\s+eligible\b|\bineligible\b"
    r"|\bno\s+longer\b",
    re.I,
)
# "Order within 2 hours for delivery tomorrow" is an order cut-off, not a delivery promise.
_ORDER_CUTOFF_RE = re.compile(r"\border\s+(?:by|within|in\s+the\s+next)\s+[^.,;·]*", re.I)
_COURIER_TYPE_RE = re.compile(r"courier|two[_\s-]?hour|2[_\s-]?h(?:ou)?r|same[_\s-]?day", re.I)
_TYPE_KEYS = {"type", "deliverytype", "shippingmethod", "method", "code", "deliverymethod", "shippingtype",
              "fulfillmenttype", "optiontype", "deliveryoptiontype"}
_SKIP_TEXT_KEYS = re.compile(r"url|link|href|image|img|icon|partnumber|^id$|analytics|a11y|accessibility|voText", re.I)


def _dict_text(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(k, str) and _SKIP_TEXT_KEYS.search(k):
            continue
        if isinstance(v, str) and v.strip() and not v.strip().startswith(("http", "/")):
            parts.append(clean_text(v))
    return " ".join(p for p in parts if p)


_QUOTE_KEYS = ("displayName", "date", "shippingCost", "quote", "subHeader", "message", "deliveryMessage")


def _quote_text(d: dict) -> str:
    parts = []
    for k in _QUOTE_KEYS:
        v = d.get(k)
        if isinstance(v, str) and clean_text(v) and not v.strip().startswith(("http", "/")):
            parts.append(clean_text(v))
    return " · ".join(dict.fromkeys(parts))


def _is_two_hour_text(s: str) -> bool:
    if not s or _TWO_HOUR_NEG_RE.search(s):
        return False
    return bool(TWO_HOUR_RE.search(_ORDER_CUTOFF_RE.sub(" ", s)))


def detect_two_hour(part_delivery: Any) -> tuple[bool, str | None, list[str]]:
    """Inspect one part's deliveryMessage subtree. Returns (two_hour, quote, all_quotes).

    2-hour/courier delivery is detected when (first match wins for the quote):
      * a key containing "courier" / "twoHour" has a true value (isCourier, courierDelivery, ...), or
      * a type-ish key (type, deliveryType, shippingMethod, ...) has a value like COURIER / TWO_HOUR, or
      * any text (option displayName/date, compact quote, ...) mentions courier / "within 2 hours" /
        "2-hour delivery" / "Today, 2 hours" — unless the same text says it's not available.
    """
    quotes: list[str] = []
    two_hour = False
    two_quote: str | None = None
    if not isinstance(part_delivery, (dict, list)):
        return False, None, quotes
    for node in walk(part_delivery):
        if not isinstance(node, dict):
            continue
        qtext = _quote_text(node)
        if qtext:
            quotes.append(qtext)
        hit = False
        negated = bool(qtext and _TWO_HOUR_NEG_RE.search(qtext))
        for k, v in node.items():
            if not isinstance(k, str):
                continue
            kl = k.lower()
            if ("courier" in kl or "twohour" in kl or "2hour" in kl) and (v is True or (isinstance(v, str) and v.strip().lower() == "true")):
                hit = True
            elif kl in _TYPE_KEYS and isinstance(v, str) and _COURIER_TYPE_RE.search(v):
                hit = True
            elif not negated and isinstance(v, str) and not _SKIP_TEXT_KEYS.search(k) and _is_two_hour_text(clean_text(v)):
                hit = True
        if not hit and qtext and _is_two_hour_text(qtext):
            hit = True
        if hit and not two_hour:
            two_hour, two_quote = True, qtext or _dict_text(node) or None
    quotes = list(dict.fromkeys(q for q in quotes if q))[:8]
    return two_hour, two_quote, quotes


def _delivery_quote(part_delivery: Any) -> str | None:
    for path in (("compact", "quote"), ("regular", "quote"), ("compact", "label"), ("quote",)):
        v = _dig(part_delivery, *path)
        if isinstance(v, str) and clean_text(v):
            return clean_text(v)
    opts = _dig(part_delivery, "regular", "deliveryOptions")
    if isinstance(opts, list):
        for o in opts:
            if isinstance(o, dict):
                t = " — ".join(x for x in (clean_text(o.get("displayName")), clean_text(o.get("date")),
                                           clean_text(o.get("shippingCost"))) if x)
                if t:
                    return t
    return None


# --------------------------------------------------------------------------- parse_fulfillment


def _find_content(data: Any) -> dict | None:
    content = _dig(data, "body", "content")
    if isinstance(content, dict) and (_get_ci(content, "pickupMessage") is not None or _get_ci(content, "deliveryMessage") is not None):
        return content
    for node in walk(data):
        if isinstance(node, dict) and (_get_ci(node, "pickupMessage") is not None or _get_ci(node, "deliveryMessage") is not None):
            return node
    return content if isinstance(content, dict) else None


def _pickup_label(quote: str | None, store_name: str, dist: float | None, part_label: str) -> str:
    q = (quote or "").strip()
    q = re.sub(r"\b(Available|Today|Tomorrow)\b", lambda m: m.group(1).lower(), q)
    dist_s = f" ({_fmt_dist(dist)} mi)" if dist is not None else ""
    head = f"Pickup {q} at" if q else "Pickup at"
    return f"{head} {store_name}{dist_s} — {part_label}"


def parse_fulfillment(
    data: Any,
    parts: list[str],
    part_labels: dict[str, str] | None = None,
    max_distance: float | None = 25,
    watch_pickup: bool = True,
    watch_delivery: bool = True,
    pickup_today_only: bool = True,
) -> CheckResult:
    """Turn a fulfillment-messages JSON payload into a CheckResult (pure, no I/O)."""
    part_labels = dict(part_labels or {})
    content = _find_content(data)
    if content is None:
        head_status = _dig(data, "head", "status")
        return CheckResult(status="error", status_text="Unexpected Apple response",
                           error=f"Apple response had no availability data (head.status={head_status!r})")

    pickup = _get_ci(content, "pickupMessage")
    pickup = pickup if isinstance(pickup, dict) else {}
    delivery_msg = _get_ci(content, "deliveryMessage")
    delivery_msg = delivery_msg if isinstance(delivery_msg, dict) else {}

    titles: dict[str, str] = {}
    stores_out: list[dict] = []
    available: list[Availability] = []
    stores_with_pickup: set[str] = set()
    out_of_range = 0

    raw_stores = _get_ci(pickup, "stores")
    for s in raw_stores if isinstance(raw_stores, list) else []:
        if not isinstance(s, dict):
            continue
        number = str(_get_ci(s, "storeNumber") or _get_ci(s, "storeId") or "").strip()
        name = clean_text(_get_ci(s, "storeName") or _get_ci(s, "name")) or number or "Apple Store"
        if not number:
            number = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "unknown"
        city = clean_text(_get_ci(s, "city") or _dig(s, "address", "city")) or None
        dist = _distance_miles(s)
        if max_distance and max_distance > 0 and dist is not None and dist > float(max_distance):
            out_of_range += 1
            continue
        pa = _get_ci(s, "partsAvailability")
        pa = pa if isinstance(pa, dict) else {}
        store_parts = []
        for part in parts:
            info = _get_ci(pa, part)
            info = info if isinstance(info, dict) else {}
            title = _pickup_title(info)
            if title and part not in titles:
                titles[part] = title
            label = part_labels.get(part) or title or part
            quote = _pickup_quote(info)
            ok, display = _pickup_available(info, quote) if info else (False, "unknown")
            today = ok and _pickup_is_today(quote)
            store_parts.append({"part_number": part, "label": label, "available": ok, "today": today,
                                "quote": quote, "display": display})
            if ok and watch_pickup and (today or not pickup_today_only):
                stores_with_pickup.add(number)
                available.append(Availability(key=f"pickup:{number}:{part}",
                                              label=_pickup_label(quote, name, dist, label)))
        stores_out.append({"store_number": number, "name": name, "city": city,
                           "distance_miles": dist, "parts": store_parts})
    stores_out.sort(key=lambda x: (x["distance_miles"] is None, x["distance_miles"] or 0))

    delivery_out: list[dict] = []
    has_2h = False
    for part in parts:
        pd = _get_ci(delivery_msg, part)
        label = part_labels.get(part) or titles.get(part) or part
        if not isinstance(pd, dict):
            delivery_out.append({"part_number": part, "label": label, "two_hour": False, "quote": None, "quotes": []})
            continue
        two_hour, two_quote, quotes = detect_two_hour(pd)
        buyable = _dig(pd, "regular", "buyability", "isBuyable")
        if buyable is None:
            buyable = _dig(pd, "regular", "isBuyable")
        if buyable is False:
            two_hour = False  # can't order at all
        quote = two_quote if two_hour else _delivery_quote(pd)
        delivery_out.append({"part_number": part, "label": label, "two_hour": two_hour, "quote": quote,
                             "quotes": quotes})
        if two_hour and watch_delivery:
            has_2h = True
            available.append(Availability(key=f"delivery2h:{part}", label=f"2-hour delivery available — {label}"))

    detail: dict[str, Any] = {"stores": stores_out, "delivery": delivery_out}
    if out_of_range:
        detail["stores_out_of_range"] = out_of_range
    for k in ("errorMessage", "notAvailableNearby", "notAvailableNearOneStore"):
        v = _get_ci(pickup, k)
        if isinstance(v, str) and clean_text(v) and not stores_out:
            detail["pickup_message"] = clean_text(v)
            break
    loc = _get_ci(delivery_msg, "deliveryLocationLink") or _get_ci(pickup, "location")
    if isinstance(loc, str) and clean_text(loc):
        detail["location"] = clean_text(loc)

    title = next((titles[p] for p in parts if p in titles), None)

    if not watch_pickup and not watch_delivery:
        return CheckResult(status="unknown", status_text="Nothing to watch", available=[], title=title, detail=detail)

    bits = []
    if stores_with_pickup:
        n = len(stores_with_pickup)
        bits.append(f"Pickup at {n} store{'s' if n != 1 else ''}")
    if has_2h:
        bits.append("2h delivery")
    if available:
        return CheckResult(status="in_stock", status_text=" · ".join(bits), available=available, title=title, detail=detail)

    if not stores_out and not delivery_msg and detail.get("pickup_message") and _get_ci(pickup, "errorMessage"):
        return CheckResult(status="error", status_text="Apple error", error=detail["pickup_message"], title=title, detail=detail)
    return CheckResult(status="out_of_stock", status_text="Not available nearby", available=[], title=title, detail=detail)


# --------------------------------------------------------------------------- HTTP (cookies, cache, fallback)


class _AppleSession:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: httpx.AsyncClient | None = None
        self.created = 0.0
        self.warmed = False
        self.client_lock: asyncio.Lock | None = None
        self.key_locks: dict[tuple, asyncio.Lock] = {}


_sess = _AppleSession()
_cache: dict[tuple, tuple[float, Any]] = {}


def _session() -> _AppleSession:
    global _sess
    loop = asyncio.get_running_loop()
    if _sess.loop is not loop:
        _sess = _AppleSession()
        _sess.loop = loop
        _sess.client_lock = asyncio.Lock()
    return _sess


_BASE_HEADERS = {k: v for k, v in fetcher.DOCUMENT_HEADERS.items()
                 if k in {"User-Agent", "Accept-Language", "Accept-Encoding", "sec-ch-ua", "sec-ch-ua-mobile",
                          "sec-ch-ua-platform"}}
_DOC_HEADERS = dict(fetcher.DOCUMENT_HEADERS)
_XHR_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
    "x-skip-redirect": "true",
}


async def _reset_client() -> None:
    s = _session()
    if s.client is not None:
        with contextlib.suppress(Exception):
            await s.client.aclose()
    s.client = None
    s.warmed = False


async def _client() -> httpx.AsyncClient:
    s = _session()
    assert s.client_lock is not None
    async with s.client_lock:
        if s.client is not None and (s.client.is_closed or time.monotonic() - s.created > JAR_TTL):
            await _reset_client()
        if s.client is None:
            s.client = fetcher.make_client(headers=_BASE_HEADERS)
            s.created = time.monotonic()
            s.warmed = False
        if not s.warmed:
            s.warmed = True
            try:  # collect Apple's session cookies like a real visitor would
                await fetcher.http_get(APPLE_ORIGIN + STORE_PREFIX + WARMUP_PATH, headers=_DOC_HEADERS, client=s.client)
            except Exception as e:  # noqa: BLE001 - warm-up is best effort
                log.info("apple warm-up failed: %s", e)
        return s.client


def fulfillment_url(parts: list[str], zip_code: str) -> str:
    params: list[tuple[str, str]] = [("fae", "true"), ("pl", "true"), ("mts.0", "regular"), ("mts.1", "compact")]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    params += [("searchNearby", "true"), ("location", zip_code)]
    return f"{APPLE_ORIGIN}{STORE_PREFIX}{FULFILLMENT_PATH}?{urlencode(params)}"


def _parse_json(text: str) -> Any:
    t = (text or "").lstrip()
    if not t.startswith(("{", "[")):
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


async def _import_cookies(cookies: list[dict]) -> None:
    try:
        client = await _client()
        for c in cookies:
            if "apple.com" in str(c.get("domain", "")):
                client.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path") or "/")
    except Exception as e:  # pragma: no cover - best effort
        log.debug("cookie import failed: %s", e)


# After Apple blocks plain HTTP, go straight to the browser for a while (monotonic deadline).
BROWSER_PREFERENCE_TTL = 30 * 60
_prefer_browser_until = 0.0


async def _fetch_via_browser(url: str, referer: str, reason: str) -> Any:
    global _prefer_browser_until
    if not fetcher.browser_enabled():
        raise AppleError(f"Apple blocked the availability request ({reason}); browser fallback disabled")
    try:
        status, text, cookies = await fetcher.browser_fetch_from_page(referer, url)
    except FetchError as e:
        raise AppleError(f"Apple blocked the availability request ({reason}); browser retry failed: {e}") from e
    data = _parse_json(text) if status == 200 else None
    if data is None:
        _prefer_browser_until = 0.0
        raise AppleError(f"Apple blocked the availability request ({reason}; browser got HTTP {status})")
    _prefer_browser_until = time.monotonic() + BROWSER_PREFERENCE_TTL
    await _import_cookies(cookies)
    return data


async def _fetch_fulfillment_uncached(parts: list[str], zip_code: str, referer: str) -> Any:
    url = fulfillment_url(parts, zip_code)
    if fetcher.browser_enabled() and _prefer_browser_until > time.monotonic():
        try:
            return await _fetch_via_browser(url, referer, "recently blocked")
        except AppleError as e:
            log.info("apple browser path failed, trying plain HTTP: %s", e)

    headers = dict(_XHR_HEADERS, Referer=referer)
    client = await _client()
    resp = await fetcher.http_get(url, headers=headers, client=client)
    data = _parse_json(resp.text) if resp.status_code == 200 else None
    if data is not None:
        return data

    blocked = resp.status_code in BLOCK_STATUSES or (resp.status_code == 200 and data is None)
    if not blocked:
        raise AppleError(f"Apple returned HTTP {resp.status_code}")
    reason = f"HTTP {resp.status_code}" if resp.status_code != 200 else "non-JSON response"
    log.info("apple fulfillment blocked (%s); retrying via browser", reason)
    await _reset_client()
    return await _fetch_via_browser(url, referer, reason)


def _merge(payloads: list[Any]) -> Any:
    if len(payloads) == 1:
        return payloads[0]
    stores: dict[str, dict] = {}
    order: list[str] = []
    delivery: dict[str, Any] = {}
    pickup_extra: dict[str, Any] = {}
    for p in payloads:
        content = _find_content(p) or {}
        pm = _get_ci(content, "pickupMessage") or {}
        for k, v in pm.items() if isinstance(pm, dict) else []:
            if k != "stores":
                pickup_extra.setdefault(k, v)
        for s in (_get_ci(pm, "stores") or []) if isinstance(pm, dict) else []:
            if not isinstance(s, dict):
                continue
            num = str(_get_ci(s, "storeNumber") or id(s))
            if num not in stores:
                stores[num] = dict(s, partsAvailability=dict(_get_ci(s, "partsAvailability") or {}))
                order.append(num)
            else:
                stores[num]["partsAvailability"].update(_get_ci(s, "partsAvailability") or {})
        dm = _get_ci(content, "deliveryMessage")
        if isinstance(dm, dict):
            for k, v in dm.items():
                delivery.setdefault(k, v)
    return {"body": {"content": {"pickupMessage": dict(pickup_extra, stores=[stores[n] for n in order]),
                                 "deliveryMessage": delivery}}}


async def fetch_fulfillment(parts: list[str], zip_code: str, referer: str | None = None) -> Any:
    """Fetch (with a short shared cache and in-flight de-duplication) the fulfillment payload."""
    referer = referer if referer and is_apple_url(referer) else APPLE_ORIGIN + STORE_PREFIX + WARMUP_PATH
    key = (tuple(parts), zip_code, STORE_PREFIX)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    s = _session()
    lock = s.key_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        chunks = [parts[i:i + MAX_PARTS_PER_REQUEST] for i in range(0, len(parts), MAX_PARTS_PER_REQUEST)]
        payloads = [await _fetch_fulfillment_uncached(c, zip_code, referer) for c in chunks]
        data = _merge(payloads)
        _cache[key] = (time.monotonic() + RESPONSE_CACHE_TTL, data)
        for k in [k for k, (exp, _) in _cache.items() if exp < time.monotonic()]:
            _cache.pop(k, None)
        return data


# --------------------------------------------------------------------------- check entry point


@dataclass
class AppleConfig:
    parts: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    zip: str = ""
    max_distance_miles: float | None = 25
    watch_pickup: bool = True
    watch_delivery: bool = True
    pickup_today_only: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> "AppleConfig":
        d = d or {}
        parts: list[str] = []
        labels: dict[str, str] = {}
        for p in d.get("parts") or []:
            if isinstance(p, str):
                pn, label = normalize_part(p), None
            elif isinstance(p, dict):
                pn, label = normalize_part(p.get("part_number") or p.get("part")), p.get("label")
            else:
                continue
            if pn and pn not in parts:
                parts.append(pn)
                if isinstance(label, str) and label.strip():
                    labels[pn] = label.strip()
        md = d.get("max_distance_miles", 25)  # explicit null/"" = no distance limit
        try:
            md = float(md) if md not in (None, "") else None
        except (TypeError, ValueError):
            md = 25.0
        wp, wd, pt = d.get("watch_pickup"), d.get("watch_delivery"), d.get("pickup_today_only")
        return cls(parts=parts, labels=labels, zip=normalize_zip(d.get("zip")), max_distance_miles=md,
                   watch_pickup=True if wp is None else bool(wp), watch_delivery=True if wd is None else bool(wd),
                   pickup_today_only=True if pt is None else bool(pt))


async def check_apple(url: str, apple_config: dict | None) -> CheckResult:
    cfg = AppleConfig.from_dict(apple_config)
    if not cfg.parts:
        return CheckResult(status="error", status_text="No part numbers",
                           error="Apple item has no valid part numbers (e.g. MG8H4LL/A)")
    if not cfg.zip:
        return CheckResult(status="error", status_text="No ZIP code", error="Apple item needs a ZIP code")
    try:
        data = await fetch_fulfillment(cfg.parts, cfg.zip, referer=url)
    except (AppleError, FetchError) as e:
        return CheckResult(status="error", status_text="Apple check failed", error=str(e))
    result = parse_fulfillment(data, cfg.parts, cfg.labels, cfg.max_distance_miles, cfg.watch_pickup,
                               cfg.watch_delivery, cfg.pickup_today_only)
    result.detail["zip"] = cfg.zip
    return result


# --------------------------------------------------------------------------- resolve product page -> part numbers

_DIM_ORDER = ["screensize", "size", "model", "capacity", "storage", "memory", "chip", "color", "colour", "finish",
              "band", "connectivity", "carrier"]


def _pretty_dim(v: str) -> str:
    s = str(v).strip()
    m = re.fullmatch(r"(\d+)(gb|tb)", s, re.I)
    if m:
        return f"{m.group(1)}{m.group(2).upper()}"
    m = re.fullmatch(r"(\d+)_(\d+)inch", s, re.I)
    if m:
        return f"{m.group(1)}.{m.group(2)}-inch"
    s = s.replace("_", " ").replace("-", " ")
    return s[:1].upper() + s[1:]


def _display_value(v: Any) -> str | None:
    if isinstance(v, str):
        return clean_text(v) or None
    if isinstance(v, dict):
        for k in ("value", "displayValue", "text", "label", "name", "title"):
            x = v.get(k)
            if isinstance(x, str) and clean_text(x):
                return clean_text(x)
    return None


def _collect_display_maps(blobs: list) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    for blob in blobs:
        for node in walk(blob):
            if not isinstance(node, dict):
                continue
            for k, v in node.items():
                if not (isinstance(k, str) and k.lower().startswith("dimension") and isinstance(v, dict)):
                    continue
                for sub, dv in v.items():
                    text = _display_value(dv)
                    if isinstance(sub, str) and text and not isinstance(dv, str):
                        maps.setdefault(k, {}).setdefault(sub, text)
    return maps


def _price_text(v: Any, currency: Any = None) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dict):
        for k in ("currentPrice", "fullPrice", "price", "amount", "value", "raw_amount", "rawAmount", "priceValue"):
            if k in v:
                p = _price_text(v[k], v.get("currency") or v.get("priceCurrency") or currency)
                if p:
                    return p
        return None
    if isinstance(v, str):
        t = clean_text(v)
        if re.search(r"[$€£¥₹]", t) and parse_amount(t):
            m = re.search(r"(?:[A-Z]{1,3})?[$€£¥₹]\s?[\d.,]+", t)
            return m.group(0).strip() if m else t
    d = parse_amount(v)
    if d is None or d == 0:
        return None
    return format_price(v, currency or "USD")


def _collect_price_maps(blobs: list) -> dict[str, str]:
    prices: dict[str, str] = {}
    for blob in blobs:
        for node in walk(blob):
            if isinstance(node, dict):
                pm = node.get("prices")
                if isinstance(pm, dict):
                    for k, v in pm.items():
                        p = _price_text(v)
                        if isinstance(k, str) and p:
                            prices.setdefault(k, p)
    return prices


_PART_KEYS = ("partNumber", "part", "sku", "fullPartNumber", "productPartNumber", "partNumberWithRegion")


def _variant_label(node: dict, maps: dict[str, dict[str, str]]) -> str | None:
    dims: dict[str, Any] = {}
    d = node.get("dimensions")
    if isinstance(d, dict):
        dims.update(d)
    for k, v in node.items():
        if isinstance(k, str) and k.lower().startswith("dimension") and isinstance(v, str):
            dims[k] = v
    if dims:
        pieces: list[tuple[int, str]] = []
        for k, v in dims.items():
            if not isinstance(v, str) or not v.strip():
                continue
            key_l = k.lower().removeprefix("dimension")
            if "carrier" in key_l:
                if v.upper().startswith("UNLOCKED"):
                    continue  # the default; keep labels short ("256GB Cosmic Orange")
                text = (maps.get(k) or {}).get(v) or _pretty_dim(v.split("/")[0])
                rank = len(_DIM_ORDER)
            else:
                text = (maps.get(k) or {}).get(v) or _pretty_dim(v)
                rank = next((i for i, name in enumerate(_DIM_ORDER) if name in key_l), len(_DIM_ORDER))
            pieces.append((rank, text))
        pieces.sort(key=lambda x: x[0])
        label = " ".join(t for _, t in pieces)
        if label:
            return label
    for k in ("name", "title", "productTitle", "displayName", "label", "description"):
        v = node.get(k)
        if isinstance(v, str) and clean_text(v) and len(clean_text(v)) < 140:
            return clean_text(v)
    return None


def _script_blobs(soup: BeautifulSoup, html: str) -> list:
    blobs: list = []
    for s in soup.find_all("script"):
        if s.get("src"):
            continue
        text = s.string or s.get_text() or ""
        if not text.strip() or len(text) > 8_000_000:
            continue
        typ = str(s.get("type") or "").lower()
        if "json" in typ:
            data = loads_lenient(text)
            if data is not None:
                blobs.append(data)
            continue
        # JS: window.X = {...}; var y = {...}; X({...}); JSON.parse("...")
        attempts, pos = 0, 0
        for m in re.finditer(r"(?:=|\()\s*(?=[{\[])", text):
            if m.end() < pos:
                continue  # inside a literal we already consumed
            attempts += 1
            if attempts > 150:
                break
            lit = extract_balanced(text, m.end())
            if not lit:
                break  # unbalanced from here on; later matches would scan to the end too
            pos = m.end() + len(lit)
            if len(lit) < 40:
                continue
            data = loads_lenient(lit)
            if data is None:
                data = loads_lenient(_js_to_json(lit))
            if isinstance(data, (dict, list)):
                blobs.append(data)
        for m in re.finditer(r"JSON\.parse\(\s*(['\"])((?:\\.|(?!\1).)*)\1\s*\)", text, re.S):
            inner = _js_string(m.group(2), m.group(1))
            data = loads_lenient(inner) if inner else None
            if isinstance(data, (dict, list)):
                blobs.append(data)
    return blobs


def _js_to_json(lit: str) -> str:
    """Best-effort JS object literal -> JSON: quote bare keys, drop trailing commas."""
    t = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:(?!//)", r'\1"\2":', lit)
    t = re.sub(r"(?<![\w$])(undefined|void 0)(?![\w$])", "null", t)
    return re.sub(r",\s*([}\]])", r"\1", t)


def _js_string(raw: str, quote: str) -> str | None:
    """Decode the body of a JS string literal (used for JSON.parse('...') payloads)."""
    body = raw.replace("\\'", "'")
    if quote == "'":
        body = re.sub(r'(?<!\\)"', r'\\"', body)
    try:
        return json.loads('"' + body + '"')
    except ValueError:
        return None


def _clean_product_name(t: str | None) -> str | None:
    t = clean_text(t)
    if not t:
        return None
    t = re.sub(r"\s+[-–|]\s+Apple(?:\s*\([A-Z]{2}\))?(?:\s+Store)?\s*$", "", t)
    t = re.sub(r"^Buy\s+", "", t)
    return t.strip() or None


def parse_product_page(html: str, url: str) -> dict:
    """Extract {product_name, image_url, variants:[{part_number,label,price}]} from an Apple
    product/buy page (pure, no I/O)."""
    soup = BeautifulSoup(html or "", "lxml")

    def meta(*keys: str) -> str | None:
        for k in keys:
            m = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k})
            if m and m.get("content"):
                return str(m["content"]).strip()
        return None

    name = _clean_product_name(meta("og:title") or (soup.title.string if soup.title else None))
    img = meta("og:image", "twitter:image")
    image_url = absolutize(img, url)

    blobs = _script_blobs(soup, html or "")
    maps = _collect_display_maps(blobs)
    prices = _collect_price_maps(blobs)

    variants: dict[str, dict] = {}

    url_part = part_from_url(url)
    if url_part:
        variants[url_part] = {"part_number": url_part, "label": None, "price": None}

    for blob in blobs:
        for node in walk(blob):
            if not isinstance(node, dict):
                continue
            pn = None
            for k in _PART_KEYS:
                v = node.get(k)
                if isinstance(v, str) and is_part_number(v.strip().upper()):
                    pn = v.strip().upper()
                    break
            if not pn:
                continue
            label = _variant_label(node, maps)
            price = _price_text(node.get("price")) or _price_text(node.get("fullPrice"))
            if not price:
                for k in (pn, node.get("priceKey"), _dig(node, "price", "priceKey")):
                    if isinstance(k, str) and k in prices:
                        price = prices[k]
                        break
            cur = variants.get(pn)
            if cur is None:
                variants[pn] = {"part_number": pn, "label": label, "price": price}
            else:
                cur["label"] = cur["label"] or label
                cur["price"] = cur["price"] or price

    if len(variants) <= (1 if url_part else 0):
        # Last resort: any "partNumber":"XXXXXLL/A" in the raw HTML.
        for m in re.finditer(r"[\"']part(?:Number)?[\"']\s*:\s*[\"']([A-Z0-9]{5}[A-Z]{1,2}(?:/|\\u002F|\\/)A)[\"']", html or ""):
            pn = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
            if is_part_number(pn):
                variants.setdefault(pn, {"part_number": pn, "label": None, "price": None})

    out = []
    for v in list(variants.values())[:400]:
        v["label"] = v["label"] or name or v["part_number"]
        out.append(v)
    return {"product_name": name, "image_url": image_url, "variants": out}


def part_from_url(url: str) -> str | None:
    path = unquote(urlsplit(url).path)
    m = re.search(r"/product/([A-Z0-9]{5}[A-Z]{1,2}/A)(?:/|$)", path, re.I)
    if m:
        return normalize_part(m.group(1))
    m = _PART_IN_TEXT_RE.search(path.upper())
    return normalize_part(m.group(1)) if m else None


async def resolve_apple(url: str) -> dict:
    empty = {"product_name": None, "image_url": None, "variants": []}
    url_part = part_from_url(url or "")
    if url_part:
        empty["variants"] = [{"part_number": url_part, "label": url_part, "price": None}]
    if not url or not is_apple_url(url):
        return empty
    html = None
    try:
        client = await _client()
        resp = await fetcher.http_get(url, headers=_DOC_HEADERS, client=client)
        if resp.status_code == 200 and not fetcher.looks_like_challenge(resp.text):
            html = resp.text
    except Exception as e:
        log.info("apple resolve fetch failed for %s: %s", url, e)
    result = parse_product_page(html, url) if html else None
    if (result is None or not result["variants"]) and fetcher.browser_enabled():
        try:
            fr = await fetcher.browser_fetch(url)
            if fr.status < 400:
                result = parse_product_page(fr.text, url)
        except Exception as e:
            log.info("apple resolve browser fetch failed for %s: %s", url, e)
    if result is None:
        return empty
    return result


async def shutdown() -> None:
    global _sess, _prefer_browser_until
    s = _sess
    same = False
    with contextlib.suppress(RuntimeError):
        same = s.loop is asyncio.get_running_loop()
    if same and s.client is not None:
        with contextlib.suppress(Exception):
            await s.client.aclose()
    _sess = _AppleSession()
    _cache.clear()
    _prefer_browser_until = 0.0


fetcher.register_shutdown(shutdown)
