"""User management (administrators)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import check_password_strength, end_all_sessions, hash_password, require_admin
from ..database import get_db
from ..models import Car, User
from ..schemas import UserIn, UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


def user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, username=u.username, full_name=u.full_name, email=u.email, role=u.role, active=u.active,  # type: ignore[arg-type]
        car_ids=[c.id for c in u.cars], created_at=u.created_at, last_login=u.last_login,
    )  # fmt: skip


def _get(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "User not found")
    return u


def _apply(db: Session, u: User, body: UserIn) -> None:
    clash = db.scalar(select(User).where(User.username == body.username, User.id != (u.id or -1)))
    if clash:
        raise HTTPException(409, f"The username '{body.username}' is taken")
    cars = db.scalars(select(Car).where(Car.id.in_(body.car_ids))).all() if body.car_ids else []
    if len(cars) != len(set(body.car_ids)):
        raise HTTPException(422, "Unknown car")
    u.username = body.username
    u.full_name = body.full_name
    u.email = body.email
    u.role = body.role
    u.active = body.active
    u.cars = list(cars)
    if body.password:
        check_password_strength(body.password)
        u.password_hash = hash_password(body.password)


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True))) or 0


@router.get("", response_model=list[UserOut])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[UserOut]:
    return [user_out(u) for u in db.scalars(select(User).order_by(User.username))]


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    if not body.password:
        raise HTTPException(422, "Give the new user a password")
    u = User()
    _apply(db, u, body)
    db.add(u)
    db.commit()
    return user_out(u)


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserIn, request: Request, me: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    u = _get(db, user_id)
    was_admin = u.is_admin and u.active
    _apply(db, u, body)
    db.flush()  # the session doesn't autoflush: make the change visible to the count below
    if was_admin and not (u.is_admin and u.active) and _active_admins(db) == 0:
        db.rollback()
        raise HTTPException(409, "Keep at least one active administrator")
    if not u.active or body.password:
        end_all_sessions(db, u.id, keep_request=request if u.id == me.id else None)
    db.commit()
    return user_out(u)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, me: User = Depends(require_admin), db: Session = Depends(get_db)) -> Response:
    u = _get(db, user_id)
    if u.id == me.id:
        raise HTTPException(409, "You can't delete your own account")
    db.delete(u)  # their sessions stay; "entered by" becomes empty
    db.commit()
    return Response(status_code=204)
