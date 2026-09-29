"""Admin-only user management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import images
from ..db import get_db
from ..models import Item, Notification, User
from ..models import Session as SessionRow
from ..schemas import UserCreate, UserOut, UserPatch
from ..security import admin_required, hash_password
from .auth import make_user

router = APIRouter(prefix="/users", tags=["users"])


def _admin_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) or 0


def _get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("", response_model=list[UserOut])
def list_users(_: User = Depends(admin_required), db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.id)).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, _: User = Depends(admin_required), db: Session = Depends(get_db)):
    return make_user(db, body.username, body.password, body.is_admin)


@router.patch("/{user_id}", response_model=UserOut)
def patch_user(
    user_id: int, body: UserPatch, _: User = Depends(admin_required), db: Session = Depends(get_db)
):
    user = _get_user(db, user_id)
    if body.is_admin is False and user.is_admin and _admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last admin")
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        db.query(SessionRow).filter(SessionRow.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, me: User = Depends(admin_required), db: Session = Depends(get_db)):
    if user_id == me.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    user = _get_user(db, user_id)
    if user.is_admin and _admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last admin")
    files = db.scalars(select(Item.image_path).where(Item.user_id == user.id)).all()
    db.query(Notification).filter(Notification.user_id == user.id).delete(synchronize_session=False)
    db.query(Item).filter(Item.user_id == user.id).delete(synchronize_session=False)  # events cascade in DB
    db.delete(user)  # settings/sessions cascade
    db.commit()
    for f in files:
        images.delete_image_file(f)
    return Response(status_code=204)
