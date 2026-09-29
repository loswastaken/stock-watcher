"""Shared helpers for the electronics / games / Microsoft adapters: page fetching with the
right browser policy, waiting-room detection, site phrase rules layered over the generic
checker, and embedded-JSON walking.

Precedence used by :func:`analyze_page` (first conclusive wins):

1. ``dom`` callback — site-specific elements (B&H ``data-selenium=stockStatus``, Micro Center
   ``inventoryCnt``...), highest confidence.
2. *strong* phrase rules — site vocabulary that must beat stale structured data
   ("Order period has ended", "Request an invitation", "Auto Notify").
3. generic structured data (JSON-LD / microdata / RDFa / meta tags).
4. *weak* phrase rules (site wording the generic heuristics don't know). A weak "in" rule never
   overrides a generic out-of-stock verdict.
5. generic button / text heuristics.

Rules match ``where``: "button" (main-area buttons), "text" (main-area text), "any" (both), or
"status" (only the buy box / availability elements, e.g. ``.stock_msg``, ``.av-stock``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from bs4 import BeautifulSoup, Tag

from .. import fetcher, generic
from ..base import CheckResult
from ..util import clean_text, parse_amount
from .base import AdapterContext, result

QUEUE_TEXT = "Waiting room active — drop may be live"

_QUEUE_HOST_RE = re.compile(r"(?:^|\.)(?:queue-it\.net|direct-queue\.playstation\.com)$", re.I)
_QUEUE_BODY_RE = re.compile(
    r"you\s+are\s+now\s+in\s+line|you\s+are\s+(?:now\s+)?in\s+the\s+(?:virtual\s+)?queue"
    r"|<title>[^<]{0,80}(?:queue-it|waiting\s+room)[^<]{0,80}</title>",
    re.I,
)


# --------------------------------------------------------------------------- waiting rooms


def is_queue_url(url: str | None) -> bool:
    return bool(url) and bool(_QUEUE_HOST_RE.search(fetcher.host_of(url)))


def is_queued(fetched: Any) -> bool:
    """True for a waiting-room page: the fetcher's ``queued`` flag (when it has one), a
    redirect to a queue host, or waiting-room wording in the page head."""
    if getattr(fetched, "queued", False):
        return True
    if is_queue_url(getattr(fetched, "url", None)):
        return True
    text = getattr(fetched, "text", "") or ""
    return bool(_QUEUE_BODY_RE.search(text[:80_000]))


def queue_result(ctx: AdapterContext, **detail: Any) -> CheckResult:
    return finish(result(None, QUEUE_TEXT, queue=True, **detail), ctx)


# --------------------------------------------------------------------------- fetching

_SCRIPT_RE = re.compile(r"<script(?![^>]*application/ld\+json)[^>]*>.*?</script>|<style[^>]*>.*?</style>", re.S | re.I)


def visible_signals(html: str) -> bool:
    """Product signals outside inline JS: a client-rendered shell whose bundles merely
    *contain* "add to cart" strings doesn't count (so the browser gets a turn)."""
    return fetcher.has_product_signals(_SCRIPT_RE.sub(" ", html or ""))


async def fetch_page(url: str, ctx: AdapterContext, *, needs: Callable[[str], bool] | None = None,
                     render_js: bool | None = None) -> Any:
    """fetcher.fetch_html with the retailer's browser policy: sites flagged ``browser`` need
    *visible* product signals, otherwise the fetcher retries the page in Chromium."""
    if needs is None:
        needs = visible_signals if ctx.retailer.browser else fetcher.has_product_signals
    render = bool(ctx.generic_config.get("render_js")) if render_js is None else render_js
    return await fetcher.fetch_html(url, render_js=render, needs=needs)


# --------------------------------------------------------------------------- results


def finish(res: CheckResult, ctx: AdapterContext, **detail: Any) -> CheckResult:
    """Stamp retailer/adapter keys and derive price_value from the formatted price."""
    res.detail.setdefault("retailer", ctx.retailer.key)
    res.detail.setdefault("adapter", ctx.retailer.key)
    for k, v in detail.items():
        if v is not None:
            res.detail[k] = v
    if res.detail.get("price_value") is None and res.price:
        amt = parse_amount(res.price)
        if amt is not None:
            res.detail["price_value"] = float(amt)
    res.detail.setdefault("price_value", None)
    return res


def seller_detail(ctx: AdapterContext, seller: str | None = None, third_party: bool | None = False) -> dict:
    return {"seller": seller or ctx.retailer.name, "third_party": third_party}


