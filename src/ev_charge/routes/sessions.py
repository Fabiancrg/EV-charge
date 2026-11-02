"""Charging sessions API: CRUD, filters, CSV export, form suggestions."""

from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from ..auth import car_scope, check_car_access, current_user, restrict_car_ids
from ..database import get_db
from ..models import Car, ChargeSession, User
from ..pricing import Pricing
from ..schemas import SessionIn, SessionOut, SessionPatch
from ..services import km_since_previous, session_out, session_sort_key

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _get(db: Session, session_id: int, user: User) -> ChargeSession:
    s = db.get(ChargeSession, session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    scope = car_scope(user)
    if scope is not None and s.car_id not in scope:
        raise HTTPException(404, "Session not found")
    return s


def _check_car(db: Session, car_id: int, user: User) -> None:
    if db.get(Car, car_id) is None:
        raise HTTPException(422, "Unknown car")
    check_car_access(user, car_id)


def _filtered(
    db: Session,
    user: User,
    car_id: list[int] | None,
    date_from: date | None,
    date_to: date | None,
    to_reimburse: bool | None,
    q: str | None,
) -> list[SessionOut]:
    all_sessions = db.scalars(select(ChargeSession).options(joinedload(ChargeSession.car))).all()
    km_prev = km_since_previous(all_sessions)  # over the whole history, not just the filtered rows
    query = select(ChargeSession).options(joinedload(ChargeSession.car))
    car_id = restrict_car_ids(user, car_id)
    if car_id:
        query = query.where(ChargeSession.car_id.in_(car_id))
    if date_from:
        query = query.where(ChargeSession.charge_date >= date_from)
    if date_to:
        query = query.where(ChargeSession.charge_date <= date_to)
    if to_reimburse is not None:
        query = query.where(ChargeSession.to_reimburse.is_(to_reimburse))
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(ChargeSession.location.ilike(like), ChargeSession.provider.ilike(like), ChargeSession.notes.ilike(like)))
    rows = sorted(db.scalars(query).all(), key=session_sort_key, reverse=True)
    pricing = Pricing(db)
    return [session_out(s, pricing, km_prev) for s in rows]


@router.get("", response_model=list[SessionOut])
def list_sessions(
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    to_reimburse: bool | None = None,
    q: str | None = None,
    limit: int | None = Query(default=None, ge=1),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[SessionOut]:
    rows = _filtered(db, user, car_id, date_from, date_to, to_reimburse, q)
    return rows[:limit] if limit else rows


@router.get("/export.csv")
def export_csv(
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    to_reimburse: bool | None = None,
    q: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    rows = _filtered(db, user, car_id, date_from, date_to, to_reimburse, q)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")  # Excel in most European locales
    writer.writerow(
        [
            "date",
            "car",
            "odometer_km",
            "km_since_previous",
            "location",
            "provider",
            "kwh",
            "start",
            "end",
            "duration_min",
            "avg_kw",
            "cost",
            "price_per_kwh",
            "to_reimburse",
            "notes",
        ]
    )
    for r in rows:
        writer.writerow([
            r.charge_date.isoformat(), r.car_name, r.odometer, r.km_since_previous, r.location, r.provider, r.kwh,
            r.start_time.strftime("%H:%M") if r.start_time else "", r.end_time.strftime("%H:%M") if r.end_time else "",
            r.duration_minutes, r.avg_power_kw, r.cost, r.price_per_kwh, "yes" if r.cost_source == "tariff" else "",
            "yes" if r.to_reimburse else "no", r.notes, r.created_by,
        ])  # fmt: skip
    return Response(
        content="﻿" + buf.getvalue(),  # BOM so Excel reads UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="charging-sessions-{date.today().isoformat()}.csv"'},
    )


@router.get("/suggestions")
def suggestions(car_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Values that pre-fill the session form: known locations/providers, the car's last reading,
    and per location the provider and reimbursement flag used last time."""
    scope = car_scope(user)
    rows = sorted((s for s in db.scalars(select(ChargeSession)) if scope is None or s.car_id in scope), key=session_sort_key)
    pricing = Pricing(db)
    by_location: dict[str, dict] = {}
    for s in rows:
        if s.location:
            by_location[s.location] = {"provider": s.provider, "to_reimburse": s.to_reimburse}
    car_rows = [s for s in rows if s.car_id == car_id] if car_id else []
    last_odo = next((s for s in reversed(car_rows) if s.odometer is not None), None)
    mine = [s for s in rows if s.created_by_id == user.id]
    last_car = max(mine or rows, key=lambda s: s.created_at, default=None)
    return {
        "locations": sorted({s.location for s in rows if s.location} | set(pricing.home_names), key=str.casefold),
        "home_locations": pricing.home_names,
        "providers": sorted({s.provider for s in rows if s.provider}, key=str.casefold),
        "by_location": by_location,
        "last_odometer": last_odo.odometer if last_odo else None,
        "last_odometer_date": last_odo.charge_date.isoformat() if last_odo else None,
        "last_used_car_id": last_car.car_id if last_car else None,
    }


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> SessionOut:
    return session_out(_get(db, session_id, user), Pricing(db))


@router.post("", response_model=SessionOut, status_code=201)
def create_session(body: SessionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> SessionOut:
    _check_car(db, body.car_id, user)
    s = ChargeSession(**body.model_dump(), created_by_id=user.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return session_out(s, Pricing(db))


@router.put("/{session_id}", response_model=SessionOut)
def update_session(session_id: int, body: SessionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> SessionOut:
    s = _get(db, session_id, user)
    _check_car(db, body.car_id, user)
    for key, value in body.model_dump().items():
        setattr(s, key, value)
    db.commit()
    db.refresh(s)
    return session_out(s, Pricing(db))


@router.patch("/reimburse")
def set_reimburse(body: SessionPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    scope = car_scope(user)
    rows = [s for s in db.scalars(select(ChargeSession).where(ChargeSession.id.in_(body.ids))) if scope is None or s.car_id in scope]
    for s in rows:
        s.to_reimburse = body.to_reimburse
    db.commit()
    return {"updated": len(rows)}


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    db.delete(_get(db, session_id, user))
    db.commit()
    return Response(status_code=204)
