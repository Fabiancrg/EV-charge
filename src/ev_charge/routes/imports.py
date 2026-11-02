"""Import API: read a spreadsheet, preview the mapped rows, import them."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..importer import ImportRequest, import_rows, parse_rows, row_out, summary
from ..spreadsheet import MAX_FILE_BYTES, SpreadsheetError, read_spreadsheet

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/import", tags=["import"])


@router.post("/sheet")
async def read_sheet(file: UploadFile = File(...)) -> dict[str, Any]:
    """Read an Excel workbook (.xlsx/.xlsm) or a CSV file into rows of text cells; nothing is stored."""
    content = await file.read(MAX_FILE_BYTES + 1)
    try:
        sheets = read_spreadsheet(file.filename or "upload.xlsx", content)
    except SpreadsheetError as e:
        raise HTTPException(422, str(e)) from None
    except Exception:
        logger.exception("Cannot read spreadsheet %r", file.filename)
        raise HTTPException(422, "Cannot read this file: is it a valid .xlsx or .csv file?") from None
    return {"filename": file.filename, "sheets": [s.as_dict() for s in sheets]}


def _check(req: ImportRequest) -> None:
    if "car" not in req.mapping and req.car_id is None:
        raise HTTPException(422, "Choose a car, or map the column that holds the car")


@router.post("/sessions/preview")
def preview(req: ImportRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    _check(req)
    rows = parse_rows(db, req)
    return {"summary": summary(rows, req), "rows": [row_out(r) for r in rows]}


@router.post("/sessions")
def do_import(req: ImportRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    _check(req)
    return import_rows(db, req)
