from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from datacourt import ledger
from datacourt.api.deps import SESSION_COOKIE, client_key, current_user, rate_limit
from datacourt.config import get_settings
from datacourt.db import models as m
from datacourt.db.session import get_db
from datacourt.security import DUMMY_PASSWORD_HASH, hash_token, verify_password
from datacourt.services import bootstrap, purge

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    workspace_name: str | None = Field(default=None, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


def _set_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=settings.cookie_domain,
    )


def me_payload(s: Session, user: m.User) -> dict:
    rows = s.execute(
        select(m.Organization, m.OrganizationMember.role)
        .join(m.OrganizationMember, m.OrganizationMember.org_id == m.Organization.id)
        .where(m.OrganizationMember.user_id == user.id)
        .order_by(m.Organization.created_at)
    ).all()
    return {
        "user": {"id": str(user.id), "email": user.email, "name": user.name, "is_demo": user.is_demo},
        "organizations": [
            {"id": str(o.id), "name": o.name, "slug": o.slug, "role": str(r), "is_demo": o.is_demo}
            for o, r in rows
        ],
    }


@router.post(
    "/register", dependencies=[Depends(rate_limit("auth", get_settings().rate_limit_auth_per_minute))]
)
def register(body: RegisterIn, request: Request, response: Response, s: Session = Depends(get_db)) -> dict:
    email = body.email.strip().lower()
    if s.scalar(select(m.User.id).where(m.User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="an account with this email already exists")
    user = bootstrap.create_user(s, email, body.password, body.name)
    org = bootstrap.create_org(s, body.workspace_name or f"{body.name.split()[0]}'s workspace", user)
    token = bootstrap.create_session(
        s, user, get_settings().session_ttl_hours, request.headers.get("user-agent")
    )
    ledger.security_event(
        s, "auth.register", org_id=org.id, actor_id=user.id, meta={"client": client_key(request)}
    )
    s.commit()
    _set_cookie(response, token)
    return me_payload(s, user)


@router.post("/login", dependencies=[Depends(rate_limit("auth", get_settings().rate_limit_auth_per_minute))])
def login(body: LoginIn, request: Request, response: Response, s: Session = Depends(get_db)) -> dict:
    user = s.scalar(select(m.User).where(m.User.email == body.email.strip().lower()))
    ok = verify_password(body.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not ok or not user.is_active or user.is_demo:
        ledger.security_event(s, "auth.login_failed", meta={"client": client_key(request)})
        s.commit()
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = bootstrap.create_session(
        s, user, get_settings().session_ttl_hours, request.headers.get("user-agent")
    )
    ledger.security_event(s, "auth.login", actor_id=user.id, meta={"client": client_key(request)})
    s.commit()
    _set_cookie(response, token)
    return me_payload(s, user)


@router.post("/demo", dependencies=[Depends(rate_limit("auth-demo", 10))])
def demo(request: Request, response: Response, s: Session = Depends(get_db)) -> dict:
    if not get_settings().demo_enabled:
        raise HTTPException(status_code=404, detail="demo disabled")
    try:
        user = bootstrap.join_demo(s)
    except LookupError as exc:
        raise HTTPException(status_code=503, detail="demo workspace is not available yet") from exc
    token = bootstrap.create_session(s, user, 12, request.headers.get("user-agent"))
    s.commit()
    _set_cookie(response, token)
    return me_payload(s, user)


@router.post("/logout")
def logout(request: Request, response: Response, s: Session = Depends(get_db)) -> dict:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        sess = s.scalar(select(m.AuthSession).where(m.AuthSession.token_hash == hash_token(raw)))
        if sess is not None:
            sess.revoked_at = datetime.now(UTC)
            s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", domain=get_settings().cookie_domain)
    return {"ok": True}


@router.get("/me")
def me(user: m.User = Depends(current_user), s: Session = Depends(get_db)) -> dict:
    return me_payload(s, user)


class DeleteAccountIn(BaseModel):
    confirm_email: EmailStr


@router.delete("/account")
def delete_account(
    body: DeleteAccountIn,
    response: Response,
    user: m.User = Depends(current_user),
    s: Session = Depends(get_db),
) -> dict:
    if body.confirm_email.strip().lower() != user.email:
        raise HTTPException(status_code=400, detail="confirmation email does not match")
    ledger.security_event(s, "auth.account_deleted", meta={"user_id": str(user.id)})
    result = bootstrap.delete_account(s, user)
    s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", domain=get_settings().cookie_domain)
    purged = purge.run_inline(result.pop("purge_jobs"))
    return {**result, **purged}
