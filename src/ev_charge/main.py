"""EV Charge — FastAPI application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import CONFIG
from .database import SessionLocal, init_db
from .routes import audit as audit_routes
from .routes import auth, cars, chargers, documents, groups, imports, mail, reports, roles, sessions, sso, tariffs, tokens, ui, users


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    init_db()
    from .audit import purge
    from .permissions import ensure_builtin_roles, ensure_default_group

    with SessionLocal() as db:
        ensure_builtin_roles(db)
        ensure_default_group(db)
        purge(db, CONFIG.audit_retention_days)
    poller = None
    if CONFIG.charger_polling:
        from .live import poll_loop

        poller = asyncio.create_task(poll_loop(SessionLocal, max(15, CONFIG.charger_poll_seconds)))
        logging.getLogger(__name__).info("Charger polling every %s s", max(15, CONFIG.charger_poll_seconds))
    yield
    if poller:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller


app = FastAPI(title="EV Charge", version=__version__, lifespan=lifespan)


class _StaticFiles(StaticFiles):
    """Static files that browsers re-validate (ETag) instead of using a stale copy after an update."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
for module in (auth, sso, mail, tokens, users, groups, roles, audit_routes, cars, chargers, sessions, tariffs, imports, reports, documents, ui):
    app.include_router(module.router)


@app.middleware("http")
async def audit_changes(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Record the changes made through the API in the audit log (who, what, when)."""
    response = await call_next(request)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        from .audit import record_change

        await asyncio.to_thread(record_change, SessionLocal, request, response.status_code)
    return response


@app.middleware("http")
async def same_origin_writes(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Refuse state-changing requests sent by another site (CSRF), on top of the SameSite=Lax cookie."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)


@app.get("/api/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
