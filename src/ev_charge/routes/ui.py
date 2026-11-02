"""HTML pages. Each page is a Jinja shell that loads its data from the JSON API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import CONFIG

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals.update(app_version=__version__, currency=CONFIG.currency)


def _page(request: Request, template: str, active: str) -> HTMLResponse:
    return templates.TemplateResponse(request, template, {"active": active})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return _page(request, "dashboard.html", "dashboard")


@router.get("/cars", response_class=HTMLResponse)
def cars(request: Request) -> HTMLResponse:
    return _page(request, "cars.html", "cars")


@router.get("/sessions", response_class=HTMLResponse)
def sessions(request: Request) -> HTMLResponse:
    return _page(request, "sessions.html", "sessions")


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request) -> HTMLResponse:
    return _page(request, "reports.html", "reports")
