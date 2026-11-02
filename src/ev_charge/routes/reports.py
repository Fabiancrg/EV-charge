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

router = APIRouter(prefix="/api/reports", tags=["reports"])

_COLUMNS = [
    ("charge_date", "Date"), ("car_name", "Car"), ("license_plate", "Plate"), ("odometer", "Odometer (km)"), ("location", "Location"),
    ("provider", "Provider"), ("start_time", "Start"), ("end_time", "End"), ("kwh", "kWh"), ("price_per_kwh", "Price/kWh"),
    ("home", "Home tariff"), ("amount", "Amount"), ("notes", "Notes"),
]  # fmt: skip


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
    fmt: Literal["json", "csv", "xlsx"] = "json",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any] | Response:
    data = build_reimbursement(db, restrict_car_ids(user, car_id), date_from, date_to)
    data["prepared_by"] = user.full_name or user.username
    data["currency"] = CONFIG.currency
    if fmt == "json":
        return data
    period = f"{data['date_from'] or 'start'}_{data['date_to'] or date.today().isoformat()}"
    values = [[("yes" if r[k] else "") if k == "home" else r[k] for k, _ in _COLUMNS] for r in data["rows"]]
    total = ["Total", "", "", "", "", "", "", "", data["totals"]["kwh"], "", "", data["totals"]["amount"], ""]
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
        ws.cell(r, 9).number_format = "0.00"
        ws.cell(r, 10).number_format = "0.0000"
        ws.cell(r, 12).number_format = "#,##0.00"
    ws.append(total)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    ws.cell(ws.max_row, 12).number_format = f'#,##0.00 "{CONFIG.currency}"'
    for col, width in zip("ABCDEFGHIJKLM", (12, 18, 12, 13, 22, 16, 7, 7, 9, 10, 10, 12, 30), strict=True):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="reimbursement-{period}.xlsx"'},
    )
