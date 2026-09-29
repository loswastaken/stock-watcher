"""Generic product-page stock detection.

Detection precedence in ``auto`` mode (first conclusive group wins; every signal found
is recorded in ``detail["signals"]``):

1. Structured data — JSON-LD, then microdata, then RDFa: schema.org ``availability`` of
   the page's main Product / ProductGroup (variants: in stock if *any* variant is, unless
   the URL pins a variant, e.g. Shopify ``?variant=123``).
2. Meta tags — ``product:availability`` / ``og:availability`` / stray ``itemprop=availability``.
3. Page heuristics, scoped to the main content (nav/header/footer/scripts/recommendation
   carousels/cart drawers removed): enabled buy button → in stock; "Sold out" / "Notify me"
   button → out; out-of-stock phrases → out; "in stock" phrase → in; only a disabled buy
   button → out.
4. Otherwise ``unknown``.

Before any of that, ``check_generic`` (auto mode) lets the platform recipes in
``retailers/platforms.py`` (Shopify, SFCC, BigCommerce, WooCommerce, Magento, OpenCart)
answer from the platform's own product data; their conclusive verdict wins. A virtual
waiting room yields ``unknown`` with ``detail["queue"] = True`` (see ``queue_result``).

Dead links are reported as such (``missing_page``): an error page (HTTP 404/410, a "Page not found"
title/heading, a ``/404`` or ``/pageerror`` final URL) → "Product page not found"; a product URL that
redirects to the store's homepage or to a non-product page → "…redirects to the homepage/elsewhere —
the link may be stale". Those are ``error`` results so the user knows to update the link.

Availability semantics: *orderable* counts as in stock — InStock, LimitedAvailability,
OnlineOnly, InStoreOnly, PreOrder, PreSale, BackOrder, MadeToOrder. The status text says
which ("Pre-order", "Backorder", ...). OutOfStock, SoldOut, Discontinued → out of stock.
"""
from __future__ import annotations

import logging
import unicodedata
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .base import Availability, CheckResult
from .fetcher import fetch_html, has_product_signals, host_of
from .retailers.platforms import detect_and_check, queue_result
from .util import absolutize, clean_text, format_price, loads_lenient, parse_amount, state_classes

log = logging.getLogger("stockwatcher.checkers.generic")

STOCK_KEY = "stock"

# --------------------------------------------------------------------------- availability vocabulary

# token -> (verdict, status text); tokens are lowercase alphanumerics only
_AVAIL: dict[str, tuple[str, str]] = {
    "instock": ("in", "In stock"),
    "limitedavailability": ("in", "Limited stock"),
    "onlineonly": ("in", "In stock online"),
    "instoreonly": ("in", "In store only"),
    "preorder": ("in", "Pre-order"),
    "presale": ("in", "Pre-order"),
    "backorder": ("in", "Backorder"),
    "madetoorder": ("in", "Made to order"),
    "outofstock": ("out", "Out of stock"),
    "soldout": ("out", "Sold out"),
    "discontinued": ("out", "Discontinued"),
    # free-form values seen in meta tags / feeds
    "available": ("in", "In stock"),
    "availablefororder": ("in", "In stock"),
    "availableforpreorder": ("in", "Pre-order"),
    "oos": ("out", "Out of stock"),
    "unavailable": ("out", "Out of stock"),
    "notavailable": ("out", "Out of stock"),
    "outofstockonline": ("out", "Out of stock"),
    "temporarilyoutofstock": ("out", "Out of stock"),
}
# preference when several in-stock tokens are present
_IN_RANK = ["instock", "available", "availablefororder", "limitedavailability", "onlineonly", "instoreonly",
            "madetoorder", "preorder", "presale", "availableforpreorder", "backorder"]

_SCHEMA_NAME = {
    "instock": "InStock", "limitedavailability": "LimitedAvailability", "onlineonly": "OnlineOnly",
    "instoreonly": "InStoreOnly", "preorder": "PreOrder", "presale": "PreSale", "backorder": "BackOrder",
    "madetoorder": "MadeToOrder", "outofstock": "OutOfStock", "soldout": "SoldOut", "discontinued": "Discontinued",
}


def norm_availability(value: Any) -> tuple[str | None, str | None]:
    """Return (token, verdict) for an availability value like 'https://schema.org/InStock',
    'schema:OutOfStock', 'in stock', 'oos'. verdict is 'in' | 'out' | None."""
    if isinstance(value, dict):
        value = value.get("@id") or value.get("@value") or value.get("name")
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str) or not value.strip():
        return None, None
    v = value.strip().rstrip("/")
    v = v.rsplit("/", 1)[-1].rsplit("#", 1)[-1].rsplit(":", 1)[-1]
    token = re.sub(r"[^a-z]", "", v.lower())
    hit = _AVAIL.get(token)
    return token, (hit[0] if hit else None)


def _label(token: str) -> str:
    return _SCHEMA_NAME.get(token, token)


# --------------------------------------------------------------------------- config


@dataclass
class GenericConfig:
    mode: str = "auto"
    selector: str | None = None
    in_stock_text: str | None = None
    out_of_stock_text: str | None = None
    render_js: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "GenericConfig":
        d = d or {}
        mode = str(d.get("mode") or "auto").strip().lower()
        if mode not in {"auto", "selector", "text"}:
            mode = "auto"

        def s(k: str) -> str | None:
            v = d.get(k)
            return v.strip() if isinstance(v, str) and v.strip() else None

        return cls(mode=mode, selector=s("selector"), in_stock_text=s("in_stock_text"),
                   out_of_stock_text=s("out_of_stock_text"), render_js=bool(d.get("render_js")))


# --------------------------------------------------------------------------- structured data


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    vals = t if isinstance(t, list) else [t]
    out = set()
    for v in vals:
        if isinstance(v, str):
            out.add(v.rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower())
    return out


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


_PRODUCT_TYPES = {"product", "productgroup", "productmodel", "individualproduct", "someproducts", "vehicle", "car"}


def _is_product(node: dict) -> bool:
    ts = _types(node)
    return bool(ts & _PRODUCT_TYPES) or any(t.endswith("product") for t in ts)


def _is_offer(node: dict) -> bool:
    return bool(_types(node) & {"offer", "aggregateoffer"})


