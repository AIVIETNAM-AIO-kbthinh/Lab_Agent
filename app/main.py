"""FastAPI app factory (tiến trình api, stateless)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import clock
from app.api import admin, auth, chat, exports, forms, health, templates, uploads
from app.api.session_mw import app_middleware
from app.config import get_loaded
from app.errors import LabAgentError
from app.logging import get_logger, setup_logging
from app.services import Services, build_services

WEB = Path(__file__).parent / "web"
log = get_logger(__name__)


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services
        if svc is None:
            loaded = get_loaded()
            setup_logging(loaded.settings.log_level)
            _sentry(loaded.settings.sentry_dsn, loaded.settings.app_env)
            svc = build_services(loaded)
            from app.db.migrate import assert_up_to_date

            await assert_up_to_date(svc.sm)
        clock.configure(svc.config.timezone)
        app.state.services = svc
        log.info("api_started", lab=svc.config.lab.id, env=svc.settings.app_env)
        yield
        if services is None:
            await svc.aclose()

    app = FastAPI(title="Lab Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if services is not None:
        app.state.services = services
    app.middleware("http")(app_middleware)

    @app.exception_handler(LabAgentError)
    async def _lab_error(_: Request, exc: LabAgentError) -> JSONResponse:
        return JSONResponse({"detail": exc.user_message}, status_code=400)

    for r in (
        auth.router,
        chat.router,
        uploads.router,
        forms.router,
        exports.router,
        templates.router,
        admin.router,
        health.router,
    ):
        app.include_router(r)
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB / "static" / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(WEB / "static" / "login.html")

    @app.get("/admin/kb/web", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return FileResponse(WEB / "static" / "admin.html")

    return app


def _sentry(dsn: str | None, env: str) -> None:
    if not dsn:
        return
    import sentry_sdk

    # không gửi nội dung request/PII lên Sentry
    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        send_default_pii=False,
        traces_sample_rate=0.0,
        max_request_body_size="never",
    )
