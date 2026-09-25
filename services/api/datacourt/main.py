"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from datacourt import __version__
from datacourt.config import ConfigurationError, get_settings
from datacourt.logging_setup import configure_logging, log, request_id_var
from datacourt.storage import StorageUnavailable

logger = logging.getLogger("datacourt.api")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    log(logger, logging.INFO, "api starting", **settings.redacted())
    if settings.run_embedded_worker and settings.execution_backend == "embedded":
        from datacourt.worker import start_embedded_worker

        app.state.worker = start_embedded_worker()
    if settings.seed_demo_on_start and settings.demo_enabled and settings.execution_backend == "embedded":
        # Seeding runs the ML pipeline, so it only happens where the worker runs in-process.
        # With GitHub Actions, run the worker workflow with task "seed-demo" instead.
        from datacourt.demo import seed_in_background

        seed_in_background()
    yield
    if getattr(app.state, "worker", None) is not None:
        app.state.worker.stop_event.set()


def _unconfigured_app(problems: list[str]) -> FastAPI:
    """Served when production settings are missing: every route explains what to configure
    (setting names only, never values) instead of failing with an opaque 500."""
    app = FastAPI(title="DataCourt AI API (not configured)", docs_url=None, redoc_url=None, openapi_url=None)
    body = {"detail": "The DataCourt API is not configured yet.", "configured": False, "problems": problems}

    @app.get(f"{API_PREFIX}/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": False, **body}, status_code=503)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def unavailable(path: str) -> JSONResponse:
        return JSONResponse(body, status_code=503)

    return app


def create_app() -> FastAPI:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        configure_logging()
        log(logger, logging.ERROR, "api not configured", problems=exc.problems)
        return _unconfigured_app(exc.problems)
    if settings.execution_backend == "github_actions":
        from datacourt import execution

        execution.install()
    app = FastAPI(
        title="DataCourt AI API",
        version=__version__,
        description="Evidence-driven dataset forensics: findings, court cases, review, what-if experiments and governance.",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["content-type", "x-datacourt-csrf", "authorization"],
        max_age=600,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid[:64])
        started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid[:64]
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        dur = int((time.monotonic() - started) * 1000)
        if dur > 2000 or response.status_code >= 500:
            # Path only (no query string: it may contain search terms).
            log(
                logger,
                logging.WARNING,
                "slow or failed request",
                path=request.url.path,
                status=response.status_code,
                duration_ms=dur,
                request_id=rid[:64],
            )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()[:20]]
        return JSONResponse({"detail": "invalid request", "errors": errors}, status_code=422)

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable):
        return JSONResponse(
            {"detail": "Object storage is temporarily unavailable (daily quota reached). Try again later."},
            status_code=503,
            headers={"Retry-After": "3600", "Cache-Control": "no-store"},
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error")
        return JSONResponse({"detail": "internal error", "request_id": request_id_var.get()}, status_code=500)

    # Imported only once the settings are known to be valid: routers read them at import time.
    from datacourt.api.routers import (
        audits,
        auth,
        court,
        datasets,
        experiments,
        findings,
        orgs,
        review,
        system,
    )

    for r in (system, auth, orgs, datasets, audits, findings, court, review, experiments):
        app.include_router(r.router, prefix=API_PREFIX)
    return app


app = create_app()
