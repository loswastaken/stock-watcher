"""Big-box retailers whose pages we read as HTML (BJ's, Costco, Home Depot, Kohl's, Meijer, Office Depot,
QVC, Verizon, Toys"R"Us/Macy's, StockX, eBay), plus small helpers shared by the other big-box adapters
(target, bestbuy, amazon, walmart, kroger).

Each site function fetches the page once, applies the site's own button/phrase vocabulary and, when that
is inconclusive, returns the generic analysis of the same HTML (JSON-LD / microdata / meta / buttons), so
the page is never fetched twice.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .. import fetcher, generic
from ..base import Availability, CheckResult
from ..fetcher import FetchError, FetchResult
from ..util import clean_text, parse_amount, walk
from .base import STOCK_KEY, AdapterContext, next_data, result, soup_of

QUEUE_TEXT = "Waiting room active — drop may be live"
THIRD_PARTY_TEXT = "Third-party sellers only"


# =========================================================================== shared helpers


def base_detail(ctx: AdapterContext, **extra: Any) -> dict:
    """Detail skeleton; None values are dropped except seller/third_party (None there means 'unknown')."""
    d: dict[str, Any] = {"retailer": ctx.retailer.key, "adapter": ctx.retailer.key}
    d.update({k: v for k, v in extra.items() if v is not None or k in ("seller", "third_party")})
    return d


def is_queued(fetched: Any) -> bool:
    return bool(getattr(fetched, "queued", False))


def queued_result(ctx: AdapterContext, **extra: Any) -> CheckResult:
    return result(None, QUEUE_TEXT, detail=base_detail(ctx, queue=True, **extra))


def error_result(ctx: AdapterContext, message: str, **extra: Any) -> CheckResult:
    return CheckResult(status="error", status_text=message, error=message, detail=base_detail(ctx, **extra))


def blocked(host: str) -> FetchError:
    return FetchError(f"Blocked by bot protection on {host}")


async def fetch_page(url: str, ctx: AdapterContext, *, render_js: bool = False,
                     needs: Callable[[str], bool] | None = fetcher.has_product_signals) -> FetchResult:
    """fetch_html honouring the item's own render_js switch."""
    rjs = bool(render_js or (ctx.generic_config or {}).get("render_js"))
    return await fetcher.fetch_html(url, render_js=rjs, needs=needs)


def price_value(price: Any) -> float | None:
    amt = parse_amount(price) if price is not None else None
    return float(amt) if amt is not None else None


def generic_result(fetched: FetchResult, url: str, ctx: AdapterContext, **extra: Any) -> CheckResult:
    """The generic checker's verdict for an already-fetched page, tagged with retailer detail."""
    res = generic.analyze(fetched.text, url, ctx.generic_config, base_url=fetched.url)
    res.detail.update(base_detail(ctx))
    res.detail["fetched_via"] = "browser" if fetched.via_browser else "http"
    pv = price_value(res.price)
    if pv is not None:
        res.detail.setdefault("price_value", pv)
    res.detail.update({k: v for k, v in extra.items() if v is not None})
    return res


def override(res: CheckResult, verdict: str | None, status_text: str, *, label: str | None = None,
             matched: str | None = None) -> CheckResult:
    """Replace the verdict of a (generic) result, keeping its title/price/image."""
    if verdict == "in":
        res.status, res.available = "in_stock", [Availability(key=STOCK_KEY, label=label or status_text)]
    elif verdict == "out":
        res.status, res.available = "out_of_stock", []
    else:
        res.status, res.available = "unknown", []
    res.status_text = status_text
    if matched:
        res.detail["matched"] = matched
    return res


# --------------------------------------------------------------------------- site phrase rules


@dataclass(frozen=True)
class Rule:
    kind: str  # "button" (visible button/link text) | "text" (visible page text)
    pattern: re.Pattern
    verdict: str | None  # "in" | "out" | None (unknown)
    label: str


def _b(p: str, verdict: str | None, label: str) -> Rule:
    return Rule("button", re.compile(p, re.IGNORECASE), verdict, label)


