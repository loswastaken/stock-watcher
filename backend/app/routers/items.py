"""Items CRUD, manual check, images, preview."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import images, scheduler
from ..checkers import preview_url
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
    UrlRequest,
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


async def _safe_preview(url: str) -> dict:
    try:
        data = await asyncio.wait_for(preview_url(url), PREVIEW_TIMEOUT)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.info("preview failed for %s: %s", url, e)
        return {}


# ---------------------------------------------------------------------- routes
@router.post("/preview", response_model=PreviewOut)
async def preview(body: UrlRequest, user: User = Depends(current_user)):
    try:
        data = await asyncio.wait_for(preview_url(body.url), PREVIEW_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.info("preview failed for %s: %s", body.url, e)
        return PreviewOut(
            status="unknown",
            is_apple=is_apple_host(body.url),
            error=f"Could not fetch the page ({type(e).__name__})",
        )
    data = data if isinstance(data, dict) else {}
    return PreviewOut(
        name=data.get("name"),
        image_url=data.get("image_url"),
        price=data.get("price"),
        status=data.get("status") or "unknown",
        is_apple=bool(data.get("is_apple", is_apple_host(body.url))),
        error=data.get("error"),
    )


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
    if data.get("url") is not None and data["url"] != item.url:
        item.url = data["url"]
        recheck = True
    if data.get("enabled") is not None:
        item.enabled = data["enabled"]
    if data.get("notify_enabled") is not None:
        item.notify_enabled = data["notify_enabled"]
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
        ids = list(db.scalars(select(Item.id).where(Item.user_id == user.id, Item.enabled.is_(True))))
    return {"queued": scheduler.queue_checks(ids), "total": len(ids)}


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
