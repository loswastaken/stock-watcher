from __future__ import annotations

import httpx
import pytest

from app import updates
from tests.conftest import make_client

REG = "https://ghcr.io"
REPO = "loswastaken/stock-watcher"


def mock_registry(respx_mock, revision="abc123def456"):
    respx_mock.get(f"{REG}/token").respond(200, json={"token": "t"})
    respx_mock.get(f"{REG}/v2/{REPO}/manifests/latest").respond(200, json={"manifests": [
        {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
        {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
    ]})
    respx_mock.get(f"{REG}/v2/{REPO}/manifests/sha256:amd").respond(200, json={"config": {"digest": "sha256:cfg"}})
    respx_mock.get(f"{REG}/v2/{REPO}/blobs/sha256:cfg").respond(200, json={
        "created": "2026-09-29T16:00:00Z", "config": {"Labels": {updates.REVISION_LABEL: revision}}})


@pytest.fixture(autouse=True)
def _reset_cache():
    updates._cache.update(at=0.0, latest=None, error=None)
    yield
    updates._cache.update(at=0.0, latest=None, error=None)


def test_status_reports_update_available(admin, respx_mock, monkeypatch):
    from app import config

    monkeypatch.setenv("APP_VERSION", "0000old")
    config.reload_settings()
    mock_registry(respx_mock)
    r = admin.get("/api/system/update").json()
    assert r["latest_version"] == "abc123def456" and r["current_version"] == "0000old"
    assert r["update_available"] is True and r["can_update"] is False and r["error"] is None


def test_status_up_to_date_and_errors(admin, respx_mock, monkeypatch):
    from app import config

    monkeypatch.setenv("APP_VERSION", "abc123def456")
    config.reload_settings()
    mock_registry(respx_mock)
    assert admin.post("/api/system/update/check").json()["update_available"] is False

    respx_mock.get(f"{REG}/token").respond(401)
    r = admin.post("/api/system/update/check").json()
    assert "public" in r["error"]


def test_apply_calls_watchtower_for_this_image_only(admin, respx_mock, monkeypatch):
    from app import config

    assert admin.post("/api/system/update/apply").status_code == 400  # not configured
    monkeypatch.setenv("WATCHTOWER_URL", "http://nas.test:8080/")
    monkeypatch.setenv("WATCHTOWER_TOKEN", "secret")
    config.reload_settings()
    route = respx_mock.get("http://nas.test:8080/v1/update").respond(200)
    assert admin.post("/api/system/update/apply").status_code == 202
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer secret"
    assert req.url.params["image"] == "ghcr.io/loswastaken/stock-watcher"

    # Watchtower restarting us mid-request counts as started
    respx_mock.get("http://nas.test:8080/v1/update").mock(side_effect=httpx.ReadTimeout("x"))
    assert admin.post("/api/system/update/apply").status_code == 202
    respx_mock.get("http://nas.test:8080/v1/update").respond(401)
    r = admin.post("/api/system/update/apply")
    assert r.status_code == 400 and "token" in r.json()["detail"]
    respx_mock.get("http://nas.test:8080/v1/update").mock(side_effect=httpx.ConnectError("refused"))
    assert "Could not reach Watchtower" in admin.post("/api/system/update/apply").json()["detail"]


def test_admin_only(admin):
    c = make_client()
    assert c.get("/api/system/update").status_code == 401
    admin.post("/api/users", json={"username": "bob", "password": "bobpassword1", "is_admin": False})
    assert c.post("/api/auth/login", json={"username": "bob", "password": "bobpassword1"}).status_code == 200
    assert c.get("/api/system/update").status_code == 403
    assert c.post("/api/system/update/apply").status_code == 403
