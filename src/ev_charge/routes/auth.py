"""Sign-in, sign-out, first-run setup and the current user's own account (password, sign-in sessions)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, i18n
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
    record_failure,
    start_session,
    verify_password,
)
from ..database import get_db
from ..i18n import msg
from ..models import AuthSession, Group, Role, User
from ..permissions import ensure_default_group
from ..schemas import LoginIn, Me, PasswordChange, SessionInfo, SetupIn
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
        manages_group=p.is_group_manager,
    )


def setup_needed(db: Session) -> bool:
    return not db.scalar(select(func.count()).select_from(User))


@router.post("/login", response_model=Me)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> Me:
    from ..auth import principal_for

    keys = [f"user:{body.username.strip().lower()}", f"ip:{client_ip(request) or '?'}"]
    check_throttle(keys)
    if not local_login_allowed(db):
        raise HTTPException(403, msg("Password sign-in is disabled: use single sign-on"))
    user = authenticate(db, body.username, body.password)
    if user is None:
        record_failure(keys)
        audit.record(db, request, "login.failed", username=body.username.strip().lower()[:100])
        db.commit()
        raise HTTPException(401, msg("Wrong username or password"))
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


class LanguageIn(BaseModel):
    language: str | None = None  # empty: follow the browser


@router.put("/language")
def set_language(body: LanguageIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, str | None]:
    """Choose the language of the interface: saved in the profile when signed in, and remembered by this browser
    (so the sign-in page keeps it). Works without being signed in, for the public pages."""
    from ..auth import principal_from_request

    if body.language is not None and i18n.valid(body.language) is None:
        raise HTTPException(422, msg("Unknown language: choose one of {languages}", languages=", ".join(i18n.LANGUAGES)))
    principal = principal_from_request(db, request)
    if principal is not None and principal.token is None:
        principal.user.language = body.language
        db.commit()
    if body.language:
        response.set_cookie(i18n.COOKIE, body.language, max_age=365 * 86400, samesite="lax", path="/")
    else:
        response.delete_cookie(i18n.COOKIE, path="/")
    return {"language": body.language}


@router.get("/me", response_model=Me)
def me(user: Principal = Depends(current_user)) -> Me:
    return me_out(user)


@router.post("/password", status_code=204)
def change_password(body: PasswordChange, request: Request, user: Principal = Depends(browser_only), db: Session = Depends(get_db)) -> Response:
    if user.user.has_password and not verify_password(body.current_password, user.user.password_hash):
        raise HTTPException(422, msg("The current password is wrong"))
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
        raise HTTPException(409, msg("Setup is already done: sign in"))
    check_password_strength(body.password)
    admin_role = db.scalar(select(Role).where(Role.name == "Administrator"))
    ensure_default_group(db)
    user = User(
        username=body.username.strip().lower(), full_name=(body.full_name or "").strip() or None,
        role_id=admin_role.id if admin_role else None, password_hash=hash_password(body.password),
        group_id=db.scalar(select(Group.id).order_by(Group.id)), group_manager=True,
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
        raise HTTPException(404, msg("Session not found"))
    s = db.scalar(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.token_hash.like(f"{session_id}%")))
    if s is None:
        raise HTTPException(404, msg("Session not found"))
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
