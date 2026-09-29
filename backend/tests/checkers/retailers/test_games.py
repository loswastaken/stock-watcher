"""Games / toys adapters (app.checkers.retailers.games) against hand-written fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.checkers import fetcher
from app.checkers.retailers import games
from app.checkers.retailers import run_adapter
from app.checkers.retailers.base import AdapterContext, RetailerConfig
from app.checkers.retailers.registry import match_retailer

FX = Path(__file__).resolve().parents[1] / "fixtures" / "retailers" / "games"


def fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def fxj(name: str):
    return json.loads(fx(name))


def ctx_for(url: str, **rc) -> AdapterContext:
    return AdapterContext(retailer=match_retailer(url), generic_config={}, retailer_config=RetailerConfig.from_dict(rc))


@pytest.fixture
def page(monkeypatch):
    calls: list[str] = []
    state: dict = {}

    def set_page(html: str, final: str | None = None, queued: bool = False):
        state.update(html=html, final=final, queued=queued)
        return calls

    async def fake(url, render_js=False, *, needs=None):
        calls.append(url)
        res = fetcher.FetchResult(url=state["final"] or url, status=200, text=state["html"])
        if state["queued"]:
            res.queued = True
        return res

    monkeypatch.setattr(fetcher, "fetch_html", fake)
    return set_page


# ============================================================ LEGO

LEGO_URL = "https://www.lego.com/en-us/product/millennium-falcon-75192"


def test_lego_code_and_status_map():
    assert games.lego_code(LEGO_URL) == "75192"
    assert games.lego_code("https://www.lego.com/en-us/product/christmas-2023-40642?icmp=x") == "40642"
    assert games.lego_status("E_AVAILABLE") == ("in", "In stock")
    assert games.lego_status("A_PRE_ORDER_FOR_DATE") == ("in", "Pre-order")
    assert games.lego_status("F_BACKORDER_FOR_DATE") == ("in", "Backorder")
    assert games.lego_status("C_COMING_SOON") == ("out", "Coming soon")
    assert games.lego_status("K_OUT_OF_STOCK") == ("out", "Out of stock")
    assert games.lego_status("R_RETIRED") == ("out", "Retired")
    assert games.lego_status("Z_SOMETHING_NEW") == (None, None)


async def test_lego_available_from_apollo(page):
    page(fx("lego_available.html"))
    res = await run_adapter(LEGO_URL, {}, {})
    assert res.status == "in_stock" and res.status_text == "In stock"
    assert res.detail["availability_status"] == "E_AVAILABLE" and res.detail["price_value"] == 849.99
    assert res.title == "Millennium Falcon™" and res.detail["seller"] == "LEGO"
    assert res.detail["adapter"] == "lego"


@pytest.mark.parametrize("status,can_add,expect,text", [
    ("R_RETIRED", "false", "out_of_stock", "Retired"),
    ("F_BACKORDER_FOR_DATE", "true", "in_stock", "Backorder"),
    ("E_AVAILABLE", "false", "out_of_stock", "Temporarily unavailable"),
])
async def test_lego_status_variants(page, status, can_add, expect, text):
    html = fx("lego_available.html").replace('"availabilityStatus":"E_AVAILABLE"', f'"availabilityStatus":"{status}"')
    html = html.replace('"canAddToBag":true', f'"canAddToBag":{can_add}')
    page(html)
    res = await games.lego(LEGO_URL, ctx_for(LEGO_URL))
    assert (res.status, res.status_text) == (expect, text)


async def test_lego_without_state_uses_generic(page):
    page("<html><body><main><h1>Set</h1><button>Add to Bag</button></main></body></html>")
    res = await games.lego(LEGO_URL, ctx_for(LEGO_URL))
    assert res.status == "in_stock" and res.detail["adapter"] == "generic"


# ============================================================ Nintendo

NIN_URL = "https://www.nintendo.com/us/store/products/nintendo-switch-2-mario-kart-world-bundle-123456/"


async def test_nintendo_store_product_out_of_stock(page):
    page(fx("nintendo_oos.html"))
    res = await games.nintendo(NIN_URL, ctx_for(NIN_URL))
    assert res.status == "out_of_stock" and "isSalableQty" in res.detail["matched"]
    assert res.detail["price_value"] == 499.99 and res.detail["sku"] == "7100071"


async def test_nintendo_matches_product_by_url_key(page):
    page(fx("nintendo_oos.html"))
    url = "https://www.nintendo.com/us/store/products/joy-con-2-pair-120000/"
    res = await games.nintendo(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["sku"] == "7100099"


async def test_nintendo_request_invitation_is_invite_only(page):
    url = "https://www.nintendo.com/us/store/products/nintendo-switch-2-system-123455/"
    page(fx("nintendo_invite.html"))
    res = await games.nintendo(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.status_text == "Invite only"


# ============================================================ POP MART


async def test_popmart_all_skus_sold_out(page):
    url = "https://www.popmart.com/us/products/3571/THE-MONSTERS-Big-into-Energy-Series"
    page(fx("popmart_soldout.html"))
    res = await games.popmart(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.detail["sku_count"] == 2
    assert res.detail["price_value"] == 27.99 and res.detail["seller"] == "POP MART"


async def test_popmart_one_sku_in_stock(page):
    url = "https://www.popmart.com/us/products/3571/THE-MONSTERS-Big-into-Energy-Series"
    page(fx("popmart_soldout.html").replace('"onlineStock":0,"onlineLockStock":0}},\n          {"id":5002',
                                            '"onlineStock":14,"onlineLockStock":0}},\n          {"id":5002'))
    res = await games.popmart(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["stock_qty"] == 14


async def test_popmart_pinned_sku(page):
    url = "https://www.popmart.com/us/products/3571/THE-MONSTERS?skuId=5002"
    page(fx("popmart_soldout.html").replace('"onlineStock":0,"onlineLockStock":0}},\n          {"id":5002',
                                            '"onlineStock":14,"onlineLockStock":0}},\n          {"id":5002'))
    res = await games.popmart(url, ctx_for(url))
    assert res.status == "out_of_stock"


# ============================================================ Pokemon Center

PC_URL = "https://www.pokemoncenter.com/product/100-10185-101/pokemon-tcg-prismatic-evolutions-elite-trainer-box"


async def test_pokemoncenter_preloaded_state_preorder(page):
    page(fx("pokemoncenter_preorder.html"))
    res = await games.pokemoncenter(PC_URL, ctx_for(PC_URL))
    assert res.status == "in_stock" and res.status_text == "Pre-order"
    assert res.detail["price_value"] == 49.99 and res.detail["product_code"] == "100-10185-101"


async def test_pokemoncenter_matches_code_not_first_entity(page):
    url = "https://www.pokemoncenter.com/product/100-10020-101/pikachu-plush"
    page(fx("pokemoncenter_preorder.html"))
    res = await games.pokemoncenter(url, ctx_for(url))
    assert res.status == "out_of_stock"


async def test_pokemoncenter_waiting_room(page):
    page("<html><head><title>Pokémon Center</title></head><body><h2>You are now in line.</h2>"
         "<p>Thank you for your patience.</p></body></html>")
    res = await games.pokemoncenter(PC_URL, ctx_for(PC_URL))
    assert res.status == "unknown" and res.detail["queue"] is True


async def test_fetcher_queued_flag_is_honoured(page):
    page("<html><body>busy</body></html>", queued=True)
    res = await games.pokemoncenter(PC_URL, ctx_for(PC_URL))
    assert res.status_text == "Waiting room active — drop may be live" and res.detail["queue"] is True


# ============================================================ PlayStation Direct

PSD_URL = "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console.3006222"
PSD_API = ("https://api.direct.playstation.com/commercewebservices/ps-direct-us/users/anonymous/products/productList")


def test_psdirect_code():
    assert games.psdirect_code(PSD_URL) == "3006222"
    assert games.psdirect_code("https://direct.playstation.com/en-us/accessories/accessory/dualsense-edge.3005727/") == "3005727"
    assert games.psdirect_code("https://direct.playstation.com/en-us/ps5") is None


@respx.mock
async def test_psdirect_api_in_stock():
    route = respx.get(PSD_API, params={"fields": "BASIC", "productCodes": "3006222"}).mock(
        return_value=httpx.Response(200, json=fxj("psdirect_instock.json")))
    res = await games.psdirect(PSD_URL, ctx_for(PSD_URL))
    assert route.called
    assert res.status == "in_stock" and res.detail["price_value"] == 749.99
    assert res.title == "PlayStation®5 Pro Console" and res.detail["seller"] == "PlayStation Direct"


@respx.mock
async def test_psdirect_api_out_of_stock():
    respx.get(PSD_API).mock(return_value=httpx.Response(200, json=fxj("psdirect_oos.json")))
    res = await games.psdirect(PSD_URL, ctx_for(PSD_URL))
    assert res.status == "out_of_stock" and res.detail["stock_level"] == "outOfStock"


@respx.mock
async def test_psdirect_queue_redirect():
    respx.get(PSD_API).mock(return_value=httpx.Response(
        302, headers={"Location": "https://direct-queue.playstation.com/?c=sonyied&e=psdirectprodku1"}))
    respx.get("https://direct-queue.playstation.com/").mock(
        return_value=httpx.Response(200, text="<html><title>PlayStation Direct Queue</title></html>"))
    res = await games.psdirect(PSD_URL, ctx_for(PSD_URL))
    assert res.status == "unknown" and res.detail["queue"] is True


@respx.mock
async def test_psdirect_api_blocked_falls_back_to_page_queue(page):
    respx.get(PSD_API).mock(return_value=httpx.Response(403, text="Access Denied"))
    page("<html><body>wait</body></html>", final="https://direct-queue.playstation.com/?c=sonyied&e=ps5")
    res = await games.psdirect(PSD_URL, ctx_for(PSD_URL))
    assert res.detail["queue"] is True


# ============================================================ GameStop

GS_URL = "https://www.gamestop.com/consoles-hardware/playstation-5/consoles/products/sony-playstation-5-pro-console/417934.html"


async def test_gamestop_gtmdata_new_not_available(page):
    page(fx("gamestop_pdp.html"))
    res = await games.gamestop(GS_URL, ctx_for(GS_URL))
    assert res.status == "out_of_stock" and res.detail["condition"] == "New"
    assert res.detail["price_value"] == 749.99


async def test_gamestop_pre_owned_condition_from_url(page):
    page(fx("gamestop_pdp.html"))
    url = GS_URL + "?condition=Pre-Owned"
    res = await games.gamestop(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["price_value"] == 679.99


async def test_gamestop_inconclusive_falls_through_to_sfcc(page):
    page("<html><body><main><h1>Sony PlayStation 5 Pro</h1><p>Loading…</p></main></body></html>")
    assert await games.gamestop(GS_URL, ctx_for(GS_URL)) is None


# ============================================================ Bandai / Play-Asia


async def test_bandai_order_period_ended_beats_jsonld(page):
    url = "https://p-bandai.com/us/item/N2716836001001"
    page(fx("bandai_ended.html"))
    res = await games.bandai(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.status_text == "Order period has ended"
    assert res.detail["price_value"] == 260.0 and res.detail["seller"] == "Premium Bandai"


async def test_playasia_in_stock_usually_ships(page):
    url = "https://www.play-asia.com/persona-3-reload-english/13/70gu6x"
    page(fx("playasia_instock.html"))
    res = await games.playasia(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["retailer"] == "playasia"


# ============================================================ review fixes: never another product's state


def _nd_page(obj, body: str = "<main><h1>Main product</h1></main>") -> str:
    return (f"<html><body>{body}<script id=\"__NEXT_DATA__\" type=\"application/json\">{json.dumps(obj)}"
            "</script></body></html>")


async def test_nintendo_unmatched_url_key_does_not_borrow_other_product(page):
    url = "https://www.nintendo.com/us/store/products/main-thing/"
    page(_nd_page({"props": {"x": [{"__typename": "StoreProduct", "urlKey": "other-item", "name": "Other",
                                    "sku": "1", "salesStatus": "IN_STOCK"}]}}))
    res = await games.nintendo(url, ctx_for(url))
    assert res.status != "in_stock"


async def test_pokemoncenter_unmatched_code_does_not_borrow_availability(page):
    page(_nd_page({"props": {"a": {"code": "999", "availability": {"state": "AVAILABLE"}},
                             "b": {"code": "701-1", "name": "x"}}}))
    url = "https://www.pokemoncenter.com/product/701-1/foo"
    res = await games.pokemoncenter(url, ctx_for(url))
    assert res.status != "in_stock"


async def test_pokemoncenter_related_list_under_product_does_not_count(page):
    page(_nd_page({"product": {"id": "701-1", "name": "Main",
                               "related": [{"id": "999", "availability": {"state": "AVAILABLE"}}]}}))
    url = "https://www.pokemoncenter.com/product/701-1/foo"
    res = await games.pokemoncenter(url, ctx_for(url))
    assert res.status != "in_stock"


def _tile(name, cond, av, pid=None):
    info = {"name": name, "condition": cond, "availability": av, "sku": name}
    if pid:
        info["productID"] = pid
    return f"<div data-gtmdata='{json.dumps({'productInfo': info})}'></div>"


async def test_gamestop_carousel_tile_is_not_the_product(page):
    page(f"<html><body><main><h1>Main</h1>{_tile('main', 'Digital', 'Not Available')}</main>"
         f"<section>{_tile('other', 'New', 'Available')}</section></body></html>")
    assert await games.gamestop(GS_URL, ctx_for(GS_URL)) is None  # inconclusive -> SFCC recipe / generic


async def test_gamestop_tile_with_other_pid_is_ignored(page):
    page(f"<html><body><main><h1>Sony PlayStation 5 Pro Console</h1>"
         f"{_tile('ps5pro', 'New', 'Not Available', '417934')}{_tile('ps5slim', 'New', 'Available', '400001')}"
         "</main></body></html>")
    res = await games.gamestop(GS_URL, ctx_for(GS_URL))
    assert res.status == "out_of_stock"
    page(f"<html><body><main><h1>Sony PlayStation 5 Pro Console</h1>"
         f"{_tile('ps5slim', 'New', 'Available', '400001')}</main></body></html>")
    assert await games.gamestop(GS_URL, ctx_for(GS_URL)) is None


async def test_playasia_discontinued_elsewhere_is_not_out(page):
    url = "https://www.play-asia.com/persona-3-reload-english/13/70gu6x"
    page(fx("playasia_instock.html").replace("In stock, usually ships within 24 hours.", "Ships in 3-5 days")
         + "")
    res = await games.playasia(url, ctx_for(url))
    assert res.status != "out_of_stock"  # "(Discontinued)" labels another edition in the notes


async def test_playasia_status_out_of_stock_beats_in_stock_text(page):
    url = "https://www.play-asia.com/persona-3-reload-english/13/70gu6x"
    page(fx("playasia_instock.html").replace("In stock, usually ships within 24 hours.", "Out of stock")
         .replace("</div>\n</body>", "<p class='notes'>Tip: in stock, usually ships items leave in 24h.</p>"
                                      "</div>\n</body>"))
    res = await games.playasia(url, ctx_for(url))
    assert res.status == "out_of_stock"
