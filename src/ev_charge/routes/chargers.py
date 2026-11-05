"""Chargers whose sessions are reimbursed (administrators manage them; everyone can pick one in a session),
their connection to the charger's cloud API, live status and history import."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import history as hist
from .. import live
from ..auth import Principal, car_scope, current_user, require
from ..connectors import CONNECTORS, ConnectorError
from ..database import get_db
from ..models import Car, Charger, ChargeSession
from ..schemas import ChargerConnection, ChargerIn, ChargerOut, HistoryApply
from ..secretbox import SecretError, decrypt_json, encrypt_json

router = APIRouter(prefix="/api/chargers", tags=["chargers"])
MAX_HISTORY_DAYS = 400


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


def _check_car(db: Session, car_id: int | None) -> None:
    if car_id is not None and db.get(Car, car_id) is None:
        raise HTTPException(422, "Unknown car")


def charger_out(c: Charger, user: Principal, usage: list[ChargeSession] | None = None) -> ChargerOut:
    o = ChargerOut.model_validate({**{k: getattr(c, k) for k in ChargerIn.model_fields}, "id": c.id})
    used = usage or []
    o.sessions = len(used)
    o.kwh = round(sum(s.kwh for s in used), 3)
    o.last_used = max((s.charge_date for s in used), default=None)
    o.connector = c.connector
    o.poll_enabled = c.poll_enabled
    o.external_id = c.external_id
    o.live = live.live_state(c) or None
    o.last_poll_at = c.last_poll_at
    o.last_poll_error = c.last_poll_error
    if user.has("chargers:manage") and c.connector and c.connector in CONNECTORS:
        try:
            config = decrypt_json(c.connector_secret)
        except SecretError:
            config = {}
        plain = [f["name"] for f in map(_field, CONNECTORS[c.connector].fields) if not f["secret"]]
        o.connector_settings = {k: config[k] for k in plain if config.get(k)}
        o.connector_account = config.get("email") or config.get("url")
    return o


def _visible(user: Principal, c: Charger) -> bool:
    """The live data of a charger is shown to who manages chargers, or may see its default car."""
    return user.has("chargers:manage") or (c.default_car_id is not None and user.can("car:view", c.default_car_id))


@router.get("", response_model=list[ChargerOut])
def list_chargers(user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[ChargerOut]:
    scope = car_scope(user)  # usage figures cover the sessions the user can see
    usage: dict[int, list[ChargeSession]] = {}
    for s in db.scalars(select(ChargeSession).where(ChargeSession.charger_id.is_not(None))):
        if scope is None or s.car_id in scope:
            usage.setdefault(s.charger_id, []).append(s)  # type: ignore[arg-type]
    out = []
    for c in db.scalars(select(Charger).order_by(Charger.active.desc(), Charger.name)):
        o = charger_out(c, user, usage.get(c.id))
        if not _visible(user, c):
            o.live = None
        out.append(o)
    return out


@router.get("/connectors")
def connector_types(_: Principal = Depends(current_user)) -> list[dict[str, Any]]:
    """Charger APIs EV Charge can connect to, and the credentials each needs."""
    return [{"id": k, "label": cls.label, "fields": [_field(f) for f in cls.fields]} for k, cls in CONNECTORS.items()]


def _field(f: tuple) -> dict[str, Any]:
    """(name, label, secret[, required]) → dict."""
    return {"name": f[0], "label": f[1], "secret": f[2], "required": f[3] if len(f) > 3 else True}


@router.post("", response_model=ChargerOut, status_code=201)
def create_charger(body: ChargerIn, user: Principal = Depends(require("chargers:manage")), db: Session = Depends(get_db)) -> ChargerOut:
    _check_car(db, body.default_car_id)
    c = Charger(**body.model_dump())
    db.add(c)
    _save(db, c)
    return charger_out(c, user)


@router.put("/{charger_id}", response_model=ChargerOut)
def update_charger(charger_id: int, body: ChargerIn, user: Principal = Depends(require("chargers:manage")), db: Session = Depends(get_db)) -> ChargerOut:
    c = _get(db, charger_id)
    _check_car(db, body.default_car_id)
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    _save(db, c)
    return charger_out(c, user)


@router.delete("/{charger_id}", status_code=204)
async def delete_charger(charger_id: int, _: Principal = Depends(require("chargers:manage")), db: Session = Depends(get_db)) -> Response:
    c = _get(db, charger_id)
    count = db.scalar(select(func.count()).select_from(ChargeSession).where(ChargeSession.charger_id == charger_id)) or 0
    if count:
        raise HTTPException(409, f"'{c.name}' was used for {count} session(s): mark it inactive instead")
    await live.forget_connector(c.id)
    db.delete(c)
    db.commit()
    return Response(status_code=204)


# ── Connection to the charger's API ──────────────────────────────────────────


@router.put("/{charger_id}/connection", response_model=ChargerOut)
async def set_connection(charger_id: int, body: ChargerConnection, user: Principal = Depends(require("chargers:manage")), db: Session = Depends(get_db)) -> ChargerOut:
    """Store (encrypted) or remove the credentials of the charger's cloud account, and test them."""
    c = _get(db, charger_id)
    await live.forget_connector(c.id)
    if body.connector is None:
        c.connector = c.connector_secret = c.external_id = c.live_state = c.last_poll_error = None
        c.poll_enabled = False
        db.commit()
        return charger_out(c, user)
    cls = CONNECTORS.get(body.connector)
    if cls is None:
        raise HTTPException(422, f"Unknown connector '{body.connector}'")
    try:
        stored = decrypt_json(c.connector_secret) if c.connector == body.connector else {}
    except SecretError:
        stored = {}
    fields = [_field(f) for f in cls.fields]
    # An empty secret keeps the stored one; an empty plain field clears it
    config = {f["name"]: (body.credentials.get(f["name"]) or "").strip() or (stored.get(f["name"], "") if f["secret"] else "") for f in fields}
    missing = [f["label"] for f in fields if f["required"] and not config.get(f["name"])]
    if missing:
        raise HTTPException(422, f"Missing: {', '.join(missing)}")
    try:
        connector = cls(config)
    except ConnectorError as e:
        raise HTTPException(422, str(e)) from None
    try:
        info = await connector.describe()  # log in once: wrong credentials are reported now
    except ConnectorError as e:
        raise HTTPException(422, str(e)) from None
    finally:
        await connector.close()
    c.connector = body.connector
    c.connector_secret = encrypt_json(config)
    c.poll_enabled = body.poll_enabled
    c.external_id = info.get("serial") or c.external_id
    if not c.serial_number and info.get("serial"):
        c.serial_number = info["serial"]
    if not c.model and info.get("model"):
        c.model = info["model"]
    c.last_poll_error = None
    db.commit()
    return charger_out(c, user)


