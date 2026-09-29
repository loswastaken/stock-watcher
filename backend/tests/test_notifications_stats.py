from __future__ import annotations

from datetime import timedelta

from app import db
from app.models import CheckEvent, Item, Notification, User, utcnow
from tests.conftest import make_client


def uid_of(name):
    with db.SessionLocal() as s:
        return s.query(User).filter_by(username_key=name).one().id


def seed(uid, n=3, item_name="Thing", **kw):
    with db.SessionLocal() as s:
        it = Item(user_id=uid, name=item_name, url="https://s.test/", **kw)
        s.add(it)
        s.flush()
        ids = []
        for i in range(n):
            note = Notification(user_id=uid, item_id=it.id, title=f"t{i}", message="m", url="https://s.test/",
                                created_at=utcnow() - timedelta(minutes=n - i))
            s.add(note)
            s.flush()
            ids.append(note.id)
        s.commit()
        return it.id, ids


def test_list_paginate_read_delete(admin):
    iid, ids = seed(uid_of("admin"), 5)
    r = admin.get("/api/notifications").json()
    assert r["total"] == 5 and r["unread_count"] == 5 and len(r["items"]) == 5
    assert r["items"][0]["title"] == "t4"  # newest first
    n0 = r["items"][0]
    assert n0["item_name"] == "Thing" and n0["item_id"] == iid and n0["created_at"].endswith("Z")
    assert set(n0) == {"id", "item_id", "item_name", "title", "message", "url", "image_url",
                       "created_at", "read", "delivered", "delivery_error"}

    page = admin.get("/api/notifications?limit=2&offset=2").json()
    assert [n["title"] for n in page["items"]] == ["t2", "t1"] and page["total"] == 5

    assert admin.post(f"/api/notifications/{ids[4]}/read").status_code == 204
    r = admin.get("/api/notifications?unread_only=true").json()
    assert r["total"] == 4 and r["unread_count"] == 4 and all(not n["read"] for n in r["items"])

    assert admin.post("/api/notifications/read-all").status_code == 204
    assert admin.get("/api/notifications").json()["unread_count"] == 0

    assert admin.delete(f"/api/notifications/{ids[0]}").status_code == 204
    assert admin.delete(f"/api/notifications/{ids[0]}").status_code == 404
    assert admin.get("/api/notifications").json()["total"] == 4
    assert admin.delete("/api/notifications").status_code == 204
    assert admin.get("/api/notifications").json() == {"items": [], "unread_count": 0, "total": 0}


def test_item_deletion_nulls_item_id(admin):
    iid, ids = seed(uid_of("admin"), 1)
    assert admin.delete(f"/api/items/{iid}").status_code == 204
    n = admin.get("/api/notifications").json()["items"][0]
    assert n["item_id"] is None and n["item_name"] is None and n["title"] == "t0"


def test_notifications_isolated(admin, user2):
    _, ids = seed(uid_of("admin"), 2)
    seed(uid_of("bob"), 1, item_name="Bobs")
    assert user2.get("/api/notifications").json()["total"] == 1
    assert user2.post(f"/api/notifications/{ids[0]}/read").status_code == 404
    assert user2.delete(f"/api/notifications/{ids[0]}").status_code == 404
    user2.post("/api/notifications/read-all")
    user2.delete("/api/notifications")
    assert admin.get("/api/notifications").json()["total"] == 2
    assert admin.get("/api/notifications").json()["unread_count"] == 2


def test_stats(admin, user2):
    empty = admin.get("/api/stats").json()
    assert empty == {"total": 0, "in_stock": 0, "out_of_stock": 0, "unknown": 0, "error": 0, "paused": 0,
                     "unread_notifications": 0, "checks_24h": 0, "alerts_24h": 0}
    uid = uid_of("admin")
    with db.SessionLocal() as s:
        items = [
            Item(user_id=uid, name="a", url="https://s.test/a", status="in_stock"),
            Item(user_id=uid, name="b", url="https://s.test/b", status="out_of_stock"),
            Item(user_id=uid, name="c", url="https://s.test/c", status="out_of_stock", enabled=False),
            Item(user_id=uid, name="d", url="https://s.test/d", status="error"),
            Item(user_id=uid, name="e", url="https://s.test/e", status="unknown"),
        ]
        s.add_all(items)
        s.flush()
        now = utcnow()
        s.add(CheckEvent(item_id=items[0].id, status="in_stock", checked_at=now - timedelta(hours=1)))
        s.add(CheckEvent(item_id=items[0].id, status="in_stock", checked_at=now - timedelta(hours=2)))
        s.add(CheckEvent(item_id=items[1].id, status="out_of_stock", checked_at=now - timedelta(hours=30)))
        s.add(Notification(user_id=uid, item_id=items[0].id, title="x", created_at=now - timedelta(hours=1)))
        s.add(Notification(user_id=uid, item_id=items[0].id, title="old", created_at=now - timedelta(hours=48), read=True))
        s.commit()
    s = admin.get("/api/stats").json()
    assert s == {"total": 5, "in_stock": 1, "out_of_stock": 2, "unknown": 1, "error": 1, "paused": 1,
                 "unread_notifications": 1, "checks_24h": 2, "alerts_24h": 1}
    # other user sees nothing of it
    assert user2.get("/api/stats").json()["total"] == 0
