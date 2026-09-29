"""Target: Redsky JSON APIs.

* ``pdp_client_v1``          → title, price, image, Target Plus (marketplace) seller
* ``product_fulfillment_v1`` → ``shipping_options`` (delivery) and ``store_options`` (order pickup per
  store). This is what target.com itself calls (its ``__CONFIG__`` lists ``productFulfillment``);
  the older ``pdp_fulfillment_v1`` now answers 410 Gone and is only tried as a fallback.
* ``nearby_stores_v1``       → stores within ``radius_miles`` of the configured ZIP

The API key is scraped from the product page (cached) with a well-known fallback key. The same page
embeds ``serverLocationVariables`` (the visitor's geo-IP ZIP, state and nearest store); that store is
used for delivery-only checks. Redsky rejects the old "digital" store 3991 ("Parameter store_id cannot
be digital store 3991"), so a real store is always looked up: pinned store → nearest store to the item's
ZIP → the page's geo store → nearest store to a default ZIP → no store params at all.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlencode

from .. import fetcher
from ..base import Availability, CheckResult
from ..fetcher import FetchError
from ..util import clean_text
from .base import STOCK_KEY, AdapterContext, dig, get_json, result
from .bigbox import THIRD_PARTY_TEXT, base_detail, error_result

log = logging.getLogger("stockwatcher.checkers.retailers.target")

TCIN_RE = re.compile(r"/A-(\d{5,10})(?:[/?#]|$)")
PRESELECT_RE = re.compile(r"[?&]preselect=(\d{5,10})")
API_KEY_RE = re.compile(r'\\?"apiKey\\?"\s*:\s*\\?"([0-9a-f]{40})')
FALLBACK_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
REDSKY = "https://redsky.target.com/redsky_aggregations/v1/web"
DEFAULT_ZIP = "55403"  # Minneapolis: last resort for finding *a* real store for delivery checks
FULFILLMENT_ENDPOINTS = ("product_fulfillment_v1", "pdp_fulfillment_v1")  # current first, legacy fallback
KEY_TTL = 6 * 3600
STORES_TTL = 12 * 3600
STORES_EMPTY_TTL = 300  # "no stores" may be a hiccup: re-ask soon
MAX_PICKUP_STORES = 3  # each extra store can cost a fulfillment request

SHIP_IN = {"IN_STOCK": "In stock", "LIMITED_STOCK": "Limited stock", "PRE_ORDER_SELLABLE": "Pre-order"}
SHIP_OUT = {"OUT_OF_STOCK": "Out of stock", "PRE_ORDER_UNSELLABLE": "Pre-order sold out",
            "UNAVAILABLE": "Not available online", "DISCONTINUED": "Discontinued"}
PICKUP_IN = {"IN_STOCK", "LIMITED_STOCK"}

_key_cache: dict[str, Any] = {"key": None, "exp": 0.0, "loc": None}
_stores_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}


def tcin_from_url(url: str) -> str | None:
    m = PRESELECT_RE.search(url) or TCIN_RE.search(url.split("#", 1)[0].split("?", 1)[0] + "?")
    return m.group(1) if m else None


def _headers(tcin: str) -> dict[str, str]:
    return {"Origin": "https://www.target.com", "Referer": f"https://www.target.com/p/-/A-{tcin}",
            "Sec-Fetch-Site": "same-site"}


_Q = r'\\?"'  # a quote, possibly JSON-escaped inside JSON.parse("...")
_LOC_STORE_RE = re.compile(rf'primaryStore{_Q}\s*:\s*\{{\s*{_Q}id{_Q}\s*:\s*{_Q}(\d{{3,5}}){_Q}')
_LOC_STORE_ID_RE = re.compile(rf'{_Q}store_id{_Q}\s*:\s*{_Q}(\d{{3,5}}){_Q}')
_LOC_ZIP_RE = re.compile(rf'{_Q}zipCode{_Q}\s*:\s*{_Q}(\d{{5}})')
_LOC_STATE_RE = re.compile(rf'{_Q}state{_Q}\s*:\s*{_Q}([A-Z]{{2}}){_Q}')
_LOC_LAT_RE = re.compile(rf'{_Q}latitude{_Q}\s*:\s*{_Q}?(-?\d{{1,3}}\.\d+)')
_LOC_LNG_RE = re.compile(rf'{_Q}longitude{_Q}\s*:\s*{_Q}?(-?\d{{1,3}}\.\d+)')


def page_location(html: str) -> dict | None:
    """The geo-IP location target.com embeds in ``__TGT_DATA__.serverLocationVariables``:
    {"store_id", "zip", "state", "latitude", "longitude"} (values may be None), or None."""
    i = (html or "").find("serverLocationVariables")
    if i < 0:
        return None
    block = html[i:i + 1500]
    m = _LOC_STORE_RE.search(block) or _LOC_STORE_ID_RE.search(block)
    if not m or m.group(1) == "3991":
        return None

    def g(rx: re.Pattern) -> str | None:
        mm = rx.search(block)
        return mm.group(1) if mm else None

    return {"store_id": m.group(1), "zip": g(_LOC_ZIP_RE), "state": g(_LOC_STATE_RE),
            "latitude": g(_LOC_LAT_RE), "longitude": g(_LOC_LNG_RE)}


async def api_key(page_url: str, *, refresh: bool = False) -> str:
    now = time.monotonic()
    if not refresh and _key_cache["key"] and _key_cache["exp"] > now:
        return _key_cache["key"]
    key = loc = None
    try:
        resp = await fetcher.http_get(page_url)
        m = API_KEY_RE.search(resp.text or "")
        key = m.group(1) if m else None
        loc = page_location(resp.text or "")
    except FetchError as e:
        log.info("target: could not load PDP for apiKey (%s); using fallback key", e)
    _key_cache["key"] = key or FALLBACK_KEY
    _key_cache["loc"] = loc or _key_cache.get("loc")
    _key_cache["exp"] = now + (KEY_TTL if key else 1800)
    return _key_cache["key"]


async def _redsky(endpoint: str, params: dict[str, Any], tcin: str, page_url: str) -> Any:
    """GET a Redsky aggregation; on 401/403 re-scrape the key once and retry."""
    for attempt in range(2):
        key = await api_key(page_url, refresh=attempt > 0)
        q = {"key": key, **{k: v for k, v in params.items() if v not in (None, "")}}
        try:
            return await get_json(f"{REDSKY}/{endpoint}?{urlencode(q)}", headers=_headers(tcin))
        except FetchError as e:
            if attempt == 0 and e.status in (401, 403):
                continue
            raise
    return None  # pragma: no cover


async def nearby_stores(zip_code: str, radius: int, tcin: str, page_url: str) -> list[dict] | None:
    """Stores within ``radius`` (nearest first, at most MAX_PICKUP_STORES), [] when there are none,
    or None when Target didn't answer the question (never cached)."""
    ck = (zip_code, radius)
    hit = _stores_cache.get(ck)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    data = await _redsky("nearby_stores_v1", {"limit": MAX_PICKUP_STORES, "within": radius, "place": zip_code,
                                              "channel": "WEB", "page": f"/p/A-{tcin}"}, tcin, page_url)
    raw = dig(data, "data", "nearby_stores")
    if not isinstance(raw, dict) or not isinstance(raw.get("stores", []), list):
        return None
    stores: list[dict] = []
    for s in raw.get("stores") or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("store_id") or s.get("location_id")
        if sid is None:
            continue
        dist = s.get("distance")
        try:
            dist = float(dist) if dist is not None else None
        except (TypeError, ValueError):
            dist = None
        if dist is not None and dist > radius:
            continue
        stores.append({"id": str(sid), "name": clean_text(s.get("location_name")) or None, "distance": dist,
                       "state": dig(s, "mailing_address", "region") or dig(s, "mailing_address", "state")})
    stores.sort(key=lambda s: s["distance"] if s["distance"] is not None else float("inf"))
    stores = stores[:MAX_PICKUP_STORES]
    _stores_cache[ck] = (time.monotonic() + (STORES_TTL if stores else STORES_EMPTY_TTL), stores)
    return stores


