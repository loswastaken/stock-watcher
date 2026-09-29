"""Per-retailer adapters. ``run_adapter`` returns None when the generic checker should handle the URL."""
from __future__ import annotations

import importlib
import logging
from typing import Awaitable, Callable

from ..base import CheckResult
from ..fetcher import FetchError
from .base import AdapterContext, RetailerConfig
from .registry import RETAILERS, Retailer, match_retailer, retailer_by_key

log = logging.getLogger("stockwatcher.checkers.retailers")

Adapter = Callable[[str, AdapterContext], Awaitable["CheckResult | None"]]
_cache: dict[str, Adapter | None] = {}


def load_adapter(spec: str | None) -> Adapter | None:
    """Resolve ``"module:function"`` (relative to this package). Missing modules → None."""
    if not spec:
        return None
    if spec not in _cache:
        mod_name, _, fn_name = spec.partition(":")
        try:
            mod = importlib.import_module(f"{__name__}.{mod_name}")
            _cache[spec] = getattr(mod, fn_name or "check")
        except (ImportError, AttributeError):
            log.warning("retailer adapter %s is not available", spec)
            _cache[spec] = None
    return _cache[spec]


async def run_adapter(url: str, generic_config: dict | None, retailer_config: dict | None) -> CheckResult | None:
    """Run the site adapter for ``url`` if there is one. Custom per-item rules (selector/text mode)
    always win, so those return None. FetchError propagates; other adapter bugs fall back to generic."""
    gc = generic_config or {}
    if str(gc.get("mode") or "auto").lower() != "auto":
        return None
    retailer = match_retailer(url)
    if retailer is None:
        return None
    fn = load_adapter(retailer.adapter)
    if fn is None:
        return None
    ctx = AdapterContext(retailer=retailer, generic_config=gc, retailer_config=RetailerConfig.from_dict(retailer_config))
    try:
        res = await fn(url, ctx)
    except FetchError:
        raise
    except Exception:  # noqa: BLE001 - a broken adapter must not take the item down
        log.exception("%s adapter crashed for %s; falling back to generic", retailer.key, url)
        return None
    if res is not None:
        res.detail.setdefault("adapter", retailer.adapter.partition(":")[0] if retailer.adapter else "generic")
    return res


__all__ = ["RETAILERS", "Retailer", "AdapterContext", "RetailerConfig", "match_retailer", "retailer_by_key",
           "run_adapter", "load_adapter"]
