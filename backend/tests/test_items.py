from __future__ import annotations

import io

import pytest
from PIL import Image

from tests.conftest import fake_result, make_client


def png_bytes(size=(1200, 900), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def preview_mock(monkeypatch):
    calls = []

    async def fake_preview(url):
        calls.append(url)
        return {"name": "Previewed Name", "image_url": None, "price": "$9.99", "status": "in_stock", "is_apple": False}

    monkeypatch.setattr("app.routers.items.preview_url", fake_preview)
    return calls


def create(c, **kw):
    body = {"url": "https://shop.example.com/p/1", **kw}
    r = c.post("/api/items", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_item_defaults_from_preview(admin, preview_mock):
    item = create(admin)
    assert item["name"] == "Previewed Name"
    assert item["kind"] == "generic" and item["enabled"] is True and item["notify_enabled"] is True
    assert item["interval_minutes"] == 2
    assert item["status"] == "unknown" and item["image_url"] is None
    assert item["generic_config"] == {"mode": "auto", "selector": None, "in_stock_text": None,
                                      "out_of_stock_text": None, "render_js": False}
    assert item["apple_config"] is None and item["last_result"] == {}
    assert item["created_at"].endswith("Z") and item["last_checked_at"] is None
    assert preview_mock == ["https://shop.example.com/p/1"]


def test_create_item_preview_failure_falls_back_to_host(admin, monkeypatch):
    async def boom(url):
        raise RuntimeError("nope")

    monkeypatch.setattr("app.routers.items.preview_url", boom)
    assert create(admin)["name"] == "shop.example.com"


def test_create_item_explicit_name_skips_preview(admin, preview_mock):
    item = create(admin, name="  My Thing ", interval_minutes=5, notify_enabled=False)
    assert item["name"] == "My Thing" and item["interval_minutes"] == 5 and item["notify_enabled"] is False
    assert preview_mock == []


def test_create_validation(admin, preview_mock):
    for bad in ("ftp://x.com/a", "javascript:alert(1)", "not a url", "", "https://"):
        r = admin.post("/api/items", json={"url": bad})
        assert r.status_code == 422, bad
        assert isinstance(r.json()["detail"], str)
    assert admin.post("/api/items", json={"url": "https://a.test/", "interval_minutes": 0}).status_code == 422
    assert admin.post("/api/items", json={"url": "https://a.test/", "kind": "apple"}).status_code == 422
    r = admin.post("/api/items", json={"url": "https://a.test/", "generic_config": {"mode": "selector"}})
    assert r.status_code == 422


def test_min_interval_setting(admin, preview_mock, monkeypatch):
    from app import config

    monkeypatch.setenv("MIN_INTERVAL_SECONDS", "300")
    config.reload_settings()
    r = admin.post("/api/items", json={"url": "https://a.test/", "interval_minutes": 2})
    assert r.status_code == 422 and "at least 5" in r.json()["detail"]
    assert admin.post("/api/items", json={"url": "https://a.test/", "interval_minutes": 5}).status_code == 201


def test_apple_item_uses_user_defaults(admin, preview_mock):
    admin.put("/api/settings", json={"default_zip": "95014", "default_max_distance_miles": 40, "default_interval_minutes": 3})
    item = create(
        admin,
        url="https://www.apple.com/shop/buy-iphone/iphone-17-pro",
        apple_config={"parts": [{"part_number": "MG8H4LL/A", "label": "256GB"}, {"part_number": "MG8J4LL/A"}]},
    )
    assert item["kind"] == "apple" and item["interval_minutes"] == 3
    cfg = item["apple_config"]
    assert cfg["zip"] == "95014" and cfg["max_distance_miles"] == 40
    assert cfg["watch_pickup"] is True and cfg["watch_delivery"] is True
    assert cfg["parts"][1] == {"part_number": "MG8J4LL/A", "label": "MG8J4LL/A"}


def test_crud_and_history(admin, preview_mock):
    item = create(admin)
    iid = item["id"]
    assert [i["id"] for i in admin.get("/api/items").json()] == [iid]
    assert admin.get(f"/api/items/{iid}").json()["id"] == iid

    r = admin.patch(f"/api/items/{iid}", json={"name": "Renamed", "enabled": False, "interval_minutes": 10})
    body = r.json()
    assert body["name"] == "Renamed" and body["enabled"] is False and body["interval_minutes"] == 10
    assert admin.patch(f"/api/items/{iid}", json={"name": ""}).status_code == 422
    assert admin.patch(f"/api/items/{iid}", json={"url": "ftp://x"}).status_code == 422
    assert admin.patch(f"/api/items/{iid}", json={"apple_config": {"parts": [{"part_number": "X"}]}}).status_code == 422
    assert admin.get(f"/api/items/{iid}/history").json() == []
    assert admin.get(f"/api/items/{iid}/history?limit=0").status_code == 422

    assert admin.delete(f"/api/items/{iid}").status_code == 204
    assert admin.get(f"/api/items/{iid}").status_code == 404
    assert admin.delete(f"/api/items/{iid}").status_code == 404
    assert admin.get("/api/items").json() == []


def test_manual_check_runs_scheduler_path(admin, preview_mock, monkeypatch):
    async def fake_run_check(kind, url, gc, ac, retailer_config=None):
        return fake_result("in_stock", ("stock",), price="$5.00", detail={"signals": ["json-ld: InStock"]})

    monkeypatch.setattr("app.scheduler.run_check", fake_run_check)
    item = create(admin)
    r = admin.post(f"/api/items/{item['id']}/check")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_stock" and body["price"] == "$5.00"
    assert body["last_checked_at"].endswith("Z") and body["last_change_at"]
    assert body["last_result"] == {"signals": ["json-ld: InStock"]}
    hist = admin.get(f"/api/items/{item['id']}/history").json()
    assert len(hist) == 1 and hist[0]["status"] == "in_stock" and hist[0]["changed"] is True
    assert admin.post("/api/items/9999/check").status_code == 404


def test_preview_and_apple_resolve_endpoints(admin, monkeypatch):
    async def fake_preview(url):
        return {"name": "N", "image_url": "https://i/x.jpg", "price": None, "status": "unknown", "is_apple": True}

    async def fake_resolve(url):
        return {"product_name": "iPhone", "image_url": None, "variants": [{"part_number": "A/B", "label": "256", "price": "$1"}]}

    monkeypatch.setattr("app.routers.items.preview_url", fake_preview)
    monkeypatch.setattr("app.routers.apple.resolve_apple", fake_resolve)
    r = admin.post("/api/items/preview", json={"url": "https://www.apple.com/x"})
    assert r.json()["name"] == "N" and r.json()["is_apple"] is True
    r = admin.post("/api/apple/resolve", json={"url": "https://www.apple.com/x"})
    assert r.json()["variants"][0]["part_number"] == "A/B"
    assert admin.post("/api/apple/resolve", json={"url": "nope"}).status_code == 422

    async def boom(url):
        raise RuntimeError("x")

    monkeypatch.setattr("app.routers.items.preview_url", boom)
    monkeypatch.setattr("app.routers.apple.resolve_apple", boom)
    r = admin.post("/api/items/preview", json={"url": "https://a.test/"})
    assert r.status_code == 200 and r.json()["error"]
    r = admin.post("/api/apple/resolve", json={"url": "https://a.test/"})
    assert r.status_code == 200 and r.json()["variants"] == [] and r.json()["error"]


def test_auth_required(client):
    for method, path in [("get", "/api/items"), ("post", "/api/items/1/check"), ("get", "/api/stats"),
                         ("get", "/api/notifications"), ("get", "/api/settings"), ("get", "/api/images/abc12345.webp"),
                         ("post", "/api/apple/resolve"), ("post", "/api/items/preview")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_isolation_between_users(admin, user2, preview_mock):
    item = create(admin, name="Admin thing")
    iid = item["id"]
    assert user2.get("/api/items").json() == []
    assert user2.get(f"/api/items/{iid}").status_code == 404
    assert user2.patch(f"/api/items/{iid}", json={"name": "hax"}).status_code == 404
    assert user2.delete(f"/api/items/{iid}").status_code == 404
    assert user2.post(f"/api/items/{iid}/check").status_code == 404
    assert user2.get(f"/api/items/{iid}/history").status_code == 404
    assert user2.post(f"/api/items/{iid}/image", files={"file": ("a.png", png_bytes(), "image/png")}).status_code == 404
    assert user2.post(f"/api/items/{iid}/image/refresh").status_code == 404
    assert admin.get(f"/api/items/{iid}").json()["name"] == "Admin thing"


def test_image_upload_serving_and_isolation(admin, user2, preview_mock, tmp_path):
    item = create(admin)
    iid = item["id"]
    r = admin.post(f"/api/items/{iid}/image", files={"file": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["image_url"]
    assert url.startswith("/api/images/") and url.endswith(".webp")

    got = admin.get(url)
    assert got.status_code == 200 and got.headers["content-type"] == "image/webp"
    im = Image.open(io.BytesIO(got.content))
    assert max(im.size) <= 800

    # another user cannot fetch it, nor can anonymous
    assert user2.get(url).status_code == 404
    assert make_client().get(url).status_code == 401

    # traversal / unreferenced files
    assert admin.get("/api/images/..%2f..%2fsecret.key").status_code == 404
    assert admin.get("/api/images/%2e%2e%2fstockwatcher.db").status_code == 404
    assert admin.get("/api/images/unknown12345678.webp").status_code == 404

    # replacing deletes the old file
    from app import config

    old = url.rsplit("/", 1)[1]
    r = admin.post(f"/api/items/{iid}/image", files={"file": ("b.png", png_bytes(color=(0, 0, 255)), "image/png")})
    new = r.json()["image_url"].rsplit("/", 1)[1]
    assert new != old
    files = {p.name for p in config.get_settings().images_dir.iterdir()}
    assert files == {new}

    # deleting the item removes the file
    admin.delete(f"/api/items/{iid}")
    assert not list(config.get_settings().images_dir.iterdir())


def test_image_upload_rejects_bad_files(admin, preview_mock, monkeypatch):
    iid = create(admin)["id"]
    r = admin.post(f"/api/items/{iid}/image", files={"file": ("a.png", b"not an image", "image/png")})
    assert r.status_code == 400 and r.json()["detail"]
    r = admin.post(f"/api/items/{iid}/image", files={"file": ("a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")})
    assert r.status_code == 400
    monkeypatch.setattr("app.images.MAX_BYTES", 1000)
    r = admin.post(f"/api/items/{iid}/image", files={"file": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 413


def test_image_refresh(admin, respx_mock, monkeypatch):
    async def fake_preview(url):
        return {"name": "N", "image_url": "https://cdn.example.com/p.png", "price": None, "status": "unknown", "is_apple": False}

    monkeypatch.setattr("app.routers.items.preview_url", fake_preview)
    iid = create(admin, name="x")["id"]
    respx_mock.get("https://cdn.example.com/p.png").respond(200, content=png_bytes(), headers={"content-type": "image/png"})
    r = admin.post(f"/api/items/{iid}/image/refresh")
    assert r.status_code == 200 and r.json()["image_url"].endswith(".webp")

    respx_mock.get("https://cdn.example.com/p.png").respond(404)
    r = admin.post(f"/api/items/{iid}/image/refresh")
    assert r.status_code == 422


async def test_create_downloads_image_in_background(db_ready, respx_mock, monkeypatch):
    """The post-create task downloads the image, then runs the first check."""
    from app import db, scheduler
    from app.models import Item, User
    from app.routers.items import _post_create

    with db.SessionLocal() as s:
        u = User(username="u", username_key="u", password_hash="x")
        s.add(u); s.flush()
        it = Item(user_id=u.id, name="n", url="https://a.test/")
        s.add(it); s.commit()
        iid = it.id

    async def fake_run_check(*a, **k):
        return fake_result("out_of_stock", ())

    monkeypatch.setattr("app.scheduler.run_check", fake_run_check)
    respx_mock.get("https://cdn.test/i.png").respond(200, content=png_bytes(), headers={"content-type": "image/png"})
    await _post_create(iid, "https://cdn.test/i.png")
    with db.SessionLocal() as s:
        it = s.get(Item, iid)
        assert it.image_path and it.image_path.endswith(".webp")
        assert it.status == "out_of_stock" and it.last_checked_at is not None


def test_check_all_queues_only_own_active_items(admin, preview_mock):
    from app import scheduler

    a = create(admin)
    b = create(admin, url="https://shop.example.com/p/2")
    admin.patch(f"/api/items/{b['id']}", json={"enabled": False})  # paused: skipped
    other = make_client()
    other.post("/api/auth/setup", json={"username": "x", "password": "x" * 8})  # no-op: setup already done
    scheduler._queued.clear()
    try:
        r = admin.post("/api/items/check-all")
        assert r.status_code == 202 and r.json() == {"queued": 1, "total": 1}
        assert scheduler._queued == {a["id"]}
        # already queued -> not started twice
        assert admin.post("/api/items/check-all").json() == {"queued": 0, "total": 1}
    finally:
        scheduler._queued.clear()
    assert make_client().post("/api/items/check-all").status_code == 401


def test_purchase_moves_item_out_of_watching(admin, preview_mock):
    from app import db, scheduler
    from app.models import Item

    a = create(admin)
    b = create(admin, url="https://shop.example.com/p/2")
    with db.SessionLocal() as s:
        it = s.get(Item, a["id"])
        it.price, it.status, it.available_keys = "$10.00", "in_stock", ["stock"]
        s.commit()

    r = admin.post(f"/api/items/{a['id']}/purchase")
    assert r.status_code == 200
    p = r.json()
    assert p["purchased_at"].endswith("Z") and p["purchased_price"] == "$10.00"
    first = p["purchased_at"]
    assert admin.post(f"/api/items/{a['id']}/purchase").json()["purchased_at"] == first  # idempotent

    # not checked any more, not counted as watched
    assert a["id"] not in scheduler.select_due_items() and b["id"] in scheduler.select_due_items()
    scheduler._queued.clear()
    try:
        assert admin.post("/api/items/check-all").json()["total"] == 1
    finally:
        scheduler._queued.clear()
    st = admin.get("/api/stats").json()
    assert st["total"] == 1 and st["purchased"] == 1 and st["in_stock"] == 0

    # back to watching: alerts re-armed, re-checked soon, next in-stock alerts again
    u = admin.post(f"/api/items/{a['id']}/unpurchase").json()
    assert u["purchased_at"] is None and u["purchased_price"] is None
    assert u["enabled"] is True and u["notify_enabled"] is True and u["last_checked_at"] is None
    with db.SessionLocal() as s:
        assert s.get(Item, a["id"]).available_keys == []
    assert admin.get("/api/stats").json()["purchased"] == 0


def test_purchase_is_scoped_to_owner(admin, preview_mock):
    a = create(admin)
    admin.post("/api/users", json={"username": "eve", "password": "evepassword1", "is_admin": False})
    c = make_client()
    c.post("/api/auth/login", json={"username": "eve", "password": "evepassword1"})
    assert c.post(f"/api/items/{a['id']}/purchase").status_code == 404
    assert c.post(f"/api/items/{a['id']}/unpurchase").status_code == 404
