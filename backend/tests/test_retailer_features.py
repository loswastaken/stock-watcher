"""HotStock-style features: price limits, muted stores, auto re-arm, rich alerts, multi-store
product groups, restock history, retailer registry API."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import db, notifier, scheduler
from app.checkers import CheckResult
from app.models import CheckEvent, Item, Notification, User, UserSettings, utcnow
from tests.conftest import fake_result

NTFY = "https://ntfy.test/"
TARGET_URL = "https://www.target.com/p/widget/-/A-12345678"


# ------------------------------------------------------------------ scheduler
@pytest.fixture
def world(db_ready):
    with db.SessionLocal() as s:
        u = User(username="u", username_key="u", password_hash="x")
        u.settings = UserSettings(ntfy_server="https://ntfy.test", ntfy_topic="alerts", ntfy_priority=4)
        s.add(u)
        s.flush()
        it = Item(user_id=u.id, name="Widget", url=TARGET_URL, interval_minutes=2, available_keys=[],
                  retailer_config={"fulfillment": "pickup", "zip": "60302", "radius_miles": 10})
        s.add(it)
        s.commit()
        return u.id, it.id


def set_checker(monkeypatch, *results):
    queue = list(results)
    calls = []

    async def fake(kind, url, gc, ac, retailer_config=None):
        calls.append(retailer_config)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr("app.scheduler.run_check", fake)
    return calls


def item(iid) -> Item:
    with db.SessionLocal() as s:
        return s.get(Item, iid)


def notifs(uid):
    with db.SessionLocal() as s:
        return s.query(Notification).filter_by(user_id=uid).order_by(Notification.id).all()


def update_item(iid, **fields):
    with db.SessionLocal() as s:
        it = s.get(Item, iid)
        for k, v in fields.items():
            setattr(it, k, v)
        s.commit()


def update_settings(uid, **fields):
    with db.SessionLocal() as s:
        s.query(UserSettings).filter_by(user_id=uid).update(fields)
        s.commit()


async def test_retailer_config_passed_and_last_in_stock_at(world, monkeypatch, respx_mock):
    uid, iid = world
    respx_mock.post(NTFY).respond(200)
    calls = set_checker(monkeypatch, fake_result("out_of_stock", ()), fake_result("in_stock", ("stock",)),
                        fake_result("out_of_stock", ()))
    await scheduler.check_item(iid)
    assert calls[0] == {"fulfillment": "pickup", "zip": "60302", "radius_miles": 10}
    assert item(iid).last_in_stock_at is None
    await scheduler.check_item(iid)
    seen = item(iid).last_in_stock_at
    assert seen is not None
    await scheduler.check_item(iid)
    assert item(iid).last_in_stock_at == seen  # kept once it goes out of stock again


async def test_alert_names_store_price_and_cart_button(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    res = fake_result("in_stock", ("pickup:1234",), price="$49.99")
    res.available[0].label = "Pickup · Oak Park (3 mi)"
    res.detail = {"retailer": "target", "price_value": 49.99, "cart_url": "https://www.target.com/cart?x=1"}
    set_checker(monkeypatch, res)
    await scheduler.check_item(iid)
    n = notifs(uid)[0]
    assert n.title == "Back in stock at Target: Widget"
    assert n.message == "$49.99 · Pickup · Oak Park (3 mi)"
    payload = json.loads(route.calls.last.request.content)
    assert payload["click"] == TARGET_URL
    assert payload["actions"] == [{"action": "view", "label": "Add to cart",
                                   "url": "https://www.target.com/cart?x=1", "clear": True}]


async def test_no_actions_without_cart_url(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)))
    await scheduler.check_item(iid)
    payload = json.loads(route.calls.last.request.content)
    assert "actions" not in payload
    assert notifs(uid)[0].message == "label-stock"  # no price known


async def test_price_limit_blocks_alert_until_price_drops(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    update_item(iid, max_price=50.0)
    pricey = fake_result("in_stock", ("stock",), price="$59.99")
    pricey.detail = {"price_value": 59.99}
    cheap = fake_result("in_stock", ("stock",), price="$45.00")
    cheap.detail = {"price_value": 45.0}
    set_checker(monkeypatch, pricey, cheap)

    await scheduler.check_item(iid)
    it = item(iid)
    assert it.status == "in_stock" and it.available_keys == []
    assert it.status_text == "In stock · above your $50 limit"
    assert it.last_in_stock_at is not None
    assert notifs(uid) == [] and route.call_count == 0

    await scheduler.check_item(iid)  # dropped under the limit -> alert
    assert item(iid).available_keys == ["stock"]
    assert len(notifs(uid)) == 1 and route.call_count == 1


async def test_price_limit_uses_parsed_price_when_no_price_value(world, monkeypatch, respx_mock):
    uid, iid = world
    respx_mock.post(NTFY).respond(200)
    update_item(iid, max_price=99.5)
    set_checker(monkeypatch, fake_result("in_stock", ("stock",), price="$1,099.00"))
    await scheduler.check_item(iid)
    assert item(iid).status_text.endswith("above your $99.50 limit") and notifs(uid) == []


async def test_price_limit_without_known_price_still_alerts(world, monkeypatch, respx_mock):
    uid, iid = world
    respx_mock.post(NTFY).respond(200)
    update_item(iid, max_price=10.0)
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)))
    await scheduler.check_item(iid)
    assert len(notifs(uid)) == 1


async def test_muted_retailer_never_alerts(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    update_settings(uid, muted_retailers=["target"])
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)))
    await scheduler.check_item(iid)
    it = item(iid)
    assert it.status == "in_stock" and it.available_keys == ["stock"]  # still tracked
    assert it.notify_enabled is True  # not muted by an alert
    assert notifs(uid) == [] and route.call_count == 0


async def test_auto_rearm_alerts_on_every_restock(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    update_settings(uid, auto_rearm=True)
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)), fake_result("in_stock", ("stock",)),
                fake_result("out_of_stock", ()), fake_result("in_stock", ("stock",)))
    await scheduler.check_item(iid)
    assert item(iid).notify_enabled is True and len(notifs(uid)) == 1
    await scheduler.check_item(iid)  # still in stock: no repeat
    assert len(notifs(uid)) == 1
    await scheduler.check_item(iid)  # out
    await scheduler.check_item(iid)  # restock -> alerts again
    assert len(notifs(uid)) == 2 and route.call_count == 2


async def test_without_auto_rearm_item_is_muted_after_alert(world, monkeypatch, respx_mock):
    uid, iid = world
    respx_mock.post(NTFY).respond(200)
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)), fake_result("out_of_stock", ()),
                fake_result("in_stock", ("stock",)))
    for _ in range(3):
        await scheduler.check_item(iid)
    assert item(iid).notify_enabled is False and len(notifs(uid)) == 1


async def test_send_ntfy_backwards_compatible(respx_mock):
    route = respx_mock.post(NTFY).respond(200)

    class S:
        ntfy_server, ntfy_topic, ntfy_token, ntfy_priority = "https://ntfy.test", "t", None, 3

    ok, err = await notifier.send_ntfy(S(), "T", "M", "https://x.test", ["a"], 3)
    assert ok and err is None and "actions" not in json.loads(route.calls.last.request.content)
    actions = [{"action": "view", "label": str(i), "url": "https://x.test"} for i in range(5)]
    await notifier.send_ntfy(S(), "T", "M", actions=actions)
    assert len(json.loads(route.calls.last.request.content)["actions"]) == 3  # ntfy max


# ------------------------------------------------------------------------- API
@pytest.fixture
def preview_mock(monkeypatch):
    async def fake_preview(url):
        return {"name": "Previewed", "image_url": None, "price": "$9.99", "status": "in_stock", "is_apple": False}

    monkeypatch.setattr("app.routers.items.preview_url", fake_preview)


def create(c, **kw):
    r = c.post("/api/items", json={"name": "Widget", "url": TARGET_URL, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_retailers_endpoint(admin):
    rs = admin.get("/api/retailers").json()
    keys = {r["key"] for r in rs}
    assert {"target", "bestbuy", "amazon", "walmart"} <= keys
    target = next(r for r in rs if r["key"] == "target")
    assert target["pickup"] is True and target["seller_filter"] is True and target["color"].startswith("#")
    assert set(target) == {"key", "name", "domain", "color", "pickup", "seller_filter", "note"}


def test_retailers_requires_auth(client):
    assert client.get("/api/retailers").status_code == 401


def test_item_out_has_retailer_fields(admin):
    it = create(admin)
    assert it["retailer"]["key"] == "target"
    for k in ("retailer_config", "max_price", "last_in_stock_at", "product_group", "seller", "third_party",
              "cart_url"):
        assert it[k] is None
    it = create(admin, url="https://unknown-shop.example/p/1")
    assert it["retailer"] is None


def test_item_out_surfaces_last_result_details(admin):
    it = create(admin)
    with db.SessionLocal() as s:
        s.get(Item, it["id"]).last_result = {"retailer": "target", "seller": "Acme Co", "third_party": True,
                                              "cart_url": "https://www.target.com/cart"}
        s.commit()
    got = admin.get(f"/api/items/{it['id']}").json()
    assert got["seller"] == "Acme Co" and got["third_party"] is True
    assert got["cart_url"] == "https://www.target.com/cart"
    assert admin.get("/api/items").json()[0]["cart_url"] == "https://www.target.com/cart"


def test_pickup_defaults_from_settings(admin):
    admin.put("/api/settings", json={"default_zip": "60302", "default_max_distance_miles": 15})
    it = create(admin, retailer_config={"fulfillment": "pickup"}, max_price=49.99)
    cfg = it["retailer_config"]
    assert cfg["fulfillment"] == "pickup" and cfg["zip"] == "60302" and cfg["radius_miles"] == 15
    assert cfg["official_only"] is True and it["max_price"] == 49.99
    # explicit values win
    it = create(admin, retailer_config={"fulfillment": "any", "zip": "10001", "radius_miles": 5})
    assert it["retailer_config"]["zip"] == "10001" and it["retailer_config"]["radius_miles"] == 5


def test_pickup_validation(admin):
    r = admin.post("/api/items", json={"name": "W", "url": TARGET_URL, "retailer_config": {"fulfillment": "pickup"}})
    assert r.status_code == 422 and "ZIP" in r.json()["detail"]
    ok = admin.post("/api/items", json={"name": "W", "url": TARGET_URL,
                                         "retailer_config": {"fulfillment": "pickup", "store_id": "1234"}})
    assert ok.status_code == 201
    r = admin.post("/api/items", json={"name": "W", "url": "https://www.amazon.com/dp/B000000000",
                                        "retailer_config": {"fulfillment": "pickup", "zip": "60302"}})
    assert r.status_code == 422 and "pickup" in r.json()["detail"]
    r = admin.post("/api/items", json={"name": "W", "url": TARGET_URL, "retailer_config": {"fulfillment": "drone"}})
    assert r.status_code == 422
    r = admin.post("/api/items", json={"name": "W", "url": TARGET_URL, "max_price": -1})
    assert r.status_code == 422


def test_patch_price_limit_and_retailer_config(admin):
    it = create(admin)
    got = admin.patch(f"/api/items/{it['id']}", json={"max_price": 25}).json()
    assert got["max_price"] == 25
    got = admin.patch(f"/api/items/{it['id']}", json={"name": "Renamed"}).json()
    assert got["max_price"] == 25  # untouched when omitted
    got = admin.patch(f"/api/items/{it['id']}", json={"max_price": None}).json()
    assert got["max_price"] is None
    got = admin.patch(f"/api/items/{it['id']}",
                      json={"retailer_config": {"fulfillment": "pickup", "zip": "60302", "official_only": False}}).json()
    assert got["retailer_config"]["fulfillment"] == "pickup" and got["retailer_config"]["official_only"] is False
    got = admin.patch(f"/api/items/{it['id']}", json={"retailer_config": None}).json()
    assert got["retailer_config"] is None


def test_track_at_another_store(admin, user2):
    it = create(admin, max_price=99, interval_minutes=5)
    assert admin.get(f"/api/items/{it['id']}/stores").json()[0]["id"] == it["id"]

    r = admin.post(f"/api/items/{it['id']}/stores", json={"url": "https://www.bestbuy.com/site/w/6500000.p?skuId=6500000"})
    assert r.status_code == 201, r.text
    sib = r.json()
    assert sib["name"] == "Widget" and sib["max_price"] == 99 and sib["interval_minutes"] == 5
    assert sib["retailer"]["key"] == "bestbuy" and sib["product_group"]
    assert admin.get(f"/api/items/{it['id']}").json()["product_group"] == sib["product_group"]

    rows = admin.get(f"/api/items/{sib['id']}/stores").json()
    assert [r["id"] for r in rows] == [it["id"], sib["id"]]
    assert rows[0]["retailer"]["key"] == "target" and rows[1]["status"] == "unknown"
    assert {"price", "last_in_stock_at", "last_checked_at", "status_text"} <= set(rows[0])

    # a third store joins the same group; duplicates are rejected
    third = admin.post(f"/api/items/{sib['id']}/stores", json={"url": "https://www.walmart.com/ip/123"}).json()
    assert third["product_group"] == sib["product_group"]
    assert len(admin.get(f"/api/items/{it['id']}/stores").json()) == 3
    dup = admin.post(f"/api/items/{it['id']}/stores", json={"url": "https://www.walmart.com/ip/123"})
    assert dup.status_code == 409
    assert admin.post(f"/api/items/{it['id']}/stores", json={"url": "nope"}).status_code == 422
    # other users can't see or extend the group
    assert user2.get(f"/api/items/{it['id']}/stores").status_code == 404
    assert user2.post(f"/api/items/{it['id']}/stores", json={"url": "https://x.test/p"}).status_code == 404


def test_restock_history(admin):
    it = create(admin)
    t0 = utcnow() - timedelta(hours=10)
    seq = ["out_of_stock", "in_stock", "in_stock", "error", "in_stock", "out_of_stock", "unknown", "in_stock"]
    with db.SessionLocal() as s:
        for i, st in enumerate(seq):
            s.add(CheckEvent(item_id=it["id"], checked_at=t0 + timedelta(hours=i), status=st,
                             status_text=f"s{i}"))
        s.commit()
    rs = admin.get(f"/api/items/{it['id']}/restocks").json()
    assert [r["status_text"] for r in rs] == ["s7", "s1"]  # newest first; error blip isn't a restock
    assert rs[0]["checked_at"].endswith("Z")
    assert len(admin.get(f"/api/items/{it['id']}/restocks?limit=1").json()) == 1


def test_preview_returns_retailer(admin, monkeypatch):
    async def fake_preview(url):
        return {"name": "N", "image_url": None, "price": None, "status": "unknown", "is_apple": False}

    monkeypatch.setattr("app.routers.items.preview_url", fake_preview)
    body = admin.post("/api/items/preview", json={"url": TARGET_URL}).json()
    assert body["retailer"]["key"] == "target"

    async def keyed(url):
        return {"name": "N", "status": "unknown", "is_apple": False, "retailer": "bestbuy"}

    monkeypatch.setattr("app.routers.items.preview_url", keyed)
    assert admin.post("/api/items/preview", json={"url": TARGET_URL}).json()["retailer"]["key"] == "bestbuy"
    body = admin.post("/api/items/preview", json={"url": "https://nowhere.example/x"}).json()
    assert body["retailer"]["key"] == "bestbuy"


def test_settings_muting_and_alert_options(admin):
    body = admin.put("/api/settings", json={"muted_retailers": ["target", "Amazon", "target"],
                                            "auto_rearm": True, "alert_sound": False}).json()
    assert body["muted_retailers"] == ["target", "amazon"]
    assert body["auto_rearm"] is True and body["alert_sound"] is False
    body = admin.put("/api/settings", json={"theme": "light"}).json()
    assert body["muted_retailers"] == ["target", "amazon"]  # omitted -> unchanged
    assert admin.put("/api/settings", json={"muted_retailers": []}).json()["muted_retailers"] == []
    r = admin.put("/api/settings", json={"muted_retailers": ["nope"]})
    assert r.status_code == 422


def test_migration_adds_new_columns(db_ready):
    engine = db.get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_items_product_group")
        conn.exec_driver_sql('ALTER TABLE items DROP COLUMN product_group')
        conn.exec_driver_sql('ALTER TABLE items DROP COLUMN max_price')
        conn.exec_driver_sql('ALTER TABLE user_settings DROP COLUMN alert_sound')
    db.init_db()
    from sqlalchemy import inspect

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("items")}
    assert {"product_group", "max_price", "retailer_config", "last_in_stock_at"} <= cols
    assert "ix_items_product_group" in {ix["name"] for ix in insp.get_indexes("items")}
    assert "alert_sound" in {c["name"] for c in insp.get_columns("user_settings")}


async def test_error_result_type_unchanged(world, monkeypatch):
    uid, iid = world
    set_checker(monkeypatch, CheckResult(status="error", status_text="x", error="x"))
    await scheduler.check_item(iid)
    assert item(iid).last_in_stock_at is None
