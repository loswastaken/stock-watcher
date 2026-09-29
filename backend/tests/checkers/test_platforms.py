"""Platform recipes (Shopify, SFCC, BigCommerce, WooCommerce, Magento, OpenCart), waiting
rooms and the preview path. Hand-written fixtures + respx; no live network."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

import app.checkers as checkers
from app.checkers import generic
from app.checkers import retailers as retailers_pkg
from app.checkers.base import CheckResult
from app.checkers.fetcher import QUEUE_STATUS_TEXT
from app.checkers.retailers import platforms
from app.checkers.retailers.platforms import detect_and_check, detect_platform, next_data_find, queue_result

from .conftest import load


def fx(name: str) -> str:
    return load(f"platforms/{name}")


def fxj(name: str):
    return json.loads(fx(name))


# ------------------------------------------------------------------ detection


@pytest.mark.parametrize(("name", "platform"), [
    ("shopify_product.html", "shopify"),
    ("sfcc_gamestop.html", "sfcc"),
    ("sfcc_disney.html", "sfcc"),
    ("bigcommerce_in.html", "bigcommerce"),
    ("woo_product.html", "woocommerce"),
    ("magento_in.html", "magento"),
    ("opencart_in.html", "opencart"),
    ("robot_product.html", None),
])
def test_detect_platform(name, platform):
    assert detect_platform(fx(name)) == platform


def test_detect_shopify_from_headers():
    assert detect_platform("<html></html>", {"X-ShopId": "123"}) == "shopify"
    assert detect_platform("<html></html>", {"powered-by": "Shopify"}) == "shopify"
    assert detect_platform("<html></html>", {"server": "nginx"}) is None


async def test_unknown_platform_returns_none():
    assert await detect_and_check("https://x.example/p", "<html><body>hi</body></html>", "https://x.example/p", {}) is None


# ------------------------------------------------------------------ Shopify

SHOP = "https://us.govee.com/products/rgbic-floor-lamp"
SHOP_JS = "https://us.govee.com/products/rgbic-floor-lamp.js"


@respx.mock
async def test_shopify_any_variant_in_stock():
    respx.get(SHOP).mock(return_value=httpx.Response(200, text=fx("shopify_product.html")))
    js = respx.get(SHOP_JS).mock(return_value=httpx.Response(200, json=fxj("shopify_product.js.json")))
    r = await generic.check_generic(SHOP, {"mode": "auto"})
    assert js.called
    assert r.status == "in_stock" and r.status_text == "In stock"
    assert r.detail["adapter"] == "shopify"
    assert r.detail["variant_id"] == 40002  # first available variant
    assert r.detail["cart_url"] == "https://us.govee.com/cart/40002:1"
    assert r.price == "$94.99" and r.detail["price_value"] == pytest.approx(94.99)
    assert r.title == "Govee RGBIC Floor Lamp"
    assert r.image_url == "https://us.govee.com/cdn/shop/files/floor-lamp-white.jpg"
    assert "1 of 2 variants available" in r.detail["matched"]
    assert r.detail["fetched_via"] == "http"


@respx.mock
async def test_shopify_pinned_variant_sold_out_overrides_jsonld():
    url = SHOP + "?variant=40001"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx("shopify_product.html")))
    respx.get(SHOP_JS).mock(return_value=httpx.Response(200, json=fxj("shopify_product.js.json")))
    r = await generic.check_generic(url, {"mode": "auto"})
    # JSON-LD claims InStock for the product, but the pinned Black variant is sold out.
    assert r.status == "out_of_stock" and r.status_text == "Selected variant sold out"
    assert r.detail["variant"] == "Black" and r.detail["variant_id"] == 40001
    assert r.detail["generic_status"] == "in_stock"
    assert r.price == "$89.99"


@respx.mock
async def test_shopify_pinned_variant_in_stock_label():
    url = SHOP + "?variant=40002"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx("shopify_product.html")))
    respx.get(SHOP_JS).mock(return_value=httpx.Response(200, json=fxj("shopify_product.js.json")))
    r = await generic.check_generic(url, {"mode": "auto"})
    assert r.status == "in_stock"
    assert r.available[0].key == "stock" and r.available[0].label == "In stock · White"
    assert r.detail["cart_url"] == "https://us.govee.com/cart/40002:1"


@respx.mock
async def test_shopify_all_sold_out():
    url = "https://nyxigame.com/products/wizard"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx("shopify_product.html")))
    respx.get(url + ".js").mock(return_value=httpx.Response(200, json=fxj("shopify_soldout.js.json")))
    r = await generic.check_generic(url, {"mode": "auto"})
    assert r.status == "out_of_stock" and r.status_text == "Sold out"
    assert r.title == "NYXI Wizard Controller"
    assert r.image_url == "https://nyxigame.com/cdn/shop/files/wizard.jpg"
    assert r.detail["variant"] is None  # "Default Title" is not a real variant name


@respx.mock
async def test_shopify_endpoint_failure_falls_back_to_generic():
    respx.get(SHOP).mock(return_value=httpx.Response(200, text=fx("shopify_product.html")))
    respx.get(SHOP_JS).mock(return_value=httpx.Response(404, text="nope"))
    r = await generic.check_generic(SHOP, {"mode": "auto"})
    assert r.status == "in_stock" and r.detail["adapter"] == "generic"
    assert r.detail["matched"].startswith("json-ld")


@respx.mock
async def test_platform_recipes_skipped_for_custom_rules():
    route = respx.get(SHOP_JS).mock(return_value=httpx.Response(200, json=fxj("shopify_product.js.json")))
    respx.get(SHOP).mock(return_value=httpx.Response(200, text=fx("shopify_product.html")))
    r = await generic.check_generic(SHOP, {"mode": "text", "out_of_stock_text": "Sold out"})
    assert not route.called and r.status == "in_stock"


# ------------------------------------------------------------------ SFCC

GS = "https://www.gamestop.com/consoles/nintendo-switch-2/20023800.html"
GS_API = "https://www.gamestop.com/on/demandware.store/Sites-gamestop-us-Site/default/Product-Variation"


@respx.mock
@pytest.mark.parametrize(("fixture", "status", "text"), [
    ("sfcc_variation_in.json", "in_stock", "In stock"),
    ("sfcc_variation_out.json", "out_of_stock", "Out of stock"),
])
async def test_sfcc_gamestop(fixture, status, text):
    respx.get(GS).mock(return_value=httpx.Response(200, text=fx("sfcc_gamestop.html")))
    api = respx.get(GS_API).mock(return_value=httpx.Response(200, json=fxj(fixture)))
    r = await generic.check_generic(GS, {"mode": "auto"})
    assert api.called
    q = api.calls[0].request.url.params
    assert q["pid"] == "20023800" and q["quantity"] == "1"
    assert api.calls[0].request.headers["x-requested-with"] == "XMLHttpRequest"
    assert r.status == status and r.status_text == text
    assert r.detail["adapter"] == "sfcc" and r.price == "$449.99"
    assert r.title == "Nintendo Switch 2 Console"


@respx.mock
async def test_sfcc_variation_params_pass_through():
    url = GS + "?dwvar_20023800_condition=Pre-Owned"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx("sfcc_gamestop.html")))
    api = respx.get(GS_API).mock(return_value=httpx.Response(200, json=fxj("sfcc_variation_in.json")))
    await generic.check_generic(url, {"mode": "auto"})
    assert api.calls[0].request.url.params["dwvar_20023800_condition"] == "Pre-Owned"


@respx.mock
async def test_sfcc_disney_preorder():
    url = "https://www.disneystore.com/stitch-plush-medium-15-465012345678.html"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx("sfcc_disney.html")))
    api = respx.get("https://www.disneystore.com/on/demandware.store/Sites-shopDisney-Site/en_US/Product-Variation").mock(
        return_value=httpx.Response(200, json=fxj("sfcc_variation_preorder.json")))
    r = await generic.check_generic(url, {"mode": "auto"})
    assert api.calls[0].request.url.params["pid"] == "465012345678"
    assert r.status == "in_stock" and r.status_text == "Pre-order" and r.price == "$29.99"


@respx.mock
async def test_sfcc_master_without_selection_is_inconclusive():
    respx.get(GS).mock(return_value=httpx.Response(200, text=fx("sfcc_gamestop.html")))
    respx.get(GS_API).mock(return_value=httpx.Response(200, json=fxj("sfcc_variation_master.json")))
    r = await generic.check_generic(GS, {"mode": "auto"})
    # Recipe can't tell -> generic page analysis (enabled "Add to Cart" button).
    assert r.detail["adapter"] == "generic" and r.status == "in_stock"


@respx.mock
async def test_sfcc_api_error_is_swallowed():
    respx.get(GS).mock(return_value=httpx.Response(200, text=fx("sfcc_gamestop.html")))
    respx.get(GS_API).mock(return_value=httpx.Response(500, text="err"))
    r = await generic.check_generic(GS, {"mode": "auto"})
    assert r.detail["adapter"] == "generic"


# ------------------------------------------------------------------ BigCommerce


@respx.mock
@pytest.mark.parametrize(("fixture", "status"), [("bigcommerce_in.html", "in_stock"), ("bigcommerce_out.html", "out_of_stock")])
async def test_bigcommerce(fixture, status):
    url = "https://www.robertscamera.com/sony-a7-iv-body"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx(fixture)))
    r = await generic.check_generic(url, {"mode": "auto"})
    assert r.status == status and r.detail["adapter"] == "bigcommerce"
    assert r.price == "$2,498.00" and r.detail["price_value"] == 2498
    # title/image come from the generic page analysis
    assert r.title == "Sony A7 IV Body" and r.image_url.endswith("a7iv.jpg")


# ------------------------------------------------------------------ WooCommerce

WOO = "https://consutronix.com/product/rtx-5080-gaming-pc/"
WOO_API = "https://consutronix.com/wp-json/wc/store/v1/products"


@respx.mock
async def test_woocommerce_store_api_in_stock():
    respx.get(WOO).mock(return_value=httpx.Response(200, text=fx("woo_product.html")))
    api = respx.get(WOO_API).mock(return_value=httpx.Response(200, json=fxj("woo_store_api.json")))
    r = await generic.check_generic(WOO, {"mode": "auto"})
    assert api.calls[0].request.url.params["slug"] == "rtx-5080-gaming-pc"
    # the API is authoritative over the (stale, cached) page saying out of stock
    assert r.status == "in_stock" and r.detail["adapter"] == "woocommerce"
    assert r.price == "$2,199.00" and r.image_url == "https://consutronix.com/wp-content/uploads/5080.jpg"


@respx.mock
async def test_woocommerce_dom_fallback_when_api_blocked():
    respx.get(WOO).mock(return_value=httpx.Response(200, text=fx("woo_product.html")))
    respx.get(WOO_API).mock(return_value=httpx.Response(401, json={"code": "rest_forbidden"}))
    respx.get("https://consutronix.com/wp-json/wc/store/products").mock(return_value=httpx.Response(404))
    r = await generic.check_generic(WOO, {"mode": "auto"})
    assert r.status == "out_of_stock" and r.detail["adapter"] == "woocommerce"
    assert "p.stock" in r.detail["matched"]


# ------------------------------------------------------------------ Magento / OpenCart


@respx.mock
@pytest.mark.parametrize(("fixture", "status"), [("magento_in.html", "in_stock"), ("magento_out.html", "out_of_stock")])
async def test_magento(fixture, status):
    url = "https://www.zotacstore.com/us/zt-b50900j-10p"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx(fixture)))
    r = await generic.check_generic(url, {"mode": "auto"})
    assert r.status == status and r.detail["adapter"] == "magento"
    assert r.price == "$1,999.99"


@respx.mock
@pytest.mark.parametrize(("fixture", "status", "text"), [
    ("opencart_in.html", "in_stock", "In stock"),
    ("opencart_out.html", "out_of_stock", "Out of stock"),
    ("opencart_custom_status.html", "in_stock", "In stock"),
])
async def test_opencart(fixture, status, text):
    url = "https://us-store.msi.com/index.php?route=product/product&product_id=42"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx(fixture)))
    r = await generic.check_generic(url, {"mode": "auto"})
    assert r.status == status and r.status_text == text and r.detail["adapter"] == "opencart"
    assert r.price == "$899.99" and r.title == "MSI Claw 8 AI+"


# ------------------------------------------------------------------ robustness / helpers


async def test_recipe_crash_returns_none(monkeypatch):
    async def boom(*a, **k):
        raise KeyError("bad data")

    monkeypatch.setitem(platforms._RECIPES, "magento", boom)
    assert await detect_and_check("https://z.example/p", fx("magento_in.html"), "https://z.example/p", {}) is None


def test_queue_result_shape():
    r = queue_result(title="Thing", fetched_via="http")
    assert r.status == "unknown" and r.status_text == QUEUE_STATUS_TEXT == "Waiting room active — drop may be live"
    assert r.detail["queue"] is True and r.detail["fetched_via"] == "http" and r.title == "Thing"
    assert r.available == []


def test_next_data_find():
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"apollo":{"P:1":{"productCode":"75313","availabilityStatus":"E_AVAILABLE"},'
            '"P:2":{"productCode":"10300"}}}}</script>')
    hits = next_data_find(html, lambda d: d.get("productCode") == "75313")
    assert hits == [{"productCode": "75313", "availabilityStatus": "E_AVAILABLE"}]
    assert next_data_find("<html></html>", lambda d: True) == []
    assert next_data_find(html, lambda d: d["missing"]) == []  # predicate errors are ignored


# ------------------------------------------------------------------ waiting rooms


@respx.mock
@pytest.mark.parametrize("fixture", ["queue_it.html", "imperva_waiting_room.html"])
async def test_check_generic_waiting_room(fixture):
    url = "https://www.gamestop.com/consoles/ps5/20012345.html"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx(fixture)))
    r = await generic.check_generic(url, {"mode": "auto"})
    assert r.status == "unknown" and r.status_text == QUEUE_STATUS_TEXT and r.detail["queue"] is True


@respx.mock
async def test_run_check_queue_redirect(monkeypatch):
    url = "https://shop.example/products/drop"
    respx.get(url).mock(return_value=httpx.Response(302, headers={"Location": "https://shop.example/throttle/queue?x=1"}))
    respx.get("https://shop.example/throttle/queue?x=1").mock(return_value=httpx.Response(200, text="<html>Please wait</html>"))
    r = await checkers.run_check("generic", url, {"mode": "auto"}, None)
    assert r.status == "unknown" and r.detail["queue"] is True


# ------------------------------------------------------------------ preview


@respx.mock
async def test_preview_uses_platform_recipe_and_reports_retailer():
    url = "https://us.govee.com/products/rgbic-floor-lamp?variant=40001"
    respx.get(url).mock(return_value=httpx.Response(200, text=fx("shopify_product.html")))
    respx.get(SHOP_JS).mock(return_value=httpx.Response(200, json=fxj("shopify_product.js.json")))
    p = await checkers.preview_url(url)
    assert p["status"] == "out_of_stock" and p["price"] == "$89.99" and p["name"] == "Govee RGBIC Floor Lamp"
    assert p["retailer"]["key"] == "govee" and p["retailer"]["name"] == "Govee"
    assert p["is_apple"] is False


async def test_preview_uses_site_adapter(monkeypatch):
    seen = []

    async def fake_run_adapter(url, gc, rc):
        seen.append(url)
        return CheckResult(status="in_stock", status_text="In stock", title="PS5 Pro", price="$699.99",
                           image_url="https://img.example/ps5.jpg")

    async def no_generic(url, cfg):
        raise AssertionError("generic must not run when the adapter answered")

    monkeypatch.setattr(retailers_pkg, "run_adapter", fake_run_adapter)
    monkeypatch.setattr(generic, "check_generic", no_generic)
    url = "https://www.bestbuy.com/site/sony-playstation-5-pro/6614313.p?skuId=6614313"
    p = await checkers.preview_url(url)
    assert seen == [url]
    assert p["name"] == "PS5 Pro" and p["price"] == "$699.99" and p["status"] == "in_stock"
    assert p["image_url"] == "https://img.example/ps5.jpg"
    assert p["retailer"]["key"] == "bestbuy" and p["retailer"]["pickup"] is True


async def test_preview_adapter_fetch_error(monkeypatch):
    from app.checkers.fetcher import FetchError

    async def fail(url, gc, rc):
        raise FetchError("Timed out fetching www.bestbuy.com")

    monkeypatch.setattr(retailers_pkg, "run_adapter", fail)
    p = await checkers.preview_url("https://www.bestbuy.com/site/x/6614313.p")
    assert p["status"] == "error" and "Timed out" in p["error"] and p["retailer"]["key"] == "bestbuy"
