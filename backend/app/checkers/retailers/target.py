"""Target: Redsky JSON APIs.

* ``pdp_client_v1``      → title, price, image, Target Plus (marketplace) seller
* ``pdp_fulfillment_v1`` → ``shipping_options`` (delivery) and ``store_options`` (order pickup per store)
* ``nearby_stores_v1``   → stores within ``radius_miles`` of the configured ZIP

The API key is scraped from the product page (cached) with a well-known fallback key.
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
DEFAULT_STORE = "3991"  # used for pricing when no store is known (common public default)
KEY_TTL = 6 * 3600
STORES_TTL = 12 * 3600
MAX_PICKUP_STORES = 5

SHIP_IN = {"IN_STOCK": "In stock", "LIMITED_STOCK": "Limited stock", "PRE_ORDER_SELLABLE": "Pre-order"}
SHIP_OUT = {"OUT_OF_STOCK": "Out of stock", "PRE_ORDER_UNSELLABLE": "Pre-order sold out",
            "UNAVAILABLE": "Not available online", "DISCONTINUED": "Discontinued"}
PICKUP_IN = {"IN_STOCK", "LIMITED_STOCK"}

_key_cache: dict[str, Any] = {"key": None, "exp": 0.0}
_stores_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}


def tcin_from_url(url: str) -> str | None:
    m = PRESELECT_RE.search(url) or TCIN_RE.search(url.split("#", 1)[0].split("?", 1)[0] + "?")
    return m.group(1) if m else None


def _headers(tcin: str) -> dict[str, str]:
    return {"Origin": "https://www.target.com", "Referer": f"https://www.target.com/p/-/A-{tcin}",
            "Sec-Fetch-Site": "same-site"}


async def api_key(page_url: str, *, refresh: bool = False) -> str:
    now = time.monotonic()
    if not refresh and _key_cache["key"] and _key_cache["exp"] > now:
        return _key_cache["key"]
    key = None
    try:
        resp = await fetcher.http_get(page_url)
        m = API_KEY_RE.search(resp.text or "")
        key = m.group(1) if m else None
    except FetchError as e:
        log.info("target: could not load PDP for apiKey (%s); using fallback key", e)
    _key_cache["key"] = key or FALLBACK_KEY
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


async def nearby_stores(zip_code: str, radius: int, tcin: str, page_url: str) -> list[dict]:
    ck = (zip_code, radius)
    hit = _stores_cache.get(ck)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    data = await _redsky("nearby_stores_v1", {"limit": MAX_PICKUP_STORES, "within": radius, "place": zip_code,
                                              "channel": "WEB", "page": f"/p/A-{tcin}"}, tcin, page_url)
    stores = []
    for s in dig(data, "data", "nearby_stores", "stores", default=[]) or []:
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
    _stores_cache[ck] = (time.monotonic() + STORES_TTL, stores)
    return stores


def _fulfillment_params(tcin: str, store_id: str | None, zip_code: str | None, state: str | None) -> dict:
    p: dict[str, Any] = {"is_bot": "false", "tcin": tcin, "zip": zip_code, "state": state, "channel": "WEB",
                         "page": f"/p/A-{tcin}"}
    if store_id:
        p.update(store_id=store_id, scheduled_delivery_store_id=store_id, required_store_id=store_id,
                 has_required_store_id="true")
    else:  # the endpoint expects a store even for shipping-only lookups
        p.update(store_id=DEFAULT_STORE, scheduled_delivery_store_id=DEFAULT_STORE)
    return p


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
    if want_pickup:
        if rc.store_id:
            stores = [{"id": rc.store_id, "name": None, "distance": None, "state": None}]
        else:
            stores = await nearby_stores(rc.zip, rc.radius_miles, tcin, page_url)
    primary = stores[0]["id"] if stores else None

    # ---- product info (title/price/seller); failures here are not fatal
    product: dict = {}
    try:
        pdp = await _redsky("pdp_client_v1", {
            "tcin": tcin, "is_bot": "false", "store_id": primary or DEFAULT_STORE,
            "pricing_store_id": primary or DEFAULT_STORE, "has_pricing_store_id": "true",
            "has_financing_options": "true", "channel": "WEB", "page": f"/p/A-{tcin}"}, tcin, page_url)
        product = dig(pdp, "data", "product", default={}) or {}
    except FetchError as e:
        if e.status in (401, 403):
            raise
        log.info("target pdp_client failed for %s: %s", tcin, e)
    item = product.get("item") or {}
    title = clean_text(dig(item, "product_description", "title")) or None
    image = dig(item, "enrichment", "images", "primary_image_url")
    price = (dig(product, "price", "current_retail") or dig(product, "price", "current_retail_min")
             or dig(product, "price", "formatted_current_price"))
    vendors = [v.get("vendor_name") for v in (item.get("product_vendors") or []) if isinstance(v, dict)]
    is_marketplace = dig(item, "fulfillment", "is_marketplace")
    if is_marketplace is True:
        seller, third_party = (vendors[0] if vendors and vendors[0] else "Target Plus partner"), True
    elif is_marketplace is False:
        seller, third_party = "Target", False
    else:
        seller, third_party = None, None

    detail = base_detail(ctx, tcin=tcin, seller=seller, third_party=third_party, fulfillment=rc.fulfillment)
    if rc.zip:
        detail["zip"] = rc.zip
    if rc.wants_pickup and not has_location:
        detail["pickup_skipped"] = "Set a ZIP code for pickup"

    common = dict(price=price, title=title, image_url=image, detail=detail)
    if third_party and rc.official_only:
        return result("out", THIRD_PARTY_TEXT, **common)

    # ---- fulfillment
    state = stores[0].get("state") if stores else None
    ful_json = await _redsky("pdp_fulfillment_v1", _fulfillment_params(tcin, primary, rc.zip, state), tcin, page_url)
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
                absorb(await _redsky("pdp_fulfillment_v1", _fulfillment_params(tcin, s["id"], rc.zip, s.get("state")),
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
        elif not stores:
            texts.append(f"No Target stores {where}")
        else:
            texts.append(f"No pickup {where}")

    if available:
        in_texts = [t for t in texts if t in SHIP_IN.values() or t.startswith("Pickup")]
        return result("in", " · ".join(in_texts) or "In stock", available=available, **common)
    return result("out", " · ".join(texts) or "Out of stock", **common)
