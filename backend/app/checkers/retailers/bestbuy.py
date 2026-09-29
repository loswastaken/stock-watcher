"""Best Buy.

Signals, first that answers wins:

1. Official Products API when ``BESTBUY_API_KEY`` is set (``orderable`` + ``onlineAvailability``,
   ``marketplace``), plus in-store pickup near the ZIP via ``/v1/products/{sku}/stores.json``.
2. ``/api/3.0/priceBlocks`` → ``sku.buttonState.buttonState`` + price.
3. ``/button-state/api/v5/button-state`` → ``buttonStateResponseInfos[0].buttonState``.
4. The product page's ``data-button-state`` attribute (page fetch falls back to the browser),
   then the generic checker on the same HTML.

Both URL styles are handled: ``/site/{slug}/{sku}.p?skuId={sku}`` and ``/product/{slug}/{BSIN}/sku/{sku}``
(a ``/product/`` URL without ``/sku/`` is resolved by reading the SKU from the page).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .. import fetcher
from ..base import Availability, CheckResult
from ..fetcher import FetchError, FetchResult
from ..util import clean_text
from .base import STOCK_KEY, AdapterContext, dig, get_json, result, soup_of
from .bigbox import THIRD_PARTY_TEXT, base_detail, error_result, generic_result, is_queued, override, queued_result

log = logging.getLogger("stockwatcher.checkers.retailers.bestbuy")

SKU_URL_RE = re.compile(r"[?&]skuId=(\d{5,9})|/sku/(\d{5,9})(?:[/?#]|$)|/(\d{7,8})\.p(?:[?#/]|$)")
SKU_PAGE_RES = [
    re.compile(r'data-sku-id=["\'](\d{5,9})["\']'),
    re.compile(r'\\?"skuId\\?"\s*:\s*\\?"?(\d{5,9})'),
]
BUTTON_STATE_RE = re.compile(r'data-button-state=["\']([A-Z_]+)["\']')
BUTTON_STATE_JSON_RE = re.compile(r'\\?"buttonState\\?"\s*:\s*\\?"([A-Z_]+)')

BUTTON_IN = {"ADD_TO_CART": "In stock", "PRE_ORDER": "Pre-order", "BUY_NOW": "In stock"}
BUTTON_OUT = {"SOLD_OUT": "Sold out", "COMING_SOON": "Coming soon", "CHECK_STORES": "Sold out online (check stores)",
              "UNAVAILABLE": "Unavailable", "NOT_AVAILABLE": "Unavailable", "SOLD_OUT_ONLINE": "Sold out online",
              "IN_STORE_ONLY": "In-store only"}
ORDERABLE_IN = {"Available": "In stock", "PreOrder": "Pre-order", "BackOrder": "Backorder"}
ORDERABLE_OUT = {"SoldOut": "Sold out", "ComingSoon": "Coming soon", "NotAvailable": "Unavailable"}

API = "https://api.bestbuy.com/v1"
WWW = "https://www.bestbuy.com"
API_FIELDS = ("sku,name,salePrice,regularPrice,onlineAvailability,orderable,inStoreAvailability,marketplace,"
              "image,url")
PICKUP_NEEDS_KEY = "Pickup needs BESTBUY_API_KEY"


def sku_from_url(url: str) -> str | None:
    m = SKU_URL_RE.search(url)
    return next((g for g in m.groups() if g), None) if m else None


def cart_url(sku: str) -> str:
    return f"https://api.bestbuy.com/click/-/{sku}/cart"


def page_url(url: str) -> str:
    """The product page with the international splash suppressed."""
    parts = urlsplit(url if "//" in url else f"https://{url}")
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "intl"]
    q.append(("intl", "nosplash"))
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, urlencode(q), ""))


def map_button(state: str | None) -> tuple[str | None, str]:
    s = (state or "").strip().upper()
    if s in BUTTON_IN:
        return "in", BUTTON_IN[s]
    if s in BUTTON_OUT:
        return "out", BUTTON_OUT[s]
    if "SOLD_OUT" in s or "UNAVAILABLE" in s:
        return "out", "Sold out"
    return None, f"Unrecognised button state {s}" if s else "Unknown"


def _seller_info(obj: Any) -> tuple[str | None, bool | None]:
    """Best-effort marketplace detection in Best Buy JSON (field names unverified)."""
    seller, third = None, None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                kl = k.lower()
                if kl in ("sellername", "sellerdisplayname", "marketplacesellername") and isinstance(v, str) and v:
                    seller = seller or v
                elif kl in ("marketplace", "ismarketplace", "ismarketplaceitem", "marketplaceitem") \
                        and isinstance(v, bool):
                    third = v if third is None else (third or v)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    if seller and third is None:
        third = not re.match(r"^\s*best\s*buy\b", seller, re.IGNORECASE)
    if third is False and not seller:
        seller = "Best Buy"
    return seller, third


def _build(ctx: AdapterContext, sku: str, verdict: str | None, text: str, *, source: str,
           available: list[Availability] | None = None, price: Any = None, title: str | None = None,
           image: str | None = None, seller: str | None = None, third_party: bool | None = None,
           button_state: str | None = None, **extra: Any) -> CheckResult:
    detail = base_detail(ctx, sku=sku, source=source, seller=seller, third_party=third_party,
                         cart_url=cart_url(sku), button_state=button_state)
    detail.update({k: v for k, v in extra.items() if v is not None})
    if verdict == "in" and third_party and ctx.retailer_config.official_only:
        return result("out", THIRD_PARTY_TEXT, price=price, title=title, image_url=image, detail=detail)
    return result(verdict, text, available=available, price=price, title=title, image_url=image, detail=detail)


_SLUG_RES = (re.compile(r"/site/([^/?#]+)/\d{5,9}\.p"), re.compile(r"/product/([^/?#]+)/"))
_WORD_RE = re.compile(r"[a-z0-9]{2,}")


def _slug_words(path_or_url: str | None) -> set[str]:
    path = urlsplit(path_or_url or "").path
    for rx in _SLUG_RES:
        m = rx.search(path)
        if m:
            return set(_WORD_RE.findall(m.group(1).lower()))
    return set()


def other_product(url: str, sku_block: dict) -> str | None:
    """Best Buy reuses SKU numbers: when the product behind the URL's SKU shares not one word with the
    URL's slug (a PS5 Pro link whose SKU is now a LEGO set), return that product's name."""
    want = _slug_words(url)
    name = clean_text(dig(sku_block, "names", "short") or dig(sku_block, "names", "title")) or ""
    have = _slug_words(sku_block.get("url")) | set(_WORD_RE.findall(name.lower()))
    if len(want) < 2 or not have or want & have:
        return None
    return name or sku_block.get("url") or "another product"


def _pickup_note(text: str, ctx: AdapterContext) -> str:
    """fulfillment=any without the API: the verdict is delivery-only; say pickup wasn't checked."""
    return f"{text} · {PICKUP_NEEDS_KEY}" if ctx.retailer_config.wants_pickup else text


