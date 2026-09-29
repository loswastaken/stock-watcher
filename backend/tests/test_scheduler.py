from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from app import db, scheduler
from app.checkers import Availability, CheckResult
from app.models import CheckEvent, Item, Notification, User, UserSettings, utcnow
from tests.conftest import fake_result

NTFY = "https://ntfy.test/"


@pytest.fixture
def world(db_ready):
    """A user with ntfy configured and one item; returns (user_id, item_id)."""
    with db.SessionLocal() as s:
        u = User(username="u", username_key="u", password_hash="x")
        u.settings = UserSettings(ntfy_server="https://ntfy.test", ntfy_topic="alerts", ntfy_token="tk", ntfy_priority=5)
        s.add(u)
        s.flush()
        it = Item(user_id=u.id, name="Widget", url="https://shop.test/w", interval_minutes=2, available_keys=[])
        s.add(it)
        s.commit()
        return u.id, it.id


def set_checker(monkeypatch, *results):
    """run_check returns the given results in order (last one repeats)."""
    queue = list(results)
    calls = []

    async def fake(kind, url, gc, ac):
        calls.append((kind, url))
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr("app.scheduler.run_check", fake)
    return calls


def item(iid) -> Item:
    with db.SessionLocal() as s:
        return s.get(Item, iid)


def notifs(uid):
    with db.SessionLocal() as s:
        return s.query(Notification).filter_by(user_id=uid).order_by(Notification.id).all()


async def test_new_keys_alert_once_and_delivery_recorded(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200, json={})
    set_checker(monkeypatch, fake_result("in_stock", ("stock",), price="$10"))

    assert await scheduler.check_item(iid) is True
    it = item(iid)
    assert it.status == "in_stock" and it.available_keys == ["stock"] and it.price == "$10"
    assert it.consecutive_errors == 0 and it.last_change_at is not None
    ns = notifs(uid)
    assert len(ns) == 1 and ns[0].delivered is True and ns[0].delivery_error is None
    assert ns[0].item_id == iid and "Widget" in ns[0].title and ns[0].message == "label-stock"
    assert route.call_count == 1
    req = route.calls.last.request
    payload = json.loads(req.content)
    assert payload["topic"] == "alerts" and payload["priority"] == 5
    assert payload["click"] == "https://shop.test/w" and payload["tags"] == ["shopping_cart"]
    assert "Widget" in payload["title"]
    assert req.headers["authorization"] == "Bearer tk"

    # same keys again -> no new notification
    await scheduler.check_item(iid)
    assert len(notifs(uid)) == 1 and route.call_count == 1
    with db.SessionLocal() as s:
        assert s.query(CheckEvent).filter_by(item_id=iid).count() == 2
        last = s.query(CheckEvent).filter_by(item_id=iid).order_by(CheckEvent.id.desc()).first()
        assert last.changed is False


async def test_only_newly_added_keys_are_summarised(world, monkeypatch, respx_mock):
    uid, iid = world
    respx_mock.post(NTFY).respond(200)
    set_checker(
        monkeypatch,
        fake_result("in_stock", ("pickup:R1:P",)),
        fake_result("in_stock", ("pickup:R1:P", "pickup:R2:P", "delivery2h:P")),
    )
    await scheduler.check_item(iid)
    await scheduler.check_item(iid)
    ns = notifs(uid)
    assert len(ns) == 2
    assert ns[1].message == "label-pickup:R2:P; label-delivery2h:P"  # single summarized notification


async def test_key_disappears_then_returns_alerts_again(world, monkeypatch, respx_mock):
    uid, iid = world
    respx_mock.post(NTFY).respond(200)
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)), fake_result("out_of_stock", ()), fake_result("in_stock", ("stock",)))
    for _ in range(3):
        await scheduler.check_item(iid)
    assert len(notifs(uid)) == 2  # no oos notice (setting off), second restock alerts


async def test_error_keeps_previous_keys_and_counts(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    set_checker(
        monkeypatch,
        fake_result("in_stock", ("stock",)),
        CheckResult(status="error", status_text="HTTP 503", error="HTTP 503"),
        CheckResult(status="error", status_text="HTTP 503", error="HTTP 503"),
        fake_result("in_stock", ("stock",)),
    )
    await scheduler.check_item(iid)
    await scheduler.check_item(iid)
    it = item(iid)
    assert it.status == "error" and it.consecutive_errors == 1 and it.last_error == "HTTP 503"
    assert it.available_keys == ["stock"]  # kept
    await scheduler.check_item(iid)
    assert item(iid).consecutive_errors == 2
    await scheduler.check_item(iid)  # recovery with same key: no flapping alert
    it = item(iid)
    assert it.status == "in_stock" and it.consecutive_errors == 0 and it.last_error is None
    assert len(notifs(uid)) == 1 and route.call_count == 1


async def test_checker_exception_becomes_error(world, monkeypatch):
    uid, iid = world
    set_checker(monkeypatch, RuntimeError("kaboom"))
    await scheduler.check_item(iid)
    it = item(iid)
    assert it.status == "error" and "kaboom" in it.last_error and it.consecutive_errors == 1


async def test_ntfy_failure_recorded(world, monkeypatch, respx_mock):
    uid, iid = world
    respx_mock.post(NTFY).respond(500, text="boom")
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)))
    await scheduler.check_item(iid)
    n = notifs(uid)[0]
    assert n.delivered is False and "500" in n.delivery_error


