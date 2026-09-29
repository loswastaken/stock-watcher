"""Best Buy adapter: official API, priceBlocks, button-state service and page fallback."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.checkers.retailers import AdapterContext, RetailerConfig, bestbuy, retailer_by_key

FIX = Path(__file__).parent.parent / "fixtures" / "retailers" / "bigbox"
OLD = "https://www.bestbuy.com/site/nvidia-geforce-rtx-5090-32gb-gddr7-graphics-card-dark-gun-metal/6624827.p?skuId=6624827"
NEW = "https://www.bestbuy.com/product/nvidia-geforce-rtx-5090-32gb-gddr7/JJGGLHVZ3L/sku/6624827"
NO_SKU = "https://www.bestbuy.com/product/apple-airpods-4-white/JJGCQ8XHHR"
PRICE_BLOCKS = "https://www.bestbuy.com/api/3.0/priceBlocks"
BUTTON_STATE = "https://www.bestbuy.com/button-state/api/v5/button-state"
API = "https://api.bestbuy.com/v1/products/6624827"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fj(name: str):
    return json.loads(fx(name))


def ctx(**rc) -> AdapterContext:
    return AdapterContext(retailer=retailer_by_key("bestbuy"), retailer_config=RetailerConfig.from_dict(rc))


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("BESTBUY_API_KEY", raising=False)


@pytest.mark.parametrize("url,sku", [
    (OLD, "6624827"),
    (NEW, "6624827"),
    ("https://www.bestbuy.com/site/some-thing/6624827.p", "6624827"),
    ("https://www.bestbuy.com/site/searchpage.jsp?st=5090", None),
    (NO_SKU, None),
])
def test_sku_parsing(url, sku):
    assert bestbuy.sku_from_url(url) == sku


def test_button_state_mapping():
    assert bestbuy.map_button("ADD_TO_CART") == ("in", "In stock")
    assert bestbuy.map_button("PRE_ORDER") == ("in", "Pre-order")
    assert bestbuy.map_button("CHECK_STORES")[0] == "out"
    assert bestbuy.map_button("COMING_SOON") == ("out", "Coming soon")
    assert bestbuy.map_button("SOLD_OUT_IN_YOUR_AREA")[0] == "out"
    assert bestbuy.map_button("QUEUED")[0] is None


def test_page_url_suppresses_intl_splash():
    assert bestbuy.page_url(OLD).endswith("?skuId=6624827&intl=nosplash")


@respx.mock
@pytest.mark.parametrize("url", [OLD, NEW])
async def test_price_blocks_in_stock(url):
    route = respx.get(url__startswith=PRICE_BLOCKS).mock(
        return_value=httpx.Response(200, json=fj("bestbuy_priceblocks_in.json")))
    bs = respx.get(url__startswith=BUTTON_STATE)
    res = await bestbuy.check(url, ctx())
    assert parse_qs(urlsplit(str(route.calls.last.request.url)).query)["skus"] == ["6624827"]
    assert res.status == "in_stock" and res.status_text == "In stock"
    assert res.price == "$1,999.99" and res.detail["price_value"] == 1999.99
    assert res.title.startswith("NVIDIA GeForce RTX 5090")
    assert res.detail["cart_url"] == "https://api.bestbuy.com/click/-/6624827/cart"
    assert res.detail["source"] == "priceBlocks" and res.detail["button_state"] == "ADD_TO_CART"
    assert not bs.called


@respx.mock
async def test_price_blocks_sold_out():
    respx.get(url__startswith=PRICE_BLOCKS).mock(
        return_value=httpx.Response(200, json=fj("bestbuy_priceblocks_soldout.json")))
    res = await bestbuy.check(OLD, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Sold out"


@respx.mock
async def test_price_blocks_blocked_then_button_state():
    respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(403, text="Access Denied"))
    respx.get(url__startswith=BUTTON_STATE).mock(
        return_value=httpx.Response(200, json=fj("bestbuy_buttonstate_preorder.json")))
    res = await bestbuy.check(NEW, ctx())
    assert res.status == "in_stock" and res.status_text == "Pre-order"
    assert res.detail["source"] == "button-state"


@respx.mock
async def test_stalled_endpoints_fall_back_to_page_button_state():
    respx.get(url__startswith=PRICE_BLOCKS).mock(side_effect=httpx.ReadTimeout("stalled"))
    bs = respx.get(url__startswith=BUTTON_STATE)
    page = respx.get(url__startswith="https://www.bestbuy.com/site/").mock(
        return_value=httpx.Response(200, text=fx("bestbuy_page_soldout.html")))
    res = await bestbuy.check(OLD, ctx())
    assert not bs.called  # a stalled host is not retried endpoint by endpoint
    assert "intl=nosplash" in str(page.calls.last.request.url)
    # the page's own button (SOLD_OUT) wins over stale JSON-LD and the carousel's ADD_TO_CART
    assert res.status == "out_of_stock" and res.status_text == "Sold out"
    assert res.detail["button_state"] == "SOLD_OUT" and res.detail["source"] == "page"
    assert res.title.startswith("NVIDIA GeForce RTX 5090") and res.detail["price_value"] == 1999.99


@respx.mock
async def test_product_url_without_sku_reads_it_from_page():
    respx.get(url__startswith=NO_SKU).mock(return_value=httpx.Response(200, text=fx("bestbuy_page_newstyle.html")))
    route = respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=BUTTON_STATE).mock(return_value=httpx.Response(500))
    res = await bestbuy.check(NO_SKU, ctx())
    assert "skus=6447382" in str(route.calls.last.request.url)
    assert res.status == "in_stock" and res.detail["sku"] == "6447382"
    assert res.detail["seller"] == "Best Buy" and res.detail["third_party"] is False


@respx.mock
async def test_pickup_without_api_key_reports_delivery_with_note():
    respx.get(url__startswith=PRICE_BLOCKS).mock(
        return_value=httpx.Response(200, json=fj("bestbuy_priceblocks_in.json")))
    res = await bestbuy.check(OLD, ctx(fulfillment="any", zip="55423"))
    assert res.status == "in_stock"
    assert res.status_text == "In stock · Pickup needs BESTBUY_API_KEY"


@respx.mock
async def test_official_api_with_store_pickup(monkeypatch):
    monkeypatch.setenv("BESTBUY_API_KEY", "k3y")
    prod = respx.get(url__startswith=API + ".json").mock(
        return_value=httpx.Response(200, json=fj("bestbuy_api_product.json")))
    stores = respx.get(url__startswith=API + "/stores.json").mock(
        return_value=httpx.Response(200, json=fj("bestbuy_api_stores.json")))
    pb = respx.get(url__startswith=PRICE_BLOCKS)
    res = await bestbuy.check(OLD, ctx(fulfillment="any", zip="55423", radius_miles=25))
    assert not pb.called
    assert parse_qs(urlsplit(str(prod.calls.last.request.url)).query)["apiKey"] == ["k3y"]
    assert parse_qs(urlsplit(str(stores.calls.last.request.url)).query)["postalCode"] == ["55423"]
    assert [a.key for a in res.available] == ["stock", "pickup:281", "pickup:12"]
    assert res.available[1].label == "Pickup · Richfield (3.4 mi)"
    assert res.available[2].label == "Pickup · Roseville (11.9 mi) · low stock"
    assert res.status_text == "In stock · Pickup at 2 stores"
    assert res.detail["seller"] == "Best Buy" and res.detail["third_party"] is False
    assert res.detail["source"] == "api" and res.price == "$1,999.99"


@respx.mock
async def test_official_api_marketplace_and_sold_out(monkeypatch):
    monkeypatch.setenv("BESTBUY_API_KEY", "k3y")
    body = fj("bestbuy_api_product.json")
    body["marketplace"] = True
    route = respx.get(url__startswith=API + ".json").mock(return_value=httpx.Response(200, json=body))
    res = await bestbuy.check(OLD, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    body.update(marketplace=False, orderable="SoldOut", onlineAvailability=False)
    route.mock(return_value=httpx.Response(200, json=body))
    res = await bestbuy.check(OLD, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Sold out"


@respx.mock
async def test_official_api_failure_falls_back_to_website(monkeypatch):
    monkeypatch.setenv("BESTBUY_API_KEY", "bad")
    respx.get(url__startswith=API + ".json").mock(return_value=httpx.Response(403, json={"error": "quota"}))
    respx.get(url__startswith=PRICE_BLOCKS).mock(
        return_value=httpx.Response(200, json=fj("bestbuy_priceblocks_in.json")))
    res = await bestbuy.check(OLD, ctx())
    assert res.status == "in_stock" and res.detail["source"] == "priceBlocks"


async def test_pickup_only_without_zip_is_error():
    res = await bestbuy.check(OLD, ctx(fulfillment="pickup"))
    assert res.status == "error" and res.status_text == "Set a ZIP code for pickup"


# ------------------------------------------------------------------ review fixes

def test_page_button_state_requires_the_pinned_sku():
    pinned = '<div class="pdp"><button data-sku-id="6111111" data-button-state="SOLD_OUT" disabled>Sold Out</button></div>'
    assert bestbuy.page_button_state(pinned, "6111111") == "SOLD_OUT"
    carousel = ('<div class="pdp"></div><ul><li><button data-sku-id="6999999" data-button-state="ADD_TO_CART">Add'
                '</button></li></ul>')
    assert bestbuy.page_button_state(carousel, "6111111") is None
    anonymous = '<ul><li><button data-button-state="ADD_TO_CART">Add</button></li></ul>'
    assert bestbuy.page_button_state(anonymous, "6111111") is None
    json_other = '<script>{"skuId":"6999999","buttonState":"ADD_TO_CART"}</script>'
    assert bestbuy.page_button_state(json_other, "6111111") is None
    json_mine = ('<script>[{"skuId":"6999999","buttonState":"ADD_TO_CART"},{"buttonState":"SOLD_OUT","skuId":"6111111"}]'
                 '</script>')
    assert bestbuy.page_button_state(json_mine, "6111111") == "SOLD_OUT"


@respx.mock
async def test_page_without_the_skus_button_is_not_carousel_in_stock():
    respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=BUTTON_STATE).mock(return_value=httpx.Response(500))
    respx.get(url__startswith="https://www.bestbuy.com/site/").mock(return_value=httpx.Response(200, text=(
        "<html><body><main><h1>RTX 5090</h1><p>Specifications and overview text for this card.</p></main>"
        "<section><ul><li><button data-sku-id='6500000' data-button-state='ADD_TO_CART'>Add to Cart</button></li>"
        "</ul></section></body></html>")))
    res = await bestbuy.check(OLD, ctx())
    assert res.detail.get("button_state") is None
    assert res.status != "in_stock"


@respx.mock
async def test_pickup_only_without_api_key_is_unknown():
    pb = respx.get(url__startswith=PRICE_BLOCKS).mock(
        return_value=httpx.Response(200, json=fj("bestbuy_priceblocks_in.json")))
    res = await bestbuy.check(OLD, ctx(fulfillment="pickup", zip="55423"))
    assert res.status == "unknown" and res.status_text == "Pickup needs BESTBUY_API_KEY"
    assert res.available == [] and not pb.called


@respx.mock
async def test_pickup_only_with_failing_api_is_unknown(monkeypatch):
    monkeypatch.setenv("BESTBUY_API_KEY", "bad")
    respx.get(url__startswith=API + ".json").mock(return_value=httpx.Response(403, json={"error": "quota"}))
    respx.get(url__startswith=PRICE_BLOCKS).mock(
        return_value=httpx.Response(200, json=fj("bestbuy_priceblocks_in.json")))
    res = await bestbuy.check(OLD, ctx(fulfillment="pickup", zip="55423"))
    assert res.status == "unknown" and res.available == []


# ------------------------------------------------------------------ Best Buy Marketplace (2026-09-29 report)

MKT = "https://www.bestbuy.com/product/canon-powershot-g7-x-mark-iii-20-1-megapixel-digital-camera-black/J7C86S93T6"
MKT_SKU = "https://www.bestbuy.com/product/canon-powershot-g7-x-mark-iii-20-1-megapixel-digital-camera-black/J7C86S93T6/sku/12357608"
# priceBlocks for the marketplace SKU as the false "In stock" implies: ADD_TO_CART and no seller fields
MKT_BLOCK = [{"sku": {"skuId": "12357608", "buttonState": {"buttonState": "ADD_TO_CART", "displayText": "Add to Cart",
                                                         "skuId": "12357608"},
                      "names": {"short": "Canon - PowerShot G7 X Mark III 20.1-Megapixel Digital Camera - Black"},
                      "price": {"currentPrice": 1395.99}, "condition": "new"}}]


def test_marketplace_page_signals():
    sig = bestbuy.page_signals(fx("bestbuy_page_marketplace.html"), "12357608")
    assert sig == {"seller": "Abe's Electronics Center", "third_party": True, "unavailable_online": False,
                   "high_demand": True}
    assert bestbuy.sku_from_page(fx("bestbuy_page_marketplace.html"), MKT) == "12357608"


@pytest.mark.parametrize("text,seller", [
    ("Sold & shipped by Abe's Electronics Center 4.71 (2,118 ratings)", "Abe's Electronics Center"),
    ("Sold and shipped by Abe’s Electronics Center Seller rating 4.71", "Abe’s Electronics Center"),
    ("Ships from and sold by Best Buy. Free returns", "Best Buy"),
    ("Sold by: Camera Land LLC. See all offers", "Camera Land LLC"),
])
def test_seller_from_text(text, seller):
    assert bestbuy._seller_from_text(text) == seller


@respx.mock
async def test_marketplace_bsin_url_is_third_party_not_in_stock():
    respx.get(url__startswith=MKT).mock(return_value=httpx.Response(200, text=fx("bestbuy_page_marketplace.html")))
    route = respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(200, json=MKT_BLOCK))
    res = await bestbuy.check(MKT, ctx())
    assert "skus=12357608" in str(route.calls.last.request.url)
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only (Abe's Electronics Center)"
    assert res.detail["seller"] == "Abe's Electronics Center" and res.detail["third_party"] is True
    assert res.price == "$1,395.99"
    # official sellers off: a marketplace listing in Best Buy's reservation flow still isn't "In stock"
    res = await bestbuy.check(MKT, ctx(official_only=False))
    assert res.status == "unknown" and res.status_text == bestbuy.HIGH_DEMAND_TEXT


@respx.mock
async def test_marketplace_sku_url_reads_the_page_for_the_seller():
    page = respx.get(url__startswith=MKT_SKU).mock(
        return_value=httpx.Response(200, text=fx("bestbuy_page_marketplace.html")))
    respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(200, json=MKT_BLOCK))
    res = await bestbuy.check(MKT_SKU, ctx())
    assert page.called and res.status == "out_of_stock" and "Abe's Electronics Center" in res.status_text
    # page unreachable: an 8-digit (marketplace-range) SKU with no named seller is not "In stock"
    page.mock(side_effect=httpx.ConnectError("reset"))
    res = await bestbuy.check(MKT_SKU, ctx())
    assert res.status == "unknown" and res.status_text == bestbuy.SELLER_UNCONFIRMED


@respx.mock
async def test_marketplace_fields_in_price_blocks_need_no_page():
    block = json.loads(json.dumps(MKT_BLOCK))
    block[0]["sku"]["seller"] = {"id": "abes-1", "displayName": "Abe's Electronics Center"}
    block[0]["sku"]["productOptions"] = {"multipleSellers": [{"sellerName": "Best Buy", "condition": "openBox"}]}
    page = respx.get(url__startswith=MKT_SKU)
    respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(200, json=block))
    res = await bestbuy.check(MKT_SKU, ctx())
    assert not page.called
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only (Abe's Electronics Center)"
    block[0]["sku"].pop("seller")
    block[0]["sku"]["isMarketplace"] = True
    res = await bestbuy.check(MKT_SKU, ctx())
    assert res.status == "out_of_stock" and res.detail["third_party"] is True


@respx.mock
async def test_unavailable_for_online_purchase_is_out():
    respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(403))
    respx.get(url__startswith=BUTTON_STATE).mock(return_value=httpx.Response(200, json={
        "buttonStateResponseInfos": [{"skuId": "6624827", "buttonState": "ADD_TO_CART",
                                      "displayText": "This item is currently unavailable for online purchase"}]}))
    res = await bestbuy.check(OLD, ctx())
    assert res.status == "out_of_stock" and res.status_text == bestbuy.UNAVAILABLE_ONLINE


@respx.mock
async def test_page_json_ld_in_stock_cannot_override_marketplace_seller():
    # both APIs down: the page's JSON-LD says InStock, but its seller is a marketplace one
    html = fx("bestbuy_page_marketplace.html").replace('data-button-state="ADD_TO_CART"', "")
    respx.get(url__startswith=MKT_SKU).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=BUTTON_STATE).mock(return_value=httpx.Response(500))
    res = await bestbuy.check(MKT_SKU, ctx())
    assert res.status == "out_of_stock" and res.detail["third_party"] is True
    res = await bestbuy.check(MKT_SKU, ctx(official_only=False))
    assert res.status == "unknown"  # high-demand reservation flow, not a confirmed "In stock"
    html2 = html.replace("Pickup not available for this item",
                         "This item is currently unavailable for online purchase")
    respx.get(url__startswith=MKT_SKU).mock(return_value=httpx.Response(200, text=html2))
    res = await bestbuy.check(MKT_SKU, ctx(official_only=False))
    assert res.status == "out_of_stock" and res.status_text == bestbuy.UNAVAILABLE_ONLINE


@respx.mock
async def test_preview_uses_official_seller_default_and_reports_seller():
    from app import checkers

    respx.get(url__startswith=MKT).mock(return_value=httpx.Response(200, text=fx("bestbuy_page_marketplace.html")))
    respx.get(url__startswith=PRICE_BLOCKS).mock(return_value=httpx.Response(200, json=MKT_BLOCK))
    p = await checkers.preview_url(MKT)
    assert p["status"] == "out_of_stock" and p["status_text"].startswith("Third-party sellers only")
    assert p["seller"] == "Abe's Electronics Center" and p["third_party"] is True
    assert p["name"].startswith("Canon")
