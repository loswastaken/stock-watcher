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

Best Buy Marketplace: third-party listings ("Sold & shipped by Abe's Electronics Center") keep an
"Add to cart" button state even when the cart then answers "This item is currently unavailable for
online purchase". With official sellers only (the default) a marketplace seller is "Third-party
sellers only (<seller>)"; either way "currently unavailable for online purchase" is out. Marketplace
SKUs are 8 digits (12357608) where Best Buy's own are 7: an 8-digit SKU whose seller can't be seen
is never reported in stock as if Best Buy sold it — the page is read for the seller first.
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


_SELLER_NAME_KEYS = {"sellername", "sellerdisplayname", "marketplacesellername", "soldby", "soldbyname",
                     "sellerdisplay", "merchantname", "vendorname"}
_MARKETPLACE_BOOL_KEYS = {"marketplace", "ismarketplace", "ismarketplaceitem", "marketplaceitem", "ismarketplaceseller",
                          "marketplaceseller", "isthirdparty", "thirdparty", "isthirdpartyseller", "ismarketplacesku"}
_MARKETPLACE_TYPE_KEYS = {"sellertype", "fulfillmenttype", "offertype", "listingtype", "sellingchannel", "channel"}
_MARKETPLACE_TYPE_RE = re.compile(r"^(?:marketplace|3p|third[_\s-]?party|mkp)$", re.IGNORECASE)
# other offers of the same SKU (open box, other sellers): never the featured offer's seller
_OTHER_OFFER_KEYS = {"productoptions", "multiplesellers", "openbox", "alternateoffers", "otheroffers"}


def _is_best_buy(name: str) -> bool:
    return bool(re.match(r"^\s*best\s*buy\b", name or "", re.IGNORECASE))


def _seller_info(obj: Any) -> tuple[str | None, bool | None]:
    """Marketplace detection in Best Buy JSON (priceBlocks / button-state): seller name fields
    (``sellerName``, ``seller: {name}`` ...), marketplace flags (``isMarketplace`` ...) and seller/offer
    type strings ("MARKETPLACE", "3P"). Other offers of the SKU (``productOptions.multipleSellers``)
    are skipped. Field names beyond the recorded first-party answers are best-effort."""
    seller, third = None, None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                kl = k.lower()
                if kl in _OTHER_OFFER_KEYS:
                    continue
                if kl in _SELLER_NAME_KEYS and isinstance(v, str) and clean_text(v):
                    seller = seller or clean_text(v)
                elif kl == "seller" and isinstance(v, dict):
                    name = next((clean_text(v[x]) for x in ("displayName", "name", "sellerName")
                                 if isinstance(v.get(x), str) and clean_text(v[x])), None)
                    seller = seller or name
                    stack.append(v)
                elif kl == "seller" and isinstance(v, str) and clean_text(v):
                    seller = seller or clean_text(v)
                elif kl in _MARKETPLACE_BOOL_KEYS and isinstance(v, bool):
                    third = v if third is None else (third or v)
                elif kl in _MARKETPLACE_TYPE_KEYS and isinstance(v, str) and _MARKETPLACE_TYPE_RE.match(v.strip()):
                    third = True
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    if seller and third is None:
        third = not _is_best_buy(seller)
    if third is False and not seller:
        seller = "Best Buy"
    return seller, third


def marketplace_sku(sku: str | None) -> bool:
    """Best Buy's own SKUs are 7 digits; Marketplace listings got 8-digit ones (12357608)."""
    return bool(sku) and len(sku) >= 8


def third_party_text(seller: str | None) -> str:
    return f"{THIRD_PARTY_TEXT} ({seller[:60]})" if seller and not _is_best_buy(seller) else THIRD_PARTY_TEXT


UNAVAILABLE_ONLINE = "Unavailable online"
HIGH_DEMAND_TEXT = "High-demand item (reservation flow) — stock not confirmed"
SELLER_UNCONFIRMED = "Couldn't confirm the seller (Best Buy Marketplace listing?)"


def _build(ctx: AdapterContext, sku: str, verdict: str | None, text: str, *, source: str,
           available: list[Availability] | None = None, price: Any = None, title: str | None = None,
           image: str | None = None, seller: str | None = None, third_party: bool | None = None,
           button_state: str | None = None, **extra: Any) -> CheckResult:
    detail = base_detail(ctx, sku=sku, source=source, seller=seller, third_party=third_party,
                         cart_url=cart_url(sku), button_state=button_state)
    detail.update({k: v for k, v in extra.items() if v is not None})
    if verdict == "in" and third_party and ctx.retailer_config.official_only:
        return result("out", third_party_text(seller), price=price, title=title, image_url=image, detail=detail)
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


