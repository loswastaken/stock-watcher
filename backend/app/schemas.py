"""Pydantic API schemas (see PLAN.md "REST API")."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

from .checkers.retailers.registry import match_retailer, retailer_by_key
from .models import DEFAULT_GENERIC_CONFIG, Item


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="milliseconds") + "Z"


UTCDateTime = Annotated[datetime, PlainSerializer(_utc_iso, return_type=str, when_used="always")]

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]+$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_http_url(v: str) -> str:
    v = (v or "").strip()
    if len(v) > 2048:
        raise ValueError("url is too long")
    try:
        parts = urlsplit(v)
        host = parts.hostname
    except ValueError:
        raise ValueError("url must be a valid http(s) URL")
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ValueError("url must be a valid http(s) URL")
    return v


# ---------------------------------------------------------------- auth / users
class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SetupRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=1024)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        return check_username(v)


def check_username(v: str) -> str:
    v = (v or "").strip()
    if not 1 <= len(v) <= 64:
        raise ValueError("username must be 1-64 characters")
    if not USERNAME_RE.match(v):
        raise ValueError("username may only contain letters, digits and _ . @ + -")
    return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    is_admin: bool
    created_at: UTCDateTime


class AuthStatus(BaseModel):
    setup_required: bool
    user: UserOut | None = None


class ChangePassword(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class UserCreate(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=1024)
    is_admin: bool = False

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        return check_username(v)


class UserPatch(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=1024)
    is_admin: bool | None = None


# -------------------------------------------------------------------- settings
class SettingsOut(BaseModel):
    ntfy_server: str
    ntfy_topic: str | None
    ntfy_token_set: bool
    ntfy_priority: int
    default_interval_minutes: int
    default_zip: str | None
    default_max_distance_miles: int
    notify_on_out_of_stock: bool
    theme: str
    muted_retailers: list[str] = Field(default_factory=list)
    auto_rearm: bool = False
    alert_sound: bool = True


class SettingsUpdate(BaseModel):
    ntfy_server: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = Field(default=None, max_length=500)
    ntfy_priority: int | None = Field(default=None, ge=1, le=5)
    default_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    default_zip: str | None = Field(default=None, max_length=16)
    default_max_distance_miles: int | None = Field(default=None, ge=1, le=500)
    notify_on_out_of_stock: bool | None = None
    theme: Literal["dark", "light", "system"] | None = None
    muted_retailers: list[str] | None = Field(default=None, max_length=500)
    auto_rearm: bool | None = None
    alert_sound: bool | None = None

    @field_validator("muted_retailers")
    @classmethod
    def _muted(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for key in v:
            key = (key or "").strip().lower()
            if not retailer_by_key(key):
                raise ValueError(f"unknown retailer '{key}'")
            if key not in out:
                out.append(key)
        return out

    @field_validator("ntfy_server")
    @classmethod
    def _server(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = validate_http_url(v).rstrip("/")
        return v

    @field_validator("ntfy_topic")
    @classmethod
    def _topic(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if v and not TOPIC_RE.match(v):
            raise ValueError("ntfy_topic may only contain letters, digits, _ and - (max 64)")
        return v

    @field_validator("default_zip")
    @classmethod
    def _zip(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class TestNotificationResult(BaseModel):
    ok: bool
    error: str | None = None


# ----------------------------------------------------------------------- items
class GenericConfig(BaseModel):
    mode: Literal["auto", "selector", "text"] = "auto"
    selector: str | None = Field(default=None, max_length=500)
    in_stock_text: str | None = Field(default=None, max_length=500)
    out_of_stock_text: str | None = Field(default=None, max_length=500)
    render_js: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.mode == "selector" and not (self.selector or "").strip():
            raise ValueError("generic_config.selector is required when mode is 'selector'")
        if self.mode == "text" and not (
            (self.in_stock_text or "").strip() or (self.out_of_stock_text or "").strip()
        ):
            raise ValueError(
                "generic_config needs in_stock_text or out_of_stock_text when mode is 'text'"
            )
        return self


class ApplePart(BaseModel):
    part_number: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=200)

    @field_validator("part_number")
    @classmethod
    def _pn(cls, v: str) -> str:
        return v.strip()


class AppleConfig(BaseModel):
    parts: list[ApplePart] = Field(default_factory=list, max_length=50)
    zip: str | None = Field(default=None, max_length=16)
    max_distance_miles: float = Field(default=25, ge=1, le=500)
    watch_pickup: bool = True
    pickup_today_only: bool = True
    watch_delivery: bool = True


class RetailerConfigIn(BaseModel):
    """Per-item retailer options (see checkers/retailers/base.py RetailerConfig)."""
    fulfillment: Literal["delivery", "pickup", "any"] = "delivery"
    zip: str | None = Field(default=None, max_length=16)
    radius_miles: int = Field(default=25, ge=1, le=250)
    store_id: str | None = Field(default=None, max_length=64)
    official_only: bool = True
    condition: Literal["new", "any"] = "new"

    @field_validator("zip", "store_id")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v is not None else None


class ItemCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    url: str
    kind: Literal["generic", "apple"] | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    notify_enabled: bool = True
    image_url: str | None = Field(default=None, max_length=2048)
    generic_config: GenericConfig | None = None
    apple_config: AppleConfig | None = None
    retailer_config: RetailerConfigIn | None = None
    max_price: float | None = Field(default=None, gt=0, le=1_000_000)
    product_group: str | None = Field(default=None, max_length=64)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return validate_http_url(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return v.strip() if v else None


class ItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = None
    enabled: bool | None = None
    notify_enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    generic_config: GenericConfig | None = None
    apple_config: AppleConfig | None = None
    retailer_config: RetailerConfigIn | None = None
    max_price: float | None = Field(default=None, gt=0, le=1_000_000)  # null clears the limit
    product_group: str | None = Field(default=None, max_length=64)  # null / "" detaches

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v) if v is not None else v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        return v


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    url: str
    kind: str
    enabled: bool
    notify_enabled: bool
    muted_by_alert: bool = False  # alerts were turned off by an alert (auto re-arm may restore them)
    interval_minutes: int
    image_url: str | None
    status: str
    status_text: str
    price: str | None
    last_checked_at: UTCDateTime | None
    last_change_at: UTCDateTime | None
    last_error: str | None
    generic_config: dict[str, Any]
    apple_config: dict[str, Any] | None
    last_result: dict[str, Any]
    purchased_at: UTCDateTime | None = None
    purchased_price: str | None = None
    retailer_config: dict[str, Any] | None = None
    max_price: float | None = None
    last_in_stock_at: UTCDateTime | None = None
    product_group: str | None = None
    # derived: registry entry for the item's store + seller/cart details from the last check
    retailer: dict[str, Any] | None = None
    seller: str | None = None
    third_party: bool | None = None
    cart_url: str | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime

    @model_validator(mode="before")
    @classmethod
    def _from_item(cls, v):
        if not isinstance(v, Item):
            return v
        data = {name: getattr(v, name) for name in cls.model_fields if hasattr(v, name)}
        data.update(item_extras(v))
        return data

    @field_validator("generic_config", mode="before")
    @classmethod
    def _gc(cls, v):
        return v if v else dict(DEFAULT_GENERIC_CONFIG)

    @field_validator("last_result", mode="before")
    @classmethod
    def _lr(cls, v):
        return v if v else {}

    @field_validator("status_text", mode="before")
    @classmethod
    def _st(cls, v):
        return v or ""

    @field_validator("muted_by_alert", mode="before")
    @classmethod
    def _mba(cls, v):
        return bool(v)


def item_retailer(item: Item) -> dict[str, Any] | None:
    detail = item.last_result if isinstance(item.last_result, dict) else {}
    r = retailer_by_key(str(detail.get("retailer") or "")) or match_retailer(item.url or "")
    return r.to_dict() if r else None


def item_extras(item: Item) -> dict[str, Any]:
    detail = item.last_result if isinstance(item.last_result, dict) else {}
    seller = detail.get("seller")
    third_party = detail.get("third_party")
    cart_url = detail.get("cart_url")
    return {
        "retailer": item_retailer(item),
        "seller": str(seller) if seller else None,
        "third_party": third_party if isinstance(third_party, bool) else None,
        "cart_url": cart_url if isinstance(cart_url, str) and cart_url.startswith(("http://", "https://")) else None,
    }


class UrlRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return validate_http_url(v)


class StoreCreate(BaseModel):
    url: str
    retailer_config: RetailerConfigIn | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return validate_http_url(v)


class PreviewOut(BaseModel):
    name: str | None = None
    image_url: str | None = None
    price: str | None = None
    status: str | None = "unknown"
    is_apple: bool = False
    retailer: dict[str, Any] | None = None  # registry entry for the URL's store, when known
    error: str | None = None


class RetailerOut(BaseModel):
    key: str
    name: str
    domain: str
    color: str
    pickup: bool
    seller_filter: bool
    note: str | None = None


class StoreRow(BaseModel):
    """One store in a product group (GET /items/{id}/stores)."""
    id: int
    name: str
    url: str
    retailer: dict[str, Any] | None
    status: str
    status_text: str
    price: str | None
    max_price: float | None
    enabled: bool
    notify_enabled: bool
    last_in_stock_at: UTCDateTime | None
    last_checked_at: UTCDateTime | None


class RestockOut(BaseModel):
    """A moment the item came back in stock (GET /items/{id}/restocks)."""
    id: int
    checked_at: UTCDateTime
    status_text: str


class AppleVariant(BaseModel):
    part_number: str
    label: str | None = None
    price: str | None = None


class AppleResolveOut(BaseModel):
    product_name: str | None = None
    image_url: str | None = None
    variants: list[AppleVariant] = Field(default_factory=list)
    selected_part_number: str | None = None  # the model the pasted URL points to
    error: str | None = None


class CheckEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    item_id: int
    checked_at: UTCDateTime
    status: str
    status_text: str
    error: str | None
    duration_ms: int
    changed: bool


# --------------------------------------------------------------- notifications
class NotificationOut(BaseModel):
    id: int
    item_id: int | None
    item_name: str | None
    title: str
    message: str
    url: str | None
    image_url: str | None
    created_at: UTCDateTime
    read: bool
    delivered: bool
    delivery_error: str | None


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    unread_count: int
    total: int


class StatsOut(BaseModel):
    total: int
    in_stock: int
    out_of_stock: int
    unknown: int
    error: int
    paused: int
    unread_notifications: int
    checks_24h: int
    alerts_24h: int
    purchased: int = 0


class HealthOut(BaseModel):
    status: str
    version: str
