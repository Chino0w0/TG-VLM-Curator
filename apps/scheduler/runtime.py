from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr

from tgcurator.application import Settings
from tgcurator.application.ports.contracts import TelegramGateway
from tgcurator.application.processing import (
    DurableWakeupDispatcher,
    ProcessingRangeScheduler,
    RangeScheduleReport,
    WakeupDispatchReport,
)
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyDurableWakeupRepository,
    SqlAlchemyProcessingRangeScheduleRepository,
)
from tgcurator.infrastructure.queue import CeleryTaskDispatcher, create_celery_client


@dataclass(frozen=True, slots=True)
class SchedulerCycleReport:
    range_schedule: RangeScheduleReport
    wakeup_dispatch: WakeupDispatchReport


@dataclass(slots=True)
class SchedulerRuntime:
    """Composition root for one scheduler cycle.

    It coordinates database work, Telegram boundary observation, and the broker dispatcher while
    keeping all broker calls outside the PostgreSQL transactions in repository adapters.
    """

    database: AsyncDatabase
    range_scheduler: ProcessingRangeScheduler
    wakeup_dispatcher: DurableWakeupDispatcher

    async def run_once(self, *, now: datetime, dispatch_limit: int = 100) -> SchedulerCycleReport:
        range_schedule = await self.range_scheduler.schedule(now=now)
        wakeup_dispatch = await self.wakeup_dispatcher.dispatch_due(now=now, limit=dispatch_limit)
        return SchedulerCycleReport(
            range_schedule=range_schedule,
            wakeup_dispatch=wakeup_dispatch,
        )

    async def close(self) -> None:
        await self.database.dispose()


def create_scheduler_runtime(
    *,
    settings: Settings,
    telegram_gateway: TelegramGateway,
) -> SchedulerRuntime:
    """Compose durable scheduling with a Celery producer.

    A concrete Telegram gateway is deliberately injected here; its Telethon implementation arrives
    with M3. This keeps M2 scheduling usable for FIXED ranges without smuggling Telegram concerns
    into application services.
    """

    database_url = _required_value(settings.database_url, field="TGCURATOR_DATABASE_URL")
    broker_url = _required_secret(settings.celery_broker_url, field="TGCURATOR_CELERY_BROKER_URL")
    database = AsyncDatabase(database_url)
    range_scheduler = ProcessingRangeScheduler(
        repository=SqlAlchemyProcessingRangeScheduleRepository(database),
        telegram_gateway=telegram_gateway,
    )
    wakeup_dispatcher = DurableWakeupDispatcher(
        repository=SqlAlchemyDurableWakeupRepository(database),
        task_dispatcher=CeleryTaskDispatcher(create_celery_client(broker_url=broker_url)),
    )
    return SchedulerRuntime(
        database=database,
        range_scheduler=range_scheduler,
        wakeup_dispatcher=wakeup_dispatcher,
    )


def _required_value(value: str | None, *, field: str) -> str:
    if value is None or not value.strip():
        raise RuntimeError(f"{field} is required")
    return value


def _required_secret(value: SecretStr | None, *, field: str) -> str:
    if value is None or not value.get_secret_value().strip():
        raise RuntimeError(f"{field} is required")
    return value.get_secret_value()
