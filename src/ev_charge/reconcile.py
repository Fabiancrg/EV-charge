"""Reimbursement documents: read the file sent by the reimbursing company and match its sessions with ours.

Two formats are read:

- **Eneco eMobility** — the CSV "Credit Reimbursement Document" (one row per session, `;`-separated);
- **Last Mile Solutions** (50five…) — the PDF "Document de remboursement" and its annex of sessions.

A document line is matched with the session that started at (nearly) the same time, or, for a session
without a start time, on the same day with the same energy; then with several sessions when the company
grouped them in one line. Documents may be written in another time zone
(UTC) than the sessions: the whole-hour shift that matches best is found first.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .i18n import msg
from .models import ChargeSession

MAX_FILE_BYTES = 10 * 1024 * 1024


class DocumentError(ValueError):
    """The file can't be read as a reimbursement document (the message is shown to the user)."""


@dataclass
class DocLine:
    external_id: str | None
    started_at: datetime
    ended_at: datetime | None
    kwh: float
    amount: float


@dataclass
class ParsedDocument:
    source: str  # "eneco" | "lms"
    number: str
    issuer: str | None
    document_date: date | None
    due_date: date | None
    period: str | None  # "YYYY-MM"
    amount: float
    kwh: float
    lines: list[DocLine] = field(default_factory=list)


def _num(text: str) -> float:
    """'1.234,56' / '1,234.56' / '-3,41' → float (the last separator is the decimal one)."""
    t = text.strip().replace(" ", "").replace(" ", "")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    else:
        t = t.replace(",", ".")
    return float(t)


# ── Eneco eMobility CSV ──────────────────────────────────────────────────────

_ENECO_COLUMNS = ("Invoice Number", "Session Start Date", "Session Start Time", "Volume", "Item Amount Incl")


def _dmy(text: str) -> date | None:
    text = text.strip()
    return datetime.strptime(text, "%d-%m-%Y").date() if text else None


def _dmy_hms(day: str, clock: str) -> datetime | None:
    d = _dmy(day)
    if d is None:
        return None
    clock = clock.strip() or "00:00:00"
    return datetime.combine(d, datetime.strptime(clock, "%H:%M:%S" if clock.count(":") == 2 else "%H:%M").time())


def parse_eneco_csv(content: bytes) -> ParsedDocument:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    if not rows or any(c not in rows[0] for c in _ENECO_COLUMNS):
        raise DocumentError(msg("This CSV file isn't an Eneco eMobility reimbursement document (columns such as 'Invoice Number' and 'Session Start Date' are missing)."))
    if len({r["Invoice Number"] for r in rows}) > 1:
        raise DocumentError(msg("This CSV file holds several documents: import one document at a time."))
    first = rows[0]
    lines = []
    for n, r in enumerate(rows, start=2):
        try:
            start = _dmy_hms(r["Session Start Date"], r["Session Start Time"])
            if start is None:
                raise ValueError("no start date")
            end = _dmy_hms(r.get("Session End Date") or "", r.get("Session End Time") or "")
            # A reimbursement is a credit: amounts are negative in the file
            lines.append(DocLine((r.get("Item ID") or "").strip() or None, start, end, _num(r["Volume"]), -_num(r["Item Amount Incl"]) + 0.0))
        except (ValueError, KeyError) as e:
            raise DocumentError(msg("Row {n} of the CSV file can't be read ({error}).", n=n, error=e)) from None
    periods = {(r.get("Session Period") or "").strip() for r in rows}
    period = next(iter(periods)) if len(periods) == 1 else ""
    total = (first.get("Invoice Amount Incl") or "").strip()
    return ParsedDocument(
        source="eneco",
        number=first["Invoice Number"].strip(),
        issuer=(first.get("Invoice From Name") or "").strip() or "Eneco eMobility",
        document_date=_dmy(first.get("Invoice Date") or ""),
        due_date=_dmy(first.get("Invoice Due Date") or ""),
        period=f"{period[:4]}-{period[4:6]}" if re.fullmatch(r"\d{6}", period) else None,
        amount=round(-_num(total) + 0.0 if total else sum(x.amount for x in lines), 2),
        kwh=round(sum(x.kwh for x in lines), 3),
        lines=lines,
    )


# ── Last Mile Solutions PDF ──────────────────────────────────────────────────

