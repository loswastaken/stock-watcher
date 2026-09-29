from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health():
    return HealthOut(status="ok", version=get_settings().app_version)