def _t(p: str, verdict: str | None, label: str) -> Rule:
    return Rule("text", re.compile(p, re.IGNORECASE), verdict, label)


_BUTTON_SEL = ("button, input[type=submit], input[type=button], [role=button], a[class*=btn], a[class*=button], "
               "a[class*=cart], a[id*=cart], a[class*=Button], [data-testid*=button], [data-automation-id*=button]")


def _buttons(soup: BeautifulSoup) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for el in soup.select(_BUTTON_SEL):
        text = generic._button_text(el)
        if text and len(text) <= 80:
            out.append((text, generic._is_disabled(el)))
    return out


def apply_rules(html: str, rules: Iterable[Rule]) -> tuple[str | None, str, str] | None:
    """First matching rule wins → (verdict, label, why). An "in" button rule only matches enabled buttons."""
    soup = BeautifulSoup(html or "", "lxml")
    generic._strip_noise(soup)
    buttons = _buttons(soup)
    root = soup.body or soup
    text = clean_text(root.get_text(" ", strip=True))
    for r in rules:
        if r.kind == "button":
            for t, disabled in buttons:
                if r.pattern.search(t) and not (r.verdict == "in" and disabled):
                    return r.verdict, r.label, f"button: '{t}'"
        else:
            m = r.pattern.search(text)
            if m:
                return r.verdict, r.label, f"text: '{clean_text(m.group(0))[:60]}'"
    return None


async def dom_check(url: str, ctx: AdapterContext, rules: list[Rule], *, render_js: bool = False,
                    needs: Callable[[str], bool] | None = fetcher.has_product_signals,
                    fetched: FetchResult | None = None) -> CheckResult:
    if fetched is None:
        fetched = await fetch_page(url, ctx, render_js=render_js, needs=needs)
    if is_queued(fetched):
        return queued_result(ctx)
    res = generic_result(fetched, url, ctx)
    hit = apply_rules(fetched.text, rules)
    if hit:
        verdict, label, why = hit
        override(res, verdict, label, matched=why)
    return res


# =========================================================================== DOM-only sites

_ADD = r"^\W*add\s+to\s+(?:cart|bag|basket)\W*$"

BJS_RULES = [
    _b(r"^\W*(?:out\s+of\s+stock|sold\s+out|not\s+available|unavailable)\W*$", "out", "Out of stock"),
    _b(_ADD, "in", "In stock"),
    _t(r"\bthis\s+item\s+is\s+(?:currently\s+)?(?:out\s+of\s+stock|unavailable)\b", "out", "Out of stock"),
    _t(r"\b(?:no\s+longer\s+available|item\s+not\s+available)\b", "out", "No longer available"),
]

COSTCO_RULES = [
    _b(r"^\W*(?:out\s+of\s+stock|sold\s+out|item\s+not\s+available|unavailable)\W*$", "out", "Out of stock"),
    _b(_ADD, "in", "In stock"),
    _b(r"^\W*sign\s+in\s+to\s+buy\W*$", "in", "In stock (members: sign in to buy)"),
    _t(r"\b(?:product\s+not\s+found|this\s+item\s+is\s+no\s+longer\s+available)\b", "out", "No longer available"),
]

HOMEDEPOT_RULES = [
    _t(r"\bthis\s+item\s+(?:has\s+been\s+)?discontinued\b|\bdiscontinued\s+item\b", "out", "Discontinued"),
    _t(r"\bthis\s+item\s+is\s+(?:currently\s+)?unavailable\b|\bout\s+of\s+stock\s+online\b", "out",
       "Out of stock online"),
    _b(_ADD, "in", "In stock"),
    _b(r"^\W*(?:out\s+of\s+stock|unavailable|check\s+nearby\s+stores)\W*$", "out", "Out of stock online"),
]

KOHLS_RULES = [
    _t(r"\bthis\s+(?:product|item)\s+is\s+(?:currently\s+)?(?:out\s+of\s+stock|unavailable|sold\s+out)\b", "out",
       "Out of stock"),
    _b(r"^\W*(?:out\s+of\s+stock|sold\s+out|unavailable)\W*$", "out", "Out of stock"),
    _b(_ADD, "in", "In stock"),
]

