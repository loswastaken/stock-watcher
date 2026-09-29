"""Microsoft Store + Xbox hardware/software: displaycatalog (product, SKUs, price, purchase
actions) plus the inventory service for physical goods. Anything unexpected -> None (generic)."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

from ..base import CheckResult
from ..fetcher import FetchError
from ..util import clean_text
from . import base as rbase
from .base import AdapterContext, dig, result
from .pagekit import finish

CATALOG = "https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds={big}&market={market}&languages={lang}"
INVENTORY = "https://inv.mp.microsoft.com/v2.0/inventory/{market}/{big}/{sku}/{avail}"
_HEADERS = {"Origin": "https://www.microsoft.com", "Referer": "https://www.microsoft.com/", "Sec-Fetch-Site": "cross-site"}

_BIG_RE = re.compile(r"^(?=[0-9A-Z]*\d)(?=[0-9A-Z]*[A-Z])[0-9A-Z]{12}$")
_SKU_RE = re.compile(r"^[0-9A-Z]{4}$")
_LOCALE_RE = re.compile(r"^([a-z]{2})-([a-z]{2})$", re.I)
_DIGITAL_KINDS = {"game", "application", "durable", "consumable", "unmanagedconsumable", "pass", "avatar", "movie",
                  "tvseries", "bundle"}


def parse_ids(url: str) -> tuple[str | None, str | None]:
    """-> (bigId, skuId) from /d/<slug>/<bigId>[/<sku>], /configure/<bigId>, /productId/<bigId>..."""
    segs = [s for s in urlsplit(url).path.split("/") if s]
    for i in range(len(segs) - 1, -1, -1):
        seg = segs[i].upper()
        if _BIG_RE.match(seg):
            nxt = segs[i + 1].upper() if i + 1 < len(segs) else ""
            return seg, (nxt if _SKU_RE.match(nxt) else None)
    return None, None


def market_of(url: str) -> tuple[str, str]:
    for seg in urlsplit(url).path.split("/"):
        m = _LOCALE_RE.match(seg)
        if m:
            return m.group(2).upper(), f"{m.group(1).lower()}-{m.group(2).upper()}"
    return "US", "en-US"


def _purchasable(avail: dict) -> bool:
    return "Purchase" in (avail.get("Actions") or [])


def _pick(dsas: list[dict], sku_id: str | None) -> tuple[dict | None, dict | None]:
    def sku_of(d: dict) -> str:
        return str(dig(d, "Sku", "SkuId") or "").upper()

    ordered = ([d for d in dsas if sku_of(d) == sku_id] if sku_id else []) or dsas
    for d in ordered:
        for a in d.get("Availabilities") or []:
            if isinstance(a, dict) and _purchasable(a):
                return d, a
    d = ordered[0] if ordered else None
    avs = [a for a in (d or {}).get("Availabilities") or [] if isinstance(a, dict)]
    return d, (avs[0] if avs else None)


def _image(prod: dict) -> str | None:
    imgs = [i for i in dig(prod, "LocalizedProperties", 0, "Images") or [] if isinstance(i, dict)]
    for purpose in ("Poster", "BoxArt", "Tile", "Hero", "Screenshot"):
        for i in imgs:
            if i.get("ImagePurpose") == purpose and i.get("Uri"):
                u = str(i["Uri"])
                return "https:" + u if u.startswith("//") else u
    return None


def _lots_in_stock(inv: Any) -> bool | None:
    lots = dig(inv, "availableLots")
    if not isinstance(lots, dict) or not lots:
        return None
    seen = False
    for per_lot in lots.values():
        if not isinstance(per_lot, dict):
            continue
        for entry in per_lot.values():
            if isinstance(entry, dict) and "inStock" in entry:
                seen = True
                if str(entry.get("inStock")).strip().lower() == "true":
                    return True
    return False if seen else None


async def check(url: str, ctx: AdapterContext) -> CheckResult | None:
    big, sku_id = parse_ids(url)
    if not big:
        return None
    market, lang = market_of(url)
    try:
        data = await rbase.get_json(CATALOG.format(big=big, market=market, lang=lang), headers=_HEADERS)
    except FetchError:
        return None
    prod = dig(data, "Products", 0)
    if not isinstance(prod, dict):
        return None
    title = clean_text(dig(prod, "LocalizedProperties", 0, "ProductTitle")) or None
    dsas = [d for d in prod.get("DisplaySkuAvailabilities") or [] if isinstance(d, dict)]
    dsa, avail = _pick(dsas, sku_id)
    if dsa is None or avail is None:
        return None
    sku = str(dig(dsa, "Sku", "SkuId") or "")
    price = dig(avail, "OrderManagementData", "Price", "ListPrice")
    currency = dig(avail, "OrderManagementData", "Price", "CurrencyCode") or "USD"
    sku_title = clean_text(dig(dsa, "Sku", "LocalizedProperties", 0, "SkuTitle")) or None
    kind = str(prod.get("ProductKind") or "")
    preorder = bool(dig(dsa, "Sku", "Properties", "IsPreOrder"))
    detail: dict[str, Any] = {"big_id": big, "sku_id": sku, "availability_id": avail.get("AvailabilityId"),
                              "product_kind": kind or None, "sku_title": sku_title, "seller": "Microsoft",
                              "third_party": False, "market": market}

    def done(verdict: str | None, text: str, matched: str) -> CheckResult:
        detail["matched"] = matched
        res = result(verdict, text, price=price, currency=currency, title=title, image_url=_image(prod),
                     detail=detail)
        return finish(res, ctx)

    if not _purchasable(avail):
        return done("out", "Not available to buy", f"displaycatalog Actions={avail.get('Actions')}")
    in_text = "Pre-order" if preorder else "In stock"
    if kind.lower() in _DIGITAL_KINDS:
        return done("in", "Pre-order" if preorder else "Available to buy", f"displaycatalog Purchase ({kind})")

    try:
        inv = await rbase.get_json(
            INVENTORY.format(market=market, big=big, sku=quote(sku), avail=quote(str(avail.get("AvailabilityId") or ""))),
            headers=_HEADERS,
        )
    except FetchError as e:
        return done(None, "Unknown", f"inventory lookup failed: {e}")
    stocked = _lots_in_stock(inv)
    if stocked is True:
        return done("in", in_text, "inventory availableLots inStock=True")
    if stocked is False:
        return done("out", "Out of stock", "inventory availableLots inStock=False")
    return done(None, "Unknown", "inventory: no lots reported")
