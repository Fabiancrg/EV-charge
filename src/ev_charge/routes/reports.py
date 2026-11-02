"""Report data API: charts and the reimbursement statement (JSON, CSV or Excel)."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..auth import current_user, restrict_car_ids
from ..config import CONFIG
from ..database import get_db
from ..models import User
from ..reports import build_reimbursement, build_report
from ..schemas import ReimbursementStatus

router = APIRouter(prefix="/api/reports", tags=["reports"])

_COLUMNS = [
    ("charge_date", "Date"), ("car_name", "Car"), ("license_plate", "Plate"), ("odometer", "Odometer (km)"), ("location", "Location"),
    ("charger_name", "Charger"), ("reimbursement_company", "Reimbursed by"), ("start_time", "Start"), ("end_time", "End"),
    ("kwh", "kWh"), ("price_per_kwh", "Price/kWh"), ("home", "Home tariff"), ("amount", "Amount"), ("status", "Status"), ("notes", "Notes"),
]  # fmt: skip
_STATUS_LABELS = {"in_progress": "In progress", "not_recorded": "Not recorded", "recorded": "Recorded", "invoiced": "Invoiced", "paid": "Paid"}


@router.get("")
def report(
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return build_report(db, restrict_car_ids(user, car_id), date_from, date_to)


@router.get("/reimbursement", response_model=None)
def reimbursement(
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    status: list[ReimbursementStatus] | None = Query(default=None),
    company: str | None = Query(default=None, description="Reimbursing company of the charger; empty: sessions without a charger"),
    fmt: Literal["json", "csv", "xlsx"] = "json",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any] | Response:
    data = build_reimbursement(db, restrict_car_ids(user, car_id), date_from, date_to, status, company)
    data["prepared_by"] = user.full_name or user.username
    data["currency"] = CONFIG.currency
    if fmt == "json":
        return data
    period = f"{data['date_from'] or 'start'}_{data['date_to'] or date.today().isoformat()}"

    def cell(r: dict[str, Any], k: str) -> Any:
        if k == "home":
            return "yes" if r[k] else ""
        return _STATUS_LABELS.get(r[k], r[k]) if k == "status" else r[k]

    values = [[cell(r, k) for k, _ in _COLUMNS] for r in data["rows"]]
    keys = [k for k, _ in _COLUMNS]
    total: list[Any] = [""] * len(keys)
    total[0], total[keys.index("kwh")], total[keys.index("amount")] = "Total", data["totals"]["kwh"], data["totals"]["amount"]
    col = {k: i + 1 for i, k in enumerate(keys)}  # 1-based Excel columns
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow([label for _, label in _COLUMNS])
        writer.writerows(values)
        writer.writerow(total)
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="reimbursement-{period}.csv"'})

    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reimbursement"
    ws.append(["Charging costs to reimburse"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Period", f"{data['date_from'] or '…'} → {data['date_to'] or '…'}"])
    ws.append(["Car(s)", ", ".join(f"{c['name']}{' (' + c['license_plate'] + ')' if c['license_plate'] else ''}" for c in data["cars"])])
    if company is not None:
        ws.append(["Reimbursed by", company or "(no charger)"])
    if status:
        ws.append(["Status", ", ".join(_STATUS_LABELS[s] for s in status)])
    ws.append(["Prepared by", data["prepared_by"], "on", date.today().isoformat()])
    ws.append([])
    ws.append([label for _, label in _COLUMNS])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    first = ws.max_row + 1
    for row in values:
        ws.append(row)
    for r in range(first, ws.max_row + 1):
        ws.cell(r, 1).number_format = "yyyy-mm-dd"
        ws.cell(r, 1).value = date.fromisoformat(ws.cell(r, 1).value)
        ws.cell(r, col["kwh"]).number_format = "0.00"
        ws.cell(r, col["price_per_kwh"]).number_format = "0.0000"
        ws.cell(r, col["amount"]).number_format = "#,##0.00"
    ws.append(total)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    ws.cell(ws.max_row, col["amount"]).number_format = f'#,##0.00 "{CONFIG.currency}"'
    for letter, width in zip("ABCDEFGHIJKLMNO", (12, 18, 12, 13, 22, 20, 16, 7, 7, 9, 10, 10, 12, 13, 30), strict=True):
        ws.column_dimensions[letter].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="reimbursement-{period}.xlsx"'},
    )
