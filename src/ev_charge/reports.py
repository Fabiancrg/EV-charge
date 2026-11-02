"""Aggregations behind the report page (all computed in Python: personal data volumes are small)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Car, ChargeSession
from .pricing import Pricing
from .services import duration_minutes, session_sort_key, years_between

TOP_N = 7  # providers / locations shown individually; the rest fold into "Other"


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_range(first: date, last: date) -> list[str]:
    months = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _ranked(groups: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    rows = sorted(({"name": k, **v} for k, v in groups.items()), key=lambda r: -r["kwh"])
    if len(rows) > TOP_N + 1:
        rest = rows[TOP_N:]
        other = {"name": "Other", "kwh": sum(r["kwh"] for r in rest), "sessions": sum(r["sessions"] for r in rest), "cost": sum(r["cost"] for r in rest)}
        rows = rows[:TOP_N] + [other]
    for r in rows:
        r["kwh"] = round(r["kwh"], 2)
        r["cost"] = round(r["cost"], 2)
    return rows


def build_report(db: Session, car_ids: list[int] | None = None, date_from: date | None = None, date_to: date | None = None) -> dict[str, Any]:
    all_cars = db.scalars(select(Car).order_by(Car.id)).all()
    color_index = {c.id: i for i, c in enumerate(all_cars)}  # colour follows the car, not the filter
    cars = [c for c in all_cars if not car_ids or c.id in car_ids]
    wanted = {c.id for c in cars}

    every = [s for s in db.scalars(select(ChargeSession)).all() if s.car_id in wanted]

    def in_range(s: ChargeSession) -> bool:
        return (date_from is None or s.charge_date >= date_from) and (date_to is None or s.charge_date <= date_to)

    sessions = sorted((s for s in every if in_range(s)), key=session_sort_key)
    pricing = Pricing(db)
    prices = {s.id: pricing.price(s) for s in sessions}

    def cost(s: ChargeSession) -> float | None:  # home tariff for home charging, else the cost entered
        return prices[s.id].cost

    # Distance and consumption: pairs of consecutive odometer readings per car (whole history, so the first
    # session in the range still gets its distance); a pair counts when its later session is in the range.
    km_pairs: list[tuple[ChargeSession, int]] = []
    for car in cars:
        readings = sorted((s for s in every if s.car_id == car.id and s.odometer is not None), key=session_sort_key)
        for prev, cur in zip(readings, readings[1:], strict=False):
            delta = cur.odometer - prev.odometer  # type: ignore[operator]
            if delta >= 0 and in_range(cur):
                km_pairs.append((cur, delta))

    months = _month_range(sessions[0].charge_date, sessions[-1].charge_date) if sessions else []
    idx = {m: i for i, m in enumerate(months)}

    kwh_by_month = {c.id: [0.0] * len(months) for c in cars}
    km_by_month = {c.id: [0] * len(months) for c in cars}
    reimb = {"to_reimburse": [0.0] * len(months), "private": [0.0] * len(months)}
    providers: dict[str, dict[str, float]] = defaultdict(lambda: {"kwh": 0.0, "sessions": 0, "cost": 0.0})
    locations: dict[str, dict[str, float]] = defaultdict(lambda: {"kwh": 0.0, "sessions": 0, "cost": 0.0})
    table: dict[str, dict[str, float]] = {m: {"sessions": 0, "kwh": 0.0, "km": 0, "cost": 0.0, "kwh_to_reimburse": 0.0, "cost_to_reimburse": 0.0} for m in months}
    minutes_total = 0

    for s in sessions:
        i = idx[_month(s.charge_date)]
        kwh_by_month[s.car_id][i] += s.kwh
        reimb["to_reimburse" if s.to_reimburse else "private"][i] += s.kwh
        row = table[months[i]]
        row["sessions"] += 1
        row["kwh"] += s.kwh
        row["cost"] += cost(s) or 0
        if s.to_reimburse:
            row["kwh_to_reimburse"] += s.kwh
            row["cost_to_reimburse"] += cost(s) or 0
        for groups, key in ((providers, s.provider), (locations, s.location)):
            g = groups[key or "(not set)"]
            g["kwh"] += s.kwh
            g["sessions"] += 1
            g["cost"] += cost(s) or 0
        minutes_total += duration_minutes(s.start_time, s.end_time) or 0

    for s, delta in km_pairs:
        i = idx[_month(s.charge_date)]
        km_by_month[s.car_id][i] += delta
        table[months[i]]["km"] += delta

    total_kwh = sum(s.kwh for s in sessions)
    with_cost = [s for s in sessions if cost(s) is not None]
    km_total = sum(d for _, d in km_pairs)
    kwh_for_km = sum(s.kwh for s, _ in km_pairs)
    reimbursable = [s for s in sessions if s.to_reimburse]

    lease = None
    if len(cars) == 1 and cars[0].lease_start and cars[0].lease_end:
        car = cars[0]
        start_odo = car.initial_odometer or 0
        allowed_end = start_odo + round(car.lease_km_per_year * years_between(car.lease_start, car.lease_end)) if car.lease_km_per_year else None
        lease = {
            "start": car.lease_start.isoformat(),
            "end": car.lease_end.isoformat(),
            "start_odometer": start_odo,
            "allowed_end_odometer": allowed_end,
            "readings": [{"date": s.charge_date.isoformat(), "odometer": s.odometer} for s in sorted(every, key=session_sort_key) if s.odometer is not None],
        }

    return {
        "cars": [{"id": c.id, "name": c.name, "color_index": color_index[c.id]} for c in cars],
        "totals": {
            "sessions": len(sessions),
            "kwh": round(total_kwh, 2),
            "km": km_total,
            "consumption_kwh_100km": round(kwh_for_km / km_total * 100, 1) if km_total else None,
            "cost": round(sum(cost(s) or 0 for s in with_cost), 2) if with_cost else None,
            "avg_price_per_kwh": round(sum(cost(s) or 0 for s in with_cost) / sum(s.kwh for s in with_cost), 3) if with_cost else None,
            "home_kwh": round(sum(s.kwh for s in sessions if prices[s.id].home), 2),
            "home_without_tariff": sum(prices[s.id].missing_tariff for s in sessions),
            "avg_kwh_per_session": round(total_kwh / len(sessions), 1) if sessions else None,
            "charging_hours": round(minutes_total / 60, 1),
            "sessions_to_reimburse": len(reimbursable),
            "kwh_to_reimburse": round(sum(s.kwh for s in reimbursable), 2),
            "cost_to_reimburse": round(sum(cost(s) or 0 for s in reimbursable), 2),
        },
        "months": months,
        "kwh_by_month": {str(k): [round(v, 2) for v in vals] for k, vals in kwh_by_month.items()},
        "km_by_month": {str(k): vals for k, vals in km_by_month.items()},
        "reimburse_by_month": {k: [round(v, 2) for v in vals] for k, vals in reimb.items()},
        "by_provider": _ranked(providers),
        "by_location": _ranked(locations),
        "monthly": [{"month": m, **{k: round(v, 2) for k, v in table[m].items()}} for m in months],
        "lease": lease,
    }


def build_reimbursement(db: Session, car_ids: list[int] | None, date_from: date | None, date_to: date | None) -> dict[str, Any]:
    """Sessions flagged "to be reimbursed", priced (home tariff or cost entered), for a printable statement."""
    pricing = Pricing(db)
    cars = {c.id: c for c in db.scalars(select(Car))}
    rows = []
    for s in sorted(db.scalars(select(ChargeSession).where(ChargeSession.to_reimburse.is_(True))), key=session_sort_key):
        if car_ids and s.car_id not in car_ids:
            continue
        if (date_from and s.charge_date < date_from) or (date_to and s.charge_date > date_to):
            continue
        p = pricing.price(s)
        unit = p.tariff_price if p.home else (round(p.cost / s.kwh, 4) if p.cost is not None and s.kwh else None)
        rows.append({
            "id": s.id, "charge_date": s.charge_date.isoformat(), "car_id": s.car_id, "car_name": cars[s.car_id].name,
            "license_plate": cars[s.car_id].license_plate, "odometer": s.odometer, "location": s.location, "provider": s.provider,
            "kwh": s.kwh, "start_time": s.start_time.strftime("%H:%M") if s.start_time else None,
            "end_time": s.end_time.strftime("%H:%M") if s.end_time else None, "home": p.home, "price_per_kwh": unit,
            "amount": p.cost, "missing_price": p.cost is None, "notes": s.notes,
        })  # fmt: skip
    used_cars = sorted({r["car_id"] for r in rows} | set(car_ids or []), key=lambda i: cars[i].name if i in cars else "")
    return {
        "cars": [{"id": i, "name": cars[i].name, "license_plate": cars[i].license_plate, "make": cars[i].make, "model": cars[i].model} for i in used_cars if i in cars],
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "rows": rows,
        "totals": {
            "sessions": len(rows),
            "kwh": round(sum(r["kwh"] for r in rows), 2),
            "home_kwh": round(sum(r["kwh"] for r in rows if r["home"]), 2),
            "amount": round(sum(r["amount"] or 0 for r in rows), 2),
            "home_amount": round(sum(r["amount"] or 0 for r in rows if r["home"]), 2),
            "missing_price": sum(r["missing_price"] for r in rows),
        },
    }
