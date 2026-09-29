"""Supported retailers (the registry the checkers use), for the Stores page and the item form."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..checkers.retailers.registry import RETAILERS
from ..models import User
from ..schemas import RetailerOut
from ..security import current_user

router = APIRouter(prefix="/retailers", tags=["retailers"])


@router.get("", response_model=list[RetailerOut])
def list_retailers(_: User = Depends(current_user)):
    return sorted((r.to_dict() for r in RETAILERS), key=lambda r: r["name"].lower())
