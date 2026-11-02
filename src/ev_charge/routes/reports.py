"""Report data API."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..reports import build_report

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("")
def report(
    car_id: list[int] | None = Query(default=None),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return build_report(db, car_id, date_from, date_to)
