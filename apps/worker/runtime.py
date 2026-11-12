from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from tgcurator.application import Settings, get_settings
from tgcurator.application.analysis import AnalysisOrchestrator, AnalysisStageRunWorker
from tgcurator.application.cleanup import ArchiveCleanupWorker
from tgcurator.application.media import ImageArchiveWorker, VideoArchiveWorker
from tgcurator.application.ports.operations import OperationsTelemetry
from tgcurator.application.processing import RangeExecutionHistoryIngestion, RangeExecutionWorker
from tgcurator.application.publishing import PublicationWorker
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyAnalysisStageRunRepository,
    SqlAlchemyRangeExecutionWorkerRepository,
)
from tgcurator.infrastructure.observability import OperationsMonitor
from tgcurator.shared import DomainValidationError


@dataclass(slots=True)
class WorkerRuntime:
    """Worker composition root for PostgreSQL-owned range and image-archive work."""

    database: AsyncDatabase
    range_execution_worker: RangeExecutionWorker
    range_execution_history_ingestion: RangeExecutionHistoryIngestion | None = None
    image_archive_worker: ImageArchiveWorker | None = None
    video_archive_worker: VideoArchiveWorker | None = None
    analysis_stage_run_worker: AnalysisStageRunWorker | None = None
    publication_worker: PublicationWorker | None = None
    cleanup_worker: ArchiveCleanupWorker | None = None
    telemetry: OperationsTelemetry | None = None

    async def handle_range_execution(self, *, execution_id: str, now: datetime) -> bool:
        normalized_execution_id = _normalize_execution_id(execution_id)

        async def _execute() -> bool:
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

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_image_archive(self, *, image_asset_id: str, now: datetime) -> bool:
        normalized_image_asset_id = _normalize_image_asset_id(image_asset_id)

        async def _execute() -> bool:
            if self.image_archive_worker is None:
                # The wake-up remains durable and will be repaired once Telegram identity/session
                # and archive storage composition are injected. Never fake archive completion.
                return False
            return await self.image_archive_worker.process(
                image_asset_id=normalized_image_asset_id,
                now=now,
            )

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_video_archive(self, *, video_asset_id: str, now: datetime) -> bool:
        normalized_video_asset_id = _normalize_uuid(video_asset_id, field="video_asset_id")

        async def _execute() -> bool:
            if self.video_archive_worker is None:
                return False
            return await self.video_archive_worker.process(
                video_asset_id=normalized_video_asset_id,
                now=now,
            )

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_analysis_stage_run(self, *, stage_run_id: str, now: datetime) -> bool:
        normalized_stage_run_id = _normalize_uuid(stage_run_id, field="stage_run_id")

        async def _execute() -> bool:
            if self.analysis_stage_run_worker is None:
                return False
            return await self.analysis_stage_run_worker.process(
                stage_run_id=normalized_stage_run_id,
                now=now,
            )

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_publication(self, *, intent_id: str, now: datetime) -> bool:
        normalized_intent_id = _normalize_uuid(intent_id, field="intent_id")

        async def _execute() -> bool:
            if self.publication_worker is None:
                # Publication remains database-owned and scheduler repair will wake it again after a
                # Telegram identity/session composition is injected. Never fake remote completion.
                return False
            return await self.publication_worker.process(
                intent_id=normalized_intent_id,
                now=now,
            )

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_archive_cleanup(self, *, job_id: str, now: datetime) -> bool:
        normalized_job_id = _normalize_uuid(job_id, field="job_id")

        async def _execute() -> bool:
            if self.cleanup_worker is None:
                # Cleanup storage is deployment-specific. Keep the PostgreSQL job and wake-up
                # durable until a resolver is explicitly composed; never infer a backend from keys.
                return False
            return await self.cleanup_worker.process(job_id=normalized_job_id, now=now)

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def _run_with_heartbeat(
        self,
        *,
        now: datetime,
        action: Callable[[], Awaitable[bool]],
    ) -> bool:
        try:
            result = await action()
        except Exception:
            if self.telemetry is not None:
                self.telemetry.heartbeat_worker(
                    at=now,
                    database_ready=False,
                    broker_ready=True,
                )
            raise
        if self.telemetry is not None:
            self.telemetry.heartbeat_worker(
                at=now,
                database_ready=True,
                broker_ready=True,
            )
        return result

    async def close(self) -> None:
        await self.database.dispose()


def create_worker_runtime(
    *,
    settings: Settings,
    telemetry: OperationsTelemetry | None = None,
) -> WorkerRuntime:
    database_url = settings.database_url
    if database_url is None or not database_url.strip():
        raise RuntimeError("TGCURATOR_DATABASE_URL is required")
    database = AsyncDatabase(database_url)
    return WorkerRuntime(
        database=database,
        range_execution_worker=RangeExecutionWorker(
            repository=SqlAlchemyRangeExecutionWorkerRepository(database)
        ),
        analysis_stage_run_worker=AnalysisStageRunWorker(
            repository=SqlAlchemyAnalysisStageRunRepository(database),
            orchestrator=AnalysisOrchestrator(provider=None),
        ),
        telemetry=telemetry if telemetry is not None else OperationsMonitor(),
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


async def run_video_archive_task(video_asset_id: str, *, settings: Settings | None = None) -> bool:
    """Run one durable video-archive wake-up without inventing missing media composition."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await runtime.handle_video_archive(
            video_asset_id=video_asset_id,
            now=datetime.now(UTC),
        )
    finally:
        await runtime.close()


async def run_analysis_stage_run_task(
    stage_run_id: str, *, settings: Settings | None = None
) -> bool:
    """Run one durable analysis StageRun using the configured external provider, if any."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await runtime.handle_analysis_stage_run(
            stage_run_id=stage_run_id,
            now=datetime.now(UTC),
        )
    finally:
        await runtime.close()


async def run_publication_task(intent_id: str, *, settings: Settings | None = None) -> bool:
    """Run one UUID-only publication wake-up without fabricating a Telegram session."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await runtime.handle_publication(
            intent_id=intent_id,
            now=datetime.now(UTC),
        )
    finally:
        await runtime.close()


async def run_archive_cleanup_task(job_id: str, *, settings: Settings | None = None) -> bool:
    """Run one UUID-only cleanup wake-up without fabricating archive storage composition."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await runtime.handle_archive_cleanup(
            job_id=job_id,
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
