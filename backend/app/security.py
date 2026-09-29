"""Password hashing, sessions, auth dependencies, login rate limiting, CSRF/security-headers middleware."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response
from sqlalchemy.orm import Session

from .config import get_secret_key, get_settings
from .db import SessionLocal
from .models import Session as SessionRow
from .models import User, utcnow

COOKIE_NAME = "sw_session"

# ------------------------------------------------------------------ passwords
_hasher = PasswordHasher()  # argon2id
_dummy_hash: str | None = None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


def burn_password_check(password: str) -> None:
    """Spend roughly the same time as a real verification (unknown-user timing equalizer)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("stock-watcher-dummy")
    verify_password(_dummy_hash, password)


# ------------------------------------------------------------------- sessions
def hash_token(token: str) -> str:
    """HMAC-SHA256 (keyed with the server secret) of the session token; only this is stored."""
    return hmac.new(get_secret_key().encode(), token.encode(), hashlib.sha256).hexdigest()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_session(db: Session, user_id: int, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        SessionRow(
            id=hash_token(token),
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(days=get_settings().session_days),
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
            ip=client_ip(request)[:64],
        )
    )
    db.commit()
    return token


def cookie_is_secure(request: Request) -> bool:
    mode = get_settings().cookie_secure
    if mode == "auto":
        return request.url.scheme == "https"
    return mode == "true"


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    s = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=s.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=cookie_is_secure(request)
    )


def purge_expired_sessions() -> int:
    with SessionLocal() as db:
        n = db.query(SessionRow).filter(SessionRow.expires_at <= utcnow()).delete()
        db.commit()
        return n


def _lookup_user(request: Request) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    with SessionLocal() as db:
        row = db.get(SessionRow, hash_token(token))
        if row is None:
            return None
        if row.expires_at <= utcnow():
            db.delete(row)
            db.commit()
            return None
        user = db.get(User, row.user_id)
        if user is None:
            return None
        db.expunge(user)  # detached, attributes already loaded
        request.state.session_id = row.id
        return user


def optional_user(request: Request) -> User | None:
    return _lookup_user(request)


def current_user(request: Request) -> User:
    """Authenticated user (detached from any DB session) or 401."""
    user = _lookup_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def admin_required(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# --------------------------------------------------------------- rate limiting
class LoginLimiter:
    """Sliding window of failed logins per (ip, username)."""

    def __init__(self) -> None:
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: tuple[str, str], now: float, window: int) -> list[float]:
        lst = [t for t in self._fails.get(key, []) if now - t < window]
        if lst:
            self._fails[key] = lst
        else:
            self._fails.pop(key, None)
        return lst

    def check(self, ip: str, username: str) -> None:
        """Enforce a per-(ip, username) limit plus a looser per-username cap.

        The per-username cap matters behind a reverse proxy, where X-Forwarded-For
        can be spoofed to rotate the apparent client IP.
        """
        s = get_settings()
        now = time.monotonic()
        with self._lock:
            if len(self._fails) > 5000:
                for k in list(self._fails):
                    self._prune(k, now, s.login_window_seconds)
            for key, limit in (
                ((ip, username), s.login_max_failures),
                (("*", username), s.login_max_failures * 3),
            ):
                lst = self._prune(key, now, s.login_window_seconds)
                if len(lst) >= limit:
                    retry = max(1, int(s.login_window_seconds - (now - lst[0])))
                    raise HTTPException(
                        status_code=429,
                        detail="Too many failed login attempts. Try again later.",
                        headers={"Retry-After": str(retry)},
                    )

    def fail(self, ip: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._fails.setdefault((ip, username), []).append(now)
            self._fails.setdefault(("*", username), []).append(now)

    def reset(self, ip: str, username: str) -> None:
        with self._lock:
            self._fails.pop((ip, username), None)

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()


login_limiter = LoginLimiter()


# ---------------------------------------------------------------- middleware
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_host(value: str) -> str | None:
    try:
        return urlsplit(value).netloc.lower() or None
    except ValueError:
        return None


class SecurityMiddleware:
    """Pure-ASGI CSRF protection for mutating /api requests + baseline security headers.

    - Bodies must be application/json or multipart/form-data.
    - If Origin (else Referer) is present, its host must match Host / X-Forwarded-Host.
    """

    def __init__(self, app):
        self.app = app

    async def _reject(self, send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path.startswith("/api/") and method not in _SAFE_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            has_body = headers.get("content-length", "0") not in ("", "0") or "transfer-encoding" in headers
            if has_body:
                ctype = headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype != "application/json" and ctype != "multipart/form-data":
                    await self._reject(send, 415, "Content-Type must be application/json or multipart/form-data")
                    return
            source = headers.get("origin") or headers.get("referer")
            if source:
                src_host = _origin_host(source)
                allowed = {
                    h.strip().lower()
                    for h in (headers.get("host", ""), headers.get("x-forwarded-host", ""))
                    if h.strip()
                }
                # X-Forwarded-Host may be a comma-separated list
                for h in headers.get("x-forwarded-host", "").split(","):
                    if h.strip():
                        allowed.add(h.strip().lower())
                if src_host is None or src_host not in allowed:
                    await self._reject(send, 403, "Cross-origin request blocked")
                    return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                hdrs = message.setdefault("headers", [])
                existing = {k.lower() for k, _ in hdrs}
                for k, v in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin"),
                ):
                    if k not in existing:
                        hdrs.append((k, v))
            await send(message)

        await self.app(scope, receive, send_wrapper)