def _compact_expanded(nodes: list) -> list:
    """Convert extruct's expanded RDFa JSON-LD (full IRIs, value lists) into compact dicts."""
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        c: dict[str, Any] = {}
        for k, v in n.items():
            if k == "@id":
                c["@id"] = v
                continue
            if k == "@type":
                c["@type"] = [str(t).rsplit("/", 1)[-1].rsplit("#", 1)[-1] for t in _as_list(v)]
                continue
            key = k.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            vals = []
            for item in _as_list(v):
                if isinstance(item, dict) and "@value" in item:
                    vals.append(item["@value"])
                elif isinstance(item, dict) and set(item) == {"@id"}:
                    vals.append({"@id": item["@id"]})
                else:
                    vals.append(item)
            c[key] = vals[0] if len(vals) == 1 else vals
        out.append(c)
    return out


@dataclass
class _Entry:
    token: str
    verdict: str | None
    raw: str
    offer: dict
    product: dict
    variant: str | None = None


@dataclass
class StructuredResult:
    source: str
    verdict: str | None = None  # in | out | None
    token: str | None = None
    signals: list[str] = field(default_factory=list)
    name: str | None = None
    image: Any = None
    price: str | None = None


class _Graph:
    def __init__(self, roots: list):
        self.nodes: list[dict] = []
        self.by_id: dict[str, dict] = {}
        stack = list(reversed(roots))
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(reversed(cur))
            elif isinstance(cur, dict):
                self.nodes.append(cur)
                nid = cur.get("@id")
                if isinstance(nid, str) and len(cur) > 1 and nid not in self.by_id:
                    self.by_id[nid] = cur
                stack.extend(reversed([v for v in cur.values() if isinstance(v, (dict, list))]))

    def deref(self, v: Any) -> Any:
        if isinstance(v, dict) and set(v) - {"@type"} == {"@id"}:
            return self.by_id.get(v["@id"], v)
        return v


def _variant_hints(url: str) -> set[str]:
    q = parse_qs(urlsplit(url).query)
    hints = set()
    for k in ("variant", "variant_id", "variantId", "sku", "skuId", "skuid"):
        for v in q.get(k, []):
            if v:
                hints.add(v)
    return hints


def _collect_entries(g: _Graph, product: dict, depth: int = 0, variant: str | None = None) -> list[_Entry]:
    entries: list[_Entry] = []
    if depth > 4:
        return entries

    def add(offer: dict, value: Any) -> None:
        token, verdict = norm_availability(value)
        if token:
            raw = value if isinstance(value, str) else str(value)
            entries.append(_Entry(token, verdict, raw, offer, product, variant))

    for off in _as_list(product.get("offers")):
        off = g.deref(off)
        if not isinstance(off, dict):
            continue
        if off.get("availability") is not None:
            add(off, off.get("availability"))
        for inner in _as_list(off.get("offers")):  # AggregateOffer.offers
            inner = g.deref(inner)
            if isinstance(inner, dict) and inner.get("availability") is not None:
                add(inner, inner.get("availability"))
    if product.get("availability") is not None and not entries:
        add(product, product.get("availability"))
    for v in _as_list(product.get("hasVariant")):
        v = g.deref(v)
        if isinstance(v, dict):
            vname = clean_text(v.get("name")) or clean_text(v.get("sku")) or None
            entries.extend(_collect_entries(g, v, depth + 1, vname))
    return entries


def _entry_matches_hint(e: _Entry, hints: set[str]) -> bool:
    for h in hints:
        for obj in (e.offer, e.product):
            for k in ("url", "@id"):
                u = obj.get(k)
                if isinstance(u, str) and re.search(rf"[=/]{re.escape(h)}(?:$|[&#/?])", u):
                    return True
            for k in ("sku", "productID", "gtin", "gtin13", "gtin12", "mpn"):
                if str(obj.get(k) or "") == h:
                    return True
    return False


_CODE_KEYS = ("sku", "mpn", "productID", "model")


def _entry_code(e: _Entry) -> str | None:
    for obj in (e.offer, e.product):
        for k in _CODE_KEYS:
            v = obj.get(k)
            if isinstance(v, (str, int)) and len(str(v).strip()) >= 5:
                return str(v).strip()
    return None


def _entry_in_path(e: _Entry, page_url: str) -> bool:
    """One of the entry's model / SKU codes is a whole token of the URL path."""
    path = unquote(urlsplit(page_url or "").path).lower()
    for obj in (e.offer, e.product):
        for k in _CODE_KEYS:
            v = obj.get(k)
            code = str(v).strip().lower() if isinstance(v, (str, int)) else ""
            if len(code) >= 5 and re.search(rf"(?:^|[^a-z0-9]){re.escape(code)}(?:$|[^a-z0-9])", path):
                return True
    return False


def _offer_price(offer: dict, product: dict) -> str | None:
    cur = offer.get("priceCurrency") or product.get("priceCurrency")
    amount = offer.get("price")
    if amount in (None, "") or parse_amount(amount) in (None, 0):
        amount = offer.get("lowPrice")
    if amount in (None, "") or parse_amount(amount) in (None, 0):
        for spec in _as_list(offer.get("priceSpecification")):
            if isinstance(spec, dict) and parse_amount(spec.get("price")):
                amount = spec.get("price")
                cur = cur or spec.get("priceCurrency")
                break
    d = parse_amount(amount)
    if d is None or d == 0:
        return None
    return format_price(amount, cur)


def _url_path(u: Any) -> str:
    if not isinstance(u, str):
        return ""
    return urlsplit(u).path.rstrip("/").lower()


