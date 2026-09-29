"""Apple helpers."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends

from ..checkers import resolve_apple
from ..models import User
from ..schemas import AppleResolveOut, UrlRequest
from ..security import current_user

log = logging.getLogger("stockwatcher.apple")
router = APIRouter(prefix="/apple", tags=["apple"])


@router.post("/resolve", response_model=AppleResolveOut)
async def resolve(body: UrlRequest, user: User = Depends(current_user)):
    try:
        data = await asyncio.wait_for(resolve_apple(body.url), 45)
    except Exception as e:  # noqa: BLE001 - best effort; UI falls back to manual entry
        log.info("apple resolve failed for %s: %s", body.url, e)
        return AppleResolveOut(error=f"Could not read the Apple page ({type(e).__name__})")
    data = data if isinstance(data, dict) else {}
    return AppleResolveOut(
        product_name=data.get("product_name"),
        image_url=data.get("image_url"),
        variants=data.get("variants") or [],
        selected_part_number=data.get("selected_part_number"),
    )
