"""Charging sessions API: CRUD, filters, CSV export, form suggestions."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from ..auth import Principal, car_scope, check_car_access, current_user, restrict_car_ids
from ..database import get_db
from ..models import Car, Charger, ChargeSession
from ..pricing import Pricing
from ..schemas import ReimbursementStatus, SessionBulk, SessionIn, SessionOut, default_status
from ..services import km_since_previous, session_out, session_sort_key

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _get(db: Session, session_id: int, user: Principal, permission: str = "car:view") -> ChargeSession:
    s = db.get(ChargeSession, session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    if not user.can("car:view", s.car_id):
        raise HTTPException(404, "Session not found")
    check_car_access(user, s.car_id, permission)
    return s


def _check_car(db: Session, car_id: int, user: Principal, permission: str) -> None:
    if db.get(Car, car_id) is None:
        raise HTTPException(422, "Unknown car")
    check_car_access(user, car_id, permission)


def _check_charger(db: Session, charger_id: int | None, car_id: int, previous: int | None = None) -> None:
    """previous: the charger the session already has (kept as it is, even inactive)."""
    if charger_id is None or charger_id == previous:
        return
    c = db.get(Charger, charger_id)
    car = db.get(Car, car_id)
    if c is None or car is None or c.group_id != car.group_id:  # a charger of another group doesn't exist for this car
        raise HTTPException(422, "Unknown charger")
    if not c.active:
        raise HTTPException(422, f"The charger '{c.name}' is inactive")


@dataclass
class Filters:
    car_id: list[int] | None
    date_from: date | None
    date_to: date | None
    to_reimburse: bool | None
    status: list[ReimbursementStatus] | None
    charger_id: list[int] | None
    q: str | None


def filters(
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    to_reimburse: bool | None = None,
    status: list[ReimbursementStatus] | None = Query(default=None, description="Reimbursement status (repeatable)"),
    charger_id: list[int] | None = Query(default=None, description="Charger (repeatable); 0 = no charger"),
    q: str | None = None,
) -> Filters:
    return Filters(car_id, date_from, date_to, to_reimburse, status, charger_id, q)


def _filtered(db: Session, user: Principal, f: Filters) -> list[SessionOut]:
    all_sessions = db.scalars(select(ChargeSession).options(joinedload(ChargeSession.car))).all()
    km_prev = km_since_previous(all_sessions)  # over the whole history, not just the filtered rows
    query = select(ChargeSession).options(joinedload(ChargeSession.car))
    car_id = restrict_car_ids(user, f.car_id)
    if car_id:
        query = query.where(ChargeSession.car_id.in_(car_id))
    if f.date_from:
        query = query.where(ChargeSession.charge_date >= f.date_from)
    if f.date_to:
        query = query.where(ChargeSession.charge_date <= f.date_to)
    if f.to_reimburse is not None:
        query = query.where(ChargeSession.to_reimburse.is_(f.to_reimburse))
    if f.status:
        query = query.where(ChargeSession.reimbursement_status.in_(f.status))
    if f.charger_id:
        ids = [c for c in f.charger_id if c]
        cond = ChargeSession.charger_id.in_(ids) if ids else None
        if 0 in f.charger_id:
            cond = ChargeSession.charger_id.is_(None) if cond is None else or_(cond, ChargeSession.charger_id.is_(None))
        query = query.where(cond)
    q = f.q
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(ChargeSession.location.ilike(like), ChargeSession.provider.ilike(like), ChargeSession.notes.ilike(like)))
    rows = sorted(db.scalars(query).all(), key=session_sort_key, reverse=True)
    pricing = Pricing(db)
    return [session_out(s, pricing, km_prev) for s in rows]


@router.get("", response_model=list[SessionOut])
def list_sessions(
    f: Filters = Depends(filters),
    limit: int | None = Query(default=None, ge=1),
    user: Principal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[SessionOut]:
    rows = _filtered(db, user, f)
    return rows[:limit] if limit else rows


@router.get("/export.csv")
def export_csv(f: Filters = Depends(filters), user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    rows = _filtered(db, user, f)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")  # Excel in most European locales
    writer.writerow([
        "date", "car", "odometer_km", "km_since_previous", "location", "provider", "kwh", "start", "end", "duration_min", "avg_kw",
        "charger", "reimbursement_company", "cost", "price_per_kwh", "home_tariff", "to_reimburse", "reimbursement_status", "notes", "entered_by",
    ])  # fmt: skip
    for r in rows:
        writer.writerow([
            r.charge_date.isoformat(), r.car_name, r.odometer, r.km_since_previous, r.location, r.provider, r.kwh,
            r.start_time.strftime("%H:%M") if r.start_time else "", r.end_time.strftime("%H:%M") if r.end_time else "",
            r.duration_minutes, r.avg_power_kw, r.charger_name, r.reimbursement_company, r.cost, r.price_per_kwh, "yes" if r.cost_source == "tariff" else "",
            "yes" if r.to_reimburse else "no", (r.reimbursement_status or "").replace("_", " "), r.notes, r.created_by,
        ])  # fmt: skip
    return Response(
        content="﻿" + buf.getvalue(),  # BOM so Excel reads UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="charging-sessions-{date.today().isoformat()}.csv"'},
    )


@router.get("/suggestions")
def suggestions(car_id: int | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Values that pre-fill the session form: known locations/providers, the car's last reading,
    and per location the provider, charger and reimbursement flag used last time."""
    scope = car_scope(user)
    rows = sorted((s for s in db.scalars(select(ChargeSession)) if scope is None or s.car_id in scope), key=session_sort_key)
    by_location: dict[str, dict] = {}
    for s in rows:
        if s.location:
            by_location[s.location] = {"provider": s.provider, "to_reimburse": s.to_reimburse, "charger_id": s.charger_id}
    car_rows = [s for s in rows if s.car_id == car_id] if car_id else []
    last_odo = next((s for s in reversed(car_rows) if s.odometer is not None), None)
    mine = [s for s in rows if s.created_by_id == user.id]
    last_car = max(mine or rows, key=lambda s: s.created_at, default=None)
    return {
        "locations": sorted({s.location for s in rows if s.location}, key=str.casefold),
        "providers": sorted({s.provider for s in rows if s.provider}, key=str.casefold),
        "by_location": by_location,
        "last_odometer": last_odo.odometer if last_odo else None,
        "last_odometer_date": last_odo.charge_date.isoformat() if last_odo else None,
        "last_used_car_id": last_car.car_id if last_car else None,
    }


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> SessionOut:
    return session_out(_get(db, session_id, user), Pricing(db))


