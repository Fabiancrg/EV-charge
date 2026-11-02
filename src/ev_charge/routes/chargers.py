"""Chargers whose sessions are reimbursed (administrators manage them; everyone can pick one in a session)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import car_scope, current_user, require_admin
from ..database import get_db
from ..models import Charger, ChargeSession, User
from ..schemas import ChargerIn, ChargerOut

router = APIRouter(prefix="/api/chargers", tags=["chargers"])


def _get(db: Session, charger_id: int) -> Charger:
    c = db.get(Charger, charger_id)
    if c is None:
        raise HTTPException(404, "Charger not found")
    return c


def _save(db: Session, c: Charger) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A charger named '{c.name}' already exists") from None


@router.get("", response_model=list[ChargerOut])
def list_chargers(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[ChargerOut]:
    scope = car_scope(user)  # usage figures cover the sessions the user can see
    usage: dict[int, list[ChargeSession]] = {}
    for s in db.scalars(select(ChargeSession).where(ChargeSession.charger_id.is_not(None))):
        if scope is None or s.car_id in scope:
            usage.setdefault(s.charger_id, []).append(s)  # type: ignore[arg-type]
    out = []
    for c in db.scalars(select(Charger).order_by(Charger.active.desc(), Charger.name)):
        o = ChargerOut.model_validate(c)
        used = usage.get(c.id, [])
        o.sessions = len(used)
        o.kwh = round(sum(s.kwh for s in used), 2)
        o.last_used = max((s.charge_date for s in used), default=None)
        out.append(o)
    return out


@router.post("", response_model=ChargerOut, status_code=201)
def create_charger(body: ChargerIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> ChargerOut:
    c = Charger(**body.model_dump())
    db.add(c)
    _save(db, c)
    return ChargerOut.model_validate(c)


@router.put("/{charger_id}", response_model=ChargerOut)
def update_charger(charger_id: int, body: ChargerIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> ChargerOut:
    c = _get(db, charger_id)
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    _save(db, c)
    return ChargerOut.model_validate(c)


@router.delete("/{charger_id}", status_code=204)
def delete_charger(charger_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> Response:
    c = _get(db, charger_id)
    count = db.scalar(select(func.count()).select_from(ChargeSession).where(ChargeSession.charger_id == charger_id)) or 0
    if count:
        raise HTTPException(409, f"'{c.name}' was used for {count} session(s): mark it inactive instead")
    db.delete(c)
    db.commit()
    return Response(status_code=204)
