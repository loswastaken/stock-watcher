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

from app.checkers import fetcher, generic, run_check
from app.checkers.retailers import bigbox, electronics, target

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
    assert res.status_text == "Info page — watch the NVIDIA Marketplace listing instead"
    assert res.detail["info_only"] is True and res.detail["watch_instead"] == \
        "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5080/"
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


AOD = "https://www.amazon.com/gp/product/ajax/aodAjaxMain/*"


@pytest.mark.parametrize("asin,text,seller,offers,total", [
    # 2026-09-29 18:01 headed Chrome run: no buy box; the offer list is Woot only ($289.99) ...
    ("B0D1XD1ZV3", "Not sold by Amazon — 1 other seller from $289.99", "Woot", 1, 1),
    # ... and 9 marketplace offers (7 used, 2 new from $1,348.99), none by Amazon.com
    ("B0DGY63Z2H", "Not sold by Amazon — 2 other sellers from $1,348.99", "TheBest Of TheWest", 2, 9),
])
async def test_amazon_offer_list_without_amazon_is_specific_out(asin, text, seller, offers, total):
    with replay({f"https://www.amazon.com/dp/{asin}?th=1&psc=1": {"body": f"amazon/dp_{asin}.html"},
                 AOD: {"body": f"amazon/aod_{asin}.html"}}) as calls:
        res = await check(f"https://www.amazon.com/dp/{asin}")
    assert not calls.missing
    assert res.status == "out_of_stock" and res.status_text == text
    assert res.detail["seller"] == seller and res.detail["third_party"] is True
    assert res.detail["offers"] == offers and res.detail["offers_total"] == total
    assert res.price is None  # no Amazon price to report
    # official_only off: the marketplace offers count
    with replay({f"https://www.amazon.com/dp/{asin}?th=1&psc=1": {"body": f"amazon/dp_{asin}.html"},
                 AOD: {"body": f"amazon/aod_{asin}.html"}}):
        res = await check(f"https://www.amazon.com/dp/{asin}", {"official_only": False})
    assert res.status == "in_stock" and res.status_text == "Available from other sellers"


def test_amazon_offer_list_parse_real_markup():
    from app.checkers.retailers import amazon
    offers = amazon.parse_offers(body("amazon/aod_B0DGY63Z2H.html"))
    assert len(offers) == 9 and not any(o["amazon"] for o in offers)
    assert [o["price"] for o in offers if o["new"]] == ["$1,348.99", "$1,534.99"]
    # Amazon Resale / Warehouse (used goods) is not the Amazon.com retail offer
    fake = '<div id="aod-offer"><div id="aod-offer-heading">New</div><div id="aod-offer-soldBy">' \
           '<a href="/gp/aag/main?seller=A2L77EE7U53NWQ">Amazon Resale</a></div></div>'
    assert amazon.parse_offers(fake)[0]["amazon"] is False


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


async def test_meijer_empty_shell():
    # Both runs (headless 15:4x and headed Chrome 18:02) rendered Meijer's app with an empty <main> for
    # item 4549659. Meijer item numbers are UPC-based (10+ digits): a 7-digit one that renders nothing is
    # a dead link; a UPC-like one that renders nothing says so precisely.
    url = "https://www.meijer.com/shopping/product/nintendo-switch-2-console/4549659.html"
    with replay({url: {"body": "meijer/product_details_shell.html"}}):
        res = await check(url)
    assert res.status == "error" and res.status_text == generic.NOT_FOUND_TEXT and res.detail["dead_link"]
    url2 = "https://www.meijer.com/shopping/product/nintendo-switch-2-console/4549688501.html"
    with replay({url2: {"body": "meijer/product_details_shell.html"}}):
        res = await check(url2)
    assert res.status == "unknown" and res.status_text == bigbox.MEIJER_EMPTY_TEXT


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
    ("https://www.lenovo.com/us/en/p/laptops/legion-laptops/legion-pro-series/legion-pro-7i-gen-10-16-inch-intel/"
     "83f5cto1wwus1", "https://www.lenovo.com/us/en/p/laptops/legion-laptops/legion-pro-series/"
     "legion-pro-7i-gen-10-16-inch-intel/len101g0039?displayrulevalidation=false", "lenovo/legion_pro_7i.html",
     "$3,279.99"),
    # the 65" variant the URL names (OLED65C5PUA) — not the 77" one ($2,199.99) the old code priced
    ("https://www.lg.com/us/tvs/lg-oled65c5pua-oled-4k-tv", None, "lg/oled65c5.html", "$1,499.99"),
    ("https://us-store.msi.com/Graphics-Cards/NVIDIA-GPU/GeForce-RTX-50-Series/GeForce-RTX-5070-Ti-16G-GAMING-TRIO-OC",
     None, "msi/rtx5070ti_trio.html", "$1,249.99"),  # #prices-new (MSI's OpenCart theme)
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


