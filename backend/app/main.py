"""FastAPI application: API under /api, built SPA served for everything else."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from . import db, scheduler
from .checkers import shutdown as shutdown_checkers
from .config import get_settings, prepare_data_dir
from .routers import apple, auth, health, images, items, notifications, retailers, settings, stats, system, users
from .security import SecurityMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stockwatcher")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    prepare_data_dir(s)  # DATA_DIR, DATA_DIR/images, secret key
    db.init_engine()
    db.init_db()
    if s.scheduler_enabled:
        scheduler.start()
    log.info("Stock Watcher %s ready (data dir: %s)", s.app_version, s.data_dir)
    try:
        yield
    finally:
        await scheduler.stop()
        try:
            await shutdown_checkers()
        except Exception:  # noqa: BLE001
            log.exception("checker shutdown failed")
        db.dispose_engine()


def _validation_message(exc: RequestValidationError) -> tuple[str, list[dict]]:
    errors = []
    parts = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        msg = str(err.get("msg", "Invalid value"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        field = ".".join(loc)
        errors.append({"field": field, "message": msg})
        parts.append(f"{field}: {msg}" if field else msg)
    return "; ".join(parts) or "Invalid request", errors


def create_app() -> FastAPI:
    app = FastAPI(title="Stock Watcher", version=get_settings().app_version, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError):
        detail, errors = _validation_message(exc)
        return JSONResponse(status_code=422, content={"detail": detail, "errors": errors})

    for module in (health, auth, users, settings, items, notifications, apple, stats, images, system, retailers):
        app.include_router(module.router, prefix="/api")

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                   include_in_schema=False)
    async def api_not_found(path: str):
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        static_dir = get_settings().static_dir
        if not static_dir.is_dir():
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        root = static_dir.resolve()
        if full_path:
            candidate = (root / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                headers = {}
                if full_path.startswith("assets/"):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return FileResponse(candidate, headers=headers)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    return app


app = create_app()