def analyze_structured(roots: list, source: str, page_url: str) -> StructuredResult:
    res = StructuredResult(source=source)
    g = _Graph(roots)
    products = [n for n in g.nodes if _is_product(n)]
    if not products:
        products = [n for n in g.nodes if _is_offer(n)]
        # treat a bare Offer as a pseudo product holding itself
        products = [{"offers": n, "name": n.get("name")} for n in products]
    if not products:
        return res

    # Don't treat a ProductGroup's variants as independent top-level products.
    variant_ids = set()
    for p in products:
        for v in _as_list(p.get("hasVariant")):
            variant_ids.add(id(g.deref(v)))
    top = [p for p in products if id(p) not in variant_ids] or products

    page_path = _url_path(page_url)
    ranked = sorted(top, key=lambda p: 0 if page_path and _url_path(p.get("url")) == page_path else 1)

    main: dict | None = None
    entries: list[_Entry] = []
    for p in ranked:
        es = _collect_entries(g, p)
        if es:
            main, entries = p, es
            break
    if main is None:
        main = ranked[0]

    # microdata repeats itemprop="name" (Zotac's Magento page: the title and the "[Refurbished]" variant name
    # come back as a list): the first one is the product's own
    name = next((n for n in _as_list(main.get("name")) if isinstance(n, str) and clean_text(n)), None)
    res.name = clean_text(name) or None
    res.image = main.get("image")
    if not res.image:
        for v in _as_list(main.get("hasVariant")):
            v = g.deref(v)
            if isinstance(v, dict) and v.get("image"):
                res.image = v.get("image")
                break

    if not entries:
        return res

    hints = _variant_hints(page_url)
    pinned = [e for e in entries if _entry_matches_hint(e, hints)] if hints else []
    if not pinned and len(entries) > 1:
        # the URL path names one variant's model / SKU (LG ".../lg-oled65c5pua-oled-4k-tv": the ProductGroup
        # lists 42"–83" and only the 77" was InStock on 2026-09-29 — the 65" was OutOfStock)
        path_hits = [e for e in entries if _entry_in_path(e, page_url)]
        if path_hits and len(path_hits) < len(entries):
            pinned, hints = path_hits, {_entry_code(path_hits[0]) or "path"}
    considered = pinned or entries

    ins = [e for e in considered if e.verdict == "in"]
    outs = [e for e in considered if e.verdict == "out"]
    others = [e for e in considered if e.verdict is None]

    def sig(e: _Entry) -> str:
        s = f"{source}: {_label(e.token)}"
        if e.variant:
            s += f" (variant: {e.variant})"
        elif len(considered) > 1 and e.offer.get("name"):
            s += f" ({clean_text(e.offer.get('name'))})"
        return s

    if pinned:
        res.signals.append(f"{source}: URL selects variant {', '.join(sorted(hints))}")
    if ins:
        best = sorted(ins, key=lambda e: _IN_RANK.index(e.token) if e.token in _IN_RANK else 99)[0]
        res.verdict, res.token = "in", best.token
        extra = f" ({len(ins)} of {len(considered)} offers/variants available)" if len(considered) > 1 else ""
        res.signals.append(sig(best) + extra)
        price_from = [best] + ins
    elif outs:
        res.verdict, res.token = "out", outs[0].token
        extra = f" (all {len(considered)} offers/variants)" if len(considered) > 1 else ""
        res.signals.append(sig(outs[0]) + extra)
        price_from = outs
    else:
        res.signals.append(f"{source}: unrecognised availability '{others[0].raw}'")
        price_from = others

    for e in price_from + entries:
        p = _offer_price(e.offer, e.product)
        if p:
            res.price = p
            break
    return res


def extract_json_ld(soup: BeautifulSoup) -> list:
    roots = []
    for s in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        data = loads_lenient(s.string or s.get_text() or "")
        if data is not None:
            roots.append(data)
    return roots


def extract_micro_rdfa(html: str, url: str) -> tuple[list, list]:
    micro: list = []
    rdfa: list = []
    want = []
    if re.search(r"\bitemscope\b", html, re.I):
        want.append("microdata")
    # RDFa parsing (rdflib) is slow; only run it when schema.org RDFa is plausibly present.
    if re.search(r"\btypeof\s*=", html, re.I) and re.search(r"schema\.org|\bschema:", html, re.I) and len(html) < 3_000_000:
        want.append("rdfa")
    if not want:
        return micro, rdfa
    try:
        import extruct

        data = extruct.extract(html, base_url=url, syntaxes=want, uniform=True, errors="ignore")
        micro = data.get("microdata") or []
        rdfa = _compact_expanded(data.get("rdfa") or [])
    except Exception as e:  # extruct is strict about odd markup
        log.debug("extruct failed for %s: %s", url, e)
    return micro, rdfa


# --------------------------------------------------------------------------- meta tags


_META_AVAIL_KEYS = {"product:availability", "og:availability", "og:product:availability", "availability",
                    "product:stock_status"}


def analyze_meta(soup: BeautifulSoup) -> tuple[str | None, str | None, list[str]]:
    verdict = token = None
    signals: list[str] = []
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or "").strip().lower()
        if key in _META_AVAIL_KEYS:
            val = m.get("content") or ""
            t, v = norm_availability(val)
            if t:
                signals.append(f"meta {key}: {clean_text(val)}")
                if v and verdict is None:
                    verdict, token = v, t
    for el in soup.find_all(attrs={"itemprop": re.compile(r"^\s*availability\s*$", re.I)}):
        if el.find_parent(attrs={"itemscope": True}) is not None:
            continue  # handled as microdata
        val = el.get("content") or el.get("href") or el.get_text(" ", strip=True)
        t, v = norm_availability(val)
        if t:
            signals.append(f"itemprop availability: {clean_text(val)[:60]}")
            if v and verdict is None:
                verdict, token = v, t
    return verdict, token, signals


# --------------------------------------------------------------------------- heuristics

_NOISE_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "link", "head", "select", "option"]
_NOISE_ROLES = {"navigation", "banner", "contentinfo", "search", "dialog", "alertdialog"}
_NOISE_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:related|recommend\w*|upsells?|cross-?sells?|also-?(?:bought|viewed|like\w*)|you-?may-?also"
    r"|recently-?viewed|similar|carousel|mini-?cart|cart-?drawer|drawer-?cart|side-?cart|cookie\w*|newsletter"
    r"|breadcrumbs?|reviews?|megamenu|mega-menu|site-?header|site-?footer|announcement\w*)(?:$|[\s_-])",
    re.I,
)
_HIDDEN_CLASSES = {"hidden", "hide", "d-none", "is-hidden", "js-hidden", "visually-hidden-until-js", "u-hidden",
                   "display-none", "invisible"}
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)