# =========================================================================== NVIDIA marketplace in the browser
# 2026-09-29 18:02 headed Chrome run: plain GETs of marketplace.nvidia.com and api.store.nvidia.com got Akamai
# "Access Denied" (403); the browser-loaded page (recorded, 539 KB) rendered with its "Out of Stock" button.

NV_5090 = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/nvidia-geforce-rtx-5090/"


def _nv_browser(monkeypatch, captured: list[dict]) -> list:
    calls: list = []

    async def fake_browser_fetch(url, *, capture=None):
        calls.append((url, capture))
        return fetcher.FetchResult(url=url, status=200, via_browser=True, captured=captured,
                                   text=body("nvidia/marketplace_rtx5090_browser.html"))

    monkeypatch.setenv("ENABLE_BROWSER", "true")
    monkeypatch.setattr(fetcher, "browser_fetch", fake_browser_fetch)
    electronics._nv_page_cache.clear()
    return calls


def _inv(active: str, price: str = "1999.00") -> dict:  # feinventory's documented listMap shape
    return {"success": True, "map": None, "listMap": [
        {"is_active": active, "product_url": NV_5090, "price": price, "fe_sku": "NVGFT590_US", "locale": "US"}]}


@pytest.mark.parametrize("active,status", [("true", "in_stock"), ("false", "out_of_stock")])
async def test_nvidia_marketplace_reads_the_pages_own_inventory_call(monkeypatch, active, status):
    captured = [{"url": "https://api.store.nvidia.com/partner/v1/feinventory?skus=NVGFT590&locale=en-us",
                 "status": 200, "body": json.dumps(_inv(active))}]
    calls = _nv_browser(monkeypatch, captured)
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5090.json"}}) as http:
        res = await check(NV_5090)
    assert res.status == status and res.detail["inventory_via"] == "browser"
    assert res.detail["sku"] == "NVGFT590" and res.price == "$1,999.00"
    assert calls[0][0] == NV_5090 and calls[0][1]("https://api.store.nvidia.com/partner/v1/feinventory?skus=X")
    assert not any("api.store.nvidia.com" in c for c in http)  # no doomed plain request (Akamai 403)


async def test_nvidia_marketplace_no_capture_uses_rendered_page(monkeypatch):
    _nv_browser(monkeypatch, [])
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5090.json"},
                 NV_INV: {"status": 403, "body": "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD></HTML>"}}):
        res = await check(NV_5090)
    assert res.status == "out_of_stock" and res.status_text == "Out of stock"
    assert res.detail["matched"] == "rendered page: 'Out of Stock' button shown"
    assert res.title == "NVIDIA GeForce RTX 5090" and res.price == "$1,999.00"
    html = body("nvidia/marketplace_rtx5090_browser.html")
    shown = html.replace('style="display: none;" id="form-action-addToCart"',
                         'style="display: block;" id="form-action-addToCart"').replace(" stock-grey-out", "")
    assert shown != html and electronics.nv_dom_state(shown)[0] == "in"


# 2026-09-29 19:53 sweep (sites.local.json from `discover --write`): partner cards on the marketplace. The PNY
# page's JSON-LD mpn ("VCG5060T8DFXPB1-O") has a dash, no FE SKU exists for a Ti, so the adapter returned None
# and the generic checker called it in stock from a bare "Add to Cart" button (no price). The MSI page got
# the RTX 5080 *Founders Edition* SKU guessed for it.
NV_PNY = ("https://marketplace.nvidia.com/en-us/consumer/graphics-cards/"
          "pny-geforce-rtx-5060-ti-8gb-overclocked-dual-fan-graphics-card/")
NV_MSI = "https://marketplace.nvidia.com/en-us/consumer/graphics-cards/msi-geforce-rtx-5080-16g-ventus-3x-oc-black/"


def _nv_page(monkeypatch, html: str) -> list:
    calls: list = []

    async def fake_browser_fetch(url, *, capture=None):
        calls.append(url)
        return fetcher.FetchResult(url=url, status=200, via_browser=True, captured=[], text=html)

    monkeypatch.setenv("ENABLE_BROWSER", "true")
    monkeypatch.setattr(fetcher, "browser_fetch", fake_browser_fetch)
    electronics._nv_page_cache.clear()
    return calls


