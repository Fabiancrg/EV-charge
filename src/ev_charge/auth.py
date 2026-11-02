"""Authentication: password hashing, sign-in sessions (cookie), current user, roles and car scope.

- Passwords: scrypt (standard library), salted, with the parameters stored in the hash.
- Sessions: a random token in an HttpOnly, SameSite=Lax cookie; the database keeps only its SHA-256.
- Roles: ``admin`` can do everything; ``driver`` works only with the cars assigned to them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import CONFIG
from .database import get_db
from .models import AuthSession, User

COOKIE_NAME = "ev_session"
ROLES = ("admin", "driver")
MIN_PASSWORD_LENGTH = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


# ── Passwords ────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, f"The password must have at least {MIN_PASSWORD_LENGTH} characters")


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # same work for unknown usernames (no user enumeration by timing)


# ── Failed sign-in throttling (in memory, per username and per client address) ──

_FAILS: dict[str, list[float]] = {}
_WINDOW_SECONDS = 15 * 60
_MAX_FAILS = 8


def _recent(key: str) -> list[float]:
    now = time.monotonic()
    fails = [t for t in _FAILS.get(key, []) if now - t < _WINDOW_SECONDS]
    _FAILS[key] = fails
    return fails


def check_throttle(keys: Iterable[str]) -> None:
    if any(len(_recent(k)) >= _MAX_FAILS for k in keys):
        raise HTTPException(429, "Too many failed sign-ins: try again in a few minutes")


def record_failure(keys: Iterable[str]) -> None:
    for k in keys:
        _recent(k).append(time.monotonic())


def clear_failures(keys: Iterable[str]) -> None:
    for k in keys:
        _FAILS.pop(k, None)


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.active:
        return None
    return user


# ── Sessions ─────────────────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes


def start_session(db: Session, response: Response, user: User, request: Request) -> None:
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(days=CONFIG.session_days)
    now = datetime.now(UTC)
    db.execute(delete(AuthSession).where(AuthSession.expires_at < now))  # housekeeping
    db.add(AuthSession(token_hash=_token_hash(token), user_id=user.id, expires_at=now + lifetime))
    user.last_login = now
    db.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(lifetime.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def end_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")


def end_all_sessions(db: Session, user_id: int, keep_request: Request | None = None) -> None:
    """Sign a user out everywhere (password changed, account disabled), optionally keeping the current browser."""
    query = delete(AuthSession).where(AuthSession.user_id == user_id)
    token = keep_request.cookies.get(COOKIE_NAME) if keep_request else None
    if token:
        query = query.where(AuthSession.token_hash != _token_hash(token))
    db.execute(query)


def user_from_request(db: Session, request: Request) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    s = db.get(AuthSession, _token_hash(token))
    if s is None or _aware(s.expires_at) < datetime.now(UTC) or not s.user.active:
        return None
    return s.user


# ── Dependencies ─────────────────────────────────────────────────────────────


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_from_request(db, request)
    if user is None:
        raise HTTPException(401, "Sign in first")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Administrators only")
    return user


def car_scope(user: User) -> set[int] | None:
    """Car ids the user may work with; None means every car (admin)."""
    return None if user.is_admin else {c.id for c in user.cars}


def check_car_access(user: User, car_id: int) -> None:
    scope = car_scope(user)
    if scope is not None and car_id not in scope:
        raise HTTPException(403, "This car is not assigned to you")


def restrict_car_ids(user: User, requested: list[int] | None) -> list[int] | None:
    """The car filter of a list/report request, limited to the user's cars (None: every car)."""
    scope = car_scope(user)
    if scope is None:
        return requested or None
    if requested:
        return [c for c in requested if c in scope] or [-1]
    return sorted(scope) or [-1]
