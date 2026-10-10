"""Pre-release audit regressions for generic detection and price parsing: each test pins one false in-stock /
wrong-product / mis-parse finding (negated "in stock" wording, recommendation products, anchored buy labels,
variant pinning, glued / dot-grouped amounts, error-page rules, ...)."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.checkers.generic import BUY_RE, _in_stock_phrase, analyze, missing_page
from app.checkers.util import format_price, parse_amount

URL = "https://shop.example/products/widget-3000"


def page(body: str, ld: object = None, title: str = "Widget 3000") -> str:
    script = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld is not None else ""
    return f"<html><head><title>{title}</title>{script}</head><body><main><h1>Widget 3000</h1><p>$49.99</p>{body}</main></body></html>"


# ------------------------------------------------------------------ heuristics: "in stock" wording


@pytest.mark.parametrize("body", [
    "<button disabled>Add to cart</button><p>Enter your email and we'll notify you when this item is in stock.</p>",
    '<button class="btn" disabled>Add to cart</button><a href="#">Email me when it\'s in stock</a>',
    "<p>Sorry, this item isn't in stock right now.</p>",
    "<p>This item is no longer in stock.</p>",
    "<p>This item is not currently in stock.</p>",
    "<button disabled>Add to Cart</button><p>Get an alert when it is in stock</p>",
    '<button disabled>Add to Cart</button><a href="#">Check if it\'s in stock at a store near you</a>',
    "<p>Availability: 0 in stock</p>",
])
def test_negated_or_conditional_in_stock_wording_is_not_in_stock(body):
    r = analyze(page(body), URL)
    assert r.status != "in_stock", (r.status, r.detail.get("matched"))


@pytest.mark.parametrize("body", [
    "<p>Availability: In stock</p>",
    "<p>Pick up in store when ready.</p><p>In stock online.</p>",
    "<p>Free shipping when you spend $50 · In stock</p>",
    "<p>10 in stock</p>",
])
def test_plain_in_stock_wording_still_counts(body):
    r = analyze(page(body), URL)
    assert r.status == "in_stock" and r.detail["matched"].startswith("text:"), r.detail.get("matched")


# ------------------------------------------------------------------ heuristics: buy buttons


@pytest.mark.parametrize("body", [
    '<button disabled>Sold out</button><button type="button">Add purchase protection</button>',
    '<button disabled>Sold out</button><button type="button">See other purchase options</button>',
    '<button class="btn" disabled>Sold Out</button><button type="button">Buy now, pay later</button>',
    '<button class="btn">Pre-order sold out</button>',
    '<button class="btn">Add to Cart - Sold Out</button>',
    '<p>Currently unavailable.</p><a class="btn" href="/b2b">Bulk purchase</a>',
])
def test_buy_label_must_start_the_button_and_not_say_sold_out(body):
    r = analyze(page(body), URL)
    assert r.status == "out_of_stock", (r.status, r.detail.get("matched"))


@pytest.mark.parametrize("label,status_text", [("Add to cart", "In stock"), ("Buy it now", "In stock"),
                                               ("Pre-order now", "Pre-order"), ("+ Add to Bag", "In stock")])
def test_real_buy_buttons_still_count(label, status_text):
    r = analyze(page(f"<button>{label}</button>"), URL)
    assert r.status == "in_stock" and r.status_text == status_text


# ------------------------------------------------------------------ structured data: whose product


def _sold_out_page(ld: object, body: str = '<button disabled>Sold out</button>') -> str:
    return page(body, ld)


def test_related_product_nested_in_the_main_one_lends_no_stock():
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000", "sku": "W3000",
          "isRelatedTo": [{"@type": "Product", "name": "Widget Case", "url": "https://shop.example/products/widget-case",
                           "offers": {"@type": "Offer", "price": "19.99", "priceCurrency": "USD",
                                      "availability": "https://schema.org/InStock"}}]}
    r = analyze(_sold_out_page(ld), URL)
    assert (r.status, r.title, r.price) == ("out_of_stock", "Widget 3000", None)


def test_recommendation_item_list_lends_no_stock():
    ld = [{"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000", "sku": "W3000", "url": URL},
          {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
              {"@type": "ListItem", "position": 1, "item": {
                  "@type": "Product", "name": "Gizmo", "url": "https://shop.example/products/gizmo",
                  "offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "USD", "availability": "InStock"}}}]}]
    r = analyze(_sold_out_page(ld), URL)
    assert (r.status, r.title, r.price) == ("out_of_stock", "Widget 3000", None)


def test_second_root_product_lends_no_stock():
    ld = [{"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000"},
          {"@context": "https://schema.org", "@type": "Product", "name": "Widget Charger",
           "offers": {"@type": "Offer", "price": "29", "priceCurrency": "USD", "availability": "InStock"}}]
    r = analyze(_sold_out_page(ld), URL)
    assert (r.status, r.title, r.price) == ("out_of_stock", "Widget 3000", None)


def test_microdata_accessory_card_is_not_the_json_ld_product():
    body = ('<button disabled>Sold out</button><section class="pdp-accessories"><div itemscope '
            'itemtype="https://schema.org/Product"><span itemprop="name">Widget Case</span><div itemprop="offers" '
            'itemscope itemtype="https://schema.org/Offer"><link itemprop="availability" href="https://schema.org/InStock">'
            '<meta itemprop="price" content="19.99"><meta itemprop="priceCurrency" content="USD"></div></div></section>')
    r = analyze(page(body, {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000"}), URL)
    assert (r.status, r.title, r.price) == ("out_of_stock", "Widget 3000", None)
    # the same product described twice (JSON-LD without offers, microdata with them) still counts
    same = body.replace("Widget Case", "Widget 3000")
    r = analyze(page(same, {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000"}), URL)
    assert r.status == "in_stock" and r.detail["matched"] == "microdata: InStock" and r.price == "$19.99"


def test_root_offer_with_item_offered_product_is_that_products_offer():
    ld = {"@context": "https://schema.org", "@type": "Offer", "availability": "https://schema.org/OutOfStock",
          "price": "10", "priceCurrency": "USD", "itemOffered": {"@type": "Product", "name": "Tee"}}
    r = analyze(page("<button>Add to cart</button>", ld), "https://shop.example/products/tee")
    assert (r.status, r.price, r.detail["matched"]) == ("out_of_stock", "$10.00", "json-ld: OutOfStock")


@pytest.mark.parametrize("ref", [["#a"], {"x": 1}])
def test_non_string_id_reference_does_not_crash(ref):
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000",
          "offers": [{"@id": ref}, {"@type": "Offer", "availability": "https://schema.org/InStock", "price": "5"}]}
    assert analyze(page("", ld), URL).status == "in_stock"


# ------------------------------------------------------------------ structured data: URL-selected variant

TEE = "https://shop.example/products/tee"


def test_url_variant_pins_one_of_several_top_level_products():
    ld = [{"@context": "https://schema.org", "@type": "Product", "name": "Tee - Red", "sku": "TEE-RED-01", "url": TEE,
           "offers": {"@type": "Offer", "url": TEE + "?variant=111", "sku": "TEE-RED-01",
                      "availability": "https://schema.org/InStock", "price": "20", "priceCurrency": "USD"}},
          {"@context": "https://schema.org", "@type": "Product", "name": "Tee - Blue", "sku": "TEE-BLU-02", "url": TEE,
           "offers": {"@type": "Offer", "url": TEE + "?variant=222", "sku": "TEE-BLU-02",
                      "availability": "https://schema.org/OutOfStock", "price": "20", "priceCurrency": "USD"}}]
    r = analyze(page("", ld), TEE + "?variant=222")
    assert (r.status, r.title) == ("out_of_stock", "Tee - Blue")
    assert "json-ld: URL selects variant 222" in r.detail["signals"]
    assert analyze(page("", ld), TEE + "?variant=111").status == "in_stock"


def test_product_url_carrying_the_selected_variant_does_not_pin_every_offer():
    # themes put selected_or_first_available_variant.url on the Product: it names one variant, not all offers
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Tee", "url": TEE + "?variant=222",
          "offers": [{"@type": "Offer", "url": TEE + "?variant=111", "availability": "https://schema.org/InStock",
                      "price": "20", "priceCurrency": "USD"},
                     {"@type": "Offer", "url": TEE + "?variant=222", "availability": "https://schema.org/OutOfStock",
                      "price": "25", "priceCurrency": "USD"}]}
    r = analyze(page("", ld), TEE + "?variant=222")
    assert (r.status, r.price) == ("out_of_stock", "$25.00")


# ------------------------------------------------------------------ prices


@pytest.mark.parametrize("value,expected", [
    ("$1,299 $1,499", Decimal("1299")),  # sale + struck-out regular price in one text
    ("2 for $10", Decimal("2")),
    ("$10 - $20", Decimal("10")),
    ("1 299,00 €", Decimal("1299.00")),
    ("1,299.00", Decimal("1299.00")),
    ("1.099,00", Decimal("1099.00")),
    (["1299", "999"], None),
    ({"price": "1299"}, None),
    ("-$5.00", None),
])
def test_parse_amount_takes_one_amount_only(value, expected):
    assert parse_amount(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("1.299", Decimal("1299")), ("€1.299", Decimal("1299")), ("1.299.000", Decimal("1299000")),
    ("$1.29", Decimal("1.29")), ("12.99", Decimal("12.99")), ("1.5", Decimal("1.5")), ("0.999", Decimal("0.999")),
])
def test_parse_amount_dot_thousands(value, expected):
    assert parse_amount(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -3, Decimal("NaN")])
def test_parse_amount_rejects_non_finite_and_negative_numbers(value):
    assert parse_amount(value) is None and format_price(value, "USD") is None


def test_microdata_sale_and_regular_price_is_the_sale_price():
    html = ('<html><body><main><div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Widget Pro</h1>'
            '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><link itemprop="availability" '
            'href="https://schema.org/InStock"><meta itemprop="priceCurrency" content="USD"><span itemprop="price" '
            'content="1299">$1,299</span><s><span itemprop="price" content="1499">$1,499</span></s></div></div></main>'
            '</body></html>')
    assert analyze(html, URL).price == "$1,299.00"


def test_json_ld_nan_price_is_no_price():
    html = page("", {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000",
                     "offers": {"@type": "Offer", "price": float("nan"), "availability": "InStock"}})
    r = analyze(html, URL)
    assert r.status == "in_stock" and r.price is None


def test_european_meta_price_with_dot_thousands():
    html = ('<html><head><meta property="product:price:amount" content="1.299"><meta property="product:price:currency" '
            'content="EUR"></head><body><main><h1>Thing</h1></main></body></html>')
    assert analyze(html, URL).price == "€1,299.00"


def test_displayed_dollar_price_keeps_the_stores_own_dollar_currency():
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000",
          "offers": {"@type": "Offer", "price": "129.99", "priceCurrency": "CAD", "availability": "InStock"}}
    r = analyze(page('<div class="product__price--sale">$129.99</div>', ld), URL)
    assert r.price == "CA$129.99" and "structured_price" not in r.detail
    # a converted amount is still the visitor's displayed price (NYXI: GBP structured, USD rendered)
    ld["offers"]["priceCurrency"] = "GBP"
    r = analyze(page('<div class="product__price--sale">$164.15</div>', ld), URL)
    assert r.price == "$164.15" and r.detail["structured_price"] == "£129.99"


# ------------------------------------------------------------------ dead links


@pytest.mark.parametrize("url", ["https://www.nike.com/t/air-max-90/DH7568-404",
                                 "https://shop.example/products/error-404-tee", "https://shop.example/p/hd-404"])
def test_404_inside_a_product_url_is_not_an_error_page(url):
    html = "<html><head><title>Air Max 90</title></head><body><h1>Air Max 90</h1></body></html>"
    assert missing_page(url, url, html) is None
    assert missing_page(url, None, html) is None


def test_redirect_to_a_404_segment_is_an_error_page():
    assert missing_page("https://shop.example/item/x", "https://shop.example/404", "<html></html>")
    assert missing_page("https://shop.example/item/x", "https://shop.example/en/404/", "<html></html>")


@pytest.mark.parametrize("title,dead", [("404 Brand Denim Jacket", False), ("404 Brand Denim Jacket | Shop", False),
                                        ("404 Not Found", True), ("Error 404", True), ("404 - Page Not Found", True),
                                        ("404: This page could not be found.", True), ("404", True)])
def test_404_title_needs_error_wording(title, dead):
    html = f"<html><head><title>{title}</title></head><body><h1>{title}</h1></body></html>"
    assert bool(missing_page("https://shop.example/p/x", None, html)) is dead


# ------------------------------------------------- final-review regressions (kept from the earlier behaviour)
def test_review_widget_product_block_first_does_not_hide_the_offers():
    rev = {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000",
           "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8", "reviewCount": "120"}}
    main = {"@context": "https://schema.org", "@type": "Product", "name": "Widget 3000", "sku": "W3000",
            "url": "https://shop.example.com/products/widget-3000",
            "offers": {"@type": "Offer", "price": "19.99", "priceCurrency": "USD",
                       "availability": "https://schema.org/InStock"}}
    html = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in (rev, main))
    r = analyze(f"<html><head>{html}</head><body><main><h1>Widget 3000</h1></main></body></html>",
                "https://shop.example.com/collections/gadgets/products/widget-3000")
    assert (r.status, r.price) == ("in_stock", "$19.99")


@pytest.mark.parametrize("final, html", [
    ("https://shop.example.com/error-404", "<title>Shop Example</title>"),
    ("https://shop.example.com/page-404.html", "<title>Shop Example</title>"),
    ("https://shop.example.com/products/widget", "<title>Shop Example</title><h1>404 – We can’t find that page</h1>"),
    ("https://shop.example.com/products/widget", "<title>Shop Example</title><h1>404. That’s an error.</h1>"),
])
def test_soft_404_pages_are_still_dead_links(final, html):
    assert missing_page("https://shop.example.com/products/widget", final, html, 200) == "Product page not found"


@pytest.mark.parametrize("label", ["add_shopping_cart Add to cart", "Purchase"])
def test_icon_and_bare_purchase_buttons_are_buy_buttons(label):
    assert BUY_RE.search(label) and not BUY_RE.search("Bulk purchase")


def test_zero_reviews_before_in_stock_is_not_a_negation():
    assert _in_stock_phrase("0 reviews In stock") and not _in_stock_phrase("0 in stock")