# --------------------------------------------------------------------------- official API


async def _api_check(sku: str, key: str, ctx: AdapterContext) -> CheckResult:
    rc = ctx.retailer_config
    q = urlencode({"apiKey": key, "show": API_FIELDS, "format": "json"})
    data = await get_json(f"{API}/products/{sku}.json?{q}")
    if isinstance(data, dict) and isinstance(data.get("products"), list):  # search-style response
        data = data["products"][0] if data["products"] else {}
    orderable = data.get("orderable")
    online = data.get("onlineAvailability")
    marketplace = data.get("marketplace")
    third = marketplace if isinstance(marketplace, bool) else None
    seller = ("Marketplace seller" if third else "Best Buy") if third is not None else None
    price = data.get("salePrice") if data.get("salePrice") is not None else data.get("regularPrice")
    title, image = data.get("name"), data.get("image")

    available: list[Availability] = []
    texts: list[str] = []
    if rc.wants_delivery:
        if (orderable in ORDERABLE_IN and not (orderable == "Available" and online is False)) \
                or (orderable is None and online is True):
            label = ORDERABLE_IN.get(orderable, "In stock")
            if third and rc.official_only:
                texts.append(THIRD_PARTY_TEXT)
            else:
                available.append(Availability(key=STOCK_KEY, label=label))
                texts.append(label)
        else:
            texts.append(ORDERABLE_OUT.get(orderable or "", "Sold out online" if online is False else "Out of stock"))

    stores_detail = None
    if rc.wants_pickup:
        if not rc.zip:
            if rc.fulfillment == "pickup":
                return error_result(ctx, "Set a ZIP code for pickup", sku=sku, cart_url=cart_url(sku))
        else:
            sq = urlencode({"postalCode": rc.zip, "apiKey": key, "format": "json"})
            sj = await get_json(f"{API}/products/{sku}/stores.json?{sq}")
            stores_detail = []
            for s in (sj or {}).get("stores") or []:
                sid = str(s.get("storeID") or s.get("storeId") or "")
                if not sid:
                    continue
                try:
                    dist = float(s["distance"]) if s.get("distance") is not None else None
                except (TypeError, ValueError):
                    dist = None
                if rc.store_id and sid != rc.store_id:
                    continue
                if not rc.store_id and dist is not None and dist > rc.radius_miles:
                    continue
                name = clean_text(s.get("name")) or f"store #{sid}"
                low = bool(s.get("lowStock"))
                stores_detail.append({"id": sid, "name": name, "distance": dist, "low_stock": low})
                label = f"Pickup · {name}" + (f" ({dist:.1f} mi)" if dist is not None else "")
                available.append(Availability(key=f"pickup:{sid}", label=label + (" · low stock" if low else "")))
            n = len(stores_detail)
            texts.append(available[-1].label if n == 1 else f"Pickup at {n} stores" if n else
                         f"No pickup within {rc.radius_miles} mi of {rc.zip}")

    detail = base_detail(ctx, sku=sku, source="api", seller=seller, third_party=third,
                         cart_url=cart_url(sku), orderable=orderable, pickup_stores=stores_detail)
    if available:
        in_texts = [t for t in texts if t in ORDERABLE_IN.values() or t.startswith("Pickup")]
        return result("in", " · ".join(in_texts), available=available, price=price, title=title, image_url=image,
                      detail=detail)
    return result("out", " · ".join(texts) or "Out of stock", price=price, title=title, image_url=image,
                  detail=detail)


