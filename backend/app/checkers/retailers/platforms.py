"""E-commerce platform recipes: stock checks that work for *any* store on a known platform.

``detect_and_check`` looks at an already-fetched product page, recognises the platform and
asks the platform's own product endpoint (or its well-known DOM) for availability:

- Shopify: ``/products/<handle>.js`` (variants, ``?variant=`` pins one; cart link
  ``/cart/<variant>:1``)
- Salesforce Commerce Cloud (Demandware): ``Product-Variation?pid=`` JSON
- BigCommerce: inline ``BCData`` product attributes
- WooCommerce: Store API ``/wp-json/wc/store/v1/products?slug=`` (DOM fallback)
- Magento 2 and OpenCart: product-page DOM

Recipes never raise for site problems: anything unexpected makes them return None and the
generic checker takes over.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from ..base import Availability, CheckResult
from ..fetcher import QUEUE_STATUS_TEXT, FetchError
from ..util import absolutize, clean_text, extract_balanced, loads_lenient, parse_amount, state_classes, walk
from .base import RetailerConfig, dig, get_json, next_data, result, soup_of

log = logging.getLogger("stockwatcher.checkers.platforms")

__all__ = ["detect_and_check", "detect_platform", "queue_result", "shopify_cart_url", "next_data_find",
           "iter_next_data", "check_shopify", "check_sfcc", "check_bigcommerce", "check_woocommerce",
           "check_magento", "check_opencart"]


# --------------------------------------------------------------------------- shared helpers


def queue_result(*, title: str | None = None, image_url: str | None = None, price: Any = None,
                 **detail: Any) -> CheckResult:
    """The result for a virtual waiting room (queue-it, Imperva, Shopify throttle, PS Direct):
    status unknown, ``detail["queue"] = True``."""
    d = {"queue": True, "signals": ["waiting room detected"]}
    d.update({k: v for k, v in detail.items() if v is not None})
    return result(None, QUEUE_STATUS_TEXT, title=title, image_url=image_url, price=price, detail=d)


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def _query(url: str) -> dict[str, list[str]]:
    try:
        return parse_qs(urlsplit(url).query)
    except ValueError:
        return {}


def _abs_img(v: Any, base: str) -> str | None:
    if isinstance(v, dict):
        v = v.get("src") or v.get("url")
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip()
    if v.startswith("//"):
        v = "https:" + v
    return absolutize(v, base)


_IN_WORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pre[\s-]?order", re.I), "Pre-order"),
    (re.compile(r"back[\s-]?order", re.I), "Backorder"),
    (re.compile(r"\bin\s+stock\b|\bavailable\b|\bonly\s+\d+\s+left\b|\blimited\s+(?:stock|quantit)|\bships\b", re.I),
     "In stock"),
]
_OUT_WORDS = re.compile(
    r"out\s+of\s+stock|sold\s*out|\bunavailable\b|not\s+available|no\s+longer\s+available|discontinued"
    r"|coming\s+soon|notify\s+me",
    re.I,
)


def classify_text(text: str | None) -> tuple[str | None, str | None]:
    """('in'|'out'|None, status text) for a free-form availability message."""
    t = clean_text(text)
    if not t:
        return None, None
    if _OUT_WORDS.search(t):
        return "out", ("Sold out" if re.search(r"sold\s*out", t, re.I)
                       else "Coming soon" if re.search(r"coming\s+soon", t, re.I) else "Out of stock")
    for rx, label in _IN_WORDS:
        if rx.search(t):
            return "in", label
    return None, None


# --------------------------------------------------------------------------- __NEXT_DATA__


def iter_next_data(html_or_data: Any) -> Iterator[Any]:
    """Every dict/list node of a page's ``__NEXT_DATA__`` (accepts HTML or parsed data)."""
    data = next_data(html_or_data) if isinstance(html_or_data, str) else html_or_data
    if data is None:
        return iter(())
    return walk(data)


