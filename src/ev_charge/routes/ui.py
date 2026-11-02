"""HTML pages. Each page is a Jinja shell that loads its data from the JSON API.

Pages need a signed-in user (else: redirect to /login, or /setup on first run);
Users and Tariffs are for administrators.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import __version__
from ..auth import user_from_request
from ..config import CONFIG
from ..database import get_db
from .auth import me_out, setup_needed

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals.update(app_version=__version__, currency=CONFIG.currency)


def _page(request: Request, db: Session, template: str, active: str, admin_only: bool = False) -> Response:
    user = user_from_request(db, request)
    if user is None:
        if setup_needed(db):
            return RedirectResponse("/setup", status_code=303)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    if admin_only and not user.is_admin:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, template, {"active": active, "user": user, "me": me_out(user).model_dump(mode="json")})


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


@router.get("/tariffs", response_class=HTMLResponse)
def tariffs(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "tariffs.html", "tariffs", admin_only=True)


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, db: Session = Depends(get_db)) -> Response:
    return _page(request, db, "users.html", "users", admin_only=True)


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, db: Session = Depends(get_db)) -> Response:
    if setup_needed(db):
        return RedirectResponse("/setup", status_code=303)
    if user_from_request(db, request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"mode": "login"})


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request, db: Session = Depends(get_db)) -> Response:
    if not setup_needed(db):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"mode": "setup"})