# --------------------------------------------------------------------------- www endpoints


def _api_headers(referer: str) -> dict[str, str]:
    return {"Referer": referer, "Origin": WWW, "Sec-Fetch-Site": "same-origin", "X-Requested-With": "XMLHttpRequest"}


async def _price_blocks(sku: str, referer: str) -> dict | None:
    data = await get_json(f"{WWW}/api/3.0/priceBlocks?skus={sku}", headers=_api_headers(referer))
    block = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
    return block if isinstance(dig(block, "sku"), dict) else None


async def _button_state(sku: str, referer: str) -> dict | None:
    data = await get_json(f"{WWW}/button-state/api/v5/button-state?skus={sku}&context=pdp&source=buttonView",
                          headers=_api_headers(referer))
    info = dig(data, "buttonStateResponseInfos", 0)
    return info if isinstance(info, dict) and info.get("buttonState") else None


def _page_needs(html: str) -> bool:
    return bool(BUTTON_STATE_RE.search(html) or BUTTON_STATE_JSON_RE.search(html))


async def check(url: str, ctx: AdapterContext) -> CheckResult | None:
    rc = ctx.retailer_config
    sku = sku_from_url(url)
    purl = page_url(url)
    fetched: FetchResult | None = None
    if not sku:
        if "/product/" not in urlsplit(url).path:
            return None
        fetched = await fetcher.fetch_html(purl, needs=_page_needs)
        if is_queued(fetched):
            return queued_result(ctx)
        sku = next((m.group(1) for rx in SKU_PAGE_RES for m in [rx.search(fetched.text)] if m), None)
        if not sku:
            return generic_result(fetched, url, ctx)

    key = os.environ.get("BESTBUY_API_KEY", "").strip()
    api_error: FetchError | None = None
    if key:
        try:
            return await _api_check(sku, key, ctx)
        except FetchError as e:
            api_error = e
            log.warning("bestbuy API failed for %s (%s); falling back to the website", sku, e)
    elif rc.fulfillment == "pickup" and not rc.zip:
        return error_result(ctx, "Set a ZIP code for pickup", sku=sku, cart_url=cart_url(sku))
    if rc.fulfillment == "pickup":
        # The website endpoints only describe shipping: an online "Add to cart" says nothing about
        # any store, so a pickup-only watch can't be answered without the official API.
        text = PICKUP_NEEDS_KEY if api_error is None else "Pickup check failed (Best Buy API error)"
        return result(None, text, detail=base_detail(ctx, sku=sku, source="none", cart_url=cart_url(sku),
                                                     pickup_unavailable=True))

    # ---- priceBlocks, then the button-state service
    network_down = False
    try:
        block = await _price_blocks(sku, purl)
    except FetchError as e:
        log.info("bestbuy priceBlocks failed for %s: %s", sku, e)
        block, network_down = None, e.status is None  # stalled/reset: the sibling endpoint will be too
    if block:
        s = block["sku"]
        other = other_product(url, s)
        if other:
            return error_result(ctx, f"This Best Buy SKU is now a different product ({other[:80]}) — update the link",
                                sku=sku, other_product=other, dead_link=True)
        state = dig(s, "buttonState", "buttonState")
        verdict, text = map_button(state)
        if verdict is not None:
            seller, third = _seller_info(block)
            price = dig(s, "price", "currentPrice")
            title = dig(s, "names", "short") or dig(s, "names", "title")
            return _build(ctx, sku, verdict, _pickup_note(text, ctx), source="priceBlocks", price=price, title=title,
                          seller=seller, third_party=third, button_state=state)
    if not network_down:
        try:
            info = await _button_state(sku, purl)
        except FetchError as e:
            log.info("bestbuy button-state failed for %s: %s", sku, e)
            info = None
        if info:
            state = info.get("buttonState")
            verdict, text = map_button(state)
            if verdict is not None:
                seller, third = _seller_info(info)
                return _build(ctx, sku, verdict, _pickup_note(text, ctx), source="button-state", seller=seller,
                              third_party=third, button_state=state)

    # ---- the page itself
    if fetched is None:
        fetched = await fetcher.fetch_html(purl, render_js=bool(ctx.generic_config.get("render_js")),
                                           needs=_page_needs)
        if is_queued(fetched):
            return queued_result(ctx, sku=sku)
    res = generic_result(fetched, url, ctx, sku=sku, cart_url=cart_url(sku))
    state = page_button_state(fetched.text, sku)
    if state:
        verdict, text = map_button(state)
        res.detail.update(source="page", button_state=state)
        if verdict is not None:
            override(res, verdict, text, matched=f"data-button-state={state}")
    else:
        res.detail["source"] = "page-generic"
    seller, third = _seller_info_from_page(fetched.text)
    res.detail.update(seller=seller, third_party=third)
    if res.status == "in_stock" and third and rc.official_only:
        override(res, "out", THIRD_PARTY_TEXT)
    if rc.wants_pickup:
        res.status_text = _pickup_note(res.status_text, ctx)
    return res


