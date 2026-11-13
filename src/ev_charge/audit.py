"""Audit log: who did what, when, from where."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.orm import Session

from .models import AuditEvent, User

logger = logging.getLogger(__name__)


def record(
    db: Session,
    request: Request | None,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    user: User | None = None,
    username: str | None = None,
) -> None:
    """Add an event (committed with the caller's transaction). The acting user is the request's, unless given."""
    principal = getattr(request.state, "principal", None) if request is not None else None
    actor = user or (principal.user if principal is not None else None)
    if principal is not None and principal.token is not None:
        details = {**(details or {}), "api_token": principal.token.name}
    db.add(
        AuditEvent(
            at=datetime.now(UTC),
            user_id=actor.id if actor else None,
            username=(actor.username if actor else username),
            action=action,
            target=target,
            details=json.dumps(details, default=str) if details else None,
            ip=request.client.host if request is not None and request.client else None,
        )
    )


def purge(db: Session, keep_days: int) -> int:
    if keep_days <= 0:
        return 0
    result = db.execute(delete(AuditEvent).where(AuditEvent.at < datetime.now(UTC) - timedelta(days=keep_days)))
    db.commit()
    return result.rowcount or 0


# ── Generic record of the changes made through the API ──────────────────────
# Routes that record their own, more detailed events (or only read/preview) are skipped.
_SKIP_PREFIXES = (
    "/api/auth",
    "/api/users",
    "/api/groups",
    "/api/roles",
    "/api/settings",
    "/api/tokens",
    "/api/import/sheet",
    "/api/import/sessions/preview",
    "/api/tariffs/creg/upload",
    "/api/reimbursement-documents/preview",
)
_SKIP_SUFFIXES = ("/poll",)
_VERBS = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}


def describe_change(method: str, path: str) -> tuple[str, str | None] | None:
    """('cars.update', 'cars:3') for PUT /api/cars/3; None when not audited."""
    if method not in _VERBS or not path.startswith("/api/") or path.startswith(_SKIP_PREFIXES) or path.endswith(_SKIP_SUFFIXES):
        return None
    parts = [p for p in path[5:].split("/") if p]
    if not parts:
        return None
    resource, ident, rest = parts[0], None, parts[1:]
    if rest and rest[0].isdigit():
        ident, rest = rest[0], rest[1:]
    verb = "_".join(rest) if rest else _VERBS[method]
    if rest and method == "DELETE":
        verb += "_delete"
    return f"{resource}.{verb}"[:50], (f"{resource}:{ident}" if ident else None)


def record_change(session_factory: Any, request: Request, status_code: int) -> None:
    change = describe_change(request.method, request.url.path)
    principal = getattr(request.state, "principal", None)
    if change is None or status_code >= 400 or principal is None:
        return
    action, target = change
    try:
        with session_factory() as db:
            details = {"status": status_code}
            if request.url.query:
                details["query"] = request.url.query[:300]
            record(db, request, action, target, details)
            db.commit()
    except Exception:  # the audit must never break a request
        logger.exception("Cannot write the audit log")
