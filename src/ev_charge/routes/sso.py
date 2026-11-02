"""Single sign-on routes: sign-in redirect and callback, public state for the sign-in page, settings."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import audit, sso
from ..auth import Principal, client_ip, require, start_session
from ..database import get_db
from ..models import Role

router = APIRouter(tags=["sso"])
FLOW_COOKIE = "ev_sso"


def _safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/"


def _redirect_uri(request: Request, db: Session) -> str:
    return sso.get_config(db)["redirect_uri"] or str(request.url_for("sso_callback"))


@router.get("/api/auth/sso")
def sso_state(db: Session = Depends(get_db)) -> dict[str, Any]:
    """For the sign-in page (no sign-in needed)."""
    cfg = sso.get_config(db)
    return {"enabled": sso.sso_enabled(db), "name": cfg["name"], "local_login": sso.local_login_allowed(db)}


@router.get("/auth/sso/login", include_in_schema=False)
async def sso_login(request: Request, next: str | None = None, db: Session = Depends(get_db)) -> Response:
    try:
        url, flow = await sso.begin(db, _redirect_uri(request, db), _safe_next(next))
    except sso.SsoError as e:
        return RedirectResponse(f"/login?error={quote(str(e))}", status_code=303)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(FLOW_COOKIE, flow, max_age=sso.FLOW_SECONDS, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/auth/sso")
    return response


@router.get("/auth/sso/callback", include_in_schema=False, name="sso_callback")
async def sso_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None,
    db: Session = Depends(get_db),
) -> Response:  # fmt: skip
    def fail(message: str) -> Response:
        audit.record(db, request, "login.failed", details={"method": "sso", "reason": message})
        db.commit()
        r = RedirectResponse(f"/login?error={quote(message)}", status_code=303)
        r.delete_cookie(FLOW_COOKIE, path="/auth/sso")
        return r

    if error:
        return fail(f"Single sign-on: {error_description or error}")
    if not code or not state:
        return fail("Single sign-on: missing code")
    try:
        claims, next_url = await sso.finish(db, request.cookies.get(FLOW_COOKIE), state, code)
        user, how = sso.resolve_user(db, claims)
    except sso.SsoError as e:
        db.rollback()
        return fail(str(e))
    response = RedirectResponse(_safe_next(next_url), status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/auth/sso")
    audit.record(db, request, "login", user=user, details={"method": "sso", "account": how, "subject": claims.get("sub"), "ip": client_ip(request)})
    if how in ("created", "linked"):
        audit.record(db, request, f"user.sso_{how}", target=f"user:{user.id}", user=user, details={"username": user.username, "role_id": user.role_id})
    db.commit()
    start_session(db, response, user, request, method="sso")
    return response


# ── Settings ─────────────────────────────────────────────────────────────────


class GroupRole(BaseModel):
    group: str = Field(min_length=1, max_length=200)
    role_id: int


class SsoSettings(BaseModel):
    enabled: bool = False
    name: str = Field(default="Authelia", max_length=50)
    issuer: str = Field(default="", max_length=300)
    client_id: str = Field(default="", max_length=200)
    client_secret: str | None = Field(default=None, max_length=500)  # write-only; empty: unchanged
    scopes: str = Field(default="openid profile email groups", max_length=300)
    auth_method: str = Field(default="client_secret_basic", pattern="^client_secret_(basic|post)$")
    redirect_uri: str = Field(default="", max_length=300)
    username_claim: str = Field(default="preferred_username", max_length=50)
    groups_claim: str = Field(default="groups", max_length=50)
    auto_create: bool = True
    default_role_id: int | None = None
    group_roles: list[GroupRole] = Field(default=[], max_length=50)
    local_login: bool = True


@router.get("/api/settings/sso")
def get_sso(request: Request, _: Principal = Depends(require("settings:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = sso.get_config(db)
    return {**cfg, "has_secret": bool(sso.get_secret(db)), "callback_url": _redirect_uri(request, db), "allow_local_login_override": sso.CONFIG.allow_local_login}


@router.put("/api/settings/sso")
def put_sso(body: SsoSettings, request: Request, me: Principal = Depends(require("settings:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    for ref in [body.default_role_id] + [g.role_id for g in body.group_roles]:
        if ref is not None and db.get(Role, ref) is None:
            raise HTTPException(422, "Unknown role")
    if body.enabled and not (body.issuer and body.client_id and (body.client_secret or sso.get_secret(db))):
        raise HTTPException(422, "To enable single sign-on, fill in the issuer, the client ID and the client secret")
    if body.enabled and not body.local_login and not me.user.oidc_subject and not sso.CONFIG.allow_local_login:
        raise HTTPException(409, "Sign in once with single sign-on before turning password sign-in off, so you can't lock yourself out")
    cfg = body.model_dump(exclude={"client_secret"})
    cfg["issuer"] = cfg["issuer"].strip().rstrip("/")
    cfg["group_roles"] = [g.model_dump() for g in body.group_roles]
    sso.save_config(db, cfg, (body.client_secret or "").strip() or None)
    sso._discovery.clear()
    audit.record(db, request, "settings.sso", details={k: v for k, v in cfg.items()} | {"client_secret": "changed" if body.client_secret else "unchanged"})
    db.commit()
    return get_sso(request, me, db)


@router.post("/api/settings/sso/test")
async def test_sso(body: SsoSettings, _: Principal = Depends(require("settings:manage"))) -> dict[str, Any]:
    """Read the provider's discovery document (nothing is saved)."""
    sso._discovery.pop(body.issuer.strip().rstrip("/"), None)
    try:
        doc = await sso.discover(body.issuer.strip())
    except sso.SsoError as e:
        raise HTTPException(422, str(e)) from None
    return {k: doc.get(k) for k in ("issuer", "authorization_endpoint", "token_endpoint", "userinfo_endpoint", "scopes_supported", "claims_supported")}
