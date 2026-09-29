"""Environment-driven settings (see PLAN.md "Config")."""
from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("stockwatcher.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    data_dir: Path = Path("/data")
    secret_key: str | None = None
    # "auto": Secure only when the request arrived over HTTPS (directly or via X-Forwarded-Proto).
    cookie_secure: Literal["auto", "true", "false"] = "auto"
    session_days: int = 30
    min_interval_seconds: int = 60
    check_concurrency: int = 4
    static_dir: Path = Path("/app/static")
    tz: str = "UTC"
    enable_browser: bool = True
    app_version: str = "dev"
    # Extras (not in PLAN.md): handy for tests / debugging.
    scheduler_enabled: bool = True
    login_max_failures: int = 10
    login_window_seconds: int = 900

    @field_validator("secret_key", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "stockwatcher.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def prepare_data_dir(settings: Settings | None = None) -> Settings:
    """Create DATA_DIR/images and resolve the secret key (env, else persisted file)."""
    s = settings or get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.images_dir.mkdir(parents=True, exist_ok=True)
    if not s.secret_key:
        key_file = s.data_dir / "secret.key"
        key = ""
        try:
            key = key_file.read_text().strip()
        except FileNotFoundError:
            pass
        if not key:
            key = secrets.token_urlsafe(48)
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(key)
            log.info("generated new secret key at %s", key_file)
        s.secret_key = key
    return s


def get_secret_key() -> str:
    s = get_settings()
    if not s.secret_key:
        prepare_data_dir(s)
    assert s.secret_key
    return s.secret_key