# Month names and abbreviations in French, Dutch, English and German (accents and dots removed)
_MONTHS = {
    **{k: 1 for k in ("jan", "janv", "janvier", "januari", "january", "januar")},
    **{k: 2 for k in ("feb", "fev", "fevr", "fevrier", "februari", "february", "februar")},
    **{k: 3 for k in ("mar", "mars", "mrt", "maart", "march", "marz", "maerz")},
    **{k: 4 for k in ("apr", "avr", "avril", "april")},
    **{k: 5 for k in ("may", "mai", "mei")},
    **{k: 6 for k in ("jun", "juin", "juni", "june")},
    **{k: 7 for k in ("jul", "juil", "juill", "juillet", "juli", "july")},
    **{k: 8 for k in ("aug", "aou", "aout", "augustus", "august")},
    **{k: 9 for k in ("sep", "sept", "septembre", "september")},
    **{k: 10 for k in ("oct", "okt", "octobre", "oktober", "october")},
    **{k: 11 for k in ("nov", "novembre", "november")},
    **{k: 12 for k in ("dec", "dez", "decembre", "december", "dezember")},
}
_WORD = r"([^\W\d_]+)\.?"
_DOC_DATE = re.compile(rf"\b(\d{{1,2}})-{_WORD}-(\d{{4}})\b")
_DOC_PERIOD = re.compile(rf"(?<![\d-]){_WORD}-(\d{{4}})\b")
_SESSION_TIME = re.compile(rf"\b(\d{{1,2}})-{_WORD} (\d{{1,2}}):(\d{{2}})\b")
_SESSION_ID = re.compile(r"^\s*(\d{6,12})\s*$", re.MULTILINE)
_KWH = re.compile(r"(\d[\d.,]*) ?kWh\b(?! */)")
_EUR = re.compile(r"(?:EUR|€) ?(\d[\d.,]*)")
_SUMMARY = re.compile(r"(\d[\d.,]*) ?kWh (?:EUR|€) ?(\d[\d.,]*)")


def _month(word: str) -> int | None:
    plain = "".join(c for c in unicodedata.normalize("NFD", word.lower()) if unicodedata.category(c) != "Mn")
    return _MONTHS.get(plain)


def _pdf_text(content: bytes) -> str:
    import pypdf
    from pypdf.errors import PyPdfError

    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PyPdfError, ValueError, OSError) as e:
        raise DocumentError(msg("This PDF file can't be read ({error}).", error=e)) from None


def parse_lms_text(text: str) -> ParsedDocument:
    """The text of a Last Mile Solutions reimbursement document: a summary page, then one block per session
    (id, start and end as '01-oct. 17:34', energy, price per kWh, total)."""
    ids = list(_SESSION_ID.finditer(text))
    if not ids:
        raise DocumentError(msg("No charging session was found in this PDF: only reimbursement documents of Last Mile Solutions (50five…) are read."))
    head = text[: ids[0].start()]
    number = re.search(r"\b\d{12,}\b", head)
    summary = _SUMMARY.search(head)
    if number is None or summary is None:
        raise DocumentError(msg("The document number or its total wasn't found in this PDF."))
    document_date = next((date(int(y), m, int(d)) for d, w, y in _DOC_DATE.findall(head) if (m := _month(w))), None)
    period = next(((int(y), m) for w, y in _DOC_PERIOD.findall(head) if (m := _month(w))), None)
    ref = period or ((document_date.year, document_date.month) if document_date else None)
    if ref is None:
        raise DocumentError(msg("The date of this document wasn't found: the year of its sessions is unknown."))

    def moment(day: str, word: str, hour: str, minute: str) -> datetime:
        month = _month(word)
        if month is None:
            raise ValueError(f"unknown month '{word}'")
        # The annex gives no year: the session is in the month of the document, or just before/after it
        year = ref[0] - 1 if month - ref[1] > 6 else ref[0] + 1 if ref[1] - month > 6 else ref[0]
        return datetime(year, month, int(day), int(hour), int(minute))

    lines = []
    for i, m in enumerate(ids):
        block = text[m.end() : ids[i + 1].start() if i + 1 < len(ids) else len(text)]
        times, kwh, amounts = _SESSION_TIME.findall(block), _KWH.search(block), _EUR.findall(block)
        if not times or kwh is None or not amounts:
            continue  # a number alone on its line that isn't a session
        try:
            start = moment(*times[0])
            end = moment(*times[1]) if len(times) > 1 else None
            lines.append(DocLine(m.group(1), start, end, _num(kwh.group(1)), _num(amounts[-1])))
        except ValueError as e:
            raise DocumentError(msg("Session {number} of the PDF can't be read ({error}).", number=m.group(1), error=e)) from None
    if not lines:
        raise DocumentError(msg("No charging session was found in this PDF."))
    return ParsedDocument(
        source="lms",
        number=number.group(0),
        issuer="Last Mile Solutions" + (" (50five)" if "50five" in text else ""),
        document_date=document_date,
        due_date=None,
        period=f"{period[0]:04d}-{period[1]:02d}" if period else None,
        amount=_num(summary.group(2)),
        kwh=_num(summary.group(1)),
        lines=lines,
    )


