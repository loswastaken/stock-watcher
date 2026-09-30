"""Games / toys: LEGO, Nintendo, POP MART and Pokemon Center (embedded Next.js / preloaded
state), PlayStation Direct (OCC product API), GameStop (data-gtmdata; SFCC recipe runs after us
when inconclusive), Bandai Namco and Play-Asia (site vocabulary)."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .. import fetcher
from ..base import CheckResult
from ..fetcher import FetchError
from ..util import clean_text, loads_lenient
from .base import AdapterContext, dig, next_data, result
from .pagekit import (
    Hit,
    PageView,
    analyze_page,
    check_page,
    fetch_page,
    finish,
    is_queue_url,
    is_queued,
    iter_dicts,
    js_assignment,
    queue_result,
    rule,
    seller_detail,
)


# --------------------------------------------------------------------------- Apollo helpers


def _apollo_refs(state: Any) -> dict[str, dict]:
    refs: dict[str, dict] = {}
    for d in iter_dicts(state):
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(k, str) and (":" in k or k == "ROOT_QUERY") and (
                    "__typename" in v or k == "ROOT_QUERY"):
                refs.setdefault(k, v)
    return refs


def _deref(v: Any, refs: dict[str, dict]) -> Any:
    if isinstance(v, dict):
        if isinstance(v.get("__ref"), str):
            return refs.get(v["__ref"], v)
        if v.get("type") == "id" and isinstance(v.get("id"), str) and len(v) <= 4:
            return refs.get(v["id"], v)
    return v


def _collect(node: Any, refs: dict[str, dict], keys: set[str], depth: int = 0, seen: set | None = None,
             out: dict | None = None) -> dict:
    """First occurrence of each key in ``keys`` under ``node`` (following Apollo refs)."""
    out = {} if out is None else out
    seen = set() if seen is None else seen
    node = _deref(node, refs)
    if depth > 8 or id(node) in seen or len(out) == len(keys):
        return out
    seen.add(id(node))
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and k not in out and v is not None:
                dv = _deref(v, refs)
                if k in ("price", "prices") and isinstance(dv, dict) or not isinstance(dv, (dict, list)):
                    out[k] = dv
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                _collect(v, refs, keys, depth + 1, seen, out)
    elif isinstance(node, list):
        for v in node:
            _collect(v, refs, keys, depth + 1, seen, out)
    return out


# =========================================================================== LEGO

_LEGO_CODE_RE = re.compile(r"/product/[^/?#]*?-?(\d{4,8})(?:[/?#]|$)", re.I)
_LEGO_KEYS = {"availabilityStatus", "canAddToBag", "availabilityText", "price", "name", "primaryImage", "baseImgUrl"}


def lego_code(url: str) -> str | None:
    m = _LEGO_CODE_RE.search(url)
    return m.group(1) if m else None


def lego_status(status: str | None) -> tuple[str | None, str | None]:
    s = str(status or "").upper()
    if not s:
        return None, None
    if s.startswith("E_") or s in ("AVAILABLE", "IN_STOCK"):
        return "in", "In stock"
    if s.startswith(("A_", "P_")) or "PRE_ORDER" in s:
        return "in", "Pre-order"
    if s.startswith(("F_", "G_")) or "BACKORDER" in s:
        return "in", "Backorder"
    if s.startswith(("B_", "C_")) or "COMING_SOON" in s:
        return "out", "Coming soon"
    if s.startswith(("H_", "K_")) or "OUT_OF_STOCK" in s:
        return "out", "Out of stock"
    if s.startswith(("L_", "R_")) or "RETIRED" in s or "READ_ONLY" in s:
        return "out", "Retired"
    return None, None


def _lego_state(html: str, code: str | None) -> dict | None:
    nd = next_data(html)
    if nd is None:
        return None
    refs = _apollo_refs(nd)
    nodes = [d for d in iter_dicts(nd) if code and str(d.get("productCode") or "") == code]
    for n in nodes:
        got = _collect(n, refs, _LEGO_KEYS)
        if "availabilityStatus" in got or "canAddToBag" in got:
            return got
    statuses = {d.get("availabilityStatus") for d in iter_dicts(nd) if isinstance(d.get("availabilityStatus"), str)}
    if len(statuses) == 1:  # single-product page without a matching productCode
        node = next(d for d in iter_dicts(nd) if isinstance(d.get("availabilityStatus"), str))
        return _collect(node, refs, _LEGO_KEYS)
    return None


def _lego_price(p: Any) -> tuple[Any, str | None]:
    if not isinstance(p, dict):
        return None, None
    cur = p.get("currencyCode") or p.get("currency")
    if isinstance(p.get("centAmount"), (int, float)):
        return p["centAmount"] / 100, cur
    return p.get("formattedAmount") or p.get("amount"), cur


async def lego(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx, needs=lambda h: "__NEXT_DATA__" in h or fetcher.has_product_signals(h))
    if is_queued(fetched):
        return queue_result(ctx)
    code = lego_code(url)
    st = _lego_state(fetched.text, code)

    def dom(view: PageView) -> Hit | None:
        if not st:
            return None
        verdict, text = lego_status(st.get("availabilityStatus"))
        can_add = st.get("canAddToBag")
        if verdict == "in" and can_add is False:
            verdict, text = "out", "Temporarily unavailable"
        if verdict is None and isinstance(can_add, bool):
            verdict, text = ("in", "In stock") if can_add else ("out", "Out of stock")
        if verdict is None:
            return None
        price, cur = _lego_price(st.get("price"))
        det = {"product_code": code, "availability_status": st.get("availabilityStatus"),
               "availability_text": st.get("availabilityText"), "can_add_to_bag": can_add,
               **seller_detail(ctx, "LEGO")}
        if cur and cur != "USD" and price is not None:
            det["currency"] = cur
        return Hit(verdict, text, f"__NEXT_DATA__ availabilityStatus={st.get('availabilityStatus')}", det, price=price,
                   title=clean_text(st.get("name")) or None)

    res = analyze_page(fetched.text, url, ctx, fetched.url, dom=dom, extra={"product_code": code})
    return res


# =========================================================================== Nintendo

NINTENDO_RULES = [
    rule(r"\brequest\s+an\s+invitation\b", "out", "Invite only", strong=True),
]
_NIN_STRING_FIELDS = ("salesStatus", "stockStatus", "inventoryStatus", "availability", "purchaseStatus")
_NIN_BOOL_FIELDS = ("isSalableQty", "isSalable", "inStock", "purchasable")


def _nin_enum(v: str) -> tuple[str | None, str | None]:
    s = re.sub(r"[^A-Z]", "_", v.upper())
    if any(t in s for t in ("OUT_OF_STOCK", "SOLD_OUT", "UNAVAILABLE", "NOT_AVAILABLE", "NOT_SALABLE")):
        return "out", "Out of stock"
    if "COMING_SOON" in s or "UNRELEASED" in s:
        return "out", "Coming soon"
    if "PRE_ORDER" in s or "PREORDER" in s:
        return "in", "Pre-order"
    if "IN_STOCK" in s or s.strip("_") in ("AVAILABLE", "SALABLE", "ON_SALE") or s.endswith("_AVAILABLE"):
        return "in", "In stock"
    return None, None


def _nin_product(html: str, url: str) -> dict | None:
    nd = next_data(html)
    if nd is None:
        return None
    refs = _apollo_refs(nd)
    prods = [d for d in iter_dicts(nd) if d.get("__typename") == "StoreProduct"]
    if not prods:
        return None
    segs = [s.lower() for s in urlsplit(url).path.split("/") if s]
    slug = segs[-1] if segs else ""
    # only the StoreProduct this URL names (urlKey = last path segment, or its SKU in the path);
    # bundles / "related" products on the page never stand in for it
    p = next((d for d in prods if slug and str(d.get("urlKey") or "").strip("/").lower() == slug), None)
    if p is None:
        p = next((d for d in prods if d.get("sku") and str(d["sku"]).lower() in segs), None)
    if p is None:
        return None
    return {k: _deref(v, refs) for k, v in p.items()}


async def nintendo(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx, needs=lambda h: "__NEXT_DATA__" in h or fetcher.has_product_signals(h))
    if is_queued(fetched):
        return queue_result(ctx)
    prod = _nin_product(fetched.text, url)

    def dom(view: PageView) -> Hit | None:
        from .pagekit import match_rules

        inv = match_rules(view, NINTENDO_RULES, strong=True)
        if inv:
            return Hit("out", "Invite only", inv[1], seller_detail(ctx, "Nintendo"))
        if not prod:
            return None
        price = dig(prod, "prices", "minimum", "finalPrice") or dig(prod, "prices", "minimum", "regularPrice")
        det = {"sku": prod.get("sku"), **seller_detail(ctx, "Nintendo")}
        for f in _NIN_STRING_FIELDS:
            v = prod.get(f)
            if isinstance(v, str) and v:
                verdict, text = _nin_enum(v)
                if verdict:
                    return Hit(verdict, text, f"__NEXT_DATA__ StoreProduct.{f}={v}", det, price=price,
                               title=clean_text(prod.get("name")) or None)
        for f in _NIN_BOOL_FIELDS:
            v = prod.get(f)
            if isinstance(v, bool):
                return Hit("in" if v else "out", "In stock" if v else "Out of stock",
                           f"__NEXT_DATA__ StoreProduct.{f}={v}", det, price=price,
                           title=clean_text(prod.get("name")) or None)
        return None

    return analyze_page(fetched.text, url, ctx, fetched.url, dom=dom, rules=NINTENDO_RULES)


# =========================================================================== POP MART


def _popmart_skus(html: str) -> list[dict] | None:
    nd = next_data(html)
    if nd is None:
        return None
    for d in iter_dicts(nd):
        skus = d.get("skus")
        if isinstance(skus, list) and skus and all(isinstance(s, dict) for s in skus) and any(
                isinstance(s.get("stock"), dict) for s in skus):
            return skus
    return None


def _popmart_price(sku: dict) -> float | None:
    """POP MART sku prices look like integer cents (2799 -> $27.99); decimals are dollars."""
    for k in ("discountPrice", "price"):
        v = sku.get(k)
        if isinstance(v, bool) or v in (None, "", 0):
            continue
        if isinstance(v, int) and v >= 100:
            return v / 100
        if isinstance(v, float) or (isinstance(v, str) and "." in v):
            try:
                return float(v)
            except ValueError:
                continue
    return None


# A spuId that is no longer sold renders the product's SEO title over this notice + "BACK TO HOMEPAGE"
# (the same sentence also sits in the i18n bundle of every page, so only a rendered element counts).
_PM_EMPTY_RE = re.compile(r"\bthe\s+product\s+you\s+are\s+looking\s+for\s+is\s+not\s+available\b", re.I)


def _popmart_unavailable(view: PageView) -> bool:
    for node in view.soup.find_all(string=_PM_EMPTY_RE):
        if node.parent is not None and node.parent.name not in ("script", "style", "template", "noscript"):
            return True
    return False


async def popmart(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx)
    if is_queued(fetched):
        return queue_result(ctx)
    skus = _popmart_skus(fetched.text)
    pin = (parse_qs(urlsplit(url).query).get("skuId") or [None])[0]

    def dom(view: PageView) -> Hit | None:
        if not skus:
            if _popmart_unavailable(view):
                return Hit("out", "Not available (removed from sale)",
                           "text: 'The product you are looking for is not available'", seller_detail(ctx, "POP MART"))
            return None
        considered = [s for s in skus if pin and str(s.get("id")) == pin] or skus
        counts = []
        for s in considered:
            n = dig(s, "stock", "onlineStock")
            try:
                counts.append(int(n))
            except (TypeError, ValueError):
                continue
        if not counts:
            return None
        total = sum(c for c in counts if c > 0)
        det = {"sku_count": len(considered), "stock_qty": total, **seller_detail(ctx, "POP MART")}
        pick = next((s for s in considered if (dig(s, "stock", "onlineStock") or 0) > 0), considered[0])
        price = _popmart_price(pick)
        sig = f"__NEXT_DATA__ skus[].stock.onlineStock={counts}"
        if total > 0:
            return Hit("in", "In stock", sig, det, price=price)
        return Hit("out", "Sold out", sig, det, price=price)

    return analyze_page(fetched.text, url, ctx, fetched.url, dom=dom)


# =========================================================================== Pokemon Center

_PC_CODE_RE = re.compile(r"/product/([0-9A-Za-z-]+)", re.I)
_PC_STATES = {
    "AVAILABLE": ("in", "In stock"),
    "AVAILABLE_FOR_PRE_ORDER": ("in", "Pre-order"),
    "AVAILABLE_FOR_BACK_ORDER": ("in", "Backorder"),
    "NOT_AVAILABLE": ("out", "Out of stock"),
    "OUT_OF_STOCK": ("out", "Out of stock"),
}
_PC_ID_KEYS = ("code", "sku", "id", "productId", "displayId", "productCode")


def _pc_state(html: str, code: str | None) -> tuple[str | None, dict | None]:
    roots = [r for r in (next_data(html), js_assignment(html, "__PRELOADED_STATE__")) if r is not None]
    if not roots:
        return None, None

    def own_state(d: dict) -> str | None:
        a = d.get("availability")
        if isinstance(a, dict):
            a = a.get("state")
        return a.upper() if isinstance(a, str) and a.upper() in _PC_STATES else None

    def avail_in(node: dict) -> str | None:
        """The node's own availability, or one in a direct child object (not in lists, which hold
        related products / bundles)."""
        s = own_state(node)
        if s:
            return s
        for v in node.values():
            if isinstance(v, dict) and not any(k in v for k in _PC_ID_KEYS):
                s = own_state(v)
                if s:
                    return s
        return None

    want = (code or "").lower()
    if not want:
        return None, None
    for root in roots:
        for d in iter_dicts(root):
            node = None
            if any(str(d.get(k) or "").lower() == want for k in _PC_ID_KEYS):
                node = d
            else:
                node = next((v for k, v in d.items() if isinstance(v, dict) and str(k).lower() == want), None)
            if node is not None:
                s = avail_in(node)
                if s:
                    return s, node
    # no availability tied to this product code: never borrow another product's state
    return None, None


def _pc_price(node: dict | None) -> Any:
    if not node:
        return None
    for d in iter_dicts(node):
        for k in ("purchasePrice", "listPrice", "price"):
            v = d.get(k)
            if isinstance(v, dict):
                amt = v.get("amount") if v.get("amount") is not None else v.get("display")
                if amt is not None:
                    return amt
            elif isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("amount") is not None:
                return v[0]["amount"]
    return None


async def pokemoncenter(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx)
    if is_queued(fetched):
        return queue_result(ctx)
    m = _PC_CODE_RE.search(urlsplit(url).path)
    code = m.group(1) if m else None
    state, node = _pc_state(fetched.text, code)

    def dom(view: PageView) -> Hit | None:
        if not state:
            return None
        verdict, text = _PC_STATES[state]
        return Hit(verdict, text, f"embedded state availability={state}",
                   {"product_code": code, **seller_detail(ctx, "Pokémon Center")}, price=_pc_price(node))

    return analyze_page(fetched.text, url, ctx, fetched.url, dom=dom, extra={"product_code": code})


# =========================================================================== PlayStation Direct

PSD_API = ("https://api.direct.playstation.com/commercewebservices/ps-direct-{region}/users/anonymous/products/"
           "productList?fields=BASIC&productCodes={code}")
_PSD_CODE_RE = re.compile(r"[./](\d{7,})(?:[/?#]|$)")
_PSD_IN = {"instock": "In stock", "lowstock": "Low stock"}
_PSD_OUT = {"outofstock": "Out of stock"}


def psdirect_code(url: str) -> str | None:
    m = _PSD_CODE_RE.search(urlsplit(url).path)
    return m.group(1) if m else None


def _psd_region(url: str) -> str:
    m = re.search(r"/[a-z]{2}-([a-z]{2})(?:/|$)", urlsplit(url).path, re.I)
    return m.group(1).lower() if m else "us"


_PSD_PAGE_CODE_RE = re.compile(r"""data-product-code=["'](\d{7,})["']""")


def psdirect_page_code(html: str) -> str | None:
    """The product code a code-less page (/en-us/buy-consoles/playstation5-pro-console-2-tb, the links PS
    Direct's own menus carried on 2026-09-29) renders on its product components (``data-product-code``, which
    the page's script sends to productList). Only when the page names exactly one product."""
    codes = set(_PSD_PAGE_CODE_RE.findall(html or ""))
    return codes.pop() if len(codes) == 1 else None


async def _psd_api(url: str, code: str, ctx: AdapterContext) -> CheckResult | None:
    api = PSD_API.format(region=_psd_region(url), code=code)
    headers = {"Accept": "application/json, text/plain, */*", "Origin": "https://direct.playstation.com",
               "Referer": "https://direct.playstation.com/", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
               "Sec-Fetch-Site": "same-site"}
    data = None
    try:
        resp = await fetcher.http_get(api, headers=headers)
        if is_queue_url(str(resp.url)) or any(is_queue_url(str(h.headers.get("location") or ""))
                                              for h in resp.history):
            return queue_result(ctx, product_code=code)
        if resp.status_code < 400:
            data = json.loads(resp.text)
    except (FetchError, ValueError):
        data = None
    prod = dig(data, "products", 0)
    return _psd_from_api(prod, code, ctx) if isinstance(prod, dict) else None


async def psdirect(url: str, ctx: AdapterContext) -> CheckResult | None:
    code = psdirect_code(url)
    if code:
        res = await _psd_api(url, code, ctx)
        if res is not None:
            return res
    fetched = await fetch_page(url, ctx)
    if is_queued(fetched):
        return queue_result(ctx, product_code=code)
    if not code and getattr(fetched, "status", 200) < 400:
        page_code = psdirect_page_code(fetched.text)
        if page_code:
            res = await _psd_api(url, page_code, ctx)
            if res is not None:
                res.detail["code_from"] = "page"
                return res
            code = page_code
    return analyze_page(fetched.text, url, ctx, fetched.url, extra={"product_code": code, "source": "html"})


def _psd_from_api(prod: dict, code: str, ctx: AdapterContext) -> CheckResult | None:
    raw = str(dig(prod, "stock", "stockLevelStatus") or "")
    key = raw.replace("_", "").lower()
    price = dig(prod, "price", "value")
    if price is None:
        price = dig(prod, "price", "formattedValue")
    title = clean_text(prod.get("name")) or None
    img = next((i.get("url") for i in prod.get("images") or [] if isinstance(i, dict) and i.get("url")), None)
    detail = {"product_code": code, "stock_level": raw or None, "matched": f"productList stockLevelStatus={raw}",
              "source": "api", **seller_detail(ctx, "PlayStation Direct")}
    if key in _PSD_IN:
        return finish(result("in", _PSD_IN[key], price=price, title=title, image_url=img, detail=detail), ctx)
    if key in _PSD_OUT:
        return finish(result("out", _PSD_OUT[key], price=price, title=title, image_url=img, detail=detail), ctx)
    return None


# =========================================================================== GameStop

_GS_IN = {"available": "In stock", "in stock": "In stock", "instock": "In stock", "preorder": "Pre-order",
          "pre-order": "Pre-order", "pre order": "Pre-order", "backorder": "Backorder"}
_GS_OUT = {"not available": "Out of stock", "notavailable": "Out of stock", "unavailable": "Out of stock",
           "out of stock": "Out of stock", "outofstock": "Out of stock", "sold out": "Sold out"}


_GS_PID_RE = re.compile(r"/(\d{6,8})\.html", re.I)


def _gs_infos(view: PageView, pid: str | None = None) -> list[dict]:
    """productInfo blobs for this product: those in the main product area (carousels / recommended
    tiles stripped) whose productID, when present, is the URL's pid."""
    scope = view.scope if view.scope is not None else view.soup
    out = []
    for el in scope.select("[data-gtmdata]"):
        data = loads_lenient(str(el.get("data-gtmdata") or ""))
        info = data.get("productInfo") if isinstance(data, dict) else None
        if not (isinstance(info, dict) and info.get("availability")):
            continue
        # the URL's number is the SKU (".../microsoft-xbox-one-battery-pack/100820.html": sku 100820,
        # productID 10121310), older pages used the productID
        ids = {str(info.get(k)) for k in ("sku", "productID", "productId", "pid", "masterID") if info.get(k)}
        if pid and ids and pid not in ids:
            continue
        out.append(info)
    return out


async def gamestop(url: str, ctx: AdapterContext) -> CheckResult | None:
    want = (parse_qs(urlsplit(url).query).get("condition") or [None])[0]
    want = (want or ("New" if ctx.retailer_config.condition == "new" else "")).lower()
    pm = _GS_PID_RE.search(urlsplit(url).path)
    pid = pm.group(1) if pm else None

    def dom(view: PageView) -> Hit | None:
        found = _gs_infos(view, pid)
        infos = found
        if want:  # a different condition (Pre-Owned, Digital) never stands in for the one watched
            infos = [i for i in found if str(i.get("condition") or "").lower() in (want, "")]
            if found and not infos:
                # only other conditions are sold here (2026-09-29: an Xbox One battery pack sold Pre-Owned
                # only — the page's JSON-LD says InStock for that used offer, which the generic checker took)
                other = sorted({str(i.get("condition")) for i in found if i.get("condition")})
                i0 = found[0]
                det = {"sku": i0.get("sku") or i0.get("productID"), "condition": ", ".join(other) or None,
                       **seller_detail(ctx, "GameStop")}
                return Hit("out", f"No {want.title()} copies — only {' / '.join(other)}", "data-gtmdata condition",
                           det, title=clean_text(i0.get("name")) or None)
        if not infos:
            return None
        info = next((i for i in infos if str(i.get("condition") or "").lower() == want), infos[0])
        a = str(info.get("availability") or "").strip().lower()
        det = {"sku": info.get("sku") or info.get("productID"), "condition": info.get("condition"),
               **seller_detail(ctx, "GameStop")}
        sig = f"data-gtmdata availability='{info.get('availability')}'"
        name = clean_text(info.get("name")) or None
        if a in _GS_IN:
            return Hit("in", _GS_IN[a], sig, det, price=info.get("price"), title=name)
        if a in _GS_OUT:
            return Hit("out", _GS_OUT[a], sig, det, price=info.get("price"), title=name)
        return None

    res = await check_page(url, ctx, dom=dom, fall_through=True)
    if res is not None and res.detail.get("matched") == "data-gtmdata condition" and res.price:
        # the page's price is the other condition's (Pre-Owned $5.99), not the watched one's
        res.detail["other_condition_price"] = res.price
        res.price = None
        res.detail["price_value"] = None
    return res


# =========================================================================== Bandai / Play-Asia

BANDAI_RULES = [
    rule(r"\border\s+period\s+has\s+ended\b", "out", "Order period has ended", strong=True),
    rule(r"\border\s+period\s+has\s+not\s+(?:yet\s+)?(?:started|begun)\b", "out", "Coming soon", strong=True),
    rule(r"^\W*coming\s+soon\W*$", "out", "Coming soon", where="button"),
]


async def bandai(url: str, ctx: AdapterContext) -> CheckResult | None:
    host = fetcher.host_of(url)
    fall = not (host == "p-bandai.com" or host.endswith(".p-bandai.com"))
    res = await check_page(url, ctx, rules=BANDAI_RULES, fall_through=fall)
    if res is not None and res.status != "unknown" and not res.detail.get("queue") and not fall:
        res.detail.setdefault("seller", "Premium Bandai")
        res.detail.setdefault("third_party", False)
    return res


# Only the stock-status element counts: "(Discontinued)" also labels *other* editions in the notes,
# and "In stock, usually ships" must not beat a generic out-of-stock verdict.
PLAYASIA_RULES = [
    rule(r"\bout\s+of\s+stock\b|\bsold\s*out\b", "out", "Out of stock", where="status"),
    rule(r"\bin\s+stock,?\s+usually\s+ships\b", "in", "In stock", where="status"),
    rule(r"\bdiscontinued\b", "out", "Discontinued", where="status"),
]


async def playasia(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await check_page(url, ctx, rules=PLAYASIA_RULES)


__all__ = ["bandai", "gamestop", "lego", "nintendo", "popmart", "playasia", "psdirect", "pokemoncenter"]
