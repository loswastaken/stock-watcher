"""Amazon: product-page DOM (buy box, #availability, seller) with first-party detection.

Amazon itself is merchant ``ATVPDKIKX0DER`` / "Ships from and sold by Amazon". When the buy box
belongs to another seller (or there is no buy box, only "See all buying options") and the item is set to
official sellers only, the item reads out of stock with "Third-party sellers only".
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .. import fetcher
from ..base import CheckResult
from ..fetcher import FetchError
from ..util import clean_text
from .base import AdapterContext, result, soup_of
from .bigbox import THIRD_PARTY_TEXT, generic_result, is_queued, override, queued_result

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product|exec/obidos/ASIN)/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)
AMAZON_MERCHANT_ID = "ATVPDKIKX0DER"
BLOCKED = "Blocked by bot protection on amazon.com"
_CAPTCHA_RE = re.compile(r"opfcaptcha|/errors/validateCaptcha|click the button below to continue shopping"
                         r"|enter the characters you see below|type the characters you see in this image", re.IGNORECASE)
_AMAZON_SELLER_RE = re.compile(r"^\s*amazon(?:\.com)?(?:\s+services(?:\s+llc)?)?\s*$|^\s*amazon\.com,?\s+inc\.?\s*$",
                               re.IGNORECASE)
_SHIPS_SOLD_AMAZON_RE = re.compile(r"ships\s+from\s+and\s+sold\s+by\s+amazon", re.IGNORECASE)
PRICE_SELECTORS = (
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#apex_desktop .a-price .a-offscreen",
    "#tp_price_block_total_price_ww .a-offscreen",
)


def asin_from_url(url: str) -> str | None:
    m = ASIN_RE.search(url)
    return m.group(1).upper() if m else None


def cart_url(asin: str) -> str:
    return f"https://www.amazon.com/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"


def _needs(html: str) -> bool:
    return ('id="productTitle"' in html or "add-to-cart-button" in html or 'id="availability"' in html
            or bool(_CAPTCHA_RE.search(html[:60_000])))


def _text(soup: BeautifulSoup, *selectors: str) -> str:
    for sel in selectors:
        el = soup.select_one(sel)
        if el is not None:
            t = clean_text(el.get("value") if el.name == "input" else el.get_text(" ", strip=True))
            if t:
                return t
    return ""


def seller_info(soup: BeautifulSoup) -> tuple[str | None, bool | None]:
    """(seller name, third_party) for the buy-box offer."""
    mid = soup.select_one("input#merchantID, input[name='merchantID']")
    mid_val = clean_text(mid.get("value")) if mid is not None else ""
    profile = _text(soup, "#sellerProfileTriggerId")
    merchant_text = _text(soup, "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
                          "#merchantInfoFeature_feature_div", "#merchant-info", "#tabular-buybox")
    if mid_val:
        if mid_val == AMAZON_MERCHANT_ID:
            return "Amazon.com", False
        return profile or merchant_text or mid_val, True
    if profile:
        return profile, not bool(_AMAZON_SELLER_RE.match(profile))
    if _SHIPS_SOLD_AMAZON_RE.search(merchant_text):
        return "Amazon.com", False
    if merchant_text:
        m = re.search(r"sold\s+by\s+(.+?)(?:\s+and\s+fulfilled|\.|$)", merchant_text, re.IGNORECASE)
        name = clean_text(m.group(1)) if m else merchant_text
        name = re.sub(r"^(?:ships\s+from\s+)?", "", name, flags=re.IGNORECASE)
        if _AMAZON_SELLER_RE.match(name) or re.match(r"^amazon(\.com)?\b", name, re.IGNORECASE):
            return "Amazon.com", False
        return name[:80], True
    return None, None


def _enabled(soup: BeautifulSoup, selector: str) -> bool:
    el = soup.select_one(selector)
    return el is not None and not el.has_attr("disabled") and str(el.get("aria-disabled", "")).lower() != "true"


async def check(url: str, ctx: AdapterContext) -> CheckResult | None:
    asin = asin_from_url(url)
    if not asin:
        return None
    page = f"https://www.amazon.com/dp/{asin}?th=1&psc=1"
    try:
        fetched = await fetcher.fetch_html(page, render_js=bool(ctx.generic_config.get("render_js")), needs=_needs)
    except FetchError as e:
        if "bot protection" in str(e).lower():
            raise FetchError(BLOCKED, status=e.status) from e
        raise
    if is_queued(fetched):
        return queued_result(ctx, asin=asin)
    html = fetched.text
    if "/errors/validateCaptcha" in fetched.url or (
            'id="productTitle"' not in html and _CAPTCHA_RE.search(html[:200_000])):
        raise FetchError(BLOCKED)

    soup = soup_of(html)
    res = generic_result(fetched, url, ctx, asin=asin, cart_url=cart_url(asin))
    title = _text(soup, "#productTitle")
    if title:
        res.title = title
    price = _text(soup, *PRICE_SELECTORS)
    if price:
        tmp = result(None, price=price)
        res.price = tmp.price
        if "price_value" in tmp.detail:
            res.detail["price_value"] = tmp.detail["price_value"]
    img = soup.select_one("#landingImage")
    if img is not None and (img.get("data-old-hires") or img.get("src")):
        res.image_url = img.get("data-old-hires") or img.get("src")

    seller, third = seller_info(soup)
    res.detail.update(seller=seller, third_party=third)
    availability = _text(soup, "#availability")
    res.detail["availability_text"] = availability or None
    official = ctx.retailer_config.official_only

    has_buy = _enabled(soup, "#add-to-cart-button") or _enabled(soup, "#buy-now-button") \
        or _enabled(soup, "input[name='submit.add-to-cart']")
    preorder = bool(re.search(r"pre-?order|will\s+be\s+released\s+on", availability + " " +
                              _text(soup, "#buy-now-button", "#preorder_availability"), re.IGNORECASE)) \
        or soup.select_one("#preorder_availability, #pre-order-button") is not None
    if condition_used_only(soup) and ctx.retailer_config.condition == "new":
        return override(res, "out", "Only used offers", matched="used-only buy box")
    if has_buy:
        if third and official:
            return override(res, "out", THIRD_PARTY_TEXT, matched=f"buy box seller: {seller}")
        text = "Pre-order" if preorder else _in_text(availability)
        return override(res, "in", text, matched="#add-to-cart-button")
    if soup.select_one("#outOfStock") is not None or re.search(r"currently\s+unavailable", availability, re.IGNORECASE):
        return override(res, "out", "Currently unavailable", matched="#outOfStock")
    if soup.select_one("#buybox-see-all-buying-choices, #buybox-see-all-buying-choices-announce") is not None:
        res.detail.update(seller=None, third_party=True)
        if official:
            return override(res, "out", THIRD_PARTY_TEXT, matched="only 'See all buying options'")
        return override(res, "in", "Available from other sellers", matched="'See all buying options'")
    if re.search(r"temporarily\s+out\s+of\s+stock|out\s+of\s+stock", availability, re.IGNORECASE):
        return override(res, "out", "Out of stock", matched="#availability")
    return res


def _in_text(availability: str) -> str:
    m = re.search(r"only\s+\d+\s+left\s+in\s+stock", availability, re.IGNORECASE)
    if m:
        return clean_text(m.group(0)).capitalize()
    if re.search(r"temporarily\s+out\s+of\s+stock", availability, re.IGNORECASE):
        return "Backorder (orderable)"
    return "In stock"


def condition_used_only(soup: BeautifulSoup) -> bool:
    """Buy box only offers a used/renewed copy (no new offer)."""
    return (soup.select_one("#usedOnlyBuybox, #usedOnlyBuyBox") is not None
            and soup.select_one("#newAccordionRow, #newOfferAccordionRow, #qualifiedBuybox") is None)
