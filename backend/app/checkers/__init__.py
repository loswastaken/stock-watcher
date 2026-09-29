"""Checker entry points. Implemented by the checkers module; signatures are a contract (PLAN.md)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from . import apple as _apple
from . import fetcher as _fetcher
from . import generic as _generic
from . import retailers as _retailers
from .base import Availability, CheckResult

log = logging.getLogger("stockwatcher.checkers")

# Internal caps (the scheduler/routers add their own outer timeouts too: 120 s per check,
# 65 s per preview request, so these must stay below those).
#
# Worst cases: a plain request (httpx or curl_cffi) is <= 15 s; the browser fallback is
# <= ~45 s (30 s navigation + 8 s network idle + challenge settling); fetch_html makes at
# most one plain request and one browser attempt (~60 s); site adapters add a few API calls
# (<= 15 s each) before/after the page fetch. Inside a check or preview all fetches share a
# ``fetcher.deadline`` a few seconds short of the cap, so the browser gets whatever is left
# (and returns the page it has) instead of being cancelled mid-way.
CHECK_TIMEOUT = 110.0
PREVIEW_TIMEOUT = 60.0
_DEADLINE_MARGIN = 3.0  # seconds kept for analysis after the last fetch


def _error(e: BaseException | str) -> CheckResult:
    msg = e if isinstance(e, str) else (str(e) or type(e).__name__)
    return CheckResult(status="error", status_text="Check failed", available=[], error=msg[:500])


_HTTP_ERROR_RE = re.compile(r"^HTTP (\d{3}) from (\S+)")


def _fetch_error(e: _fetcher.FetchError, url: str) -> CheckResult:
    """A 404/410 from the product's own site means a dead link: say so, so users update it."""
    m = _HTTP_ERROR_RE.match(str(e))
    # only the page's own host counts (an API 404 on e.g. redsky.target.com is not a dead product link)
    if e.status in (404, 410) and m and m.group(2).lower().removeprefix("www.") == \
            _fetcher.host_of(url).removeprefix("www."):
        text = f"{_generic.NOT_FOUND_TEXT} (HTTP {e.status})"
        return CheckResult(status="error", status_text=text, available=[], error=f"{text} — update the link",
                           detail={"dead_link": True})
    return _error(str(e))


async def _with_deadline(coro, seconds: float):
    with _fetcher.deadline(seconds):
        return await coro


async def _check_site(url: str, generic_config: dict | None, retailer_config: dict | None) -> CheckResult:
    result = await _retailers.run_adapter(url, generic_config, retailer_config)
    if result is None:
        result = await _generic.check_generic(url, generic_config)
    retailer = _retailers.match_retailer(url)
    if retailer is not None:
        result.detail.setdefault("retailer", retailer.key)
    return result


async def run_check(kind: str, url: str, generic_config: dict | None, apple_config: dict | None,
                    retailer_config: dict | None = None) -> CheckResult:
    """Run one availability check. Never raises (except cancellation)."""
    try:
        if (kind or "").lower() == "apple":
            coro = _apple.check_apple(url, apple_config)
        else:
            coro = _check_site(url, generic_config, retailer_config)
        result = await asyncio.wait_for(_with_deadline(coro, CHECK_TIMEOUT - _DEADLINE_MARGIN), CHECK_TIMEOUT)
        if result.status == "error" and not result.error:
            result.error = result.status_text or "Check failed"
        return result
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return _error("Check timed out")
    except _fetcher.FetchError as e:
        return _fetch_error(e, url)
    except Exception as e:  # noqa: BLE001 - contract: checkers never raise for site problems
        log.exception("checker crashed for %s", url)
        return _error(f"{type(e).__name__}: {e}")


def _retailer_info(url: str) -> dict | None:
    try:
        r = _retailers.match_retailer(url or "")
    except Exception:  # noqa: BLE001
        return None
    return r.to_dict() if r is not None else None


def _preview_base(url: str) -> dict:
    return {"name": None, "image_url": None, "price": None, "status": "unknown",
            "is_apple": _apple.is_apple_url(url or ""), "retailer": _retailer_info(url)}


async def _preview(url: str, out: dict) -> dict:
    """Fills ``out`` in place, so a timed-out preview still has what was found so far."""
    is_apple = out["is_apple"]
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
        out.update(name=res.title, image_url=res.image_url, price=out["price"] or res.price)
        return out
    # Same path as a check: site adapter first, then platform recipes / generic analysis.
    try:
        res = await _retailers.run_adapter(url, None, None)
        if res is None:
            res = await _generic.check_generic(url, None)
    except _fetcher.FetchError as e:
        out["status"] = "error"
        out["error"] = str(e)
        return out
    out.update(name=res.title, image_url=res.image_url, price=res.price, status=res.status)
    if res.status == "error":
        out["error"] = res.error or res.status_text
    return out


def _fill_from_recorded_page(out: dict, url: str, entries: list[dict]) -> None:
    """Name / image / price for a timed-out preview from a product page fetched so far."""
    if out.get("name") and out.get("image_url"):
        return
    host = _fetcher.host_of(url)
    for e in reversed(entries):
        body, status = e.get("body") or "", e.get("status")
        ctype = (e.get("headers") or {}).get("content-type", "")
        final = e.get("final_url") or e.get("url") or url
        if not body or not isinstance(status, int) or not 200 <= status < 300:
            continue
        if "html" not in ctype and body.lstrip()[:1] != "<":
            continue
        if _fetcher.host_of(final) != host or _fetcher.looks_like_challenge(body, final) \
                or _fetcher.looks_like_queue(body, final):
            continue
        try:
            res = _generic.analyze(body, url, None, base_url=final)
        except Exception:  # noqa: BLE001 - best effort
            continue
        if not (res.title or res.image_url):
            continue
        out["name"] = out.get("name") or res.title
        out["image_url"] = out.get("image_url") or res.image_url
        out["price"] = out.get("price") or res.price
        return


async def preview_url(url: str) -> dict:
    """→ {name, image_url, price, status, is_apple, retailer} (plus ``error`` on failure).
    ``retailer`` is the registry entry's ``to_dict()`` or None. Never raises. On timeout the
    result keeps whatever was found so far (name / image / price) with ``status: "error"``."""
    try:
        out = _preview_base(url)
    except Exception as e:  # noqa: BLE001
        return {"name": None, "image_url": None, "price": None, "status": "error", "is_apple": False,
                "retailer": None, "error": str(e) or type(e).__name__}
    with _fetcher.recording(reuse=True) as entries:
        try:
            return await asyncio.wait_for(_with_deadline(_preview(url, out), PREVIEW_TIMEOUT - _DEADLINE_MARGIN),
                                          PREVIEW_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.info("preview timed out for %s", url)
            with contextlib.suppress(Exception):
                _fill_from_recorded_page(out, url, entries)
            out.update(status="error", error=f"Preview timed out after {PREVIEW_TIMEOUT:g} s")
            return out
        except Exception as e:  # noqa: BLE001
            log.info("preview failed for %s: %s", url, e)
            out.update(status="error", error=str(e) or type(e).__name__)
            return out


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
