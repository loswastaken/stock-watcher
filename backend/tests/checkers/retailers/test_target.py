"""Target adapter: Redsky pdp_client / pdp_fulfillment / nearby_stores (respx-mocked)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.checkers.retailers import AdapterContext, RetailerConfig, retailer_by_key, run_adapter, target

FIX = Path(__file__).parent.parent / "fixtures" / "retailers" / "bigbox"
URL = "https://www.target.com/p/pokemon-scarlet-violet-booster-bundle/-/A-89444444"
PDP = "https://www.target.com/p/-/A-89444444"
REDSKY = "https://redsky.target.com/redsky_aggregations/v1/web/"
SCRAPED_KEY = "0123456789abcdef0123456789abcdef01234567"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fj(name: str) -> dict:
    return json.loads(fx(name))


def ctx(**rc) -> AdapterContext:
    return AdapterContext(retailer=retailer_by_key("target"), retailer_config=RetailerConfig.from_dict(rc))


def q(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


@pytest.fixture(autouse=True)
def _reset():
    target._key_cache.update(key=None, exp=0.0)
    target._stores_cache.clear()
    yield
    target._key_cache.update(key=None, exp=0.0)
    target._stores_cache.clear()


def mock_common(client_json: str = "target_pdp_client.json", pdp_status: int = 200):
    page = fx("target_pdp.html") if pdp_status == 200 else "<html>Access denied</html>"
    respx.get(PDP).mock(return_value=httpx.Response(pdp_status, text=page))
    return respx.get(url__startswith=REDSKY + "pdp_client_v1").mock(
        return_value=httpx.Response(200, json=fj(client_json)))


def test_tcin_parsing():
    assert target.tcin_from_url(URL) == "89444444"
    assert target.tcin_from_url("https://www.target.com/p/x/-/A-12345678?preselect=87654321#lnk=sametab") == "87654321"
    assert target.tcin_from_url("https://www.target.com/c/toys/-/N-5xtb0") is None


@respx.mock
async def test_delivery_in_stock_uses_scraped_key():
    client = mock_common()
    ful = respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment.json")))
    res = await target.check(URL, ctx())
    assert res.status == "in_stock" and res.status_text == "In stock"
    assert [a.key for a in res.available] == ["stock"]
    assert res.title == "Pokémon Trading Card Game: Scarlet & Violet Booster Bundle"
    assert res.price == "$26.99" and res.detail["price_value"] == 26.99
    assert res.detail["seller"] == "Target" and res.detail["third_party"] is False
    assert res.detail["retailer"] == "target" and "cart_url" not in res.detail
    assert q(client.calls.last.request)["key"] == SCRAPED_KEY
    assert q(ful.calls.last.request)["tcin"] == "89444444"
    assert client.calls.last.request.headers["origin"] == "https://www.target.com"


@respx.mock
async def test_fallback_key_when_pdp_unavailable():
    client = mock_common(pdp_status=404)
    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment_oos.json")))
    res = await target.check(URL, ctx())
    assert q(client.calls.last.request)["key"] == target.FALLBACK_KEY
    assert res.status == "out_of_stock" and res.status_text == "Out of stock"


@respx.mock
async def test_rejected_key_is_rescraped_once():
    target._key_cache.update(key="f" * 40, exp=1e18)  # stale cached key
    respx.get(PDP).mock(return_value=httpx.Response(200, text=fx("target_pdp.html")))

    def client_side(request):
        return httpx.Response(401 if q(request)["key"] == "f" * 40 else 200, json=fj("target_pdp_client.json"))

    respx.get(url__startswith=REDSKY + "pdp_client_v1").mock(side_effect=client_side)
    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment.json")))
    res = await target.check(URL, ctx())
    assert res.status == "in_stock" and target._key_cache["key"] == SCRAPED_KEY


@respx.mock
async def test_pickup_nearby_stores_within_radius():
    mock_common()
    stores = respx.get(url__startswith=REDSKY + "nearby_stores_v1").mock(
        return_value=httpx.Response(200, json=fj("target_nearby_stores.json")))

    def ful_side(request):
        sid = q(request).get("store_id")
        return httpx.Response(200, json=fj("target_fulfillment_store3.json" if sid == "3456"
                                           else "target_fulfillment.json"))

    ful = respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(side_effect=ful_side)
    res = await target.check(URL, ctx(fulfillment="pickup", zip="60302", radius_miles=25))
    assert q(stores.calls.last.request)["place"] == "60302"
    assert q(stores.calls.last.request)["within"] == "25"
    # one call for the nearest store (covers 1234 + 2345), one for 3456; 9999 is outside the radius
    assert [q(c.request)["store_id"] for c in ful.calls] == ["1234", "3456"]
    assert q(ful.calls[0].request)["zip"] == "60302" and q(ful.calls[0].request)["state"] == "IL"
    assert res.status == "in_stock"
    assert [(a.key, a.label) for a in res.available] == [
        ("pickup:1234", "Pickup · Oak Park (3.2 mi)"),
        ("pickup:3456", "Pickup · Chicago Lincoln Park (9.1 mi)"),
    ]
    assert res.status_text == "Pickup at 2 stores"
    by_id = {s["id"]: s for s in res.detail["pickup_stores"]}
    assert by_id["2345"]["status"] == "UNAVAILABLE" and "9999" not in by_id


@respx.mock
async def test_pickup_pinned_store_single_label():
    mock_common()
    ful = respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment.json")))
    res = await target.check(URL, ctx(fulfillment="pickup", store_id="1234"))
    assert q(ful.calls.last.request)["required_store_id"] == "1234"
    assert [a.key for a in res.available] == ["pickup:1234"]
    assert res.status_text == "Pickup · Oak Park"


@respx.mock
async def test_any_mode_out_online_but_pickup_available():
    mock_common()
    respx.get(url__startswith=REDSKY + "nearby_stores_v1").mock(
        return_value=httpx.Response(200, json=fj("target_nearby_stores.json")))

    def ful_side(request):
        sid = q(request).get("store_id")
        if sid == "3456":
            return httpx.Response(200, json=fj("target_fulfillment_store3.json"))
        return httpx.Response(200, json=fj("target_fulfillment_oos.json"))

    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(side_effect=ful_side)
    res = await target.check(URL, ctx(fulfillment="any", zip="60302"))
    assert res.status == "in_stock"
    assert [a.key for a in res.available] == ["pickup:3456"]
    assert res.status_text == "Pickup · Chicago Lincoln Park (9.1 mi)"


@respx.mock
async def test_pickup_none_available_is_out():
    mock_common()
    respx.get(url__startswith=REDSKY + "nearby_stores_v1").mock(
        return_value=httpx.Response(200, json=fj("target_nearby_stores.json")))
    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment_oos.json")))
    res = await target.check(URL, ctx(fulfillment="pickup", zip="60302", radius_miles=10))
    assert res.status == "out_of_stock"
    assert res.status_text == "No pickup within 10 mi of 60302"


async def test_pickup_without_zip_is_error():
    res = await target.check(URL, ctx(fulfillment="pickup"))
    assert res.status == "error" and res.status_text == "Set a ZIP code for pickup"


@respx.mock
async def test_target_plus_seller_blocked_when_official_only():
    mock_common("target_pdp_client_marketplace.json")
    ful = respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment.json")))
    res = await target.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["seller"] == "CardCollectorz" and res.detail["third_party"] is True
    assert not ful.called
    res2 = await target.check(URL, ctx(official_only=False))
    assert res2.status == "in_stock"


@respx.mock
async def test_preorder_and_run_adapter_dispatch():
    mock_common()
    body = fj("target_fulfillment.json")
    body["data"]["product"]["fulfillment"]["shipping_options"]["availability_status"] = "PRE_ORDER_SELLABLE"
    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(return_value=httpx.Response(200, json=body))
    res = await run_adapter(URL, None, None)
    assert res.status == "in_stock" and res.status_text == "Pre-order"
    assert res.detail["adapter"] == "target"


# ------------------------------------------------------------------ review fixes


@respx.mock
async def test_empty_nearby_stores_answer_is_not_cached_for_hours():
    mock_common()
    stores = respx.get(url__startswith=REDSKY + "nearby_stores_v1").mock(
        return_value=httpx.Response(200, json={"data": None}))
    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment.json")))
    res = await target.check(URL, ctx(fulfillment="pickup", zip="60302"))
    assert res.status == "unknown"  # no answer about stores is not "no pickup"
    stores.mock(return_value=httpx.Response(200, json=fj("target_nearby_stores.json")))
    res = await target.check(URL, ctx(fulfillment="pickup", zip="60302"))
    assert res.status == "in_stock" and stores.call_count == 2
    # a genuinely empty list is cached briefly only
    target._stores_cache.clear()
    stores.mock(return_value=httpx.Response(200, json={"data": {"nearby_stores": {"stores": []}}}))
    res = await target.check(URL, ctx(fulfillment="pickup", zip="60302"))
    assert res.status == "out_of_stock"
    exp, cached = target._stores_cache[("60302", 25)]
    assert cached == [] and exp - target.time.monotonic() <= target.STORES_EMPTY_TTL <= 300


@respx.mock
async def test_nearby_stores_failure_keeps_delivery_for_any():
    mock_common()
    respx.get(url__startswith=REDSKY + "nearby_stores_v1").mock(return_value=httpx.Response(500))
    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment.json")))
    res = await target.check(URL, ctx(fulfillment="any", zip="60302"))
    assert res.status == "in_stock" and [a.key for a in res.available] == ["stock"]
    assert not target._stores_cache


@respx.mock
async def test_warm_any_check_makes_at_most_four_requests():
    mock_common()
    five = {"data": {"nearby_stores": {"stores": [
        {"store_id": str(i), "location_name": f"S{i}", "distance": i, "mailing_address": {"region": "IL"}}
        for i in range(1, 6)]}}}
    respx.get(url__startswith=REDSKY + "nearby_stores_v1").mock(return_value=httpx.Response(200, json=five))

    def ful_side(request):  # each call only reports its own store
        sid = q(request).get("store_id")
        return httpx.Response(200, json={"data": {"product": {"fulfillment": {
            "shipping_options": {"availability_status": "OUT_OF_STOCK"},
            "store_options": [{"location_id": sid, "order_pickup": {"availability_status": "OUT_OF_STOCK"}}]}}}})

    ful = respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(side_effect=ful_side)
    await target.check(URL, ctx(fulfillment="any", zip="60601"))
    before = len(respx.calls)
    res = await target.check(URL, ctx(fulfillment="any", zip="60601"))
    assert len(respx.calls) - before <= 4
    assert len(res.detail["pickup_stores"]) == target.MAX_PICKUP_STORES == 3
    assert [q(c.request)["store_id"] for c in ful.calls][-3:] == ["1", "2", "3"]


@respx.mock
async def test_marketplace_vendor_when_is_marketplace_missing():
    body = fj("target_pdp_client.json")
    item = body["data"]["product"]["item"]
    del item["fulfillment"]["is_marketplace"]
    item["product_vendors"] = [{"id": "77", "vendor_name": "GameStopDeals LLC", "relationship_type": "MARKETPLACE"}]
    respx.get(PDP).mock(return_value=httpx.Response(200, text=fx("target_pdp.html")))
    respx.get(url__startswith=REDSKY + "pdp_client_v1").mock(return_value=httpx.Response(200, json=body))
    respx.get(url__startswith=REDSKY + "pdp_fulfillment_v1").mock(
        return_value=httpx.Response(200, json=fj("target_fulfillment.json")))
    res = await target.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["seller"] == "GameStopDeals LLC" and res.detail["third_party"] is True


def test_seller_of_plain_supplier_vendor_is_not_third_party():
    # first-party items list their supplier as vendor_name; that alone is not a marketplace seller
    assert target.seller_of({"fulfillment": {}, "product_vendors": [{"vendor_name": "POKEMON USA INC"}]}) == (None, None)
    assert target.seller_of({"fulfillment": {"is_marketplace": False}}) == ("Target", False)