@router.post("", response_model=SessionOut, status_code=201)
def create_session(body: SessionIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> SessionOut:
    _check_car(db, body.car_id, user, "sessions:create")
    _check_charger(db, body.charger_id, body.car_id)
    values = body.model_dump()
    if not user.can("reimbursement:manage", body.car_id):  # the reimbursement follows the defaults
        values["to_reimburse"] = body.charger_id is not None
        values["reimbursement_status"] = default_status(values["to_reimburse"], None, body.charger_id)
    s = ChargeSession(**values, created_by_id=user.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return session_out(s, Pricing(db))


@router.put("/{session_id}", response_model=SessionOut)
def update_session(session_id: int, body: SessionIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> SessionOut:
    s = _get(db, session_id, user, "sessions:edit")
    if body.car_id != s.car_id:
        _check_car(db, body.car_id, user, "sessions:create")
    _check_charger(db, body.charger_id, body.car_id, previous=s.charger_id if body.car_id == s.car_id else None)
    values = body.model_dump()
    if not (user.can("reimbursement:manage", s.car_id) and user.can("reimbursement:manage", body.car_id)):
        values["to_reimburse"], values["reimbursement_status"] = s.to_reimburse, s.reimbursement_status  # unchanged
    for key, value in values.items():
        setattr(s, key, value)
    db.commit()
    db.refresh(s)
    return session_out(s, Pricing(db))


@router.patch("/bulk")
def bulk_update(body: SessionBulk, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Change the reimbursement flag, reimbursement status, charger and/or provider of the selected sessions."""
    charger = None
    if body.charger_id is not None:
        charger = db.get(Charger, body.charger_id)
        if charger is None:
            raise HTTPException(422, "Unknown charger")
        if not charger.active:
            raise HTTPException(422, f"The charger '{charger.name}' is inactive")
    needs = []
    if body.to_reimburse is not None or body.reimbursement_status is not None:
        needs.append("reimbursement:manage")
    if body.charger_id is not None or body.clear_charger or body.provider is not None or body.clear_provider:
        needs.append("sessions:edit")
    rows = [s for s in db.scalars(select(ChargeSession).where(ChargeSession.id.in_(body.ids))) if all(user.can(p, s.car_id) for p in needs or ["car:view"])]
    if charger is not None:  # a charger is set on the sessions of the cars of its group only
        rows = [s for s in rows if s.car.group_id == charger.group_id]
    for s in rows:
        if body.clear_charger:
            s.charger_id = None
        elif body.charger_id is not None:
            s.charger_id = body.charger_id
            if body.to_reimburse is None:
                s.to_reimburse = True  # a charger's sessions are reimbursed
            if not s.provider and charger is not None and charger.default_provider:
                s.provider = charger.default_provider  # fill an empty provider with the charger's default
        if body.clear_provider:
            s.provider = None
        elif body.provider is not None:
            s.provider = body.provider
        if body.to_reimburse is not None:
            s.to_reimburse = body.to_reimburse
        status = body.reimbursement_status if body.reimbursement_status and s.to_reimburse else s.reimbursement_status
        s.reimbursement_status = default_status(s.to_reimburse, status, s.charger_id)
    db.commit()
    return {"updated": len(rows), "skipped": len(body.ids) - len(rows)}


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    db.delete(_get(db, session_id, user, "sessions:edit"))
    db.commit()
    return Response(status_code=204)
