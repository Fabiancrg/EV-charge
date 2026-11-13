"""Authentication: password hashing, sign-in sessions (cookie), current user, roles and car scope.

- Passwords: scrypt (standard library), salted, with the parameters stored in the hash.
- Sessions: a random token in an HttpOnly, SameSite=Lax cookie; the database keeps only its SHA-256.
- Permissions: see permissions.py (global role + a role per car); API tokens (Bearer evc_…) narrow them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import CONFIG
from .database import get_db
from .i18n import msg
from .models import ApiToken, AuthSession, Car, Group, User
from .permissions import ALL_PERMISSIONS, CAR_PERMISSIONS, GLOBAL_PERMISSIONS, Grants, expand

COOKIE_NAME = "ev_session"
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
        raise HTTPException(422, msg("The password must have at least {n} characters", n=MIN_PASSWORD_LENGTH))


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
        raise HTTPException(429, msg("Too many failed sign-ins: try again in a few minutes"))


def record_failure(keys: Iterable[str]) -> None:
    for k in keys:
        _recent(k).append(time.monotonic())


def clear_failures(keys: Iterable[str]) -> None:
    for k in keys:
        _FAILS.pop(k, None)


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.has_password:
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


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def start_session(db: Session, response: Response, user: User, request: Request, method: str = "password") -> None:
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(days=CONFIG.session_days)
    now = datetime.now(UTC)
    db.execute(delete(AuthSession).where(AuthSession.expires_at < now))  # housekeeping
    db.add(
        AuthSession(
            token_hash=_token_hash(token), user_id=user.id, expires_at=now + lifetime, last_seen_at=now,
            ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300], method=method,
        )
    )  # fmt: skip
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


def session_from_request(db: Session, request: Request) -> AuthSession | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    s = db.get(AuthSession, _token_hash(token))
    if s is None or _aware(s.expires_at) < datetime.now(UTC) or not s.user.active:
        return None
    now = datetime.now(UTC)
    if s.last_seen_at is None or now - _aware(s.last_seen_at) > timedelta(minutes=1):
        s.last_seen_at = now
        db.commit()
    return s


# ── API tokens ───────────────────────────────────────────────────────────────

TOKEN_PREFIX = "evc_"


def new_api_token() -> tuple[str, str]:
    """A fresh token (shown once) and its stored hash."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, _token_hash(raw)


def token_from_request(db: Session, request: Request) -> ApiToken | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    raw = header[7:].strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None
    t = db.scalar(select(ApiToken).where(ApiToken.token_hash == _token_hash(raw)))
    now = datetime.now(UTC)
    if t is None or t.revoked or not t.user.active or (t.expires_at is not None and _aware(t.expires_at) < now):
        raise HTTPException(401, msg("Invalid or expired API token"))
    if t.last_used_at is None or now - _aware(t.last_used_at) > timedelta(minutes=1):
        t.last_used_at = now
        db.commit()
    return t


# ── Principal: who is calling, with which permissions ────────────────────────


def compute_grants(user: User) -> Grants:
    g = Grants()
    if user.role is not None:
        perms = user.role.permission_set
        g.global_perms = expand(perms, GLOBAL_PERMISSIONS)
        if user.role.all_cars:
            g.all_cars = expand(perms, CAR_PERMISSIONS)
    for grant in user.grants:
        if grant.role is not None:
            g.per_car.setdefault(grant.car_id, set()).update(expand(grant.role.permission_set, CAR_PERMISSIONS))
    g.group_id = user.group_id
    if user.group_manager and user.group is not None:  # a group manager: everything on the cars of the group
        g.group_manager = True
        for car in user.group.cars:
            g.per_car.setdefault(car.id, set()).update(CAR_PERMISSIONS)
    return g


@dataclass
class Principal:
    user: User
    grants: Grants
    token: ApiToken | None = None
    session: AuthSession | None = None

    # Shortcuts used by the routes
    @property
    def id(self) -> int:
        return self.user.id

    @property
    def username(self) -> str:
        return self.user.username

    @property
    def full_name(self) -> str | None:
        return self.user.full_name

    @property
    def display_name(self) -> str:
        return self.user.full_name or self.user.username

    def has(self, permission: str) -> bool:
        return self.grants.has(permission)

    def can(self, permission: str, car_id: int) -> bool:
        return self.grants.on_car(permission, car_id)

    def cars_with(self, permission: str = "car:view") -> set[int] | None:
        return self.grants.cars_with(permission)

    @property
    def group_id(self) -> int | None:
        return self.grants.group_id

    def manages_group(self, group_id: int | None, permission: str = "groups:manage") -> bool:
        """May manage what belongs to this group: with the global permission (any group, or no group at all),
        or as a manager of that very group."""
        return self.has(permission) or (group_id is not None and self.grants.group_manager and self.grants.group_id == group_id)

    @property
    def is_group_manager(self) -> bool:
        return self.grants.group_manager and self.grants.group_id is not None


