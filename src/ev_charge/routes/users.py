"""User management: global role, a role per car, password, sign-out everywhere."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..auth import Principal, check_password_strength, end_all_sessions, hash_password, require
from ..database import get_db
from ..models import Car, CarGrant, Role, User
from ..permissions import matches
from ..schemas import CarGrantIO, UserIn, UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


def user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, username=u.username, full_name=u.full_name, email=u.email, role_id=u.role_id, role_name=u.role.name if u.role else None,
        active=u.active, grants=[CarGrantIO(car_id=g.car_id, role_id=g.role_id) for g in u.grants if g.role_id is not None],
        has_password=u.has_password, sso=bool(u.oidc_subject), created_at=u.created_at, last_login=u.last_login,
    )  # fmt: skip


def _get(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "User not found")
    return u


def user_admins_left(db: Session) -> int:
    """Active users whose global role can manage users (there must always be one)."""
    managing = {r.id for r in db.scalars(select(Role)) if matches(r.permission_set, "users:manage")}
    return sum(1 for u in db.scalars(select(User).where(User.active.is_(True))) if u.role_id in managing)  # by id: u.role may be stale


def check_admins_left(db: Session) -> None:
    db.flush()  # the session doesn't autoflush: make the change visible to the count below
    if user_admins_left(db) == 0:
        db.rollback()
        raise HTTPException(409, "Keep at least one active user whose role can manage users")


def _apply(db: Session, u: User, body: UserIn) -> dict:
    clash = db.scalar(select(User).where(User.username == body.username, User.id != (u.id or -1)))
    if clash:
        raise HTTPException(409, f"The username '{body.username}' is taken")
    if body.role_id is not None and db.get(Role, body.role_id) is None:
        raise HTTPException(422, "Unknown role")
    wanted: dict[int, int] = {}
    for g in body.grants:
        if db.get(Car, g.car_id) is None or db.get(Role, g.role_id) is None:
            raise HTTPException(422, "Unknown car or role")
        wanted[g.car_id] = g.role_id
    before = {"role_id": u.role_id, "grants": {g.car_id: g.role_id for g in u.grants}, "active": u.active}
    u.username = body.username
    u.full_name = body.full_name
    u.email = body.email
    u.role_id = body.role_id
    u.active = body.active
    current = {g.car_id: g for g in u.grants}
    for car_id, g in current.items():
        if car_id not in wanted:
            u.grants.remove(g)
        else:
            g.role_id = wanted[car_id]
    for car_id, role_id in wanted.items():
        if car_id not in current:
            u.grants.append(CarGrant(car_id=car_id, role_id=role_id))
    if body.password:
        check_password_strength(body.password)
        u.password_hash = hash_password(body.password)
    after = {"role_id": u.role_id, "grants": wanted, "active": u.active}
    return {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}


@router.get("", response_model=list[UserOut])
def list_users(_: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> list[UserOut]:
    return [user_out(u) for u in db.scalars(select(User).order_by(User.username))]


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserIn, request: Request, _: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> UserOut:
    u = User(password_hash="!")  # without a password: single sign-on only
    db.add(u)
    _apply(db, u, body)
    db.flush()
    audit.record(db, request, "user.created", target=f"user:{u.id}", details={"username": u.username, "role_id": u.role_id, "cars": len(body.grants)})
    db.commit()
    return user_out(u)


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserIn, request: Request, me: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> UserOut:
    u = _get(db, user_id)
    changes = _apply(db, u, body)
    check_admins_left(db)
    if not u.active or body.password:
        end_all_sessions(db, u.id, keep_request=request if u.id == me.id else None)
    audit.record(db, request, "user.updated", target=f"user:{u.id}", details={"username": u.username, **changes, **({"password": "reset"} if body.password else {})})
    db.commit()
    return user_out(u)


@router.post("/{user_id}/signout", status_code=204)
def sign_out_everywhere(user_id: int, request: Request, me: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> Response:
    u = _get(db, user_id)
    end_all_sessions(db, u.id, keep_request=request if u.id == me.id else None)
    audit.record(db, request, "user.signed_out", target=f"user:{u.id}", details={"username": u.username})
    db.commit()
    return Response(status_code=204)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, request: Request, me: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> Response:
    u = _get(db, user_id)
    if u.id == me.id:
        raise HTTPException(409, "You can't delete your own account")
    username = u.username
    db.delete(u)  # their sessions stay; "entered by" becomes empty
    check_admins_left(db)
    audit.record(db, request, "user.deleted", target=f"user:{user_id}", details={"username": username})
    db.commit()
    return Response(status_code=204)
