"""Amazon adapter: buy box DOM, seller detection, captcha handling."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.checkers import fetcher
from app.checkers.fetcher import FetchError, FetchResult
from app.checkers.retailers import AdapterContext, RetailerConfig, amazon, retailer_by_key

FIX = Path(__file__).parent.parent / "fixtures" / "retailers" / "bigbox"
URL = "https://www.amazon.com/Nintendo-Switch-2-System/dp/B0DZZWMB2L/ref=sr_1_1?crid=X"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def ctx(**rc) -> AdapterContext:
    return AdapterContext(retailer=retailer_by_key("amazon"), retailer_config=RetailerConfig.from_dict(rc))


def serve(monkeypatch, html: str, final_url: str | None = None) -> list:
    calls: list = []

    async def fake(url, render_js=False, *, needs=None):
        calls.append(url)
        return FetchResult(url=final_url or url, status=200, text=html)

    monkeypatch.setattr(fetcher, "fetch_html", fake)
    return calls


@pytest.mark.parametrize("url,asin", [
    (URL, "B0DZZWMB2L"),
    ("https://www.amazon.com/gp/product/B08FC5L3RG?th=1", "B08FC5L3RG"),
    ("https://amazon.com/gp/aw/d/B0CL5KNB9M", "B0CL5KNB9M"),
    ("https://www.amazon.com/s?k=ps5", None),
])
def test_asin(url, asin):
    assert amazon.asin_from_url(url) == asin


async def test_first_party_in_stock(monkeypatch):
    calls = serve(monkeypatch, fx("amazon_1p_instock.html"))
    res = await amazon.check(URL, ctx())
    assert calls == ["https://www.amazon.com/dp/B0DZZWMB2L?th=1&psc=1"]
    assert res.status == "in_stock" and res.status_text == "In stock"
    assert res.title == "Nintendo Switch 2 Console"
    assert res.price == "$449.00" and res.detail["price_value"] == 449.0
    assert res.image_url == "https://m.media-amazon.com/images/I/61large.jpg"
    assert res.detail["seller"] == "Amazon.com" and res.detail["third_party"] is False
    assert res.detail["cart_url"] == "https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0DZZWMB2L&Quantity.1=1"


async def test_third_party_buy_box(monkeypatch):
    serve(monkeypatch, fx("amazon_3p_buybox.html"))
    res = await amazon.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["seller"] == "CardKingdomDeals" and res.detail["third_party"] is True
    res = await amazon.check(URL, ctx(official_only=False))
    assert res.status == "in_stock" and res.status_text == "Only 3 left in stock"


def serve_aod(monkeypatch, html: str | None, status: int = 200) -> list:
    """The "See All Buying Options" offer list (``aodAjaxMain``); ``None`` = the request fails."""
    import httpx

    calls: list = []

    async def fake(url, *, headers=None, client=None):
        calls.append(url)
        if html is None:
            raise FetchError("Network error: ConnectError")
        return httpx.Response(status, text=html, request=httpx.Request("GET", url))

    monkeypatch.setattr(fetcher, "http_get", fake)
    return calls


async def test_only_see_all_buying_options_unreadable_offer_list(monkeypatch):
    # No buy box and the offer list can't be read: the page names no seller, so don't claim
    # "Third-party sellers only" (that would hide an Amazon restock behind a false reason).
    serve(monkeypatch, fx("amazon_see_all_buying.html"))
    calls = serve_aod(monkeypatch, None)
    res = await amazon.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "No featured offer (see all buying options)"
    assert res.detail["third_party"] is None and "aodAjaxMain" in calls[0]
    res = await amazon.check(URL, ctx(official_only=False))
    assert res.status == "in_stock" and res.status_text == "Available from other sellers"


async def test_only_see_all_buying_options_amazon_offer_is_in_stock(monkeypatch):
    serve(monkeypatch, fx("amazon_see_all_buying.html"))
    serve_aod(monkeypatch, fx("amazon_aod_amazon.html"))
    res = await amazon.check(URL, ctx())
    assert res.status == "in_stock" and res.detail["seller"] == "Amazon.com" and res.detail["third_party"] is False
    assert res.price == "$749.99" and res.detail["offers"] == 1  # the used offer doesn't count for "new"


async def test_only_see_all_buying_options_third_party_offers(monkeypatch):
    serve(monkeypatch, fx("amazon_see_all_buying.html"))
    serve_aod(monkeypatch, fx("amazon_aod_3p.html"))
    res = await amazon.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Not sold by Amazon — 1 other seller from $899.00"
    assert res.detail["seller"] == "Console Kings" and res.detail["third_party"] is True
    serve_aod(monkeypatch, "<div id='aod-container'><div id='aod-offer-list'></div></div>")
    res = await amazon.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "No offers"


async def test_currently_unavailable(monkeypatch):
    serve(monkeypatch, fx("amazon_unavailable.html"))
    res = await amazon.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Currently unavailable"


async def test_ships_from_and_sold_by_amazon_text(monkeypatch):
    serve(monkeypatch, """<html><body><span id="productTitle">Widget</span>
        <div id="availability">In Stock</div>
        <input id="add-to-cart-button" type="submit" value="Add to Cart">
        <div id="merchant-info">Ships from and sold by Amazon.com.</div></body></html>""")
    res = await amazon.check(URL, ctx())
    assert res.status == "in_stock" and res.detail["seller"] == "Amazon.com"
    assert res.detail["third_party"] is False


async def test_captcha_page_raises_blocked(monkeypatch):
    serve(monkeypatch, fx("amazon_captcha.html"))
    with pytest.raises(FetchError, match="^Blocked by bot protection on amazon.com$"):
        await amazon.check(URL, ctx())


async def test_fetch_bot_protection_is_reworded(monkeypatch):
    async def fake(url, render_js=False, *, needs=None):
        raise FetchError("Blocked by bot protection on www.amazon.com", status=503)

    monkeypatch.setattr(fetcher, "fetch_html", fake)
    with pytest.raises(FetchError, match="^Blocked by bot protection on amazon.com$"):
        await amazon.check(URL, ctx())


async def test_non_product_url_falls_through():
    assert await amazon.check("https://www.amazon.com/s?k=switch", ctx()) is None


# ------------------------------------------------------------------ review fixes

_NO_SELLER = """<html><body><span id="productTitle">Widget</span><div id="availability"><span>In Stock</span></div>
    <input id="add-to-cart-button" type="submit" value="Add to Cart">{extra}</body></html>"""


async def test_official_only_unknown_seller_is_unknown(monkeypatch):
    serve(monkeypatch, _NO_SELLER.format(extra=""))
    res = await amazon.check(URL, ctx())
    assert res.status == "unknown" and res.status_text == "Couldn't confirm the seller"
    res = await amazon.check(URL, ctx(official_only=False))
    assert res.status == "in_stock"
    serve(monkeypatch, _NO_SELLER.format(extra='<input type="hidden" id="merchantID" value="ATVPDKIKX0DER">'))
    res = await amazon.check(URL, ctx())
    assert res.status == "in_stock" and res.detail["third_party"] is False


async def test_temporarily_out_of_stock_with_add_to_cart_is_out(monkeypatch):
    serve(monkeypatch, _NO_SELLER.format(extra='<div id="merchant-info">Ships from and sold by Amazon.com.</div>')
          .replace("In Stock", "Temporarily out of stock. Order now and we'll deliver when available."))
    res = await amazon.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Temporarily out of stock"