MEIJER_RULES = [
    _b(r"^\W*(?:out\s+of\s+stock|sold\s+out|unavailable)\W*$", "out", "Out of stock"),
    _t(r"\b(?:currently\s+unavailable|not\s+available\s+(?:online|for\s+(?:shipping|pickup|delivery)))\b", "out",
       "Unavailable"),
    _b(_ADD, "in", "In stock"),
]

OFFICEDEPOT_RULES = [
    _t(r"\bthis\s+item\s+is\s+no\s+longer\s+available\b|\bno\s+longer\s+available\b", "out", "No longer available"),
    _b(r"^\W*(?:out\s+of\s+stock|sold\s+out|unavailable)\W*$", "out", "Out of stock"),
    _b(_ADD, "in", "In stock"),
    _t(r"\bdelivery\s+unavailable\b|\bout\s+of\s+stock\s+for\s+delivery\b", "out", "Delivery unavailable"),
]

QVC_RULES = [
    _b(r"^\W*join\s+(?:the\s+)?wait\s*-?list\W*$", "out", "Waitlist only"),
    _b(r"^\W*advanced?\s+order\W*$", "in", "Pre-order (Advanced Order)"),
    _b(r"^\W*(?:sold\s+out|out\s+of\s+stock)\W*$", "out", "Sold out"),
    _b(r"^\W*add\s+to\s+(?:cart|bag)\W*$|^\W*buy\s+now\W*$", "in", "In stock"),
    _t(r"\badvanced?\s+order\b", "in", "Pre-order (Advanced Order)"),
    _t(r"\bjoin\s+(?:the\s+)?wait\s*-?list\b", "out", "Waitlist only"),
]

VERIZON_RULES = [
    _b(r"^\W*(?:out\s+of\s+stock|sold\s+out|unavailable)\W*$", "out", "Out of stock"),
    _t(r"\bbackordered\b", "in", "Backordered (orderable)"),
    _t(r"\bships\s+by\s+\w", "in", "In stock (ships later)"),
    _b(r"^\W*pre[\s-]?order(?:\s+now)?\W*$", "in", "Pre-order"),
    _b(r"^\W*(?:add\s+to\s+cart|continue|buy\s+now)\W*$", "in", "In stock"),
    _t(r"\bout\s+of\s+stock\b|\bcurrently\s+unavailable\b", "out", "Out of stock"),
]

MACYS_RULES = [
    _t(r"\bthis\s+(?:product|item)\s+is\s+(?:currently\s+)?(?:unavailable|out\s+of\s+stock|sold\s+out)\b", "out",
       "Out of stock"),
    _b(r"^\W*(?:sold\s+out|out\s+of\s+stock|unavailable|not\s+available)\W*$", "out", "Out of stock"),
    _b(r"^\W*add\s+to\s+bag\W*$", "in", "In stock"),
]


