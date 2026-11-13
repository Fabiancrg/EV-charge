"""Reimbursement documents API: reconcile the document of a reimbursing company with our sessions
(they become "invoiced"), then follow the payments to receive (they become "paid")."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..auth import Principal, car_scope, current_user, require, visible_groups
from ..config import CONFIG
from ..database import get_db
from ..models import Car, Charger, ChargeSession, ReimbursementDocument, ReimbursementLine
from ..pricing import Pricing, get_setting, set_setting
from ..reconcile import MAX_FILE_BYTES, DocumentError, ParsedDocument, kwh_close, main_charger, match_lines, parse_document
from ..schemas import default_status
from ..services import session_sort_key

router = APIRouter(prefix="/api/reimbursement-documents", tags=["reimbursement documents"])


def manager(user: Principal = Depends(current_user)) -> Principal:
    """Reimbursement documents are for those who follow the reimbursements of at least one car."""
    if car_scope(user, "reimbursement:manage") == set():
        raise HTTPException(403, "Permission needed: reimbursement:manage")
    return user


class PaymentIn(BaseModel):
    paid_date: date | None = None  # default: today
    paid_amount: float | None = Field(default=None, ge=0)  # default: the amount of the document
    notes: str | None = Field(default=None, max_length=2000)


class LineSessions(BaseModel):
    session_ids: list[int] = Field(max_length=50)  # the sessions of the line (usually one); empty: unlink


class SettingsIn(BaseModel):
    ignore_below: float = Field(ge=0, le=1000)


class NewSession(BaseModel):
    car_id: int | None = None  # default: the car of the charger, else the car of the document's other sessions


class ChargerIn(BaseModel):
    charger_id: int


# ── Reading ──────────────────────────────────────────────────────────────────


def _documents(db: Session, user: Principal) -> list[ReimbursementDocument]:
    """The documents the caller may see: those whose matched sessions are all on cars they manage."""
    scope = car_scope(user, "reimbursement:manage")
    docs = db.scalars(
        select(ReimbursementDocument)
        .options(
            selectinload(ReimbursementDocument.lines).selectinload(ReimbursementLine.sessions).options(joinedload(ChargeSession.car), joinedload(ChargeSession.charger)),
            joinedload(ReimbursementDocument.charger),
        )
        .order_by(ReimbursementDocument.document_date.desc(), ReimbursementDocument.id.desc())
    ).all()
    if scope is None:
        return list(docs)

    def visible(d: ReimbursementDocument) -> bool:
        cars = {s.car_id for ln in d.lines for s in ln.sessions}
        return cars <= scope and (bool(cars) or d.created_by_id == user.id)

    return [d for d in docs if visible(d)]


def _get(db: Session, document_id: int, user: Principal) -> ReimbursementDocument:
    doc = next((d for d in _documents(db, user) if d.id == document_id), None)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


def _document_out(d: ReimbursementDocument, today: date, below: float) -> dict[str, Any]:
    paid = d.paid_date is not None
    return {
        "id": d.id, "source": d.source, "number": d.number, "issuer": d.issuer, "document_date": d.document_date, "due_date": d.due_date,
        "period": d.period, "amount": d.amount, "kwh": d.kwh, "filename": d.filename, "charger_id": d.charger_id,
        "charger_name": d.charger.name if d.charger else None, "status": "paid" if paid else "to_receive",
        "overdue": not paid and d.due_date is not None and d.due_date < today, "paid_date": d.paid_date, "paid_amount": d.paid_amount,
        "difference": round(d.paid_amount - d.amount, 2) if paid and d.paid_amount is not None else None, "notes": d.notes,
        "lines": len(d.lines), "matched": sum(bool(ln.sessions) for ln in d.lines), "sessions": sum(len(ln.sessions) for ln in d.lines),
        "unmatched": sum(not ln.sessions and not _ignored(ln, below) for ln in d.lines), "created_at": d.created_at,
    }  # fmt: skip


def _session_brief(s: ChargeSession, pricing: Pricing) -> dict[str, Any]:
    return {
        "id": s.id, "charge_date": s.charge_date, "start_time": s.start_time, "end_time": s.end_time, "car_id": s.car_id, "car_name": s.car.name,
        "charger_id": s.charger_id, "charger_name": s.charger.name if s.charger else None, "kwh": s.kwh, "amount": pricing.price(s).cost,
        "status": s.reimbursement_status,
    }  # fmt: skip


IGNORE_BELOW = "reimbursement_ignore_below"  # setting: lines without a session below this amount need none


def ignore_below(db: Session) -> float:
    """Amount under which a line without a session is left aside. Default 0.01: only the lines of 0.00
    (the car was plugged in for a moment, a few Wh: nothing to reimburse). 0: no line is left aside."""
    return float(get_setting(db, IGNORE_BELOW, 0.01))


def _ignored(line: Any, below: float) -> bool:
    return round(line.amount, 2) < below


def _price_mismatch(line: Any, kwh: float, amount: float) -> bool:
    """The line isn't priced at our tariff: its energy at the price per kWh of our session(s) isn't its amount (to the cent).
    A small difference in energy between the two meters changes the amount by a few cents: that isn't a price difference."""
    return kwh > 0 and abs(line.kwh * amount / kwh - line.amount) >= 0.02


