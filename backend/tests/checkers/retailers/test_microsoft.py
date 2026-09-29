"""Microsoft Store / Xbox adapter (displaycatalog + inventory)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.checkers.retailers import microsoft as ms
from app.checkers.retailers import run_adapter
from app.checkers.retailers.base import AdapterContext, RetailerConfig
from app.checkers.retailers.registry import match_retailer

FX = Path(__file__).resolve().parents[1] / "fixtures" / "retailers" / "electronics"
CATALOG = "https://displaycatalog.mp.microsoft.com/v7.0/products"
INV = "https://inv.mp.microsoft.com/v2.0/inventory/US/8WJ714N3RBTL/0002/9X3DG4F5LCRW"
MS_URL = "https://www.microsoft.com/en-us/d/xbox-series-x-1tb-digital-edition-white/8wj714n3rbtl"


def fxj(name: str):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def ctx_for(url: str) -> AdapterContext:
    return AdapterContext(retailer=match_retailer(url), generic_config={}, retailer_config=RetailerConfig())


@pytest.mark.parametrize("url,ids", [
    (MS_URL, ("8WJ714N3RBTL", None)),
    ("https://www.microsoft.com/en-us/d/surface-pro-11th-edition/8n3zd6r5xqsr/000z", ("8N3ZD6R5XQSR", "000Z")),
    ("https://www.xbox.com/en-US/configure/8WJ714N3RBTL", ("8WJ714N3RBTL", None)),
    ("https://www.xbox.com/en-US/games/store/halo-infinite/9PP5G1F0C2B6/0010", ("9PP5G1F0C2B6", "0010")),
    ("https://www.microsoft.com/en-us/store/b/xbox", (None, None)),
])
def test_parse_ids(url, ids):
    assert ms.parse_ids(url) == ids


def test_market_from_locale():
    assert ms.market_of("https://www.xbox.com/en-GB/configure/8WJ714N3RBTL") == ("GB", "en-GB")
    assert ms.market_of("https://www.microsoft.com/store/productId/9NBLGGH4R32N") == ("US", "en-US")


@respx.mock
async def test_console_in_stock_picks_purchasable_sku():
    cat = respx.get(CATALOG, params={"bigIds": "8WJ714N3RBTL", "market": "US", "languages": "en-US"}).mock(
        return_value=httpx.Response(200, json=fxj("ms_displaycatalog_console.json")))
    inv = respx.get(INV).mock(return_value=httpx.Response(200, json=fxj("ms_inventory_instock.json")))
    res = await run_adapter(MS_URL, {}, {})
    assert cat.called and inv.called
    assert res.status == "in_stock" and res.detail["sku_id"] == "0002"
    assert res.detail["price_value"] == 649.99 and res.price == "$649.99"
    assert res.title == "Xbox Series X – 1TB Digital Edition (White)"
    assert res.image_url == "https://store-images.s-microsoft.com/image/apps.2.poster"
    assert res.detail["seller"] == "Microsoft" and res.detail["retailer"] == "microsoft"


@respx.mock
async def test_console_out_of_stock_on_xbox_configure():
    url = "https://www.xbox.com/en-US/configure/8WJ714N3RBTL"
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json=fxj("ms_displaycatalog_console.json")))
    respx.get(INV).mock(return_value=httpx.Response(200, json=fxj("ms_inventory_oos.json")))
    res = await ms.check(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.detail["retailer"] == "xbox"


@respx.mock
async def test_pinned_sku_without_purchase_action_is_out():
    url = MS_URL + "/0001"
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json=fxj("ms_displaycatalog_console.json")))
    res = await ms.check(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.status_text == "Not available to buy"
    assert res.detail["price_value"] == 599.99


@respx.mock
async def test_inventory_failure_is_unknown():
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json=fxj("ms_displaycatalog_console.json")))
    respx.get(INV).mock(return_value=httpx.Response(500))
    res = await ms.check(MS_URL, ctx_for(MS_URL))
    assert res.status == "unknown" and "inventory lookup failed" in res.detail["matched"]


@respx.mock
async def test_digital_game_skips_inventory():
    url = "https://www.xbox.com/en-US/games/store/halo-infinite/9PP5G1F0C2B6/0010"
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json=fxj("ms_displaycatalog_game.json")))
    res = await ms.check(url, ctx_for(url))
    assert res.status == "in_stock" and res.status_text == "Available to buy"
    assert res.detail["price_value"] == 59.99


@respx.mock
async def test_catalog_error_falls_through():
    respx.get(CATALOG).mock(return_value=httpx.Response(503))
    assert await ms.check(MS_URL, ctx_for(MS_URL)) is None
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"Products": []}))
    assert await ms.check(MS_URL, ctx_for(MS_URL)) is None