BUY_RE = re.compile(
    r"\b(?:add(?:ing)?\s+to\s+(?:shopping\s+)?(?:cart|bag|basket|trolley|order)|buy\s+(?:it\s+)?now"
    r"|pre[\s-]?order(?:\s+now)?|order\s+now|purchase(?:\s+now)?|add\s+to\s+my\s+(?:cart|bag))\b",
    re.I,
)
OOS_BUTTON_RE = re.compile(
    r"^\W*(?:sold\s*out|out\s+of\s+stock|temporarily\s+out\s+of\s+stock|(?:currently\s+)?unavailable|not\s+available"
    r"|notify\s+me\b.*|email\s+me\s+when\b.*|alert\s+me\b.*|coming\s+soon|join\s+(?:the\s+)?wait\s*-?list|waitlist"
    r"|no\s+longer\s+available|discontinued)\W*$",
    re.I,
)
OOS_PHRASES = [
    ("out of stock", re.compile(r"\bout\s+of\s+stock\b", re.I)),
    ("sold out", re.compile(r"\bsold\s*out\b", re.I)),
    ("currently unavailable", re.compile(r"\bcurrently\s+unavailable\b", re.I)),
    ("notify me when available", re.compile(r"\bnotify\s+me\s+when\s+(?:it'?s\s+|this\s+item\s+is\s+|back\s+in\s+stock|available|in\s+stock)", re.I)),
    ("email me when available", re.compile(r"\bemail\s+me\s+when\s+(?:available|back\s+in\s+stock|in\s+stock)", re.I)),
    ("coming soon", re.compile(r"\bcoming\s+soon\b", re.I)),
    ("no longer available", re.compile(r"\bno\s+longer\s+available\b", re.I)),
    ("not available for purchase", re.compile(r"\bnot\s+available\s+(?:for\s+purchase|to\s+order|online)\b", re.I)),
]
IN_PHRASE_RE = re.compile(
    r"(?<!not\s)(?<!back\s)(?<!when\s)\bin\s+stock\b(?!\s+(?:alert|notification|soon))|\bready\s+to\s+ship\b"
    r"|\bavailable\s+now\b|\bonly\s+\d+\s+left\b|\bships\s+(?:today|tomorrow|in\s+\d)",
    re.I,
)


def _classes(el: Tag) -> list[str]:
    c = el.get("class") or []
    return [x.lower() for x in (c if isinstance(c, list) else str(c).split())]


def _is_hidden(el: Tag) -> bool:
    if el.has_attr("hidden") or str(el.get("aria-hidden", "")).lower() == "true":
        return True
    if el.name == "input" and str(el.get("type", "")).lower() == "hidden":
        return True
    if _HIDDEN_STYLE_RE.search(str(el.get("style") or "")):
        return True
    cls = _classes(el)
    if any(":" in c for c in cls):  # Tailwind-style "hidden md:block" is visible on desktop
        return False
    return bool(_HIDDEN_CLASSES & set(cls))


def _strip_noise(soup: BeautifulSoup) -> None:
    for t in soup.find_all(_NOISE_TAGS):
        t.decompose()
    for t in soup.find_all(["header", "footer", "nav", "aside"]):
        if t.name in ("header", "footer") and t.find_parent(["main", "article"]) is not None:
            continue
        t.decompose()
    for el in soup.find_all(True):
        if el.decomposed:
            continue
        if el.attrs is None or el.name in ("html", "body", "main"):
            continue
        role = str(el.get("role") or "").lower()
        ident = " ".join(_classes(el)) + " " + str(el.get("id") or "")
        if role in _NOISE_ROLES or _is_hidden(el) or (ident.strip() and _NOISE_CLASS_RE.search(ident)):
            el.decompose()


def _main_scope(soup: BeautifulSoup) -> Tag:
    candidates = [
        soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)}),
        soup.find("main"),
        soup.find(attrs={"role": "main"}),
        soup.find(id=re.compile(r"^(main-?content|maincontent|main|content|product)$", re.I)),
    ]
    for c in candidates:
        if isinstance(c, Tag) and len(c.get_text(" ", strip=True)) > 40:
            return c
    return soup.body or soup


def _button_text(el: Tag) -> str:
    if el.name == "input":
        return clean_text(el.get("value") or el.get("aria-label") or "")
    txt = clean_text(el.get_text(" ", strip=True))
    return txt or clean_text(el.get("aria-label") or el.get("title") or "")


def _is_disabled(el: Tag) -> bool:
    if el.has_attr("disabled") or str(el.get("aria-disabled", "")).lower() == "true":
        return True
    cls = " ".join(state_classes(el))
    if re.search(r"disabled|sold-?out|out-?of-?stock|unavailable|inactive", cls):
        return True
    fs = el.find_parent("fieldset")
    return bool(fs is not None and fs.has_attr("disabled"))


@dataclass
class HeuristicResult:
    verdict: str | None = None
    status_text: str | None = None
    matched: str | None = None
    signals: list[str] = field(default_factory=list)


def analyze_heuristics(soup: BeautifulSoup) -> HeuristicResult:
    """soup must already be noise-stripped (it is mutated by _strip_noise)."""
    res = HeuristicResult()
    scope = _main_scope(soup)
    enabled_buy: list[str] = []
    disabled_buy: list[str] = []
    oos_buttons: list[str] = []
    seen: set[int] = set()
    sel = ("button, input[type=submit], input[type=button], [role=button], a[class*=btn], a[class*=button], "
           "a[class*=cart], a[id*=cart], a[id*=buy], a[class*=buy]")
    for el in scope.select(sel):
        if id(el) in seen:
            continue
        seen.add(id(el))
        text = _button_text(el)
        if not text or len(text) > 60:
            continue
        if OOS_BUTTON_RE.search(text):
            oos_buttons.append(text)
        elif BUY_RE.search(text):
            (disabled_buy if _is_disabled(el) else enabled_buy).append(text)

    for t in enabled_buy[:2]:
        res.signals.append(f"button: '{t}' (enabled)")
    for t in disabled_buy[:2]:
        res.signals.append(f"button: '{t}' (disabled)")
    for t in oos_buttons[:2]:
        res.signals.append(f"button: '{t}'")

    text = clean_text(scope.get_text(" ", strip=True))
    oos_found = [name for name, rx in OOS_PHRASES if rx.search(text)]
    for name in oos_found[:3]:
        res.signals.append(f"text: '{name}'")
    in_m = IN_PHRASE_RE.search(text)
    if in_m:
        res.signals.append(f"text: '{clean_text(in_m.group(0)).lower()}'")

    if enabled_buy:
        t = enabled_buy[0]
        res.verdict = "in"
        res.status_text = "Pre-order" if re.search(r"pre[\s-]?order", t, re.I) else "In stock"
        res.matched = f"button: '{t}' (enabled)"
    elif oos_buttons:
        res.verdict, res.status_text, res.matched = "out", "Out of stock", f"button: '{oos_buttons[0]}'"
    elif oos_found:
        res.verdict = "out"
        res.status_text = "Sold out" if oos_found[0] == "sold out" else "Coming soon" if oos_found[0] == "coming soon" else "Out of stock"
        res.matched = f"text: '{oos_found[0]}'"
    elif in_m:
        res.verdict, res.status_text = "in", "In stock"
        res.matched = f"text: '{clean_text(in_m.group(0)).lower()}'"
    elif disabled_buy:
        res.verdict, res.status_text, res.matched = "out", "Out of stock", f"button: '{disabled_buy[0]}' (disabled)"
    return res