async def test_no_topic_or_notify_disabled_skips_ntfy(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    with db.SessionLocal() as s:
        s.get(Item, iid).notify_enabled = False
        s.commit()
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)), fake_result("in_stock", ("stock", "other")))
    await scheduler.check_item(iid)
    assert route.call_count == 0
    n = notifs(uid)
    assert len(n) == 1 and n[0].delivered is False and n[0].delivery_error is None  # in-app only

    with db.SessionLocal() as s:
        s.get(Item, iid).notify_enabled = True
        s.query(UserSettings).update({"ntfy_topic": None})
        s.commit()
    await scheduler.check_item(iid)
    assert route.call_count == 0 and len(notifs(uid)) == 2


async def test_out_of_stock_notice_optional(world, monkeypatch, respx_mock):
    uid, iid = world
    route = respx_mock.post(NTFY).respond(200)
    with db.SessionLocal() as s:
        s.query(UserSettings).update({"notify_on_out_of_stock": True})
        s.commit()
    set_checker(monkeypatch, fake_result("in_stock", ("stock",)), fake_result("out_of_stock", (), status_text="Sold out"), fake_result("out_of_stock", ()))
    for _ in range(3):
        await scheduler.check_item(iid)
    ns = notifs(uid)
    assert len(ns) == 2 and ns[1].title.startswith("Out of stock") and ns[1].message == "Sold out"
    assert route.call_count == 2  # repeated out_of_stock does not renotify
    assert item(iid).available_keys == []


async def test_history_pruned_to_500(world, monkeypatch):
    uid, iid = world
    with db.SessionLocal() as s:
        s.add_all([CheckEvent(item_id=iid, status="unknown", status_text="", duration_ms=1) for _ in range(505)])
        s.commit()
    set_checker(monkeypatch, fake_result("out_of_stock", ()))
    await scheduler.check_item(iid)
    with db.SessionLocal() as s:
        assert s.query(CheckEvent).filter_by(item_id=iid).count() == 500


async def test_no_concurrent_duplicate_checks(world, monkeypatch):
    uid, iid = world
    gate = asyncio.Event()
    started = 0

    async def slow(kind, url, gc, ac):
        nonlocal started
        started += 1
        await gate.wait()
        return fake_result("out_of_stock", ())

    monkeypatch.setattr("app.scheduler.run_check", slow)
    t1 = asyncio.create_task(scheduler.check_item(iid))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(scheduler.check_item(iid))  # waits for the running one
    t3 = await scheduler.check_item(iid, wait=False)  # returns immediately
    assert t3 is False
    await asyncio.sleep(0.05)
    assert not t2.done()
    gate.set()
    assert await t1 is True and await t2 is False
    assert started == 1


async def test_deleted_item_mid_check(world, monkeypatch):
    uid, iid = world

    async def deleting(kind, url, gc, ac):
        with db.SessionLocal() as s:
            s.delete(s.get(Item, iid))
            s.commit()
        return fake_result("in_stock", ("stock",))

    monkeypatch.setattr("app.scheduler.run_check", deleting)
    await scheduler.check_item(iid)  # must not raise
    assert notifs(uid) == []
    assert await scheduler.check_item(iid) is False  # missing item


async def test_due_selection(world):
    uid, iid = world
    assert scheduler.select_due_items() == [iid]  # never checked
    with db.SessionLocal() as s:
        it = s.get(Item, iid)
        it.last_checked_at = utcnow() - timedelta(seconds=30)
        s.commit()
    assert scheduler.select_due_items() == []
    with db.SessionLocal() as s:
        s.get(Item, iid).last_checked_at = utcnow() - timedelta(minutes=3)
        s.commit()
    assert scheduler.select_due_items() == [iid]
    with db.SessionLocal() as s:
        s.get(Item, iid).enabled = False
        s.commit()
    assert scheduler.select_due_items() == []


async def test_tick_runs_due_items_with_concurrency_limit(world, monkeypatch):
    uid, iid = world
    with db.SessionLocal() as s:
        for n in range(4):
            s.add(Item(user_id=uid, name=f"i{n}", url=f"https://s.test/{n}", available_keys=[]))
        s.commit()
    running = 0
    peak = 0

    async def fake(kind, url, gc, ac):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return fake_result("out_of_stock", ())

    monkeypatch.setattr("app.scheduler.run_check", fake)
    monkeypatch.setenv("CHECK_CONCURRENCY", "2")
    from app import config

    config.reload_settings()
    scheduler.start()
    try:
        assert await scheduler.tick() == 5
        assert await scheduler.tick() == 0  # all queued/in flight, not started twice
        await asyncio.gather(*list(scheduler._background))  # drain deterministically
        assert not scheduler._queued and not scheduler._inflight
    finally:
        await scheduler.stop()
    assert peak == 2
    with db.SessionLocal() as s:
        assert all(i.last_checked_at is not None for i in s.query(Item).all())


async def test_auto_image_fetch_from_result(world, monkeypatch, respx_mock):
    import io
    from PIL import Image

    uid, iid = world
    buf = io.BytesIO()
    Image.new("RGB", (50, 50)).save(buf, "PNG")
    respx_mock.get("https://cdn.test/a.png").respond(200, content=buf.getvalue())
    set_checker(monkeypatch, fake_result("out_of_stock", (), image_url="https://cdn.test/a.png"))
    await scheduler.check_item(iid)
    assert item(iid).image_path.endswith(".webp")