def _line_out(line: Any, sessions: list[ChargeSession], pricing: Pricing, index: int, below: float) -> dict[str, Any]:
    """line: a parsed DocLine (preview) or a stored ReimbursementLine; sessions: ours matched with it (several when the company grouped them)."""
    briefs = [_session_brief(s, pricing) for s in sorted(sessions, key=session_sort_key)]
    kwh = round(sum(b["kwh"] for b in briefs), 3)
    amount = round(sum(b["amount"] or 0 for b in briefs), 2)
    return {
        "id": getattr(line, "id", None), "index": index, "external_id": line.external_id, "started_at": line.started_at, "ended_at": line.ended_at,
        "kwh": line.kwh, "amount": line.amount, "sessions": briefs, "sessions_kwh": kwh if briefs else None, "sessions_amount": amount if briefs else None,
        "kwh_mismatch": bool(briefs) and not kwh_close(line.kwh, kwh),
        "amount_diff": round(amount - line.amount, 2) if briefs else None,
        "price_mismatch": _price_mismatch(line, kwh, amount) if briefs else False,
        "ignored": not briefs and _ignored(line, below),  # no session, and too small to need one
    }  # fmt: skip


def _summary(lines: list[dict[str, Any]], amount: float, below: float, offset: int | None = None) -> dict[str, Any]:
    matched = [ln for ln in lines if ln["sessions"]]
    return {
        "lines": len(lines), "matched": len(matched), "unmatched": sum(not ln["sessions"] and not ln["ignored"] for ln in lines),
        "ignored": sum(ln["ignored"] for ln in lines),
        "ignored_amount": round(sum(ln["amount"] for ln in lines if ln["ignored"]), 2), "sessions": sum(len(ln["sessions"]) for ln in matched),
        "grouped": sum(len(ln["sessions"]) > 1 for ln in matched),
        "document_amount": amount, "lines_amount": round(sum(ln["amount"] for ln in lines), 2),
        "matched_amount": round(sum(ln["amount"] for ln in matched), 2),
        "unmatched_amount": round(sum(ln["amount"] for ln in lines if not ln["sessions"] and not ln["ignored"]), 2),
        "sessions_amount": round(sum(ln["sessions_amount"] for ln in matched), 2),
        "kwh_mismatches": sum(ln["kwh_mismatch"] for ln in lines),
        "price_mismatches": sum(ln["price_mismatch"] for ln in lines),
        "time_offset_minutes": offset, "ignore_below": below,
    }  # fmt: skip


def _eligible(db: Session, user: Principal, charger_id: int | None, first: date, last: date) -> list[ChargeSession]:
    """Sessions a document line can be linked to: made with the charger (any charger when none is given), to be reimbursed,
    recorded, on no document yet, on a car the caller manages."""
    scope = car_scope(user, "reimbursement:manage")
    query = (
        select(ChargeSession)
        .options(joinedload(ChargeSession.car), joinedload(ChargeSession.charger))
        .where(ChargeSession.to_reimburse.is_(True), ChargeSession.reimbursement_line_id.is_(None))
        .where(ChargeSession.charge_date >= first - timedelta(days=1), ChargeSession.charge_date <= last + timedelta(days=1))
        .where(ChargeSession.charger_id == charger_id if charger_id is not None else ChargeSession.charger_id.is_not(None))
    )
    rows = [s for s in db.scalars(query) if s.reimbursement_status != "in_progress" and (scope is None or s.car_id in scope)]
    return sorted(rows, key=session_sort_key)


