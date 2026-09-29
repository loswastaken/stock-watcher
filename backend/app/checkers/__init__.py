"""Checker entry points. Implemented by the checkers module; signatures are a contract (PLAN.md)."""
from __future__ import annotations

from .base import Availability, CheckResult


async def run_check(kind: str, url: str, generic_config: dict | None, apple_config: dict | None) -> CheckResult:
    raise NotImplementedError


async def preview_url(url: str) -> dict:
    raise NotImplementedError


async def resolve_apple(url: str) -> dict:
    raise NotImplementedError


async def shutdown() -> None:
    return None


__all__ = ["Availability", "CheckResult", "run_check", "preview_url", "resolve_apple", "shutdown"]
