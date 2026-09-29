"""Per-user settings + ntfy test notification."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, UserSettings
from ..notifier import send_ntfy
from ..scheduler import min_interval_minutes
from ..schemas import SettingsOut, SettingsUpdate, TestNotificationResult
from ..security import current_user

router = APIRouter(prefix="/settings", tags=["settings"])


def get_or_create_settings(db: Session, user_id: int) -> UserSettings:
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None:
        s = UserSettings(user_id=user_id)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def settings_out(s: UserSettings) -> SettingsOut:
    return SettingsOut(
        ntfy_server=s.ntfy_server or "https://ntfy.sh",
        ntfy_topic=s.ntfy_topic or None,
        ntfy_token_set=bool(s.ntfy_token),
        ntfy_priority=s.ntfy_priority or 4,
        default_interval_minutes=s.default_interval_minutes or 2,
        default_zip=s.default_zip or None,
        default_max_distance_miles=s.default_max_distance_miles or 25,
        notify_on_out_of_stock=bool(s.notify_on_out_of_stock),
        theme=s.theme or "dark",
    )


@router.get("", response_model=SettingsOut)
def get_settings_endpoint(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return settings_out(get_or_create_settings(db, user.id))


@router.put("", response_model=SettingsOut)
def update_settings(
    body: SettingsUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    s = get_or_create_settings(db, user.id)
    data = body.model_dump(exclude_unset=True)
    floor = min_interval_minutes()
    if data.get("default_interval_minutes") is not None and data["default_interval_minutes"] < floor:
        raise HTTPException(status_code=422, detail=f"default_interval_minutes must be at least {floor}")
    for key, value in data.items():
        if value is None:
            # null means "leave unchanged", except nullable text fields where it clears
            if key in ("ntfy_topic", "default_zip"):
                setattr(s, key, None)
            continue
        if key == "ntfy_token":
            s.ntfy_token = value.strip() or None  # "" clears
        elif key in ("ntfy_topic", "default_zip"):
            setattr(s, key, value or None)
        else:
            setattr(s, key, value)
    db.commit()
    db.refresh(s)
    return settings_out(s)


@router.post("/test-notification", response_model=TestNotificationResult)
async def test_notification(user: User = Depends(current_user)):
    from ..db import SessionLocal

    with SessionLocal() as db:
        s = get_or_create_settings(db, user.id)
        db.expunge(s)
    if not (s.ntfy_topic or "").strip():
        return TestNotificationResult(ok=False, error="Set an ntfy topic first")
    ok, err = await send_ntfy(
        s,
        "Stock Watcher test",
        "If you can read this, push notifications are working.",
        None,
        ["white_check_mark"],
        s.ntfy_priority,
    )
    return TestNotificationResult(ok=ok, error=err)
