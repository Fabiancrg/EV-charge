"""Home charging tariffs (price per kWh per period).

There is one set of **default** tariffs, managed by administrators (tariffs:manage). A group uses them, or
switches to **its own** tariffs, which its managers then manage. Every endpoint works on one set, chosen with
`?group_id=`: none is the default set for administrators, and one's own group for everyone else."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import creg
from ..auth import Principal, car_scope, current_user
from ..config import CONFIG
from ..database import get_db
from ..models import ChargeSession, Group, Tariff
from ..pricing import Pricing, get_setting, set_setting
from ..schemas import CregApply, CregRegion, TariffIn, TariffMode, TariffOut

router = APIRouter(prefix="/api/tariffs", tags=["tariffs"])


# ── Which set of tariffs ─────────────────────────────────────────────────────


def _group(db: Session, user: Principal, group_id: int | None) -> Group | None:
    """The group whose tariffs are meant; None: the default tariffs. Without group_id, administrators mean the
    default tariffs and everyone else their own group."""
    if group_id is None:
        if user.has("tariffs:manage") or user.group_id is None:
            return None
        group_id = user.group_id
    group = db.get(Group, group_id)
    if group is None or not (user.has("tariffs:manage") or user.group_id == group.id):
        raise HTTPException(404, "Group not found")
    return group


def _may_edit(user: Principal, tariff_set: int | None) -> bool:
    return user.manages_group(tariff_set, "tariffs:manage") if tariff_set is not None else user.has("tariffs:manage")


def _editable_set(db: Session, user: Principal, group_id: int | None) -> int | None:
    """The set to change: the default tariffs (administrators), or the own tariffs of a group (its managers too)."""
    group = _group(db, user, group_id)
    tariff_set = group.id if group else None
    if not _may_edit(user, tariff_set):
        raise HTTPException(403, "Only an administrator changes the default tariffs" if group is None else "Only a manager of the group changes its tariffs")
    if group is not None and not group.own_tariffs:
        raise HTTPException(409, f"'{group.name}' uses the default tariffs: switch it to its own tariffs first")
    return tariff_set


def _in_set(db: Session, tariff_set: int | None) -> list[Tariff]:
    return list(db.scalars(select(Tariff).where(Tariff.group_id == tariff_set if tariff_set is not None else Tariff.group_id.is_(None))))


def _overlap(db: Session, body: TariffIn, tariff_set: int | None, exclude_id: int | None = None) -> Tariff | None:
    for t in _in_set(db, tariff_set):
        if t.id == exclude_id:
            continue
        starts_before_end = body.valid_to is None or t.valid_from <= body.valid_to
        ends_after_start = t.valid_to is None or t.valid_to >= body.valid_from
        if starts_before_end and ends_after_start:
            return t
    return None


def _check(db: Session, body: TariffIn, tariff_set: int | None, exclude_id: int | None = None) -> None:
    other = _overlap(db, body, tariff_set, exclude_id)
    if other:
        until = other.valid_to.isoformat() if other.valid_to else "open-ended"
        raise HTTPException(409, f"Overlaps the tariff {other.label or ''} ({other.valid_from.isoformat()} → {until}): adjust its dates first".replace("  ", " "))


def _mode_out(db: Session, user: Principal, group: Group | None) -> dict[str, Any]:
    tariff_set = group.id if group and group.own_tariffs else None
    return {
        "group_id": group.id if group else None,
        "group_name": group.name if group else None,
        "own_tariffs": bool(group and group.own_tariffs),
        "own_count": len(_in_set(db, group.id)) if group else 0,  # its own tariffs, kept while it uses the default ones
        "can_switch": group is not None and user.manages_group(group.id, "tariffs:manage"),
        "can_edit": _may_edit(user, tariff_set) and (group is None or group.own_tariffs),
        "creg_region": (group.creg_region if tariff_set is not None else None) or get_setting(db, "creg_region", "wallonia"),
    }


@router.get("/mode")
def get_mode(group_id: int | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Which tariffs a group uses (the default ones or its own), and what the caller may do about it."""
    return _mode_out(db, user, _group(db, user, group_id))