def next_data_find(html_or_data: Any, pred: Callable[[dict], bool], limit: int = 50) -> list[dict]:
    """Dicts inside ``__NEXT_DATA__`` for which ``pred`` is true (e.g. Apollo cache entries
    with ``productCode == "75313"``). ``pred`` exceptions count as False."""
    out: list[dict] = []
    for node in iter_next_data(html_or_data):
        if not isinstance(node, dict):
            continue
        try:
            ok = pred(node)
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            out.append(node)
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------- detection


def _hdr(headers: dict | None, name: str) -> str:
    for k, v in (headers or {}).items():
        if str(k).lower() == name:
            return str(v)
    return ""


def detect_platform(html: str, headers: dict | None = None) -> str | None:
    """'shopify' | 'sfcc' | 'bigcommerce' | 'woocommerce' | 'magento' | 'opencart' | None."""
    h = html or ""
    if (_hdr(headers, "x-shopid") or "shopify" in _hdr(headers, "powered-by").lower()
            or _hdr(headers, "x-shopify-stage")
            or re.search(r"cdn\.shopify\.com|\bShopify\.shop\s*=|window\.Shopify\b|shopify-features", h)):
        return "shopify"
    if re.search(r"\bvar\s+BCData\s*=", h) or ("cdn11.bigcommerce.com" in h and "BCData" in h):
        return "bigcommerce"
    if re.search(r"/on/demandware\.store/|demandware\.static|\bdwvar_|/dw/image/v2/", h):
        return "sfcc"
    if re.search(r"wp-content/plugins/woocommerce|\bwoocommerce\b[^\"'>]{0,40}(?:page|js|no-js)|wc-block-|"
                 r"class=[\"'][^\"']*\bwoocommerce\b", h, re.I):
        return "woocommerce"
    if re.search(r"text/x-magento-init|\bMagento_[A-Z]\w+|data-mage-init|mage/cookies|/static/version\d+/frontend/", h):
        return "magento"
    if re.search(r"index\.php\?route=(?:product/product|checkout/cart)|catalog/view/theme/|id=[\"']button-cart[\"']", h):
        return "opencart"
    return None


async def detect_and_check(url: str, html: str, final_url: str, headers: dict | None,
                           rcfg: RetailerConfig | None = None) -> CheckResult | None:
    """Run the recipe for the page's platform. Returns a conclusive result (in/out of stock)
    with ``detail["adapter"]`` = platform name, or None (unknown platform / inconclusive)."""
    try:
        platform = detect_platform(html, headers)
        if platform is None:
            return None
        fn = _RECIPES[platform]
        res = await fn(url, html, final_url or url, headers or {}, rcfg)
    except FetchError as e:
        log.info("platform recipe failed for %s: %s", url, e)
        return None
    except Exception:  # noqa: BLE001 - recipes must never take a check down
        log.exception("platform recipe crashed for %s", url)
        return None
    if res is None or res.status not in ("in_stock", "out_of_stock"):
        return None
    res.detail.setdefault("adapter", platform)
    res.detail.setdefault("platform", platform)
    return res


# --------------------------------------------------------------------------- Shopify

_SHOPIFY_HANDLE_RE = re.compile(r"/products/([^/?#]+)")


def shopify_cart_url(host_or_url: str, variant_id: Any) -> str:
    host = urlsplit(host_or_url).netloc if "//" in host_or_url else host_or_url
    return f"https://{host}/cart/{variant_id}:1"


def _shopify_currency(html: str) -> str:
    for rx in (r"Shopify\.currency\s*=\s*\{[^}]*?\"active\"\s*:\s*\"([A-Z]{3})\"",
               r"property=[\"']og:price:currency[\"'][^>]*content=[\"']([A-Z]{3})",
               r"\"currency(?:Code)?\"\s*:\s*\"([A-Z]{3})\""):
        m = re.search(rx, html or "")
        if m:
            return m.group(1)
    return "USD"


def _cents(v: Any) -> Decimal | None:
    d = parse_amount(v)
    if d is None:
        return None
    if isinstance(v, str) and "." in v:  # "19.99" (products.json style) is already in units
        return d
    return d / 100


