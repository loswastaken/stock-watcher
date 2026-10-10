from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..schemas import HealthOut

router = APIRouter(tags=["health"])


@router.api_route("/health", methods=["GET", "HEAD"], response_model=HealthOut)  # HEAD: uptime monitors
def health():
    return HealthOut(status="ok", version=get_settings().app_version)
