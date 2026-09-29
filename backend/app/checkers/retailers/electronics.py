"""Electronics retailers: Newegg (ProductRealtime API), Nvidia Founders Edition (feinventory
API), Micro Center (per-store inventory), and page-vocabulary adapters for AMD, Adorama,
Antonline, B&H, Dell, EVGA, Gigabyte, LG, Leica, Lenovo, Meta Quest and Zotac.

Every adapter returns a ``CheckResult`` or None (platform recipes + generic checker run next).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .. import fetcher
from ..base import Availability, CheckResult
from ..fetcher import FetchError
from ..util import clean_text
from . import base as rbase
from .base import AdapterContext, dig, result
from .pagekit import (
    Hit,
    PageView,
    analyze_page,
    check_page,
    fetch_page,
    finish,
    is_queued,
    page_view,
    queue_result,
    rule,
    seller_detail,
    soup_text,
)

_PX_BLOCK_RE = re.compile(r"access to this page has been denied|press\s*&(?:amp;)?\s*hold|px-captcha", re.I)


def _raise_if_px_blocked(html: str, host: str) -> None:
    if _PX_BLOCK_RE.search((html or "")[:60_000]) and not fetcher.has_product_signals(html or ""):
        raise FetchError(f"Blocked by bot protection on {host} (PerimeterX)", status=403)


# =========================================================================== Newegg

_NE_ITEM_RES = (
    re.compile(r"[?&]Item=([A-Za-z0-9-]{6,})", re.I),
    re.compile(r"/p/(N82E\d{8,14})", re.I),
    re.compile(r"/p/([0-9A-Z]{2,4}-[0-9A-Z]{3,5}-[0-9A-Z]{4,6})(?:[/?#]|$)", re.I),
    re.compile(r"/p/(9SI[0-9A-Z]{8,})", re.I),
)
_NEWEGG_SELLER_RE = re.compile(r"^\s*newegg(?:\.com)?(?:\s+inc\.?)?\s*$", re.I)
NEWEGG_CART = "https://secure.newegg.com/Shopping/AddtoCart.aspx?Submit=ADD&ItemList={item}"
NEWEGG_API = "https://www.newegg.com/product/api/ProductRealtime?ItemNumber={item}"


def newegg_item(url: str) -> str | None:
    for rx in _NE_ITEM_RES:
        m = rx.search(url)
        if m:
            return m.group(1).upper()
    return None


def newegg_api_numbers(item: str) -> list[str]:
    """ProductRealtime takes the dashed item number (N82E16814932744 -> 14-932-744);
    marketplace/combo numbers go as-is. The raw number is tried second."""
    m = re.fullmatch(r"N82E168(\d{2})(\d{3})(\d{3})", item)
    return [f"{m.group(1)}-{m.group(2)}-{m.group(3)}", item] if m else [item]


def _is_newegg_seller(name: Any, seller_id: Any) -> bool | None:
    if isinstance(name, str) and name.strip():
        return bool(_NEWEGG_SELLER_RE.match(name))
    if seller_id in (None, ""):
        return None
    return str(seller_id).strip().lower() in ("", "0", "newegg")


async def newegg(url: str, ctx: AdapterContext) -> CheckResult | None:
    item = newegg_item(url)
    if item:
        for num in newegg_api_numbers(item):
            try:
                data = await rbase.get_json(NEWEGG_API.format(item=quote(num)), headers={"Referer": url})
            except FetchError:
                break  # blocked / bot check -> HTML fallback
            mi = data.get("MainItem") if isinstance(data, dict) else None
            if isinstance(mi, dict) and mi:
                return _newegg_from_api(mi, item, ctx)
    return await _newegg_html(url, ctx, item)


def _newegg_from_api(mi: dict, item: str, ctx: AdapterContext) -> CheckResult:
    rcfg = ctx.retailer_config
    title = clean_text(dig(mi, "Description", "Title") or dig(mi, "Description", "WebDescription")) or None
    img = dig(mi, "Image", "Normal", "ImageName")
    image_url = f"https://c1.neweggimages.com/ProductImage/{img}" if isinstance(img, str) and img else None
    price = mi.get("FinalPrice") if mi.get("FinalPrice") not in (None, "", 0) else mi.get("UnitCost")
    seller = mi.get("SellerName") or None
    first_party = _is_newegg_seller(seller, mi.get("SellerId"))
    cart_item = str(mi.get("Item") or item)
    instock = mi.get("Instock")
    stock = mi.get("Stock")
    if isinstance(instock, str):
        instock = instock.strip().lower() in ("true", "1", "yes")
    try:
        qty = int(stock) if stock is not None else None
    except (TypeError, ValueError):
        qty = None
    in_stock = bool(instock) and (qty is None or qty > 0)
    detail = {
        "item": cart_item,
        "stock_qty": qty,
        "seller": seller or ("Newegg" if first_party else None),
        "third_party": None if first_party is None else not first_party,
        "matched": f"ProductRealtime Instock={mi.get('Instock')} Stock={stock}",
    }
    if in_stock and first_party is False and rcfg.official_only:
        res = result("out", "Third-party sellers only", price=price, title=title, image_url=image_url, detail=detail)
    elif in_stock:
        detail["cart_url"] = NEWEGG_CART.format(item=cart_item)
        text = "In stock" if first_party is not False else f"In stock (sold by {seller})"
        res = result("in", text, price=price, title=title, image_url=image_url, detail=detail)
    else:
        res = result("out", "Out of stock", price=price, title=title, image_url=image_url, detail=detail)
    return finish(res, ctx, source="api")


_NE_SOLD_BY_RE = re.compile(r"\bsold\s+(?:and\s+shipped\s+)?by:?\s*(.{2,60})", re.I)
_NE_SELLER_END_RE = re.compile(r"\s*(?:\||\bships?\b|\bshipped\b|\bout\s+of\s+stock\b|\bin\s+stock\b|\bsold\b|\badd\s+to\b"
                               r"|\bauto\s+notify\b|[.,;(])", re.I)


def _newegg_seller_from_text(text: str) -> str | None:
    m = _NE_SOLD_BY_RE.search(text or "")
    if not m:
        return None
    seller = _NE_SELLER_END_RE.split(m.group(1), maxsplit=1)[0].strip()
    return seller or None


NEWEGG_RULES = [
    rule(r"^\W*auto\s*notify\W*$", "out", "Out of stock (Auto Notify)", strong=True, where="button"),
    rule(r"\bauto\s*notify\b", "out", "Out of stock (Auto Notify)", where="text"),
]


async def _newegg_html(url: str, ctx: AdapterContext, item: str | None) -> CheckResult | None:
    fetched = await fetch_page(url, ctx)
    if "areyouahuman" in (fetched.url or "").lower() or "areyouahuman" in (fetched.text or "")[:5000].lower():
        raise FetchError("Blocked by Newegg's bot check (areyouahuman)", status=403)
    if is_queued(fetched):
        return queue_result(ctx)
    res = analyze_page(fetched.text, url, ctx, fetched.url, rules=NEWEGG_RULES, extra={"source": "html"})
    if res is None:
        return None
    seller = _newegg_seller_from_text(page_view(fetched.text).text)
    if seller:
        first_party = bool(_NEWEGG_SELLER_RE.match(seller))
        res.detail.update(seller=seller, third_party=not first_party)
        if res.status == "in_stock" and not first_party and ctx.retailer_config.official_only:
            res.status, res.status_text, res.available = "out_of_stock", "Third-party sellers only", []
    if res.status == "in_stock" and item and res.detail.get("third_party") is not True:
        res.detail["cart_url"] = NEWEGG_CART.format(item=item)
    return res


# =========================================================================== Nvidia Founders Edition

NV_SEARCH = ("https://api.nvidia.partners/edge/product/search?page=1&limit=12&locale={locale}&category=GPU"
             "&gpu={gpu}&manufacturer=NVIDIA")
NV_INVENTORY = "https://api.store.nvidia.com/partner/v1/feinventory?skus={sku}&locale={locale}"
_NV_HEADERS = {"Origin": "https://marketplace.nvidia.com", "Referer": "https://marketplace.nvidia.com/",
               "Sec-Fetch-Site": "cross-site"}
_NV_GPU_RE = re.compile(r"rtx[-_ ]?(\d{4})((?:[-_ ]?(?:ti|super))*)", re.I)
_NV_SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9_]{4,30}$")


def nvidia_gpu(url: str) -> str | None:
    m = _NV_GPU_RE.search(urlsplit(url).path)
    if not m:
        return None
    suffix = " ".join(w.capitalize() if w.lower() == "super" else "Ti" for w in re.findall(r"ti|super", m.group(2), re.I))
    return f"RTX {m.group(1)}" + (f" {suffix}" if suffix else "")


def _nv_locale(url: str) -> str:
    m = re.search(r"/([a-z]{2}-[a-z]{2})(?:/|$)", urlsplit(url).path, re.I)
    return m.group(1).lower() if m else "en-us"


def _nv_sku(url: str, ctx: AdapterContext) -> str | None:
    q = parse_qs(urlsplit(url).query)
    for cand in (q.get("sku", [None])[0], ctx.retailer_config.store_id):
        if cand and _NV_SKU_RE.match(cand.strip().upper()):
            return cand.strip().upper()
    return None


def _nv_status(s: Any) -> str | None:
    s = str(s or "").strip().lower()
    if s in ("buy_now", "in_stock", "available"):
        return "in"
    if s in ("out_of_stock", "check_availability", "coming_soon", "not_available"):
        return "out"
    return None


async def nvidia(url: str, ctx: AdapterContext) -> CheckResult | None:
    locale = _nv_locale(url)
    sku = _nv_sku(url, ctx)
    gpu = nvidia_gpu(url)
    product: dict | None = None
    if gpu:
        try:
            data = await rbase.get_json(NV_SEARCH.format(locale=locale, gpu=quote(gpu)), headers=_NV_HEADERS)
        except FetchError:
            data = None
        product = _nv_pick(data, gpu, sku)
        if product and not sku:
            sku = str(product.get("productSKU") or "").strip().upper() or None
    if not sku:
        return None

    title = clean_text(product.get("productTitle")) if product else None
    image = product.get("imageURL") if product else None
    price: Any = product.get("productPrice") if product else None
    detail: dict[str, Any] = {"sku": sku, "gpu": gpu}
    try:
        inv = await rbase.get_json(NV_INVENTORY.format(sku=quote(sku), locale=locale), headers=_NV_HEADERS)
    except FetchError:
        inv = None
    rows = [r for r in (dig(inv, "listMap") or []) if isinstance(r, dict)]
    if rows:
        active = [r for r in rows if str(r.get("is_active")).strip().lower() == "true"]
        row = (active or rows)[0]
        price = row.get("price") or price
        link = row.get("product_url") or None
        seller = "Best Buy" if link and "bestbuy.com" in link else "NVIDIA"
        detail.update(fe_sku=row.get("fe_sku"), matched=f"feinventory is_active={row.get('is_active')}",
                      seller=seller, third_party=False, source="feinventory")
        if active:
            detail["cart_url"] = link
            res = result("in", "In stock", price=price, title=title, image_url=image, detail=detail)
        else:
            res = result("out", "Out of stock", price=price, title=title, image_url=image, detail=detail)
        return finish(res, ctx, product_url=link)
    if product:
        verdict = _nv_status(product.get("prdStatus"))
        retailers = [r for r in product.get("retailers") or [] if isinstance(r, dict)]
        if verdict != "in" and any(r.get("isAvailable") is True for r in retailers):
            # FE sold through a partner retailer (e.g. Best Buy) — only counts when it's available
            verdict = "in"
            link = next((r.get("purchaseLink") for r in retailers if r.get("isAvailable") is True), None)
            detail["cart_url"] = link
        detail.update(matched=f"product search prdStatus={product.get('prdStatus')}", seller="NVIDIA",
                      third_party=False, source="search")
        if verdict is None:
            return None
        text = "In stock" if verdict == "in" else (
            "Retailers only (check availability)" if str(product.get("prdStatus")).lower() == "check_availability"
            else "Out of stock")
        return finish(result(verdict, text, price=price, title=title, image_url=image, detail=detail), ctx)
    return None


def _nv_pick(data: Any, gpu: str, sku: str | None) -> dict | None:
    sp = dig(data, "searchedProducts") or {}
    cands = [p for p in [sp.get("featuredProduct")] + list(sp.get("productDetails") or []) if isinstance(p, dict)]
    if sku:
        for p in cands:
            if str(p.get("productSKU") or "").upper() == sku:
                return p
    want = re.sub(r"\s+", "", gpu).lower()
    for p in cands:
        fe = p.get("isFounderEdition") is True or str(p.get("manufacturer") or "").upper() == "NVIDIA"
        if fe and re.sub(r"\s+", "", str(p.get("gpu") or "")).lower() == want:
            return p
    for p in cands:
        if p.get("isFounderEdition") is True:
            return p
    return None


# =========================================================================== Micro Center

_MC_PRODUCT_RE = re.compile(r"/product/(\d+)", re.I)
_MC_COUNT_RE = re.compile(r"(\d+\+?)\s*(?:new\s+)?in\s+stock", re.I)
_MC_JS_INSTOCK_RE = re.compile(r"""['"]inStock['"]\s*:\s*['"]?(True|False)""", re.I)
_MC_JS_PRICE_RE = re.compile(r"""['"]productPrice['"]\s*:\s*['"]?([\d.,]+)""", re.I)
_MC_JS_NAME_RE = re.compile(r"""['"]productName['"]\s*:\s*['"]([^'"]+)""", re.I)
_MC_STORE_NAME_RES = (
    re.compile(r"""['"]storeName['"]\s*:\s*['"]([^'"]+)""", re.I),
)
_MC_PAGE_STORE_RES = (
    re.compile(r"storeSelected=(\d{2,4})", re.I),
    re.compile(r"""['"]store(?:Num|Number|Id|ID)['"]\s*:\s*['"]?(\d{2,4})""", re.I),
    re.compile(r"[?&]storeid=(\d{2,4})", re.I),
)


def _with_query(url: str, **params: str) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in {p.lower() for p in params}]
    q.extend(params.items())
    return urlunsplit(parts._replace(query=urlencode(q)))


async def _mc_fetch(url: str, ctx: AdapterContext, store: str | None) -> Any:
    if store:
        page_url = _with_query(url, storeid=store)
        try:
            resp = await fetcher.http_get(page_url, headers={"Cookie": f"storeSelected={store}"})
            text = resp.text if resp.content else ""
            if resp.status_code < 400 and not fetcher.looks_like_challenge(text) and fetcher.has_product_signals(text):
                return fetcher.FetchResult(url=str(resp.url), status=resp.status_code, text=text,
                                           headers=dict(resp.headers))
        except FetchError:
            pass
        return await fetch_page(page_url, ctx)  # browser fallback (store via ?storeid=)
    return await fetch_page(url, ctx)


def _mc_inventory(view: PageView, html: str) -> tuple[str | None, str | None, str]:
    """-> (verdict, count, raw text)."""
    els = view.soup.select("span.inventoryCnt, .inventoryCnt, [class*=inventory-count]")
    for el in els:
        t = soup_text(el)
        if not t:
            continue
        low = t.lower()
        m = _MC_COUNT_RE.search(t)
        if m:
            cnt = m.group(1)
            return ("out" if cnt.rstrip("+") == "0" else "in"), cnt, t
        if "sold out" in low or "out of stock" in low:
            return "out", None, t
        if "limited availability" in low or "in stock" in low:
            return "in", None, t
    m = _MC_JS_INSTOCK_RE.search(html or "")
    if m:
        return ("in" if m.group(1).lower() == "true" else "out"), None, f"inStock: {m.group(1)}"
    return None, None, ""


def _mc_store_name(view: PageView, html: str) -> str | None:
    for el in view.soup.select("[class*=storeName], [id*=storeName], [class*=store-name]"):
        t = soup_text(el)
        if t and len(t) <= 40:
            return re.sub(r"(?i)^micro\s*center\s*[-:]?\s*|\s+store$", "", t).strip() or None
    for rx in _MC_STORE_NAME_RES:
        m = rx.search(html or "")
        if m:
            return clean_text(m.group(1))
    return None


async def microcenter(url: str, ctx: AdapterContext) -> CheckResult | None:
    rcfg = ctx.retailer_config
    store = (rcfg.store_id or "").strip() or None
    if store and not re.fullmatch(r"\d{1,4}", store):
        store = None
    fetched = await _mc_fetch(url, ctx, store)
    if is_queued(fetched):
        return queue_result(ctx)
    html = fetched.text or ""
    page_store = store
    if not page_store:
        for rx in _MC_PAGE_STORE_RES:
            m = rx.search(html)
            if m:
                page_store = m.group(1)
                break

    def dom(view: PageView) -> Hit | None:
        verdict, count, raw = _mc_inventory(view, html)
        if verdict is None:
            return None
        name = _mc_store_name(view, html)
        where = f"Micro Center {name or page_store or ''}".strip()
        suffix = "" if store else " · default store (set a store ID to pick yours)"
        pm = _MC_JS_PRICE_RE.search(html)
        nm = _MC_JS_NAME_RE.search(html)
        detail = {"store_id": page_store, "store_name": name, "stock_qty": count, "seller": "Micro Center",
                  "third_party": False, "store_configured": bool(store)}
        if verdict == "in":
            label = f"In stock at {where}" + (f" ({count})" if count else "")
            key = f"pickup:{page_store or 'default'}"
            return Hit("in", label + suffix, f"inventory: '{raw}'", detail=detail,
                       available=[Availability(key=key, label=label)],
                       price=pm.group(1) if pm else None, title=clean_text(nm.group(1)) if nm else None)
        return Hit("out", f"Sold out at {where}" + suffix, f"inventory: '{raw}'", detail=detail,
                   price=pm.group(1) if pm else None, title=clean_text(nm.group(1)) if nm else None)

    m = _MC_PRODUCT_RE.search(url)
    return analyze_page(html, url, ctx, fetched.url, dom=dom, extra={"product_id": m.group(1) if m else None})


# =========================================================================== B&H Photo

_BH_IN_RE = re.compile(r"^\s*in\s+stock", re.I)
_BH_ORDERABLE_RE = re.compile(r"back-?ordered|special\s+order|more\s+on\s+the\s+way|pre-?order|ships\s+in", re.I)
_BH_OUT_RE = re.compile(r"coming\s+soon|discontinued|no\s+longer\s+available|out\s+of\s+stock|not\s+available|sold\s+out",
                        re.I)


def _bh_dom(view: PageView) -> Hit | None:
    s = view.soup
    status_el = s.select_one("[data-selenium=stockStatus]")
    status = soup_text(status_el)
    atc = s.select_one("[data-selenium=addToCartButton]")
    notify = s.select_one("[data-selenium=notifyAvailabilityButton]")
    price_el = s.select_one("[data-selenium=pricingPrice]")
    price = soup_text(price_el) or None
    det = {"stock_status": status or None, "seller": "B&H Photo", "third_party": False}
    atc_ok = atc is not None and not atc.has_attr("disabled")
    if status:
        if _BH_IN_RE.search(status):
            return Hit("in", "In stock", f"stockStatus: '{status}'", det, price=price)
        if _BH_ORDERABLE_RE.search(status) and notify is None:
            label = "Backordered" if re.search(r"back-?order", status, re.I) else (
                "Pre-order" if re.search(r"pre-?order", status, re.I) else status[:60])
            return Hit("in", label, f"stockStatus: '{status}'", det, price=price)
        if _BH_OUT_RE.search(status) or notify is not None:
            text = "Coming soon" if re.search(r"coming\s+soon", status, re.I) else "Out of stock"
            return Hit("out", text, f"stockStatus: '{status}'", det, price=price)
    if atc_ok:
        return Hit("in", "In stock", "button: addToCartButton", det, price=price)
    if notify is not None:
        return Hit("out", "Out of stock", "button: notifyAvailabilityButton", det, price=price)
    return None


async def bhphoto(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx, needs=lambda h: "data-selenium" in h or fetcher.has_product_signals(h))
    _raise_if_px_blocked(fetched.text, "bhphotovideo.com")
    if is_queued(fetched):
        return queue_result(ctx)
    return analyze_page(fetched.text, url, ctx, fetched.url, dom=_bh_dom, rules=[
        rule(r"\bmore\s+on\s+the\s+way\b", "in", "More on the way (orderable)"),
        rule(r"\bnew\s+item\s*-?\s*coming\s+soon\b", "out", "Coming soon"),
    ])


# =========================================================================== phrase-rule sites

ADORAMA_RULES = [
    rule(r"\btemporarily\s+not\s+available\b", "out", "Temporarily not available", strong=True),
    rule(r"^\W*notify\s+me\s+when\s+available\W*$", "out", "Out of stock", strong=True, where="button"),
    rule(r"\bspecial\s+order\b", "in", "Special order"),
]


async def adorama(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx)
    _raise_if_px_blocked(fetched.text, "adorama.com")
    if is_queued(fetched):
        return queue_result(ctx)
    res = analyze_page(fetched.text, url, ctx, fetched.url, rules=ADORAMA_RULES)
    if res is not None and res.status != "unknown":
        res.detail.update(seller_detail(ctx))
    return res


EVGA_RULES = [
    rule(r"^\W*auto\s*notify\W*$", "out", "Out of stock (Auto Notify)", strong=True, where="button"),
    rule(r"\bauto\s*notify\b", "out", "Out of stock (Auto Notify)", where="text"),
]


async def evga(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _store_page(url, ctx, rules=EVGA_RULES)


def _antonline_dom(view: PageView) -> Hit | None:
    for el in view.soup.select(".add_to_cart_button, .add-to-cart-button, [class*=add_to_cart]"):
        if el.name in ("button", "a", "input") and not el.has_attr("disabled") and "disabled" not in " ".join(el.get("class") or []):
            txt = soup_text(el) or str(el.get("value") or "")
            if re.search(r"sold\s*out|unavailable|notify", txt, re.I):
                return Hit("out", "Sold out", f"button: '{txt}'")
            return Hit("in", "In stock", f"button: '{txt or 'add to cart'}' (enabled)")
    return None


async def antonline(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _store_page(url, ctx, dom=_antonline_dom)


def _amd_dom(view: PageView) -> Hit | None:
    s = view.soup
    oos = s.select_one(".product-out-of-stock, .btn-out-of-stock")
    cart = s.select_one(".btn-shopping-cart")
    if cart is not None and not cart.has_attr("disabled") and oos is None:
        return Hit("in", "In stock", "AMD: .btn-shopping-cart")
    if oos is not None:
        return Hit("out", "Out of stock", f"AMD: .product-out-of-stock '{soup_text(oos)[:40]}'")
    return None


async def amd(url: str, ctx: AdapterContext) -> CheckResult | None:
    if not re.search(r"/direct-buy/", url, re.I):
        return None  # amd.com product-info pages: let the generic checker look
    return await _store_page(url, ctx, dom=_amd_dom)


DELL_RULES = [
    rule(r"\btemporarily\s+out\s+of\s+stock\b", "out", "Temporarily out of stock", strong=True, where="text"),
    rule(r"\bcurrently\s+unavailable\b", "out", "Currently unavailable", strong=True, where="text"),
]


async def dell(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _store_page(url, ctx, rules=DELL_RULES)


LG_RULES = [
    rule(r"\btemporarily\s+(?:out\s+of\s+stock|unavailable)\b", "out", "Temporarily unavailable", where="text"),
    rule(r"^\W*notify\s+me\W*$", "out", "Out of stock", where="button"),
]


async def lg(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _store_page(url, ctx, rules=LG_RULES)


LEICA_RULES = [
    rule(r"\bcurrently\s+not\s+available\b|\bnot\s+available\s+online\b", "out", "Not available online", where="text"),
    rule(r"^\W*notify\s+me\W*$", "out", "Out of stock", where="button"),
]


async def leica(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _store_page(url, ctx, rules=LEICA_RULES)


LENOVO_RULES = [
    rule(r"\btemporarily\s+unavailable\b", "out", "Temporarily unavailable", strong=True, where="text"),
    rule(r"^\W*coming\s+soon\W*$", "out", "Coming soon", where="button"),
]


async def lenovo(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _store_page(url, ctx, rules=LENOVO_RULES)


META_RULES = [
    rule(r"^\W*notify\s+me\b.*$", "out", "Out of stock", where="button"),
]


async def meta_quest(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _store_page(url, ctx, rules=META_RULES)


async def _store_page(url: str, ctx: AdapterContext, **kw: Any) -> CheckResult | None:
    res = await check_page(url, ctx, **kw)
    if res is not None and res.status != "unknown" and not res.detail.get("queue"):
        res.detail.setdefault("seller", ctx.retailer.name)
        res.detail.setdefault("third_party", False)
    return res


# =========================================================================== Gigabyte / Zotac (info sites)

NO_DIRECT_SALES = "No direct sales on this page"


def _sells_here(res: CheckResult) -> bool:
    m = str(res.detail.get("matched") or "")
    if m.startswith(("json-ld", "microdata", "rdfa", "meta", "itemprop")):
        return True
    if m.startswith("button:") and "(enabled)" in m:
        return True
    return res.detail.get("adapter") != "generic"  # a site rule decided


async def _info_or_shop(url: str, ctx: AdapterContext, **kw: Any) -> CheckResult | None:
    """Brand sites whose product pages are usually spec sheets ("Where to buy"): only a real
    offer (structured data or an enabled buy button) counts; otherwise unknown."""
    fetched = await fetch_page(url, ctx)
    if is_queued(fetched):
        return queue_result(ctx)
    res = analyze_page(fetched.text, url, ctx, fetched.url, **kw)
    if res is not None and (res.status == "unknown" or not _sells_here(res)):
        info = result(None, NO_DIRECT_SALES, title=res.title, image_url=res.image_url,
                      detail={"signals": res.detail.get("signals", []), "matched": None, "info_only": True})
        return finish(info, ctx)
    return res


async def gigabyte(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await _info_or_shop(url, ctx)


def _magento_dom(view: PageView) -> Hit | None:
    s = view.soup
    price_el = s.select_one("[data-price-type=finalPrice][data-price-amount], [data-price-amount]")
    price = price_el.get("data-price-amount") if price_el is not None else None
    if s.select_one(".stock.unavailable, .product-info-stock-sku .unavailable"):
        return Hit("out", "Out of stock", "magento: .stock.unavailable", {"seller": "ZOTAC Store", "third_party": False},
                   price=price)
    if s.select_one(".stock.available"):
        btn = s.select_one("#product-addtocart-button")
        if btn is None or not btn.has_attr("disabled"):
            return Hit("in", "In stock", "magento: .stock.available", {"seller": "ZOTAC Store", "third_party": False},
                       price=price)
    return None


async def zotac(url: str, ctx: AdapterContext) -> CheckResult | None:
    host = fetcher.host_of(url)
    if host == "zotacstore.com" or host.endswith(".zotacstore.com"):
        return await check_page(url, ctx, dom=_magento_dom)
    return await _info_or_shop(url, ctx)


__all__ = ["amd", "adorama", "antonline", "bhphoto", "dell", "evga", "gigabyte", "lg", "leica", "lenovo",
           "microcenter", "newegg", "nvidia", "meta_quest", "zotac"]
