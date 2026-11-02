"""API tokens (Settings → API tokens): each one has its own access, a role or chosen permissions,
on all cars or on chosen cars."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..auth import Principal, browser_only, new_api_token
from ..database import get_db
from ..models import ApiToken, Car, Role
from ..permissions import valid_permission
from ..schemas import TokenCreated, TokenIn, TokenOut, TokenUpdate

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


def manager(principal: Principal = Depends(browser_only)) -> Principal:
    """Tokens are managed from a browser sign-in (a token can't manage tokens)."""
    if not principal.has("tokens:manage"):
        raise HTTPException(403, "Permission needed: tokens:manage")
    return principal


def token_out(t: ApiToken) -> TokenOut:
    return TokenOut(
        id=t.id, name=t.name, description=t.description, prefix=t.prefix, role_id=t.role_id, role_name=t.role.name if t.role else None,
        permissions=sorted(t.permission_set), car_ids=t.car_list, created_by=t.user.full_name or t.user.username,
        created_at=t.created_at, expires_at=t.expires_at, last_used_at=t.last_used_at, revoked=t.revoked,
    )  # fmt: skip


def _apply(db: Session, t: ApiToken, body: TokenIn | TokenUpdate) -> None:
    if body.role_id is not None:
        if db.get(Role, body.role_id) is None:
            raise HTTPException(422, "Unknown role")
        perms: list[str] = []
    else:
        perms = sorted(set(body.permissions))
        bad = [p for p in perms if not valid_permission(p)]
        if bad:
            raise HTTPException(422, f"Unknown permission(s): {', '.join(bad)}")
        if not perms:
            raise HTTPException(422, "Give the token a role or at least one permission")
    if body.car_ids is not None:
        cars = set(body.car_ids)
        if len(db.scalars(select(Car.id).where(Car.id.in_(cars))).all()) != len(cars):
            raise HTTPException(422, "Unknown car")
    t.name = body.name.strip()
    t.description = body.description
    t.role_id = body.role_id
    t.permissions = json.dumps(perms)
    t.car_ids = None if body.car_ids is None else json.dumps(sorted(set(body.car_ids)))


def _get(db: Session, token_id: int) -> ApiToken:
    t = db.get(ApiToken, token_id)
    if t is None:
        raise HTTPException(404, "Token not found")
    return t


@router.get("", response_model=list[TokenOut])
def list_tokens(_: Principal = Depends(manager), db: Session = Depends(get_db)) -> list[TokenOut]:
    return [token_out(t) for t in db.scalars(select(ApiToken).order_by(ApiToken.revoked, ApiToken.created_at.desc()))]


@router.post("", response_model=TokenCreated, status_code=201)
def create_token(body: TokenIn, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> TokenCreated:
    raw, hashed = new_api_token()
    t = ApiToken(user_id=me.id, token_hash=hashed, prefix=raw[:12],
                 expires_at=datetime.now(UTC) + timedelta(days=body.expires_days) if body.expires_days else None)  # fmt: skip
    _apply(db, t, body)
    db.add(t)
    db.flush()
    db.refresh(t)
    audit.record(db, request, "token.created", target=f"token:{t.id}", details=body.model_dump())
    db.commit()
    return TokenCreated(**token_out(t).model_dump(), token=raw)


@router.put("/{token_id}", response_model=TokenOut)
def update_token(token_id: int, body: TokenUpdate, request: Request, _: Principal = Depends(manager), db: Session = Depends(get_db)) -> TokenOut:
    t = _get(db, token_id)
    before = token_out(t).model_dump(include={"name", "role_id", "permissions", "car_ids"})
    _apply(db, t, body)
    db.flush()
    db.refresh(t)
    audit.record(db, request, "token.updated", target=f"token:{t.id}", details={"before": before, "after": body.model_dump()})
    db.commit()
    return token_out(t)


@router.post("/{token_id}/revoke", response_model=TokenOut)
def revoke_token(token_id: int, request: Request, _: Principal = Depends(manager), db: Session = Depends(get_db)) -> TokenOut:
    t = _get(db, token_id)
    t.revoked = True
    audit.record(db, request, "token.revoked", target=f"token:{t.id}", details={"name": t.name})
    db.commit()
    return token_out(t)


@router.delete("/{token_id}", status_code=204)
def delete_token(token_id: int, request: Request, _: Principal = Depends(manager), db: Session = Depends(get_db)) -> Response:
    t = _get(db, token_id)
    db.delete(t)
    audit.record(db, request, "token.deleted", target=f"token:{token_id}", details={"name": t.name})
    db.commit()
    return Response(status_code=204)