async def check_shopify(url: str, html: str, final_url: str, headers: dict, rcfg: RetailerConfig | None) -> CheckResult | None:
    m = _SHOPIFY_HANDLE_RE.search(urlsplit(final_url).path) or _SHOPIFY_HANDLE_RE.search(urlsplit(url).path)
    if not m:
        return None
    handle = m.group(1).removesuffix(".js").removesuffix(".json")
    origin = _origin(final_url)
    data = await get_json(f"{origin}/products/{quote(handle, safe='-_.~%')}.js", headers={"Referer": final_url})
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list):
        return None
    variants = [v for v in data["variants"] if isinstance(v, dict) and v.get("id") is not None]
    currency = _shopify_currency(html)
    title = clean_text(data.get("title")) or None
    image = _abs_img(data.get("featured_image") or (data.get("images") or [None])[0], origin)

    pin = (_query(url).get("variant") or _query(final_url).get("variant") or [None])[0]
    pinned = next((v for v in variants if str(v.get("id")) == str(pin)), None) if pin else None
    signals: list[str] = []
    multi = len(variants) > 1

    if pinned is not None:
        chosen = pinned
        in_stock = bool(pinned.get("available"))
        signals.append(f"shopify: URL selects variant {pin}")
    else:
        avail = [v for v in variants if v.get("available")]
        in_stock = bool(data.get("available")) if data.get("available") is not None else bool(avail)
        chosen = (avail[0] if avail else (variants[0] if variants else None))
        if pin:
            signals.append(f"shopify: variant {pin} not found; using the whole product")
    vtitle = clean_text(chosen.get("title")) if chosen else ""
    vlabel = vtitle if vtitle and vtitle != "Default Title" else None
    n_avail = sum(1 for v in variants if v.get("available"))
    signals.append(f"shopify: {'available' if in_stock else 'sold out'}"
                   + (f" ({n_avail} of {len(variants)} variants available)" if len(variants) > 1 and pinned is None else ""))
    if chosen and chosen.get("featured_image"):
        image = _abs_img(chosen["featured_image"], origin) or image
    price = _cents(chosen.get("price")) if chosen else _cents(data.get("price"))
    detail: dict[str, Any] = {"signals": signals, "matched": signals[-1], "variant_id": chosen.get("id") if chosen else None,
                              "variant": vlabel if (pinned is not None or multi) else None}
    if chosen is not None:
        detail["cart_url"] = shopify_cart_url(origin, chosen.get("id"))
    if in_stock:
        label = f"In stock · {vlabel}" if pinned is not None and vlabel else "In stock"
        return result("in", "In stock", available=[Availability(key="stock", label=label)], price=price,
                      currency=currency, title=title, image_url=image, detail=detail)
    st = "Selected variant sold out" if pinned is not None and n_avail else "Sold out"
    return result("out", st, price=price, currency=currency, title=title, image_url=image, detail=detail)


# --------------------------------------------------------------------------- Salesforce Commerce Cloud

_SFCC_SITE_RE = re.compile(r"/on/demandware\.store/(Sites-[A-Za-z0-9_-]+-Site)/([A-Za-z_-]+)/")
_SFCC_PID_URL_RES = [
    re.compile(r"-(\d{9,})\.html"),  # Disney
    re.compile(r"zid([A-Z0-9_-]+)\.html", re.I),  # Ninja Kitchen
    re.compile(r"/(\d{6,8})\.html"),  # GameStop
]
_SFCC_LAST_SEG_RE = re.compile(r"/([A-Za-z0-9_-]+)\.html?$")


def _sfcc_pid(url: str, soup: BeautifulSoup) -> str | None:
    q = _query(url)
    if q.get("pid"):
        return q["pid"][0]
    path = urlsplit(url).path
    for rx in _SFCC_PID_URL_RES:
        m = rx.search(path)
        if m:
            return m.group(1)
    el = soup.select_one(".product-detail[data-pid], .product-details[data-pid], .pdp[data-pid], "
                         "[itemtype*='Product'][data-pid], #product-content[data-pid], .product-wrapper[data-pid]")
    if isinstance(el, Tag) and el.get("data-pid"):
        return str(el.get("data-pid"))
    m = _SFCC_LAST_SEG_RE.search(path)
    return m.group(1) if m else None


