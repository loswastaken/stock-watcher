"""Shared checker result types. Shapes are a contract — see PLAN.md."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Availability:
    key: str  # "stock" | "pickup:{store_number}:{part}" | "delivery2h:{part}"
    label: str


@dataclass
class CheckResult:
    status: str  # in_stock | out_of_stock | unknown | error
    status_text: str
    available: list[Availability] = field(default_factory=list)
    price: str | None = None
    title: str | None = None
    image_url: str | None = None
    detail: dict = field(default_factory=dict)
    error: str | None = None
