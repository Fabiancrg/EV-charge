"""Live charging sessions from the chargers' APIs.

Each poll gives a reading (status, power, energy delivered in the current session). From it:

- while the car is plugged in and energy has been delivered, a session exists for the charger's
  default car with status "in progress"; its kWh and end time follow the readings
- unplugging the car (or the energy counter restarting: a new session) closes it: status "recorded"
  (the charger has recorded it with the reimbursing company)

Sessions created here carry external_ref "live:<charger id>:<start>", so they are recognised later.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import Connector, ConnectorError, LiveReading, make_connector
from .connectors.base import CHARGING, FINISHED, OFFLINE, PAUSED, PENDING_APPROVAL, PLUGGED_IN, UNPLUGGED
from .models import Charger, ChargeSession
from .secretbox import SecretError, decrypt_json

logger = logging.getLogger(__name__)

PLUGGED = {CHARGING, PAUSED, PLUGGED_IN, FINISHED}  # a session can be open
CLOSING = {UNPLUGGED, PENDING_APPROVAL}  # the car left: close the session
RESET_TOLERANCE_KWH = 0.05  # the energy counter going down by more than this = a new session


def live_state(c: Charger) -> dict[str, Any]:
    try:
        return json.loads(c.live_state) if c.live_state else {}
    except ValueError:
        return {}


def open_session(db: Session, c: Charger) -> ChargeSession | None:
    return db.scalar(
        select(ChargeSession)
        .where(ChargeSession.charger_id == c.id, ChargeSession.external_ref.like(f"live:{c.id}:%"), ChargeSession.reimbursement_status == "in_progress")
        .order_by(ChargeSession.id.desc())
    )


def _close(s: ChargeSession) -> None:
    s.reimbursement_status = "recorded"


def apply_reading(db: Session, c: Charger, r: LiveReading, now: datetime) -> str:
    """Update the charger's live state and its live session from one reading. Returns what happened.

    now: local time of the reading (sessions use local dates and times)."""
    previous = live_state(c)
    state = {**asdict(r), "updated_at": now.isoformat(timespec="seconds")}
    # When charging starts, remember it: the session starts then, even if no energy is reported yet
    if r.status == CHARGING:
        state["charging_since"] = previous.get("charging_since") if previous.get("status") in PLUGGED and previous.get("charging_since") else now.isoformat(timespec="seconds")
    elif r.status in PLUGGED:
        state["charging_since"] = previous.get("charging_since")
    c.live_state = json.dumps(state)
    c.last_poll_at = datetime.now(UTC)
    c.last_poll_error = None
    if r.status == OFFLINE:
        return "offline"

    kwh = round(r.energy_wh / 1000, 3)
    current = open_session(db, c)
    action = "idle"
    if current is not None:
        if r.status in CLOSING:
            _close(current)
            return "closed"
        if kwh <= 0:  # no energy figure in this reading (Ohme sends 0 while it recalculates): keep the session as it is
            return "updated"
        if kwh < current.kwh - RESET_TOLERANCE_KWH:  # the counter restarted: a new session (unplug/replug not seen)
            _close(current)
            current = None
            action = "closed"
        else:
            if kwh > current.kwh:
                current.kwh = kwh
                current.end_time = now.time().replace(second=0, microsecond=0)
            return "updated"

    if r.status in PLUGGED and kwh > 0:
        if c.default_car_id is None:
            state["warning"] = "No default car on this charger: the session can't be recorded automatically"
            c.live_state = json.dumps(state)
            return "no_car"
        since = state.get("charging_since") if action == "idle" else None  # after a restart the new session starts now
        start = datetime.fromisoformat(since) if since else now
        db.add(
            ChargeSession(
                car_id=c.default_car_id,
                charge_date=start.date(),
                location=c.default_location,
                provider=c.default_provider,
                kwh=kwh,
                start_time=start.time().replace(second=0, microsecond=0),
                end_time=now.time().replace(second=0, microsecond=0),
                to_reimburse=True,
                reimbursement_status="in_progress",
                charger_id=c.id,
                external_ref=f"live:{c.id}:{start.isoformat(timespec='minutes')}",
                notes=f"Recorded automatically from the {c.name} charger",
            )
        )
        return "created" if action == "idle" else "restarted"
    return action


# ── Connectors (one per charger, kept between polls to reuse the login) ───────

_connectors: dict[int, tuple[str, Connector]] = {}
_locks: dict[int, asyncio.Lock] = {}


def connector_for(c: Charger) -> Connector:
    if not c.connector:
        raise ConnectorError("This charger has no connection")
    try:
        config = decrypt_json(c.connector_secret)
    except SecretError as exc:
        raise ConnectorError(str(exc)) from exc
    fingerprint = hashlib.sha256(f"{c.connector}:{c.connector_secret}".encode()).hexdigest()
    cached = _connectors.get(c.id)
    if cached and cached[0] == fingerprint:
        return cached[1]
    if cached:
        asyncio.ensure_future(cached[1].close())
    conn = make_connector(c.connector, config)
    _connectors[c.id] = (fingerprint, conn)
    return conn


def lock_for(charger_id: int) -> asyncio.Lock:
    return _locks.setdefault(charger_id, asyncio.Lock())


async def forget_connector(charger_id: int) -> None:
    cached = _connectors.pop(charger_id, None)
    if cached:
        await cached[1].close()


async def poll_charger(db: Session, c: Charger) -> str:
    """Read the charger once and apply the reading (commits)."""
    async with lock_for(c.id):
        try:
            reading = await connector_for(c).live()
        except ConnectorError as exc:
            c.last_poll_at = datetime.now(UTC)
            c.last_poll_error = str(exc)[:500]
            db.commit()
            await forget_connector(c.id)  # log in again next time
            return "error"
        action = apply_reading(db, c, reading, datetime.now())
        db.commit()
        if action not in ("idle", "updated", "offline"):
            logger.info("Charger %s: %s (%.3f kWh, %s)", c.name, action, reading.energy_wh / 1000, reading.status)
        return action


async def poll_all(session_factory: Any) -> None:
    db = session_factory()
    try:
        for c in db.scalars(select(Charger).where(Charger.poll_enabled.is_(True), Charger.connector.is_not(None), Charger.active.is_(True))).all():
            try:
                await poll_charger(db, c)
            except Exception:  # never stop the loop
                logger.exception("Polling charger %s failed", c.name)
                db.rollback()
    finally:
        db.close()


async def poll_loop(session_factory: Any, interval: int) -> None:
    while True:
        await poll_all(session_factory)
        await asyncio.sleep(interval)
