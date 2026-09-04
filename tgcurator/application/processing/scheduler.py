from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from tgcurator.application.ports.contracts import TaskDispatcher, TelegramGateway
from tgcurator.application.ports.processing import (
    DurableWakeupRepository,
    ProcessingRangeScheduleRepository,
)
from tgcurator.domain.processing import BoundaryKind
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration

RANGE_EXECUTION_QUEUE = "range_execution"


@dataclass(frozen=True, slots=True)
class RangeScheduleReport:
    scanned_ranges: int
    created_executions: int


@dataclass(frozen=True, slots=True)
class WakeupDispatchReport:
    claimed_wakeups: int
    dispatched_wakeups: int
    failed_dispatches: int


@dataclass(slots=True)
class ProcessingRangeScheduler:
    """Freeze finite range windows, then persist a wake-up before any task dispatch occurs."""

    repository: ProcessingRangeScheduleRepository
    telegram_gateway: TelegramGateway

    async def schedule(self, *, now: datetime) -> RangeScheduleReport:
        ensure_aware(now, field="now")
        scheduled_ranges = await self.repository.list_enabled_ranges()
        created_executions = 0

        for scheduled_range in scheduled_ranges:
            processing_range = scheduled_range.processing_range
            from_at = scheduled_range.processing_watermark_at or processing_range.start_at
            latest_message_at = None
            if processing_range.boundary_kind is BoundaryKind.LATEST:
                latest_message_at = await self.telegram_gateway.newest_message_at(
                    source_channel_id=scheduled_range.source_channel_id
                )

            execution = processing_range.create_execution(
                execution_id=str(uuid4()),
                from_at=from_at,
                observed_at=now,
                latest_message_at=latest_message_at,
            )
            if execution is None:
                continue

            created = await self.repository.create_execution_and_wakeup(
                execution=execution,
                source_profile_version_id=scheduled_range.source_profile_version_id,
                queue=RANGE_EXECUTION_QUEUE,
                available_at=now,
            )
            created_executions += int(created)

        return RangeScheduleReport(
            scanned_ranges=len(scheduled_ranges),
            created_executions=created_executions,
        )


@dataclass(slots=True)
class DurableWakeupDispatcher:
    """Deliver database-owned wake-ups after leasing them in a short transaction.

    Successful dispatches are deliberately scheduled again after repair_interval.  A Redis or
    Celery loss therefore only creates duplicate wake-ups; it cannot erase business work.
    """

    repository: DurableWakeupRepository
    task_dispatcher: TaskDispatcher
    lease_duration: timedelta = timedelta(minutes=1)
    repair_interval: timedelta = timedelta(minutes=5)
    failure_retry_delay: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        ensure_positive_duration(self.lease_duration, field="lease_duration")
        ensure_positive_duration(self.repair_interval, field="repair_interval")
        ensure_positive_duration(self.failure_retry_delay, field="failure_retry_delay")

    async def dispatch_due(self, *, now: datetime, limit: int = 100) -> WakeupDispatchReport:
        ensure_aware(now, field="now")
        if limit <= 0:
            raise DomainValidationError("limit must be greater than zero")

        claims = await self.repository.claim_due(
            now=now,
            lease_duration=self.lease_duration,
            limit=limit,
        )
        dispatched_wakeups = 0
        failed_dispatches = 0
        for claim in claims:
            try:
                await self.task_dispatcher.dispatch(queue=claim.queue, entity_id=claim.entity_id)
            except Exception:
                failed_dispatches += 1
                await self.repository.reschedule_after_failure(
                    wakeup_id=claim.wakeup_id,
                    lease_token=claim.lease_token,
                    retry_after=now + self.failure_retry_delay,
                )
            else:
                dispatched_wakeups += 1
                await self.repository.acknowledge_dispatch(
                    wakeup_id=claim.wakeup_id,
                    lease_token=claim.lease_token,
                    now=now,
                    repair_after=now + self.repair_interval,
                )

        return WakeupDispatchReport(
            claimed_wakeups=len(claims),
            dispatched_wakeups=dispatched_wakeups,
            failed_dispatches=failed_dispatches,
        )
