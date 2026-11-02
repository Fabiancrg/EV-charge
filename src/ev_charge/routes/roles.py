"""Roles: named permission sets, used as a user's global role or as their role on a car."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..auth import Principal, current_user, require
from ..database import get_db
from ..models import CarGrant, Role, User
from ..permissions import CAR_PERMISSIONS, GLOBAL_PERMISSIONS, valid_permission
from ..schemas import RoleIn, RoleOut
from .users import check_admins_left

router = APIRouter(prefix="/api/roles", tags=["roles"])


def role_out(db: Session, r: Role) -> RoleOut:
    return RoleOut(
        id=r.id, name=r.name, description=r.description, permissions=sorted(r.permission_set), all_cars=r.all_cars, builtin=r.builtin,
        users=db.scalar(select(func.count()).select_from(User).where(User.role_id == r.id)) or 0,
        car_grants=db.scalar(select(func.count()).select_from(CarGrant).where(CarGrant.role_id == r.id)) or 0,
    )  # fmt: skip


@router.get("/permissions")
def permission_catalogue(_: Principal = Depends(current_user)) -> dict:
    """Every permission, with its label and group; global ones apply app-wide, car ones per car."""
    as_list = lambda cat: [{"name": k, "label": v[0], "group": v[1]} for k, v in cat.items()]  # noqa: E731
    return {"global": as_list(GLOBAL_PERMISSIONS), "car": as_list(CAR_PERMISSIONS)}


@router.get("", response_model=list[RoleOut])
def list_roles(_: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[RoleOut]:
    return [role_out(db, r) for r in db.scalars(select(Role).order_by(Role.builtin.desc(), Role.name))]


def _apply(db: Session, r: Role, body: RoleIn) -> None:
    if not body.name:
        raise HTTPException(422, "A name is required")
    clash = db.scalar(select(Role).where(func.lower(Role.name) == body.name.lower(), Role.id != (r.id or -1)))
    if clash:
        raise HTTPException(409, f"A role named '{body.name}' already exists")
    bad = [p for p in body.permissions if not valid_permission(p)]
    if bad:
        raise HTTPException(422, f"Unknown permission(s): {', '.join(bad)}")
    r.name = body.name
    r.description = body.description
    r.permissions = json.dumps(sorted(set(body.permissions)))
    r.all_cars = body.all_cars


@router.post("", response_model=RoleOut, status_code=201)
def create_role(body: RoleIn, request: Request, _: Principal = Depends(require("roles:manage")), db: Session = Depends(get_db)) -> RoleOut:
    r = Role()
    _apply(db, r, body)
    db.add(r)
    db.flush()
    audit.record(db, request, "role.created", target=f"role:{r.id}", details=body.model_dump())
    db.commit()
    return role_out(db, r)


@router.put("/{role_id}", response_model=RoleOut)
def update_role(role_id: int, body: RoleIn, request: Request, _: Principal = Depends(require("roles:manage")), db: Session = Depends(get_db)) -> RoleOut:
    r = db.get(Role, role_id)
    if r is None:
        raise HTTPException(404, "Role not found")
    before = {"name": r.name, "permissions": sorted(r.permission_set), "all_cars": r.all_cars}
    _apply(db, r, body)
    check_admins_left(db)
    audit.record(db, request, "role.updated", target=f"role:{r.id}", details={"before": before, "after": body.model_dump()})
    db.commit()
    return role_out(db, r)


@router.delete("/{role_id}", status_code=204)
def delete_role(role_id: int, request: Request, _: Principal = Depends(require("roles:manage")), db: Session = Depends(get_db)) -> Response:
    r = db.get(Role, role_id)
    if r is None:
        raise HTTPException(404, "Role not found")
    if r.builtin:
        raise HTTPException(409, "Built-in roles can't be deleted (their permissions can be changed)")
    out = role_out(db, r)
    if out.users or out.car_grants:
        raise HTTPException(409, f"'{r.name}' is used by {out.users} user(s) and {out.car_grants} car access(es): change them first")
    db.delete(r)
    audit.record(db, request, "role.deleted", target=f"role:{role_id}", details={"name": r.name})
    db.commit()
    return Response(status_code=204)
