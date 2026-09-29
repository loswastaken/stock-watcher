"""Auth endpoints: status, first-run setup, login, logout, me, change-password."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Session as SessionRow
from ..models import User, UserSettings
from ..schemas import AuthStatus, ChangePassword, Credentials, SetupRequest, UserOut
from ..security import (
    burn_password_check,
    clear_session_cookie,
    client_ip,
    create_session,
    current_user,
    hash_password,
    login_limiter,
    optional_user,
    set_session_cookie,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_setup_lock = threading.Lock()


def make_user(db: Session, username: str, password: str, is_admin: bool) -> User:
    user = User(
        username=username,
        username_key=username.lower(),
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    user.settings = UserSettings()
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists")
    db.refresh(user)
    return user


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request, db: Session = Depends(get_db)):
    count = db.scalar(select(func.count()).select_from(User)) or 0
    user = optional_user(request) if count else None
    return AuthStatus(setup_required=count == 0, user=UserOut.model_validate(user) if user else None)


@router.post("/setup", response_model=UserOut)
def setup(body: SetupRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    with _setup_lock:
        if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
            raise HTTPException(status_code=409, detail="Setup has already been completed")
        user = make_user(db, body.username, body.password, is_admin=True)
    token = create_session(db, user.id, request)
    set_session_cookie(response, token, request)
    return user


@router.post("/login", response_model=UserOut)
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = client_ip(request)
    key = body.username.strip().lower()
    login_limiter.check(ip, key)
    user = db.scalar(select(User).where(User.username_key == key))
    if user is None:
        burn_password_check(body.password)
        ok = False
    else:
        ok = verify_password(user.password_hash, body.password)
    if not ok:
        login_limiter.fail(ip, key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    login_limiter.reset(ip, key)
    token = create_session(db, user.id, request)
    set_session_cookie(response, token, request)
    return user


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)):
    user = optional_user(request)
    sid = getattr(request.state, "session_id", None)
    if user is not None and sid:
        row = db.get(SessionRow, sid)
        if row is not None:
            db.delete(row)
            db.commit()
    resp = Response(status_code=204)
    clear_session_cookie(resp, request)
    return resp


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.post("/change-password", status_code=204)
def change_password(
    body: ChangePassword,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    row = db.get(User, user.id)
    if row is None or not verify_password(row.password_hash, body.current_password):
        # 400 (not 401) so the SPA doesn't treat it as an expired session
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    row.password_hash = hash_password(body.new_password)
    keep = getattr(request.state, "session_id", None)
    q = db.query(SessionRow).filter(SessionRow.user_id == row.id)
    if keep:
        q = q.filter(SessionRow.id != keep)
    q.delete(synchronize_session=False)
    db.commit()
    return Response(status_code=204)