# --------------------------------------------------------------------------- page metadata

_TITLE_SEPS = re.compile(r"\s+(?:\||-|–|—|::|·|»)\s+")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def clean_title(title: str | None, url: str, site_name: str | None = None) -> str | None:
    """Clean a page/product title: unescape, collapse whitespace, drop a leading/trailing
    site-name segment ("Widget | Example Store", "Amazon.com: Widget")."""
    t = clean_text(title)
    if not t:
        return None
    host = host_of(url)
    if "amazon." in host or t.lower().startswith("amazon."):
        t = re.sub(r"^amazon\.[a-z.]+\s*:\s*", "", t, flags=re.I)
        t = re.sub(r"\s+:\s+[A-Za-z &,'-]{2,40}$", "", t)  # trailing " : Electronics"
    host_core = _norm(host.removeprefix("www.").rsplit(".", 1)[0]) if host else ""
    site = _norm(site_name or "")

    def is_site(seg: str) -> bool:
        n = _norm(seg)
        if len(n) < 3:
            return False
        if n in {"officialsite", "onlinestore", "officialstore", "shop", "store"}:
            return True
        if site and (n == site or site in n or n in site):
            return True
        return bool(host_core and (n in host_core or host_core in n))

    for _ in range(2):
        seps = list(_TITLE_SEPS.finditer(t))
        if not seps:
            break
        if is_site(t[seps[-1].end():]):
            t = t[: seps[-1].start()]
            continue
        if is_site(t[: seps[0].start()]):
            t = t[seps[0].end():]
            continue
        break
    return t.strip() or None


def _image_from(v: Any) -> str | None:
    for item in _as_list(v):
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            u = item.get("url") or item.get("contentUrl") or item.get("@id")
            if isinstance(u, str) and u.strip():
                return u.strip()
    return None


def _meta(soup: BeautifulSoup, *keys: str) -> str | None:
    for k in keys:
        m = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k})
        if m and m.get("content") and str(m.get("content")).strip():
            return str(m.get("content")).strip()
    return None


@dataclass
class PageMeta:
    title: str | None = None
    image_url: str | None = None
    price: str | None = None


_SHOWN_PRICE_SEL = (".product-price-sales_productDetail, .product-info-price .product-sales-price, "
                    "[data-e2e=product-price], .product__price--sale, .price__sale .price-item--sale")
_SHOWN_PRICE_RE = re.compile(r"([$£€])\s*(\d{1,3}(?:[,\s]\d{3})*|\d+)(?:\s*[.,]\s*|\s+)(\d{2})\b")


def displayed_price(soup: BeautifulSoup) -> str | None:
    """The product's own rendered sale price ("$ 119 16" with the cents in their own element), or None."""
    el = soup.select_one(_SHOWN_PRICE_SEL)
    m = _SHOWN_PRICE_RE.search(clean_text(el.get_text(" ", strip=True))) if el is not None else None
    if not m:
        return None
    whole = re.sub(r"[,\s]", "", m.group(2))
    cur = {"$": "USD", "£": "GBP", "€": "EUR"}[m.group(1)]
    return format_price(parse_amount(f"{whole}.{m.group(3)}"), cur)


def extract_page_meta(soup: BeautifulSoup, url: str, structured: list[StructuredResult]) -> PageMeta:
    site = _meta(soup, "og:site_name", "application-name")
    sd_name = next((s.name for s in structured if s.name), None)
    title_tag = soup.title.string if soup.title and soup.title.string else None
    raw_title = sd_name or _meta(soup, "og:title", "twitter:title")
    h1 = soup.find("h1")
    h1_text = clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not raw_title and title_tag:
        raw_title = title_tag
        # "Camping Stove - OutdoorCo" + <h1>Camping Stove</h1> -> use the h1
        if h1_text and len(h1_text) >= 3 and any(clean_text(seg) == h1_text for seg in _TITLE_SEPS.split(clean_text(title_tag))):
            raw_title = h1_text
    if not raw_title:
        raw_title = h1_text or None
    title = clean_title(raw_title, url, site)

    img = _meta(soup, "og:image:secure_url", "og:image", "og:image:url")
    if not img:
        img = next((_image_from(s.image) for s in structured if _image_from(s.image)), None)
    if not img:
        img = _meta(soup, "twitter:image", "twitter:image:src")
    if not img:
        link = soup.find("link", attrs={"rel": re.compile(r"image_src", re.I)})
        img = link.get("href") if link else None
    image_url = absolutize(img, url)

    price = next((s.price for s in structured if s.price), None)
    if not price:
        amount = _meta(soup, "product:price:amount", "og:price:amount", "twitter:data1")
        cur = _meta(soup, "product:price:currency", "og:price:currency")
        if amount and parse_amount(amount):
            price = format_price(amount, cur) if (cur or "$" in amount or re.fullmatch(r"[\d.,\s]+", amount)) else None
    if not price:
        el = soup.find(attrs={"itemprop": "price"})
        if el is not None:
            amount = el.get("content") or el.get_text(" ", strip=True)
            cur_el = soup.find(attrs={"itemprop": "priceCurrency"})
            cur = (cur_el.get("content") or cur_el.get_text(strip=True)) if cur_el else None
            if parse_amount(amount):
                price = format_price(amount, cur)
    return PageMeta(title=title, image_url=image_url, price=price)


# --------------------------------------------------------------------------- dead links

