"""Kroger family of stores (Kroger, Ralphs, Fred Meyer, King Soopers, ...): official Kroger API.

Only used when ``KROGER_CLIENT_ID`` and ``KROGER_CLIENT_SECRET`` are set (free developer account at
developer.kroger.com); otherwise returns None so the generic browser path handles the page.
Stock is per store: we use the store nearest the item's ZIP (or its pinned ``store_id``).
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import fetcher
from ..base import Availability, CheckResult
from ..fetcher import FetchError
from ..util import clean_text
from .base import STOCK_KEY, AdapterContext, dig, get_json, result
from .bigbox import base_detail, error_result

log = logging.getLogger("stockwatcher.checkers.retailers.kroger")

UPC_RE = re.compile(r"/p/[^/?#]+/(\d{13})(?:[/?#]|$)")
API = "https://api.kroger.com/v1"
TOKEN_URL = f"{API}/connect/oauth2/token"
SCOPE = "product.compact"
IN_LEVELS = {"HIGH": "In stock", "LOW": "Low stock"}
OUT_LEVELS = {"TEMPORARILY_OUT_OF_STOCK": "Temporarily out of stock"}
LOCATIONS_TTL = 24 * 3600

_token: dict[str, Any] = {"value": None, "exp": 0.0, "client": None}
_locations: dict[tuple[str, int], tuple[float, list[dict]]] = {}


def upc_from_url(url: str) -> str | None:
    m = UPC_RE.search(url)
    return m.group(1) if m else None


def credentials() -> tuple[str, str] | None:
    cid = os.environ.get("KROGER_CLIENT_ID", "").strip()
    secret = os.environ.get("KROGER_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


async def access_token(creds: tuple[str, str], *, refresh: bool = False) -> str:
    now = time.monotonic()
    if not refresh and _token["value"] and _token["client"] == creds[0] and _token["exp"] > now:
        return _token["value"]
    client = fetcher.get_client()
    try:
        async with fetcher.host_slot(TOKEN_URL):
            resp = await client.post(
                TOKEN_URL, data={"grant_type": "client_credentials", "scope": SCOPE}, auth=creds,
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    except httpx.HTTPError as e:
        raise FetchError(f"Kroger API token request failed: {type(e).__name__}") from e
    if resp.status_code >= 400:
        raise FetchError(f"Kroger API rejected the client credentials (HTTP {resp.status_code})",
                         status=resp.status_code)
    try:
        body = resp.json()
        tok = str(body["access_token"])
        ttl = float(body.get("expires_in") or 1800)
    except (ValueError, KeyError, TypeError) as e:
        raise FetchError("Kroger API returned an unexpected token response") from e
    _token.update(value=tok, exp=now + max(60.0, ttl - 60), client=creds[0])
    return tok


async def _api(path: str, params: dict[str, Any], creds: tuple[str, str]) -> Any:
    url = f"{API}/{path}?{urlencode(params)}" if params else f"{API}/{path}"
    for attempt in range(2):
        tok = await access_token(creds, refresh=attempt > 0)
        try:
            return await get_json(url, headers={"Authorization": f"Bearer {tok}"})
        except FetchError as e:
            if attempt == 0 and e.status == 401:
                continue
            raise
    return None  # pragma: no cover


async def nearest_locations(zip_code: str, radius: int, creds: tuple[str, str]) -> list[dict]:
    ck = (zip_code, radius)
    hit = _locations.get(ck)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    data = await _api("locations", {"filter.zipCode.near": zip_code, "filter.radiusInMiles": radius,
                                    "filter.limit": 5}, creds)
    locs = []
    for loc in (data or {}).get("data") or []:
        lid = loc.get("locationId")
        if lid:
            name = clean_text(loc.get("name")) or clean_text(loc.get("chain")) or f"store #{lid}"
            locs.append({"id": str(lid), "name": name, "address": dig(loc, "address", "addressLine1")})
    _locations[ck] = (time.monotonic() + LOCATIONS_TTL, locs)
    return locs


def _image(product: dict) -> str | None:
    images = product.get("images") or []
    front = next((i for i in images if i.get("perspective") == "front"), images[0] if images else None)
    sizes = (front or {}).get("sizes") or []
    for want in ("large", "xlarge", "medium"):
        s = next((s for s in sizes if s.get("size") == want and s.get("url")), None)
        if s:
            return s["url"]
    return sizes[0].get("url") if sizes else None


async def check(url: str, ctx: AdapterContext) -> CheckResult | None:
    creds = credentials()
    upc = upc_from_url(url)
    if not creds or not upc:
        return None
    rc = ctx.retailer_config
    if not (rc.zip or rc.store_id):
        if rc.fulfillment == "pickup":
            return error_result(ctx, "Set a ZIP code for pickup", upc=upc)
        return None  # stock is per store; without a location the page itself is the best we have

    if rc.store_id:
        store = {"id": rc.store_id, "name": f"store #{rc.store_id}"}
    else:
        locs = await nearest_locations(rc.zip, rc.radius_miles, creds)
        if not locs:
            return result("out", f"No Kroger-family stores within {rc.radius_miles} mi of {rc.zip}",
                          detail=base_detail(ctx, upc=upc, zip=rc.zip))
        store = locs[0]

    data = await _api(f"products/{upc}", {"filter.locationId": store["id"]}, creds)
    product = (data or {}).get("data")
    if isinstance(product, list):
        product = product[0] if product else None
    detail = base_detail(ctx, upc=upc, store_id=store["id"], store_name=store["name"], seller=ctx.retailer.name,
                         third_party=False)
    if not isinstance(product, dict):
        return result("out", "Not sold at this store", detail=detail)
    item = (product.get("items") or [{}])[0] or {}
    level = str(dig(item, "inventory", "stockLevel") or "").upper() or None
    ful = item.get("fulfillment") if isinstance(item.get("fulfillment"), dict) else {}
    flags = {str(k).lower(): v for k, v in ful.items()}  # shipToHome / shiptohome / ShipToHome
    promo, regular = dig(item, "price", "promo"), dig(item, "price", "regular")
    price = promo if isinstance(promo, (int, float)) and promo > 0 else regular
    detail.update(stock_level=level, fulfillment_options=ful or None)
    common = dict(price=price, title=clean_text(product.get("description")) or None, image_url=_image(product),
                  detail=detail)

    if level is None:
        return result(None, f"Kroger didn't report stock at {store['name']}", **common)
    if level not in IN_LEVELS:
        return result("out", f"{OUT_LEVELS.get(level, 'Out of stock')} at {store['name']}", **common)

    available: list[Availability] = []
    texts: list[str] = []
    stock_text = IN_LEVELS[level]
    if rc.wants_pickup and flags.get("curbside", True) is not False:
        label = f"Pickup · {store['name']}" + (" · low stock" if level == "LOW" else "")
        available.append(Availability(key=f"pickup:{store['id']}", label=label))
        texts.append(label)
    if rc.wants_delivery and (not flags or flags.get("delivery") or flags.get("shiptohome")):
        available.append(Availability(key=STOCK_KEY, label=f"Delivery · {stock_text}"))
        texts.append(f"{stock_text} for delivery")
    if not available:
        return result("out", f"Not offered for {rc.fulfillment} at {store['name']}", **common)
    return result("in", " · ".join(texts), available=available, **common)
