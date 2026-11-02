"""Home charging tariffs (price per kWh per period) and the locations charged at that tariff."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..database import get_db
from ..models import ChargeSession, Tariff, User
from ..pricing import Pricing, home_locations, set_setting
from ..schemas import HomeLocations, TariffIn, TariffOut

router = APIRouter(prefix="/api/tariffs", tags=["tariffs"])


def _overlap(db: Session, body: TariffIn, exclude_id: int | None = None) -> Tariff | None:
    for t in db.scalars(select(Tariff)):
        if t.id == exclude_id:
            continue
        starts_before_end = body.valid_to is None or t.valid_from <= body.valid_to
        ends_after_start = t.valid_to is None or t.valid_to >= body.valid_from
        if starts_before_end and ends_after_start:
            return t
    return None


def _check(db: Session, body: TariffIn, exclude_id: int | None = None) -> None:
    other = _overlap(db, body, exclude_id)
    if other:
        until = other.valid_to.isoformat() if other.valid_to else "open-ended"
        raise HTTPException(409, f"Overlaps the tariff {other.label or ''} ({other.valid_from.isoformat()} → {until}): adjust its dates first".replace("  ", " "))


@router.get("", response_model=list[TariffOut])
def list_tariffs(_: User = Depends(current_user), db: Session = Depends(get_db)) -> list[TariffOut]:
    pricing = Pricing(db)
    usage: dict[int, list[float]] = {}
    for s in db.scalars(select(ChargeSession)):
        if pricing.is_home(s.location):
            t = pricing.tariff_on(s.charge_date)
            if t:
                usage.setdefault(t.id, []).append(s.kwh)
    out = []
    for t in sorted(pricing.tariffs, key=lambda t: t.valid_from, reverse=True):
        o = TariffOut.model_validate(t)
        o.sessions = len(usage.get(t.id, []))
        o.kwh = round(sum(usage.get(t.id, [])), 2)
        out.append(o)
    return out


@router.post("", response_model=TariffOut, status_code=201)
def create_tariff(body: TariffIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> TariffOut:
    _check(db, body)
    t = Tariff(**body.model_dump())
    db.add(t)
    db.commit()
    return TariffOut.model_validate(t)


@router.put("/{tariff_id:int}", response_model=TariffOut)
def update_tariff(tariff_id: int, body: TariffIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> TariffOut:
    t = db.get(Tariff, tariff_id)
    if t is None:
        raise HTTPException(404, "Tariff not found")
    _check(db, body, exclude_id=tariff_id)
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    db.commit()
    return TariffOut.model_validate(t)


@router.delete("/{tariff_id:int}", status_code=204)
def delete_tariff(tariff_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> Response:
    t = db.get(Tariff, tariff_id)
    if t is None:
        raise HTTPException(404, "Tariff not found")
    db.delete(t)
    db.commit()
    return Response(status_code=204)


@router.get("/home-locations", response_model=HomeLocations)
def get_home_locations(_: User = Depends(current_user), db: Session = Depends(get_db)) -> HomeLocations:
    return HomeLocations(locations=home_locations(db))


@router.put("/home-locations", response_model=HomeLocations)
def put_home_locations(body: HomeLocations, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> HomeLocations:
    seen: dict[str, str] = {}
    for loc in body.locations:
        loc = " ".join(loc.split())[:200]
        if loc:
            seen.setdefault(loc.casefold(), loc)
    set_setting(db, "home_locations", list(seen.values()))
    db.commit()
    return HomeLocations(locations=list(seen.values()))
