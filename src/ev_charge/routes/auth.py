"""Sign-in, sign-out, first-run setup and the current user's own account."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import (
    authenticate,
    check_password_strength,
    check_throttle,
    clear_failures,
    current_user,
    end_all_sessions,
    end_session,
    hash_password,
    record_failure,
    start_session,
    verify_password,
)
from ..database import get_db
from ..models import User
from ..schemas import LoginIn, Me, PasswordChange, SetupIn
from .users import user_out

router = APIRouter(prefix="/api/auth", tags=["auth"])


def me_out(user: User) -> Me:
    return Me(**user_out(user).model_dump(), display_name=user.full_name or user.username)


def setup_needed(db: Session) -> bool:
    return not db.scalar(select(func.count()).select_from(User))


@router.post("/login", response_model=Me)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> Me:
    keys = [f"user:{body.username.strip().lower()}", f"ip:{request.client.host if request.client else '?'}"]
    check_throttle(keys)
    user = authenticate(db, body.username, body.password)
    if user is None:
        record_failure(keys)
        raise HTTPException(401, "Wrong username or password")
    clear_failures(keys[:1])
    start_session(db, response, user, request)
    return me_out(user)


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    response = Response(status_code=204)
    end_session(db, request, response)
    return response


@router.get("/me", response_model=Me)
def me(user: User = Depends(current_user)) -> Me:
    return me_out(user)


@router.post("/password", status_code=204)
def change_password(body: PasswordChange, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(422, "The current password is wrong")
    check_password_strength(body.new_password)
    user.password_hash = hash_password(body.new_password)
    end_all_sessions(db, user.id, keep_request=request)  # other browsers are signed out
    db.commit()
    return Response(status_code=204)


@router.get("/setup")
def setup_state(db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"needed": setup_needed(db)}


@router.post("/setup", response_model=Me, status_code=201)
def setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)) -> Me:
    """First run only: create the first administrator."""
    if not setup_needed(db):
        raise HTTPException(409, "Setup is already done: sign in")
    check_password_strength(body.password)
    user = User(username=body.username.strip().lower(), full_name=(body.full_name or "").strip() or None, role="admin", password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    start_session(db, response, user, request)
    return me_out(user)
