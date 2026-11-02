"""Charging session import: turn spreadsheet rows + a column mapping into sessions.

The wizard (static/js/ev-import.js) reads the file through /api/import/sheet,
lets the user pick the sheet, header row and column mapping, then posts the
data rows here twice: once for the preview (nothing stored), once to import.
Values are parsed leniently: European and US number formats, several date
orders, Excel serial dates, yes/no in English, French and Dutch.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Car, Charger, ChargeSession
from .schemas import ReimbursementStatus, default_status

FIELDS = (
    "car", "charge_date", "odometer", "location", "provider", "kwh", "start_time", "end_time", "duration",
    "to_reimburse", "cost", "notes", "charger", "reimbursement_status",
)  # fmt: skip
DateOrder = Literal["auto", "dmy", "mdy", "ymd"]


class ImportRequest(BaseModel):
    rows: list[list[str]] = Field(max_length=10_000)  # data rows (below the header row)
    first_row_number: int = 2  # sheet row number of rows[0], for messages
    mapping: dict[str, int]  # field → column index
    car_id: int | None = None  # every row goes to this car (no car column)
    create_missing_cars: bool = False  # unknown names in the car column become new cars
    date_order: DateOrder = "auto"
    default_to_reimburse: bool = False  # when there is no reimbursement column
    charger_id: int | None = None  # every row was charged with this charger (no charger column)
    default_status: ReimbursementStatus | None = None  # for rows to be reimbursed without a status column/value
    skip_duplicates: bool = True


@dataclass
class ParsedRow:
    row: int
    status: str = "ok"  # ok | error | duplicate
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    car_name: str | None = None
    charger_name: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


# ── Value parsing ────────────────────────────────────────────────────────────

_TRUE = {"yes", "y", "true", "1", "x", "oui", "o", "ja", "j", "si", "vrai", "waar", "ok", "✓", "✔", "business", "pro", "professional", "professionnel", "company", "work"}
_FALSE = {"no", "n", "false", "0", "non", "nee", "neen", "faux", "onwaar", "-", "private", "prive", "privé", "personal", "perso", "personnel"}
_EXCEL_EPOCH = date(1899, 12, 30)
_STATUS_WORDS = {  # compared after norm(): lower case, no accents
    "in_progress": ("in progress", "in_progress", "ongoing", "pending", "en cours", "bezig", "lopend", "in behandeling", "in uitvoering"),
    "not_recorded": (
        "not recorded",
        "not_recorded",
        "unrecorded",
        "not exported",
        "missing",
        "non enregistre",
        "pas enregistre",
        "non exporte",
        "niet geregistreerd",
        "niet geexporteerd",
        "ontbreekt",
    ),
    "recorded": ("recorded", "exported", "registered", "enregistre", "exporte", "geregistreerd", "geexporteerd"),
    "invoiced": ("invoiced", "invoice", "billed", "facture", "facturee", "gefactureerd", "factuur"),
    "paid": ("paid", "reimbursed", "refunded", "settled", "paye", "payee", "rembourse", "betaald", "terugbetaald", "vereffend"),
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.strip().lower())


def parse_number(raw: str) -> float | None:
    """'12,5' '12.5' '1.234,56' '1,234.56' '€ 12,50' '30 kWh' → float. Empty → None; unreadable → ValueError."""
    s = str(raw or "").strip()
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s.replace("−", "-"))
    if not re.search(r"\d", s):
        raise ValueError
    if "," in s and "." in s:  # the last separator is the decimal one
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif s.count(",") > 1:
        s = s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    else:
        s = s.replace(",", ".")
    return float(s)


def parse_bool(raw: str) -> bool | None:
    v = norm(raw)
    if not v:
        return None
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError


_DATE_RE = re.compile(r"^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})(?:[ T]+(\d{1,2})[:h](\d{2})(?::\d{2}(?:\.\d+)?)?)?$")


def _split_date(raw: str) -> tuple[int, int, int, time | None] | None:
    m = _DATE_RE.match(str(raw or "").strip())
    if not m:
        return None
    t = time(int(m[4]), int(m[5])) if m[4] is not None and int(m[4]) < 24 else None
    return int(m[1]), int(m[2]), int(m[3]), t


def detect_date_order(values: list[str]) -> DateOrder:
    """Order of a date column: ymd when the year comes first, mdy only when a middle part exceeds 12."""
    order: DateOrder = "dmy"
    for v in values:
        parts = _split_date(v)
        if not parts:
            continue
        a, b, _c, _ = parts
        if a > 31:
            return "ymd"
        if b > 12 and a <= 12:
            order = "mdy"
        elif a > 12:
            return "dmy"
    return order


def parse_datetime(raw: str, order: DateOrder) -> tuple[date | None, time | None]:
    """A date (with an optional time). Excel serial numbers are accepted. Empty → (None, None)."""
    s = str(raw or "").strip()
    if not s:
        return None, None
    if re.fullmatch(r"\d{5}(\.\d+)?", s):  # Excel serial date (cell formatted as a number)
        serial = float(s)
        if 20000 < serial < 80000:
            dt = datetime.combine(_EXCEL_EPOCH, time()) + timedelta(days=serial)
            dt = dt.replace(second=0, microsecond=0)
            return dt.date(), (dt.time() if dt.time() != time() else None)
    parts = _split_date(s)
    if not parts:
        raise ValueError
    a, b, c, t = parts
    if a > 31 or order == "ymd":
        y, m, d = a, b, c
    elif order == "mdy":
        m, d, y = a, b, c
    else:
        d, m, y = a, b, c
    if y < 100:
        y += 2000
    return date(y, m, d), t


def parse_time(raw: str) -> time | None:
    """'22:30' '22h30' '22.30' '2230' '22:30:15' '2026-01-05 22:30' or an Excel day fraction (0.9375)."""
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if re.fullmatch(r"0?\.\d+", s):  # Excel fraction of a day
        minutes = round(float(s) * 1440) % 1440
        return time(minutes // 60, minutes % 60)
    m = re.search(r"(\d{1,2})\s*[:h.]\s*(\d{2})(?::\d{2})?\s*(am|pm)?$", s) or re.fullmatch(r"(\d{1,2})(\d{2})\s*(am|pm)?", s) or re.fullmatch(r"(\d{1,2})\s*()(am|pm|h)", s)
    if not m:
        raise ValueError
    hour, minute = int(m[1]), int(m[2] or 0)
    if m[3] == "pm" and hour < 12:
        hour += 12
    elif m[3] == "am" and hour == 12:
        hour = 0
    return time(hour % 24 if hour == 24 else hour, minute)


def parse_status(raw: str) -> str | None:
    """'Paid', 'facturé', 'niet geregistreerd'… → a reimbursement status. Empty → None."""
    v = norm(raw).replace("-", " ")
    if not v:
        return None
    for status, words in _STATUS_WORDS.items():  # "not recorded" is checked before "recorded"
        if v in words or any(len(w) > 4 and w in v for w in words):
            return status
    raise ValueError


def parse_duration(raw: str) -> int | None:
    """Minutes from '90', '1:30', '1h30', '1.5 h', '45 min'. Empty → None."""
    s = norm(raw).replace(",", ".")
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return round(float(s))
    m = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", s)
    if m:
        return int(m[1]) * 60 + int(m[2]) + round(int(m[3] or 0) / 60)
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|heures?|u|uur|m|mn|min|mins|minutes?)?", s):
        found = True
        total += float(num) * (60 if unit and unit[0] in "hu" else 1)
    if not found:
        raise ValueError
    return round(total)


# ── Rows → sessions ──────────────────────────────────────────────────────────


def _cars_lookup(db: Session, scope: set[int] | None) -> dict[str, Car]:
    lookup: dict[str, Car] = {}
    for car in db.scalars(select(Car)):
        if scope is not None and car.id not in scope:
            continue
        for key in (car.name, car.license_plate):
            if key:
                lookup.setdefault(re.sub(r"[\s\-.]", "", norm(key)), car)
    return lookup


def _chargers_lookup(db: Session) -> dict[str, Charger]:
    lookup: dict[str, Charger] = {}
    for c in db.scalars(select(Charger)):
        for key in (c.name, c.serial_number):
            if key:
                lookup.setdefault(re.sub(r"[\s\-.]", "", norm(key)), c)
    return lookup


def _dup_key(car_key: Any, d: date, kwh: float, odometer: int | None) -> tuple:
    return (car_key, d, round(kwh, 3), odometer)


def parse_rows(db: Session, req: ImportRequest, scope: set[int] | None = None) -> list[ParsedRow]:
    """scope: the car ids the user may import into (None: every car)."""
    mapping = {k: v for k, v in req.mapping.items() if k in FIELDS and v is not None and v >= 0}
    order = req.date_order
    if order == "auto":
        col = mapping.get("charge_date", mapping.get("start_time"))
        order = detect_date_order([r[col] for r in req.rows if col is not None and col < len(r)]) if col is not None else "dmy"
    cars = _cars_lookup(db, scope)
    chargers = _chargers_lookup(db)
    fixed_charger = db.get(Charger, req.charger_id) if req.charger_id else None
    fixed_car = db.get(Car, req.car_id) if req.car_id and (scope is None or req.car_id in scope) else None
    existing = {_dup_key(s.car_id, s.charge_date, s.kwh, s.odometer) for s in db.scalars(select(ChargeSession))}
    seen: set[tuple] = set()
    out: list[ParsedRow] = []

    for i, raw in enumerate(req.rows):
        cell = {f: (raw[c].strip() if c < len(raw) and raw[c] is not None else "") for f, c in mapping.items()}
        if not any(cell.values()):
            continue  # blank row
        p = ParsedRow(row=req.first_row_number + i)

        def parse(name: str, label: str, fn: Any, cell: dict[str, str] = cell, p: ParsedRow = p) -> Any:
            if name not in cell:
                return None
            try:
                return fn(cell[name])
            except (ValueError, TypeError, OverflowError):
                p.errors.append(f'{label}: cannot read "{cell[name]}"')
                return None

        # Car
        car: Car | None = None
        if "car" in cell and cell["car"]:
            car = cars.get(re.sub(r"[\s\-.]", "", norm(cell["car"])))
            p.car_name = car.name if car else cell["car"]
            if car is None and not req.create_missing_cars:
                p.errors.append(f'unknown car "{cell["car"]}"' + (" (or not assigned to you)" if scope is not None else ""))
        elif fixed_car is not None:
            car, p.car_name = fixed_car, fixed_car.name
        else:
            p.errors.append("no car")

        # Date and times (a date column holding a date-time also gives the start time)
        charge_date, date_time = parse("charge_date", "date", lambda v: parse_datetime(v, order)) or (None, None)
        start_date, start_t = (None, None)
        if "start_time" in cell and _split_date(cell["start_time"]):
            start_date, start_t = parse("start_time", "start", lambda v: parse_datetime(v, order)) or (None, None)
        else:
            start_t = parse("start_time", "start", parse_time)
        end_t = parse("end_time", "end", lambda v: parse_datetime(v, order)[1] if _split_date(v) else parse_time(v))
        charge_date = charge_date or start_date
        start_t = start_t or date_time
        if charge_date is None and not any("date" in e for e in p.errors):
            p.errors.append("no date")
        duration = parse("duration", "duration", parse_duration)
        if end_t is None and start_t is not None and duration:
            end_t = (datetime.combine(date.min, start_t) + timedelta(minutes=duration)).time()

        kwh = parse("kwh", "kWh", parse_number)
        if kwh is None and not any(e.startswith("kWh") for e in p.errors):
            p.errors.append("no kWh")
        elif kwh is not None and not 0 < kwh <= 1000:
            p.errors.append(f"kWh out of range ({kwh:g})")
        odometer = parse("odometer", "odometer", parse_number)
        if odometer is not None and odometer < 0:
            p.errors.append("negative odometer")
        cost = parse("cost", "cost", parse_number)
        if cost is not None and cost < 0:
            p.errors.append("negative cost")
        reimb = parse("to_reimburse", "reimbursement", parse_bool)
        status = parse("reimbursement_status", "status", parse_status)
        charger: Charger | None = None
        if cell.get("charger"):
            charger = chargers.get(re.sub(r"[\s\-.]", "", norm(cell["charger"])))
            if charger is None:
                p.errors.append(f'unknown charger "{cell["charger"]}" (add it on the Chargers page)')
        elif fixed_charger is not None:
            charger = fixed_charger
        if charge_date and charge_date > date.today() + timedelta(days=1):
            p.warnings.append("date in the future")

        p.values = {
            "car_id": car.id if car else None,
            "charge_date": charge_date,
            "odometer": round(odometer) if odometer is not None else None,
            "location": cell.get("location") or None,
            "provider": cell.get("provider") or None,
            "kwh": round(kwh, 3) if kwh is not None else None,
            "start_time": start_t,
            "end_time": end_t,
            # Without a value: a charger's sessions, and sessions with a status, are to be reimbursed
            "to_reimburse": reimb if reimb is not None else bool(charger or status or ("to_reimburse" not in mapping and req.default_to_reimburse)),
            "cost": round(cost, 2) if cost is not None else None,
            "notes": cell.get("notes") or None,
            "charger_id": charger.id if charger else None,
        }
        p.values["reimbursement_status"] = default_status(p.values["to_reimburse"], status or req.default_status, p.values["charger_id"])
        p.charger_name = charger.name if charger else None
        for key, limit in (("location", 200), ("provider", 200)):
            if p.values[key] and len(p.values[key]) > limit:
                p.values[key] = p.values[key][:limit]
                p.warnings.append(f"{key} shortened to {limit} characters")

        if p.errors:
            p.status = "error"
        else:
            key = _dup_key(car.id if car else norm(p.car_name or ""), charge_date, kwh, p.values["odometer"])  # type: ignore[arg-type]
            if key in existing:
                p.status = "duplicate"
                p.warnings.append("already recorded (same car, date, kWh and odometer)")
            elif key in seen:
                p.status = "duplicate"
                p.warnings.append("same session earlier in the file")
            seen.add(key)
        out.append(p)
    return out


def summary(rows: list[ParsedRow], req: ImportRequest) -> dict[str, Any]:
    new_cars = sorted({r.car_name for r in rows if r.status != "error" and r.values.get("car_id") is None and r.car_name}, key=str.casefold)
    importable = [r for r in rows if r.status == "ok" or (r.status == "duplicate" and not req.skip_duplicates)]
    return {
        "total": len(rows),
        "ok": sum(r.status == "ok" for r in rows),
        "errors": sum(r.status == "error" for r in rows),
        "duplicates": sum(r.status == "duplicate" for r in rows),
        "to_import": len(importable),
        "kwh": round(sum(r.values["kwh"] for r in importable), 3),
        "new_cars": new_cars,
    }


def import_rows(db: Session, req: ImportRequest, scope: set[int] | None = None, user_id: int | None = None) -> dict[str, Any]:
    rows = parse_rows(db, req, scope)
    info = summary(rows, req)
    created: dict[str, Car] = {}
    for name in info["new_cars"]:
        created[norm(name)] = Car(name=name[:100])
        db.add(created[norm(name)])
    db.flush()
    count = 0
    for r in rows:
        if r.status == "error" or (r.status == "duplicate" and req.skip_duplicates):
            continue
        values = dict(r.values)
        if values["car_id"] is None:
            values["car_id"] = created[norm(r.car_name or "")].id
        db.add(ChargeSession(**values, created_by_id=user_id))
        count += 1
    db.commit()
    return {**info, "imported": count, "cars_created": list(info["new_cars"])}


def row_out(r: ParsedRow) -> dict[str, Any]:
    v = r.values
    return {
        "row": r.row,
        "status": r.status,
        "errors": r.errors,
        "warnings": r.warnings,
        "car_name": r.car_name,
        "charge_date": v.get("charge_date").isoformat() if v.get("charge_date") else None,
        "odometer": v.get("odometer"),
        "location": v.get("location"),
        "provider": v.get("provider"),
        "kwh": v.get("kwh"),
        "start_time": v["start_time"].strftime("%H:%M") if v.get("start_time") else None,
        "end_time": v["end_time"].strftime("%H:%M") if v.get("end_time") else None,
        "to_reimburse": v.get("to_reimburse"),
        "reimbursement_status": v.get("reimbursement_status"),
        "charger_name": r.charger_name,
        "cost": v.get("cost"),
        "notes": v.get("notes"),
    }
