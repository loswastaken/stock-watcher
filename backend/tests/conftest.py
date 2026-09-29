"""Shared fixtures for backend-core tests.

Only `_isolated_env` is autouse (pure env isolation); everything else is opt-in.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db, scheduler, security
from app.checkers import Availability, CheckResult

PW = "correct horse battery"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path / "no-static"))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    config.reload_settings()
    security.login_limiter.clear()
    yield
    config.reload_settings()


@pytest.fixture
def db_ready():
    """Initialised engine + schema, without the HTTP app."""
    s = config.prepare_data_dir(config.get_settings())
    db.init_engine()
    db.init_db()
    yield s
    db.dispose_engine()


@pytest.fixture
def client(monkeypatch):
    """TestClient with lifespan running (DB init, no scheduler loop)."""
    from app.main import app

    # never let background first-checks hit a real checker
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("app.routers.items.scheduler.spawn", lambda coro: coro.close())
    with TestClient(app) as c:
        yield c


def make_client() -> TestClient:
    """Extra client (own cookie jar) against the already-initialised app."""
    from app.main import app

    return TestClient(app)


@pytest.fixture
def admin(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def user2(admin):
    """A second, regular user with its own logged-in client."""
    r = admin.post("/api/users", json={"username": "bob", "password": PW, "is_admin": False})
    assert r.status_code == 201, r.text
    c = make_client()
    r = c.post("/api/auth/login", json={"username": "bob", "password": PW})
    assert r.status_code == 200, r.text
    return c


def fake_result(status="in_stock", keys=("stock",), **kw) -> CheckResult:
    return CheckResult(
        status=status,
        status_text=kw.pop("status_text", "In stock" if status == "in_stock" else status),
        available=[Availability(key=k, label=f"label-{k}") for k in keys],
        **kw,
    )
