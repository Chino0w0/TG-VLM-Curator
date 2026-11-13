from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import SecretStr

from apps.runtime_dependencies import (
    resolve_database_url,
    upsert_service_heartbeat,
    write_heartbeat_file,
)
from tgcurator.application import MessagePipelineCoordinator, Settings
from tgcurator.application.ports.cleanup import ArchiveCleanupRepository
from tgcurator.application.ports.operations import (
    OperationsBacklogRepository,
    OperationsTelemetry,
)
from tgcurator.application.ports.pipeline import (
    MessagePipelineRepository,
    PipelineProgressionReport,
    WakeupRepairReport,
)
from tgcurator.application.ports.processing import LatestBoundarySource
from tgcurator.application.ports.publishing import PublicationRepository
from tgcurator.application.processing import (
    DurableWakeupDispatcher,
    ProcessingRangeScheduler,
    RangeScheduleReport,
    WakeupDispatchReport,
)
from tgcurator.application.routing import RoutingService
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyArchiveCleanupRepository,
    SqlAlchemyDurableWakeupRepository,
    SqlAlchemyMessagePipelineRepository,
    SqlAlchemyOperationsBacklogRepository,
    SqlAlchemyProcessingRangeScheduleRepository,
    SqlAlchemyPublicationRepository,
    SqlAlchemyRoutingRepository,
)
from tgcurator.infrastructure.observability import OperationsMonitor
from tgcurator.infrastructure.queue import CeleryTaskDispatcher, create_celery_client


class ReanalysisDriver(Protocol):
    async def drive_active(self, *, now: datetime, limit: int = 100) -> Any: ...


@dataclass(frozen=True, slots=True)
class SchedulerCycleReport:
    range_schedule: RangeScheduleReport
    wakeup_dispatch: WakeupDispatchReport
    repaired_publication_wakeups: int = 0
    repaired_cleanup_wakeups: int = 0
    pipeline_repair: WakeupRepairReport = WakeupRepairReport()
    pipeline_progression: PipelineProgressionReport = PipelineProgressionReport()


@dataclass(slots=True)
class SchedulerRuntime:
    """Composition root for one scheduler cycle.

    It coordinates database work, Telegram boundary observation, and the broker dispatcher while
    keeping all broker calls outside the PostgreSQL transactions in repository adapters.
    """

    database: AsyncDatabase
    range_scheduler: ProcessingRangeScheduler
    wakeup_dispatcher: DurableWakeupDispatcher
    publication_repository: PublicationRepository | None = None
    cleanup_repository: ArchiveCleanupRepository | None = None
    pipeline_repository: MessagePipelineRepository | None = None
    pipeline_coordinator: MessagePipelineCoordinator | None = None
    backlog_repository: OperationsBacklogRepository | None = None
    telemetry: OperationsTelemetry | None = None
    reanalysis_repository: ReanalysisDriver | None = None
    telegram_client_pool: Any | None = None

    async def run_once(
        self,
        *,
        now: datetime,
        dispatch_limit: int = 100,
        publication_repair_limit: int = 500,
        cleanup_repair_limit: int = 500,
        pipeline_repair_limit: int = 500,
        pipeline_drive_limit: int = 100,
    ) -> SchedulerCycleReport:
        try:
            pipeline_repair = WakeupRepairReport()
            if self.pipeline_repository is not None:
                pipeline_repair = await self.pipeline_repository.repair_wakeups(
                    now=now,
                    limit=pipeline_repair_limit,
                )
            repaired_publication_wakeups = 0
            if self.publication_repository is not None:
                repaired_publication_wakeups = await self.publication_repository.repair_wakeups(
                    now=now,
                    limit=publication_repair_limit,
                )
            repaired_cleanup_wakeups = 0
            if self.cleanup_repository is not None:
                repaired_cleanup_wakeups = await self.cleanup_repository.repair_wakeups(
                    now=now,
                    limit=cleanup_repair_limit,
                )
            pipeline_progression = PipelineProgressionReport()
            if self.pipeline_coordinator is not None:
                pipeline_progression = await self.pipeline_coordinator.drive_active_executions(
                    now=now,
                    limit=pipeline_drive_limit,
                )
            if self.reanalysis_repository is not None:
                await self.reanalysis_repository.drive_active(now=now, limit=pipeline_drive_limit)
            range_schedule = await self.range_scheduler.schedule(now=now)
            wakeup_dispatch = await self.wakeup_dispatcher.dispatch_due(
                now=now,
                limit=dispatch_limit,
            )
            broker_ready = wakeup_dispatch.failed_dispatches == 0
            if self.telemetry is not None:
                if self.backlog_repository is not None:
                    backlog = await self.backlog_repository.observe_backlog(now=now)
                    self.telemetry.observe_backlog(
                        publication_pending=backlog.publication_pending,
                        publication_partial=backlog.publication_partial,
                        publication_retry_due=backlog.publication_retry_due,
                        cleanup_pending=backlog.cleanup_pending,
                        cleanup_retry_due=backlog.cleanup_retry_due,
                        durable_wakeups_due=backlog.durable_wakeups_due,
                        observed_at=now,
                    )
                self.telemetry.heartbeat_scheduler(
                    at=now,
                    database_ready=True,
                    broker_ready=broker_ready,
                )
            write_heartbeat_file("scheduler", status="running", now=now)
            await upsert_service_heartbeat(
                self.database,
                service_name="scheduler",
                database_ready=True,
                broker_ready=broker_ready,
                now=now,
            )
        except Exception:
            if self.telemetry is not None:
                self.telemetry.heartbeat_scheduler(
                    at=now,
                    database_ready=False,
                    broker_ready=False,
                )
            await upsert_service_heartbeat(
                self.database,
                service_name="scheduler",
                database_ready=False,
                broker_ready=False,
                now=now,
            )
            raise
        return SchedulerCycleReport(
            range_schedule=range_schedule,
            wakeup_dispatch=wakeup_dispatch,
            repaired_publication_wakeups=repaired_publication_wakeups,
            repaired_cleanup_wakeups=repaired_cleanup_wakeups,
            pipeline_repair=pipeline_repair,
            pipeline_progression=pipeline_progression,
        )

    async def close(self) -> None:
        try:
            if self.telegram_client_pool is not None:
                await self.telegram_client_pool.close()
        finally:
            try:
                task_dispatcher = getattr(self.wakeup_dispatcher, "task_dispatcher", None)
                celery_client = getattr(task_dispatcher, "_celery", None)
                close_fn = getattr(celery_client, "close", None)
                if callable(close_fn):
                    close_fn()
            finally:
                await self.database.dispose()


