"""HTML pages. Each page is a Jinja shell that loads its data from the JSON API.

Pages need a signed-in user (else: redirect to /login, or /setup on first run);
Users and Tariffs are for administrators.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import __version__
from ..auth import principal_from_request
from ..config import CONFIG
from ..database import get_db
from .auth import me_out, setup_needed

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _asset_version() -> str:
    """A fingerprint of the static files: it changes whenever a script or style changes, so browsers
    fetch the new files after an update instead of running cached ones with new pages."""
    digest = hashlib.sha256()
    static = Path(__file__).parent.parent / "static"
    for f in sorted(static.rglob("*")):
        if f.is_file():
            digest.update(f.relative_to(static).as_posix().encode())
            digest.update(f.read_bytes())
    return digest.hexdigest()[:12]


templates.env.globals.update(app_version=__version__, asset_version=_asset_version(), currency=CONFIG.currency, poll_seconds=max(15, CONFIG.charger_poll_seconds))


def _page(request: Request, db: Session, template: str, active: str, permission: str | None = None, managers: bool = False) -> Response:
    """managers: the page is also for group managers (their own group's chargers and members)."""
    principal = principal_from_request(db, request) if not request.headers.get("authorization") else None
    user = principal.user if principal else None
    if user is None:
        if setup_needed(db):
            return RedirectResponse("/setup", status_code=303)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    if permission and not (principal.has(permission) or (managers and principal.is_group_manager)):  # type: ignore[union-attr]
        return RedirectResponse("/", status_code=303)
    me = me_out(principal).model_dump(mode="json")  # type: ignore[arg-type]
    can_reimburse = principal.cars_with("reimbursement:manage") != set()  # type: ignore[union-attr]
    if template == "payments.html" and not can_reimburse:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, template, {"active": active, "user": user, "me": me, "can": set(me["permissions"]), "can_reimburse": can_reimburse})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "dashboard.html", "dashboard")


@router.get("/cars", response_class=HTMLResponse)
def cars(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "cars.html", "cars")


@router.get("/sessions", response_class=HTMLResponse)
def sessions(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "sessions.html", "sessions")


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "reports.html", "reports")


@router.get("/reimbursement", response_class=HTMLResponse)
def reimbursement(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "reimbursement.html", "reimbursement")


@router.get("/payments", response_class=HTMLResponse)
def payments(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "payments.html", "payments")


@router.get("/tariffs", response_class=HTMLResponse)
def tariffs(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "tariffs.html", "tariffs", "tariffs:manage", managers=True)


@router.get("/chargers", response_class=HTMLResponse)
def chargers(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "chargers.html", "chargers", "chargers:manage", managers=True)


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "users.html", "users", "users:manage", managers=True)


@router.get("/groups", response_class=HTMLResponse)
def groups(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "groups.html", "groups", "groups:manage")


@router.get("/roles", response_class=HTMLResponse)
def roles(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "roles.html", "roles", "roles:manage")


@router.get("/settings", response_class=HTMLResponse)
def settings_home() -> Response:
    return RedirectResponse("/settings/sso", status_code=303)


@router.get("/settings/sso", response_class=HTMLResponse)
def settings_sso(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "settings.html", "settings", "settings:manage")


@router.get("/settings/email", response_class=HTMLResponse)
def settings_email(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "settings_email.html", "email", "settings:manage")


@router.get("/settings/tokens", response_class=HTMLResponse)
def settings_tokens(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "tokens.html", "tokens", "tokens:manage")


@router.get("/audit", response_class=HTMLResponse)
def audit(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "audit.html", "audit", "audit:read")


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "account.html", "account")


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, db: Session = Depends(get_db)) -> Response:
    if setup_needed(db):
        return RedirectResponse("/setup", status_code=303)
    if principal_from_request(db, request):
        return RedirectResponse("/", status_code=303)
    from ..sso import get_config, local_login_allowed, sso_enabled

    sso = {"enabled": sso_enabled(db), "name": get_config(db)["name"], "local_login": local_login_allowed(db)}
    return templates.TemplateResponse(request, "login.html", {"mode": "login", "sso": sso, "error": request.query_params.get("error")})


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request, db: Session = Depends(get_db)) -> Response:
    if not setup_needed(db):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"mode": "setup", "sso": {"enabled": False, "local_login": True}, "error": None})
