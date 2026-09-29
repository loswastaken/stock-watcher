"""Shared types and helpers for per-retailer adapters (see registry.py)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from ..base import Availability, CheckResult
from ..fetcher import FetchError, host_of, http_get
from ..util import format_price, parse_amount
from .registry import Retailer

STOCK_KEY = "stock"
_FULFILLMENT = {"delivery", "pickup", "any"}


@dataclass
class RetailerConfig:
    fulfillment: str = "delivery"  # delivery | pickup | any
    zip: str | None = None
    radius_miles: int = 25
    store_id: str | None = None
    official_only: bool = True
    condition: str = "new"  # new | any

    @classmethod
    def from_dict(cls, d: dict | None) -> "RetailerConfig":
        d = d or {}
        f = str(d.get("fulfillment") or "delivery").lower()

        def s(k: str) -> str | None:
            v = d.get(k)
            v = str(v).strip() if v is not None else ""
            return v or None

        try:
            radius = int(d.get("radius_miles") or 25)
        except (TypeError, ValueError):
            radius = 25
        return cls(
            fulfillment=f if f in _FULFILLMENT else "delivery",
            zip=s("zip"),
            radius_miles=max(1, min(radius, 250)),
            store_id=s("store_id"),
            official_only=d.get("official_only") is not False,
            condition="any" if str(d.get("condition") or "").lower() == "any" else "new",
        )

    @property
    def wants_delivery(self) -> bool:
        return self.fulfillment in ("delivery", "any")

    @property
    def wants_pickup(self) -> bool:
        return self.fulfillment in ("pickup", "any")


@dataclass
class AdapterContext:
    retailer: Retailer
    generic_config: dict = field(default_factory=dict)
    retailer_config: RetailerConfig = field(default_factory=RetailerConfig)


def result(
    verdict: str | None,
    status_text: str | None = None,
    *,
    available: list[Availability] | None = None,
    price: Any = None,
    currency: str | None = "USD",
    title: str | None = None,
    image_url: str | None = None,
    detail: dict | None = None,
    **extra_detail: Any,
) -> CheckResult:
    """Build a CheckResult from a verdict ('in' | 'out' | None). ``price`` may be a number
    or a string; the formatted price goes in ``price`` and the number in detail.price_value."""
    detail = dict(detail or {})
    detail.update({k: v for k, v in extra_detail.items() if v is not None})
    amount = parse_amount(price) if price is not None else None
    price_str = format_price(amount, currency) if amount is not None else (price if isinstance(price, str) else None)
    if amount is not None:
        detail.setdefault("price_value", float(amount))
    if verdict == "in":
        st = status_text or "In stock"
        avail = available if available is not None else [Availability(key=STOCK_KEY, label=st)]
        status = "in_stock" if avail else "out_of_stock"
    elif verdict == "out":
        st, avail, status = status_text or "Out of stock", [], "out_of_stock"
    else:
        st, avail, status = status_text or "Unknown", [], "unknown"
    return CheckResult(status=status, status_text=st, available=avail, price=price_str, title=title,
                       image_url=image_url, detail=detail)


async def get_json(url: str, *, headers: dict[str, str | None] | None = None) -> Any:
    """GET a JSON endpoint with XHR-style headers (the navigation-only ``Upgrade-Insecure-Requests``
    and ``Sec-Fetch-User`` defaults are removed). Raises FetchError on HTTP errors / bad JSON."""
    h: dict[str, str | None] = {
        "Accept": "application/json, text/plain, */*", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site", "Upgrade-Insecure-Requests": None, "Sec-Fetch-User": None,
    }
    h.update(headers or {})
    resp = await http_get(url, headers=h)
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} from {host_of(url)}", status=resp.status_code)
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise FetchError(f"Unexpected non-JSON response from {host_of(url)}", status=resp.status_code) from e


_NEXT_DATA_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)


def next_data(html: str) -> dict | None:
    """Parse Next.js ``__NEXT_DATA__`` from a page, or None."""
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def dig(obj: Any, *path: Any, default: Any = None) -> Any:
    """Safe nested lookup: dig(d, "a", 0, "b")."""
    cur = obj
    for p in path:
        if isinstance(p, int) and isinstance(cur, list) and -len(cur) <= p < len(cur):
            cur = cur[p]
        elif isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")
