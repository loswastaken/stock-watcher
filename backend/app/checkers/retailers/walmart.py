"""Walmart and Sam's Club: the product page's ``__NEXT_DATA__`` (``props.pageProps.initialData.data.product``).

``availabilityStatus`` IN_STOCK / LIMITED_STOCK / OUT_OF_STOCK, buy-box seller (Walmart.com / Sam's Club are
first party), ``product == null`` means the item was delisted. PerimeterX block pages raise FetchError.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .. import fetcher, generic
from ..base import CheckResult
from ..fetcher import FetchError
from ..util import clean_text
from .base import AdapterContext, dig, next_data, result
from .bigbox import THIRD_PARTY_TEXT, base_detail, blocked, dead_link, error_result, generic_result, is_queued, queued_result

ITEM_RE = re.compile(r"/ip/(?:[^/?#]+/)?(\d{5,})(?:[/?#]|$)")
_BLOCK_RE = re.compile(r"<title>\s*robot or human\??\s*</title>|px-captcha|\brobot or human\?", re.IGNORECASE)
FIRST_PARTY = {"walmart.com", "walmart", "walmart inc", "walmart inc.", "sam's club", "sams club", "samsclub.com",
               "sam’s club"}

STATUS_IN = {"IN_STOCK": "In stock", "LIMITED_STOCK": "Limited stock", "AVAILABLE": "In stock"}
STATUS_OUT = {"OUT_OF_STOCK": "Out of stock", "NOT_AVAILABLE": "Unavailable", "UNAVAILABLE": "Unavailable",
              "RETIRED": "No longer available"}


def item_id(url: str) -> str | None:
    m = ITEM_RE.search(url)
    return m.group(1) if m else None


def cart_url(item: str) -> str:
    return f"https://affil.walmart.com/cart/addToCart?items={item}"


def _needs(html: str) -> bool:
    return "__NEXT_DATA__" in html or fetcher.has_product_signals(html)


def _is_block_page(fetched: Any) -> bool:
    path = urlsplit(fetched.url or "").path or ""
    if path.startswith("/blocked"):
        return True
    return "__NEXT_DATA__" not in fetched.text and bool(_BLOCK_RE.search(fetched.text[:200_000]))


def seller_of(product: dict) -> tuple[str | None, bool | None]:
    name = clean_text(product.get("sellerDisplayName") or product.get("sellerName")) or None
    stype = str(product.get("sellerType") or "").upper()
    if name:
        return name, name.lower() not in FIRST_PARTY
    if stype in ("INTERNAL", "EXTERNAL"):
        return None, stype == "EXTERNAL"
    return None, None


async def check(url: str, ctx: AdapterContext) -> CheckResult | None:
    host = ctx.retailer.domain  # walmart.com | samsclub.com
    is_walmart = ctx.retailer.key == "walmart"
    iid = item_id(url)
    try:
        fetched = await fetcher.fetch_html(url, render_js=bool(ctx.generic_config.get("render_js")), needs=_needs)
    except FetchError as e:
        if "bot protection" in str(e).lower():
            raise blocked(host) from e
        raise
    if is_queued(fetched):
        return queued_result(ctx, item_id=iid)
    if _is_block_page(fetched):
        raise blocked(host)
    delisted = '"product":null' in fetched.text.replace(" ", "")  # handled below as "No longer available"
    dead = None if delisted else dead_link(fetched, url, ctx, item_id=iid)
    if dead is not None:
        return dead
    final_iid = item_id(fetched.url or "")
    if iid and final_iid and final_iid != iid and iid not in fetched.text:
        # Walmart redirects a retired item to a *different* product (e.g. PS5 Pro → PS5 Digital Slim):
        # that product's stock says nothing about the one being watched.
        other = None
        nd_other = dig(next_data(fetched.text), "props", "pageProps", "initialData", "data", "product", "name")
        if isinstance(nd_other, str):
            other = clean_text(nd_other)[:80]
        msg = f"Walmart redirects this item to a different product{f' ({other})' if other else ''} — update the link"
        return error_result(ctx, msg, item_id=iid, redirected_to=fetched.url, dead_link=True)

    iid = iid or final_iid
    cart = cart_url(iid) if (iid and is_walmart) else None
    nd = next_data(fetched.text)
    data = dig(nd, "props", "pageProps", "initialData", "data")
    detail = base_detail(ctx, item_id=iid, cart_url=cart)
    if isinstance(data, dict) and "product" in data and data["product"] is None:
        return result("out", "No longer available", detail=detail)
    product = data.get("product") if isinstance(data, dict) else None
    if not isinstance(product, dict):
        return generic_result(fetched, url, ctx, item_id=iid, cart_url=cart, source="page-generic")
    upstream = str(product.get("upstreamErrorCode") or "")
    if not clean_text(product.get("name")) and not product.get("usItemId") and upstream.startswith("404"):
        # an item id the store doesn't know: a hollow product (no name / id, "OUT_OF_STOCK") with upstream
        # 404 codes, and "Uh-oh... This page could not be found." (Sam's Club 16634389868, 2026-09-29)
        return generic.missing_result(generic.NOT_FOUND_TEXT, url, fetched.url, upstream_error=upstream[:120],
                                      **base_detail(ctx, item_id=iid))

    seller, third = seller_of(product)
    detail.update(seller=seller, third_party=third, source="__NEXT_DATA__")
    status = str(product.get("availabilityStatus") or "").upper()
    detail["availability_status"] = status or None
    price = (dig(product, "priceInfo", "currentPrice", "price")
             or dig(product, "priceInfo", "currentPrice", "priceString")
             or dig(product, "priceInfo", "priceRange", "minPrice"))
    title = clean_text(product.get("name")) or None
    image = dig(product, "imageInfo", "thumbnailUrl")
    common = dict(price=price, title=title, image_url=image, detail=detail)

    if status in STATUS_IN:
        if third and ctx.retailer_config.official_only:
            return result("out", THIRD_PARTY_TEXT, **common)
        preorder = dig(product, "preOrder", "isPreOrder") is True
        return result("in", "Pre-order" if preorder else STATUS_IN[status], **common)
    if status in STATUS_OUT or "OUT_OF_STOCK" in status:
        return result("out", STATUS_OUT.get(status, "Out of stock"), **common)
    return result(None, f"Unrecognised status {status}" if status else "Unknown", **common)
