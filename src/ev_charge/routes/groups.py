"""Groups API: who shares cars and chargers (a household, a team).

Administrators (groups:manage) create groups, set how many members each may have, and name their managers
on the Users page. Everyone else sees their own group."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..auth import Principal, current_user, require
from ..database import get_db
from ..models import Car, Charger, Group, User
from ..schemas import GroupIn, GroupOut

router = APIRouter(prefix="/api/groups", tags=["groups"])


def group_out(db: Session, g: Group) -> GroupOut:
    members = [u for u in db.scalars(select(User).where(User.group_id == g.id, User.active.is_(True)).order_by(User.username))]
    return GroupOut(
        id=g.id, name=g.name, max_members=g.max_members, notes=g.notes, members=len(members),
        managers=[u.full_name or u.username for u in members if u.group_manager],
        cars=db.scalar(select(func.count()).select_from(Car).where(Car.group_id == g.id)) or 0,
        chargers=db.scalar(select(func.count()).select_from(Charger).where(Charger.group_id == g.id)) or 0,
    )  # fmt: skip


def _get(db: Session, group_id: int) -> Group:
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(404, "Group not found")
    return g


def _apply(db: Session, g: Group, body: GroupIn) -> None:
    if not body.name:
        raise HTTPException(422, "A name is required")
    if db.scalar(select(Group.id).where(func.lower(Group.name) == body.name.lower(), Group.id != (g.id or -1))):
        raise HTTPException(409, f"A group named '{body.name}' already exists")
    g.name, g.max_members, g.notes = body.name, body.max_members, body.notes


@router.get("", response_model=list[GroupOut])
def list_groups(me: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[GroupOut]:
    """Every group for administrators; one's own group otherwise."""
    query = select(Group).order_by(Group.name)
    if not me.has("groups:manage"):
        query = query.where(Group.id == (me.group_id if me.group_id is not None else -1))
    return [group_out(db, g) for g in db.scalars(query)]


@router.post("", response_model=GroupOut, status_code=201)
def create_group(body: GroupIn, request: Request, _: Principal = Depends(require("groups:manage")), db: Session = Depends(get_db)) -> GroupOut:
    g = Group()
    _apply(db, g, body)
    db.add(g)
    db.flush()
    audit.record(db, request, "group.created", target=f"group:{g.id}", details={"name": g.name, "max_members": g.max_members})
    db.commit()
    return group_out(db, g)


@router.put("/{group_id}", response_model=GroupOut)
def update_group(group_id: int, body: GroupIn, request: Request, _: Principal = Depends(require("groups:manage")), db: Session = Depends(get_db)) -> GroupOut:
    g = _get(db, group_id)
    before = {"name": g.name, "max_members": g.max_members}
    _apply(db, g, body)
    after = {"name": g.name, "max_members": g.max_members}
    audit.record(db, request, "group.updated", target=f"group:{g.id}", details={k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]} | {"name": g.name})
    db.commit()
    return group_out(db, g)


@router.delete("/{group_id}", status_code=204)
def delete_group(group_id: int, request: Request, _: Principal = Depends(require("groups:manage")), db: Session = Depends(get_db)) -> Response:
    g = _get(db, group_id)
    out = group_out(db, g)
    members = db.scalar(select(func.count()).select_from(User).where(User.group_id == g.id)) or 0
    if members or out.cars or out.chargers:
        raise HTTPException(409, f"'{g.name}' still has {members} user(s), {out.cars} car(s) and {out.chargers} charger(s): move or delete them first")
    db.delete(g)
    audit.record(db, request, "group.deleted", target=f"group:{group_id}", details={"name": out.name})
    db.commit()
    return Response(status_code=204)
