"""Kroger adapter: official API (client credentials), nearest store stock level."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.checkers.fetcher import FetchError
from app.checkers.retailers import AdapterContext, RetailerConfig, kroger, retailer_by_key

FIX = Path(__file__).parent.parent / "fixtures" / "retailers" / "bigbox"
URL = "https://www.kroger.com/p/nintendo-switch-2-mario-kart-world-bundle/0004549600051"
API = "https://api.kroger.com/v1"


def fj(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def ctx(**rc) -> AdapterContext:
    return AdapterContext(retailer=retailer_by_key("kroger"), retailer_config=RetailerConfig.from_dict(rc))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KROGER_CLIENT_ID", "cid")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "secret")
    kroger._token.update(value=None, exp=0.0, client=None)
    kroger._locations.clear()
    yield
    kroger._token.update(value=None, exp=0.0, client=None)
    kroger._locations.clear()


def mock_api(product=None):
    token = respx.post(f"{API}/connect/oauth2/token").mock(
        return_value=httpx.Response(200, json=fj("kroger_token.json")))
    locs = respx.get(url__startswith=f"{API}/locations").mock(
        return_value=httpx.Response(200, json=fj("kroger_locations.json")))
    prod = respx.get(url__startswith=f"{API}/products/0004549600051").mock(
        return_value=httpx.Response(200, json=product or fj("kroger_product.json")))
    return token, locs, prod


def test_upc():
    assert kroger.upc_from_url(URL) == "0004549600051"
    assert kroger.upc_from_url("https://www.ralphs.com/p/x/0001111041700?fulfillment=PICKUP") == "0001111041700"
    assert kroger.upc_from_url("https://www.kroger.com/search?query=switch") is None


def _page_path(monkeypatch) -> list:
    calls: list = []

    async def page(url, upc, c):
        calls.append(upc)
        return "page"

    monkeypatch.setattr(kroger, "check_page", page)
    return calls


async def test_without_credentials_reads_the_page(monkeypatch):
    monkeypatch.delenv("KROGER_CLIENT_ID")
    calls = _page_path(monkeypatch)
    assert await kroger.check(URL, ctx(fulfillment="pickup", zip="45209")) == "page" and calls


async def test_without_location_delivery_reads_the_page_pickup_errors(monkeypatch):
    calls = _page_path(monkeypatch)
    assert await kroger.check(URL, ctx()) == "page" and calls
    res = await kroger.check(URL, ctx(fulfillment="pickup"))
    assert res.status == "error" and res.status_text == "Set a ZIP code for pickup"


@respx.mock
async def test_pickup_at_nearest_store_and_token_cache():
    token, locs, prod = mock_api()
    res = await kroger.check(URL, ctx(fulfillment="pickup", zip="45209", radius_miles=10))
    req = token.calls.last.request
    assert req.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in req.content and b"scope=product.compact" in req.content
    lq = parse_qs(urlsplit(str(locs.calls.last.request.url)).query)
    assert lq["filter.zipCode.near"] == ["45209"] and lq["filter.radiusInMiles"] == ["10"]
    pq = parse_qs(urlsplit(str(prod.calls.last.request.url)).query)
    assert pq["filter.locationId"] == ["01400943"]
    assert prod.calls.last.request.headers["authorization"] == "Bearer eyJhbGciOiJSUzI1NiJ9.test-token"
    assert res.status == "in_stock"
    assert [(a.key, a.label) for a in res.available] == [
        ("pickup:01400943", "Pickup · Kroger - Hyde Park Plaza · low stock")]
    assert res.title == "Nintendo Switch 2 Mario Kart World Bundle"
    assert res.price == "$499.99" and res.detail["price_value"] == 499.99
    assert res.image_url.endswith("/large/front/0004549600051")
    assert res.detail["stock_level"] == "LOW"
    # second check reuses the cached token and store lookup
    await kroger.check(URL, ctx(fulfillment="pickup", zip="45209", radius_miles=10))
    assert token.call_count == 1 and locs.call_count == 1 and prod.call_count == 2


@respx.mock
async def test_temporarily_out_of_stock():
    body = fj("kroger_product.json")
    body["data"]["items"][0]["inventory"]["stockLevel"] = "TEMPORARILY_OUT_OF_STOCK"
    mock_api(body)
    res = await kroger.check(URL, ctx(fulfillment="any", store_id="01400376"))
    assert res.status == "out_of_stock"
    assert res.status_text == "Temporarily out of stock at store #01400376"


@respx.mock
async def test_delivery_uses_fulfillment_flags():
    body = fj("kroger_product.json")
    body["data"]["items"][0]["inventory"]["stockLevel"] = "HIGH"
    mock_api(body)
    res = await kroger.check(URL, ctx(fulfillment="delivery", zip="45209"))
    assert [a.key for a in res.available] == ["stock"] and res.status_text == "In stock for delivery"
    body["data"]["items"][0]["fulfillment"].update(delivery=False, shipToHome=False)
    mock_api(body)
    res = await kroger.check(URL, ctx(fulfillment="delivery", zip="45209"))
    assert res.status == "out_of_stock"


@respx.mock
async def test_expired_token_is_refreshed_once():
    token = respx.post(f"{API}/connect/oauth2/token").mock(
        return_value=httpx.Response(200, json=fj("kroger_token.json")))
    kroger._token.update(value="old", exp=1e18, client="cid")
    calls = []

    def prod_side(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(401 if request.headers["authorization"] == "Bearer old" else 200,
                              json=fj("kroger_product.json"))

    respx.get(url__startswith=f"{API}/products/").mock(side_effect=prod_side)
    res = await kroger.check(URL, ctx(fulfillment="pickup", store_id="01400943"))
    assert calls == ["Bearer old", "Bearer eyJhbGciOiJSUzI1NiJ9.test-token"]
    assert res.status == "in_stock" and token.call_count == 1


@respx.mock
async def test_bad_credentials_raise():
    respx.post(f"{API}/connect/oauth2/token").mock(return_value=httpx.Response(401, json={"error": "invalid"}))
    with pytest.raises(FetchError, match="rejected the client credentials"):
        await kroger.check(URL, ctx(fulfillment="pickup", zip="45209"))


@respx.mock
async def test_fulfillment_flags_are_case_insensitive():
    body = fj("kroger_product.json")
    body["data"]["items"][0]["inventory"]["stockLevel"] = "HIGH"
    # the API spells it "shipToHome" in some responses and "shiptohome" in others
    body["data"]["items"][0]["fulfillment"] = {"curbside": False, "delivery": False, "instore": True,
                                               "shiptohome": True}
    mock_api(body)
    res = await kroger.check(URL, ctx(fulfillment="delivery", zip="45209"))
    assert res.status == "in_stock" and [a.key for a in res.available] == ["stock"]
    body["data"]["items"][0]["fulfillment"] = {"Curbside": False, "Delivery": False, "ShipToHome": False}
    mock_api(body)
    res = await kroger.check(URL, ctx(fulfillment="any", zip="45209"))
    assert res.status == "out_of_stock"
