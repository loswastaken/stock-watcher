"""Dashboard stats."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CheckEvent, Item, Notification, User, utcnow
from ..schemas import StatsOut
from ..security import current_user

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("", response_model=StatsOut)
def stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    since = utcnow() - timedelta(hours=24)
    by_status = dict(
        db.execute(
            select(Item.status, func.count())
            .where(Item.user_id == user.id, Item.purchased_at.is_(None))
            .group_by(Item.status)
        ).all()
    )
    total = sum(by_status.values())
    paused = db.scalar(
        select(func.count()).select_from(Item).where(
            Item.user_id == user.id, Item.enabled.is_(False), Item.purchased_at.is_(None))
    ) or 0
    purchased = db.scalar(
        select(func.count()).select_from(Item).where(Item.user_id == user.id, Item.purchased_at.is_not(None))
    ) or 0
    unread = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read.is_(False))
    ) or 0
    checks = db.scalar(
        select(func.count())
        .select_from(CheckEvent)
        .join(Item, Item.id == CheckEvent.item_id)
        .where(Item.user_id == user.id, CheckEvent.checked_at >= since)
    ) or 0
    alerts = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.created_at >= since)
    ) or 0
    return StatsOut(
        total=total,
        in_stock=by_status.get("in_stock", 0),
        out_of_stock=by_status.get("out_of_stock", 0),
        unknown=by_status.get("unknown", 0),
        error=by_status.get("error", 0),
        paused=paused,
        unread_notifications=unread,
        checks_24h=checks,
        alerts_24h=alerts,
        purchased=purchased,
    )
