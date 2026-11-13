"""User management: global role, group, a role per car, password, sign-out everywhere.

Administrators (users:manage) manage everyone. A group manager manages the members of their group: they add
members up to the group's limit and give them a role on the group's cars, but can't give a privileged
global role, change the group, name managers, or touch an account managed by an administrator."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..auth import Principal, check_password_strength, current_user, end_all_sessions, hash_password
from ..database import get_db
from ..models import Car, CarGrant, Group, Role, User
from ..permissions import GLOBAL_PERMISSIONS, expand, matches
from ..schemas import CarGrantIO, UserIn, UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


def user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, username=u.username, full_name=u.full_name, email=u.email, role_id=u.role_id, role_name=u.role.name if u.role else None,
        active=u.active, grants=[CarGrantIO(car_id=g.car_id, role_id=g.role_id) for g in u.grants if g.role_id is not None],
        has_password=u.has_password, sso=bool(u.oidc_subject), created_at=u.created_at, last_login=u.last_login,
        group_id=u.group_id, group_name=u.group.name if u.group else None, group_manager=u.group_manager,
    )  # fmt: skip


def manager(me: Principal = Depends(current_user)) -> Principal:
    """Who may manage users: administrators, and group managers (their own group)."""
    if not (me.has("users:manage") or me.is_group_manager):
        raise HTTPException(403, "Permission needed: users:manage, or be a manager of your group")
    return me


def privileged(role: Role | None) -> bool:
    """A global role only an administrator gives: it has app-wide permissions, or applies to every car."""
    return role is not None and (role.all_cars or bool(expand(role.permission_set, GLOBAL_PERMISSIONS)))


def _managed(db: Session, user_id: int, me: Principal) -> User:
    """The user, for who may manage them."""
    u = db.get(User, user_id)
    if u is None or (not me.has("users:manage") and u.group_id != me.group_id):
        raise HTTPException(404, "User not found")
    if not me.has("users:manage") and privileged(u.role):
        raise HTTPException(403, "This account is managed by an administrator")
    return u


def check_group_size(db: Session, u: User) -> None:
    """An active member more must fit in the group's limit (set by an administrator)."""
    if u.group_id is None or not u.active:
        return
    group = db.get(Group, u.group_id)
    others = db.scalar(select(func.count()).select_from(User).where(User.group_id == u.group_id, User.active.is_(True), User.id != (u.id or -1))) or 0
    if group is not None and others >= group.max_members:
        raise HTTPException(409, f"The group '{group.name}' is limited to {group.max_members} member(s): ask an administrator to raise the limit")


def user_admins_left(db: Session) -> int:
    """Active users whose global role can manage users (there must always be one)."""
    managing = {r.id for r in db.scalars(select(Role)) if matches(r.permission_set, "users:manage")}
    return sum(1 for u in db.scalars(select(User).where(User.active.is_(True))) if u.role_id in managing)  # by id: u.role may be stale


def check_admins_left(db: Session) -> None:
    db.flush()  # the session doesn't autoflush: make the change visible to the count below
    if user_admins_left(db) == 0:
        db.rollback()
        raise HTTPException(409, "Keep at least one active user whose role can manage users")


def _apply(db: Session, u: User, body: UserIn, me: Principal) -> dict:
    admin = me.has("users:manage")
    clash = db.scalar(select(User).where(User.username == body.username, User.id != (u.id or -1)))
    if clash:
        raise HTTPException(409, f"The username '{body.username}' is taken")
    role = db.get(Role, body.role_id) if body.role_id is not None else None
    if body.role_id is not None and role is None:
        raise HTTPException(422, "Unknown role")
    if not admin and privileged(role) and body.role_id != u.role_id:
        raise HTTPException(403, f"Only an administrator can give the role '{role.name}'")  # type: ignore[union-attr]
    # The group: administrators choose (a new user joins theirs by default); a group manager's members stay in the group
    was_member = u.id is not None and u.group_id is not None and u.active
    if admin:
        group_id = body.group_id if body.group_id is not None else (me.group_id if u.id is None else u.group_id)
        if group_id is not None and db.get(Group, group_id) is None:
            raise HTTPException(422, "Unknown group")
        group_manager = body.group_manager and group_id is not None
    else:
        group_id, group_manager = me.group_id, bool(u.group_manager) if u.id is not None else False
    wanted: dict[int, int] = {}
    for g in body.grants:
        car = db.get(Car, g.car_id)
        if car is None or db.get(Role, g.role_id) is None or (not admin and car.group_id != me.group_id):
            raise HTTPException(422, "Unknown car or role")
        wanted[g.car_id] = g.role_id
    if not admin:  # the cars of other groups a member may have (given by an administrator) are kept
        wanted |= {g.car_id: g.role_id for g in u.grants if g.role_id is not None and g.car.group_id != me.group_id}
    before = {"role_id": u.role_id, "grants": {g.car_id: g.role_id for g in u.grants}, "active": u.active, "group_id": u.group_id, "group_manager": u.group_manager}
    joins = group_id != u.group_id or (body.active and not was_member)
    u.group_id, u.group_manager = group_id, group_manager
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
    if joins:
        check_group_size(db, u)
    after = {"role_id": u.role_id, "grants": wanted, "active": u.active, "group_id": u.group_id, "group_manager": u.group_manager}
    return {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}


@router.get("", response_model=list[UserOut])
def list_users(me: Principal = Depends(manager), db: Session = Depends(get_db)) -> list[UserOut]:
    """Every user for administrators; the members of their group for a group manager."""
    query = select(User).order_by(User.username)
    if not me.has("users:manage"):
        query = query.where(User.group_id == me.group_id)
    return [user_out(u) for u in db.scalars(query)]


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserIn, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> UserOut:
    u = User(password_hash="!")  # without a password: single sign-on only
    db.add(u)
    _apply(db, u, body, me)
    db.flush()
    audit.record(db, request, "user.created", target=f"user:{u.id}", details={"username": u.username, "role_id": u.role_id, "cars": len(body.grants)})
    db.commit()
    return user_out(u)


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserIn, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> UserOut:
    u = _managed(db, user_id, me)
    changes = _apply(db, u, body, me)
    check_admins_left(db)
    if not u.active or body.password:
        end_all_sessions(db, u.id, keep_request=request if u.id == me.id else None)
    audit.record(db, request, "user.updated", target=f"user:{u.id}", details={"username": u.username, **changes, **({"password": "reset"} if body.password else {})})
    db.commit()
    return user_out(u)


@router.post("/{user_id}/signout", status_code=204)
def sign_out_everywhere(user_id: int, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> Response:
    u = _managed(db, user_id, me)
    end_all_sessions(db, u.id, keep_request=request if u.id == me.id else None)
    audit.record(db, request, "user.signed_out", target=f"user:{u.id}", details={"username": u.username})
    db.commit()
    return Response(status_code=204)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> Response:
    u = _managed(db, user_id, me)
    if u.id == me.id:
        raise HTTPException(409, "You can't delete your own account")
    username = u.username
    db.delete(u)  # their sessions stay; "entered by" becomes empty
    check_admins_left(db)
    audit.record(db, request, "user.deleted", target=f"user:{user_id}", details={"username": username})
    db.commit()
    return Response(status_code=204)