def token_grants(token: ApiToken) -> Grants:
    """An API token's own access: its role (or permissions), on all cars or on its chosen cars."""
    perms = token.permission_set
    car_perms = expand(perms, CAR_PERMISSIONS)
    cars = token.car_list
    return Grants(
        global_perms=expand(perms, GLOBAL_PERMISSIONS),
        all_cars=car_perms if cars is None else set(),
        per_car={} if cars is None else {c: set(car_perms) for c in cars},
        group_id=token.user.group_id,  # what it adds goes to the group of who created it
    )


def principal_for(user: User, token: ApiToken | None = None, session: AuthSession | None = None) -> Principal:
    grants = token_grants(token) if token is not None else compute_grants(user)
    return Principal(user=user, grants=grants, token=token, session=session)


def principal_from_request(db: Session, request: Request) -> Principal | None:
    token = token_from_request(db, request)
    if token is not None:
        return principal_for(token.user, token=token)
    s = session_from_request(db, request)
    if s is None:
        return None
    return principal_for(s.user, session=s)


# ── Dependencies ─────────────────────────────────────────────────────────────


def current_user(request: Request, db: Session = Depends(get_db)) -> Principal:
    p = principal_from_request(db, request)
    if p is None:
        raise HTTPException(401, msg("Sign in first"))
    request.state.principal = p  # for the audit log
    request.state.language = p.user.language  # for the error messages
    return p


def require(*permissions: str) -> Any:
    """Dependency: the caller needs one of these global permissions."""

    def checker(principal: Principal = Depends(current_user)) -> Principal:
        if not any(principal.has(p) for p in permissions):
            raise HTTPException(403, msg("Permission needed: {permissions}", permissions=" or ".join(permissions)))
        return principal

    return checker


def browser_only(principal: Principal = Depends(current_user)) -> Principal:
    """Account settings can't be changed with an API token."""
    if principal.token is not None:
        raise HTTPException(403, msg("Not available with an API token"))
    return principal


def visible_groups(principal: Principal, db: Session) -> set[int] | None:
    """Groups whose chargers the caller may see: their own, and those of the cars they may see; None: every group."""
    cars = principal.cars_with("car:view")
    if principal.has("groups:manage") or cars is None:
        return None
    ids = {principal.group_id} if principal.group_id is not None else set()
    if cars:
        ids |= {g for g in db.scalars(select(Car.group_id).where(Car.id.in_(cars))) if g is not None}
    return ids


def target_group(db: Session, principal: Principal, wanted: int | None, permission: str) -> int | None:
    """The group a new car or charger goes to: administrators choose (default: their own group),
    group managers add to their own group only."""
    if principal.has(permission):
        group_id = wanted if wanted is not None else principal.group_id
        if group_id is not None and db.get(Group, group_id) is None:
            raise HTTPException(422, msg("Unknown group"))
        return group_id
    if principal.is_group_manager:
        if wanted not in (None, principal.group_id):
            raise HTTPException(403, msg("You can only add to your own group"))
        return principal.group_id
    label = ALL_PERMISSIONS.get(permission, (permission,))[0].lower()
    raise HTTPException(403, msg("Not allowed ({label}): ask an administrator, or a manager of your group", label=label))


def check_group_manager(principal: Principal, group_id: int | None, permission: str) -> None:
    if not principal.manages_group(group_id, permission):
        label = ALL_PERMISSIONS.get(permission, (permission,))[0].lower()
        raise HTTPException(403, msg("Not allowed ({label}): this isn't managed by you", label=label))


def car_scope(principal: Principal, permission: str = "car:view") -> set[int] | None:
    """Car ids the caller has this permission on; None means every car."""
    return principal.cars_with(permission)


def check_car_access(principal: Principal, car_id: int, permission: str = "car:view") -> None:
    if not principal.can(permission, car_id):
        label = ALL_PERMISSIONS.get(permission, (permission,))[0].lower()
        raise HTTPException(403, msg("Not allowed on this car ({label})", label=label))


def restrict_car_ids(principal: Principal, requested: list[int] | None, permission: str = "car:view") -> list[int] | None:
    """The car filter of a list/report request, limited to the caller's cars (None: every car)."""
    scope = car_scope(principal, permission)
    if scope is None:
        return requested or None
    if requested:
        return [c for c in requested if c in scope] or [-1]
    return sorted(scope) or [-1]
