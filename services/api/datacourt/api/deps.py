"""Request dependencies: authentication, CSRF/origin protection, rate limiting."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.security import hash_token, rate_limiter
from datacourt.tenancy import Principal

SESSION_COOKIE = "dc_session"
CSRF_HEADER = "x-datacourt-csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    return hash_token(ip)[:16]


def rate_limit(bucket: str, per_minute: int | None = None):
    def dep(request: Request) -> None:
        settings = get_settings()
        limit = per_minute or settings.rate_limit_default_per_minute
        if not rate_limiter.allow(f"{bucket}:{client_key(request)}", limit):
            raise HTTPException(status_code=429, detail="too many requests; slow down")

    return dep


def _principal_from_request(request: Request, s: Session) -> Principal | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        row = s.scalar(
            select(m.ApiToken).where(
                m.ApiToken.token_hash == hash_token(token), m.ApiToken.revoked_at.is_(None)
            )
        )
        if row is None:
            raise HTTPException(status_code=401, detail="invalid API token")
        row.last_used_at = datetime.now(UTC)
        s.commit()
        return Principal(user=None, token=row)
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    sess = s.scalar(
        select(m.AuthSession).where(
            m.AuthSession.token_hash == hash_token(raw), m.AuthSession.revoked_at.is_(None)
        )
    )
    if sess is None or sess.expires_at < datetime.now(UTC):
        return None
    user = s.get(m.User, sess.user_id)
    if user is None or not user.is_active:
        return None
    # Cookie-authenticated state changes must carry the custom header (blocked cross-site by CORS)
    # and, when the browser sends one, an allowed Origin.
    if request.method not in SAFE_METHODS:
        settings = get_settings()
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail="missing CSRF header")
        origin = request.headers.get("origin")
        if (
            origin
            and origin.rstrip("/") not in settings.origins
            and origin.rstrip("/") != settings.public_app_url.rstrip("/")
        ):
            raise HTTPException(status_code=403, detail="origin not allowed")
    return Principal(user=user)


def optional_principal(request: Request, s: Session = Depends(get_db)) -> Principal | None:
    return _principal_from_request(request, s)


def current_principal(request: Request, s: Session = Depends(get_db)) -> Principal:
    p = _principal_from_request(request, s)
    if p is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    if (
        p.token is not None
        and request.method not in SAFE_METHODS
        and "audits:write" not in (p.token.scopes or [])
    ):
        raise HTTPException(status_code=403, detail="token lacks write scope")
    return p


def current_user(p: Principal = Depends(current_principal)) -> m.User:
    if p.user is None:
        raise HTTPException(status_code=403, detail="this endpoint requires a user session")
    return p.user


def not_demo(p: Principal = Depends(current_principal)) -> Principal:
    if p.user is not None and p.user.is_demo:
        raise HTTPException(status_code=403, detail="demo accounts are read-only; register to make changes")
    return p
