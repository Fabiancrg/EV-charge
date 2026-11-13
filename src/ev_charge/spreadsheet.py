"""
Minimal spreadsheet reader for the session import wizard (.xlsx / .xlsm / .csv).
Ported from Flow Manager (keep the two in step).

Reads every sheet into a grid of display strings (openpyxl for workbooks,
the csv module for CSV). Column mapping happens in the browser and value
parsing in importer.py; this module only turns the file into text cells:

- strings, numbers (integers without ".0"), booleans
- dates and times using the cell's number format (``2026-10-06``, ``14:30``,
  ``2026-10-06 14:30``; duration formats such as ``[h]:mm`` give ``H:MM``)
- line breaks inside a cell (Alt+Enter) are kept as "\n", whatever the source
  (LF, CR LF, CR or Excel's ``_x000D_`` escape), so lists can be one item per line
- merged cells: every cell of a merged range gets the top-left value
  (e.g. a date merged across the rows of one day is repeated on each row)
"""

from __future__ import annotations

import csv
import io
import posixpath
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import openpyxl
from openpyxl.utils.escape import unescape
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.worksheet import Worksheet

from .i18n import msg

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PART_BYTES = 200 * 1024 * 1024  # uncompressed size of one XML part (zip-bomb guard)
MAX_ROWS = 5000
MAX_COLS = 100


class SpreadsheetError(ValueError):
    """The file cannot be read as a spreadsheet."""


def _clean_text(text: str) -> str:
    """Trim a cell and normalise its line breaks to "\n" (Alt+Enter in Excel)."""
    if "_x" in text:
        text = unescape(text)  # Excel escapes such as _x000D_ (carriage return)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\x0b", "\n")
    return "\n".join(line.strip() for line in text.split("\n")).strip()


@dataclass
class Sheet:
    name: str
    rows: list[list[str]] = field(default_factory=list)
    hidden: bool = False
    truncated: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "hidden": self.hidden, "truncated": self.truncated, "rows": self.rows}


def read_spreadsheet(filename: str, content: bytes) -> list[Sheet]:
    """Read a workbook (.xlsx, .xlsm) or a .csv file into sheets of text rows."""
    if len(content) > MAX_FILE_BYTES:
        raise SpreadsheetError(msg("File too large (max {size} MB)", size=MAX_FILE_BYTES // (1024 * 1024)))
    ext = posixpath.splitext(filename.lower())[1]
    if ext in (".xls", ".xlsb", ".ods", ".numbers"):
        raise SpreadsheetError(msg("'{ext}' files are not supported: save the workbook as .xlsx (Excel Workbook) or .csv", ext=ext))
    if ext == ".csv" or (ext not in (".xlsx", ".xlsm") and not zipfile.is_zipfile(io.BytesIO(content))):
        return [_read_csv(posixpath.splitext(posixpath.basename(filename))[0] or "CSV", content)]
    return _read_xlsx(content)


# ── CSV ──────────────────────────────────────────────────────────────────────


def _read_csv(name: str, content: bytes) -> Sheet:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost anything
        raise SpreadsheetError(msg("Cannot decode the CSV file (use UTF-8)"))
    sheet = Sheet(name=name)
    for row in csv.reader(io.StringIO(text, newline=""), delimiter=_csv_delimiter(text)):
        if len(sheet.rows) >= MAX_ROWS:
            sheet.truncated = True
            break
        sheet.rows.append([_clean_text(c) for c in row[:MAX_COLS]])
    _trim(sheet)
    return sheet


def _csv_delimiter(text: str) -> str:
    """Delimiter of a CSV file: the sniffer's guess when the header line agrees, else the most frequent candidate in it."""
    candidates = ",;\t|"
    header = text.lstrip("\r\n").splitlines()[0] if text.strip() else ""
    try:
        sniffed = csv.Sniffer().sniff(text[:20000], delimiters=candidates).delimiter
        if sniffed in header:
            return sniffed
    except csv.Error:
        pass
    counts = {d: header.count(d) for d in candidates}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


# ── XLSX ─────────────────────────────────────────────────────────────────────


def _format_number(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return format(value, ".10g")


def _hours_minutes(total_seconds: float) -> str:
    minutes = round(total_seconds / 60)
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def _cell_text(value: Any) -> str:
    """Display text of a cell value as converted by openpyxl (dates/times come from the number format)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return _format_number(value)
    if isinstance(value, datetime):
        value = value.replace(microsecond=0) + timedelta(seconds=round(value.microsecond / 1e6))
        return value.strftime("%Y-%m-%d") if value.time() == time(0) else value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):  # time of day / short duration ("h:mm" format)
        return _hours_minutes(value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1e6)
    if isinstance(value, timedelta):  # elapsed-time formats such as "[h]:mm"
        return _hours_minutes(value.total_seconds())
    return _clean_text(str(value))


def _read_xlsx(content: bytes) -> list[Sheet]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            if any(info.file_size > MAX_PART_BYTES for info in zf.infolist()):
                raise SpreadsheetError(msg("The workbook is too large once uncompressed"))
    except zipfile.BadZipFile as exc:
        raise SpreadsheetError(msg("Not a valid .xlsx file")) from exc
    try:
        # Not read-only: merged cell ranges are only available on full worksheets
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, keep_links=False)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError) as exc:
        raise SpreadsheetError(msg("Not a valid .xlsx file")) from exc
    try:
        sheets = [_read_worksheet(ws) for ws in wb.worksheets]
    finally:
        wb.close()
    if not sheets:
        raise SpreadsheetError(msg("The workbook has no worksheet"))
    return sheets


def _read_worksheet(ws: Worksheet) -> Sheet:
    sheet = Sheet(name=ws.title, hidden=ws.sheet_state != "visible")
    # Only the cells stored in the file: iter_rows() would create an empty cell for every
    # coordinate of the used range, which can be huge for sheets formatted down to row 1048576.
    grid: dict[tuple[int, int], str] = {}
    for (r, c), cell in ws._cells.items():
        if cell.value is None or c > MAX_COLS:
            continue
        if r > MAX_ROWS:
            sheet.truncated = True
            continue
        text = _cell_text(cell.value)
        if text:
            grid[(r - 1, c - 1)] = text
    # Merged cells: openpyxl keeps the value in the top-left cell only; repeat it over the range
    for rng in ws.merged_cells.ranges:
        value = grid.get((rng.min_row - 1, rng.min_col - 1))
        if not value:
            continue
        for r in range(rng.min_row - 1, min(rng.max_row, MAX_ROWS)):
            for c in range(rng.min_col - 1, min(rng.max_col, MAX_COLS)):
                grid.setdefault((r, c), value)
    if grid:
        height, width = max(r for r, _ in grid) + 1, max(c for _, c in grid) + 1
        sheet.rows = [[grid.get((r, c), "") for c in range(width)] for r in range(height)]
    _trim(sheet)
    return sheet


def _trim(sheet: Sheet) -> None:
    """Drop trailing empty rows and columns."""
    while sheet.rows and not any(sheet.rows[-1]):
        sheet.rows.pop()
    width = max((i + 1 for row in sheet.rows for i, v in enumerate(row) if v), default=0)
    sheet.rows = [(row + [""] * width)[:width] for row in sheet.rows]
