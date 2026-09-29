"""Authenticated image serving (only files referenced by the caller's items)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import images
from ..db import get_db
from ..models import Item, User
from ..security import current_user

router = APIRouter(prefix="/images", tags=["images"])

MEDIA_TYPES = {
    "webp": "image/webp",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
}


@router.get("/{filename}")
def get_image(filename: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    path = images.image_file(filename)  # None for traversal / odd names
    if path is None:
        raise HTTPException(status_code=404, detail="Image not found")
    owned = db.scalar(
        select(Item.id).where(Item.user_id == user.id, Item.image_path == filename).limit(1)
    )
    if owned is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(filename.rsplit(".", 1)[-1].lower(), "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=86400"},
    )
