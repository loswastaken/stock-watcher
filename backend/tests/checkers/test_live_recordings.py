"""Regression tests replaying REAL page/API recordings (site-probe run of 2026-09-29, home connection).

Fixtures live in ``fixtures/live/<store>/*.gz`` (gzipped, big inline scripts stripped). Each test serves
the recorded bodies through respx exactly as the site answered (status, redirects) and runs the full
``run_check`` path (adapter → platform recipes → generic). Bodies the recording doesn't have (e.g.
Target's product_fulfillment_v1, NVIDIA's feinventory) are small synthetic answers in the documented
shape, marked as such.
"""
from __future__ import annotations

import gzip
import json
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from app.checkers import generic, run_check
from app.checkers.retailers import electronics, target

LIVE = Path(__file__).parent / "fixtures" / "live"


def body(name: str) -> str:
    return gzip.decompress((LIVE / f"{name}.gz").read_bytes()).decode("utf-8")


def _key(url: str) -> tuple:
    p = urlsplit(url)
    return (p.hostname, p.path.rstrip("/") or "/", tuple(sorted((k, v) for k, v in parse_qsl(p.query) if k != "intl")))


@contextmanager
def replay(routes: dict[str, dict]):
    """Serve ``routes``: url → {"status", "body" (fixture name, str or JSON-able), "final" (redirect target),
    "type"}. A url ending in ``*`` matches by prefix. Anything else answers 599 and is listed in
    ``calls.missing``."""
    exact, prefix = {}, []
    for u, spec in routes.items():
        if u.endswith("*"):
            prefix.append((u[:-1], spec))
        else:
            exact[_key(u)] = spec
            if spec.get("final"):
                exact[_key(spec["final"])] = {**spec, "final": None}

    class Calls(list):
        missing: list[str]

    calls = Calls()
    calls.missing = []

    def side(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        spec = exact.get(_key(url)) or next((s for p, s in prefix if url.startswith(p)), None)
        if spec is None:
            calls.missing.append(url)
            return httpx.Response(599, text="not recorded")
        if spec.get("final") and _key(spec["final"]) != _key(url):
            return httpx.Response(302, headers={"Location": spec["final"]})
        b = spec.get("body", "")
        if isinstance(b, (dict, list)):
            return httpx.Response(spec.get("status", 200), json=b)
        text = body(b) if isinstance(b, str) and (LIVE / f"{b}.gz").exists() else b
        ctype = spec.get("type") or ("application/json" if text.lstrip()[:1] in "[{" else "text/html; charset=utf-8")
        return httpx.Response(spec.get("status", 200), text=text, headers={"content-type": ctype})

    with respx.mock(assert_all_called=False) as router:
        router.route().mock(side_effect=side)
        yield calls


async def check(url: str, rc: dict | None = None):
    return await run_check("site", url, None, None, rc)


# =========================================================================== Target


@pytest.fixture
def _target_state():
    target._key_cache.update(key=None, exp=0.0, loc=None)
    target._stores_cache.clear()
    yield
    target._key_cache.update(key=None, exp=0.0, loc=None)
    target._stores_cache.clear()


REDSKY = "https://redsky.target.com/redsky_aggregations/v1/web/"
# product_fulfillment_v1 wasn't in the recording (the old code called pdp_fulfillment_v1, which now
# answers 410): a synthetic answer in the same data.product.fulfillment shape.
TARGET_FULFILLMENT_IN = {"data": {"product": {"tcin": "94693225", "fulfillment": {
    "product_id": "94693225", "is_out_of_stock_in_all_store_locations": False,
    "shipping_options": {"availability_status": "IN_STOCK", "available_to_promise_quantity": 25.0},
    "store_options": []}}}}


async def test_target_delivery_uses_real_store_not_digital_3991(_target_state):
    url = "https://www.target.com/p/nintendo-switch-2-console/-/A-94693225"
    with replay({
        "https://www.target.com/p/-/A-94693225": {"body": "target/pdp_94693225.html"},
        # recorded with store_id=3991: HTTP 206 + "Parameter store_id cannot be digital store 3991" errors
        REDSKY + "pdp_client_v1*": {"status": 206, "body": "target/pdp_client_94693225.json"},
        REDSKY + "product_fulfillment_v1*": {"body": TARGET_FULFILLMENT_IN},
        REDSKY + "pdp_fulfillment_v1*": {"status": 410, "body": ""},
    }) as calls:
        res = await check(url)
    assert res.status == "in_stock", (res.status_text, res.error, calls.missing)
    assert res.price == "$499.99" and res.title and "Switch 2" in res.title
    assert not any("3991" in c for c in calls)
    client = next(c for c in calls if "pdp_client_v1" in c)
    ful = next(c for c in calls if "product_fulfillment_v1" in c)
    # the page's own __TGT_DATA__.serverLocationVariables (geo-IP store 3363, ZIP 02122, MA) is used
    assert dict(parse_qsl(urlsplit(client).query))["store_id"] == "3363"
    q = dict(parse_qsl(urlsplit(ful).query))
    assert q["store_id"] == "3363" and q["zip"] == "02122" and q["state"] == "MA"
    assert q["key"] == "9f36aeafbe60771e321a7cc95a78140772ab3e96"  # the key the page embeds
    assert not any("pdp_fulfillment_v1" in c for c in calls)


def test_target_page_location_and_key_from_recorded_page():
    html = body("target/pdp_94693225.html")
    assert target.API_KEY_RE.search(html).group(1) == "9f36aeafbe60771e321a7cc95a78140772ab3e96"
    assert target.page_location(html) == {"store_id": "3363", "zip": "02122", "state": "MA",
                                           "latitude": "42.300", "longitude": "-71.050"}


async def test_target_gone_tcin_is_product_not_found(_target_state):
    url = ("https://www.target.com/p/pokemon-trading-card-game-scarlet-38-violet-prismatic-evolutions-elite-"
           "trainer-box/-/A-93954435")
    with replay({
        "https://www.target.com/p/-/A-93954435": {"status": 404, "body": "target/pdp_93954435_404.html"},
        REDSKY + "nearby_stores_v1*": {"body": "target/nearby_stores_60601.json"},
        REDSKY + "pdp_client_v1*": {"status": 404, "body": "target/pdp_client_93954435_404.json"},
    }) as calls:
        res = await check(url, {"fulfillment": "pickup", "zip": "60601", "radius_miles": 25})
    assert res.status == "error" and res.status_text == "Product page not found (HTTP 404)"
    assert "store_id=2799" in next(c for c in calls if "pdp_client_v1" in c)  # nearest real store


async def test_target_legacy_fulfillment_is_fallback_only(_target_state):
    url = "https://www.target.com/p/-/A-94693225"
    with replay({
        url: {"body": "target/pdp_94693225.html"},
        REDSKY + "pdp_client_v1*": {"status": 206, "body": "target/pdp_client_94693225.json"},
        REDSKY + "product_fulfillment_v1*": {"status": 404, "body": ""},
        REDSKY + "pdp_fulfillment_v1*": {"body": TARGET_FULFILLMENT_IN},
    }) as calls:
        res = await check(url)
    assert res.status == "in_stock"
    assert [c.split("?")[0].rsplit("/", 1)[1] for c in calls if "fulfillment" in c] == [
        "product_fulfillment_v1", "pdp_fulfillment_v1"]


# =========================================================================== NVIDIA

NV_SEARCH = "https://api.nvidia.partners/edge/product/search*"
NV_INV = "https://api.store.nvidia.com/partner/v1/feinventory*"


async def test_nvidia_geforce_info_page_is_never_in_stock():
    url = "https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5080/"
    # the page's JSON-LD claims InStock at $999 (marketing), which is what the generic checker saw
    assert generic.analyze(body("nvidia/geforce_rtx5080_info.html"), url).status == "in_stock"
    with replay({
        NV_SEARCH: {"body": "nvidia/search_rtx5080.json"},  # real: 0 products for manufacturer=NVIDIA
        NV_INV: {"status": 503, "body": ""},
        url: {"body": "nvidia/geforce_rtx5080_info.html"},
    }) as calls:
        res = await check(url)
    assert res.status == "unknown" and res.status_text == electronics.NV_INFO_TEXT
    assert "skus=NVGFT580" in next(c for c in calls if "feinventory" in c)
    assert not any(c.startswith("https://www.nvidia.com/") for c in calls)


async def test_nvidia_geforce_info_page_maps_to_fe_inventory():
    url = "https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5080/"
    inv = {"success": True, "map": None, "listMap": [  # synthetic feinventory row (documented shape)
        {"is_active": "true", "product_url": "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/"
         "nvidia-geforce-rtx-5080/", "price": "999.00", "fe_sku": "NVGFT580_US", "locale": "US"}]}
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5080.json"}, NV_INV: {"body": inv}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == "$999.00" and res.detail["sku_guessed"] is True


async def test_nvidia_marketplace_fe_page_reads_sku_from_json_ld():
    url = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5090/"
    electronics._nv_page_cache.clear()
    inv = {"success": True, "map": None, "listMap": [
        {"is_active": "false", "product_url": url, "price": "1999.00", "fe_sku": "NVGFT590_US", "locale": "US"}]}
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5090.json"}, url: {"body": "nvidia/marketplace_rtx5090.html"},
                 NV_INV: {"body": inv}}) as calls:
        res = await check(url)
    electronics._nv_page_cache.clear()
    assert res.status == "out_of_stock" and res.detail["sku"] == "NVGFT590"  # JSON-LD "mpn" on the real page
    assert res.title == "NVIDIA GeForce RTX 5090" and res.price == "$1,999.00"
    assert "skus=NVGFT590" in next(c for c in calls if "feinventory" in c)


