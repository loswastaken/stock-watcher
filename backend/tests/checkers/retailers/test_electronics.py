"""Electronics adapters (app.checkers.retailers.electronics) against hand-written fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.checkers import fetcher
from app.checkers.retailers import electronics as el
from app.checkers.retailers import run_adapter
from app.checkers.retailers.base import AdapterContext, RetailerConfig
from app.checkers.retailers.registry import match_retailer

FX = Path(__file__).resolve().parents[1] / "fixtures" / "retailers" / "electronics"


def fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def fxj(name: str):
    return json.loads(fx(name))


def ctx_for(url: str, **rc) -> AdapterContext:
    return AdapterContext(retailer=match_retailer(url), generic_config={}, retailer_config=RetailerConfig.from_dict(rc))


@pytest.fixture
def page(monkeypatch):
    """Serve fixture HTML from fetcher.fetch_html; returns the list of requested URLs."""
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


# ============================================================ Newegg

NE_URL = "https://www.newegg.com/gigabyte-aorus-rtx-5090/p/N82E16814932744"
NE_API = "https://www.newegg.com/product/api/ProductRealtime"


def test_newegg_item_parsing():
    assert el.newegg_item(NE_URL) == "N82E16814932744"
    assert el.newegg_item("https://www.newegg.com/Product/Product.aspx?Item=N82E16814126597") == "N82E16814126597"
    assert el.newegg_item("https://www.newegg.com/p/1FT-001N-00050") == "1FT-001N-00050"
    assert el.newegg_item("https://www.newegg.com/asus-rog/p/9SIA4REK4R6437?Item=9SIA4REK4R6437") == "9SIA4REK4R6437"
    assert el.newegg_item("https://www.newegg.com/p/pl?d=rtx+5090") is None
    assert el.newegg_api_numbers("N82E16814932744") == ["14-932-744", "N82E16814932744"]
    assert el.newegg_api_numbers("9SIA4REK4R6437") == ["9SIA4REK4R6437"]


@respx.mock
async def test_newegg_api_first_party_in_stock():
    route = respx.get(NE_API, params={"ItemNumber": "14-932-744"}).mock(
        return_value=httpx.Response(200, json=fxj("newegg_realtime_instock.json")))
    res = await run_adapter(NE_URL, {}, {})
    assert route.called
    assert res.status == "in_stock" and res.available[0].key == "stock"
    assert res.title.startswith("GIGABYTE AORUS GeForce RTX 5090")
    assert res.detail["price_value"] == 2599.99 and res.price == "$2,599.99"
    assert res.detail["seller"] == "Newegg" and res.detail["third_party"] is False
    assert res.detail["cart_url"] == "https://secure.newegg.com/Shopping/AddtoCart.aspx?Submit=ADD&ItemList=N82E16814932744"
    assert res.detail["retailer"] == "newegg" and res.detail["stock_qty"] == 12
    assert res.image_url == "https://c1.neweggimages.com/ProductImage/14-932-744-01.png"


@respx.mock
async def test_newegg_api_out_of_stock():
    respx.get(NE_API).mock(return_value=httpx.Response(200, json=fxj("newegg_realtime_oos.json")))
    res = await el.newegg(NE_URL, ctx_for(NE_URL))
    assert res.status == "out_of_stock" and res.status_text == "Out of stock"
    assert "cart_url" not in res.detail and res.detail["price_value"] == 2599.99


@respx.mock
async def test_newegg_marketplace_seller_filtered_by_default():
    url = "https://www.newegg.com/asus-rog-astral/p/9SIA4REK4R6437"
    respx.get(NE_API).mock(return_value=httpx.Response(200, json=fxj("newegg_realtime_marketplace.json")))
    res = await el.newegg(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["third_party"] is True and res.detail["seller"] == "GPU Deals Direct"


@respx.mock
async def test_newegg_marketplace_seller_allowed():
    url = "https://www.newegg.com/asus-rog-astral/p/9SIA4REK4R6437"
    respx.get(NE_API).mock(return_value=httpx.Response(200, json=fxj("newegg_realtime_marketplace.json")))
    res = await el.newegg(url, ctx_for(url, official_only=False))
    assert res.status == "in_stock" and "GPU Deals Direct" in res.status_text
    assert res.detail["cart_url"].endswith("ItemList=9SIA4REK4R6437")


@respx.mock
async def test_newegg_api_blocked_falls_back_to_page_auto_notify(page):
    respx.get(NE_API).mock(return_value=httpx.Response(403, text="<html>denied</html>"))
    calls = page(fx("newegg_page_autonotify.html"))
    res = await el.newegg(NE_URL, ctx_for(NE_URL))
    assert calls == [NE_URL]
    assert res.status == "out_of_stock" and "Auto Notify" in res.status_text
    assert res.detail["source"] == "html" and res.detail["seller"] == "Newegg"


@respx.mock
async def test_newegg_page_marketplace_seller(page):
    url = "https://www.newegg.com/p/1FT-001N-00050"
    respx.get(NE_API).mock(return_value=httpx.Response(200, json={}))
    page(fx("newegg_page_marketplace.html"))
    res = await el.newegg(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["seller"] == "GPU Deals Direct"


@respx.mock
async def test_newegg_bot_check_raises(page):
    respx.get(NE_API).mock(return_value=httpx.Response(200, text="<html>are you a human</html>"))
    page("<html><body>Are you a human?</body></html>", final="https://www.newegg.com/areyouahuman?referrer=x")
    with pytest.raises(fetcher.FetchError, match="areyouahuman"):
        await el.newegg(NE_URL, ctx_for(NE_URL))


# ============================================================ Nvidia

NV_URL = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5090/"
NV_SEARCH = "https://api.nvidia.partners/edge/product/search"
NV_INV = "https://api.store.nvidia.com/partner/v1/feinventory"


def test_nvidia_gpu_from_url():
    assert el.nvidia_gpu(NV_URL) == "RTX 5090"
    assert el.nvidia_gpu("https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5070-ti/") == "RTX 5070 Ti"
    assert el.nvidia_gpu("https://marketplace.nvidia.com/en-us/consumer/graphics-cards/rtx-4080-super/") == "RTX 4080 Super"
    assert el.nvidia_gpu("https://www.nvidia.com/en-us/shield/") is None


@respx.mock
async def test_nvidia_founders_edition_in_stock():
    s = respx.get(NV_SEARCH, params={"gpu": "RTX 5090"}).mock(return_value=httpx.Response(200, json=fxj("nvidia_search.json")))
    inv = respx.get(NV_INV, params={"skus": "NVGFT590", "locale": "en-us"}).mock(
        return_value=httpx.Response(200, json=fxj("nvidia_feinventory_active.json")))
    res = await el.nvidia(NV_URL, ctx_for(NV_URL))
    assert s.called and inv.called
    assert res.status == "in_stock" and res.detail["sku"] == "NVGFT590"
    assert res.detail["price_value"] == 1999.0 and res.title == "NVIDIA GeForce RTX 5090"
    assert res.detail["cart_url"].startswith("https://marketplace.nvidia.com/")
    assert res.detail["seller"] == "NVIDIA" and res.detail["third_party"] is False


@respx.mock
async def test_nvidia_out_of_stock_with_sku_override():
    # SKU given in retailer_config: no search call needed for the SKU (search still names the product)
    respx.get(NV_SEARCH).mock(return_value=httpx.Response(500))
    respx.get(NV_INV, params={"skus": "NVGFT590"}).mock(
        return_value=httpx.Response(200, json=fxj("nvidia_feinventory_inactive.json")))
    res = await el.nvidia(NV_URL, ctx_for(NV_URL, store_id="NVGFT590"))
    assert res.status == "out_of_stock" and res.detail["fe_sku"] == "NVGFT590_US"


@respx.mock
async def test_nvidia_inventory_down_uses_search_status():
    url = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5080/"
    respx.get(NV_SEARCH).mock(return_value=httpx.Response(200, json=fxj("nvidia_search.json")))
    respx.get(NV_INV).mock(return_value=httpx.Response(503))
    res = await el.nvidia(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["sku"] == "NVGFT580" and res.detail["source"] == "search"


@respx.mock
async def test_nvidia_never_reports_another_gpus_founders_edition():
    # RTX 5070 page: the search lists the 5090 / 5080 FEs but not a 5070 FE -> no product, no SKU
    url = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5070/"
    respx.get(NV_SEARCH).mock(return_value=httpx.Response(200, json=fxj("nvidia_search.json")))
    inv = respx.get(NV_INV).mock(return_value=httpx.Response(200, json=fxj("nvidia_feinventory_active.json")))
    assert await el.nvidia(url, ctx_for(url)) is None
    assert not inv.called
    # a pinned SKU missing from the search must not borrow the 5080 FE's buy_now status
    url = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5080/"
    respx.get(NV_INV).mock(return_value=httpx.Response(503))
    assert await el.nvidia(url, ctx_for(url, store_id="NVGFT999")) is None


async def test_nvidia_without_gpu_or_sku_falls_through():
    url = "https://www.nvidia.com/en-us/shield/"
    assert await el.nvidia(url, ctx_for(url)) is None


# ============================================================ Micro Center

MC_URL = "https://www.microcenter.com/product/691234/asus-nvidia-geforce-rtx-5090-tuf"


@respx.mock
async def test_microcenter_store_stock_with_cookie_and_storeid():
    route = respx.get("https://www.microcenter.com/product/691234/asus-nvidia-geforce-rtx-5090-tuf",
                      params={"storeid": "101"}).mock(return_value=httpx.Response(200, text=fx("microcenter_instock.html")))
    res = await el.microcenter(MC_URL, ctx_for(MC_URL, store_id="101", fulfillment="pickup"))
    req = route.calls.last.request
    assert "storeSelected=101" in req.headers.get("cookie", "")
    assert res.status == "in_stock"
    assert [a.key for a in res.available] == ["pickup:101"]
    assert res.available[0].label == "In stock at Micro Center Tustin (7)"
    assert res.detail["stock_qty"] == "7" and res.detail["price_value"] == 2199.99
    assert "default store" not in res.status_text
    assert res.title == "ASUS NVIDIA GeForce RTX 5090 TUF Gaming OC 32GB"


@respx.mock
async def test_microcenter_without_store_uses_page_default(page):
    page(fx("microcenter_soldout.html"))
    url = "https://www.microcenter.com/product/690001/nvidia-geforce-rtx-5090-founders-edition"
    res = await el.microcenter(url, ctx_for(url))
    assert res.status == "out_of_stock"
    assert res.status_text.startswith("Sold out at Micro Center Chicago") and "default store" in res.status_text
    assert res.detail["store_id"] == "131" and res.detail["store_configured"] is False


async def test_microcenter_default_store_in_stock_label(page):
    page(fx("microcenter_instock.html"))
    res = await el.microcenter(MC_URL, ctx_for(MC_URL))
    # no store chosen: the site's default store is reported as plain "stock", not a pickup location
    assert res.status == "in_stock" and [a.key for a in res.available] == ["stock"]
    assert "default store" in res.status_text


async def test_microcenter_ignores_store_ids_in_links(page):
    html = ("<html><body><main><h1>RTX 5090</h1><span class='inventoryCnt'>5 NEW IN STOCK</span>"
            "<a href='/product/691234/x?storeid=155'>Check Denver</a></main></body></html>")
    page(html)
    res = await el.microcenter(MC_URL, ctx_for(MC_URL))
    assert res.status == "in_stock" and [a.key for a in res.available] == ["stock"]
    assert res.detail["store_id"] is None


@pytest.mark.parametrize("cnt", ["3 OPEN BOX IN STOCK", "2 REFURBISHED IN STOCK"])
async def test_microcenter_open_box_is_not_new_stock(page, cnt):
    page(f"<html><body><main><h1>RTX 5090</h1><span class='inventoryCnt'>{cnt}</span></main></body></html>")
    res = await el.microcenter(MC_URL, ctx_for(MC_URL))
    assert res.status == "out_of_stock"
    res = await el.microcenter(MC_URL, ctx_for(MC_URL, condition="any"))
    assert res.status == "in_stock"


async def test_microcenter_new_count_next_to_open_box(page):
    page("<html><body><main><h1>RTX 5090</h1><span class='inventoryCnt'>4 OPEN BOX IN STOCK</span>"
         "<span class='inventoryCnt'>0 NEW IN STOCK</span></main></body></html>")
    res = await el.microcenter(MC_URL, ctx_for(MC_URL))
    assert res.status == "out_of_stock"


@pytest.mark.parametrize("text,verdict", [
    ("25+ NEW IN STOCK", "in"), ("0 NEW IN STOCK", "out"), ("SOLD OUT", "out"), ("Limited Availability", "in"),
])
def test_microcenter_inventory_vocabulary(text, verdict):
    from app.checkers.retailers.pagekit import page_view

    html = f"<main><span class='inventoryCnt'>{text}</span></main>"
    assert el._mc_inventory(page_view(html), html)[0] == verdict


# ============================================================ B&H

BH_URL = "https://www.bhphotovideo.com/c/product/1667800-REG/sony_ilce7m4_b_alpha_a7_iv_mirrorless.html"


async def test_bhphoto_in_stock(page):
    page(fx("bhphoto_instock.html"))
    res = await el.bhphoto(BH_URL, ctx_for(BH_URL))
    assert res.status == "in_stock" and res.detail["price_value"] == 2498.0
    assert res.detail["stock_status"] == "In Stock" and res.detail["seller"] == "B&H Photo"


async def test_bhphoto_more_on_the_way_is_orderable_despite_jsonld(page):
    page(fx("bhphoto_backordered.html"))
    res = await el.bhphoto(BH_URL, ctx_for(BH_URL))
    assert res.status == "in_stock" and res.status_text == "More on the Way"


async def test_bhphoto_coming_soon(page):
    page(fx("bhphoto_comingsoon.html"))
    res = await el.bhphoto(BH_URL, ctx_for(BH_URL))
    assert res.status == "out_of_stock" and res.status_text == "Coming soon"


async def test_bhphoto_perimeterx_block_raises(page):
    page(fx("bhphoto_blocked.html"))
    with pytest.raises(fetcher.FetchError, match="PerimeterX"):
        await el.bhphoto(BH_URL, ctx_for(BH_URL))


# ============================================================ phrase sites


async def test_adorama_temporarily_not_available(page):
    url = "https://www.adorama.com/niz8.html"
    page(fx("adorama_unavailable.html"))
    res = await el.adorama(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.status_text == "Temporarily not available"
    assert res.detail["retailer"] == "adorama"


async def test_adorama_special_order_only_counts_in_stock_status(page):
    url = "https://www.adorama.com/niz8.html"
    # "special order" in a policy blurb must not override a clear out-of-stock verdict
    page("<html><body><main><h1>Nikon Z8</h1><div class='av-stock'><span>Out of stock</span></div>"
         "<p>Special order items are non-returnable.</p></main></body></html>")
    res = await el.adorama(url, ctx_for(url))
    assert res.status == "out_of_stock"
    page("<html><body><main><h1>Nikon Z8</h1><p>Lots of product description text here.</p>"
         "<div class='av-stock'><span class='av-message'>Special Order</span></div></main></body></html>")
    res = await el.adorama(url, ctx_for(url))
    assert res.status == "in_stock" and res.status_text == "Special order"


async def test_evga_auto_notify_is_out(page):
    url = "https://www.evga.com/products/product.aspx?pn=220-G7-1000-X1"
    page(fx("evga_autonotify.html"))
    res = await el.evga(url, ctx_for(url))
    assert res.status == "out_of_stock" and "Auto Notify" in res.status_text


async def test_amd_direct_buy(page):
    url = "https://www.amd.com/en/direct-buy/5458372200/us"
    page(fx("amd_directbuy_oos.html"))
    res = await el.amd(url, ctx_for(url))
    assert res.status == "out_of_stock"
    page(fx("amd_directbuy_instock.html"))
    res = await el.amd(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["seller"] == "AMD"


async def test_amd_info_page_falls_through():
    url = "https://www.amd.com/en/products/graphics/desktops/radeon/9000-series/amd-radeon-rx-9070xt.html"
    assert await el.amd(url, ctx_for(url)) is None


async def test_dell_temporarily_out_beats_stale_jsonld(page):
    url = "https://www.dell.com/en-us/shop/alienware-32-4k-qd-oled-gaming-monitor-aw3225qf/apd/210-bmdp/monitors-monitor-accessories"
    page(fx("dell_temp_oos.html"))
    res = await el.dell(url, ctx_for(url))
    assert res.status == "out_of_stock" and res.status_text == "Temporarily out of stock"
    assert res.detail["price_value"] == 899.99


async def test_antonline_add_to_cart(page):
    url = "https://www.antonline.com/Sony/Electronics/Gaming_Devices/Gaming_Consoles/1500001"
    page("<html><body><main><h1>PlayStation 5 Pro Console</h1><p>$699.99</p>"
         "<button class='add_to_cart_button uk-button'>Add to Cart</button></main></body></html>")
    res = await el.antonline(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["seller"] == "Antonline"


async def test_antonline_related_add_to_cart_is_not_the_product(page):
    url = "https://www.antonline.com/Sony/Electronics/Gaming_Devices/Gaming_Consoles/1500001"
    page("<html><body><main><h1>PlayStation 5 Pro Console</h1><p>Our flagship console, restocks vary.</p>"
         "<span>Sold Out</span></main><div class='rel'><a class='add_to_cart_button' href='#'>Add to Cart</a>"
         "</div></body></html>")
    res = await el.antonline(url, ctx_for(url))
    assert res.status == "out_of_stock"


async def test_antonline_disabled_main_button_beats_later_enabled_one(page):
    url = "https://www.antonline.com/Sony/Electronics/Gaming_Devices/Gaming_Consoles/1500001"
    page("<html><body><main><h1>PlayStation 5 Pro Console</h1><p>Our flagship console, restocks vary.</p>"
         "<button class='add_to_cart_button' disabled>Add to Cart</button>"
         "<div><button class='add_to_cart_button'>Add to Cart</button></div></main></body></html>")
    res = await el.antonline(url, ctx_for(url))
    assert res.status == "out_of_stock"


async def test_antonline_queue_it(page):
    url = "https://www.antonline.com/Sony/Electronics/Gaming_Devices/Gaming_Consoles/1500001"
    page("<html><title>Queue-it</title><body>You are now in line</body></html>",
         final="https://antonline.queue-it.net/?c=antonline&e=ps5")
    res = await el.antonline(url, ctx_for(url))
    assert res.status == "unknown" and res.detail["queue"] is True
    assert res.status_text == "Waiting room active — drop may be live"


async def test_gigabyte_info_page_is_unknown(page):
    url = "https://www.gigabyte.com/Graphics-Card/GV-N5090GAMING-OC-32GD"
    page(fx("gigabyte_info.html"))
    res = await el.gigabyte(url, ctx_for(url))
    assert res.status == "unknown" and res.status_text == "No direct sales on this page"
    assert res.detail["info_only"] is True


async def test_gigabyte_shop_page_with_offer_is_trusted(page):
    url = "https://www.gigabyte.com/us/shop/aorus-monitor"
    page('<html><head><script type="application/ld+json">{"@type":"Product","name":"AORUS FO32U2P",'
         '"offers":{"@type":"Offer","price":"999.99","priceCurrency":"USD","availability":"https://schema.org/InStock"}}'
         "</script></head><body><main><h1>AORUS FO32U2P</h1></main></body></html>")
    res = await el.gigabyte(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["price_value"] == 999.99


async def test_zotac_store_magento_stock(page):
    url = "https://www.zotacstore.com/us/zt-b50800j-10p"
    page(fx("zotacstore_instock.html"))
    res = await el.zotac(url, ctx_for(url))
    assert res.status == "in_stock" and res.detail["price_value"] == 1199.99


async def test_zotac_brand_site_is_info_only(page):
    url = "https://www.zotac.com/us/product/graphics_card/zotac-gaming-geforce-rtx-5080-solid-oc"
    page(fx("gigabyte_info.html").replace("GIGABYTE", "ZOTAC"))
    res = await el.zotac(url, ctx_for(url))
    assert res.status == "unknown" and res.status_text == "No direct sales on this page"


@pytest.mark.parametrize("fn,url,html,status,text", [
    ("lenovo", "https://www.lenovo.com/us/en/p/laptops/legion-laptops/legion-pro-series/legion-pro-7i-gen-10/len101g0038",
     "<main><h1>Legion Pro 7i</h1><p>Temporarily unavailable</p><button>Add to Cart</button></main>",
     "out_of_stock", "Temporarily unavailable"),
    ("lg", "https://www.lg.com/us/tvs/lg-oled65c5pua-oled-4k-tv",
     "<main><h1>LG C5 65 inch OLED</h1><button>Notify me</button></main>", "out_of_stock", "Out of stock"),
    ("leica", "https://leica-camera.com/en-US/photography/cameras/q/q3-black",
     "<main><h1>Leica Q3</h1><p>Currently not available</p></main>", "out_of_stock", "Not available online"),
    ("meta_quest", "https://www.meta.com/quest/quest-3/",
     "<main><h1>Meta Quest 3</h1><button>Add to cart</button></main>", "in_stock", "In stock"),
])
async def test_phrase_sites(page, fn, url, html, status, text):
    page(f"<html><body>{html}</body></html>")
    res = await getattr(el, fn)(url, ctx_for(url))
    assert (res.status, res.status_text) == (status, text)
    assert res.detail["retailer"] == match_retailer(url).key