# --------------------------------------------------------------------------- phrase rules


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    verdict: str | None  # "in" | "out" | None (unknown)
    text: str
    strong: bool = False
    where: str = "any"  # "button" | "text" | "any" | "status"


def rule(rx: str, verdict: str | None, text: str, *, strong: bool = False, where: str = "any") -> Rule:
    return Rule(re.compile(rx, re.I), verdict, text, strong, where)


@dataclass
class PageView:
    soup: BeautifulSoup  # full, untouched document
    text: str  # visible main-content text (noise stripped)
    buttons: list[tuple[str, bool]]  # (text, disabled)
    scope: Tag | BeautifulSoup | None = None  # main product area of a noise-stripped copy
    status: str = ""  # text of the buy box / availability / stock-status elements in ``scope``


_BUTTON_SEL = ("button, input[type=submit], input[type=button], [role=button], a[class*=btn], a[class*=button], "
               "a[class*=cart], a[id*=cart], a[id*=buy], a[class*=buy]")


_STATUS_RE = re.compile(r"stock|avail|status|inventory|buy-?box|buybox|add-?to-?cart|purchase|ship", re.I)


def _status_text(scope: Any) -> str:
    texts: list[str] = []
    for el in scope.find_all(True):
        ident = " ".join(el.get("class") or []) + " " + str(el.get("id") or "")
        if not _STATUS_RE.search(ident) or el.find("h1") is not None:
            continue
        t = clean_text(el.get_text(" ", strip=True))
        if t and len(t) <= 300 and not any(t in x for x in texts):
            texts.append(t)
    return " | ".join(texts)


def page_view(html: str) -> PageView:
    full = BeautifulSoup(html or "", "lxml")
    work = BeautifulSoup(html or "", "lxml")
    generic._strip_noise(work)  # noqa: SLF001 - shared package internals
    scope = product_scope(work)
    buttons: list[tuple[str, bool]] = []
    for el in scope.select(_BUTTON_SEL):
        t = generic._button_text(el)  # noqa: SLF001
        if t and len(t) <= 60:
            buttons.append((t, generic._is_disabled(el)))  # noqa: SLF001
    return PageView(soup=full, text=clean_text(scope.get_text(" ", strip=True)), buttons=buttons,
                    scope=scope, status=_status_text(scope))


def product_scope(work: BeautifulSoup) -> Any:
    """Where the page's own product lives (``work`` already noise-stripped): generic's main scope,
    or — when that fell back to <body> because <main> has little text — a Product itemtype / <main>
    container that holds the page's <h1>, so related-product blocks outside it don't count."""
    scope = generic._main_scope(work)  # noqa: SLF001
    if scope is not (work.body or work):
        return scope
    for c in (work.find(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)}), work.find("main"),
              work.find(attrs={"role": "main"})):
        if isinstance(c, Tag) and c.find("h1") is not None:
            return c
    return scope


def match_rules(view: PageView, rules: Iterable[Rule], *, strong: bool,
                allow_in: bool = True) -> tuple[Rule, str] | None:
    for r in rules:
        if r.strong != strong or (r.verdict == "in" and not allow_in):
            continue
        if r.where == "status":
            m = r.pattern.search(view.status)
            if m:
                return r, f"status: '{clean_text(m.group(0))}'"
            continue
        if r.where in ("button", "any"):
            for t, disabled in view.buttons:
                if r.pattern.search(t) and not (r.verdict == "in" and disabled):
                    return r, f"button: '{t}'" + (" (disabled)" if disabled else "")
        if r.where in ("text", "any"):
            m = r.pattern.search(view.text)
            if m:
                return r, f"text: '{clean_text(m.group(0))}'"
    return None


def _foreign_buy_button(g: CheckResult, view: PageView) -> bool:
    """generic said "in" because of an enabled buy button, but the product area has none (the
    button belonged to a related product outside it)."""
    m = str(g.detail.get("matched") or "")
    if g.status != "in_stock" or not m.startswith("button:"):
        return False
    return not any(generic.BUY_RE.search(t) and not d for t, d in view.buttons)


def _structured_verdict(g: CheckResult) -> bool:
    m = str(g.detail.get("matched") or "")
    return g.status in ("in_stock", "out_of_stock") and m.startswith(("json-ld", "microdata", "rdfa", "meta", "itemprop"))


@dataclass
class Hit:
    """A site-specific verdict. ``available`` overrides the default ``stock`` entry;
    ``price`` overrides the generic price."""

    verdict: str | None
    text: str
    signal: str
    detail: dict | None = None
    available: list | None = None
    price: Any = None
    title: str | None = None