NOT_FOUND_TEXT = "Product page not found"
NO_STOCK_INFO_TEXT = "No stock info on the page"
HOME_REDIRECT_TEXT = "Product page redirects to the homepage — the link may be stale"
MOVED_TEXT = "Product page redirects to a non-product page — the link may be stale"

_NF_URL_RE = re.compile(
    r"(?:^|[/_.=-])(?:404|pageerror|page-?not-?found|not-?found|errorpages?|smarterror)(?:$|[/_.?&=-])", re.I)
_NF_TITLE_SEG_RE = re.compile(
    r"^\W*(?:(?:error\s*)?404\b.*|page\s+(?:not\s+found|cannot\s+be\s+found|could\s+not\s+be\s+found"
    r"|does\s*n[o']t\s+exist)|(?:product|item|page)\s+not\s+found|not\s+found|oops[!.,]*\s+(?:page\s+)?not\s+found"
    r"|this\s+page\s+(?:could\s+not\s+be\s+found|does\s*n[o']t\s+exist))\W*$",
    re.I,
)
_NF_HEADING_RE = re.compile(
    r"^\W*(?:we\s+)?(?:could\s*n[o']t|can\s*n[o']t|cannot|were\s+unable\s+to)\s+find\s+(?:anything|that\s+page|the\s+page"
    r"|the\s+product|this\s+(?:page|product))\b|^\W*(?:sorry[,!.]*\s+)?(?:this\s+|the\s+)?page\s+(?:not\s+found|cannot\s+be"
    r"\s+found|could\s+not\s+be\s+found|does\s*n[o']t\s+exist|you\s+(?:are|were)\s+looking\s+for\s+"
    r"(?:does\s*n[o']t|does\s+not|could\s+not|can\s*n[o']t))|^\W*(?:product|item)\s+not\s+found\W*$|^\W*404\b",
    re.I,
)
_NO_RESULTS_RE = re.compile(r"^\W*(?:no\s+results?(?:\s+found)?|0\s+results?|no\s+products?\s+found)\W*$", re.I)
_SEARCH_URL_RE = re.compile(r"search|[?&](?:q|query|keyword|keywords|st|term|text)=", re.I)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.S)
_TAGS_RE = re.compile(r"<[^>]+>")
_PRODUCT_PATH_RE = re.compile(r"/(?:products?|p|dp|ip|itm|item|pdp|sku)/", re.I)
_NON_PRODUCT_PATH_RE = re.compile(r"^/(?:pages|collections|search|c|category|categories|brands?|shop)(?:/|$)", re.I)
_LOCALE_SEG = r"(?:[a-z]{2}[-_][a-z]{2}|en|us|ca|uk|gb|au|de|fr|es|it|nl|jp|kr|mx|br|in)"
_HOME_PATH_RE = re.compile(
    rf"^/?(?:{_LOCALE_SEG}/){{0,2}}{_LOCALE_SEG}?/?(?:(?:default|index|home)\.(?:aspx?|html?|php|jsp|cfm))?$", re.I)


def _site(host: str) -> str:
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _same_retailer(a: str, b: str) -> bool:
    from .retailers.registry import match_retailer

    ra, rb = match_retailer(a), match_retailer(b)
    return ra is not None and ra is rb


def _strip_path(u: str) -> str:
    path = urlsplit(u).path or "/"
    return path.split(";", 1)[0]


def is_homepage_path(path: str) -> bool:
    return bool(_HOME_PATH_RE.match(path or "/"))


def missing_page(url: str, final_url: str | None, html: str | None, status: int | None = None) -> str | None:
    """Why ``url`` is a dead link, or None: an error status / "not found" page, or a redirect from a
    product URL to the site's homepage or to a non-product page (same site only — a waiting room or
    a sister store is not a dead link)."""
    if status in (404, 410):
        return f"{NOT_FOUND_TEXT} (HTTP {status})"
    final = final_url or url
    same_site = _site(host_of(final)) == _site(host_of(url)) or _same_retailer(url, final)
    fpath = _strip_path(final)
    if same_site and _NF_URL_RE.search(fpath):
        return NOT_FOUND_TEXT
    head = (html or "")[:400_000]
    m = _TITLE_TAG_RE.search(head)
    if m:
        title = clean_text(_TAGS_RE.sub(" ", m.group(1)))
        segs = [title] + _TITLE_SEPS.split(title) if title else []
        if any(_NF_TITLE_SEG_RE.match(seg) for seg in segs):
            return NOT_FOUND_TEXT
        # a product link answered with the site's empty search page (Antonline: "No Results - antonline.com")
        if any(_NO_RESULTS_RE.match(seg) for seg in segs) and not _SEARCH_URL_RE.search(url):
            return NOT_FOUND_TEXT
    for hm in _H1_RE.finditer(html or ""):
        h1 = clean_text(_TAGS_RE.sub(" ", hm.group(1)))
        if h1 and len(h1) <= 160 and _NF_HEADING_RE.search(h1):
            return NOT_FOUND_TEXT
    rpath = _strip_path(url)
    if same_site and final_url and not is_homepage_path(rpath):
        if is_homepage_path(fpath):
            return HOME_REDIRECT_TEXT
        if _PRODUCT_PATH_RE.search(rpath) and not _PRODUCT_PATH_RE.search(fpath) \
                and _NON_PRODUCT_PATH_RE.match(fpath):
            return MOVED_TEXT
    return None


def missing_result(reason: str, url: str, final_url: str | None = None, **detail: Any) -> CheckResult:
    d: dict[str, Any] = {"matched": reason, "dead_link": True}
    if final_url and final_url != url:
        d["final_url"] = final_url
    d.update({k: v for k, v in detail.items() if v is not None})
    return CheckResult(status="error", status_text=reason, available=[], error=reason, detail=d)


# --------------------------------------------------------------------------- analysis entry points


def _result(verdict: str | None, status_text: str | None, meta: PageMeta, detail: dict, label: str | None = None) -> CheckResult:
    if verdict == "in":
        st = status_text or "In stock"
        return CheckResult(status="in_stock", status_text=st,
                           available=[Availability(key=STOCK_KEY, label=label or st)],
                           price=meta.price, title=meta.title, image_url=meta.image_url, detail=detail)
    if verdict == "out":
        return CheckResult(status="out_of_stock", status_text=status_text or "Out of stock", available=[],
                           price=meta.price, title=meta.title, image_url=meta.image_url, detail=detail)
    return CheckResult(status="unknown", status_text=status_text or "Unknown", available=[],
                       price=meta.price, title=meta.title, image_url=meta.image_url, detail=detail)


