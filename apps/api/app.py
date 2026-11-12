from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tgcurator.application import Settings, get_settings
from tgcurator.application.admin import AdminApplicationService, AdminError
from tgcurator.infrastructure.database import AsyncDatabase
from tgcurator.infrastructure.observability import configure_structured_logging
from tgcurator.shared import DomainValidationError

from .routers import api_router, metrics_router

logger = logging.getLogger(__name__)


class ReadinessProbe(Protocol):
    async def ping(self) -> bool: ...

    async def dispose(self) -> None: ...


class HealthResponse(BaseModel):
    status: str
    service: str
    checks: dict[str, str]


def create_app(
    settings: Settings | None = None,
    database: ReadinessProbe | None = None,
    *,
    admin_service: AdminApplicationService | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_structured_logging(resolved_settings.log_level, service="api")
    resolved_database = (
        database if database is not None else AsyncDatabase(resolved_settings.database_url)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await app.state.database.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.admin_service = admin_service

    @app.exception_handler(AdminError)
    async def handle_admin_error(_request: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                }
            },
        )

    @app.exception_handler(DomainValidationError)
    async def handle_domain_validation_error(
        _request: Request,
        exc: DomainValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "domain_validation_error",
                    "message": str(exc),
                }
            },
        )

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def liveness() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=resolved_settings.app_name,
            checks={"process": "ok"},
        )

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def readiness(request: Request) -> HealthResponse | JSONResponse:
        try:
            database_ready = await request.app.state.database.ping()
        except Exception:
            logger.exception(
                "PostgreSQL readiness probe failed",
                extra={"event": "health.database_probe_failed"},
            )
            database_ready = False

        if database_ready:
            return HealthResponse(
                status="ok",
                service=resolved_settings.app_name,
                checks={"database": "ok"},
            )
        payload = HealthResponse(
            status="not_ready",
            service=resolved_settings.app_name,
            checks={"database": "not_ready"},
        )
        return JSONResponse(status_code=503, content=payload.model_dump())

    app.include_router(metrics_router)
    app.include_router(api_router)
    return app