BSIN_URL_RE = re.compile(r"/product/[^/?#]+/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)
_CANON_RE = re.compile(r'<(?:link|meta)\b[^>]*(?:rel=["\']canonical["\']|property=["\']og:url["\'])[^>]*>', re.I)
_HREF_RE = re.compile(r'(?:href|content)=["\']([^"\']+)["\']', re.I)
_SKU_LABEL_RE = re.compile(r">\s*SKU\s*:?\s*(?:<[^>]*>\s*)*(\d{7,8})\b")


def sku_from_page(html: str, url: str) -> str | None:
    """The SKU of a ``/product/{slug}/{BSIN}`` page: canonical / og:url, the "SKU: 12357608" label, the
    skuId next to the page's BSIN, then the first SKU attribute/JSON on the page."""
    for tag in _CANON_RE.findall(html or ""):
        m = _HREF_RE.search(tag)
        sku = sku_from_url(m.group(1)) if m else None
        if sku:
            return sku
    m = _SKU_LABEL_RE.search(html or "")
    if m:
        return m.group(1)
    b = BSIN_URL_RE.search(urlsplit(url).path)
    if b:
        spots = [x.start() for x in re.finditer(re.escape(b.group(1)), html or "")]
        best = None
        for m in _SKU_JSON_RE.finditer(html or ""):
            d = min((abs(m.start() - x) for x in spots), default=None)
            if d is not None and d <= 1500 and (best is None or d < best[0]):
                best = (d, m.group(1))
        if best:
            return best[1]
    return next((m.group(1) for rx in SKU_PAGE_RES for m in [rx.search(html or "")] if m), None)


async def check(url: str, ctx: AdapterContext) -> CheckResult | None:
    rc = ctx.retailer_config
    sku = sku_from_url(url)
    purl = page_url(url)
    fetched: FetchResult | None = None
    if not sku:
        if "/product/" not in urlsplit(url).path:
            return None
        # /product/{slug}/{BSIN}: only the page knows the SKU. A failure here propagates (the check
        # reports it, e.g. "Best Buy refused the connection (bot protection) — retrying later").
        fetched = await fetcher.fetch_html(purl, needs=_page_needs)
        if is_queued(fetched):
            return queued_result(ctx)
        sku = sku_from_page(fetched.text, url)
        if not sku:
            res = generic_result(fetched, url, ctx, source="page-generic")
            return apply_page_signals(res, page_signals(fetched.text, None), ctx)

    key = os.environ.get("BESTBUY_API_KEY", "").strip()
    api_error: FetchError | None = None
    if key:
        try:
            return await _confirm(await _api_check(sku, key, ctx), sku, purl, fetched, ctx)
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
            res = _build(ctx, sku, verdict, _pickup_note(text, ctx), source="priceBlocks", price=price, title=title,
                         seller=seller, third_party=third, button_state=state)
            return await _confirm(res, sku, purl, fetched, ctx, api_text=dig(s, "buttonState", "displayText"))
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
                res = _build(ctx, sku, verdict, _pickup_note(text, ctx), source="button-state", seller=seller,
                             third_party=third, button_state=state)
                return await _confirm(res, sku, purl, fetched, ctx, api_text=info.get("displayText"))

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
    res.detail.update(seller=None, third_party=None)
    apply_page_signals(res, page_signals(fetched.text, sku), ctx)
    if res.status == "in_stock" and rc.official_only and res.detail.get("third_party") is None \
            and marketplace_sku(sku):
        override(res, None, SELLER_UNCONFIRMED, matched="marketplace-range SKU, no seller on the page")
    if rc.wants_pickup:
        res.status_text = _pickup_note(res.status_text, ctx)
    return res


_UNAVAILABLE_ONLINE_RE = re.compile(r"(?:currently\s+)?unavailable\s+for\s+online\s+purchase", re.IGNORECASE)
_HIGH_DEMAND_RE = re.compile(r"\bhigh[\s-]+demand\s+(?:product|item)\b", re.IGNORECASE)


def page_signals(html: str, sku: str | None) -> dict:
    """What the product page says beyond the button: the featured seller ("Sold & shipped by X"),
    "currently unavailable for online purchase", and the "High Demand Product" reservation notice."""
    text = _visible(html)
    seller = _seller_from_text(text) or _seller_from_json(html, sku)
    return {"seller": seller, "third_party": (not _is_best_buy(seller)) if seller else None,
            "unavailable_online": bool(_UNAVAILABLE_ONLINE_RE.search(text)),
            "high_demand": bool(_HIGH_DEMAND_RE.search(text))}


def apply_page_signals(res: CheckResult, sig: dict, ctx: AdapterContext) -> CheckResult:
    """Page facts win over an optimistic verdict (an API "Add to cart" or the page's JSON-LD InStock):
    a marketplace seller with official sellers only, or "unavailable for online purchase", is out."""
    if sig.get("seller") and res.detail.get("third_party") is None:
        res.detail.update(seller=sig["seller"], third_party=sig["third_party"], seller_source="page")
    if sig.get("high_demand"):
        res.detail["high_demand"] = True
    if res.status != "in_stock":
        return res
    if sig.get("unavailable_online"):
        return override(res, "out", UNAVAILABLE_ONLINE, matched="page: currently unavailable for online purchase")
    if res.detail.get("third_party") and ctx.retailer_config.official_only:
        return override(res, "out", third_party_text(res.detail.get("seller")),
                        matched=f"seller: {res.detail.get('seller')}")
    if sig.get("high_demand") and (res.detail.get("source") == "page-generic" or res.detail.get("third_party")):
        # the reservation flow decides at add-to-cart time (a marketplace listing's cart then said "currently
        # unavailable for online purchase"): structured data or a marketplace button can't confirm stock
        return override(res, None, HIGH_DEMAND_TEXT, matched="page: High Demand Product")
    if sig.get("high_demand") and res.status_text == "In stock":
        res.status_text = "In stock (high-demand: reservation queue)"
        res.available = [Availability(key=STOCK_KEY, label=res.status_text)]
    return res


async def _confirm(res: CheckResult, sku: str, purl: str, fetched: FetchResult | None, ctx: AdapterContext,
                   *, api_text: Any = None) -> CheckResult:
    """An "in stock" from the APIs is only as good as its seller: check the page when it's at hand or
    when the SKU is a marketplace-range one whose seller the API didn't name."""
    if res.status != "in_stock":
        return res
    if isinstance(api_text, str) and _UNAVAILABLE_ONLINE_RE.search(api_text):
        return override(res, "out", UNAVAILABLE_ONLINE, matched=f"button displayText: {api_text[:80]}")
    if fetched is None and res.detail.get("third_party") is None and marketplace_sku(sku):
        try:
            fetched = await fetcher.fetch_html(purl, needs=_page_needs)
        except FetchError as e:
            log.info("bestbuy page for seller check failed for %s: %s", sku, e)
            fetched = None
        if fetched is not None and is_queued(fetched):
            fetched = None
    if fetched is not None:
        apply_page_signals(res, page_signals(fetched.text, sku), ctx)
    if res.status == "in_stock" and ctx.retailer_config.official_only and res.detail.get("third_party") is None \
            and marketplace_sku(sku):
        override(res, None, SELLER_UNCONFIRMED, matched="marketplace-range SKU, seller not shown")
    return res


_SOLD_BY_RE = re.compile(
    r"\b(?:Sold\s*(?:&|and)\s*shipped\s+by|Ships\s+from\s+and\s+sold\s+by|Sold\s+by)\s*:?\s+"
    r"([A-Z0-9][^\s]*(?:\s+[^\s]+){0,7})")
_SELLER_STOP = {"seller", "rating", "ratings", "ships", "shipped", "sold", "learn", "see", "view", "visit", "get",
                "free", "pickup", "add", "return", "returns", "reviews", "review", "more", "details", "opens",
                "contact", "sku", "model", "delivery", "shipping", "about", "|", "·", "-", "–", "—", "(", "."}


def _seller_from_text(text: str) -> str | None:
    m = _SOLD_BY_RE.search(text or "")
    if not m:
        return None
    words: list[str] = []
    for w in m.group(1).split():
        bare = w.strip(".,;:()").lower()
        if words and (bare in _SELLER_STOP or re.match(r"^[\d.,()/]+$", w) or not (w[:1].isupper() or w[:1].isdigit()
                                                                              or bare in {"of", "and", "the", "&"})):
            break
        words.append(w)
        if w.endswith((".", ",", ";")) and not re.search(r"\b(?:inc|llc|co|ltd)\.$", w, re.I):
            break
    name = " ".join(words).rstrip(".,;: ")
    return name[:80] or None


_SELLER_JSON_RE = re.compile(r'\\?"(?:sellerName|sellerDisplayName|marketplaceSellerName)\\?"\s*:\s*\\?"([^"\\]{2,80})')


def _seller_from_json(html: str, sku: str | None) -> str | None:
    """A seller name in the page's embedded JSON, only when it sits next to this SKU (carousels list
    other products' sellers)."""
    if not sku:
        return None
    skus = [(m.start(), m.group(1)) for m in _SKU_JSON_RE.finditer(html or "")]
    for m in _SELLER_JSON_RE.finditer(html or ""):
        near = min(skus, key=lambda p: abs(p[0] - m.start()), default=None)
        if near is not None and abs(near[0] - m.start()) <= 1500 and near[1] == sku:
            return clean_text(m.group(1))
    return None


def _visible(html: str) -> str:
    soup = soup_of(html)
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    return clean_text(soup.get_text(" ", strip=True))


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
