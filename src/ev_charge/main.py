"""EV Charge — FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .database import init_db
from .routes import auth, cars, imports, reports, sessions, tariffs, ui, users


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    init_db()
    yield


app = FastAPI(title="EV Charge", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
for module in (auth, users, cars, sessions, tariffs, imports, reports, ui):
    app.include_router(module.router)


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
