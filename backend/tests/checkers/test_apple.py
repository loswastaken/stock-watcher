"""Apple Store: fulfillment parsing, 2-hour delivery detection, HTTP/caching/fallback, resolve."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.checkers import apple, fetcher
from app.checkers.apple import (
    AppleConfig,
    check_apple,
    detect_two_hour,
    fulfillment_url,
    parse_fulfillment,
    parse_product_page,
    part_from_url,
)

from .conftest import load, load_json

A, B = "MG8H4LL/A", "MG8J4LL/A"
LABELS = {A: "256GB Cosmic Orange", B: "256GB Deep Blue"}
PRODUCT_URL = "https://www.apple.com/shop/buy-iphone/iphone-17-pro"


def keys(res):
    return [a.key for a in res.available]


# ------------------------------------------------------------------ parse_fulfillment

def test_pickup_today_only_is_default():
    r = parse_fulfillment(load_json("apple_fulfillment_mixed.json"), [A, B], LABELS, 25, True, True)
    # Stanford is "available tomorrow": shown in the store table, but not an alert.
    assert keys(r) == [f"pickup:R014:{A}", f"delivery2h:{A}"]
    assert r.status_text == "Pickup at 1 store · 2h delivery"
    stanford = next(s for s in r.detail["stores"] if s["store_number"] == "R033")
    assert [(p["available"], p["today"]) for p in stanford["parts"] if p["part_number"] == B] == [(True, False)]


@pytest.mark.parametrize("quote,today", [
    ("Available Today", True), ("Today", True), ("Available", True), (None, True),
    ("Available Tomorrow", False), ("Available Oct 3", False), ("Available Fri, Oct 3", False),
])
def test_pickup_is_today(quote, today):
    assert apple._pickup_is_today(quote) is today


def test_mixed_pickup_and_courier():
    r = parse_fulfillment(load_json("apple_fulfillment_mixed.json"), [A, B], LABELS, 25, True, True,
                          pickup_today_only=False)
    assert r.status == "in_stock"
    assert r.status_text == "Pickup at 2 stores · 2h delivery"
    assert keys(r) == [f"pickup:R014:{A}", f"pickup:R033:{B}", f"delivery2h:{A}"]
    labels = [a.label for a in r.available]
    assert labels[0] == "Pickup available today at Valley Fair (3.2 mi) — 256GB Cosmic Orange"
    assert labels[1] == "Pickup available tomorrow at Stanford (12.8 mi) — 256GB Deep Blue"
    assert labels[2] == "2-hour delivery available — 256GB Cosmic Orange"
    assert r.title == "iPhone 17 Pro 256GB Cosmic Orange"

    stores = r.detail["stores"]
    assert [s["store_number"] for s in stores] == ["R014", "R033"]  # 41.2 mi Stonestown filtered out
    assert r.detail["stores_out_of_range"] == 1
    vf = stores[0]
    assert vf == {
        "store_number": "R014", "name": "Valley Fair", "city": "Santa Clara", "distance_miles": 3.2,
        "parts": [
            {"part_number": A, "label": "256GB Cosmic Orange", "available": True, "today": True, "quote": "Available Today", "display": "available"},
            {"part_number": B, "label": "256GB Deep Blue", "available": False, "today": False, "quote": "Currently unavailable", "display": "unavailable"},
        ],
    }
    delivery = {d["part_number"]: d for d in r.detail["delivery"]}
    assert delivery[A]["two_hour"] is True
    assert "Courier" in delivery[A]["quote"] and "2 hours" in delivery[A]["quote"]
    assert any("Standard Delivery" in q for q in delivery[A]["quotes"])
    assert delivery[B]["two_hour"] is False
    assert delivery[B]["quote"] == "Delivers to 95014 Tue 10/7 - Thu 10/9 — Free"
    assert r.detail["location"] == "95014"


def test_distance_filter_and_unlimited():
    data = load_json("apple_fulfillment_mixed.json")
    r = parse_fulfillment(data, [A], LABELS, 50, True, False)
    assert keys(r) == [f"pickup:R014:{A}", f"pickup:R032:{A}"]
    assert parse_fulfillment(data, [A], LABELS, 5, True, False).status_text == "Pickup at 1 store"
    assert len(parse_fulfillment(data, [A], LABELS, None, True, False).detail["stores"]) == 3
    assert len(parse_fulfillment(data, [A], LABELS, 0, True, False).detail["stores"]) == 3
    r = parse_fulfillment(data, [A], LABELS, 2, True, False)
    assert r.status == "out_of_stock" and r.status_text == "Not available nearby"


def test_watch_toggles():
    data = load_json("apple_fulfillment_mixed.json")
    r = parse_fulfillment(data, [A, B], LABELS, 25, watch_pickup=False, watch_delivery=True)
    assert keys(r) == [f"delivery2h:{A}"] and r.status_text == "2h delivery"
    r = parse_fulfillment(data, [A, B], LABELS, 25, watch_pickup=True, watch_delivery=False)
    assert all(k.startswith("pickup:") for k in keys(r))
    # detail still carries everything for the UI
    assert r.detail["delivery"][0]["two_hour"] is True
    r = parse_fulfillment(data, [A], LABELS, 25, False, False)
    assert r.status == "unknown" and r.available == []


def test_nothing_available_and_negated_courier_message():
    r = parse_fulfillment(load_json("apple_fulfillment_none.json"), [A], {}, 25, True, True)
    assert r.status == "out_of_stock"
    assert r.status_text == "Not available nearby"
    assert r.available == []
    assert [p["display"] for s in r.detail["stores"] for p in s["parts"]] == ["unavailable", "ineligible"]
    d = r.detail["delivery"][0]
    assert d["two_hour"] is False
    assert d["label"] == "iPhone 17 Pro 256GB Cosmic Orange"  # falls back to Apple's product title
    assert "Mon 10/13" in d["quote"]


def test_malformed_payload_does_not_crash():
    r = parse_fulfillment(load_json("apple_fulfillment_malformed.json"), [A], {}, 25, True, True)
    assert r.status == "in_stock"
    by_num = {s["store_number"]: s for s in r.detail["stores"]}
    assert by_num["R999"]["distance_miles"] == 5.0  # 8 km
    assert by_num["R999"]["parts"][0]["available"] is False
    assert by_num["R998"]["parts"][0]["available"] is True  # inferred from eligibility + quote
    assert by_num["outlet"]["distance_miles"] is None
    assert set(keys(r)) == {f"pickup:R998:{A}", f"pickup:outlet:{A}", f"delivery2h:{A}"}
    assert r.detail["delivery"][0]["quote"] == "Delivers within 2 hours — $9"


def test_courier_detected_from_flags_and_types():
    parts = ["MYW33LL/A", "MX2D3AM/A", "MU8F2ZM/A"]
    r = parse_fulfillment(load_json("apple_fulfillment_flags.json"), parts, {}, 25, True, True)
    two = {d["part_number"]: d["two_hour"] for d in r.detail["delivery"]}
    assert two == {"MYW33LL/A": True, "MX2D3AM/A": True, "MU8F2ZM/A": False}
    assert r.status_text == "2h delivery"
    assert r.detail["stores"] == []


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"regular": {"deliveryOptions": [{"displayName": "2-Hour Delivery", "date": "Today", "shippingCost": "$9"}]}}, True),
        ({"compact": {"quote": "Delivers to 10001<br/>Today, within 2 hours"}}, True),
        ({"regular": {"deliveryOptions": [{"displayName": "Standard Delivery", "date": "Fri 10/3"}]}}, False),
        ({"regular": {"deliveryOptions": [{"displayName": "Courier", "date": "Not available"}]}}, False),
        ({"regular": {"isCourierEligible": "true"}}, True),
        ({"regular": {"deliveryOptions": [{"displayName": "Today", "date": "Order by 3pm, delivers within 2 hours"}]}}, True),
        ({"regular": {"deliveryOptions": [{"displayName": "Standard", "date": "Order within 2 hours for delivery Fri"}]}}, False),
        ({"regular": {"courierAvailable": 1}}, False),
        ("garbage", False),
        (None, False),
    ],
)
def test_detect_two_hour_variants(payload, expected):
    assert detect_two_hour(payload)[0] is expected


@pytest.mark.parametrize("payload", [{}, {"head": {"status": "503"}}, {"body": {}}, [], "x", None])
def test_unexpected_payload_is_error(payload):
    r = parse_fulfillment(payload, [A], {}, 25, True, True)
    assert r.status == "error"
    assert r.error


def test_keys_stable_across_runs():
    data = load_json("apple_fulfillment_mixed.json")
    a = parse_fulfillment(data, [A, B], LABELS, 25, True, True)
    b = parse_fulfillment(data, [A, B], {A: "renamed", B: "renamed too"}, 25, True, True)
    assert keys(a) == keys(b)


# ------------------------------------------------------------------ config / URL helpers

def test_apple_config_normalisation():
    cfg = AppleConfig.from_dict({
        "parts": [{"part_number": " mg8h4ll/a ", "label": "Orange"}, {"part_number": "MG8H4LL/A"}, "MG8J4LL%2FA",
                  {"part_number": "Z15G"}, None],
        "zip": "95014-1234", "max_distance_miles": "15", "watch_delivery": False,
    })
    assert cfg.parts == [A, B]
    assert cfg.labels == {A: "Orange"}
    assert cfg.zip == "95014"
    assert cfg.max_distance_miles == 15.0
    assert cfg.watch_pickup is True and cfg.watch_delivery is False


def test_fulfillment_url():
    q = parse_qs(urlsplit(fulfillment_url([A, B], "95014")).query)
    assert q["parts.0"] == [A] and q["parts.1"] == [B]
    assert q["location"] == ["95014"]
    assert q["searchNearby"] == ["true"]
    assert q["mts.0"] == ["regular"] and q["mts.1"] == ["compact"]
    assert q["pl"] == ["true"] and q["fae"] == ["true"]


def test_part_from_url():
    assert part_from_url("https://www.apple.com/shop/product/MXP63AM/A/airpods-4") == "MXP63AM/A"
    assert part_from_url("https://www.apple.com/shop/product/MXP63AM%2FA/airpods-4") == "MXP63AM/A"
    assert part_from_url(PRODUCT_URL) is None


# ------------------------------------------------------------------ network path

FULFILL = dict(method="GET", host="www.apple.com", path="/shop/fulfillment-messages")


def _mock_warmup():
    return respx.get("https://www.apple.com/shop/buy-iphone").mock(
        return_value=httpx.Response(200, text="<html>shop</html>", headers={"set-cookie": "as_dc=ucp4; Path=/; Domain=.apple.com"})
    )


@respx.mock
async def test_check_apple_happy_path_headers_cookies_and_cache():
    warm = _mock_warmup()
    route = respx.route(**FULFILL).mock(return_value=httpx.Response(200, json=load_json("apple_fulfillment_mixed.json")))
    cfg = {"parts": [{"part_number": A, "label": "256GB Cosmic Orange"}], "zip": "95014", "max_distance_miles": 25}
    r = await check_apple(PRODUCT_URL, cfg)
    assert r.status == "in_stock"
    assert keys(r) == [f"pickup:R014:{A}", f"delivery2h:{A}"]
    assert r.detail["zip"] == "95014"
    assert warm.called
    req = route.calls.last.request
    assert req.headers["referer"] == PRODUCT_URL
    assert "application/json" in req.headers["accept"]
    assert "as_dc=ucp4" in req.headers.get("cookie", "")
    assert parse_qs(req.url.query.decode())["parts.0"] == [A]

    # identical (parts, zip) within 30 s is served from cache
    r2 = await check_apple(PRODUCT_URL, cfg)
    assert route.call_count == 1
    assert keys(r2) == keys(r)
    assert warm.call_count == 1


@respx.mock
async def test_html_challenge_falls_back_to_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    _mock_warmup()
    respx.route(**FULFILL).mock(return_value=httpx.Response(200, text="<html><title>Access Denied</title></html>",
                                                             headers={"content-type": "text/html"}))
    calls = []

    async def fake_from_page(page_url, target_url, accept="application/json"):
        calls.append((page_url, target_url))
        return 200, load("apple_fulfillment_mixed.json"), [{"name": "dssid2", "value": "x", "domain": ".apple.com", "path": "/"}]

    monkeypatch.setattr(fetcher, "browser_fetch_from_page", fake_from_page)
    r = await check_apple(PRODUCT_URL, {"parts": [A], "zip": "95014"})
    assert r.status == "in_stock"
    assert len(calls) == 1
    assert calls[0][0] == PRODUCT_URL and "fulfillment-messages" in calls[0][1]


@respx.mock
async def test_541_falls_back_to_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    _mock_warmup()
    respx.route(**FULFILL).mock(return_value=httpx.Response(541, text=""))
    called = []

    async def fake_from_page(page_url, target_url, accept="application/json"):
        called.append(1)
        return 200, load("apple_fulfillment_none.json"), []

    monkeypatch.setattr(fetcher, "browser_fetch_from_page", fake_from_page)
    r = await check_apple("https://example.com/not-apple", {"parts": [A], "zip": "95014"})
    assert called and r.status == "out_of_stock"


@respx.mock
async def test_block_without_browser_is_error_result():
    _mock_warmup()
    respx.route(**FULFILL).mock(return_value=httpx.Response(403, text="Forbidden"))
    r = await check_apple(PRODUCT_URL, {"parts": [A], "zip": "95014"})
    assert r.status == "error"
    assert "403" in r.error and "browser fallback disabled" in r.error


@respx.mock
async def test_browser_fallback_also_blocked_is_error(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    _mock_warmup()
    respx.route(**FULFILL).mock(return_value=httpx.Response(403, text="Forbidden"))

    async def fake_from_page(page_url, target_url, accept="application/json"):
        return 403, "nope", []

    monkeypatch.setattr(fetcher, "browser_fetch_from_page", fake_from_page)
    r = await check_apple(PRODUCT_URL, {"parts": [A], "zip": "95014"})
    assert r.status == "error"


@respx.mock
async def test_many_parts_are_chunked_and_merged(monkeypatch):
    monkeypatch.setattr(apple, "MAX_PARTS_PER_REQUEST", 1)
    _mock_warmup()
    data = load_json("apple_fulfillment_mixed.json")

    def responder(request):
        part = parse_qs(request.url.query.decode())["parts.0"][0]
        body = load_json("apple_fulfillment_mixed.json")
        for s in body["body"]["content"]["pickupMessage"]["stores"]:
            s["partsAvailability"] = {part: s["partsAvailability"][part]}
        body["body"]["content"]["deliveryMessage"] = {part: data["body"]["content"]["deliveryMessage"][part]}
        return httpx.Response(200, json=body)

    route = respx.route(**FULFILL).mock(side_effect=responder)
    r = await check_apple(PRODUCT_URL, {"parts": [A, B], "zip": "95014", "pickup_today_only": False})
    assert route.call_count == 2
    assert keys(r) == [f"pickup:R014:{A}", f"pickup:R033:{B}", f"delivery2h:{A}"]


async def test_config_errors():
    r = await check_apple(PRODUCT_URL, {"parts": [], "zip": "95014"})
    assert r.status == "error" and "part" in r.error.lower()
    r = await check_apple(PRODUCT_URL, {"parts": [A]})
    assert r.status == "error" and "zip" in r.error.lower()


# ------------------------------------------------------------------ resolve

def test_parse_buy_page_variants_with_labels_and_prices():
    info = parse_product_page(load("apple_buy_iphone.html"), PRODUCT_URL)
    assert info["product_name"] == "iPhone 17 Pro and iPhone 17 Pro Max"
    assert info["image_url"].startswith("https://store.storeimages.cdn-apple.com/")
    assert info["variants"] == [
        {"part_number": A, "label": "iPhone 17 Pro 256GB Cosmic Orange", "price": "$1,099.00"},
        {"part_number": B, "label": "iPhone 17 Pro 256GB Deep Blue", "price": "$1,099.00"},
        {"part_number": "MG9A4LL/A", "label": "iPhone 17 Pro Max 1TB Silver", "price": "$1,599.00"},
    ]


def test_parse_product_page_part_in_url_and_json_parse_blob():
    url = "https://www.apple.com/shop/product/MXP63AM/A/airpods-4"
    info = parse_product_page(load("apple_airpods.html"), url)
    assert info["product_name"] == "AirPods 4"
    assert info["image_url"] == "https://www.apple.com/is/airpods-4-select-202409?wid=1200"
    assert info["variants"] == [{"part_number": "MXP63AM/A", "label": "AirPods 4", "price": "$129.00"}]


def test_parse_product_page_garbage():
    info = parse_product_page("<html><body>nothing</body></html>", PRODUCT_URL)
    assert info["variants"] == []
    assert parse_product_page("", PRODUCT_URL)["variants"] == []


@respx.mock
async def test_resolve_apple_fetches_page():
    _mock_warmup()
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, text=load("apple_buy_iphone.html")))
    info = await apple.resolve_apple(PRODUCT_URL)
    assert len(info["variants"]) == 3


@respx.mock
async def test_resolve_apple_failure_returns_url_part():
    _mock_warmup()
    url = "https://www.apple.com/shop/product/MXP63AM/A/airpods-4"
    respx.get(url).mock(return_value=httpx.Response(500))
    info = await apple.resolve_apple(url)
    assert info["variants"] == [{"part_number": "MXP63AM/A", "label": "MXP63AM/A", "price": None}]


async def test_resolve_non_apple_url_is_empty():
    assert (await apple.resolve_apple("https://example.com/x"))["variants"] == []


@respx.mock
async def test_after_block_next_check_goes_straight_to_browser(monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "true")
    _mock_warmup()
    route = respx.route(**FULFILL).mock(return_value=httpx.Response(541, text=""))
    calls = []

    async def fake_from_page(page_url, target_url, accept="application/json"):
        calls.append(target_url)
        return 200, load("apple_fulfillment_mixed.json"), []

    monkeypatch.setattr(fetcher, "browser_fetch_from_page", fake_from_page)
    await check_apple(PRODUCT_URL, {"parts": [A], "zip": "95014"})
    assert route.call_count == 1 and len(calls) == 1
    apple._cache.clear()
    r = await check_apple(PRODUCT_URL, {"parts": [A], "zip": "95014"})
    assert route.call_count == 1 and len(calls) == 2
    assert r.status == "in_stock"


# ------------------------------------------------------------------ live apple.com buy page (Sept 2026)

LIVE_URL = "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-burgundy-unlocked"


def test_live_buy_page_variants_are_clean_and_linked_model_selected():
    r = parse_product_page(load("apple_buy_iphone18pro_live.html"), LIVE_URL)
    variants = {v["part_number"]: v for v in r["variants"]}
    assert len(variants) == 32  # 2 sizes x 4 capacities x 4 colors; AppleCare parts excluded
    assert all(pn.startswith("MJ") for pn in variants)
    assert r["selected_part_number"] == "MJW64LL/A"
    assert r["variants"][0]["part_number"] == "MJW64LL/A"  # linked model first
    assert variants["MJW64LL/A"] == {"part_number": "MJW64LL/A", "label": "iPhone 18 Pro Max 256GB Burgundy",
                                     "price": "$1,299.00"}
    assert variants["MJQ34LL/A"]["label"] == "iPhone 18 Pro 256GB Black"
    assert variants["MJQ34LL/A"]["price"] == "$1,199.00"
    for v in r["variants"]:
        assert "Footnote" not in v["label"] and "inch display" not in v["label"]


def test_live_buy_page_other_carrier_link_selects_same_model():
    url = LIVE_URL.replace("-unlocked", "-verizon")
    assert parse_product_page(load("apple_buy_iphone18pro_live.html"), url)["selected_part_number"] == "MJW64LL/A"


def test_price_keys_are_not_prices():
    assert apple._price_text("mjw64ll_a_att_iphone18pro") is None
    assert apple._price_text("1299") == "$1,299.00"
    assert apple._price_text({"amountBeforeTradeIn": 1499.0, "fullPrice": None}) == "$1,499.00"


@respx.mock
async def test_block_reset_does_not_break_concurrent_check(monkeypatch):
    """'Check all' runs Apple checks concurrently: one getting blocked (session reset) must not
    close the shared client under another check that is waiting to send ('client has been closed')."""
    import asyncio

    monkeypatch.setenv("ENABLE_BROWSER", "true")
    monkeypatch.setattr(fetcher, "HOST_MIN_GAP", 0.1)  # requests to apple.com queue up, like in production
    _mock_warmup()

    def responder(request):
        part = parse_qs(request.url.query.decode())["parts.0"][0]
        if part == B:
            return httpx.Response(403, text="denied")
        return httpx.Response(200, json=load_json("apple_fulfillment_mixed.json"))

    respx.route(**FULFILL).mock(side_effect=responder)

    async def fake_from_page(page_url, target_url, accept="application/json"):
        return 200, json.dumps(load_json("apple_fulfillment_mixed.json")), []

    monkeypatch.setattr(fetcher, "browser_fetch_from_page", fake_from_page)
    blocked = asyncio.create_task(check_apple(PRODUCT_URL, {"parts": [B], "zip": "95014"}))
    await asyncio.sleep(0)
    ok = asyncio.create_task(check_apple(PRODUCT_URL, {"parts": [A], "zip": "95014"}))
    r_blocked, r_ok = await asyncio.gather(blocked, ok)
    assert r_ok.status != "error", r_ok.error
    assert r_blocked.status != "error", r_blocked.error
