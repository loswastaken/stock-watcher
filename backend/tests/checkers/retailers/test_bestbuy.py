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