async def test_nvidia_partner_card_sold_by_marketplace_reads_the_rendered_page(monkeypatch):
    _nv_page(monkeypatch, body("nvidia/marketplace_pny_5060ti_browser.html"))
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5060ti.json"}, NV_INV: {"status": 403, "body": ""}}) as http:
        res = await check(NV_PNY)
    assert res.status == "in_stock" and res.detail["adapter"] == "nvidia"
    assert res.detail["matched"] == "rendered page: #form-action-addToCart shown (feinventory is_active)"
    assert res.price == "$499.99" and res.title == "PNY GeForce RTX 5060 Ti 8GB Overclocked Dual Fan Graphics Card"
    assert res.detail["sku"] == "VCG5060T8DFXPB1-O" and res.detail["fulfilled_by"] == "PNY"
    assert res.detail["partner_card"] is True and not res.detail.get("sku_guessed")
    assert not any("feinventory" in c or "api.nvidia.partners" in c for c in http)  # never the FE's answer


async def test_nvidia_partner_card_is_never_a_generic_in_stock(monkeypatch):
    html = body("nvidia/marketplace_pny_5060ti_browser.html")
    # the same page before / without its inventory answer: the button greyed out, no OOS or retailer box
    grey = html.replace('style="display: block;" data-wait-message="Checkout" class="productView__button--add-to-cart '
                        'button button--primary basic__button clearfix cta-button js-add-button"',
                        'style="display: block;" data-wait-message="Checkout" class="productView__button--add-to-cart '
                        'button button--primary basic__button clearfix cta-button stock-grey-out"')
    assert grey != html
    assert generic.analyze(grey, NV_PNY).status == "in_stock"  # what the generic checker would have said
    _nv_page(monkeypatch, grey)
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5060ti.json"}}):
        res = await check(NV_PNY)
    assert res.status == "unknown" and res.status_text == electronics.NV_UNREAD_TEXT
    assert res.detail["adapter"] == "nvidia" and res.detail["generic_said"] == "in_stock"


async def test_nvidia_marketplace_blocked_without_browser_is_not_in_stock():
    akamai = "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1></BODY></HTML>"
    electronics._nv_page_cache.clear()
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5060ti.json"}, NV_PNY: {"status": 403, "body": akamai}}):
        res = await check(NV_PNY)
    assert res.status != "in_stock"


async def test_nvidia_partner_card_retailer_only_never_gets_the_fe_sku(monkeypatch):
    _nv_page(monkeypatch, body("nvidia/marketplace_msi_5080_browser.html"))
    with replay({NV_SEARCH: {"body": "nvidia/search_rtx5080.json"}, NV_INV: {"body": _inv("true")}}) as http:
        res = await check(NV_MSI)
    assert res.status == "out_of_stock" and res.status_text == "Retailers only (check availability)"
    assert res.detail["sku"] == "G5080-16V3CB" and "sku_guessed" not in res.detail
    assert res.price == "$1,679.99" and res.detail["seller"] is None
    assert not any("NVGFT580" in c for c in http)


# =========================================================================== 2026-09-29 18:0x headed-Chrome run


