from __future__ import annotations

from tests.conftest import PW, make_client


def test_users_require_admin(admin, user2):
    assert user2.get("/api/users").status_code == 403
    assert user2.post("/api/users", json={"username": "x", "password": PW}).status_code == 403
    assert make_client().get("/api/users").status_code == 401
    r = admin.get("/api/users")
    assert [u["username"] for u in r.json()] == ["admin", "bob"]


def test_create_user_validation(admin):
    assert admin.post("/api/users", json={"username": "bob", "password": PW}).status_code == 201
    r = admin.post("/api/users", json={"username": "BOB", "password": PW})
    assert r.status_code == 409 and r.json()["detail"]
    assert admin.post("/api/users", json={"username": "bad name", "password": PW}).status_code == 422
    assert admin.post("/api/users", json={"username": "carl", "password": "short"}).status_code == 422


def test_last_admin_protection(admin):
    me = admin.get("/api/auth/me").json()
    r = admin.patch(f"/api/users/{me['id']}", json={"is_admin": False})
    assert r.status_code == 400
    r = admin.delete(f"/api/users/{me['id']}")
    assert r.status_code == 400  # cannot delete self

    bob = admin.post("/api/users", json={"username": "bob", "password": PW, "is_admin": True}).json()
    # two admins: demoting one is fine
    assert admin.patch(f"/api/users/{bob['id']}", json={"is_admin": False}).json()["is_admin"] is False
    # and promoting back / demoting self while another admin exists
    admin.patch(f"/api/users/{bob['id']}", json={"is_admin": True})
    assert admin.patch(f"/api/users/{me['id']}", json={"is_admin": False}).status_code == 200


def test_patch_password_logs_user_out_and_delete_cascades(admin, user2):
    bob_id = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "bob")
    assert user2.get("/api/auth/me").status_code == 200
    assert admin.patch(f"/api/users/{bob_id}", json={"password": "another-pass-1"}).status_code == 200
    assert user2.get("/api/auth/me").status_code == 401
    assert make_client().post("/api/auth/login", json={"username": "bob", "password": "another-pass-1"}).status_code == 200
    assert admin.delete(f"/api/users/{bob_id}").status_code == 204
    assert admin.delete(f"/api/users/{bob_id}").status_code == 404
    assert make_client().post("/api/auth/login", json={"username": "bob", "password": "another-pass-1"}).status_code == 401


def test_settings_defaults_and_token_masking(admin):
    s = admin.get("/api/settings").json()
    assert s == {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": None,
        "ntfy_token_set": False,
        "ntfy_priority": 4,
        "default_interval_minutes": 2,
        "default_zip": None,
        "default_max_distance_miles": 25,
        "notify_on_out_of_stock": False,
        "theme": "dark",
        "muted_retailers": [],
        "auto_rearm": False,
        "alert_sound": True,
    }
    r = admin.put("/api/settings", json={"ntfy_topic": "my-topic", "ntfy_token": "tk_secret", "ntfy_server": "https://ntfy.example.com/"})
    body = r.json()
    assert body["ntfy_token_set"] is True and "ntfy_token" not in body
    assert "tk_secret" not in r.text and "tk_secret" not in admin.get("/api/settings").text
    assert body["ntfy_server"] == "https://ntfy.example.com"
    # omitting keeps the token
    body = admin.put("/api/settings", json={"theme": "light", "default_zip": "95014"}).json()
    assert body["ntfy_token_set"] is True and body["theme"] == "light" and body["default_zip"] == "95014"
    # empty string clears
    assert admin.put("/api/settings", json={"ntfy_token": ""}).json()["ntfy_token_set"] is False
    # validation
    assert admin.put("/api/settings", json={"ntfy_priority": 9}).status_code == 422
    assert admin.put("/api/settings", json={"ntfy_server": "ftp://x"}).status_code == 422
    assert admin.put("/api/settings", json={"ntfy_topic": "bad topic!"}).status_code == 422
    assert admin.put("/api/settings", json={"theme": "neon"}).status_code == 422


def test_settings_are_per_user(admin, user2):
    admin.put("/api/settings", json={"ntfy_topic": "admins-topic"})
    assert user2.get("/api/settings").json()["ntfy_topic"] is None


def test_test_notification(admin, respx_mock):
    r = admin.post("/api/settings/test-notification")
    assert r.json() == {"ok": False, "error": "Set an ntfy topic first"}

    admin.put("/api/settings", json={"ntfy_topic": "t1", "ntfy_token": "tk", "ntfy_server": "https://ntfy.test/"})
    route = respx_mock.post("https://ntfy.test/").respond(200, json={"id": "x"})
    r = admin.post("/api/settings/test-notification")
    assert r.json() == {"ok": True, "error": None}
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tk"
    import json

    payload = json.loads(req.content)
    assert payload["topic"] == "t1" and payload["title"]

    route.respond(403, text="forbidden")
    r = admin.post("/api/settings/test-notification").json()
    assert r["ok"] is False and "403" in r["error"]
