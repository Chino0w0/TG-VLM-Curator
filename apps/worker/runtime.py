from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from tgcurator.application import Settings, get_settings
from tgcurator.application.media import ImageArchiveWorker
from tgcurator.application.processing import RangeExecutionHistoryIngestion, RangeExecutionWorker
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyRangeExecutionWorkerRepository,
)
from tgcurator.shared import DomainValidationError


@dataclass(slots=True)
class WorkerRuntime:
    """Worker composition root for PostgreSQL-owned range and image-archive work."""

    database: AsyncDatabase
    range_execution_worker: RangeExecutionWorker
    range_execution_history_ingestion: RangeExecutionHistoryIngestion | None = None
    image_archive_worker: ImageArchiveWorker | None = None

    async def handle_range_execution(self, *, execution_id: str, now: datetime) -> bool:
        normalized_execution_id = _normalize_execution_id(execution_id)
        claim = await self.range_execution_worker.claim(
            execution_id=normalized_execution_id,
            now=now,
        )
        if claim is None:
            return False
        if self.range_execution_history_ingestion is None:
            # Deployment injects a Telegram gateway after its identity/session adapter exists.
            # Leaving this lease open is safer than completing an unprocessed window.
            return True
        return await self.range_execution_history_ingestion.process(claim=claim, now=now)

    async def handle_image_archive(self, *, image_asset_id: str, now: datetime) -> bool:
        normalized_image_asset_id = _normalize_image_asset_id(image_asset_id)
        if self.image_archive_worker is None:
            # The wake-up remains durable and will be repaired once Telegram identity/session and
            # archive storage composition are injected. Never fake archive completion.
            return False
        return await self.image_archive_worker.process(
            image_asset_id=normalized_image_asset_id,
            now=now,
        )

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


async def run_image_archive_task(image_asset_id: str, *, settings: Settings | None = None) -> bool:
    """Run one durable image-archive wake-up without inventing a missing Telegram composition."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await runtime.handle_image_archive(
            image_asset_id=image_asset_id,
            now=datetime.now(UTC),
        )
    finally:
        await runtime.close()


def _normalize_execution_id(execution_id: str) -> str:
    return _normalize_uuid(execution_id, field="execution_id")


def _normalize_image_asset_id(image_asset_id: str) -> str:
    return _normalize_uuid(image_asset_id, field="image_asset_id")


def _normalize_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
