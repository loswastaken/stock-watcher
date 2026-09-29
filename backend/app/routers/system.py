"""Admin: app version and in-app updates (via Watchtower)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import updates
from ..models import User
from ..security import admin_required

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/update")
async def update_status(user: User = Depends(admin_required)):
    return await updates.status()


@router.post("/update/check")
async def update_check(user: User = Depends(admin_required)):
    return await updates.status(force=True)


@router.post("/update/apply", status_code=202)
async def update_apply(user: User = Depends(admin_required)):
    try:
        await updates.trigger_update()
    except updates.UpdateError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"started": True}