def analyze_page(
    html: str,
    url: str,
    ctx: AdapterContext,
    final_url: str | None = None,
    *,
    dom: Callable[[PageView], Hit | None] | None = None,
    rules: Iterable[Rule] = (),
    fall_through: bool = False,
    extra: dict | None = None,
) -> CheckResult | None:
    """Apply site rules over the generic analysis (see module docstring). With
    ``fall_through`` an inconclusive page returns None (platform recipes run next)."""
    rules = list(rules)
    g = generic.analyze(html, url, ctx.generic_config, base_url=final_url or url)
    view = page_view(html)
    if _foreign_buy_button(g, view):
        oos = next((name for name, rx in generic.OOS_PHRASES if rx.search(view.text)), None)
        g.available = []
        if oos:
            g.status, g.status_text = "out_of_stock", "Sold out" if oos == "sold out" else "Out of stock"
            g.detail["matched"] = f"text: '{oos}'"
        else:
            g.status, g.status_text = "unknown", "Unknown"
            g.detail["matched"] = None
    signals = list(g.detail.get("signals") or [])
    extra = dict(extra or {})

    def mine(h: Hit) -> CheckResult:
        d = {"signals": list(dict.fromkeys([h.signal] + signals))[:15], "matched": h.signal}
        d.update(extra)
        d.update(h.detail or {})
        res = result(h.verdict, h.text, available=h.available if h.verdict == "in" else None,
                     price=h.price, title=h.title or g.title, image_url=g.image_url, detail=d)
        if h.price is None:
            res.price = g.price
        return finish(res, ctx)

    if dom is not None:
        hit = dom(view)
        if hit:
            return mine(hit)
    hit2 = match_rules(view, rules, strong=True)
    if hit2:
        return mine(Hit(hit2[0].verdict, hit2[0].text, hit2[1]))
    if _structured_verdict(g):
        return finish(g, ctx, adapter="generic", **extra)
    # a clear generic out-of-stock verdict beats weak site wording that claims stock
    hit2 = match_rules(view, rules, strong=False, allow_in=g.status != "out_of_stock")
    if hit2:
        return mine(Hit(hit2[0].verdict, hit2[0].text, hit2[1]))
    if g.status in ("in_stock", "out_of_stock"):
        return finish(g, ctx, adapter="generic", **extra)
    if fall_through:
        return None
    return finish(g, ctx, adapter="generic", **extra)


async def check_page(url: str, ctx: AdapterContext, **kw: Any) -> CheckResult | None:
    """Fetch + waiting-room check + :func:`analyze_page`."""
    needs = kw.pop("needs", None)
    fetched = await fetch_page(url, ctx, needs=needs)
    if is_queued(fetched):
        return queue_result(ctx)
    res = analyze_page(fetched.text, url, ctx, fetched.url, **kw)
    if res is not None:
        res.detail.setdefault("fetched_via", "browser" if getattr(fetched, "via_browser", False) else "http")
    return res


# --------------------------------------------------------------------------- embedded JSON


def iter_dicts(obj: Any) -> Iterator[dict]:
    from ..util import walk

    for node in walk(obj):
        if isinstance(node, dict):
            yield node


_ASSIGN_CACHE: dict[str, re.Pattern] = {}


def js_assignment(html: str, name: str) -> Any:
    """Parse ``window.NAME = {...}`` / ``NAME = {...}`` / ``"NAME": {...}`` from inline JS."""
    from ..util import extract_balanced, loads_lenient

    rx = _ASSIGN_CACHE.get(name)
    if rx is None:
        rx = _ASSIGN_CACHE[name] = re.compile(r"(?:window\.|\b)" + re.escape(name) + r"\s*=\s*(?:JSON\.parse\()?")
    for m in rx.finditer(html or ""):
        i = m.end()
        while i < len(html) and html[i] in " \t\r\n":
            i += 1
        if i < len(html) and html[i] in "'\"":  # JSON.parse("...") string form
            q = html[i]
            j = i + 1
            while j < len(html) and not (html[j] == q and html[j - 1] != "\\"):
                j += 1
            try:
                import json

                return json.loads(json.loads(html[i:j + 1])) if q == '"' else None
            except ValueError:
                continue
        lit = extract_balanced(html, i)
        if lit:
            data = loads_lenient(lit)
            if data is not None:
                return data
    return None


def soup_text(el: Tag | None) -> str:
    return clean_text(el.get_text(" ", strip=True)) if el is not None else ""