def parse_document(filename: str, content: bytes) -> ParsedDocument:
    if not content:
        raise DocumentError(msg("The file is empty."))
    if len(content) > MAX_FILE_BYTES:
        raise DocumentError(msg("The file is too large (10 MB at most)."))
    if content.lstrip()[:5] == b"%PDF-":
        return parse_lms_text(_pdf_text(content))
    if filename.lower().endswith((".csv", ".txt")) or b";" in content[:2000]:
        return parse_eneco_csv(content)
    raise DocumentError(msg("Unknown file: send the CSV of Eneco eMobility or the PDF of Last Mile Solutions (50five)."))


# ── Matching ─────────────────────────────────────────────────────────────────

TIME_WINDOW_MIN = 60  # a session started within an hour of the line is the same session
EXACT_MIN = 15
_OFFSETS = (0, 60, 120, -60, -120)  # minutes added to the document's times (UTC documents, local sessions)


def kwh_close(a: float, b: float) -> bool:
    """Same energy, allowing for rounding and for the small differences between two meters."""
    return abs(a - b) <= max(0.15, 0.05 * max(a, b))


def _session_start(s: ChargeSession) -> datetime | None:
    return datetime.combine(s.charge_date, s.start_time) if s.start_time else None


def _pairs(lines: list[DocLine], sessions: list[ChargeSession], offset: int) -> list[tuple[float, int, int, bool]]:
    """(score, line index, session index, exact) of every plausible pair; the lower the score the better."""
    shift = timedelta(minutes=offset)
    by_day: dict[date, list[int]] = {}
    for j, s in enumerate(sessions):
        by_day.setdefault(s.charge_date, []).append(j)
    out = []
    for i, line in enumerate(lines):
        start = line.started_at + shift
        days = {start.date() + timedelta(days=d) for d in (-1, 0, 1)}
        for j in (j for d in days for j in by_day.get(d, ())):
            s = sessions[j]
            close = kwh_close(line.kwh, s.kwh)
            penalty = 200 * min(abs(line.kwh - s.kwh) / max(line.kwh, s.kwh, 0.1), 1.0)
            began = _session_start(s)
            if began is not None:
                gap = abs((began - start).total_seconds()) / 60
                if gap <= TIME_WINDOW_MIN or (close and began.date() == start.date()):
                    out.append((gap + penalty, i, j, gap <= EXACT_MIN and close))
            elif close and s.charge_date in (start.date(), (line.ended_at + shift).date() if line.ended_at else start.date()):
                out.append((120 + penalty, i, j, False))  # no start time: the day and the energy
    return out


def _assign(pairs: list[tuple[float, int, int, bool]]) -> tuple[dict[int, int], int]:
    result: dict[int, int] = {}
    taken: set[int] = set()
    exact = 0
    for _score, i, j, is_exact in sorted(pairs):
        if i not in result and j not in taken:
            result[i] = j
            taken.add(j)
            exact += is_exact
    return result, exact


GROUP_MARGIN = timedelta(minutes=15)


def _grouped(lines: list[DocLine], sessions: list[ChargeSession], result: dict[int, int], offset: int) -> dict[int, list[int]]:
    """Lines the company made of several sessions: when a line has no session, or one with less energy, the free
    sessions started between its start and its end are added if together they have the energy of the line."""
    shift = timedelta(minutes=offset)
    taken = set(result.values())
    groups = {i: [j] for i, j in result.items()}
    for i, line in enumerate(lines):
        single = result.get(i)
        if line.ended_at is None or (single is not None and kwh_close(line.kwh, sessions[single].kwh)):
            continue
        first, last = line.started_at + shift - GROUP_MARGIN, line.ended_at + shift + GROUP_MARGIN
        inside = [j for j, s in enumerate(sessions) if j not in taken and (began := _session_start(s)) is not None and first <= began <= last]
        group = sorted(inside + ([single] if single is not None else []))
        if len(group) > 1 and kwh_close(line.kwh, sum(sessions[j].kwh for j in group)):
            groups[i] = group
            taken.update(group)
    return groups


def match_lines(lines: list[DocLine], sessions: list[ChargeSession]) -> tuple[dict[int, list[ChargeSession]], int]:
    """Match document lines with sessions: {line index: its session(s)} and the time shift (minutes) applied to the document."""
    best: tuple[int, int, dict[int, int], int] | None = None
    for offset in _OFFSETS:
        result, exact = _assign(_pairs(lines, sessions, offset))
        if best is None or (exact, len(result)) > (best[0], best[1]):
            best = (exact, len(result), result, offset)
    assert best is not None
    groups = _grouped(lines, sessions, best[2], best[3])
    return {i: [sessions[j] for j in group] for i, group in groups.items()}, best[3]


def main_charger(matches: dict[int, list[ChargeSession]]) -> int | None:
    """The charger most of the matched sessions were made with."""
    counts = Counter(s.charger_id for group in matches.values() for s in group if s.charger_id)
    return counts.most_common(1)[0][0] if counts else None
