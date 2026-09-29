"""Checker entry points. Implemented by the checkers module; signatures are a contract (PLAN.md)."""
from __future__ import annotations

import asyncio
import logging

from . import apple as _apple
from . import fetcher as _fetcher
from . import generic as _generic
from .base import Availability, CheckResult

log = logging.getLogger("stockwatcher.checkers")

# Internal caps (the scheduler/routers add their own outer timeouts too).
CHECK_TIMEOUT = 100.0
PREVIEW_TIMEOUT = 40.0


def _error(e: BaseException | str) -> CheckResult:
    msg = e if isinstance(e, str) else (str(e) or type(e).__name__)
    return CheckResult(status="error", status_text="Check failed", available=[], error=msg[:500])


async def run_check(kind: str, url: str, generic_config: dict | None, apple_config: dict | None) -> CheckResult:
    """Run one availability check. Never raises (except cancellation)."""
    try:
        if (kind or "").lower() == "apple":
            coro = _apple.check_apple(url, apple_config)
        else:
            coro = _generic.check_generic(url, generic_config)
        result = await asyncio.wait_for(coro, CHECK_TIMEOUT)
        if result.status == "error" and not result.error:
            result.error = result.status_text or "Check failed"
        return result
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return _error("Check timed out")
    except _fetcher.FetchError as e:
        return _error(str(e))
    except Exception as e:  # noqa: BLE001 - contract: checkers never raise for site problems
        log.exception("checker crashed for %s", url)
        return _error(f"{type(e).__name__}: {e}")


async def _preview(url: str) -> dict:
    is_apple = _apple.is_apple_url(url)
    out = {"name": None, "image_url": None, "price": None, "status": "unknown", "is_apple": is_apple}
    if is_apple:
        info = await _apple.resolve_apple(url)
        out["name"] = info.get("product_name")
        out["image_url"] = info.get("image_url")
        variants = info.get("variants") or []
        url_part = _apple.part_from_url(url)
        chosen = next((v for v in variants if v.get("part_number") == url_part), None) if url_part else None
        if chosen is None and len(variants) == 1:
            chosen = variants[0]
        if chosen is None:
            prices = {v.get("price") for v in variants if v.get("price")}
            out["price"] = prices.pop() if len(prices) == 1 else None
        else:
            out["price"] = chosen.get("price")
        if out["name"] or out["image_url"]:
            return out
    try:
        fetched = await _fetcher.fetch_html(url)
    except _fetcher.FetchError as e:
        out["status"] = "error"
        out["error"] = str(e)
        return out
    res = _generic.analyze(fetched.text, url, None, base_url=fetched.url)
    out.update(
        name=out["name"] or res.title,
        image_url=out["image_url"] or res.image_url,
        price=out["price"] or res.price,
        status="unknown" if is_apple else res.status,
    )
    return out


async def preview_url(url: str) -> dict:
    """→ {name, image_url, price, status, is_apple} (plus ``error`` on failure). Never raises."""
    try:
        return await asyncio.wait_for(_preview(url), PREVIEW_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.info("preview failed for %s: %s", url, e)
        return {"name": None, "image_url": None, "price": None, "status": "error",
                "is_apple": _apple.is_apple_url(url or ""), "error": str(e) or type(e).__name__}


async def resolve_apple(url: str) -> dict:
    """→ {product_name, image_url, variants:[{part_number,label,price}]}. Never raises."""
    try:
        return await asyncio.wait_for(_apple.resolve_apple(url), PREVIEW_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.info("resolve_apple failed for %s: %s", url, e)
        part = _apple.part_from_url(url or "")
        return {"product_name": None, "image_url": None,
                "variants": [{"part_number": part, "label": part, "price": None}] if part else []}


async def shutdown() -> None:
    await _fetcher.shutdown()


__all__ = ["Availability", "CheckResult", "run_check", "preview_url", "resolve_apple", "shutdown"]
