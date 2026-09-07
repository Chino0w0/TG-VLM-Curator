from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from tgcurator.application import Settings, get_settings
from tgcurator.application.processing import RangeExecutionWorker
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyRangeExecutionWorkerRepository,
)
from tgcurator.shared import DomainValidationError


@dataclass(slots=True)
class WorkerRuntime:
    """Worker composition root for an immutable, PostgreSQL-owned execution window."""

    database: AsyncDatabase
    range_execution_worker: RangeExecutionWorker

    async def handle_range_execution(self, *, execution_id: str, now: datetime) -> bool:
        normalized_execution_id = _normalize_execution_id(execution_id)
        claim = await self.range_execution_worker.claim(
            execution_id=normalized_execution_id,
            now=now,
        )
        # M3 owns Telegram history/media ingestion and records watermarks after each committed
        # message. Until that processor exists, a successful lease intentionally remains open;
        # durable_wakeup repair will re-enqueue it after the lease expires.
        return claim is not None

    async def close(self) -> None:
        await self.database.dispose()


def create_worker_runtime(*, settings: Settings) -> WorkerRuntime:
    database_url = settings.database_url
    if database_url is None or not database_url.strip():
        raise RuntimeError("TGCURATOR_DATABASE_URL is required")
    database = AsyncDatabase(database_url)
    return WorkerRuntime(
        database=database,
        range_execution_worker=RangeExecutionWorker(
            repository=SqlAlchemyRangeExecutionWorkerRepository(database)
        ),
    )


async def run_range_execution_task(execution_id: str, *, settings: Settings | None = None) -> bool:
    """Run the M2 worker entry point for exactly one stable RangeExecution UUID."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await runtime.handle_range_execution(
            execution_id=execution_id,
            now=datetime.now(UTC),
        )
    finally:
        await runtime.close()


def _normalize_execution_id(execution_id: str) -> str:
    try:
        return str(UUID(execution_id))
    except (AttributeError, ValueError) as error:
        raise DomainValidationError("execution_id must be a UUID") from error
