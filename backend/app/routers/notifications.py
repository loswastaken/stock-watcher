"""Notification center."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, Notification, User
from ..schemas import NotificationOut, NotificationPage
from ..security import current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _to_out(n: Notification, item_name: str | None) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        item_id=n.item_id,
        item_name=item_name,
        title=n.title,
        message=n.message or "",
        url=n.url,
        image_url=n.image_url,
        created_at=n.created_at,
        read=n.read,
        delivered=n.delivered,
        delivery_error=n.delivery_error,
    )


@router.get("", response_model=NotificationPage)
def list_notifications(
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    base = Notification.user_id == user.id
    filt = [base] + ([Notification.read.is_(False)] if unread_only else [])
    rows = db.execute(
        select(Notification, Item.name)
        .outerjoin(Item, Item.id == Notification.item_id)
        .where(*filt)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(Notification).where(*filt)) or 0
    unread = (
        db.scalar(
            select(func.count()).select_from(Notification).where(base, Notification.read.is_(False))
        )
        or 0
    )
    return NotificationPage(items=[_to_out(n, name) for n, name in rows], unread_count=unread, total=total)


@router.post("/read-all", status_code=204)
def read_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read.is_(False))
        .values(read=True)
    )
    db.commit()
    return Response(status_code=204)


@router.post("/{notification_id}/read", status_code=204)
def mark_read(notification_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = db.scalar(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id)
    )
    if n is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    n.read = True
    db.commit()
    return Response(status_code=204)


@router.delete("/{notification_id}", status_code=204)
def delete_one(notification_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    res = db.execute(
        delete(Notification).where(Notification.id == notification_id, Notification.user_id == user.id)
    )
    db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return Response(status_code=204)


@router.delete("", status_code=204)
def clear_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(delete(Notification).where(Notification.user_id == user.id))
    db.commit()
    return Response(status_code=204)
