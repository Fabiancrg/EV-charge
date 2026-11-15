"""Live charging sessions from the chargers' APIs.

Each poll gives a reading (status, power, energy delivered in the current session). From it:

- while the car is plugged in and energy has been delivered, a session exists for the charger's
  default car with status "in progress"; its kWh and end time follow the readings
- unplugging the car (or the energy counter restarting: a new session) closes it: status "recorded"
  (the charger has recorded it with the reimbursing company)
- the last reading before unplugging is up to a few minutes old (chargers report the session's energy in
  steps), so once closed, the session takes its final energy and end time from the charger's own list of
  sessions, when its API has one (Zaptec, Easee)

A charger can have a secondary connection (e.g. the smart plug it is on, through Home Assistant): it is read
when the main one fails or says the charger is offline. The main connection stays the preferred source: as
soon as it answers again, the session in progress takes its figures. While the secondary one is read, the
energy it reports continues the session from where the main one left it (the two don't count from the same
moment, nor exactly the same).

Sessions created here carry external_ref "live:<charger id>:<start>", so they are recognised later.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import i18n
from .connectors import Connector, ConnectorError, LiveReading, make_connector
from .connectors.base import CHARGING, FINISHED, OFFLINE, PAUSED, PENDING_APPROVAL, PLUGGED_IN, UNPLUGGED
from .i18n import msg
from .models import Charger, ChargeSession
from .secretbox import SecretError, decrypt_json

logger = logging.getLogger(__name__)

PRIMARY, SECONDARY = "primary", "secondary"  # the connections of a charger: the main one, and the one read when it can't be
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


FINALIZE_AT = (1, 3, 6, 12, 30)  # polls after the session closed at which the charger's own record is looked up
FINALIZE_BEFORE = timedelta(hours=2)  # the charger's session may have started a little before ours


def _close(c: Charger, state: dict[str, Any], s: ChargeSession) -> None:
    s.reimbursement_status = "recorded"
    state["finalize"] = {"id": s.id, "kwh": s.kwh, "polls": 0}  # see finalize_closed()
    c.live_state = json.dumps(state)


def _span(s: ChargeSession) -> tuple[datetime, datetime]:
    start = datetime.combine(s.charge_date, s.start_time or datetime.min.time())
    end = datetime.combine(s.charge_date, s.end_time) if s.end_time else start
    return start, end + timedelta(days=1) if end < start else end


async def finalize_closed(db: Session, c: Charger, conn: Connector) -> bool:
    """Give the session that was just closed the energy and end time the charger recorded for it.

    Tried a few times after closing (the charger's API may list the session a little later), then given up.
    Nothing changes if the session was edited or removed meanwhile. Returns True when the session was corrected."""
    state = live_state(c)
    todo = state.get("finalize")
    if not isinstance(todo, dict):
        return False
    todo["polls"] = int(todo.get("polls") or 0) + 1
    s = db.get(ChargeSession, todo.get("id"))
    done = s is None or abs(s.kwh - float(todo.get("kwh") or 0)) > 0.0005 or not getattr(conn, "lists_sessions", False)
    corrected = False
    if not done and todo["polls"] in FINALIZE_AT:
        start, end = _span(s)
        try:
            recorded = [h for h in await conn.history(start - FINALIZE_BEFORE, datetime.now()) if h.is_session]
        except ConnectorError as exc:
            logger.info("Charger %s: cannot read the final figures of its last session yet (%s)", c.name, exc)
            recorded = []
        # The charger's session that covers ours (it can't have less energy than we saw)
        covering = [h for h in recorded if h.start <= end and h.end >= start and h.energy_wh / 1000 >= s.kwh - RESET_TOLERANCE_KWH]
        if covering:
            best = max(covering, key=lambda h: min(h.end, end) - max(h.start, start))
            s.kwh = round(best.energy_wh / 1000, 3)
            s.end_time = best.end.time().replace(second=0, microsecond=0)
            done = corrected = True
            logger.info("Charger %s: last session corrected to %.3f kWh, ended %s", c.name, s.kwh, s.end_time.strftime("%H:%M"))
    if done or todo["polls"] >= FINALIZE_AT[-1]:
        state.pop("finalize", None)
    c.live_state = json.dumps(state)
    return corrected


def apply_reading(db: Session, c: Charger, r: LiveReading, now: datetime, source: str = PRIMARY, primary_error: str | None = None) -> str:
    """Update the charger's live state and its live session from one reading. Returns what happened.

    now: local time of the reading (sessions use local dates and times).
    source: the connection the reading comes from; primary_error: why the main one wasn't read (secondary source)."""
    previous = live_state(c)
    state = {**asdict(r), "updated_at": now.isoformat(timespec="seconds"), "source": source}
    if source == SECONDARY:
        state["primary_error"] = primary_error  # None: the main connection answered, but the charger is offline for it
    switched = previous.get("source", PRIMARY) != source
    # When charging starts, remember it: the session starts then, even if no energy is reported yet
    if r.status == CHARGING:
        state["charging_since"] = previous.get("charging_since") if previous.get("status") in PLUGGED and previous.get("charging_since") else now.isoformat(timespec="seconds")
    elif r.status in PLUGGED:
        state["charging_since"] = previous.get("charging_since")
    if previous.get("finalize"):
        state["finalize"] = previous["finalize"]
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
            _close(c, state, current)
            return "closed"
        # After a change of source, the figures of the two can't be compared: remembered until a reading has energy
        resync = source == PRIMARY and bool(switched or previous.get("resync"))
        offset = previous.get("offset") if source == SECONDARY and not switched else None
        if kwh <= 0:  # no energy figure in this reading (Ohme sends 0 while it recalculates): keep the session as it is
            state.update({k: v for k, v in (("resync", resync or None), ("offset", offset)) if v is not None})
            c.live_state = json.dumps(state)
            return "updated"
        if source == SECONDARY:
            # The secondary source continues the session: what it misses of it (it counts from another moment) is added
            state["offset"] = offset = max(0.0, round(current.kwh - kwh, 3)) if offset is None else float(offset)
            c.live_state = json.dumps(state)
            kwh = round(kwh + offset, 3)
        elif resync:  # the main source is back: its figure is the one of the session
            if kwh != current.kwh:
                current.kwh = kwh
                current.end_time = now.time().replace(second=0, microsecond=0)
            return "updated"
        if kwh < current.kwh - RESET_TOLERANCE_KWH:  # the counter restarted: a new session (unplug/replug not seen)
            _close(c, state, current)
            current = None
            action = "closed"
            if state.pop("offset", None):  # the new session starts from the source's own figure
                kwh = round(r.energy_wh / 1000, 3)
                c.live_state = json.dumps(state)
        else:
            if kwh > current.kwh:
                current.kwh = kwh
                current.end_time = now.time().replace(second=0, microsecond=0)
            return "updated"

    if r.status in PLUGGED and kwh > 0:
        if c.default_car_id is None:
            state["warning"] = msg("No default car on this charger: the session can't be recorded automatically")  # shown through EV.t
            c.live_state = json.dumps(state)
            return "no_car"
        since = state.get("charging_since") if action == "idle" else None  # after a restart the new session starts now
        known = r.extra.get("session_start")  # the connector knows when the session started (e.g. from Home Assistant's history)
        if isinstance(known, str) and known <= now.isoformat():
            since = known
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

_connectors: dict[tuple[int, str], tuple[str, Connector]] = {}
_locks: dict[int, asyncio.Lock] = {}


def connector_for(c: Charger, source: str = PRIMARY) -> Connector:
    kind, secret = (c.connector, c.connector_secret) if source == PRIMARY else (c.connector2, c.connector2_secret)
    if not kind:
        raise ConnectorError(msg("This charger has no connection"))
    try:
        config = decrypt_json(secret)
    except SecretError as exc:
        raise ConnectorError(str(exc)) from exc
    fingerprint = hashlib.sha256(f"{kind}:{secret}".encode()).hexdigest()
    cached = _connectors.get((c.id, source))
    if cached and cached[0] == fingerprint:
        return cached[1]
    if cached:
        asyncio.ensure_future(cached[1].close())
    conn = make_connector(kind, config)
    _connectors[(c.id, source)] = (fingerprint, conn)
    return conn


def lock_for(charger_id: int) -> asyncio.Lock:
    return _locks.setdefault(charger_id, asyncio.Lock())


async def forget_connector(charger_id: int, source: str | None = None) -> None:
    """Close the connection(s) of a charger: the next reading logs in again."""
    for key in [k for k in _connectors if k[0] == charger_id and source in (None, k[1])]:
        await _connectors.pop(key)[1].close()


async def poll_charger(db: Session, c: Charger) -> str:
    """Read the charger once and apply the reading (commits)."""
    async with lock_for(c.id):
        reading, source, error = None, PRIMARY, None
        try:
            reading = await connector_for(c).live()
        except ConnectorError as exc:
            error = str(exc)
            await forget_connector(c.id, PRIMARY)  # log in again next time
        if c.connector2 and (reading is None or reading.status == OFFLINE):  # the main source can't tell: ask the secondary one
            try:
                other = await connector_for(c, SECONDARY).live()
            except ConnectorError as exc:
                other = None
                logger.info("Charger %s: its secondary connection can't be read either (%s)", c.name, exc)
                await forget_connector(c.id, SECONDARY)
            if other is not None and (other.status != OFFLINE or reading is None):
                reading, source = other, SECONDARY
        if reading is None:
            c.last_poll_at = datetime.now(UTC)
            c.last_poll_error = i18n.store(error or "")
            db.commit()
            return "error"
        action = apply_reading(db, c, reading, datetime.now(), source, i18n.store(error) if error else None)
        db.commit()
        if await finalize_closed(db, c, connector_for(c)):
            action = "corrected" if action in ("idle", "updated") else action
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
