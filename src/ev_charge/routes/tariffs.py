"""Home charging tariffs (price per kWh per period) and the locations charged at that tariff."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import creg
from ..auth import current_user, require_admin
from ..config import CONFIG
from ..database import get_db
from ..models import ChargeSession, Tariff, User
from ..pricing import Pricing, get_setting, set_setting
from ..schemas import CregApply, CregRegion, TariffIn, TariffOut

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
        if pricing.is_home(s):
            t = pricing.tariff_on(s.charge_date)
            if t:
                usage.setdefault(t.id, []).append(s.kwh)
    out = []
    for t in sorted(pricing.tariffs, key=lambda t: t.valid_from, reverse=True):
        o = TariffOut.model_validate(t)
        o.sessions = len(usage.get(t.id, []))
        o.kwh = round(sum(usage.get(t.id, [])), 3)
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


# ── Import from the CREG ─────────────────────────────────────────────────────


def _creg_label(key: str, region: str) -> str:
    year, quarter = key.split("-")
    return f"CREG {quarter} {year} ({creg.REGIONS[region]})"


def _creg_preview(db: Session, quarters: list[creg.QuarterRate], region: str, source: str) -> dict[str, Any]:
    """Compare the CREG quarters with the tariffs already stored: new / same / changed / conflict."""
    tariffs = db.scalars(select(Tariff)).all()
    items = []
    for q in quarters:
        price = q.rates.get(region)
        if price is None:
            continue
        exact = next((t for t in tariffs if t.valid_from == q.valid_from and t.valid_to == q.valid_to), None)
        overlap = next((t for t in tariffs if t.valid_from <= q.valid_to and (t.valid_to is None or t.valid_to >= q.valid_from)), None)
        if exact is not None:
            status = "same" if round(exact.price_per_kwh, 6) == round(price, 6) else "changed"
            other = exact
        elif overlap is not None:
            status, other = "conflict", overlap
        else:
            status, other = "new", None
        items.append({
            "key": q.key, "label": _creg_label(q.key, region), "valid_from": q.valid_from.isoformat(), "valid_to": q.valid_to.isoformat(),
            "price_per_kwh": price, "status": status,
            "current": {"id": other.id, "valid_from": other.valid_from.isoformat(), "valid_to": other.valid_to.isoformat() if other.valid_to else None,
                        "price_per_kwh": other.price_per_kwh, "label": other.label} if other else None,
        })  # fmt: skip
    return {
        "source": source,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "region": region,
        "regions": creg.REGIONS,
        "items": items,
    }


@router.get("/creg")
async def creg_preview(region: CregRegion | None = None, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Fetch the CREG page and compare its quarterly tariffs for a region with the stored ones (nothing is saved)."""
    region = region or get_setting(db, "creg_region", "wallonia")
    try:
        html = await asyncio.to_thread(creg.fetch_page, CONFIG.creg_url)
        quarters = creg.parse_creg(html)
    except creg.CregError as e:
        raise HTTPException(502, str(e)) from None
    return _creg_preview(db, quarters, region, CONFIG.creg_url)


@router.post("/creg/upload")
async def creg_preview_upload(region: CregRegion = Form(...), file: UploadFile = File(...), _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Same preview from the CREG page saved in a browser (when this server can't reach the internet)."""
    raw = await file.read(creg.MAX_PAGE_BYTES + 1)
    if len(raw) > creg.MAX_PAGE_BYTES:
        raise HTTPException(422, "The file is too large for the CREG page")
    try:
        quarters = creg.parse_creg(raw.decode("utf-8", errors="replace"))
    except creg.CregError as e:
        raise HTTPException(422, str(e)) from None
    return _creg_preview(db, quarters, region, f"uploaded file {file.filename or ''}".strip())


@router.post("/creg/apply")
def creg_apply(body: CregApply, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create the selected quarters, or update the price of a quarter already stored with the same dates."""
    created, updated, skipped = 0, 0, []
    note = f"Imported from the CREG ({body.source or CONFIG.creg_url}) on {datetime.now(UTC).date().isoformat()}"
    for item in sorted(body.items, key=lambda i: i.valid_from):
        if item.valid_to < item.valid_from:
            raise HTTPException(422, f"{item.key}: the period ends before it starts")
        exact = db.scalar(select(Tariff).where(Tariff.valid_from == item.valid_from, Tariff.valid_to == item.valid_to))
        if exact is not None:
            exact.price_per_kwh = item.price_per_kwh
            exact.label = _creg_label(item.key, body.region)
            exact.notes = note
            updated += 1
            continue
        if _overlap(db, TariffIn(valid_from=item.valid_from, valid_to=item.valid_to, price_per_kwh=item.price_per_kwh)):
            skipped.append(item.key)
            continue
        db.add(Tariff(valid_from=item.valid_from, valid_to=item.valid_to, price_per_kwh=item.price_per_kwh, label=_creg_label(item.key, body.region), notes=note))
        db.flush()
        created += 1
    set_setting(db, "creg_region", body.region)
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped}