def create_scheduler_runtime(
    *,
    settings: Settings,
    latest_boundary_source: LatestBoundarySource,
    telemetry: OperationsTelemetry | None = None,
) -> SchedulerRuntime:
    """Compose durable scheduling with a Celery producer.

    A concrete latest-boundary source is deliberately injected here; its Telethon adapter arrives
    with M3. M2 never fabricates a LATEST boundary while that provider is unavailable.
    """
    from tgcurator.infrastructure.database.reanalysis_repository import (
        SqlAlchemyReanalysisRepository,
    )

    database_url = _required_value(
        resolve_database_url(settings),
        field="TGCURATOR_DATABASE_URL",
    )
    broker_url = _required_secret(settings.celery_broker_url, field="TGCURATOR_CELERY_BROKER_URL")
    database = AsyncDatabase(database_url)
    range_scheduler = ProcessingRangeScheduler(
        repository=SqlAlchemyProcessingRangeScheduleRepository(database),
        latest_boundary_source=latest_boundary_source,
    )
    wakeup_dispatcher = DurableWakeupDispatcher(
        repository=SqlAlchemyDurableWakeupRepository(database),
        task_dispatcher=CeleryTaskDispatcher(create_celery_client(broker_url=broker_url)),
    )
    pipeline_repository = SqlAlchemyMessagePipelineRepository(database)
    pipeline_coordinator = MessagePipelineCoordinator(
        repository=pipeline_repository,
        routing_service=RoutingService(repository=SqlAlchemyRoutingRepository(database)),
    )
    return SchedulerRuntime(
        database=database,
        range_scheduler=range_scheduler,
        wakeup_dispatcher=wakeup_dispatcher,
        publication_repository=SqlAlchemyPublicationRepository(database),
        cleanup_repository=SqlAlchemyArchiveCleanupRepository(database),
        pipeline_repository=pipeline_repository,
        pipeline_coordinator=pipeline_coordinator,
        backlog_repository=SqlAlchemyOperationsBacklogRepository(database),
        telemetry=telemetry if telemetry is not None else OperationsMonitor(),
        reanalysis_repository=SqlAlchemyReanalysisRepository(database),
    )


def _required_value(value: str | None, *, field: str) -> str:
    if value is None or not value.strip():
        raise RuntimeError(f"{field} is required")
    return value


def _required_secret(value: SecretStr | None, *, field: str) -> str:
    if value is None or not value.get_secret_value().strip():
        raise RuntimeError(f"{field} is required")
    return value.get_secret_value()