_SOLD_BY_RE = re.compile(r"\bSold\s+(?:and\s+shipped\s+)?by\s+([A-Z0-9][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,4})")


_SKU_JSON_RE = re.compile(r'\\?"sku(?:Id|ID|_id)?\\?"\s*:\s*\\?"?(\d{5,9})')


def page_button_state(html: str, sku: str) -> str | None:
    """The add-to-cart button state for ``sku`` only. Buttons / JSON blobs of other SKUs (carousels,
    bundles, "frequently bought together") never stand in for it: no match → None."""
    soup = soup_of(html)
    for el in soup.select(f'[data-sku-id="{sku}"][data-button-state], [data-sku-id="{sku}"] [data-button-state]'):
        own = el.get("data-sku-id")
        if own and str(own) != sku:
            continue
        if el.get("data-button-state"):
            return str(el["data-button-state"]).strip().upper()
    # JSON: a buttonState belongs to the nearest SKU id in the same blob
    skus = [(m.start(), m.group(1)) for m in _SKU_JSON_RE.finditer(html)]
    for m in BUTTON_STATE_JSON_RE.finditer(html):
        near = min(skus, key=lambda p: abs(p[0] - m.start()), default=None)
        if near is not None and abs(near[0] - m.start()) <= 800 and near[1] == sku:
            return m.group(1)
    return None


def _seller_info_from_page(html: str) -> tuple[str | None, bool | None]:
    soup = soup_of(html)
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    m = _SOLD_BY_RE.search(clean_text(soup.get_text(" ", strip=True)))
    if not m:
        return None, None
    seller = m.group(1).strip()
    return seller, not re.match(r"^best\s*buy\b", seller, re.IGNORECASE)
