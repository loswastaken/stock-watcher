"""Generic (non-Apple) detection: structured data, meta tags, heuristics, modes, metadata."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.checkers import fetcher
from app.checkers.generic import analyze, check_generic, clean_title, norm_availability

from .conftest import load

URL = "https://shop.example/products/thing"


def run(fixture: str, url: str = URL, config: dict | None = None):
    return analyze(load(fixture), url, config)


# ------------------------------------------------------------------ vocabulary

@pytest.mark.parametrize(
    "value,token,verdict",
    [
        ("https://schema.org/InStock", "instock", "in"),
        ("http://schema.org/OutOfStock", "outofstock", "out"),
        ("schema:SoldOut", "soldout", "out"),
        ("InStock", "instock", "in"),
        ("https://schema.org/PreOrder", "preorder", "in"),
        ("https://schema.org/BackOrder", "backorder", "in"),
        ("https://schema.org/Discontinued", "discontinued", "out"),
        ("LimitedAvailability", "limitedavailability", "in"),
        ("in stock", "instock", "in"),
        ("oos", "oos", "out"),
        ({"@id": "https://schema.org/InStoreOnly"}, "instoreonly", "in"),
        ("https://schema.org/Reserved", "reserved", None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_norm_availability(value, token, verdict):
    assert norm_availability(value) == (token, verdict)


# ------------------------------------------------------------------ structured data

def test_jsonld_graph_with_id_reference_in_stock():
    r = run("jsonld_graph_instock.html", "https://brewco.example/products/aurora-x1")
    assert r.status == "in_stock"
    assert r.status_text == "In stock"
    assert [a.key for a in r.available] == ["stock"]
    assert r.price == "$1,099.00"
    assert r.title == "Aurora X1 Espresso Machine"
    # og:image wins over JSON-LD image; protocol-relative URL absolutized
    assert r.image_url == "https://cdn.brewco.example/products/aurora-x1_1200x.jpg?v=1712"
    assert r.detail["matched"] == "json-ld: InStock"
    assert "json-ld: InStock" in r.detail["signals"]


def test_jsonld_array_all_offers_out_of_stock_beats_button_heuristic():
    r = run("jsonld_array_outofstock.html")
    assert r.status == "out_of_stock"
    assert r.available == []
    assert r.price == "$249.99"
    assert r.title == "Nimbus 2 Headphones"
    assert r.image_url == "https://shop.example/media/nimbus2.png"
    assert r.detail["matched"].startswith("json-ld: OutOfStock")
    # the heuristic signal is still recorded for the UI's "why" line
    assert any(s.startswith("button: 'Add to cart'") for s in r.detail["signals"])


def test_aggregate_offer_any_in_stock_wins_and_price_from_available_offer():
    r = run("jsonld_aggregateoffer.html")
    assert r.status == "in_stock"
    assert r.status_text == "Limited stock"
    assert r.price == "$149.95"
    assert r.title == "Trail Runner 5"
    assert r.image_url == "https://peak.example/img/tr5.jpg"


def test_product_group_any_variant_in_stock():
    r = run("jsonld_productgroup.html", "https://shop.example/products/everyday-tee")
    assert r.status == "in_stock"
    assert "Everyday Tee - M" in r.detail["matched"]
    assert r.price == "$27.00"
    assert r.image_url == "https://shop.example/tee.jpg"


def test_product_group_url_pins_variant():
    r = run("jsonld_productgroup.html", "https://shop.example/products/everyday-tee?variant=1003")
    assert r.status == "out_of_stock"
    assert r.status_text == "Sold out"


def test_shopify_variant_param_selects_offer():
    html_url = "https://pixel.example/products/retro-console-mini"
    assert run("shopify_variants.html", html_url + "?variant=40000000001").status == "in_stock"
    r = run("shopify_variants.html", html_url + "?variant=40000000002")
    assert r.status == "out_of_stock"
    assert r.price == "$109.99"
    assert r.image_url == "https://pixel.example/cdn/shop/files/console.jpg?v=1"  # og:image:secure_url preferred
    # without a variant: any offer in stock -> in stock
    assert run("shopify_variants.html", html_url).status == "in_stock"


def test_microdata_out_of_stock():
    r = run("microdata_oos.html")
    assert r.status == "out_of_stock"
    assert r.detail["matched"] == "microdata: OutOfStock"
    assert r.price == "$39.98"
    assert r.title == "FlexiPro Garden Hose 50 ft"


def test_rdfa_preorder_counts_as_in_stock():
    r = run("rdfa_instock.html")
    assert r.status == "in_stock"
    assert r.status_text == "Pre-order"
    assert r.price == "€79.50"
    assert r.detail["matched"] == "rdfa: PreOrder"


def test_malformed_jsonld_is_parsed_leniently():
    r = run("malformed_jsonld.html")
    assert r.status == "in_stock"
    assert r.price == "€1,234.50"
    assert r.title == "Castle Siege Board Game"


def test_http_and_https_schema_urls_equivalent():
    tpl = ('<html><head><script type="application/ld+json">{"@context":"%s","@type":"Product","name":"X",'
           '"offers":{"@type":"Offer","availability":"%s/OutOfStock"}}</script></head><body></body></html>')
    for ctx in ("http://schema.org", "https://schema.org"):
        assert analyze(tpl % (ctx, ctx), URL).status == "out_of_stock"


# ------------------------------------------------------------------ meta tags / heuristics

def test_meta_tags_beat_heuristics():
    r = run("meta_tags_oos.html")
    assert r.status == "out_of_stock"
    assert r.detail["matched"] == "meta product:availability: oos"
    assert r.price == "$129.00"
    assert r.image_url == "https://keyshop.example/k8.jpg"
    assert r.title == "Mechanical Keyboard K8"


def test_enabled_buy_button_ignores_nav_footer_hidden_and_related():
    r = run("button_enabled.html")
    assert r.status == "in_stock"
    assert r.detail["matched"] == "button: 'Add to Bag' (enabled)"
    # "Out of stock" in nav / related products, "Notify me" in footer and the hidden "Sold out" span are ignored
    assert not any("text:" in s for s in r.detail["signals"])
    assert r.title == "Camping Stove"


def test_disabled_sold_out_button():
    r = run("button_disabled.html")
    assert r.status == "out_of_stock"
    assert r.detail["matched"] == "button: 'Sold out'"
    assert r.title == "Camping Stove"  # <title> "Camping Stove - OutdoorCo" matched against <h1>


def test_only_disabled_buy_button_means_out():
    r = run("disabled_buy_only.html")
    assert r.status == "out_of_stock"
    assert "disabled" in r.detail["matched"]


def test_out_of_stock_phrase():
    r = run("text_unavailable.html", "https://www.amazon.com/dp/B000000")
    assert r.status == "out_of_stock"
    assert r.detail["matched"] == "text: 'currently unavailable'"
    assert r.title == "Acme Robot Vacuum R2"


def test_in_stock_phrase():
    r = run("in_stock_text.html")
    assert r.status == "in_stock"
    assert r.detail["matched"] == "text: 'in stock'"


def test_back_in_stock_phrase_is_not_in_stock():
    html = "<html><body><main><h1>Widget</h1><p>Sign up to get an email when this is back in stock. Thanks!</p></main></body></html>"
    assert analyze(html, URL).status == "unknown"


def test_nothing_conclusive_is_unknown():
    r = run("no_signals.html")
    assert r.status == "unknown"
    assert r.status_text == "Unknown"
    assert r.available == []
    assert r.detail["matched"] is None


def test_structured_data_wins_over_conflicting_heuristics():
    r = run("jsonld_graph_instock.html")
    assert r.status == "in_stock"  # "sold out" in header/recommendations ignored anyway


def test_keys_are_stable():
    a = run("jsonld_graph_instock.html")
    b = run("button_enabled.html")
    assert [x.key for x in a.available] == [x.key for x in b.available] == ["stock"]


# ------------------------------------------------------------------ selector / text modes

def test_selector_mode_found_and_missing():
    cfg = {"mode": "selector", "selector": "#AddToCart"}
    assert run("button_enabled.html", config=cfg).status == "in_stock"
    r = run("button_enabled.html", config={"mode": "selector", "selector": "#nope"})
    assert r.status == "out_of_stock"
    assert r.detail["matched"] == "selector not found"


def test_selector_mode_disabled_element_is_out():
    r = run("button_disabled.html", config={"mode": "selector", "selector": "button[name=add]"})
    assert r.status == "out_of_stock"


def test_selector_mode_with_texts():
    cfg = {"mode": "selector", "selector": "#availability", "out_of_stock_text": "Currently unavailable"}
    assert run("text_unavailable.html", config=cfg).status == "out_of_stock"
    cfg = {"mode": "selector", "selector": "#availability", "in_stock_text": "Ready to ship"}
    assert run("text_unavailable.html", config=cfg).status == "out_of_stock"  # only in-text configured, absent
    cfg = {"mode": "selector", "selector": ".stock-status", "in_stock_text": "in stock", "out_of_stock_text": "sold out"}
    assert run("in_stock_text.html", config=cfg).status == "in_stock"
    cfg = {"mode": "selector", "selector": ".stock-status", "in_stock_text": "foo", "out_of_stock_text": "bar"}
    assert run("in_stock_text.html", config=cfg).status == "unknown"


def test_selector_mode_invalid_selector_is_error():
    r = run("button_enabled.html", config={"mode": "selector", "selector": "div[[["})
    assert r.status == "error"
    assert "selector" in r.error.lower()


def test_text_mode():
    both = {"mode": "text", "in_stock_text": "ships in", "out_of_stock_text": "SOLD OUT"}
    assert run("in_stock_text.html", config=both).status == "in_stock"
    assert run("button_disabled.html", config=both).status == "out_of_stock"
    assert run("no_signals.html", config=both).status == "unknown"
    only_out = {"mode": "text", "out_of_stock_text": "currently unavailable"}
    assert run("text_unavailable.html", config=only_out).status == "out_of_stock"
    assert run("in_stock_text.html", config=only_out).status == "in_stock"
    only_in = {"mode": "text", "in_stock_text": "in stock"}
    assert run("no_signals.html", config=only_in).status == "out_of_stock"
    # text inside <script> doesn't count
    html = "<html><body><script>var s='in stock';</script><p>hello</p></body></html>"
    assert analyze(html, URL, only_in).status == "out_of_stock"
    # newline-separated alternatives
    multi = {"mode": "text", "out_of_stock_text": "nope\ncurrently unavailable"}
    assert run("text_unavailable.html", config=multi).status == "out_of_stock"


# ------------------------------------------------------------------ titles

@pytest.mark.parametrize(
    "raw,url,site,expected",
    [
        ("Widget Pro | Example Store", "https://www.example.com/x", "Example Store", "Widget Pro"),
        ("Amazon.com: Sony WH-1000XM5 : Electronics", "https://www.amazon.com/dp/1", None, "Sony WH-1000XM5"),
        ("Nintendo Switch 2 - Best Buy", "https://www.bestbuy.com/site/1", None, "Nintendo Switch 2"),
        ("  Plain   title  ", "https://a.example/", None, "Plain title"),
        ("Walmart.com | Big TV", "https://www.walmart.com/ip/1", None, "Big TV"),
    ],
)
def test_clean_title(raw, url, site, expected):
    assert clean_title(raw, url, site) == expected


# ------------------------------------------------------------------ network path (respx)

@respx.mock
async def test_check_generic_fetches_and_analyzes():
    respx.get("https://brewco.example/products/aurora-x1").mock(
        return_value=httpx.Response(200, text=load("jsonld_graph_instock.html"), headers={"content-type": "text/html"})
    )
    r = await check_generic("https://brewco.example/products/aurora-x1", {"mode": "auto"})
    assert r.status == "in_stock"
    assert r.detail["fetched_via"] == "http"
    req = respx.calls.last.request
    assert "Chrome/" in req.headers["user-agent"]
    assert req.headers["accept-language"].startswith("en-US")
    assert "sec-ch-ua" in req.headers


@respx.mock
async def test_render_js_forces_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    calls = []

    async def fake_browser(url):
        calls.append(url)
        return fetcher.FetchResult(url=url, status=200, text=load("button_enabled.html"), via_browser=True)

    monkeypatch.setattr(fetcher, "browser_fetch", fake_browser)
    r = await check_generic(URL, {"mode": "auto", "render_js": True})
    assert calls == [URL]
    assert not route.called
    assert r.status == "in_stock"
    assert r.detail["fetched_via"] == "browser"