def _text_alternatives(s: str | None) -> list[str]:
    if not s:
        return []
    return [clean_text(x).lower() for x in re.split(r"[\r\n]+", s) if clean_text(x)]


def _text_rule(text: str, in_text: str | None, out_text: str | None) -> tuple[str | None, str]:
    """Apply in/out text rules to (already normalised, lowercase) text. Returns (verdict, why)."""
    ins, outs = _text_alternatives(in_text), _text_alternatives(out_text)
    for o in outs:
        if o in text:
            return "out", f"found out-of-stock text '{o}'"
    for i in ins:
        if i in text:
            return "in", f"found in-stock text '{i}'"
    if outs and not ins:
        return "in", "out-of-stock text absent"
    if ins and not outs:
        return "out", "in-stock text absent"
    if not ins and not outs:
        return None, "no text rules configured"
    return None, "neither text found"


def _visible_text(soup: BeautifulSoup) -> str:
    root = soup.body or soup
    for t in root.find_all(["script", "style", "noscript", "template"]):
        t.decompose()
    return clean_text(root.get_text(" ", strip=True)).lower()


def analyze(html: str, url: str, config: dict | GenericConfig | None = None, *, base_url: str | None = None) -> CheckResult:
    """Pure analysis of a fetched page (no network). ``url`` is the item URL (used for
    variant hints); ``base_url`` (final URL after redirects) is used to absolutize links."""
    cfg = config if isinstance(config, GenericConfig) else GenericConfig.from_dict(config)
    base = base_url or url
    soup = BeautifulSoup(html or "", "lxml")

    structured: list[StructuredResult] = [analyze_structured(extract_json_ld(soup), "json-ld", url)]
    micro, rdfa = extract_micro_rdfa(html or "", base)
    if micro:
        structured.append(analyze_structured(micro, "microdata", url))
    if rdfa:
        structured.append(analyze_structured(rdfa, "rdfa", url))
    meta = extract_page_meta(soup, base, structured)
    detail: dict[str, Any] = {"mode": cfg.mode, "signals": []}
    shown = displayed_price(soup)
    if shown and meta.price and shown[:1] != meta.price[:1]:
        # structured data in the store's base currency, the page shows the visitor's (NYXI, a Shopline
        # store, 2026-09-29: JSON-LD "GBP 90" from a GB session cookie, rendered "$119.16" to the US viewer)
        detail["structured_price"] = meta.price
        meta.price = shown
    canon = soup.select_one("link[rel=canonical][href], meta[property='og:url'][content]")
    if canon is not None:
        detail["canonical"] = urljoin(base, str(canon.get("href") or canon.get("content") or "").strip()) or None
    if base_url and base_url.split("#")[0] != url.split("#")[0]:
        detail["final_url"] = base_url

    if cfg.mode == "selector":
        if not cfg.selector:
            return CheckResult(status="error", status_text="No CSS selector configured", error="Selector mode needs a CSS selector",
                               title=meta.title, image_url=meta.image_url, price=meta.price, detail=detail)
        try:
            els = soup.select(cfg.selector)
        except Exception as e:
            return CheckResult(status="error", status_text="Invalid CSS selector", error=f"Invalid CSS selector: {e}",
                               title=meta.title, image_url=meta.image_url, price=meta.price, detail=detail)
        if not els:
            detail["signals"].append(f"selector '{cfg.selector}': not found")
            detail["matched"] = "selector not found"
            return _result("out", "Out of stock", meta, detail)
        el_text = clean_text(" ".join(
            e.get_text(" ", strip=True) or str(e.get("value") or e.get("content") or e.get("aria-label") or "") for e in els
        ))
        detail["signals"].append(f"selector '{cfg.selector}': {len(els)} match(es) — '{el_text[:80]}'")
        if cfg.in_stock_text or cfg.out_of_stock_text:
            verdict, why = _text_rule(el_text.lower(), cfg.in_stock_text, cfg.out_of_stock_text)
            detail["matched"] = f"selector text: {why}"
            return _result(verdict, None, meta, detail)
        if all(_is_disabled(e) for e in els):
            detail["matched"] = "selector matched only disabled elements"
            return _result("out", "Out of stock", meta, detail)
        detail["matched"] = "selector found"
        return _result("in", "In stock", meta, detail)

    if cfg.mode == "text":
        text = _visible_text(soup)
        verdict, why = _text_rule(text, cfg.in_stock_text, cfg.out_of_stock_text)
        detail["signals"].append(f"text rule: {why}")
        detail["matched"] = why
        return _result(verdict, None, meta, detail)

    # ---- auto
    for s in structured:
        detail["signals"].extend(s.signals)
    meta_verdict, meta_token, meta_signals = analyze_meta(soup)
    detail["signals"].extend(meta_signals)
    _strip_noise(soup)
    heur = analyze_heuristics(soup)
    detail["signals"].extend(heur.signals)
    detail["signals"] = list(dict.fromkeys(detail["signals"]))[:15]

    for s in structured:
        if s.verdict:
            detail["matched"] = s.signals[-1] if s.signals else s.source
            return _result(s.verdict, _AVAIL.get(s.token or "", (None, None))[1], meta, detail)
    if meta_verdict:
        detail["matched"] = meta_signals[0] if meta_signals else "meta"
        return _result(meta_verdict, _AVAIL.get(meta_token or "", (None, None))[1], meta, detail)
    if heur.verdict:
        detail["matched"] = heur.matched
        return _result(heur.verdict, heur.status_text, meta, detail)
    detail["matched"] = None
    # nothing at all to go on (no structured data, buy/sold-out buttons or stock wording): say so
    return _result(None, "Unknown" if detail["signals"] else NO_STOCK_INFO_TEXT, meta, detail)


def _needs_for(cfg: GenericConfig) -> Callable[[str], bool]:
    if cfg.mode == "text":
        alts = _text_alternatives(cfg.in_stock_text) + _text_alternatives(cfg.out_of_stock_text)

        def needs_text(html: str) -> bool:
            low = html.lower()
            return has_product_signals(html) or any(a in low for a in alts)

        return needs_text
    if cfg.mode == "selector" and cfg.selector:
        sel = cfg.selector

        def needs_sel(html: str) -> bool:
            if has_product_signals(html):
                return True
            try:
                return bool(BeautifulSoup(html, "lxml").select_one(sel))
            except Exception:
                return True

        return needs_sel
    return has_product_signals


