"""Report data API: charts and the reimbursement statement (JSON, CSV or Excel)."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from ..auth import Principal, current_user, restrict_car_ids
from ..config import CONFIG
from ..database import get_db
from ..i18n import language_of, localize, msg
from ..i18n import translate as tr
from ..reports import build_reimbursement, build_report
from ..schemas import ReimbursementStatus

router = APIRouter(prefix="/api/reports", tags=["reports"])

_COLUMNS = [
    ("charge_date", msg("Date")), ("car_name", msg("Car")), ("license_plate", msg("Plate")), ("odometer", msg("Odometer (km)")), ("location", msg("Location")),
    ("charger_name", msg("Charger")), ("reimbursement_company", msg("Reimbursed by")), ("start_time", msg("Start")), ("end_time", msg("End")),
    ("kwh", "kWh"), ("price_per_kwh", msg("Price/kWh")), ("home", msg("Home tariff")), ("amount", msg("Amount")), ("status", msg("Status")), ("notes", msg("Notes")),
]  # fmt: skip
_STATUS_LABELS = {"in_progress": msg("In progress"), "not_recorded": msg("Not recorded"), "recorded": msg("Recorded"), "invoiced": msg("Invoiced"), "paid": msg("Paid")}


@router.get("")
def report(
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    user: Principal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return build_report(db, restrict_car_ids(user, car_id), date_from, date_to)


@router.get("/reimbursement", response_model=None)
def reimbursement(
    request: Request,
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    status: list[ReimbursementStatus] | None = Query(default=None),
    company: str | None = Query(default=None, description="Reimbursing company of the charger; empty: sessions without a charger"),
    fmt: Literal["json", "csv", "xlsx"] = "json",
    user: Principal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any] | Response:
    data = build_reimbursement(db, restrict_car_ids(user, car_id), date_from, date_to, status, company)
    data["prepared_by"] = user.full_name or user.username
    data["currency"] = CONFIG.currency
    if fmt == "json":
        return data
    lang = language_of(request, user.user)
    columns = [(k, localize(lang, label)) for k, label in _COLUMNS]
    statuses = localize(lang, _STATUS_LABELS)
    period = f"{data['date_from'] or 'start'}_{data['date_to'] or date.today().isoformat()}"

    def cell(r: dict[str, Any], k: str) -> Any:
        if k == "home":
            return tr(lang, "yes") if r[k] else ""
        return statuses.get(r[k], r[k]) if k == "status" else r[k]

    values = [[cell(r, k) for k, _ in columns] for r in data["rows"]]
    keys = [k for k, _ in columns]
    total: list[Any] = [""] * len(keys)
    total[0], total[keys.index("kwh")], total[keys.index("amount")] = tr(lang, "Total"), data["totals"]["kwh"], data["totals"]["amount"]
    col = {k: i + 1 for i, k in enumerate(keys)}  # 1-based Excel columns
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow([label for _, label in columns])
        writer.writerows(values)
        writer.writerow(total)
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="reimbursement-{period}.csv"'})

    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = tr(lang, "Reimbursement")
    ws.append([tr(lang, "Charging costs to reimburse")])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([tr(lang, "Period"), f"{data['date_from'] or '…'} → {data['date_to'] or '…'}"])
    ws.append([tr(lang, "Car(s)"), ", ".join(f"{c['name']}{' (' + c['license_plate'] + ')' if c['license_plate'] else ''}" for c in data["cars"])])
    if company is not None:
        ws.append([tr(lang, "Reimbursed by"), company or tr(lang, "(no charger)")])
    if status:
        ws.append([tr(lang, "Status"), ", ".join(statuses[s] for s in status)])
    ws.append([tr(lang, "Prepared by"), data["prepared_by"], tr(lang, "on"), date.today().isoformat()])
    ws.append([])
    ws.append([label for _, label in columns])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    first = ws.max_row + 1
    for row in values:
        ws.append(row)
    for r in range(first, ws.max_row + 1):
        ws.cell(r, 1).number_format = "yyyy-mm-dd"
        ws.cell(r, 1).value = date.fromisoformat(ws.cell(r, 1).value)
        ws.cell(r, col["kwh"]).number_format = "0.000"
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
