"""Fixtures for checker tests: no politeness delays, browser off unless a test opts in,
clean module state between tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.checkers import apple, fetcher

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str):
    return json.loads(load(name))


@pytest.fixture(autouse=True)
async def _checker_state(monkeypatch):
    monkeypatch.setattr(fetcher, "HOST_MIN_GAP", 0.0)
    monkeypatch.setenv("ENABLE_BROWSER", "false")
    # respx mocks httpx only: keep impersonated hosts (curl_cffi) on httpx unless a test opts in.
    monkeypatch.setenv("STOCKWATCHER_IMPERSONATE", "0")
    fetcher._browser_hosts.clear()
    apple._cache.clear()
    yield
    await fetcher.shutdown()
    apple._cache.clear()