@router.put("/mode")
def set_mode(body: TariffMode, group_id: int | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Switch a group between the default tariffs and its own. Its own tariffs are kept when it goes back to the
    default ones; switching to its own with none yet can start from a copy of the default tariffs."""
    group = _group(db, user, group_id)
    if group is None:
        raise HTTPException(422, "Choose a group: the default tariffs have no mode")
    if not user.manages_group(group.id, "tariffs:manage"):
        raise HTTPException(403, "Only a manager of the group chooses its tariffs")
    if body.own_tariffs and not group.own_tariffs and body.copy_default and not _in_set(db, group.id):
        for t in _in_set(db, None):
            db.add(Tariff(group_id=group.id, valid_from=t.valid_from, valid_to=t.valid_to, price_per_kwh=t.price_per_kwh, label=t.label, notes=t.notes))
    group.own_tariffs = body.own_tariffs
    db.commit()
    return _mode_out(db, user, group)


@router.get("", response_model=list[TariffOut])
def list_tariffs(group_id: int | None = None, own: bool = False, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[TariffOut]:
    """The tariffs in use: the default ones, or those of the group when it has its own (own=true: its own tariffs
    even while it uses the default ones). Each with the sessions the caller may see that it prices."""
    group = _group(db, user, group_id)
    tariff_set = group.id if group and (group.own_tariffs or own) else None
    pricing = Pricing(db)
    scope = car_scope(user)
    usage: dict[int, list[float]] = {}
    for s in db.scalars(select(ChargeSession)):
        if pricing.is_home(s) and (scope is None or s.car_id in scope):
            t = pricing.tariff_on(s.charge_date, pricing.set_of(s))
            if t:
                usage.setdefault(t.id, []).append(s.kwh)
    out = []
    for t in sorted(pricing.sets.get(tariff_set, []), key=lambda t: t.valid_from, reverse=True):
        o = TariffOut.model_validate(t)
        o.sessions = len(usage.get(t.id, []))
        o.kwh = round(sum(usage.get(t.id, [])), 3)
        out.append(o)
    return out


@router.post("", response_model=TariffOut, status_code=201)
def create_tariff(body: TariffIn, group_id: int | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> TariffOut:
    tariff_set = _editable_set(db, user, group_id)
    _check(db, body, tariff_set)
    t = Tariff(**body.model_dump(), group_id=tariff_set)
    db.add(t)
    db.commit()
    return TariffOut.model_validate(t)


def _own(db: Session, user: Principal, tariff_id: int) -> Tariff:
    """A tariff the caller may change: a default one for administrators, one of their group for its managers."""
    t = db.get(Tariff, tariff_id)
    if t is None or (t.group_id is not None and not (user.has("tariffs:manage") or user.group_id == t.group_id)):
        raise HTTPException(404, "Tariff not found")
    if not _may_edit(user, t.group_id):
        raise HTTPException(403, "Only an administrator changes the default tariffs" if t.group_id is None else "Only a manager of the group changes its tariffs")
    return t


@router.put("/{tariff_id:int}", response_model=TariffOut)
def update_tariff(tariff_id: int, body: TariffIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> TariffOut:
    t = _own(db, user, tariff_id)
    _check(db, body, t.group_id, exclude_id=tariff_id)
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    db.commit()
    return TariffOut.model_validate(t)


@router.delete("/{tariff_id:int}", status_code=204)
def delete_tariff(tariff_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    db.delete(_own(db, user, tariff_id))
    db.commit()
    return Response(status_code=204)


# ── Import from the CREG ─────────────────────────────────────────────────────


def _region(db: Session, tariff_set: int | None) -> str:
    """The region of the last CREG import into the set (the group's, else the installation's)."""
    group = db.get(Group, tariff_set) if tariff_set is not None else None
    return (group.creg_region if group else None) or get_setting(db, "creg_region", "wallonia")


def _creg_label(key: str, region: str) -> str:
    year, quarter = key.split("-")
    return f"CREG {quarter} {year} ({creg.REGIONS[region]})"


def _creg_preview(db: Session, quarters: list[creg.QuarterRate], region: str, source: str, tariff_set: int | None) -> dict[str, Any]:
    """Compare the CREG quarters with the tariffs already stored in the set: new / same / changed / conflict."""
    tariffs = _in_set(db, tariff_set)
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
async def creg_preview(region: CregRegion | None = None, group_id: int | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Fetch the CREG page and compare its quarterly tariffs for a region with the stored ones (nothing is saved)."""
    tariff_set = _editable_set(db, user, group_id)
    region = region or _region(db, tariff_set)
    try:
        html = await asyncio.to_thread(creg.fetch_page, CONFIG.creg_url)
        quarters = creg.parse_creg(html)
    except creg.CregError as e:
        raise HTTPException(502, str(e)) from None
    return _creg_preview(db, quarters, region, CONFIG.creg_url, tariff_set)


@router.post("/creg/upload")
async def creg_preview_upload(
    region: CregRegion = Form(...), file: UploadFile = File(...), group_id: int | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Same preview from the CREG page saved in a browser (when this server can't reach the internet)."""
    tariff_set = _editable_set(db, user, group_id)
    raw = await file.read(creg.MAX_PAGE_BYTES + 1)
    if len(raw) > creg.MAX_PAGE_BYTES:
        raise HTTPException(422, "The file is too large for the CREG page")
    try:
        quarters = creg.parse_creg(raw.decode("utf-8", errors="replace"))
    except creg.CregError as e:
        raise HTTPException(422, str(e)) from None
    return _creg_preview(db, quarters, region, f"uploaded file {file.filename or ''}".strip(), tariff_set)


@router.post("/creg/apply")
def creg_apply(body: CregApply, group_id: int | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create the selected quarters, or update the price of a quarter already stored with the same dates."""
    tariff_set = _editable_set(db, user, group_id)
    created, updated, skipped = 0, 0, []
    note = f"Imported from the CREG ({body.source or CONFIG.creg_url}) on {datetime.now(UTC).date().isoformat()}"
    for item in sorted(body.items, key=lambda i: i.valid_from):
        if item.valid_to < item.valid_from:
            raise HTTPException(422, f"{item.key}: the period ends before it starts")
        exact = next((t for t in _in_set(db, tariff_set) if t.valid_from == item.valid_from and t.valid_to == item.valid_to), None)
        if exact is not None:
            exact.price_per_kwh = item.price_per_kwh
            exact.label = _creg_label(item.key, body.region)
            exact.notes = note
            updated += 1
            continue
        if _overlap(db, TariffIn(valid_from=item.valid_from, valid_to=item.valid_to, price_per_kwh=item.price_per_kwh), tariff_set):
            skipped.append(item.key)
            continue
        db.add(
            Tariff(group_id=tariff_set, valid_from=item.valid_from, valid_to=item.valid_to, price_per_kwh=item.price_per_kwh, label=_creg_label(item.key, body.region), notes=note)
        )
        db.flush()
        created += 1
    if tariff_set is None:
        set_setting(db, "creg_region", body.region)
    else:
        db.get(Group, tariff_set).creg_region = body.region  # type: ignore[union-attr]
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped}