async def bjs(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await dom_check(url, ctx, BJS_RULES)


def _costco_needs(html: str) -> bool:
    return "add-to-cart-btn" in html or fetcher.has_product_signals(html)


async def costco(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx, needs=_costco_needs)
    if is_queued(fetched):
        return queued_result(ctx)
    # The add-to-cart control is an <input id="add-to-cart-btn" value="Add to Cart|Out of Stock">.
    soup = soup_of(fetched.text)
    btn = soup.select_one("#add-to-cart-btn")
    if btn is not None:
        val = clean_text(btn.get("value") or btn.get_text(" ", strip=True))
        res = generic_result(fetched, url, ctx)
        if re.search(r"out\s+of\s+stock|sold\s+out|unavailable", val, re.IGNORECASE):
            return override(res, "out", "Out of stock", matched=f"#add-to-cart-btn: '{val}'")
        if re.search(r"add\s+to\s+cart", val, re.IGNORECASE) and not generic._is_disabled(btn):
            return override(res, "in", "In stock", matched=f"#add-to-cart-btn: '{val}'")
    return await dom_check(url, ctx, COSTCO_RULES, fetched=fetched)


async def homedepot(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await dom_check(url, ctx, HOMEDEPOT_RULES)


async def kohls(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await dom_check(url, ctx, KOHLS_RULES)


async def meijer(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await dom_check(url, ctx, MEIJER_RULES)


async def officedepot(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await dom_check(url, ctx, OFFICEDEPOT_RULES)


async def qvc(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await dom_check(url, ctx, QVC_RULES)


async def verizon(url: str, ctx: AdapterContext) -> CheckResult | None:
    return await dom_check(url, ctx, VERIZON_RULES)


_MACYS_LINK_RE = re.compile(r"https?://(?:www\.)?macys\.com/shop/product/[^\"'\s<>]*?ID=\d+[^\"'\s<>]*", re.IGNORECASE)


async def toysrus(url: str, ctx: AdapterContext) -> CheckResult | None:
    """toysrus.com is a content site; the US store runs on macys.com. A toysrus.com page that links to a
    Macy's product is followed to that product."""
    fetched = await fetch_page(url, ctx)
    if is_queued(fetched):
        return queued_result(ctx)
    res = await dom_check(url, ctx, MACYS_RULES, fetched=fetched)
    host = (urlsplit(fetched.url).hostname or urlsplit(url).hostname or "").lower()
    if res.status == "unknown" and host.endswith("toysrus.com"):
        m = _MACYS_LINK_RE.search(fetched.text)
        if m:
            target = m.group(0).replace("&amp;", "&")
            res = await dom_check(target, ctx, MACYS_RULES)
            res.detail["followed"] = target
    return res


# --------------------------------------------------------------------------- StockX


def _ask_amount(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("amount", v.get("value"))
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def stockx_lowest_ask(html: str) -> tuple[bool, float | None]:
    """(found_any_lowestAsk_field, lowest positive ask) from __NEXT_DATA__."""
    nd = next_data(html)
    if nd is None:
        return False, None
    found, best = False, None
    for node in walk(nd):
        if isinstance(node, dict) and "lowestAsk" in node:
            found = True
            amt = _ask_amount(node.get("lowestAsk"))
            if amt and amt > 0 and (best is None or amt < best):
                best = amt
    return found, best


async def stockx(url: str, ctx: AdapterContext) -> CheckResult | None:
    """Resale marketplace: 'in stock' means somebody is asking (selling). Best effort from __NEXT_DATA__."""
    fetched = await fetch_page(url, ctx, needs=lambda h: "__NEXT_DATA__" in h or fetcher.has_product_signals(h))
    if is_queued(fetched):
        return queued_result(ctx)
    res = generic_result(fetched, url, ctx, third_party=True)
    found, ask = stockx_lowest_ask(fetched.text)
    if ask is not None:
        text = f"Lowest ask ${ask:,.2f}"
        override(res, "in", text, matched="__NEXT_DATA__ lowestAsk")
        res.price = f"${ask:,.2f}"
        res.detail["price_value"] = ask
        res.detail["lowest_ask"] = ask
    elif found:
        override(res, "out", "No asks right now", matched="__NEXT_DATA__ lowestAsk")
    return res


# --------------------------------------------------------------------------- eBay

EBAY_ITEM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,14})")
_EBAY_ENDED_RE = re.compile(
    r"\bthis\s+listing\s+(?:was|has)\s+ended\b|\bbidding\s+(?:has\s+)?ended\b|\bthe\s+seller\s+has\s+ended\s+this"
    r"|\bthis\s+listing\s+has\s+been\s+removed\b|\bthis\s+listing\s+is\s+no\s+longer\s+available\b", re.IGNORECASE)
_EBAY_OOS_RE = re.compile(r"\bthis\s+item\s+is\s+(?:currently\s+)?(?:out\s+of\s+stock|sold\s+out)\b", re.IGNORECASE)
_EBAY_COND = {"newcondition": "New", "usedcondition": "Used", "refurbishedcondition": "Refurbished",
              "damagedcondition": "For parts or not working"}


def _ebay_condition(soup: BeautifulSoup, html: str) -> str | None:
    for sel in (".x-item-condition-text .ux-textspans", "[data-testid='x-item-condition'] .ux-textspans",
                ".x-item-condition-value .ux-textspans", "#vi-itm-cond"):
        el = soup.select_one(sel)
        if el is not None:
            t = clean_text(el.get_text(" ", strip=True))
            if t:
                return t
    m = re.search(r'"itemCondition"\s*:\s*"(?:https?://schema\.org/)?([A-Za-z]+)"', html)
    if m:
        return _EBAY_COND.get(m.group(1).lower(), m.group(1))
    return None


def _ebay_quantity(soup: BeautifulSoup) -> str | None:
    for sel in (".x-quantity__availability", "#qtySubTxt", "[data-testid='x-quantity'] .ux-textspans--SECONDARY"):
        el = soup.select_one(sel)
        if el is not None:
            t = clean_text(el.get_text(" ", strip=True))
            if t:
                return t
    return None


async def ebay(url: str, ctx: AdapterContext) -> CheckResult | None:
    fetched = await fetch_page(url, ctx)
    if "/splashui/challenge" in fetched.url or (len(fetched.text) < 200_000 and "/splashui/challenge" in fetched.text):
        raise blocked("ebay.com")
    if is_queued(fetched):
        return queued_result(ctx)
    m = EBAY_ITEM_RE.search(url) or EBAY_ITEM_RE.search(fetched.url)
    soup = soup_of(fetched.text)
    condition = _ebay_condition(soup, fetched.text)
    quantity = _ebay_quantity(soup)
    seller_el = soup.select_one(".x-sellercard-atf__info__about-seller a span, .x-sellercard-atf__info__about-seller"
                                " .ux-textspans, #RightSummaryPanel .mbg-nw")
    seller = clean_text(seller_el.get_text(" ", strip=True)) if seller_el is not None else None
    res = generic_result(fetched, url, ctx, item_id=m.group(1) if m else None, condition=condition,
                         quantity=quantity, seller=seller or None)

    stripped = BeautifulSoup(fetched.text, "lxml")
    generic._strip_noise(stripped)
    text = clean_text((stripped.body or stripped).get_text(" ", strip=True))
    suffix = f" · {condition}" if condition else ""
    if _EBAY_ENDED_RE.search(text):
        return override(res, "out", "Listing ended", matched="text: listing ended")
    if quantity and re.search(r"\bout\s+of\s+stock\b|^\s*0\s+available|\bsold\s+out\b", quantity, re.IGNORECASE):
        return override(res, "out", "Sold out", matched=f"quantity: '{quantity}'")
    if _EBAY_OOS_RE.search(text):
        return override(res, "out", "Sold out", matched="text: out of stock")
    buttons = [t for t, disabled in _buttons(stripped) if not disabled]
    if any(re.search(r"^\W*buy\s+it\s+now\W*$", t, re.IGNORECASE) for t in buttons):
        return override(res, "in", f"Buy It Now{suffix}", label="Buy It Now", matched="button: 'Buy It Now'")
    if res.status == "in_stock":
        return override(res, "in", f"In stock{suffix}", label="In stock")
    if any(re.search(r"^\W*place\s+bid\W*$", t, re.IGNORECASE) for t in buttons):
        return override(res, "in", f"Auction live{suffix}", label="Auction live", matched="button: 'Place bid'")
    return res


__all__ = [
    "Rule",
    "apply_rules",
    "base_detail",
    "bjs",
    "blocked",
    "costco",
    "dom_check",
    "ebay",
    "error_result",
    "fetch_page",
    "generic_result",
    "homedepot",
    "is_queued",
    "kohls",
    "meijer",
    "officedepot",
    "override",
    "price_value",
    "queued_result",
    "qvc",
    "stockx",
    "toysrus",
    "verizon",
]