def _unlisted(sessions: list[ChargeSession], used: set[int], first: date, last: date, pricing: Pricing) -> list[dict[str, Any]]:
    """Our sessions of the document's period that aren't on it."""
    return [_session_brief(s, pricing) for s in sessions if s.id not in used and first <= s.charge_date <= last]


def _span(lines: list[Any]) -> tuple[date, date]:
    """First and last day covered by the lines."""
    return min(ln.started_at.date() for ln in lines), max((ln.ended_at or ln.started_at).date() for ln in lines)


def _charger(db: Session, user: Principal, charger_id: int) -> Charger:
    """A charger of a group the caller may see."""
    charger = db.get(Charger, charger_id)
    groups = visible_groups(user, db)
    if charger is None or (groups is not None and charger.group_id not in groups):
        raise HTTPException(422, "Unknown charger")
    return charger


def _analyse(db: Session, user: Principal, parsed: ParsedDocument, charger_id: int | None) -> tuple[dict[int, list[ChargeSession]], int, int | None, list[ChargeSession]]:
    """Match the lines; without a chosen charger, the one most matches were made with is kept and the match redone with it."""
    if charger_id is not None:
        _charger(db, user, charger_id)
    first, last = _span(parsed.lines)
    sessions = _eligible(db, user, charger_id, first, last)
    matches, offset = match_lines(parsed.lines, sessions)
    if charger_id is None and (charger_id := main_charger(matches)) is not None:
        sessions = [s for s in sessions if s.charger_id == charger_id]
        matches, offset = match_lines(parsed.lines, sessions)
    return matches, offset, charger_id, sessions


async def _read(file: UploadFile) -> ParsedDocument:
    content = await file.read(MAX_FILE_BYTES + 1)
    try:
        return parse_document(file.filename or "", content)
    except DocumentError as e:
        raise HTTPException(422, str(e)) from None


def _existing(db: Session, parsed: ParsedDocument) -> ReimbursementDocument | None:
    return db.scalar(select(ReimbursementDocument).where(ReimbursementDocument.source == parsed.source, ReimbursementDocument.number == parsed.number))


def _default_car(doc: ReimbursementDocument) -> int | None:
    """The car of a session created from a line: the charger's default car, else the car of most sessions of the document."""
    if doc.charger is not None and doc.charger.default_car_id:
        return doc.charger.default_car_id
    cars = Counter(s.car_id for ln in doc.lines for s in ln.sessions)
    return cars.most_common(1)[0][0] if cars else None


def _time_shift(doc: ReimbursementDocument) -> timedelta:
    """Whole hours between the document's clock and ours (a document written in UTC), seen on its matched sessions."""
    shifts: Counter[int] = Counter()
    for ln in doc.lines:
        for s in ln.sessions[:1]:
            if s.start_time is not None:
                shifts[round((datetime.combine(s.charge_date, s.start_time) - ln.started_at).total_seconds() / 3600)] += 1
    return timedelta(hours=shifts.most_common(1)[0][0]) if shifts else timedelta(0)