async def test_kroger_page_state_with_no_products_is_not_sold_at_store(monkeypatch):
    monkeypatch.delenv("KROGER_CLIENT_ID", raising=False)
    url = "https://www.kroger.com/p/pokemon-trading-card-game-scarlet-violet-booster-pack/0082050005556"
    # Visible page: "Product Unavailable — Item details didn't load"; its __INITIAL_STATE__ has Kroger's own
    # server-side lookup (pdpSSR) for the default pickup store (Hayes Kroger) with products: [].
    with replay({url: {"body": "kroger/pdp_0082050005556_no_products.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock", (res.status_text, res.error)
    assert res.status_text == "Not sold at Hayes Kroger (Kroger lists no item for this UPC)"
    assert res.detail["store_id"] == "02900577" and res.detail["source"] == "page-state"


async def test_homedepot_not_currently_available_page():
    url = ("https://www.homedepot.com/p/RYOBI-ONE-18V-Cordless-3-8-in-Drill-Driver-Kit-with-1-5-Ah-Battery-and-"
           "Charger-PCL206K1/315143462")
    with replay({url: {"body": "homedepot/not_currently_available.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock" and res.status_text == "Not currently available on homedepot.com"


async def test_adorama_head_only_datadome_page_is_blocked():
    url = "https://www.adorama.com/ifjx100vs.html"
    html = body("adorama/datadome_head_only.html")
    assert "ddjskey" in html and "<body" not in html  # the real answer: <head> only
    with replay({url: {"body": "adorama/datadome_head_only.html"}}):
        res = await check(url)
    assert res.status == "error" and res.status_text == "Blocked by bot protection"
    assert res.error.startswith("Blocked by bot protection on adorama.com (DataDome")


async def test_antonline_no_results_page_is_dead_link():
    url = "https://www.antonline.com/Sony/Electronics/Gaming_Devices/Gaming_Consoles/1520631"
    with replay({url: {"body": "antonline/no_results.html"}}):
        res = await check(url)
    assert res.status == "error" and res.status_text == generic.NOT_FOUND_TEXT and res.detail["dead_link"]
    # a tracked *search* URL with no results is not a dead product link
    assert generic.missing_page("https://www.antonline.com/search?q=ps6", None,
                                "<title>No Results - antonline.com</title>") is None


async def test_stockx_catchall_404_is_dead_link():
    url = "https://stockx.com/nintendo-switch-2-console-us-version"
    with replay({url: {"body": "stockx/catchall_404.html"}}):
        res = await check(url)
    assert res.status == "error" and res.status_text == generic.NOT_FOUND_TEXT


async def test_ebay_unknown_item_is_listing_not_found():
    url = "https://www.ebay.com/itm/387123456789"
    # plain request: 403 "Error Page | eBay"; the browser: "Discover error | eBay" / "We looked everywhere!"
    with replay({url: {"body": "ebay/discover_error.html"}}):
        res = await check(url)
    assert res.status == "error" and res.status_text == "Listing not found" and res.detail["dead_link"]


async def test_microcenter_without_store_is_online_stock():
    url = ("https://www.microcenter.com/product/687907/amd-ryzen-7-9800x3d-granite-ridge-am5-470ghz-8-core-boxed-"
           "processor-heatsink-not-included")
    with replay({url: {"body": "microcenter/r7_9800x3d_shippable.html"}}) as calls:
        res = await check(url)
    assert res.status == "in_stock", (res.status_text, res.error, calls.missing)
    assert res.status_text == "In stock online (ships)" and res.price == "$429.99"
    assert res.detail["online"] is True and res.detail["store_id"] is None


# GameFly ignores the slug (id 5022850 is a Hori screen protector, $11.29 — the "in stock $11.29" of both runs
# was that product's); B&H redirects a reused id to an open-box iPod touch; Pokémon Center / Kohl's /
# Play-Asia now name another product in their canonical / redirect URL for the same id; Newegg's item
# 14-137-917 is an RTX 5090, not the RTX 5070 Ti the slug names.
@pytest.mark.parametrize("url,routes,other", [
    ("https://www.gamefly.com/game/mario-kart-world/5022850",
     {"https://www.gamefly.com/game/mario-kart-world/5022850": {"body": "gamefly/product.html"}},
     "Hori Screen Protector Filter for Nintendo Switch"),
    ("https://www.bhphotovideo.com/c/product/1809440-REG/fujifilm_16821474_x100vi_digital_camera_silver.html",
     {"https://www.bhphotovideo.com/c/product/1809440-REG/fujifilm_16821474_x100vi_digital_camera_silver.html": {
         "body": "bhphoto/redirect_1809440_ipod_touch.html",
         "final": "https://www.bhphotovideo.com/c/product/1809440-REG/apple_mvhw2ll_a_ob_ipod_touch_7th.html"}},
     "Apple 32GB iPod touch"),
    ("https://www.pokemoncenter.com/product/10-10185-101/pokemon-tcg-scarlet-and-violet-prismatic-evolutions-elite-"
     "trainer-box", {"https://www.pokemoncenter.com/product/10-10185-101/*": {
         "body": "pokemoncenter/10-10185-101_build_battle_box.html"}},
     "Pokémon TCG: Mega Evolution-Phantasmal Flames Build & Battle Box"),
    ("https://www.kohls.com/product/prd-6589434/lego-icons-orchid-plant-decor-building-set-10311.jsp",
     {"https://www.kohls.com/product/prd-6589434/*": {"body": "kohls/prd_6589434_hoodie.html"}},
     "Toddler Black Cincinnati Bengals Logo Pullover Hoodie"),
    ("https://www.play-asia.com/mario-kart-world/13/70gk5t",
     {"https://www.play-asia.com/mario-kart-world/13/70gk5t": {
         "body": "playasia/70gk5t_edf_world_brothers_2.html",
         "final": "https://www.play-asia.com/en/earth-defense-force-world-brothers-2/13/70gk5t"}},
     "Earth Defense Force: World Brothers 2"),
    ("https://www.newegg.com/msi-geforce-rtx-5070-ti-rtx-5070-ti-16g-gaming-trio-oc/p/N82E16814137917",
     {"https://www.newegg.com/product/api/ProductRealtime?ItemNumber=14-137-917": {
         "body": "newegg/realtime_14-137-917.json"}},
     "MSI GeForce RTX 5090 32G VANGUARD SOC"),
])
async def test_link_showing_another_product_is_stale(url, routes, other):
    with replay(routes) as calls:
        res = await check(url)
    assert res.status == "error", (res.status, res.status_text, calls.missing)
    assert res.status_text.startswith(f"{generic.OTHER_PRODUCT_TEXT} ({other[:40]}")
    assert res.detail["dead_link"] is True and res.price is None


@pytest.mark.parametrize("url,title", [
    ("https://www.gigabyte.com/Graphics-Card/GV-N5090GAMING-OC-32GD", "GeForce RTX™ 5090 GAMING OC 32G Graphics Card"),
    ("https://www.lego.com/en-us/product/the-botanical-collection-orchid-10311", "Orchid"),
    ("https://www.lego.com/en-us/product/millennium-falcon-75192", "Millennium Falcon™"),
    ("https://www.walmart.com/ip/Nintendo-Switch-2-Console/15949610846", "Nintendo Switch™ 2 System"),
    ("https://www.target.com/p/nintendo-switch-2-console-choose-your-game-bundle/-/A-1011032706",
     "Nintendo Switch 2 Console"),
    ("https://www.lg.com/us/tvs/lg-oled65c5pua-oled-4k-tv", "65 Inch Class LG OLED evo AI C5 4K Smart TV 2025"),
])
def test_same_product_is_not_flagged(url, title):
    from app.checkers.base import CheckResult
    res = CheckResult(status="in_stock", status_text="In stock", available=[], title=title, detail={})
    assert generic.other_product(url, res) is None


async def test_lg_url_model_variant_decides_not_any_variant():
    # 2026-09-29 18:02: the ProductGroup lists 42"–83"; only the 77" (OLED77C5PUA, $2,199.99) is InStock, the
    # 65" the URL names (OLED65C5PUA) is OutOfStock at $1,499.99. The old verdict was "In stock $2,199.99".
    url = "https://www.lg.com/us/tvs/lg-oled65c5pua-oled-4k-tv"
    with replay({url: {"body": "lg/oled65c5_r2_65in_oos.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock" and res.price == "$1,499.99"
    assert "json-ld: URL selects variant OLED65C5PUA" in res.detail["signals"]


async def test_nyxi_price_is_the_one_the_page_shows():
    # JSON-LD says GBP 90 (the Shopline session cookie had currency_code=GBP / localization=GB); the page
    # renders $119.16 (90 / 0.755294) to the US visitor — that's the price to report.
    url = "https://nyxigame.com/products/nyxi-hyperion-3-wireless-joypad-for-switch-2"
    with replay({url: {"body": "nyxi/hyperion3_gbp_jsonld.html"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == "$119.16"
    assert res.detail["structured_price"] == "£90.00"


async def test_samsclub_unknown_item_is_not_found_not_out_of_stock():
    # __NEXT_DATA__ carries a hollow product (no name / usItemId, availabilityStatus OUT_OF_STOCK) with
    # upstreamErrorCode 404.IRO.ITEMSTOREPRODUCT...; the page reads "Uh-oh... This page could not be found."
    url = "https://www.samsclub.com/ip/Nintendo-Switch-2-Mario-Kart-World-Bundle/16634389868"
    with replay({url: {"body": "samsclub/item_16634389868_not_found.html"}}):
        res = await check(url)
    assert res.status == "error" and res.status_text == generic.NOT_FOUND_TEXT and res.detail["dead_link"]


# =========================================================================== verdict audit, 2026-09-29 19:0x run
# (discovered links, headed Chrome, home connection): results that were wrong on the real pages


async def test_qvc_tailwind_disabled_variant_is_not_a_disabled_button():
    # QVC's enabled "Add to Cart" carries Tailwind's "disabled:pointer-events-none disabled:opacity-50" (styles
    # for the disabled *state*); read as a disabled button it turned a JSON-LD InStock page into "Out of stock"
    url = "https://www.qvc.com/qvc.product.K339228.html"
    with replay({url: {"body": "qvc/K339228_tailwind_atc.html"}}) as calls:
        res = await check(url)
    assert res.status == "in_stock", (res.status_text, calls.missing)
    assert res.price == "$199.98" and res.title.startswith("Blackstone 22")
    from bs4 import BeautifulSoup
    btn = BeautifulSoup('<button class="disabled:opacity-50 [&:disabled]:x">Add to Cart</button>', "lxml").button
    assert generic._is_disabled(btn) is False


async def test_gamestop_pre_owned_only_is_not_new_stock():
    # The page sells the battery pack Pre-Owned only (gtmdata condition "Pre-Owned", JSON-LD UsedCondition
    # InStock $5.99): watching New (the default), that's not stock — and $5.99 is not the New price.
    url = ("https://www.gamestop.com/gaming-accessories/batteries/xbox-one/products/"
           "microsoft-xbox-one-battery-pack/100820.html")
    with replay({url: {"body": "gamestop/100820_pre_owned_only.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock" and res.status_text == "No New copies — only Pre-Owned"
    assert res.price is None and res.detail["other_condition_price"] == "$5.99"
    with replay({url: {"body": "gamestop/100820_pre_owned_only.html"}}):
        res = await check(url, {"condition": "any"})
    assert res.status == "in_stock" and res.price == "$5.99" and res.detail["condition"] == "Pre-Owned"


async def test_bjs_reads_product_state_not_stale_head():
    # <title>/og:title named another product (an Amairah engagement ring) and no price was rendered; the
    # page's __CSR_DATA__ has the bridal set's name, minItemPrice and per-size availInventory.
    url = ("https://www.bjs.com/product/05-ct-tw-diamond-cushion-shape-double-halo-3-pc-bridal-set-in-sterling-silver/"
           "3000000000003409747/")
    with replay({url: {"body": "bjs/bridal_set_csr_state.html"}}) as calls:
        res = await check(url)
    assert res.status == "in_stock", (res.status_text, res.error, calls.missing)
    assert res.price == "$599.99" and res.title.startswith("0.5 ct. t.w. Diamond Cushion Shape Double Halo")
    assert res.detail["matched"] == "__CSR_DATA__ bjsItemsInventory: 4 of 4 item(s) available"


async def test_zotac_repeated_microdata_name_is_one_title():
    # two itemprop="name" (title + "[Refurbished]" variant) came back as "['ZOTAC ...', 'ZOTAC ... [Refurbished]']"
    url = "https://www.zotacstore.com/us/geforce-rtx-3060-ti-amp-white-edition-lhr-refurbished"
    with replay({url: {"body": "zotac/rtx3060ti_refurb.html"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == "$323.99"
    assert res.title == "ZOTAC GAMING GeForce RTX 3060 Ti AMP White Edition LHR"


async def test_walmart_third_party_price_is_not_the_items_price():
    # buy box: HyperTech (EXTERNAL) at $499.00 — a marketplace price, not Walmart's
    url = "https://www.walmart.com/ip/Nintendo-Switch-2-Console/15949610846"
    with replay({url: {"body": "walmart/switch2_third_party.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.price is None and res.detail["third_party_price"] == 499
    assert res.detail["seller"] == "HyperTech" and res.detail["third_party"] is True


async def test_target_shipping_out_but_on_the_shelf_nearby(_target_state):
    # OLIPOP minis, delivery watched: shipping_options OUT_OF_STOCK (INVENTORY_UNAVAILABLE) while same-day
    # delivery and pickup at the page's store (3363) are IN_STOCK — out, but not a bare "Out of stock"
    url = "https://www.target.com/p/olipop-apple-crisp-45-fl-oz-6pk-mini-39-s/-/A-95178842"
    with replay({
        "https://www.target.com/p/-/A-95178842": {"body": "target/pdp_95178842.html"},
        REDSKY + "pdp_client_v1*": {"body": "target/pdp_client_95178842.json"},
        REDSKY + "product_fulfillment_v1*": {"body": "target/fulfillment_95178842_ship_oos.json"},
    }) as calls:
        res = await check(url)
    assert res.status == "out_of_stock", (res.status_text, res.error, calls.missing)
    assert res.status_text == "Out of stock for shipping (only same-day delivery / store pickup)"
    assert res.price == "$11.39" and res.detail["other_fulfillment"] == ["same-day delivery", "store pickup"]


# =========================================================================== 2026-09-29 19:53 sweep audit
# (headed Chrome on the user's Mac, sample URLs from sites.local.json) — suspicious OK results checked
# against their recordings.


async def test_asus_configurable_product_price_is_not_starting_at_zero():
    # "Starting at $0.00" (data-price-amount="0") on a configurable laptop; the JSON-LD offer has $4,299.99
    url = "https://eshop.asus.com/us/rog-strix-scar-18-2026-gaming-laptop.html"
    with replay({url: {"body": "asus/scar18_configurable_oos.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock" and res.price == "$4,299.99"
    assert "Notify me" in body("asus/scar18_configurable_oos.html")  # really sold out


async def test_asus_disabled_add_to_cart_is_static_markup_not_stock():
    # eshop.asus.com renders #product-addtocart-button disabled for every product until its JS runs; this one's
    # JSON-LD says InStock and the page has no "Notify me" / out-of-stock markup (the probe said out of stock)
    url = "https://eshop.asus.com/us/90nx0631-m003t0-asus-chromebook-cm14-cm1402c.html"
    html = body("asus/cm1402c_in_stock.html")
    assert 'id="product-addtocart-button" disabled' in html
    with replay({url: {"body": "asus/cm1402c_in_stock.html"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == "$299.99"


@pytest.mark.parametrize("url,fixture,price", [
    ("https://www.homedepot.com/p/American-Standard-Rumson-2-Piece-1-28-GPF-Single-Flush-Elongated-Toilet-in-White-"
     "Seat-is-Included-719AA101-020/323484855", "homedepot/pdp_323484855_toilet.html", "$159.00"),
    ("https://www.homedepot.com/p/Fire-TV-Stick-HD-Newest-Model-Free-and-Live-TV-Alexa-Voice-Remote-Powered-by-the-"
     "TV-Effortless-Setup-B0DJGDC3BD/341824384", "homedepot/pdp_341824384_firetv.html", "$15.99"),
])
async def test_homedepot_price_from_json_ld_offer_without_availability(url, fixture, price):
    # the JSON-LD Offer has a price but no availability: the price was dropped with it. Both really are in stock
    # ("22 in stock" for pickup / "2,846 available" for delivery on the recorded pages)
    with replay({url: {"body": fixture}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == price


async def test_dell_price_shown_without_structured_data():
    # the monitor's page has no priced JSON-LD, only the rendered "Dell Price $419.99"
    url = "https://www.dell.com/en-us/shop/monitors/apd/dell-34-plus-usb-c-monitor-s3425dw/s3425dw_monitor/-"
    with replay({url: {"body": "dell/apd_s3425dw_monitor.html"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.price == "$419.99"


async def test_verizon_full_retail_price():
    url = "https://www.verizon.com/smartphones/apple-iphone-17-pro/"
    with replay({url: {"body": "verizon/iphone17pro_rendered.html"}}):
        res = await check(url)
    assert res.status == "in_stock" and res.status_text == "In stock (Ships between Wed, Sep 30 - Tue, Oct 6)"
    assert res.price == "$1,099.99"  # "Pay in full today, $1,099.99" — the 256 GB model's retail price


@pytest.mark.parametrize("tcin,fixture,seller", [
    ("90253281", "target/pdp_client_90253281_marketplace.json", "The R Group"),
    ("1008318553", "target/pdp_client_1008318553_marketplace.json", "232 Inc."),
])
async def test_target_plus_items_are_third_party(_target_state, tcin, fixture, seller):
    # both "Third-party sellers only" verdicts are right: pdp_client says fulfillment.is_marketplace = true,
    # relationship_type_code "SA", product_vendors = the Target Plus partner
    data = json.loads(body(fixture))
    assert "is_marketplace\":true" in body(fixture).replace(" ", "")
    target._key_cache.update(key="9f36aeafbe60771e321a7cc95a78140772ab3e96", exp=9e18,
                             loc={"store_id": "3363", "zip": "02122", "state": "MA"})
    with replay({REDSKY + "pdp_client_v1*": {"body": data},
                 REDSKY + "product_fulfillment_v1*": {"body": TARGET_FULFILLMENT_IN}}):
        res = await check(f"https://www.target.com/p/-/A-{tcin}")
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["seller"] == seller and res.detail["third_party"] is True


async def test_walmart_marketplace_only_item():
    # QQV Official Store sells it (sellerName on every offer); Walmart.com isn't a seller: right verdict
    url = ("https://www.walmart.com/ip/QQV-PS5-Stand-Cooling-Station-For-PS5-Slim-and-Disc-Digital-editions-Console-"
           "PS5-Accessories/1098986296")
    with replay({url: {"body": "walmart/qqv_ps5_stand_marketplace.html"}}):
        res = await check(url)
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["seller"] == "QQV Official Store" and res.price is None


async def test_psdirect_code_less_menu_link_reads_the_page_code():
    # PS Direct's menus (recorded 404 page, 2026-09-29) link code-less pages such as .../playstation5-pro-console-2-tb.
    # Synthetic page in the recorded markup's shape (data-product-code on the product component) and a
    # synthetic productList answer in the recorded API's shape.
    from app.checkers.retailers import games

    real_404 = body("psdirect/page_not_found_404.html")
    assert 'data-product-code=""' in real_404 and games.psdirect_page_code(real_404) is None  # empty data-product-code="" components don't count
    url = "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console-2-tb"
    page = ('<html><head><title>PS5 Pro Console 2TB</title></head><body><h1>PlayStation 5 Pro Console</h1>'
            '<div class="productHero-component" data-product-code="1000045123" data-products-url="https://api.direct.'
            'playstation.com/commercewebservices/ps-direct-us/users/:userId/products/productList?fields=BASIC&amp;'
            'productCodes="></div></body></html>')
    api = {"products": [{"code": "1000045123", "name": "PlayStation 5 Pro Console", "price": {"value": 749.99},
                         "stock": {"stockLevelStatus": "outOfStock"}}]}
    with replay({url: {"body": page}, "https://api.direct.playstation.com/commercewebservices/*": {"body": api}}) as calls:
        res = await check(url)
    assert res.status == "out_of_stock" and res.price == "$749.99" and res.detail["code_from"] == "page"
    assert any("productCodes=1000045123" in c for c in calls)
    assert games.psdirect_page_code(page.replace("</body>", '<div data-product-code="1000099999"></div></body>')) is None


_PSD_SLUG_URL = "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console-2-tb"
_PSD_API_OOS = {"products": [{"code": "3009726", "name": "PlayStation 5 Pro Console", "price": {"value": 899.0},
                              "stock": {"stockLevelStatus": "outOfStock"}}]}


@pytest.mark.parametrize("page", [
    # JSON in a script (productCode) with other products' empty/absent data attributes
    '<html><body><div data-product-code=""></div><script>window.pdp={"productCode":"3009726","name":"PS5 Pro"}</script></body></html>',
    # canonical link with the code
    '<html><head><link rel="canonical" href="https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console.3009726"/></head><body></body></html>',
    # a link to this very slug with the code, among menu links to other products
    '<html><body><a href="/en-us/buy-consoles/playstation5-pro-console-2-tb.3009726">Pro</a>'
    '<a href="/en-us/buy-consoles/playstation5-console-1-tb.3006222">Base</a></body></html>',
    # the API URL the page's script builds
    '<html><body><div data-products-url="https://api.direct.playstation.com/commercewebservices/ps-direct-us/users/:userId/products/productList?fields=BASIC&amp;productCodes=3009726"></div></body></html>',
])
async def test_psdirect_code_less_page_code_in_other_shapes(page):
    with replay({_PSD_SLUG_URL: {"body": page}, "https://api.direct.playstation.com/commercewebservices/*": {"body": _PSD_API_OOS}}) as calls:
        res = await check(_PSD_SLUG_URL)
    assert res.status == "out_of_stock" and res.price == "$899.00" and res.detail["code_from"] == "page"
    assert any("productCodes=3009726" in c for c in calls)


@pytest.mark.parametrize("page", [
    '<html><head><title>PlayStation 5 Pro Console (2 TB)</title></head><body><h1>PlayStation 5 Pro Console 2 TB</h1><p>Shop now</p>'
    + '<p>PlayStation Direct: consoles, accessories and more, shipped by Sony Interactive Entertainment.</p>' * 20
    + '</body></html>',
    # a page full of other products' cards: nothing to pick the right one from
    '<html><body><div data-product-code="3009726"></div><div data-product-code="3006222"></div></body></html>',
])
async def test_psdirect_code_less_page_without_a_code_says_so(page):
    with replay({_PSD_SLUG_URL: {"body": page}}) as calls:
        res = await check(_PSD_SLUG_URL)
    print(res);     assert res.status == "unknown" and res.status_text == "Couldn't find the PS Direct product code on this page"
    assert not any("productList" in c for c in calls)