@router.post("/{charger_id}/poll", response_model=ChargerOut)
async def poll_now(charger_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> ChargerOut:
    """Read the charger now (also done in the background every few minutes)."""
    c = _get(db, charger_id)
    if not _visible(user, c):
        raise HTTPException(403, "This charger is not linked to your car")
    if not c.connector:
        raise HTTPException(422, "This charger has no connection")
    await live.poll_charger(db, c)
    db.refresh(c)
    return charger_out(c, user)


# ── History import ───────────────────────────────────────────────────────────


async def _rebuilt(c: Charger, date_from: date, date_to: date) -> list[hist.Rebuilt]:
    if date_to < date_from:
        raise HTTPException(422, "The period ends before it starts")
    if (date_to - date_from).days > MAX_HISTORY_DAYS:
        raise HTTPException(422, f"Choose a period of at most {MAX_HISTORY_DAYS} days")
    if not c.connector:
        raise HTTPException(422, "This charger has no connection")
    start = datetime.combine(date_from, time.min)
    end = min(datetime.combine(date_to + timedelta(days=1), time.min), datetime.now())
    async with live.lock_for(c.id):
        try:
            stats = await live.connector_for(c).history(start, end)
        except ConnectorError as e:
            await live.forget_connector(c.id)
            raise HTTPException(502, str(e)) from None
    return hist.rebuild_sessions(stats)


@router.get("/{charger_id}/history")
async def history_preview(
    charger_id: int, date_from: date, date_to: date, car_id: int | None = None, _: Principal = Depends(require("chargers:manage")), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Sessions rebuilt from the charger's history, compared with the sessions already recorded (nothing is saved)."""
    c = _get(db, charger_id)
    car_id = car_id or c.default_car_id
    rebuilt = await _rebuilt(c, date_from, date_to)
    return {"charger_id": c.id, "car_id": car_id, "items": hist.preview(db, c, rebuilt, car_id)}


@router.post("/{charger_id}/history/apply")
async def history_apply(charger_id: int, body: HistoryApply, _: Principal = Depends(require("chargers:manage")), db: Session = Depends(get_db)) -> dict[str, int]:
    c = _get(db, charger_id)
    _check_car(db, body.car_id)
    rebuilt = await _rebuilt(c, body.date_from, body.date_to)  # fetched again: only the charger's own data is imported
    return hist.apply(db, c, rebuilt, set(body.keys), body.car_id, body.link_matches)
