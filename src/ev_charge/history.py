"""Charging sessions from a charger's history, for an import the administrator confirms.

- Zaptec lists its sessions: they are taken as they are (exact times and energy).
- Ohme only gives energy per hour: consecutive hours with energy, or separated by at most one
  empty hour, make one session. Times are then approximate (to the hour, the end tightened with
  the active charging time of the last hour).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import HourStat
from .models import Charger, ChargeSession

MAX_GAP = timedelta(hours=1)


@dataclass
class Rebuilt:
    start: datetime
    end: datetime
    kwh: float
    hours: int
    exact: bool = False  # a session listed by the charger (not rebuilt from hours)

    @property
    def key(self) -> str:
        return str(int(self.start.timestamp() * 1000))


def rebuild_sessions(stats: list[HourStat]) -> list[Rebuilt]:
    out: list[Rebuilt] = [Rebuilt(start=h.start, end=h.end, kwh=h.energy_wh / 1000, hours=0, exact=True) for h in stats if h.is_session and h.energy_wh > 0]
    current: Rebuilt | None = None
    for h in sorted((h for h in stats if h.energy_wh > 0 and not h.is_session), key=lambda h: h.start):
        end = h.start + timedelta(milliseconds=h.active_ms) if 0 < h.active_ms < (h.end - h.start).total_seconds() * 1000 else h.end
        if current is not None and h.start - current.end <= MAX_GAP:
            current.end = max(current.end, end)
            current.kwh += h.energy_wh / 1000
            current.hours += 1
        else:
            current = Rebuilt(start=h.start, end=end, kwh=h.energy_wh / 1000, hours=1)
            out.append(current)
    for s in out:
        s.kwh = round(s.kwh, 3)
    return sorted(out, key=lambda r: r.start)


def _ref(c: Charger, r: Rebuilt) -> str:
    return f"history:{c.id}:{r.key}"


def _match(r: Rebuilt, candidates: list[ChargeSession]) -> ChargeSession | None:
    """An existing session for the same charge: same day(s), close kWh."""
    days = {r.start.date(), r.end.date(), (r.start - timedelta(days=1)).date()}
    best = None
    for s in candidates:
        if s.charge_date not in days:
            continue
        if abs(s.kwh - r.kwh) <= max(1.0, 0.1 * r.kwh) and (best is None or abs(s.kwh - r.kwh) < abs(best.kwh - r.kwh)):
            best = s
    return best


def preview(db: Session, c: Charger, rebuilt: list[Rebuilt], car_id: int | None) -> list[dict[str, Any]]:
    refs = {s.external_ref for s in db.scalars(select(ChargeSession).where(ChargeSession.external_ref.like(f"history:{c.id}:%")))}
    lo = min((r.start.date() for r in rebuilt), default=date.today()) - timedelta(days=2)
    hi = max((r.end.date() for r in rebuilt), default=date.today()) + timedelta(days=1)
    query = select(ChargeSession).where(ChargeSession.charge_date >= lo, ChargeSession.charge_date <= hi)
    candidates = [s for s in db.scalars(query) if s.charger_id == c.id or (car_id is not None and s.car_id == car_id)]
    items = []
    for r in rebuilt:
        status, match = "new", None
        if _ref(c, r) in refs:
            status = "imported"
        else:
            m = _match(r, candidates)
            if m is not None:
                status, match = "match", {"id": m.id, "charge_date": m.charge_date.isoformat(), "kwh": m.kwh, "charger_id": m.charger_id,
                                          "start_time": m.start_time.strftime("%H:%M") if m.start_time else None}  # fmt: skip
        items.append({
            "key": r.key, "charge_date": r.start.date().isoformat(), "start_time": r.start.strftime("%H:%M"), "end_time": r.end.strftime("%H:%M"),
            "kwh": r.kwh, "hours": r.hours, "exact": r.exact, "status": status, "match": match,
        })  # fmt: skip
    return items


def apply(db: Session, c: Charger, rebuilt: list[Rebuilt], keys: set[str], car_id: int, link_matches: bool) -> dict[str, int]:
    """Create the selected new sessions; optionally set this charger on the matched existing sessions that have none."""
    items = {i["key"]: i for i in preview(db, c, rebuilt, car_id)}
    created = linked = 0
    for r in rebuilt:
        item = items[r.key]
        if r.key in keys and item["status"] == "new":
            db.add(
                ChargeSession(
                    car_id=car_id, charge_date=r.start.date(), location=c.default_location, provider=c.default_provider, kwh=r.kwh,
                    start_time=r.start.time().replace(second=0, microsecond=0), end_time=r.end.time().replace(second=0, microsecond=0),
                    to_reimburse=True, reimbursement_status="recorded", charger_id=c.id, external_ref=_ref(c, r),
                    notes=f"Imported from the {c.name} charging history" + ("" if r.exact else " (times rounded to the hour)"),
                )
            )  # fmt: skip
            created += 1
        elif link_matches and r.key in keys and item["status"] == "match" and item["match"]["charger_id"] is None:
            s = db.get(ChargeSession, item["match"]["id"])
            if s is not None:
                s.charger_id = c.id
                s.provider = s.provider or c.default_provider
                s.to_reimburse = True
                s.reimbursement_status = s.reimbursement_status or "recorded"
                linked += 1
    db.commit()
    return {"created": created, "linked": linked}
