"""Business logic shared by the API and the reports: car statistics, session derived values."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Car, ChargeSession
from .schemas import CarOut, LeaseStatus, SessionOut


def duration_minutes(start: time | None, end: time | None) -> int | None:
    """Minutes between start and end; an end earlier than the start means the next day."""
    if start is None or end is None:
        return None
    s = datetime.combine(date.min, start)
    e = datetime.combine(date.min, end)
    if e < s:
        e += timedelta(days=1)
    return int((e - s).total_seconds() // 60)


def session_sort_key(s: ChargeSession) -> tuple[date, time, int]:
    return (s.charge_date, s.start_time or time.min, s.id or 0)


def km_since_previous(sessions: Iterable[ChargeSession]) -> dict[int, int]:
    """Distance since the previous session of the same car that has an odometer reading."""
    by_car: dict[int, list[ChargeSession]] = {}
    for s in sessions:
        by_car.setdefault(s.car_id, []).append(s)
    result: dict[int, int] = {}
    for car_sessions in by_car.values():
        previous: int | None = None
        for s in sorted(car_sessions, key=session_sort_key):
            if s.odometer is None:
                continue
            if previous is not None:
                result[s.id] = s.odometer - previous
            previous = s.odometer
    return result


def session_out(s: ChargeSession, km_prev: dict[int, int] | None = None) -> SessionOut:
    minutes = duration_minutes(s.start_time, s.end_time)
    return SessionOut(
        id=s.id,
        car_id=s.car_id,
        car_name=s.car.name,
        charge_date=s.charge_date,
        odometer=s.odometer,
        location=s.location,
        provider=s.provider,
        kwh=s.kwh,
        start_time=s.start_time,
        end_time=s.end_time,
        to_reimburse=s.to_reimburse,
        cost=s.cost,
        notes=s.notes,
        duration_minutes=minutes,
        avg_power_kw=round(s.kwh / (minutes / 60), 1) if minutes else None,
        price_per_kwh=round(s.cost / s.kwh, 3) if s.cost is not None and s.kwh else None,
        km_since_previous=(km_prev or {}).get(s.id),
    )


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def years_between(start: date, end: date) -> float:
    """Calendar years between two dates (whole months, then the remaining days as a fraction of a month),
    so a 36-month lease is exactly 3.0 whatever the leap years."""
    if end <= start:
        return 0.0
    months = (end.year - start.year) * 12 + end.month - start.month
    if _add_months(start, months) > end:
        months -= 1
    anchor = _add_months(start, months)
    month_days = (_add_months(start, months + 1) - anchor).days
    return (months + (end - anchor).days / month_days) / 12


def lease_status(car: Car, last_odometer: int | None, today: date | None = None) -> LeaseStatus | None:
    if not car.lease_start or not car.lease_end:
        return None
    today = today or date.today()
    days_total = (car.lease_end - car.lease_start).days
    days_elapsed = min(max((today - car.lease_start).days, 0), days_total)
    status = LeaseStatus(days_total=days_total, days_elapsed=days_elapsed, days_left=days_total - days_elapsed)
    if car.lease_km_per_year:
        status.km_allowed_total = round(car.lease_km_per_year * years_between(car.lease_start, car.lease_end))
        status.km_allowed_to_date = round(car.lease_km_per_year * years_between(car.lease_start, min(max(today, car.lease_start), car.lease_end)))
    if last_odometer is not None:
        status.km_driven = max(last_odometer - (car.initial_odometer or 0), 0)
        if days_elapsed > 0:
            status.km_projected_end = round(status.km_driven * days_total / days_elapsed)
    return status


def car_out(db: Session, car: Car, today: date | None = None) -> CarOut:
    sessions = db.scalars(select(ChargeSession).where(ChargeSession.car_id == car.id)).all()
    with_odo = [s for s in sessions if s.odometer is not None]
    last = max(with_odo, key=session_sort_key) if with_odo else None
    out = CarOut.model_validate(car)
    out.session_count = len(sessions)
    out.total_kwh = round(sum(s.kwh for s in sessions), 2)
    out.last_odometer = last.odometer if last else None
    out.last_charge_date = max((s.charge_date for s in sessions), default=None)
    out.lease = lease_status(car, out.last_odometer, today)
    return out