_VENDOR_3P_RE = re.compile(r"market|partner|target\s*plus|third|3p|seller|drop\s*ship|dsv", re.IGNORECASE)
_TARGET_RE = re.compile(r"^\s*target(?:\.com|\s+corp(?:oration)?|\s+brands?(?:,?\s+inc\.?)?)?\s*$", re.IGNORECASE)


def seller_of(item: dict) -> tuple[str | None, bool | None]:
    """(seller, third_party) for a pdp_client ``item``. ``fulfillment.is_marketplace`` decides when
    present; otherwise ``product_vendors[]`` marketplace hints (relationship_type / vendor type /
    marketplace flags naming a non-Target partner) mark a Target Plus listing. Plain vendor names are
    suppliers (e.g. "POKEMON USA INC" on first-party items), not sellers."""
    vendors = [v for v in (item.get("product_vendors") or []) if isinstance(v, dict)]
    names = [clean_text(v.get("vendor_name")) for v in vendors if clean_text(v.get("vendor_name"))]
    is_marketplace = dig(item, "fulfillment", "is_marketplace")
    if is_marketplace is True:
        return (names[0] if names else "Target Plus partner"), True
    if is_marketplace is False:
        return "Target", False
    for v in vendors:
        name = clean_text(v.get("vendor_name")) or None
        if name and _TARGET_RE.match(name):
            continue
        rel = " ".join(str(v.get(k) or "") for k in ("relationship_type", "relationship_type_code", "vendor_type",
                                                          "type", "seller_type", "fulfillment_type"))
        flags = [v.get(k) for k in ("is_marketplace", "is_target_plus", "is_partner", "marketplace")]
        if any(f is True for f in flags) or (rel.strip() and _VENDOR_3P_RE.search(rel)):
            return name or "Target Plus partner", True
    for key in ("is_marketplace", "is_target_plus"):
        if item.get(key) is True:
            return (names[0] if names else "Target Plus partner"), True
    return None, None