async def check_sfcc(url: str, html: str, final_url: str, headers: dict, rcfg: RetailerConfig | None) -> CheckResult | None:
    m = _SFCC_SITE_RE.search(html or "")
    if not m:
        return None
    site, locale = m.group(1), m.group(2)
    soup = soup_of(html)
    pid = _sfcc_pid(url, soup) or _sfcc_pid(final_url, soup)
    if not pid:
        return None
    origin = _origin(final_url)
    params: list[tuple[str, str]] = [("pid", pid), ("quantity", "1")]
    for src in (url, final_url):  # variation attributes pin a variant: dwvar_<pid>_<attr>=value
        for k, vs in _query(src).items():
            if k.startswith("dwvar_") and vs and all(k != p[0] for p in params):
                params.append((k, vs[0]))
    api = f"{origin}/on/demandware.store/{site}/{locale}/Product-Variation?{urlencode(params)}"
    data = await get_json(api, headers={"X-Requested-With": "XMLHttpRequest", "Referer": final_url})
    product = data.get("product") if isinstance(data, dict) else None
    if not isinstance(product, dict):
        return None
    available = product.get("available")
    ready = product.get("readyToOrder")
    msgs = [clean_text(x) for x in (dig(product, "availability", "messages") or []) if clean_text(x)]
    msg_verdict, msg_text = classify_text(" ".join(msgs))
    signals = [f"sfcc: available={available} readyToOrder={ready}" + (f" '{'; '.join(msgs)}'" if msgs else "")]
    if available is False:
        verdict, st = "out", (msg_text if msg_verdict == "out" else "Out of stock")
    elif available is True and ready is not False:
        verdict, st = ("in", msg_text if msg_verdict == "in" else "In stock") if msg_verdict != "out" else ("out", msg_text)
    elif msg_verdict:
        verdict, st = msg_verdict, msg_text
    else:
        return None
    price = dig(product, "price", "sales", "value")
    if price is None:
        price = dig(product, "price", "sales", "formatted") or dig(product, "price", "list", "value")
    currency = dig(product, "price", "sales", "currency") or "USD"
    title = clean_text(product.get("productName")) or None
    image = None
    for size in ("large", "medium", "small", "hi-res"):
        imgs = dig(product, "images", size)
        if isinstance(imgs, list) and imgs:
            image = _abs_img(imgs[0].get("url") if isinstance(imgs[0], dict) else imgs[0], origin)
            if image:
                break
    detail = {"signals": signals, "matched": signals[0], "sku": str(product.get("id") or pid),
              "availability_message": "; ".join(msgs) or None}
    return result(verdict, st, price=price, currency=currency, title=title, image_url=image, detail=detail)


# --------------------------------------------------------------------------- BigCommerce

_BCDATA_RE = re.compile(r"\bvar\s+BCData\s*=\s*")


async def check_bigcommerce(url: str, html: str, final_url: str, headers: dict, rcfg: RetailerConfig | None) -> CheckResult | None:
    m = _BCDATA_RE.search(html or "")
    if not m:
        return None
    raw = extract_balanced(html, m.end())
    data = loads_lenient(raw) if raw else None
    attrs = data.get("product_attributes") if isinstance(data, dict) else None
    if not isinstance(attrs, dict):
        return None
    instock, purchasable, stock = attrs.get("instock"), attrs.get("purchasable"), attrs.get("stock")
    signals = [f"bigcommerce: instock={instock} purchasable={purchasable}" + (f" stock={stock}" if stock is not None else "")]
    if instock is False or (isinstance(stock, (int, float)) and not isinstance(stock, bool) and stock <= 0):
        verdict, st = "out", clean_text(attrs.get("out_of_stock_message")) or "Out of stock"
        if classify_text(st)[0] != "out":
            st = "Out of stock"
    elif instock is True and purchasable is not False:
        verdict, st = "in", "In stock"
    else:
        return None
    price = dig(attrs, "price", "without_tax", "value")
    if price is None:
        price = dig(attrs, "price", "with_tax", "value")
    currency = dig(attrs, "price", "without_tax", "currency") or dig(attrs, "price", "with_tax", "currency") or "USD"
    detail = {"signals": signals, "matched": signals[0], "sku": attrs.get("sku")}
    return result(verdict, st, price=price, currency=currency, detail=detail)


