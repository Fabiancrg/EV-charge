"""Sign-in, sign-out, first-run setup and the current user's own account (password, sessions, API tokens)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import audit
from ..auth import (
    COOKIE_NAME,
    Principal,
    _token_hash,
    authenticate,
    browser_only,
    check_password_strength,
    check_throttle,
    clear_failures,
    client_ip,
    current_user,
    end_all_sessions,
    end_session,
    hash_password,
    new_api_token,
    record_failure,
    require,
    start_session,
    verify_password,
)
from ..database import get_db
from ..models import ApiToken, AuthSession, Role, User
from ..permissions import valid_permission
from ..schemas import LoginIn, Me, PasswordChange, SessionInfo, SetupIn, TokenCreated, TokenIn, TokenOut
from ..sso import local_login_allowed
from .users import user_out

router = APIRouter(prefix="/api/auth", tags=["auth"])


def me_out(p: Principal) -> Me:
    g = p.grants
    return Me(
        **user_out(p.user).model_dump(),
        display_name=p.display_name,
        permissions=sorted(g.global_perms),
        all_cars=sorted(g.all_cars),
        car_permissions={c: sorted(perms) for c, perms in g.per_car.items() if perms},
        via_token=p.token is not None,
    )


def setup_needed(db: Session) -> bool:
    return not db.scalar(select(func.count()).select_from(User))


@router.post("/login", response_model=Me)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> Me:
    from ..auth import principal_for

    keys = [f"user:{body.username.strip().lower()}", f"ip:{client_ip(request) or '?'}"]
    check_throttle(keys)
    if not local_login_allowed(db):
        raise HTTPException(403, "Password sign-in is disabled: use single sign-on")
    user = authenticate(db, body.username, body.password)
    if user is None:
        record_failure(keys)
        audit.record(db, request, "login.failed", username=body.username.strip().lower()[:100])
        db.commit()
        raise HTTPException(401, "Wrong username or password")
    clear_failures(keys[:1])
    start_session(db, response, user, request, method="password")
    audit.record(db, request, "login", user=user, details={"method": "password"})
    db.commit()
    return me_out(principal_for(user))


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    response = Response(status_code=204)
    end_session(db, request, response)
    return response


@router.get("/me", response_model=Me)
def me(user: Principal = Depends(current_user)) -> Me:
    return me_out(user)


@router.post("/password", status_code=204)
def change_password(body: PasswordChange, request: Request, user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> Response:
    if user.user.has_password and not verify_password(body.current_password, user.user.password_hash):
        raise HTTPException(422, "The current password is wrong")
    check_password_strength(body.new_password)
    user.user.password_hash = hash_password(body.new_password)
    end_all_sessions(db, user.id, keep_request=request)  # other browsers are signed out
    audit.record(db, request, "password.changed", target=f"user:{user.id}")
    db.commit()
    return Response(status_code=204)


@router.get("/setup")
def setup_state(db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"needed": setup_needed(db)}


@router.post("/setup", response_model=Me, status_code=201)
def setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)) -> Me:
    """First run only: create the first administrator."""
    from ..auth import principal_for

    if not setup_needed(db):
        raise HTTPException(409, "Setup is already done: sign in")
    check_password_strength(body.password)
    admin_role = db.scalar(select(Role).where(Role.name == "Administrator"))
    user = User(
        username=body.username.strip().lower(), full_name=(body.full_name or "").strip() or None,
        role_id=admin_role.id if admin_role else None, password_hash=hash_password(body.password),
    )  # fmt: skip
    db.add(user)
    db.flush()
    audit.record(db, request, "setup", target=f"user:{user.id}", user=user)
    db.commit()
    db.refresh(user)
    start_session(db, response, user, request, method="password")
    return me_out(principal_for(user))


# ── My sign-in sessions ──────────────────────────────────────────────────────


def _session_id(s: AuthSession) -> str:
    return s.token_hash[:16]  # a prefix of the hash: identifies the session, can't be used to sign in


@router.get("/sessions", response_model=list[SessionInfo])
def my_sessions(request: Request, user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> list[SessionInfo]:
    current = _token_hash(request.cookies.get(COOKIE_NAME, ""))
    now = datetime.now(UTC)
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == user.id)).all()
    out = [
        SessionInfo(
            id=_session_id(s),
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.expires_at,
            ip=s.ip,
            user_agent=s.user_agent,
            method=s.method,
            current=s.token_hash == current,
        )  # fmt: skip
        for s in rows
        if (s.expires_at if s.expires_at.tzinfo else s.expires_at.replace(tzinfo=UTC)) > now
    ]
    return sorted(out, key=lambda x: (not x.current, -(x.last_seen_at or x.created_at).timestamp()))


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(session_id: str, request: Request, user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> Response:
    if len(session_id) < 16:
        raise HTTPException(404, "Session not found")
    s = db.scalar(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.token_hash.like(f"{session_id}%")))
    if s is None:
        raise HTTPException(404, "Session not found")
    db.delete(s)
    audit.record(db, request, "session.revoked", target=f"user:{user.id}", details={"session": session_id})
    db.commit()
    return Response(status_code=204)


@router.post("/sessions/revoke-others", status_code=204)
def revoke_other_sessions(request: Request, user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> Response:
    end_all_sessions(db, user.id, keep_request=request)
    audit.record(db, request, "session.revoked_others", target=f"user:{user.id}")
    db.commit()
    return Response(status_code=204)


# ── My API tokens ────────────────────────────────────────────────────────────


def token_out(t: ApiToken) -> TokenOut:
    return TokenOut(id=t.id, name=t.name, prefix=t.prefix, permissions=json.loads(t.permissions or "[]"), created_at=t.created_at,
                    expires_at=t.expires_at, last_used_at=t.last_used_at, revoked=t.revoked)  # fmt: skip


@router.get("/tokens", response_model=list[TokenOut])
def my_tokens(user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> list[TokenOut]:
    return [token_out(t) for t in db.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc()))]


@router.post("/tokens", response_model=TokenCreated, status_code=201)
def create_token(body: TokenIn, request: Request, user: Principal = Depends(require("tokens:create")), db: Session = Depends(get_db)) -> TokenCreated:
    if user.token is not None:
        raise HTTPException(403, "An API token can't create tokens")
    bad = [p for p in body.permissions if not valid_permission(p)]
    if bad:
        raise HTTPException(422, f"Unknown permission(s): {', '.join(bad)}")
    raw, hashed = new_api_token()
    t = ApiToken(
        user_id=user.id, name=body.name.strip(), token_hash=hashed, prefix=raw[:12], permissions=json.dumps(sorted(set(body.permissions))),
        expires_at=datetime.now(UTC) + timedelta(days=body.expires_days) if body.expires_days else None,
    )  # fmt: skip
    db.add(t)
    db.flush()
    audit.record(db, request, "token.created", target=f"token:{t.id}", details={"name": t.name, "permissions": body.permissions})
    db.commit()
    return TokenCreated(**token_out(t).model_dump(), token=raw)


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: int, request: Request, user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> Response:
    t = db.get(ApiToken, token_id)
    if t is None or (t.user_id != user.id and not user.has("users:manage")):
        raise HTTPException(404, "Token not found")
    t.revoked = True
    audit.record(db, request, "token.revoked", target=f"token:{t.id}", details={"name": t.name, "owner": t.user.username})
    db.commit()
    return Response(status_code=204)


@router.delete("/tokens", status_code=204)
def purge_revoked(user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> Response:
    """Remove my revoked and expired tokens from the list."""
    db.execute(delete(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked.is_(True)))
    db.commit()
    return Response(status_code=204)