def _fulfillment_params(tcin: str, store_id: str | None, zip_code: str | None, state: str | None, *,
                        pickup: bool = True, lat: str | None = None, lng: str | None = None) -> dict:
    """Query for product_fulfillment_v1, mirroring what target.com sends. Without a store the store
    params are omitted (never the rejected digital store 3991)."""
    p: dict[str, Any] = {"is_bot": "false", "tcin": tcin, "zip": zip_code, "state": state,
                         "latitude": lat, "longitude": lng, "paid_membership": "false", "base_membership": "false",
                         "card_membership": "false", "channel": "WEB", "page": f"/p/A-{tcin}"}
    if store_id:
        p.update(store_id=store_id, scheduled_delivery_store_id=store_id)
        if pickup:
            p.update(required_store_id=store_id, has_required_store_id="true")
    return p


async def fulfillment(params: dict, tcin: str, page_url: str) -> Any:
    """product_fulfillment_v1, falling back to the legacy pdp_fulfillment_v1 when the current one is
    missing (404/410). The first endpoint's error is raised if both fail."""
    first: FetchError | None = None
    for endpoint in FULFILLMENT_ENDPOINTS:
        try:
            return await _redsky(endpoint, params, tcin, page_url)
        except FetchError as e:
            if e.status not in (404, 410):
                raise
            first = first or e
    raise first  # type: ignore[misc]