def merge_page_meta(res: CheckResult, page: CheckResult) -> CheckResult:
    """Fill ``res``'s missing title/image/price from a generic ``analyze`` of the page and
    append the page's signals (for display) after ``res``'s own."""
    res.title = res.title or page.title
    res.image_url = res.image_url or page.image_url
    res.price = res.price or page.price
    sigs = list(res.detail.get("signals") or []) + list(page.detail.get("signals") or [])
    res.detail["signals"] = list(dict.fromkeys(sigs))[:15]
    res.detail.setdefault("generic_status", page.status)
    return res


async def check_generic(url: str, config: dict | None) -> CheckResult:
    cfg = GenericConfig.from_dict(config)
    fetched = await fetch_html(url, render_js=cfg.render_js, needs=_needs_for(cfg))
    via = "browser" if fetched.via_browser else "http"
    status = getattr(fetched, "status", None)
    errored = status in (404, 410)  # a "not found" page is never a waiting room
    if fetched.queued and not errored:
        res = queue_result(mode=cfg.mode, fetched_via=via)
        res.detail["matched"] = "waiting room"
        return res
    missing = missing_page(url, fetched.url, fetched.text, status if errored else None)
    if cfg.mode == "auto" and not errored:
        plat = await detect_and_check(url, fetched.text, fetched.url, fetched.headers, None)
        # a platform API keyed by the URL's own product id can still answer after a homepage redirect
        if plat is not None and (missing is None or plat.status in ("in_stock", "out_of_stock")):
            merge_page_meta(plat, analyze(fetched.text, url, cfg, base_url=fetched.url))
            plat.detail.setdefault("mode", cfg.mode)
            plat.detail["fetched_via"] = via
            return plat
    if missing:
        return missing_result(missing, url, fetched.url, fetched_via=via, adapter="generic")
    result = analyze(fetched.text, url, cfg, base_url=fetched.url)
    result.detail["fetched_via"] = via
    result.detail.setdefault("adapter", "generic")
    return result


# --------------------------------------------------------------------------- "the link shows another product"
#
# Stores reuse product ids, and several ignore the slug: the 2026-09-29 run found B&H 1809440-REG
# (fujifilm_..._x100vi_...) redirecting to an open-box iPod touch, Pokémon Center 10-10185-101
# (prismatic-evolutions-elite-trainer-box) now a Phantasmal Flames Build & Battle Box, Kohl's
# prd-6589434 (lego-icons-orchid) a toddler hoodie, Play-Asia 70gk5t (mario-kart-world) Earth Defense
# Force, GameFly 5022850 (mario-kart-world) a screen protector and a Newegg RTX 5070 Ti link an RTX 5090.
# Their stock and price are another product's, so the check says the link is stale instead.

OTHER_PRODUCT_TEXT = "This link now shows a different product"
_SLUG_STOP = {"the", "and", "for", "with", "of", "in", "on", "to", "an", "by", "at", "from", "into", "or", "new",
              "buy", "shop", "product", "products", "ip", "dp", "item", "site", "game", "en", "us", "html", "htm",
              "jsp", "aspx", "php", "reg", "pdp", "sku", "com", "www"}
_MARKS_RE = re.compile(r"[™®©℠]")


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _name_words(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", _MARKS_RE.sub(" ", s or "")).encode("ascii", "ignore").decode().lower()
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", s)
            if len(w) >= 2 and w not in _SLUG_STOP and not re.fullmatch(r"\d{6,}", w)}


def slug_words(url: str) -> tuple[str | None, set[str]]:
    """(the path segment that names the product, its words): the richest ``word-word-word`` segment."""
    path = unquote(urlsplit(url or "").path)
    best: tuple[str | None, set[str]] = (None, set())
    for seg in path.split("/"):
        base = re.sub(r"\.(?:html?|jsp|aspx?|php|p)$", "", seg, flags=re.I)
        parts = [x for x in re.split(r"[-_+\s]+", base) if x]
        if len(parts) < 2:
            continue
        w = _name_words(" ".join(parts))
        if sum(1 for x in w if re.search(r"[a-z]", x)) >= 2 and len(w) > len(best[1]):
            best = (seg, w)
    return best


def _model_tokens(words: set[str]) -> set[str]:
    return {w for w in words if re.search(r"\d", w) and (re.search(r"[a-z]", w) or len(w) >= 3)}


def other_product(url: str, res: CheckResult) -> str | None:
    """The name of the product the page actually shows when it isn't the one the URL's slug names, else None."""
    seg, want = slug_words(url)
    if not seg or len(want) < 3:
        return None
    d = res.detail or {}
    # 1) the store's own URL for the page (redirect target / canonical) keeps the id but names another product
    for other in (d.get("final_url"), d.get("canonical")):
        if not other or _site(host_of(other)) != _site(host_of(url)):
            continue
        oseg, have = slug_words(other)
        if not oseg or oseg == seg or len(have) < 2:
            continue
        if len(want & have) / len(want) < 0.5:
            return res.title or oseg
        break
    # 2) the product title shares nothing with the slug, or names other model numbers
    title_words = _name_words(res.title or "")
    if len(title_words) < 2:
        return None
    common = want & title_words
    if not common:
        return res.title
    mine, theirs = _model_tokens(want), _model_tokens(title_words)
    # part numbers are spelled differently in slugs ("GV-N5090GAMING-OC-32GD" vs "RTX 5090 GAMING OC 32G"):
    # a slug model token is matched when it contains a title token or the other way round
    matched = any(a in b or b in a for a in mine for b in theirs | title_words if len(b) >= 3 or b in theirs)
    if len(common) / len(want) < 0.5 and mine and theirs and not matched:
        return res.title
    return None


def other_product_result(url: str, res: CheckResult, other: str) -> CheckResult:
    text = f"{OTHER_PRODUCT_TEXT} ({clean_text(other)[:80]}) — update the link"
    detail = dict(res.detail or {})
    detail.update(dead_link=True, other_product=clean_text(other), matched="URL names another product")
    return CheckResult(status="error", status_text=text, available=[], title=res.title, image_url=res.image_url,
                       error=text, detail=detail)