# --------------------------------------------------------------------------- WooCommerce

_WOO_SLUG_RE = re.compile(r"/product/([^/?#]+)")


def _woo_dom(html: str) -> tuple[str | None, str | None, str | None]:
    soup = soup_of(html)
    scope = soup.select_one("div.product .summary, .product .entry-summary, div.product") or soup
    stock = scope.select_one("p.stock, .stock")
    if isinstance(stock, Tag):
        cls = " ".join(stock.get("class") or [])
        txt = clean_text(stock.get_text(" ", strip=True))
        if "out-of-stock" in cls:
            return "out", classify_text(txt)[1] if classify_text(txt)[0] == "out" else "Out of stock", f"woocommerce: p.stock '{txt}'"
        if "available-on-backorder" in cls:
            return "in", "Backorder", f"woocommerce: p.stock '{txt}'"
        if "in-stock" in cls:
            return "in", "In stock", f"woocommerce: p.stock '{txt}'"
    btn = scope.select_one("button.single_add_to_cart_button, .single_add_to_cart_button")
    if isinstance(btn, Tag):
        cls = " ".join(state_classes(btn))
        if btn.has_attr("disabled") or "disabled" in cls:
            return None, None, None  # variable products disable the button until a variant is picked
        return "in", "In stock", "woocommerce: add-to-cart button"
    return None, None, None


def _norm_path(path: str) -> str:
    return unquote(path or "").rstrip("/").lower()


def _woo_matches(item: Any, slug: str | None, paths: set[str]) -> bool:
    """The Store API item really is the page's product. Old Store API versions ignore
    ``?slug=`` and return the latest products, so ``data[0]`` alone proves nothing."""
    if not isinstance(item, dict):
        return False
    if slug and isinstance(item.get("slug"), str) and unquote(item["slug"]).lower() == unquote(slug).lower():
        return True
    link = item.get("permalink")
    if isinstance(link, str) and link:
        try:
            lp = _norm_path(urlsplit(link).path)
        except ValueError:
            return False
        return bool(lp) and lp in paths
    return False


_WOO_ATTR_KEY_RE = re.compile(r"^attribute_(?:pa_)?", re.I)


