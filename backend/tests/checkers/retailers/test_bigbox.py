"""DOM-based big-box adapters (bigbox.py): site vocabulary over the generic checker, StockX, eBay."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.checkers import fetcher
from app.checkers.fetcher import FetchError, FetchResult
from app.checkers.retailers import AdapterContext, RetailerConfig, bigbox, load_adapter, match_retailer, retailer_by_key

FIX = Path(__file__).parent.parent / "fixtures" / "retailers" / "bigbox"

MY_ADAPTERS = ["target:check", "bestbuy:check", "amazon:check", "walmart:check", "kroger:check"] + [
    f"bigbox:{f}" for f in ("bjs", "costco", "homedepot", "kohls", "meijer", "officedepot", "qvc", "stockx",
                            "toysrus", "verizon", "ebay")]


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def ctx(key: str, **rc) -> AdapterContext:
    return AdapterContext(retailer=retailer_by_key(key), retailer_config=RetailerConfig.from_dict(rc))


def serve(monkeypatch, pages: dict[str, str] | str, **kw) -> list:
    """Stub fetch_html: a single HTML string for every URL, or {url_substring: html}."""
    calls: list = []

    async def fake(url, render_js=False, *, needs=None):
        calls.append((url, render_js))
        html = pages if isinstance(pages, str) else next(v for k, v in pages.items() if k in url)
        return FetchResult(url=url, status=200, text=html, **kw)

    monkeypatch.setattr(fetcher, "fetch_html", fake)
    return calls


@pytest.mark.parametrize("spec", MY_ADAPTERS)
def test_registry_specs_resolve(spec):
    assert load_adapter(spec) is not None, spec


def test_registry_points_here():
    assert match_retailer("https://www.qvc.com/x.product.K12345.html").adapter == "bigbox:qvc"
    assert match_retailer("https://www.samsclub.com/ip/x/1").adapter == "walmart:check"


# ------------------------------------------------------------------ site vocabulary

async def test_qvc_advanced_order_is_preorder(monkeypatch):
    serve(monkeypatch, fx("qvc_advanced_order.html"))
    res = await bigbox.qvc("https://www.qvc.com/dyson-airwrap.product.A123456.html", ctx("qvc"))
    assert res.status == "in_stock" and res.status_text == "Pre-order (Advanced Order)"
    assert res.title == "Dyson Airwrap i.d. Multi-Styler and Dryer"
    assert res.detail["retailer"] == "qvc" and res.detail["adapter"] == "qvc"


async def test_qvc_join_waitlist_is_out(monkeypatch):
    serve(monkeypatch, fx("qvc_advanced_order.html").replace(
        '<button id="btnAddToCart" class="btn btnAdd">Advanced Order</button>',
        '<button class="btn btnWaitlist">Join Waitlist</button>').replace("This item will ship by October 20", ""))
    res = await bigbox.qvc("https://www.qvc.com/x.product.A123456.html", ctx("qvc"))
    assert res.status == "out_of_stock" and res.status_text == "Waitlist only"


async def test_verizon_backordered_is_orderable(monkeypatch):
    serve(monkeypatch, fx("verizon_backordered.html"))
    res = await bigbox.verizon("https://www.verizon.com/smartphones/apple-iphone-17-pro-max/", ctx("verizon"))
    assert res.status == "in_stock" and res.status_text == "Backordered (orderable)"


async def test_costco_button_beats_stale_jsonld(monkeypatch):
    serve(monkeypatch, fx("costco_oos.html"))
    res = await bigbox.costco("https://www.costco.com/pokemon-holiday-bundle.product.4000123456.html", ctx("costco"))
    assert res.status == "out_of_stock" and res.status_text == "Out of stock"
    assert res.detail["price_value"] == 89.99 and res.title == "Pokemon TCG Holiday Bundle"
    serve(monkeypatch, fx("costco_oos.html").replace('value="Out of Stock" disabled', 'value="Add to Cart"'))
    res = await bigbox.costco("https://www.costco.com/x.product.4000123456.html", ctx("costco"))
    assert res.status == "in_stock"


async def test_homedepot_discontinued(monkeypatch):
    serve(monkeypatch, fx("homedepot_discontinued.html"))
    res = await bigbox.homedepot("https://www.homedepot.com/p/DEWALT-Drill/204279858", ctx("homedepot"))
    assert res.status == "out_of_stock" and res.status_text == "Discontinued"


@pytest.mark.parametrize("fn,key,html,status,text", [
    ("bjs", "bjs", "<main><h1>Switch 2</h1><button class='btn'>Add to Cart</button></main>", "in_stock", "In stock"),
    ("bjs", "bjs", "<main><h1>Switch 2</h1><button disabled>Add to Cart</button><p>This item is currently out of "
                   "stock</p></main>", "out_of_stock", "Out of stock"),
    ("kohls", "kohls", "<main><h1>Squishmallow</h1><p>This product is out of stock.</p></main>", "out_of_stock",
     "Out of stock"),
    ("meijer", "meijer", "<main><h1>Pokemon ETB</h1><button>Add to Cart</button></main>", "in_stock", "In stock"),
    ("officedepot", "officedepot", "<main><h1>Chair</h1><p>This item is no longer available</p></main>",
     "out_of_stock", "No longer available"),
])
async def test_phrase_rules(monkeypatch, fn, key, html, status, text):
    serve(monkeypatch, f"<html><body>{html}</body></html>")
    res = await getattr(bigbox, fn)(f"https://www.{key}.com/p/item/1", ctx(key))
    assert (res.status, res.status_text) == (status, text)


async def test_inconclusive_page_uses_generic_structured_data(monkeypatch):
    serve(monkeypatch, """<html><head><script type="application/ld+json">{"@type":"Product","name":"Tote",
        "offers":{"@type":"Offer","price":"29.99","priceCurrency":"USD","availability":"https://schema.org/InStock"}}
        </script></head><body><main><h1>Tote</h1></main></body></html>""")
    res = await bigbox.kohls("https://www.kohls.com/product/prd-123/tote.jsp", ctx("kohls"))
    assert res.status == "in_stock" and res.detail["price_value"] == 29.99
    assert "json-ld" in (res.detail.get("matched") or "")


async def test_item_render_js_switch_is_honoured(monkeypatch):
    calls = serve(monkeypatch, "<main><button>Add to Cart</button></main>")
    c = ctx("bjs")
    c.generic_config = {"render_js": True}
    await bigbox.bjs("https://www.bjs.com/product/x/123", c)
    assert calls[-1][1] is True


async def test_waiting_room(monkeypatch):
    if "queued" not in FetchResult.__dataclass_fields__:
        pytest.skip("fetcher has no waiting-room flag")
    serve(monkeypatch, "<html>You are now in line</html>", queued=True)
    res = await bigbox.homedepot("https://www.homedepot.com/p/x/1", ctx("homedepot"))
    assert res.status == "unknown" and res.detail["queue"] is True
    assert res.status_text == "Waiting room active — drop may be live"


async def test_toysrus_follows_macys_link(monkeypatch):
    calls = serve(monkeypatch, {"toysrus.com": fx("toysrus_landing.html"), "macys.com": fx("macys_product.html")})
    res = await bigbox.toysrus("https://www.toysrus.com/lego-icons-titanic-10294", ctx("toysrus"))
    assert calls[-1][0] == "https://www.macys.com/shop/product/lego-icons-titanic-10294?ID=12345678&tdp=cm_app"
    assert res.status == "in_stock" and res.detail["price_value"] == 679.99
    assert res.detail["followed"].startswith("https://www.macys.com/")


# ------------------------------------------------------------------ StockX

async def test_stockx_lowest_ask(monkeypatch):
    serve(monkeypatch, fx("stockx_product.html"))
    res = await bigbox.stockx("https://stockx.com/pokemon-tcg-prismatic-evolutions-elite-trainer-box", ctx("stockx"))
    assert res.status == "in_stock" and res.status_text == "Lowest ask $98.00"
    assert res.detail["price_value"] == 98.0 and res.detail["third_party"] is True


async def test_stockx_no_asks(monkeypatch):
    serve(monkeypatch, fx("stockx_product.html").replace('{"amount":98,"chainId":"1"}', "null").replace(
        '{"amount":98}', "null"))
    res = await bigbox.stockx("https://stockx.com/x", ctx("stockx"))
    assert res.status == "out_of_stock" and res.status_text == "No asks right now"


async def test_stockx_without_next_data_is_generic(monkeypatch):
    serve(monkeypatch, "<html><body><main><h1>Sneaker</h1><p>Some description text for this product page.</p>"
                       "</main></body></html>")
    res = await bigbox.stockx("https://stockx.com/x", ctx("stockx"))
    assert res.status == "unknown" and res.detail["retailer"] == "stockx"


# ------------------------------------------------------------------ eBay

EBAY = "https://www.ebay.com/itm/Sony-PlayStation-5-Pro/226543210987?hash=item"


async def test_ebay_listing_ended_beats_jsonld(monkeypatch):
    serve(monkeypatch, fx("ebay_ended.html"))
    res = await bigbox.ebay(EBAY, ctx("ebay"))
    assert res.status == "out_of_stock" and res.status_text == "Listing ended"
    assert res.detail["item_id"] == "226543210987" and res.detail["condition"] == "New"


async def test_ebay_buy_it_now_used(monkeypatch):
    serve(monkeypatch, fx("ebay_bin_used.html"))
    res = await bigbox.ebay(EBAY, ctx("ebay"))
    assert res.status == "in_stock" and res.status_text == "Buy It Now · Used"
    assert res.detail["condition"] == "Used" and res.detail["quantity"] == "3 available"
    assert res.detail["seller"] == "gamingdeals_usa" and res.detail["price_value"] == 599.99


async def test_ebay_sold_out(monkeypatch):
    serve(monkeypatch, fx("ebay_soldout.html"))
    res = await bigbox.ebay(EBAY, ctx("ebay"))
    assert res.status == "out_of_stock" and res.status_text == "Sold out"


async def test_ebay_auction_only(monkeypatch):
    serve(monkeypatch, "<html><body><main><h1>Rare card</h1><a class='fake-btn'>Place bid</a></main></body></html>")
    res = await bigbox.ebay(EBAY, ctx("ebay"))
    assert res.status == "in_stock" and res.status_text == "Auction live"


async def test_ebay_challenge(monkeypatch):
    async def fake(url, render_js=False, *, needs=None):
        return FetchResult(url="https://www.ebay.com/splashui/challenge?ap=1&appName=orch", status=200,
                           text="<html>Checking your browser</html>")

    monkeypatch.setattr(fetcher, "fetch_html", fake)
    with pytest.raises(FetchError, match="^Blocked by bot protection on ebay.com$"):
        await bigbox.ebay(EBAY, ctx("ebay"))
