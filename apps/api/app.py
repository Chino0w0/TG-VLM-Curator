from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tgcurator.application import Settings, get_settings
from tgcurator.infrastructure.database import AsyncDatabase
from tgcurator.infrastructure.observability import configure_structured_logging


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
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_structured_logging(resolved_settings.log_level)
    resolved_database = (
        database if database is not None else AsyncDatabase(resolved_settings.database_url)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.database.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def liveness() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=resolved_settings.app_name,
            checks={"process": "ok"},
        )

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def readiness(request: Request) -> HealthResponse | JSONResponse:
        database_ready = await request.app.state.database.ping()
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

    return app
