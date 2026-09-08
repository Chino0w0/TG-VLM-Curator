from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from tgcurator.application.ports import TaskDispatcher
from tgcurator.application.ports.processing import (
    DurableWakeupRepository,
    LatestBoundarySource,
    PendingRangeExecution,
    ProcessingRangeScheduleRepository,
)
from tgcurator.domain.processing import latest_boundary_is_stable
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration

RANGE_EXECUTION_QUEUE = "range_execution"


@dataclass(frozen=True, slots=True)
class RangeScheduleReport:
    scanned_ranges: int
    created_executions: int
    skipped_ranges: int = 0


@dataclass(frozen=True, slots=True)
class WakeupDispatchReport:
    claimed_wakeups: int
    dispatched_wakeups: int
    failed_dispatches: int


@dataclass(slots=True)
class ProcessingRangeScheduler:
    """Freeze finite message-ID windows before any worker is dispatched."""

    repository: ProcessingRangeScheduleRepository
    latest_boundary_source: LatestBoundarySource
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise DomainValidationError("max_attempts must be greater than zero")

    async def schedule(self, *, now: datetime) -> RangeScheduleReport:
        ensure_aware(now, field="now")
        scheduled_ranges = await self.repository.list_enabled_ranges()
        created_executions = 0

        for scheduled_range in scheduled_ranges:
            from_message_id = (
                scheduled_range.processing_watermark_message_id
                if scheduled_range.processing_watermark_message_id is not None
                else scheduled_range.resolved_start_message_id - 1
            )
            to_message_id: int | None

            if scheduled_range.end_mode == "fixed":
                to_message_id = scheduled_range.resolved_fixed_end_message_id
            elif scheduled_range.end_mode == "latest":
                boundary = await self.latest_boundary_source.newest_message(
                    source_channel_id=scheduled_range.source_channel_id
                )
                quiet_seconds = scheduled_range.steady_after_seconds
                if boundary is None or quiet_seconds is None:
                    continue
                if not latest_boundary_is_stable(
                    boundary.published_at,
                    now,
                    timedelta(seconds=quiet_seconds),
                ):
                    continue
                to_message_id = boundary.message_id
            else:
                raise DomainValidationError(
                    f"unsupported persisted processing range end mode: {scheduled_range.end_mode!r}"
                )

            if to_message_id is None or to_message_id <= from_message_id:
                continue

            created = await self.repository.create_execution_and_wakeup(
                execution=PendingRangeExecution(
                    execution_id=str(uuid4()),
                    processing_range_id=scheduled_range.range_id,
                    source_profile_version_id=scheduled_range.source_profile_version_id,
                    from_message_id_exclusive=from_message_id,
                    to_message_id_inclusive=to_message_id,
                    max_attempts=self.max_attempts,
                ),
                queue=RANGE_EXECUTION_QUEUE,
                available_at=now,
            )
            created_executions += int(created)

        return RangeScheduleReport(
            scanned_ranges=len(scheduled_ranges),
            created_executions=created_executions,
            skipped_ranges=len(scheduled_ranges) - created_executions,
        )


@dataclass(slots=True)
class DurableWakeupDispatcher:
    """Deliver database-owned wake-ups outside their short claim transaction."""

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
                    now=now,
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