def _woo_norm(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", unquote(str(v or "")).lower())


def _woo_variation_query(url: str) -> tuple[str | None, dict[str, str]]:
    """(``variation_id``, {attribute: value}) from a product URL's query string."""
    q = _query(url)
    vid = next((v for v in q.get("variation_id") or [] if v.strip().isdigit()), None)
    attrs = {k: vals[0] for k, vals in q.items() if k.lower().startswith("attribute_") and vals and vals[0].strip()}
    return vid, attrs


def _woo_pick_variation(item: dict, attrs: dict[str, str]) -> str | None:
    """The id of the one variation of ``item`` matching the URL's ``attribute_*`` values.
    Store API variations list ``{"id", "attributes": [{"name": <label>, "value": <slug>}]}``;
    an empty value means "any"."""
    variations = item.get("variations")
    if not isinstance(variations, list) or not attrs:
        return None
    hits: list[str] = []
    for var in variations:
        if not isinstance(var, dict) or var.get("id") is None:
            continue
        vattrs = [a for a in var.get("attributes") or [] if isinstance(a, dict)]
        ok = True
        for key, want in attrs.items():
            kname, wv = _woo_norm(_WOO_ATTR_KEY_RE.sub("", key)), _woo_norm(want)
            named = [a for a in vattrs if _woo_norm(a.get("name")) == kname]
            cands = named or vattrs  # labels may differ from the attribute slug
            if not any(_woo_norm(a.get("value")) in ("", wv) for a in cands):
                ok = False
                break
        if ok:
            hits.append(str(var["id"]))
    return hits[0] if len(hits) == 1 else None


async def check_woocommerce(url: str, html: str, final_url: str, headers: dict, rcfg: RetailerConfig | None) -> CheckResult | None:
    m = _WOO_SLUG_RE.search(urlsplit(final_url).path) or _WOO_SLUG_RE.search(urlsplit(url).path)
    slug = m.group(1) if m else next((s for s in reversed(urlsplit(final_url).path.split("/")) if s), None)
    origin = _origin(final_url)
    paths = {p for p in (_norm_path(urlsplit(final_url).path), _norm_path(urlsplit(url).path)) if p}
    item = None
    api_path = None
    if slug:
        for path in ("/wp-json/wc/store/v1/products", "/wp-json/wc/store/products"):
            try:
                data = await get_json(f"{origin}{path}?{urlencode({'slug': slug})}", headers={"Referer": final_url})
            except FetchError:
                continue
            if isinstance(data, list):
                item = next((d for d in data if _woo_matches(d, slug, paths)), None)
                if item is not None:
                    api_path = path
                    break
    # A variation picked in the URL (?attribute_pa_color=blue / ?variation_id=): the parent's
    # is_in_stock only means "some variation is in stock". Use the variation's own record.
    vid, vattrs = _woo_variation_query(final_url)
    if vid is None and not vattrs:
        vid, vattrs = _woo_variation_query(url)
    # (older Store APIs have no "type": only a declared non-variable type trusts the parent)
    is_variable = item is not None and (item.get("type") in (None, "variable") or bool(item.get("variations")))
    if item is not None and (vid or vattrs) and is_variable:
        if item.get("is_in_stock") is False:
            pass  # no variation of the product is in stock: the parent's answer holds
        else:
            known = {str(v.get("id")) for v in item.get("variations") or [] if isinstance(v, dict)}
            if vid is not None and known and vid not in known:
                vid = None
            if vid is None:
                vid = _woo_pick_variation(item, vattrs)
            var = None
            if vid is not None:
                try:
                    var = await get_json(f"{origin}{api_path}/{vid}", headers={"Referer": final_url})
                except FetchError:
                    var = None
            if (not isinstance(var, dict) or str(var.get("id")) != vid or var.get("is_in_stock") is None
                    or (not known and str(var.get("parent")) != str(item.get("id")))):
                return None  # unresolved variation: let the generic checker decide
            if not var.get("name"):
                var = dict(var, name=item.get("name"))
            if not var.get("images"):
                var = dict(var, images=item.get("images"))
            item = var
    if item is not None and item.get("is_in_stock") is not None:
        in_stock = bool(item.get("is_in_stock"))
        backorder = bool(item.get("is_on_backorder"))
        purchasable = item.get("is_purchasable")
        txt = clean_text(dig(item, "stock_availability", "text"))
        signals = [f"woocommerce: is_in_stock={in_stock}" + (f" '{txt}'" if txt else "")
                   + (f" (variation {item.get('id')})" if item.get("type") == "variation" else "")]
        if in_stock and purchasable is not False:
            verdict, st = "in", "Backorder" if backorder else "In stock"
        elif not in_stock:
            verdict, st = "out", "Out of stock"
        else:
            return None
        prices = item.get("prices") or {}
        price = None
        raw_price = prices.get("price")
        amount = parse_amount(raw_price)
        if amount is not None and amount > 0:
            try:
                minor = int(prices.get("currency_minor_unit", 2))
            except (TypeError, ValueError):
                minor = 2
            price = amount / (Decimal(10) ** minor)
        image = None
        imgs = item.get("images")
        if isinstance(imgs, list) and imgs:
            image = _abs_img(imgs[0], origin)
        detail = {"signals": signals, "matched": signals[0], "sku": item.get("sku") or None,
                  "product_id": item.get("id")}
        return result(verdict, st, price=price, currency=prices.get("currency_code") or "USD",
                      title=clean_text(item.get("name")) or None, image_url=image, detail=detail)
    verdict, st, sig = _woo_dom(html)
    if verdict is None:
        return None
    return result(verdict, st, detail={"signals": [sig], "matched": sig})


# --------------------------------------------------------------------------- Magento 2


def _data_price(scope: Tag | BeautifulSoup) -> str | None:
    el = scope.select_one("[data-price-type=finalPrice][data-price-amount]") or scope.select_one("[data-price-amount]")
    return str(el.get("data-price-amount")) if isinstance(el, Tag) and el.get("data-price-amount") else None


async def check_magento(url: str, html: str, final_url: str, headers: dict, rcfg: RetailerConfig | None) -> CheckResult | None:
    soup = soup_of(html)
    scope = soup.select_one(".product-info-main") or soup.select_one(".product-info-stock-sku") or soup
    stock = scope.select_one(".stock.available, .stock.unavailable, .product-info-stock-sku .stock")
    verdict = st = sig = None
    if isinstance(stock, Tag):
        cls = set(stock.get("class") or [])
        txt = clean_text(stock.get_text(" ", strip=True))
        if "unavailable" in cls:
            verdict, st = "out", classify_text(txt)[1] if classify_text(txt)[0] == "out" else "Out of stock"
        elif "available" in cls:
            tv, tt = classify_text(txt)
            verdict, st = "in", (tt if tv == "in" else "In stock")
        sig = f"magento: .stock '{txt}'"
    if verdict is None:
        btn = soup.select_one("#product-addtocart-button")
        if isinstance(btn, Tag):
            dis = btn.has_attr("disabled") or "disabled" in " ".join(state_classes(btn))
            verdict, st = ("out", "Out of stock") if dis else ("in", "In stock")
            sig = f"magento: add-to-cart button ({'disabled' if dis else 'enabled'})"
    if verdict is None:
        return None
    return result(verdict, st, price=_data_price(scope), detail={"signals": [sig], "matched": sig})


# --------------------------------------------------------------------------- OpenCart

_OC_AVAIL_RE = re.compile(r"Availability\s*:\s*([^\n<]{1,60})", re.I)


async def check_opencart(url: str, html: str, final_url: str, headers: dict, rcfg: RetailerConfig | None) -> CheckResult | None:
    soup = soup_of(html)
    scope = soup.select_one("#product-product, #product-info, #content") or soup
    m = _OC_AVAIL_RE.search(scope.get_text("\n", strip=True))
    avail = clean_text(m.group(1)) if m else ""
    if not avail:
        # MSI's store theme: no "Availability:" label, a bare "In Stock" / "Out of stock" under the price
        pw = soup.select_one("#prices-wrapper")
        if isinstance(pw, Tag):
            pv, _ = classify_text(clean_text(pw.get_text(" ", strip=True)))
            if pv:
                avail = next((clean_text(sp.get_text(" ", strip=True)) for sp in pw.find_all("span")
                              if classify_text(clean_text(sp.get_text(" ", strip=True)))[0]), "")
    btn = soup.select_one("#button-cart")
    btn_disabled = isinstance(btn, Tag) and (btn.has_attr("disabled") or "disabled" in " ".join(state_classes(btn)))
    tv, tt = classify_text(avail)
    if avail and not tv and re.fullmatch(r"\d+", avail):
        tv, tt = ("in", "In stock") if int(avail) > 0 else ("out", "Out of stock")
    sig = f"opencart: Availability '{avail}'" if avail else None
    if tv == "out":
        verdict, st = "out", tt
    elif tv == "in" and not btn_disabled:
        verdict, st = "in", tt
    elif isinstance(btn, Tag) and not btn_disabled and avail:
        # Custom stock statuses such as "2-3 Days" still let you order.
        verdict, st = "in", "In stock"
    elif btn_disabled:
        verdict, st, sig = "out", "Out of stock", "opencart: add-to-cart button disabled"
    else:
        return None
    price = None
    pel = soup.select_one("#prices-new") or scope.select_one(".price-new, ul.list-unstyled h2, .product-price, "
                                                             "[itemprop=price]")
    if isinstance(pel, Tag):
        price = pel.get("content") or clean_text(pel.get_text(" ", strip=True)) or None
    return result(verdict, st, price=price, detail={"signals": [sig], "matched": sig})


_RECIPES: dict[str, Callable[..., Any]] = {
    "shopify": check_shopify,
    "sfcc": check_sfcc,
    "bigcommerce": check_bigcommerce,
    "woocommerce": check_woocommerce,
    "magento": check_magento,
    "opencart": check_opencart,
}
