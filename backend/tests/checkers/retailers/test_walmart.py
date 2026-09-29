"""Walmart / Sam's Club adapter: __NEXT_DATA__ product, seller logic, delisting, block pages."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.checkers import fetcher
from app.checkers.fetcher import FetchError, FetchResult
from app.checkers.retailers import AdapterContext, RetailerConfig, retailer_by_key, walmart

FIX = Path(__file__).parent.parent / "fixtures" / "retailers" / "bigbox"
URL = "https://www.walmart.com/ip/Nintendo-Switch-2-System/15949610846?classType=REGULAR"
SAMS = "https://www.samsclub.com/ip/Nintendo-Switch-2-System/13951234567"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def ctx(key: str = "walmart", **rc) -> AdapterContext:
    return AdapterContext(retailer=retailer_by_key(key), retailer_config=RetailerConfig.from_dict(rc))


def serve(monkeypatch, html: str, final_url: str | None = None, **kw) -> None:
    async def fake(url, render_js=False, *, needs=None):
        return FetchResult(url=final_url or url, status=200, text=html, **kw)

    monkeypatch.setattr(fetcher, "fetch_html", fake)


@pytest.mark.parametrize("url,iid", [
    (URL, "15949610846"),
    ("https://www.walmart.com/ip/5074872077", "5074872077"),
    ("https://www.walmart.com/browse/electronics/3944", None),
])
def test_item_id(url, iid):
    assert walmart.item_id(url) == iid


async def test_first_party_in_stock(monkeypatch):
    serve(monkeypatch, fx("walmart_instock.html"))
    res = await walmart.check(URL, ctx())
    assert res.status == "in_stock" and res.status_text == "In stock"
    assert res.title == "Nintendo Switch 2 System"
    assert res.price == "$449.00" and res.detail["price_value"] == 449.0
    assert res.detail["seller"] == "Walmart.com" and res.detail["third_party"] is False
    assert res.detail["cart_url"] == "https://affil.walmart.com/cart/addToCart?items=15949610846"
    assert res.image_url.endswith("Nintendo-Switch-2.jpeg")


async def test_marketplace_seller(monkeypatch):
    serve(monkeypatch, fx("walmart_3p.html"))
    res = await walmart.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Third-party sellers only"
    assert res.detail["seller"] == "GameFlip Resale" and res.detail["third_party"] is True
    # the marketplace seller's price is not the watched item's price
    assert res.price is None and res.detail["third_party_price"] == 599.99
    res = await walmart.check(URL, ctx(official_only=False))
    assert res.status == "in_stock" and res.detail["price_value"] == 599.99


async def test_out_of_stock(monkeypatch):
    serve(monkeypatch, fx("walmart_oos.html"))
    res = await walmart.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "Out of stock"


async def test_delisted(monkeypatch):
    serve(monkeypatch, fx("walmart_delisted.html"))
    res = await walmart.check(URL, ctx())
    assert res.status == "out_of_stock" and res.status_text == "No longer available"


@pytest.mark.parametrize("final", [URL, "https://www.walmart.com/blocked?url=L2lw&uuid=1"])
async def test_block_page_raises(monkeypatch, final):
    serve(monkeypatch, fx("walmart_blocked.html"), final_url=final)
    with pytest.raises(FetchError, match="^Blocked by bot protection on walmart.com$"):
        await walmart.check(URL, ctx())


async def test_fetch_level_block_is_reworded(monkeypatch):
    async def fake(url, render_js=False, *, needs=None):
        raise FetchError("Blocked by bot protection on www.samsclub.com")

    monkeypatch.setattr(fetcher, "fetch_html", fake)
    with pytest.raises(FetchError, match="samsclub.com$"):
        await walmart.check(SAMS, ctx("samsclub"))


async def test_sams_club_first_party_no_cart_link(monkeypatch):
    serve(monkeypatch, fx("samsclub_instock.html"))
    res = await walmart.check(SAMS, ctx("samsclub"))
    assert res.status == "in_stock" and res.status_text == "Limited stock"
    assert res.detail["seller"] == "Sam's Club" and res.detail["third_party"] is False
    assert "cart_url" not in res.detail and res.detail["retailer"] == "samsclub"


async def test_queued(monkeypatch):
    if "queued" not in FetchResult.__dataclass_fields__:
        pytest.skip("fetcher has no waiting-room flag")
    serve(monkeypatch, "<html>queue</html>", queued=True)
    res = await walmart.check(URL, ctx())
    assert res.status == "unknown" and res.detail["queue"] is True


async def test_no_next_data_uses_generic(monkeypatch):
    serve(monkeypatch, """<html><head><script type="application/ld+json">{"@context":"https://schema.org",
        "@type":"Product","name":"Legacy item","offers":{"@type":"Offer","price":"19.98","priceCurrency":"USD",
        "availability":"https://schema.org/OutOfStock"}}</script></head><body><h1>Legacy item</h1></body></html>""")
    res = await walmart.check("https://www.samsclub.com/p/legacy-item/prod12345", ctx("samsclub"))
    assert res.status == "out_of_stock" and res.title == "Legacy item"
    assert res.detail["price_value"] == 19.98 and res.detail["retailer"] == "samsclub"
