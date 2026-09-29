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

Availability semantics: *orderable* counts as in stock — InStock, LimitedAvailability,
OnlineOnly, InStoreOnly, PreOrder, PreSale, BackOrder, MadeToOrder. The status text says
which ("Pre-order", "Backorder", ...). OutOfStock, SoldOut, Discontinued → out of stock.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from .base import Availability, CheckResult
from .fetcher import fetch_html, has_product_signals, host_of
from .util import absolutize, clean_text, format_price, loads_lenient, parse_amount

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

    res.name = clean_text(main.get("name")) or None
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
    cls = " ".join(_classes(el))
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
    return _result(None, "Unknown", meta, detail)


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


async def check_generic(url: str, config: dict | None) -> CheckResult:
    cfg = GenericConfig.from_dict(config)
    fetched = await fetch_html(url, render_js=cfg.render_js, needs=_needs_for(cfg))
    result = analyze(fetched.text, url, cfg, base_url=fetched.url)
    result.detail["fetched_via"] = "browser" if fetched.via_browser else "http"
    return result