async def _delivery_location(rc: Any, tcin: str, page_url: str, *, zip_tried: bool = False) -> dict:
    """A real store (plus ZIP/state) for delivery-only lookups: pinned store → nearest to the item's
    ZIP → target.com's geo-IP store for this server → nearest to DEFAULT_ZIP → {} (no store)."""
    if rc.store_id:
        return {"store_id": rc.store_id, "zip": rc.zip}
    for place in ([rc.zip] if rc.zip and not zip_tried else []):
        try:
            found = await nearby_stores(place, max(rc.radius_miles, 50), tcin, page_url)
        except FetchError as e:
            if e.status in (401, 403):
                raise
            found = None
        if found:
            return {"store_id": found[0]["id"], "zip": place, "state": found[0].get("state")}
    await api_key(page_url)  # also scrapes the page's location
    loc = _key_cache.get("loc")
    if loc and loc.get("store_id"):
        return {**loc, "zip": rc.zip or loc.get("zip"), "state": loc.get("state") if not rc.zip else None}
    try:
        found = await nearby_stores(DEFAULT_ZIP, 50, tcin, page_url)
    except FetchError as e:
        if e.status in (401, 403):
            raise
        found = None
    if found:
        return {"store_id": found[0]["id"], "zip": rc.zip or DEFAULT_ZIP,
                "state": found[0].get("state") if not rc.zip else None}
    return {"zip": rc.zip}


def _pickup_label(name: str | None, sid: str, dist: float | None) -> str:
    where = name or f"store #{sid}"
    return f"Pickup · {where} ({dist:.1f} mi)" if dist is not None else f"Pickup · {where}"


