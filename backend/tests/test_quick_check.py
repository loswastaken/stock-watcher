"""Quick check: /items/preview pass-through fields and the server-side support classification."""
from __future__ import annotations

import pytest

from app.checkers.retailers.registry import retailer_by_key
from app.routers.items import classify_support

BESTBUY = retailer_by_key("bestbuy").to_dict()


def level(data: dict, retailer: dict | None = None) -> str:
    return classify_support(data, retailer)["level"]


@pytest.mark.parametrize(
    ("data", "retailer", "expected"),
    [
        # a registry store whose own adapter answered
        ({"status": "in_stock", "adapter": "bestbuy"}, BESTBUY, "dedicated"),
        ({"status": "unknown", "adapter": "bestbuy", "name": "X"}, BESTBUY, "dedicated"),
        # older checker without `adapter`: registry stores count as dedicated
        ({"status": "out_of_stock"}, BESTBUY, "dedicated"),
        ({"status": "unknown", "is_apple": True, "name": "iPhone"}, None, "dedicated"),
        # platform recipes, on or off the list
        ({"status": "in_stock", "adapter": "shopify"}, None, "platform"),
        ({"status": "out_of_stock", "adapter": "sfcc"}, None, "platform"),
        ({"status": "unknown", "adapter": "woocommerce", "name": "X"}, None, "platform"),
        # generic heuristics
        ({"status": "in_stock", "adapter": "generic"}, None, "generic"),
        ({"status": "out_of_stock"}, None, "generic"),
        ({"status": "unknown", "adapter": "generic", "name": "X"}, None, "generic"),
        ({"status": "in_stock", "adapter": "generic"}, BESTBUY, "generic"),
        # blocked
        ({"status": "error", "blocked": True, "error": "x"}, BESTBUY, "blocked"),
        ({"status": "error", "error": "Blocked by bot protection on shop.test"}, None, "blocked"),
        ({"status": "error", "error": "Best Buy refused the connection (bot protection) — retrying later"}, BESTBUY,
         "blocked"),
        ({"status": "unknown", "error": "HTTP 403 from shop.test"}, None, "blocked"),
        # errors / dead links
        ({"status": "error", "error": "Page not found (HTTP 404) — update the link"}, None, "unsupported"),
        ({"status": "error", "error": "Couldn't find nope.test (DNS lookup failed) — check the link"}, None,
         "unsupported"),
        ({"status": "unknown", "error": "Could not fetch the page (RuntimeError)"}, None, "unsupported"),
    ],
)
def test_classify_support_levels(data, retailer, expected):
    assert level(data, retailer) == expected


def test_classify_support_labels_and_details():
    s = classify_support({"status": "in_stock", "adapter": "shopify"}, None)
    assert s["label"] == "Auto-detected Shopify store" and "Shopify" in s["detail"]
    s = classify_support({"status": "in_stock", "adapter": "generic"}, None)
    assert s["label"] == "Works with generic detection"
    s = classify_support({"status": "unknown", "adapter": "generic", "name": "X"}, None)
    assert "CSS selector" in s["detail"] and "Text match" in s["detail"]
    s = classify_support({"status": "error", "blocked": True}, None)
    assert s["detail"].startswith("Store blocked our checker")
    s = classify_support({"status": "error", "error": "Page not found (HTTP 404) — update the link"}, None)
    assert s["label"] == "Page not found" and "404" in s["detail"]
    s = classify_support({"status": "in_stock", "adapter": "bestbuy"}, BESTBUY)
    assert "Best Buy" in s["detail"]


def test_preview_passes_checker_fields_through(admin, monkeypatch):
    async def fake_preview(url):
        return {
            "name": "RTX 5090", "image_url": "https://i/x.jpg", "price": "$1,999.99", "status": "in_stock",
            "is_apple": False, "retailer": "bestbuy", "status_text": "Add to cart", "adapter": "bestbuy",
            "fetched_via": "browser", "seller": "Some Seller", "third_party": True,
            "cart_url": "https://www.bestbuy.com/cart?sku=1", "signals": ["button: 'Add to Cart'", "", "json-ld"],
            "blocked": False, "queued": False,
        }

    monkeypatch.setattr("app.routers.items.preview_url", fake_preview)
    r = admin.post("/api/items/preview", json={"url": "https://www.bestbuy.com/site/x/1.p"})
    assert r.status_code == 200
    b = r.json()
    assert b["retailer"]["key"] == "bestbuy"
    assert b["adapter"] == "bestbuy" and b["fetched_via"] == "browser" and b["status_text"] == "Add to cart"
    assert b["seller"] == "Some Seller" and b["third_party"] is True and b["cart_url"].endswith("sku=1")
    assert b["signals"] == ["button: 'Add to Cart'", "json-ld"]
    assert b["support"]["level"] == "dedicated"


def test_preview_tolerates_missing_new_fields(admin, monkeypatch):
    async def fake_preview(url):
        return {"name": "Thing", "image_url": None, "price": None, "status": "out_of_stock", "is_apple": False,
                "retailer": None}

    monkeypatch.setattr("app.routers.items.preview_url", fake_preview)
    b = admin.post("/api/items/preview", json={"url": "https://tiny.shop.test/p/1"}).json()
    assert b["retailer"] is None and b["adapter"] is None and b["signals"] == []
    assert b["blocked"] is False and b["queued"] is False and b["third_party"] is None
    assert b["support"] == {"level": "generic", "label": "Works with generic detection",
                            "detail": b["support"]["detail"]}


def test_preview_failure_is_unsupported(admin, monkeypatch):
    async def boom(url):
        raise RuntimeError("x")

    monkeypatch.setattr("app.routers.items.preview_url", boom)
    b = admin.post("/api/items/preview", json={"url": "https://a.test/"}).json()
    assert b["error"] and b["support"]["level"] == "unsupported"
