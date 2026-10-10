from __future__ import annotations
import pytest

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


def test_auto_cookie_secure_follows_scheme(client, monkeypatch):
    from app import config

    monkeypatch.setenv("COOKIE_SECURE", "auto")
    config.reload_settings()
    # Plain http (LAN): no Secure flag, otherwise browsers drop the cookie and login loops.
    r = client.post("/api/auth/setup", json={"username": "admin", "password": PW})
    assert r.status_code == 200 and "Secure" not in r.headers["set-cookie"]
    # Over https (e.g. a Cloudflare tunnel / reverse proxy): Secure.
    from fastapi.testclient import TestClient
    from app.main import app

    https_client = TestClient(app, base_url="https://testserver")
    r = https_client.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]


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


def test_login_rate_limit_per_username_across_ips():
    from app.security import LoginLimiter
    from fastapi import HTTPException

    lim = LoginLimiter()
    for i in range(30):  # spoofed/rotating IPs, 3x the per-IP limit
        lim.check(f"10.0.0.{i}", "admin")
        lim.fail(f"10.0.0.{i}", "admin")
    with pytest.raises(HTTPException) as e:
        lim.check("10.0.0.99", "admin")
    assert e.value.status_code == 429
    lim.check("10.0.0.99", "someone-else")  # other usernames unaffected


def test_head_health_for_uptime_monitors(client):
    r = client.head("/api/health")
    assert r.status_code == 200 and not r.content
    # other methods on a known path, and unknown paths, are still JSON 404s
    assert client.put("/api/health", json={}).json() == {"detail": "Not found"}
    assert client.head("/api/does-not-exist").status_code == 404


def test_spa_odd_paths_fall_back_to_index(client, tmp_path, monkeypatch):
    from app import config

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>spa</html>")
    monkeypatch.setenv("STATIC_DIR", str(static))
    config.reload_settings()
    for path in ("/" + "a" * 300, "/%00", "/x/%00/y"):  # ENAMETOOLONG / embedded NUL
        r = client.get(path)
        assert r.status_code == 200 and "spa" in r.text, path


@pytest.mark.parametrize(("value", "expected"), [
    ("True", "true"), ("TRUE", "true"), ("1", "true"), ("yes", "true"), ("On", "true"),
    ("False", "false"), ("0", "false"), ("no", "false"), ("OFF", "false"), ("Auto", "auto"), (" auto ", "auto"),
])
def test_cookie_secure_accepts_common_spellings(monkeypatch, value, expected):
    from app import config

    monkeypatch.setenv("COOKIE_SECURE", value)
    assert config.reload_settings().cookie_secure == expected


def test_cookie_secure_rejects_unknown_values(monkeypatch):
    from pydantic import ValidationError

    from app import config

    monkeypatch.setenv("COOKIE_SECURE", "sometimes")
    with pytest.raises(ValidationError):
        config.reload_settings()
    monkeypatch.setenv("COOKIE_SECURE", "false")  # teardown reloads settings before the env is restored


def test_oversized_bodies_are_refused_before_parsing(client):
    from app.security import MAX_JSON_BODY, MAX_MULTIPART_BODY

    # unauthenticated: the body would otherwise be read (and parsed) before the 401
    r = client.post("/api/items", content=b"[" + b" " * MAX_JSON_BODY + b"]", headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"]

    def chunks():  # no Content-Length (chunked): counted as it arrives
        yield b'{"url": "'
        for _ in range(MAX_JSON_BODY // 65536 + 1):
            yield b"a" * 65536
        yield b'"}'

    r = client.post("/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    big = b"\0" * (MAX_MULTIPART_BODY + 1)
    assert client.post("/api/items/1/image", files={"file": ("a.png", big, "image/png")}).status_code == 413
    # an image upload under the multipart cap (but over the JSON one) gets through to auth
    ok = b"\0" * (MAX_JSON_BODY * 2)
    assert client.post("/api/items/1/image", files={"file": ("a.png", ok, "image/png")}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 401


def test_password_hashing_concurrency_is_bounded(monkeypatch):
    """Each argon2 hash/verify takes 64 MiB: a burst of logins must not run them all at once."""
    import threading
    import time

    from app import security

    running = peak = 0
    lock = threading.Lock()

    class SlowHasher:
        def _work(self):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1

        def hash(self, password):
            self._work()
            return "hash"

        def verify(self, password_hash, password):
            self._work()
            return True

    monkeypatch.setattr(security, "_hasher", SlowHasher())
    monkeypatch.setattr(security, "_dummy_hash", None)
    threads = [threading.Thread(target=f, args=a) for f, a in
               [(security.verify_password, ("h", "p"))] * 6 + [(security.burn_password_check, ("p",))] * 4
               + [(security.hash_password, ("p",))] * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= peak <= 2


def test_successful_login_clears_per_username_count():
    from app.security import LoginLimiter

    lim = LoginLimiter()
    for i in range(29):  # just under the per-username cap (3 x 10), from many IPs
        lim.check(f"10.0.0.{i}", "admin")
        lim.fail(f"10.0.0.{i}", "admin")
    lim.reset("192.168.1.50", "admin")  # the real admin logs in
    for i in range(29):  # the earlier failures no longer count towards the cap
        lim.check(f"10.0.1.{i}", "admin")
        lim.fail(f"10.0.1.{i}", "admin")
