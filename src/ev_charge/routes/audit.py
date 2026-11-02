"""Audit log reading."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import Principal, require
from ..database import get_db
from ..models import AuditEvent

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def read_audit(
    action: str | None = Query(default=None, description="Action, or its prefix ending with '.' (e.g. 'user.')"),
    username: str | None = None,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require("audit:read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(AuditEvent)
    if action:
        if action.endswith("."):  # a family: "login." = login, login.failed…
            query = query.where(or_(AuditEvent.action == action[:-1], AuditEvent.action.like(f"{action}%")))
        else:
            query = query.where(AuditEvent.action == action)
    if username:
        query = query.where(AuditEvent.username == username.strip().lower())
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(AuditEvent.target.ilike(like), AuditEvent.details.ilike(like), AuditEvent.ip.ilike(like)))
    if date_from:
        query = query.where(AuditEvent.at >= datetime.combine(date_from, time.min))
    if date_to:
        query = query.where(AuditEvent.at < datetime.combine(date_to + timedelta(days=1), time.min))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(AuditEvent.at.desc(), AuditEvent.id.desc()).limit(limit).offset(offset)).all()
    actions = sorted(db.scalars(select(AuditEvent.action).distinct()))
    return {
        "total": total,
        "actions": actions,
        "items": [
            {
                "id": e.id,
                "at": e.at,
                "username": e.username,
                "action": e.action,
                "target": e.target,
                "ip": e.ip,
                "details": json.loads(e.details) if e.details else None,
            }  # fmt: skip
            for e in rows
        ],
    }
