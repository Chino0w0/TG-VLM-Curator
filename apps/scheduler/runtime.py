from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr

from tgcurator.application import Settings
from tgcurator.application.ports.cleanup import ArchiveCleanupRepository
from tgcurator.application.ports.operations import (
    OperationsBacklogRepository,
    OperationsTelemetry,
)
from tgcurator.application.ports.processing import LatestBoundarySource
from tgcurator.application.ports.publishing import PublicationRepository
from tgcurator.application.processing import (
    DurableWakeupDispatcher,
    ProcessingRangeScheduler,
    RangeScheduleReport,
    WakeupDispatchReport,
)
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyArchiveCleanupRepository,
    SqlAlchemyDurableWakeupRepository,
    SqlAlchemyOperationsBacklogRepository,
    SqlAlchemyProcessingRangeScheduleRepository,
    SqlAlchemyPublicationRepository,
)
from tgcurator.infrastructure.observability import OperationsMonitor
from tgcurator.infrastructure.queue import CeleryTaskDispatcher, create_celery_client


@dataclass(frozen=True, slots=True)
class SchedulerCycleReport:
    range_schedule: RangeScheduleReport
    wakeup_dispatch: WakeupDispatchReport
    repaired_publication_wakeups: int = 0
    repaired_cleanup_wakeups: int = 0


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
    backlog_repository: OperationsBacklogRepository | None = None
    telemetry: OperationsTelemetry | None = None

    async def run_once(
        self,
        *,
        now: datetime,
        dispatch_limit: int = 100,
        publication_repair_limit: int = 500,
        cleanup_repair_limit: int = 500,
    ) -> SchedulerCycleReport:
        try:
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
            range_schedule = await self.range_scheduler.schedule(now=now)
            wakeup_dispatch = await self.wakeup_dispatcher.dispatch_due(
                now=now,
                limit=dispatch_limit,
            )
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
                    broker_ready=wakeup_dispatch.failed_dispatches == 0,
                )
        except Exception:
            if self.telemetry is not None:
                self.telemetry.heartbeat_scheduler(
                    at=now,
                    database_ready=False,
                    broker_ready=False,
                )
            raise
        return SchedulerCycleReport(
            range_schedule=range_schedule,
            wakeup_dispatch=wakeup_dispatch,
            repaired_publication_wakeups=repaired_publication_wakeups,
            repaired_cleanup_wakeups=repaired_cleanup_wakeups,
        )

    async def close(self) -> None:
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

    database_url = _required_value(settings.database_url, field="TGCURATOR_DATABASE_URL")
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
    return SchedulerRuntime(
        database=database,
        range_scheduler=range_scheduler,
        wakeup_dispatcher=wakeup_dispatcher,
        publication_repository=SqlAlchemyPublicationRepository(database),
        cleanup_repository=SqlAlchemyArchiveCleanupRepository(database),
        backlog_repository=SqlAlchemyOperationsBacklogRepository(database),
        telemetry=telemetry if telemetry is not None else OperationsMonitor(),
    )


def _required_value(value: str | None, *, field: str) -> str:
    if value is None or not value.strip():
        raise RuntimeError(f"{field} is required")
    return value


def _required_secret(value: SecretStr | None, *, field: str) -> str:
    if value is None or not value.get_secret_value().strip():
        raise RuntimeError(f"{field} is required")
    return value.get_secret_value()