async def check(url: str, ctx: AdapterContext) -> CheckResult | None:
    tcin = tcin_from_url(url)
    if not tcin:
        return None
    rc = ctx.retailer_config
    page_url = f"https://www.target.com/p/-/A-{tcin}"
    has_location = bool(rc.zip or rc.store_id)
    if rc.fulfillment == "pickup" and not has_location:
        return error_result(ctx, "Set a ZIP code for pickup", tcin=tcin)
    want_pickup = rc.wants_pickup and has_location

    # ---- stores near the ZIP (or the pinned store)
    stores: list[dict] = []
    stores_failed = False
    if want_pickup:
        if rc.store_id:
            stores = [{"id": rc.store_id, "name": None, "distance": None, "state": None}]
        else:
            try:
                found = await nearby_stores(rc.zip, rc.radius_miles, tcin, page_url)
            except FetchError as e:
                if rc.fulfillment == "pickup" or e.status in (401, 403):
                    raise
                log.info("target nearby_stores failed for %s: %s", rc.zip, e)
                found = None
            stores_failed = found is None
            stores = found or []
    primary = stores[0]["id"] if stores else None
    # delivery-only (or no pickup store found): still use a real store, never the digital 3991
    loc = ({"store_id": primary, "zip": rc.zip, "state": stores[0].get("state")} if primary
           else await _delivery_location(rc, tcin, page_url, zip_tried=want_pickup))
    store = loc.get("store_id")

    # ---- product info (title/price/seller); failures here are not fatal
    product: dict = {}
    try:
        pdp = await _redsky("pdp_client_v1", {
            "tcin": tcin, "is_bot": "false", "store_id": store, "pricing_store_id": store,
            "has_pricing_store_id": "true" if store else None,
            "has_financing_options": "true", "channel": "WEB", "page": f"/p/A-{tcin}"}, tcin, page_url)
        product = dig(pdp, "data", "product", default={}) or {}
    except FetchError as e:
        if e.status in (401, 403):
            raise
        if e.status in (404, 410):  # {"errors":[{"message":"No product found with tcin …"}]}
            return error_result(ctx, f"Product page not found (HTTP {e.status})", tcin=tcin)
        log.info("target pdp_client failed for %s: %s", tcin, e)
    item = product.get("item") or {}
    title = clean_text(dig(item, "product_description", "title")) or None
    image = dig(item, "enrichment", "images", "primary_image_url")
    price = (dig(product, "price", "current_retail") or dig(product, "price", "current_retail_min")
             or dig(product, "price", "formatted_current_price"))
    seller, third_party = seller_of(item)

    detail = base_detail(ctx, tcin=tcin, seller=seller, third_party=third_party, fulfillment=rc.fulfillment)
    if rc.zip:
        detail["zip"] = rc.zip
    if rc.wants_pickup and not has_location:
        detail["pickup_skipped"] = "Set a ZIP code for pickup"

    common = dict(price=price, title=title, image_url=image, detail=detail)
    if third_party and rc.official_only:
        return result("out", THIRD_PARTY_TEXT, **common)
    if rc.fulfillment == "pickup" and stores_failed:
        return result(None, "Target didn't return nearby stores", **common)

    # ---- fulfillment
    ful_json = await fulfillment(_fulfillment_params(tcin, store, loc.get("zip"), loc.get("state"),
                                                     pickup=bool(primary), lat=loc.get("latitude"),
                                                     lng=loc.get("longitude")), tcin, page_url)
    ful = dig(ful_json, "data", "product", "fulfillment")
    if not isinstance(ful, dict):
        if dig(ful_json, "data", "product") is None and isinstance(ful_json, dict) and "data" in ful_json:
            return result("out", "No longer available", **common)
        if product.get("children"):
            return result(None, "Pick a specific variant (size/color) on Target", **common)
        return result(None, "Target returned no fulfillment data", **common)

    available: list[Availability] = []
    texts: list[str] = []
    ship_status = dig(ful, "shipping_options", "availability_status")
    detail["shipping_status"] = ship_status
    if rc.wants_delivery:
        if ship_status in SHIP_IN and not (ful.get("sold_out") is True and ship_status != "PRE_ORDER_SELLABLE"):
            label = SHIP_IN[ship_status]
            available.append(Availability(key=STOCK_KEY, label=label if label != "In stock" else "Delivery"))
            texts.append(label)
        else:
            texts.append(SHIP_OUT.get(ship_status or "", "Out of stock online" if ship_status else
                                      "Not sold online"))

    if want_pickup:
        by_id: dict[str, dict] = {}

        def absorb(fj: Any) -> None:
            for so in dig(fj, "data", "product", "fulfillment", "store_options", default=[]) or []:
                sid = so.get("location_id") or so.get("store_id")
                if sid is not None:
                    by_id.setdefault(str(sid), so)

        absorb(ful_json)
        for s in stores[1:]:
            if s["id"] in by_id:
                continue
            try:
                absorb(await fulfillment(_fulfillment_params(tcin, s["id"], rc.zip, s.get("state")),
                                         tcin, page_url))
            except FetchError as e:
                log.info("target fulfillment for store %s failed: %s", s["id"], e)
        pickup_stores = []
        for s in stores:
            so = by_id.get(s["id"]) or {}
            name = s["name"] or clean_text(so.get("location_name")) or None
            status = dig(so, "order_pickup", "availability_status")
            qty = so.get("location_available_to_promise_quantity")
            pickup_stores.append({"id": s["id"], "name": name, "distance": s["distance"], "status": status,
                                  "quantity": qty})
            if status in PICKUP_IN:
                available.append(Availability(key=f"pickup:{s['id']}", label=_pickup_label(name, s["id"],
                                                                                           s["distance"])))
        detail["pickup_stores"] = pickup_stores
        n = sum(1 for a in available if a.key.startswith("pickup:"))
        where = f"within {rc.radius_miles} mi of {rc.zip}" if rc.zip and not rc.store_id else "at your store"
        if n == 1:
            texts.append(next(a.label for a in available if a.key.startswith("pickup:")))
        elif n > 1:
            texts.append(f"Pickup at {n} stores")
        elif stores_failed:
            texts.append("Couldn't look up nearby stores")
        elif not stores:
            texts.append(f"No Target stores {where}")
        else:
            texts.append(f"No pickup {where}")

    if available:
        in_texts = [t for t in texts if t in SHIP_IN.values() or t.startswith("Pickup")]
        return result("in", " · ".join(in_texts) or "In stock", available=available, **common)
    return result("out", " · ".join(texts) or "Out of stock", **common)
