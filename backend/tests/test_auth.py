from __future__ import annotations

from tests.conftest import PW, make_client


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "dev"}


def test_setup_flow(client):
    r = client.get("/api/auth/status")
    assert r.json() == {"setup_required": True, "user": None}
    assert client.get("/api/auth/me").status_code == 401

    r = client.post("/api/auth/setup", json={"username": "Admin", "password": "short"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)

    r = client.post("/api/auth/setup", json={"username": "Admin", "password": PW})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "Admin" and body["is_admin"] is True and body["created_at"].endswith("Z")
    assert "password_hash" not in body
    sc = r.headers["set-cookie"]
    assert "sw_session=" in sc and "HttpOnly" in sc and "SameSite=lax" in sc
    assert "Secure" not in sc  # COOKIE_SECURE=false in tests

    st = client.get("/api/auth/status").json()
    assert st["setup_required"] is False and st["user"]["username"] == "Admin"
    assert client.get("/api/auth/me").json()["username"] == "Admin"

    # setup only once
    r = make_client().post("/api/auth/setup", json={"username": "x2", "password": PW})
    assert r.status_code == 409


def test_secure_cookie_flag(client, monkeypatch):
    from app import config

    monkeypatch.setenv("COOKIE_SECURE", "true")
    config.reload_settings()
    r = client.post("/api/auth/setup", json={"username": "admin", "password": PW})
    assert "Secure" in r.headers["set-cookie"]


def test_login_logout(admin):
    c = make_client()
    assert c.post("/api/auth/login", json={"username": "admin", "password": "wrongpass1"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "ADMIN", "password": PW})  # case-insensitive
    assert r.status_code == 200
    assert c.get("/api/auth/me").status_code == 200
    r = c.post("/api/auth/logout")
    assert r.status_code == 204
    assert c.get("/api/auth/me").status_code == 401
    # logging out again is harmless
    assert c.post("/api/auth/logout").status_code == 204


def test_session_expiry(admin):
    from app import db
    from app.models import Session as SessionRow, utcnow
    from datetime import timedelta

    with db.SessionLocal() as s:
        for row in s.query(SessionRow).all():
            row.expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    assert admin.get("/api/auth/me").status_code == 401


def test_token_stored_hashed(admin):
    from app import db
    from app.models import Session as SessionRow

    token = admin.cookies.get("sw_session")
    with db.SessionLocal() as s:
        ids = [r.id for r in s.query(SessionRow).all()]
    assert token and token not in ids and len(ids) == 1 and len(ids[0]) == 64


def test_login_rate_limit(admin):
    c = make_client()
    for _ in range(10):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "nope-nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 429
    assert "retry-after" in r.headers
    # a different username is not blocked
    assert c.post("/api/auth/login", json={"username": "other", "password": "nope-nope"}).status_code == 401


def test_change_password_invalidates_other_sessions(admin):
    other = make_client()
    assert other.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 200
    r = admin.post("/api/auth/change-password", json={"current_password": "bad-bad-bad", "new_password": "new-password-1"})
    assert r.status_code == 400
    r = admin.post("/api/auth/change-password", json={"current_password": PW, "new_password": "short"})
    assert r.status_code == 422
    r = admin.post("/api/auth/change-password", json={"current_password": PW, "new_password": "new-password-1"})
    assert r.status_code == 204
    assert admin.get("/api/auth/me").status_code == 200  # current session survives
    assert other.get("/api/auth/me").status_code == 401  # others don't
    assert make_client().post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 401
    assert make_client().post("/api/auth/login", json={"username": "admin", "password": "new-password-1"}).status_code == 200


def test_csrf_origin_and_content_type(admin):
    r = admin.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = admin.post("/api/items/preview", json={"url": "https://x.test/"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = admin.post("/api/settings/test-notification", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    r = admin.post("/api/settings/test-notification", headers={"Referer": "http://evil.example/page"})
    assert r.status_code == 403
    r = admin.post("/api/auth/change-password", content="a=b", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    # forwarded host (reverse proxy) is accepted
    r = admin.post(
        "/api/settings/test-notification",
        headers={"Origin": "https://watch.example.com", "X-Forwarded-Host": "watch.example.com"},
    )
    assert r.status_code == 200
    # GET is never blocked by origin
    assert admin.get("/api/auth/me", headers={"Origin": "http://evil.example"}).status_code == 200


def test_unknown_api_route_is_json_404(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and r.json() == {"detail": "Not found"}


def test_spa_serving(client, tmp_path, monkeypatch):
    from app import config

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>spa</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("top secret")
    monkeypatch.setenv("STATIC_DIR", str(static))
    config.reload_settings()
    assert "spa" in client.get("/").text
    assert "spa" in client.get("/items/12").text
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert "top secret" not in client.get("/..%2fsecret.txt").text
    assert "top secret" not in client.get("/%2e%2e/secret.txt").text
    assert client.get("/api/nope").json() == {"detail": "Not found"}
