"""Items CRUD, manual check, images, preview."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import images, scheduler
from ..checkers import preview_url
from ..checkers.retailers.registry import match_retailer, retailer_by_key
from ..db import SessionLocal, get_db
from ..models import DEFAULT_GENERIC_CONFIG, CheckEvent, Item, User, UserSettings, utcnow
from ..schemas import (
    AppleConfig,
    CheckEventOut,
    GenericConfig,
    ItemCreate,
    ItemOut,
    ItemUpdate,
    PreviewOut,
    RestockOut,
    RetailerConfigIn,
    StoreCreate,
    StoreRow,
    UrlRequest,
    item_retailer,
)
from ..security import current_user
from .settings import get_or_create_settings

log = logging.getLogger("stockwatcher.items")
router = APIRouter(prefix="/items", tags=["items"])

PREVIEW_TIMEOUT = 65  # just above checkers.PREVIEW_TIMEOUT, which returns its own error


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_apple_host(url: str) -> bool:
    h = _host(url)
    return h == "apple.com" or h.endswith(".apple.com")


def _owned_item(db: Session, user: User, item_id: int) -> Item:
    item = db.scalar(select(Item).where(Item.id == item_id, Item.user_id == user.id))
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


def _check_interval(minutes: int) -> None:
    floor = scheduler.min_interval_minutes()
    if minutes < floor:
        raise HTTPException(status_code=422, detail=f"interval_minutes must be at least {floor}")


def _apple_config_dict(cfg: AppleConfig, defaults: UserSettings, explicit: set[str]) -> dict:
    data = cfg.model_dump()
    if "zip" not in explicit or not data.get("zip"):
        data["zip"] = defaults.default_zip or data.get("zip")
    if "max_distance_miles" not in explicit:
        data["max_distance_miles"] = defaults.default_max_distance_miles or 25
    for part in data["parts"]:
        if not part.get("label"):
            part["label"] = part["part_number"]
    return data


def _retailer_config_dict(cfg: RetailerConfigIn, defaults: UserSettings, url: str) -> dict:
    """Validate + fill pickup defaults (ZIP / radius from the user's settings)."""
    data = cfg.model_dump()
    explicit = cfg.model_fields_set
    wants_pickup = data["fulfillment"] in ("pickup", "any")
    if wants_pickup:
        retailer = match_retailer(url)
        if retailer is not None and not retailer.pickup:
            raise HTTPException(status_code=422, detail=f"{retailer.name} doesn't support in-store pickup tracking")
        if not data.get("zip") and not data.get("store_id") and defaults.default_zip:
            data["zip"] = defaults.default_zip
        if "radius_miles" not in explicit and defaults.default_max_distance_miles:
            data["radius_miles"] = max(1, min(int(defaults.default_max_distance_miles), 250))
        if data["fulfillment"] == "pickup" and not data.get("zip") and not data.get("store_id"):
            raise HTTPException(status_code=422, detail="Pickup needs a ZIP code (or store) — set one here or in Settings")
    return data


def _revalidate_retailer_config(cfg: dict | None, old_url: str, new_url: str,
                                defaults: UserSettings) -> dict | None:
    """Fit a stored retailer_config to an item's new URL (PATCH changed the URL only).

    Unknown store -> no config. Another store -> its store_id is meaningless there, and
    pickup falls back to delivery when the new store has no pickup (or no location is left).
    """
    if not cfg:
        return None
    new_r = match_retailer(new_url)
    if new_r is None:
        return None
    data = dict(cfg)
    old_r = match_retailer(old_url)
    if old_r is None or old_r.key != new_r.key:
        data.pop("store_id", None)
    if data.get("fulfillment") in ("pickup", "any") and not new_r.pickup:
        data["fulfillment"] = "delivery"
    try:
        parsed = RetailerConfigIn.model_validate(data)
    except ValueError:
        return None
    try:
        return _retailer_config_dict(parsed, defaults, new_url)
    except HTTPException:
        # pickup without a ZIP / store for the new store: track delivery instead
        return _retailer_config_dict(parsed.model_copy(update={"fulfillment": "delivery"}), defaults, new_url)


def _preview_retailer(url: str, value) -> dict | None:
    if isinstance(value, dict) and value.get("key"):
        r = retailer_by_key(str(value["key"]))
        return r.to_dict() if r else value
    if isinstance(value, str) and value:
        r = retailer_by_key(value)
        if r:
            return r.to_dict()
    r = match_retailer(url)
    return r.to_dict() if r else None


async def _safe_preview(url: str) -> dict:
    try:
        data = await asyncio.wait_for(preview_url(url), PREVIEW_TIMEOUT)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.info("preview failed for %s: %s", url, e)
        return {}


PLATFORM_NAMES = {
    "shopify": "Shopify",
    "sfcc": "Salesforce Commerce Cloud",
    "magento": "Magento",
    "bigcommerce": "BigCommerce",
    "woocommerce": "WooCommerce",
    "opencart": "OpenCart",
}
_BLOCKED_RE = re.compile(r"bot protection|blocked|captcha|access denied|refused the connection|HTTP 403|HTTP 429", re.I)
_NOT_FOUND_RE = re.compile(r"HTTP 40[4]|HTTP 410|not found|no longer available|dead link|update the link", re.I)
_BLOCKED_DETAIL = "Store blocked our checker — try again later or use a real-browser setup."
_CUSTOM_RULE_HINT = ("Add it anyway and switch the stock rule to CSS selector or Text match so it knows what "
                     "“in stock” looks like on this page.")


def classify_support(data: dict, retailer: dict | None) -> dict:
    """→ {level, label, detail}: how well the checker handles this URL, from a preview result.

    dedicated   a registry store whose own site adapter produced the result (or Apple)
    platform    a platform recipe (Shopify, SFCC, ...) recognised the store
    generic     generic heuristics — with a definite answer, or unknown (then suggest custom rules)
    blocked     bot protection stopped the checker
    unsupported the page couldn't be fetched / was not found / errored
    """
    status = str(data.get("status") or "unknown")
    error = str(data.get("error") or "")
    adapter = str(data.get("adapter") or "").strip().lower() or None
    definite = status in ("in_stock", "out_of_stock")
    store_name = (retailer or {}).get("name")

    if data.get("blocked") or (error and _BLOCKED_RE.search(error) and not definite):
        return {"level": "blocked", "label": "Blocked by bot protection", "detail": _BLOCKED_DETAIL}
    if status == "error" or (error and not definite and not data.get("name")):
        if _NOT_FOUND_RE.search(error):
            return {"level": "unsupported", "label": "Page not found",
                    "detail": error or "The store says this page doesn't exist — check the link."}
        return {"level": "unsupported", "label": "Couldn't check this page",
                "detail": error or "The checker couldn't read this page."}
    if data.get("is_apple"):
        return {"level": "dedicated", "label": "Dedicated support",
                "detail": "Apple Store integration — pick models and watch delivery or pickup near you."}
    if adapter in PLATFORM_NAMES:
        platform = PLATFORM_NAMES[adapter]
        detail = f"Checked with the built-in {platform} recipe, which works for any {platform} store."
        if not definite:
            detail += " It couldn't tell stock status from this page though. " + _CUSTOM_RULE_HINT
        return {"level": "platform", "label": f"Auto-detected {platform} store", "detail": detail}
    site_adapter = adapter not in (None, "generic")
    if retailer and (site_adapter or adapter is None):
        # adapter None: an older checker that doesn't report it — a registry store is dedicated.
        note = retailer.get("note")
        detail = f"{store_name} has a dedicated integration" + (f" — {note}." if note else ".")
        if not definite:
            detail += " Stock status wasn't clear on this check; it may resolve on the next one."
        return {"level": "dedicated", "label": "Dedicated support", "detail": detail}
    if site_adapter:  # a site adapter the registry doesn't list under this host
        return {"level": "dedicated", "label": "Dedicated support", "detail": "Checked with a dedicated integration."}
    if definite:
        return {"level": "generic", "label": "Works with generic detection",
                "detail": "Not a dedicated integration, but the page's stock signals were clear enough to read."}
    return {"level": "generic", "label": "Stock status unclear",
            "detail": "The page loaded but generic detection couldn't read stock status. " + _CUSTOM_RULE_HINT}


def _opt_str(v) -> str | None:
    return str(v) if v not in (None, "") else None


def _preview_out(url: str, data: dict) -> PreviewOut:
    retailer = _preview_retailer(url, data.get("retailer"))
    third = data.get("third_party")
    fields = dict(
        name=_opt_str(data.get("name")),
        image_url=_opt_str(data.get("image_url")),
        price=_opt_str(data.get("price")),
        status=data.get("status") or "unknown",
        is_apple=bool(data.get("is_apple", is_apple_host(url))),
        retailer=retailer,
        error=_opt_str(data.get("error")),
        status_text=_opt_str(data.get("status_text")),
        adapter=_opt_str(data.get("adapter")),
        fetched_via=_opt_str(data.get("fetched_via")),
        seller=_opt_str(data.get("seller")),
        third_party=bool(third) if third is not None else None,
        cart_url=_opt_str(data.get("cart_url")),
        signals=[str(x) for x in (data.get("signals") or []) if x][:12],
        blocked=bool(data.get("blocked")),
        queued=bool(data.get("queued")),
    )
    fields["support"] = classify_support(fields, retailer)
    return PreviewOut(**fields)


# ---------------------------------------------------------------------- routes
@router.post("/preview", response_model=PreviewOut)
async def preview(body: UrlRequest, user: User = Depends(current_user)):
    try:
        data = await asyncio.wait_for(preview_url(body.url), PREVIEW_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.info("preview failed for %s: %s", body.url, e)
        data = {"status": "unknown", "error": f"Could not fetch the page ({type(e).__name__})"}
    return _preview_out(body.url, data if isinstance(data, dict) else {})


@router.get("", response_model=list[ItemOut])
def list_items(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(Item).where(Item.user_id == user.id).order_by(Item.created_at.desc(), Item.id.desc())
    ).all()


async def _post_create(item_id: int, image_source: str | None) -> None:
    try:
        if image_source:
            await images.attach_image_from_url(item_id, image_source)
    except Exception:  # noqa: BLE001
        log.exception("image fetch after create failed for item %s", item_id)
    await scheduler.check_item(item_id, wait=False)


@router.post("", response_model=ItemOut, status_code=201)
async def create_item(body: ItemCreate, user: User = Depends(current_user)):
    with SessionLocal() as db:
        defaults = get_or_create_settings(db, user.id)
        db.expunge(defaults)

    kind = body.kind or ("apple" if body.apple_config and is_apple_host(body.url) else "generic")
    apple_cfg: dict | None = None
    if kind == "apple":
        if body.apple_config is None or not body.apple_config.parts:
            raise HTTPException(status_code=422, detail="apple_config with at least one part is required for Apple items")
        apple_cfg = _apple_config_dict(body.apple_config, defaults, body.apple_config.model_fields_set)
    generic_cfg = (body.generic_config or GenericConfig()).model_dump()
    retailer_cfg = (_retailer_config_dict(body.retailer_config, defaults, body.url)
                    if body.retailer_config is not None else None)

    interval = body.interval_minutes if body.interval_minutes is not None else defaults.default_interval_minutes
    interval = interval or 2
    _check_interval(interval)

    preview_data: dict = {}
    if not body.name:
        preview_data = await _safe_preview(body.url)
    name = (body.name or (preview_data.get("name") or "").strip() or _host(body.url) or "Item")[:200]
    image_source = body.image_url or preview_data.get("image_url")

    with SessionLocal() as db:
        item = Item(
            user_id=user.id,
            name=name,
            url=body.url,
            kind=kind,
            enabled=True,
            notify_enabled=body.notify_enabled,
            interval_minutes=interval,
            status="unknown",
            status_text="Waiting for first check",
            price=(preview_data.get("price") if isinstance(preview_data.get("price"), str) else None),
            consecutive_errors=0,
            available_keys=[],
            last_result={},
            generic_config=generic_cfg,
            apple_config=apple_cfg,
            retailer_config=retailer_cfg,
            max_price=body.max_price,
            product_group=(body.product_group or "").strip() or None,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        out = ItemOut.model_validate(item)
        item_id = item.id

    scheduler.spawn(_post_create(item_id, image_source))
    return out


@router.get("/{item_id}", response_model=ItemOut)
def get_item(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _owned_item(db, user, item_id)


@router.patch("/{item_id}", response_model=ItemOut)
def update_item(
    item_id: int, body: ItemUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    item = _owned_item(db, user, item_id)
    data = body.model_dump(exclude_unset=True)
    recheck = False

    if data.get("interval_minutes") is not None:
        _check_interval(data["interval_minutes"])
        if data["interval_minutes"] != item.interval_minutes:
            recheck = True
        item.interval_minutes = data["interval_minutes"]
    if data.get("name") is not None:
        item.name = data["name"]
    old_url = item.url
    url_changed = data.get("url") is not None and data["url"] != item.url
    if url_changed:
        item.url = data["url"]
        recheck = True
        if "retailer_config" not in data and item.retailer_config:
            item.retailer_config = _revalidate_retailer_config(
                item.retailer_config, old_url, item.url, get_or_create_settings(db, user.id))
    if data.get("enabled") is not None:
        item.enabled = data["enabled"]
    if data.get("notify_enabled") is not None:
        item.notify_enabled = data["notify_enabled"]
        item.muted_by_alert = False  # the user decided; auto re-arm leaves it alone
    if "generic_config" in data and data["generic_config"] is not None:
        item.generic_config = body.generic_config.model_dump()  # type: ignore[union-attr]
        recheck = True
    if "apple_config" in data and data["apple_config"] is not None:
        if item.kind != "apple":
            raise HTTPException(status_code=422, detail="apple_config is only valid for Apple items")
        cfg = body.apple_config  # type: ignore[assignment]
        if not cfg.parts:
            raise HTTPException(status_code=422, detail="apple_config needs at least one part")
        defaults = get_or_create_settings(db, user.id)
        item.apple_config = _apple_config_dict(cfg, defaults, cfg.model_fields_set)
        recheck = True
    if "retailer_config" in data:
        if body.retailer_config is None:
            item.retailer_config = None
        else:
            defaults = get_or_create_settings(db, user.id)
            item.retailer_config = _retailer_config_dict(body.retailer_config, defaults, item.url)
        recheck = True
    if "max_price" in data and data["max_price"] != item.max_price:
        item.max_price = data["max_price"]  # null clears the limit
        recheck = True
    if "product_group" in data:
        item.product_group = (data["product_group"] or "").strip() or None
    if recheck:
        item.last_checked_at = None  # scheduler picks it up on its next pass
    item.updated_at = utcnow()
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = _owned_item(db, user, item_id)
    image = item.image_path
    db.delete(item)
    db.commit()
    images.delete_image_file(image)
    return Response(status_code=204)


@router.post("/check-all", status_code=202)
async def check_all(user: User = Depends(current_user)):
    """Check every active item of this user now (in the background)."""
    with SessionLocal() as db:
        ids = list(db.scalars(select(Item.id).where(
            Item.user_id == user.id, Item.enabled.is_(True), Item.purchased_at.is_(None))))
    return {"queued": scheduler.queue_checks(ids), "total": len(ids)}


@router.post("/{item_id}/purchase", response_model=ItemOut)
def mark_purchased(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Move an item to the Purchased list: no more checks or alerts.

    The other stores tracking the same product (its product_group) are marked purchased
    too, with the same purchase time. Only the clicked item records a purchase price; the
    siblings keep showing their own current price.
    """
    item = _owned_item(db, user, item_id)
    if item.purchased_at is None:
        now = utcnow()
        item.purchased_at = now
        item.purchased_price = item.price
        for sib in _group_items(db, user, item):
            if sib.id != item.id and sib.purchased_at is None:
                sib.purchased_at = now
                sib.purchased_price = None
    db.commit()
    db.refresh(item)
    return ItemOut.model_validate(item)


@router.post("/{item_id}/unpurchase", response_model=ItemOut)
def unmark_purchased(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Back to the watch list: checking resumes right away with alerts armed.

    Only this item is restored; purchased siblings in its product group stay purchased.
    """
    item = _owned_item(db, user, item_id)
    item.purchased_at = None
    item.purchased_price = None
    item.enabled = True
    item.notify_enabled = True
    item.muted_by_alert = False
    item.available_keys = []  # the next in-stock result alerts again
    item.last_checked_at = None
    db.commit()
    db.refresh(item)
    return ItemOut.model_validate(item)


@router.post("/{item_id}/check", response_model=ItemOut)
async def check_now(item_id: int, user: User = Depends(current_user)):
    with SessionLocal() as db:
        _owned_item(db, user, item_id)
    await scheduler.check_item(item_id, wait=True)
    with SessionLocal() as db:
        item = _owned_item(db, user, item_id)
        return ItemOut.model_validate(item)


@router.get("/{item_id}/history", response_model=list[CheckEventOut])
def history(
    item_id: int,
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _owned_item(db, user, item_id)
    return db.scalars(
        select(CheckEvent)
        .where(CheckEvent.item_id == item_id)
        .order_by(CheckEvent.id.desc())
        .limit(limit)
    ).all()


@router.get("/{item_id}/restocks", response_model=list[RestockOut])
def restocks(
    item_id: int,
    limit: int = Query(20, ge=1, le=200),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """When the item came back in stock: checks where the status turned in_stock after being
    out of stock (errors / inconclusive checks in between don't count as a restock)."""
    _owned_item(db, user, item_id)
    rows = db.execute(
        select(CheckEvent.id, CheckEvent.checked_at, CheckEvent.status, CheckEvent.status_text)
        .where(CheckEvent.item_id == item_id, CheckEvent.status.in_(("in_stock", "out_of_stock")))
        .order_by(CheckEvent.id)
    ).all()
    out: list[RestockOut] = []
    prev: str | None = None
    for ev_id, checked_at, status, status_text in rows:
        if status == "in_stock" and prev == "out_of_stock":
            out.append(RestockOut(id=ev_id, checked_at=checked_at, status_text=status_text or ""))
        prev = status
    return list(reversed(out))[:limit]


# ------------------------------------------------------------ multi-store groups
def _group_items(db: Session, user: User, item: Item) -> list[Item]:
    if not item.product_group:
        return [item]
    return list(db.scalars(
        select(Item)
        .where(Item.user_id == user.id, Item.product_group == item.product_group)
        .order_by(Item.created_at, Item.id)
    ))


def _store_row(item: Item) -> StoreRow:
    return StoreRow(
        id=item.id, name=item.name, url=item.url, retailer=item_retailer(item), status=item.status,
        status_text=item.status_text or "", price=item.price, max_price=item.max_price,
        enabled=bool(item.enabled), notify_enabled=bool(item.notify_enabled),
        last_in_stock_at=item.last_in_stock_at, last_checked_at=item.last_checked_at,
    )


@router.get("/{item_id}/stores", response_model=list[StoreRow])
def list_stores(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Every store tracking the same product (the item's product_group), including this one."""
    item = _owned_item(db, user, item_id)
    return [_store_row(i) for i in _group_items(db, user, item)]


@router.post("/{item_id}/stores", response_model=ItemOut, status_code=201)
def add_store(item_id: int, body: StoreCreate, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    """Track the same product at another store: a sibling item in the same product_group."""
    item = _owned_item(db, user, item_id)
    if item.purchased_at is not None:
        raise HTTPException(status_code=409,
                            detail="This item is marked purchased — use Watch again before adding another store")
    if is_apple_host(body.url):
        raise HTTPException(status_code=422, detail="Add Apple Store products from the Add item page")
    siblings = _group_items(db, user, item)
    if any(s.url == body.url for s in siblings):
        raise HTTPException(status_code=409, detail="This store is already tracked for this product")
    if not item.product_group:
        item.product_group = uuid.uuid4().hex[:12]
    retailer_cfg = None
    if body.retailer_config is not None:
        retailer_cfg = _retailer_config_dict(body.retailer_config, get_or_create_settings(db, user.id), body.url)
    sibling = Item(
        user_id=user.id,
        name=item.name,
        url=body.url,
        kind="generic",
        enabled=True,
        notify_enabled=True,
        interval_minutes=item.interval_minutes,
        status="unknown",
        status_text="Waiting for first check",
        consecutive_errors=0,
        available_keys=[],
        last_result={},
        generic_config=dict(DEFAULT_GENERIC_CONFIG),
        retailer_config=retailer_cfg,
        max_price=item.max_price,
        product_group=item.product_group,
        image_path=images.copy_image_file(item.image_path),
    )
    db.add(sibling)
    db.commit()
    db.refresh(sibling)
    out = ItemOut.model_validate(sibling)
    scheduler.spawn(_post_create(sibling.id, None))
    return out


@router.post("/{item_id}/image", response_model=ItemOut)
async def upload_image(
    item_id: int, file: UploadFile = File(...), user: User = Depends(current_user)
):
    with SessionLocal() as db:
        _owned_item(db, user, item_id)
    data = await file.read(images.MAX_BYTES + 1)
    if len(data) > images.MAX_BYTES:
        raise HTTPException(status_code=413, detail="Image is larger than 8 MB")
    try:
        filename = await asyncio.to_thread(images.store_image_bytes, data)
    except images.ImageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not await asyncio.to_thread(images.set_item_image, item_id, filename):
        raise HTTPException(status_code=404, detail="Item not found")
    with SessionLocal() as db:
        return ItemOut.model_validate(_owned_item(db, user, item_id))


@router.post("/{item_id}/image/refresh", response_model=ItemOut)
async def refresh_image(item_id: int, user: User = Depends(current_user)):
    with SessionLocal() as db:
        url = _owned_item(db, user, item_id).url
    data = await _safe_preview(url)
    src = data.get("image_url")
    if not src:
        raise HTTPException(status_code=422, detail="No image found on the page")
    if not await images.attach_image_from_url(item_id, src):
        raise HTTPException(status_code=422, detail="Could not download the image from the page")
    with SessionLocal() as db:
        return ItemOut.model_validate(_owned_item(db, user, item_id))