async def test_nvidia_marketplace_inventory_down_is_clear_unknown():
    url = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5090/"
    electronics._nv_page_cache.clear()
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5090.json"}, url: {"body": "nvidia/marketplace_rtx5090.html"},
                 NV_INV: {"status": 503, "body": ""}}):
        res = await check(url)
    electronics._nv_page_cache.clear()
    assert res.status == "unknown" and "Founders Edition" in res.status_text


# =========================================================================== Best Buy


async def test_bestbuy_reused_sku_is_flagged_not_priced():
    # SKU 6603968 (sample URL slug: PS5 Pro) now belongs to a $4.99 LEGO set — the "$4.99 price" bug
    url = "https://www.bestbuy.com/site/sony-playstation-5-pro-console-white/6603968.p?skuId=6603968"
    with replay({"https://www.bestbuy.com/api/3.0/priceBlocks?skus=6603968": {
            "body": "bestbuy/priceblocks_6603968.json"}}):
        res = await check(url)
    assert res.status == "error" and "different product" in res.status_text and "LEGO" in res.status_text
    assert res.price is None


async def test_bestbuy_switch2_in_stock():
    url = "https://www.bestbuy.com/product/nintendo-switch-2-system-black/JJGCQ8WQ7K/sku/6614313"
    with replay({"https://www.bestbuy.com/api/3.0/priceBlocks?skus=6614313": {
            "body": "bestbuy/priceblocks_6614313.json"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == "$499.99"


# =========================================================================== Amazon


@pytest.mark.parametrize("asin,title", [("B0DGY63Z2H", "PlayStation 5 Pro Console"),
                                        ("B0D1XD1ZV3", "Apple AirPods Pro 2")])
async def test_amazon_no_buy_box_is_not_called_third_party(asin, title):
    # Recorded pages have no buy box at all ("See All Buying Options" only) and name no seller.
    url = f"https://www.amazon.com/dp/{asin}"
    with replay({f"https://www.amazon.com/dp/{asin}?th=1&psc=1": {"body": f"amazon/dp_{asin}.html"}}) as calls:
        res = await check(url)
    assert res.title.startswith(title)
    assert res.status == "out_of_stock" and res.status_text == "No featured offer (see all buying options)"
    assert res.detail["third_party"] is None and res.detail["seller"] is None
    assert any("aodAjaxMain" in c for c in calls)  # the offer list was asked (unrecorded here)


# =========================================================================== PS Direct


async def test_psdirect_page_not_found_is_not_a_waiting_room():
    url = "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console.3009726"
    with replay({
        "https://api.direct.playstation.com/commercewebservices/ps-direct-us/users/anonymous/products/productList*": {
            "body": "psdirect/productlist_3009726.json"},  # validProductCode: false
        url: {"status": 404, "body": "psdirect/page_not_found_404.html"},
    }):
        res = await check(url)
    assert res.status == "error" and res.status_text.startswith("Product page not found")
    assert not res.detail.get("queue")


# =========================================================================== dead links (stale sample URLs)


@pytest.mark.parametrize("url,final,fixture,text", [
    ("https://www.amd.com/en/direct-buy/5335621300/us", "https://shop-us-en.amd.com/",
     "amd/directbuy_redirect_home.html", generic.HOME_REDIRECT_TEXT),
    ("https://www.dell.com/en-us/shop/alienware-aurora-gaming-desktop/spd/alienware-aurora-ac16250-gaming-desktop",
     "https://www.dell.com/en-us", "dell/redirect_home.html", generic.HOME_REDIRECT_TEXT),
    ("https://www.evga.com/products/product.aspx?pn=220-G7-1000-X1", "https://www.evga.com/default.asp",
     "evga/redirect_default_asp.html", generic.HOME_REDIRECT_TEXT),
    ("https://www.squishmallows.com/products/squishmallows-original-16-inch-cam-the-cat",
     "https://shop.jazwares.com/pages/squishmallows/", "jazwares/redirect_pages_squishmallows.html",
     generic.MOVED_TEXT),
    ("https://www.nextwarehouse.com/item/?p_num=1234567&n=PNY+GeForce+RTX+5080",
     "https://www.nextwarehouse.com/404.cfm?type=product", "nextwarehouse/404_cfm.html", generic.NOT_FOUND_TEXT),
    ("https://p-bandai.com/us/item/N2780424001001", None, "bandai/page_not_found.html", generic.NOT_FOUND_TEXT),
    ("https://www.officedepot.com/a/products/9690143/Nintendo-Switch-2-Console/",
     "https://www.officedepot.com/a/products/9690143//;jsessionid=0000frsZ9lf6zYy7pwnrFyI-XtA:17h4h7dlm",
     "officedepot/sku_not_found.html", generic.NOT_FOUND_TEXT),
])
async def test_stale_links_say_so(url, final, fixture, text):
    with replay({url: {"body": fixture, "final": final}, "https://shop.jazwares.com/products/*": {"status": 404}}):
        res = await check(url)
    assert res.status == "error" and res.status_text == text, (res.status, res.status_text, res.error)
    assert res.detail.get("dead_link") is True


async def test_walmart_redirect_to_another_item_is_not_its_stock():
    url = "https://www.walmart.com/ip/PlayStation-5-Pro-Console/5113183757"
    with replay({url: {"body": "walmart/redirect_other_item.html",
                       "final": "https://www.walmart.com/ip/PlayStation-5-Digital-Console-Slim/17852302051"}}):
        res = await check(url)
    assert res.status == "error" and "different product" in res.status_text and "Digital" in res.status_text


async def test_popmart_unavailable_product():
    url = ("https://www.popmart.com/us/products/1123/THE-MONSTERS---Big-into-Energy-Series-Vinyl-Plush-Pendant-"
           "Blind-Box")
    with replay({url: {"body": "popmart/product_not_available.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock" and res.status_text == "Not available (removed from sale)"


async def test_toysrus_homepage_is_not_a_product():
    res = await check("https://www.toysrus.com/")
    assert res.status == "unknown" and res.status_text.startswith("Not a product page")


# =========================================================================== bot walls / empty pages


@pytest.mark.parametrize("fixture", ["homedepot/akamai_challenge.html", "homedepot/akamai_error_page.html"])
async def test_homedepot_akamai_pages_are_blocked(fixture):
    url = ("https://www.homedepot.com/p/RYOBI-ONE-18V-Cordless-3-8-in-Drill-Driver-Kit-with-1-5-Ah-Battery-and-"
           "Charger-PCL206K1/315143462")
    with replay({url: {"body": fixture}}):
        res = await check(url)
    assert res.status == "error" and "Blocked by bot protection on www.homedepot.com" in res.error


async def test_meijer_empty_shell_says_no_stock_info():
    url = "https://www.meijer.com/shopping/product/nintendo-switch-2-console/4549659.html"
    with replay({url: {"body": "meijer/product_details_shell.html"}}):
        res = await check(url)
    assert res.status == "unknown" and res.status_text == generic.NO_STOCK_INFO_TEXT


# =========================================================================== pages that decide


async def test_verizon_selected_config_ships():
    url = "https://www.verizon.com/smartphones/apple-iphone-17-pro/"
    with replay({url: {"body": "verizon/iphone17pro.html"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.status_text == "In stock (Ships between Wed, Sep 30 - Tue, Oct 6)"
    assert res.title == "Apple iPhone 17 Pro"


async def test_leica_brand_page_follows_shop_now():
    url = "https://leica-camera.com/en-US/photography/cameras/q/q3-black"
    shop = ("https://leicacamerausa.com/leica-q3-black.html?utm_campaign=buy-now_leica-camera-com"
            "&utm_source=leica-camera-com&utm_medium=hero%3Abutton_19080")
    shop_page = ('<html><head><title>Leica Q3, black</title><script type="application/ld+json">{"@context":'
                 '"https://schema.org","@type":"Product","name":"Leica Q3","offers":{"@type":"Offer","price":"5995.00",'
                 '"priceCurrency":"USD","availability":"https://schema.org/InStock"}}</script></head>'
                 '<body><h1>Leica Q3</h1></body></html>')  # synthetic: the shop page wasn't recorded
    with replay({url: {"body": "leica/q3_black_brand_page.html"}, shop: {"body": shop_page}}):
        res = await check(url)
    assert res.status == "in_stock" and res.detail["followed"] == shop and res.price == "$5,995.00"
    with replay({url: {"body": "leica/q3_black_brand_page.html"}}):  # shop unreachable
        res = await check(url)
    assert res.status == "unknown" and res.status_text.startswith("Sold at leicacamerausa.com")


@pytest.mark.parametrize("url,final,fixture,price", [
    ("https://www.gamefly.com/game/mario-kart-world/5022850", None, "gamefly/product.html", "$11.29"),
    ("https://www.lenovo.com/us/en/p/laptops/legion-laptops/legion-pro-series/legion-pro-7i-gen-10-16-inch-intel/"
     "83f5cto1wwus1", "https://www.lenovo.com/us/en/p/laptops/legion-laptops/legion-pro-series/"
     "legion-pro-7i-gen-10-16-inch-intel/len101g0039?displayrulevalidation=false", "lenovo/legion_pro_7i.html",
     "$3,279.99"),
    ("https://www.lg.com/us/tvs/lg-oled65c5pua-oled-4k-tv", None, "lg/oled65c5.html", "$2,199.99"),
    ("https://us-store.msi.com/Graphics-Cards/NVIDIA-GPU/GeForce-RTX-50-Series/GeForce-RTX-5070-Ti-16G-GAMING-TRIO-OC",
     None, "msi/rtx5070ti_trio.html", None),
    ("https://www.meta.com/quest/quest-3/", None, "oculus/quest3.html", "$599.99"),
    ("https://www.nintendo.com/us/store/products/nintendo-switch-2-system-123669/", None, "nintendo/switch2.html",
     "$499.99"),
    ("https://www.walmart.com/ip/Nintendo-Switch-2-Console/15949610846", None, "walmart/switch2_instock.html",
     "$499.00"),
])
async def test_real_in_stock_pages(url, final, fixture, price):
    with replay({url: {"body": fixture, "final": final}}) as calls:
        res = await check(url)
    assert res.status == "in_stock", (res.status_text, res.error, calls.missing)
    assert res.price == price


async def test_ninja_homepage_redirect_still_answered_by_sfcc_pid():
    # ninjakitchen.com redirects to sharkninja.com/, but SFCC's Product-Variation for the URL's pid answers
    url = "https://www.ninjakitchen.com/pdp/ninja-creami-deluxe-11-in-1-ice-cream-frozen-treat-maker/zidNC501.html"
    with replay({url: {"body": "ninja/redirect_sharkninja_home.html", "final": "https://www.sharkninja.com/"},
                 "https://www.sharkninja.com/on/demandware.store/*": {"body": "ninja/sfcc_variation_NC501.json"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == "$249.99"


async def test_gigabyte_spec_page_is_no_direct_sales():
    url = "https://www.gigabyte.com/Graphics-Card/GV-N5090GAMING-OC-32GD"
    with replay({url: {"body": "gigabyte/rtx5090_gaming_oc.html"}}):
        res = await check(url)
    assert res.status == "unknown" and res.status_text == electronics.NO_DIRECT_SALES


def test_live_fixtures_stay_small():
    total = sum(p.stat().st_size for p in LIVE.rglob("*.gz"))
    assert total < 6_000_000, total
    json.dumps(total)