def _detail(db: Session, user: Principal, doc: ReimbursementDocument) -> dict[str, Any]:
    pricing = Pricing(db)
    below = ignore_below(db)
    lines = [_line_out(ln, ln.sessions, pricing, i, below) for i, ln in enumerate(doc.lines)]
    unlisted: list[dict[str, Any]] = []
    if doc.lines:
        first, last = _span(doc.lines)
        unlisted = _unlisted(_eligible(db, user, doc.charger_id, first, last), set(), first, last, pricing)
    return {
        "document": _document_out(doc, date.today(), below),
        "lines": lines,
        "unlisted_sessions": unlisted,
        "summary": _summary(lines, doc.amount, below),
        "default_car_id": _default_car(doc),
        "currency": CONFIG.currency,
    }


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("")
def list_documents(user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every document, newest first, and the totals of the payments to receive."""
    today = date.today()
    below = ignore_below(db)
    docs = [_document_out(d, today, below) for d in _documents(db, user)]

    def total(rows: list[dict[str, Any]], key: str = "amount") -> dict[str, Any]:
        return {"documents": len(rows), "amount": round(sum(r[key] or 0 for r in rows), 2)}

    waiting = [d for d in docs if d["status"] == "to_receive"]
    return {
        "documents": docs,
        "totals": {
            "to_receive": total(waiting),
            "overdue": total([d for d in waiting if d["overdue"]]),
            "received": total([d for d in docs if d["status"] == "paid"], "paid_amount"),
        },
        "ignore_below": below,
        "currency": CONFIG.currency,
    }


@router.put("/settings")
def update_settings(body: SettingsIn, _: Principal = Depends(require("settings:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Amount under which a line of a document without a session is left aside (no warning); 0: none is."""
    set_setting(db, IGNORE_BELOW, round(body.ignore_below, 2))
    db.commit()
    return {"ignore_below": ignore_below(db)}


@router.post("/preview")
async def preview(file: UploadFile = File(...), charger_id: int | None = None, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Read a document (Eneco CSV, Last Mile Solutions PDF) and match its lines with the sessions of a charger; nothing is stored.
    Without charger_id, the charger most matches were made with is proposed (document.charger_id)."""
    parsed = await _read(file)
    matches, offset, charger, sessions = _analyse(db, user, parsed, charger_id)
    pricing = Pricing(db)
    below = ignore_below(db)
    lines = [_line_out(ln, matches.get(i, []), pricing, i, below) for i, ln in enumerate(parsed.lines)]
    first, last = _span(parsed.lines)
    return {
        "document": {
            "source": parsed.source,
            "number": parsed.number,
            "issuer": parsed.issuer,
            "document_date": parsed.document_date,
            "due_date": parsed.due_date,
            "period": parsed.period,
            "amount": parsed.amount,
            "kwh": parsed.kwh,
            "filename": file.filename,
            "charger_id": charger,
        },  # fmt: skip
        "already_imported": _existing(db, parsed) is not None,
        "lines": lines,
        "unlisted_sessions": _unlisted(sessions, {s.id for group in matches.values() for s in group}, first, last, pricing),
        "summary": _summary(lines, parsed.amount, below, offset),
        "currency": CONFIG.currency,
    }


@router.post("", status_code=201)
async def create_document(file: UploadFile = File(...), charger_id: int | None = None, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Store the document of a charger; the sessions matched with its lines become "invoiced" and its amount a payment to receive."""
    if charger_id is None:
        raise HTTPException(422, "Choose the charger this document is about")
    parsed = await _read(file)
    if _existing(db, parsed) is not None:
        raise HTTPException(409, f"The document {parsed.number} was already imported")
    matches, _offset, charger, _sessions = _analyse(db, user, parsed, charger_id)
    doc = ReimbursementDocument(
        source=parsed.source, number=parsed.number, issuer=parsed.issuer, document_date=parsed.document_date, due_date=parsed.due_date, period=parsed.period,
        amount=parsed.amount, kwh=parsed.kwh, filename=(file.filename or "")[:255] or None, charger_id=charger, created_by_id=user.id,
    )  # fmt: skip
    for i, ln in enumerate(parsed.lines):
        line = ReimbursementLine(position=i, external_id=ln.external_id, started_at=ln.started_at, ended_at=ln.ended_at, kwh=ln.kwh, amount=ln.amount)
        for session in matches.get(i, []):
            line.sessions.append(session)
            session.reimbursement_status = "invoiced"
        doc.lines.append(line)
    db.add(doc)
    db.commit()
    return _detail(db, user, _get(db, doc.id, user))


@router.get("/{document_id}")
def get_document(document_id: int, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _detail(db, user, _get(db, document_id, user))


def _free(session: ChargeSession) -> None:
    """The session leaves its document: back to the default status."""
    session.reimbursement_line = None
    if session.to_reimburse:
        session.reimbursement_status = default_status(True, None, session.charger_id)


def _set_sessions(doc: ReimbursementDocument, status: str) -> None:
    for ln in doc.lines:
        for s in ln.sessions:
            if s.to_reimburse:
                s.reimbursement_status = status


@router.put("/{document_id}/charger")
def set_charger(document_id: int, body: ChargerIn, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Set the charger of a document (for those imported without one); its linked sessions must be this charger's."""
    doc = _get(db, document_id, user)
    charger = _charger(db, user, body.charger_id)
    others = sum(s.charger_id != charger.id for ln in doc.lines for s in ln.sessions)
    if others:
        raise HTTPException(422, f"{others} session(s) linked to this document weren't made with '{charger.name}': unlink them first")
    doc.charger_id = charger.id
    db.commit()
    return _detail(db, user, _get(db, document_id, user))


@router.put("/{document_id}/payment")
def record_payment(document_id: int, body: PaymentIn, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The payment was received: the sessions of the document become "paid"."""
    doc = _get(db, document_id, user)
    doc.paid_date = body.paid_date or date.today()
    doc.paid_amount = round(body.paid_amount if body.paid_amount is not None else doc.amount, 2)
    if "notes" in body.model_fields_set:
        doc.notes = (body.notes or "").strip() or None
    _set_sessions(doc, "paid")
    db.commit()
    return _detail(db, user, _get(db, document_id, user))


@router.delete("/{document_id}/payment")
def cancel_payment(document_id: int, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Back to "payment to receive": the sessions of the document are "invoiced" again."""
    doc = _get(db, document_id, user)
    doc.paid_date = doc.paid_amount = None
    _set_sessions(doc, "invoiced")
    db.commit()
    return _detail(db, user, _get(db, document_id, user))


@router.put("/{document_id}/lines/{line_id}")
def link_line(document_id: int, line_id: int, body: LineSessions, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Set by hand the session(s) of a line: one, several when the company grouped sessions in one line, or none (unlink)."""
    doc = _get(db, document_id, user)
    line = next((ln for ln in doc.lines if ln.id == line_id), None)
    if line is None:
        raise HTTPException(404, "Line not found")
    wanted = set(body.session_ids)
    current = {s.id: s for s in line.sessions}
    new = wanted - set(current)
    if new:
        day = line.started_at.date()
        free = {s.id: s for s in _eligible(db, user, doc.charger_id, day - timedelta(days=366), day + timedelta(days=366))}
        if not new <= set(free):
            raise HTTPException(
                422,
                "This session can't be linked: it must be one of the document's charger, to be reimbursed, not in progress, on no document yet, and on a car you manage",
            )
        for sid in new:
            free[sid].reimbursement_line = line
            free[sid].reimbursement_status = "paid" if doc.paid_date else "invoiced"
    for sid, session in current.items():
        if sid not in wanted:
            _free(session)
    db.commit()
    return _detail(db, user, _get(db, document_id, user))


@router.post("/{document_id}/lines/{line_id}/session", status_code=201)
def create_session_from_line(document_id: int, line_id: int, body: NewSession, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create the session a line has none for (one we never recorded), from what the document says: date, times, energy;
    with the charger of the document and its defaults, linked to the line, and a note telling where it comes from."""
    doc = _get(db, document_id, user)
    line = next((ln for ln in doc.lines if ln.id == line_id), None)
    if line is None:
        raise HTTPException(404, "Line not found")
    if line.sessions:
        raise HTTPException(409, "This line already has a session")
    if doc.charger is None:
        raise HTTPException(422, "Choose the charger of the document first")
    car_id = body.car_id or _default_car(doc)
    car = db.get(Car, car_id) if car_id is not None else None
    if car is None or car.group_id != doc.charger.group_id:
        raise HTTPException(422, "Choose the car of the session")
    if not (user.can("sessions:create", car_id) and user.can("reimbursement:manage", car_id)):
        raise HTTPException(403, "Not allowed on this car (add sessions and manage their reimbursement)")
    shift = _time_shift(doc)
    start = line.started_at + shift
    end = line.ended_at + shift if line.ended_at else None
    session = ChargeSession(
        car_id=car_id, charge_date=start.date(), start_time=start.time().replace(second=0), end_time=end.time().replace(second=0) if end else None, kwh=line.kwh,
        location=doc.charger.default_location, provider=doc.charger.default_provider, charger_id=doc.charger.id, to_reimburse=True,
        cost=None if doc.charger.uses_home_tariff else line.amount,  # the home tariff prices it; else the amount of the document
        reimbursement_status="paid" if doc.paid_date else "invoiced", notes=f"Created from invoice {doc.number}", created_by_id=user.id,
    )  # fmt: skip
    session.reimbursement_line = line
    db.add(session)
    db.commit()
    return _detail(db, user, _get(db, document_id, user))


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: int, user: Principal = Depends(manager), db: Session = Depends(get_db)) -> Response:
    """Delete the document; its sessions go back to "recorded" (or "not recorded" without a charger)."""
    doc = _get(db, document_id, user)
    for ln in doc.lines:
        for s in list(ln.sessions):
            _free(s)
    db.delete(doc)
    db.commit()
    return Response(status_code=204)
