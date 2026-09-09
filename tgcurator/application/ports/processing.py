from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LatestMessageBoundary:
    message_id: int
    published_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduledProcessingRange:
    range_id: str
    source_channel_id: str
    source_profile_version_id: str
    end_mode: str
    resolved_start_message_id: int
    resolved_fixed_end_message_id: int | None
    processing_watermark_message_id: int | None
    steady_after_seconds: int | None


@dataclass(frozen=True, slots=True)
class PendingRangeExecution:
    execution_id: str
    processing_range_id: str
    source_profile_version_id: str
    from_message_id_exclusive: int
    to_message_id_inclusive: int
    max_attempts: int = 5


@dataclass(frozen=True, slots=True)
class ClaimedWakeup:
    wakeup_id: str
    queue: str
    entity_id: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class ClaimedRangeExecution:
    execution_id: str
    processing_range_id: str
    source_channel_id: str
    source_profile_version_id: str
    from_message_id_exclusive: int
    to_message_id_inclusive: int
    watermark_message_id: int
    attempt_count: int
    max_attempts: int
    lease_token: str


class LatestBoundarySource(Protocol):
    async def newest_message(self, *, source_channel_id: str) -> LatestMessageBoundary | None: ...


class ProcessingRangeScheduleRepository(Protocol):
    async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]: ...

    async def create_execution_and_wakeup(
        self, *, execution: PendingRangeExecution, queue: str, available_at: datetime
    ) -> bool: ...


class DurableWakeupRepository(Protocol):
    async def claim_due(
        self, *, now: datetime, lease_duration: timedelta, limit: int
    ) -> tuple[ClaimedWakeup, ...]: ...

    async def acknowledge_dispatch(
        self, *, wakeup_id: str, lease_token: str, now: datetime, repair_after: datetime
    ) -> bool: ...

    async def reschedule_after_failure(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        now: datetime,
        retry_after: datetime,
    ) -> bool: ...

    async def complete_for_entity(self, *, queue: str, entity_id: str, now: datetime) -> bool: ...


class RangeExecutionWorkerRepository(Protocol):
    async def claim_execution(
        self, *, execution_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedRangeExecution | None: ...

    async def advance_watermark(
        self, *, execution_id: str, lease_token: str, watermark_message_id: int, now: datetime
    ) -> bool: ...

    async def complete_execution(
        self, *, execution_id: str, lease_token: str, now: datetime
    ) -> bool: ...

    async def fail_execution(
        self,
        *,
        execution_id: str,
        lease_token: str,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool: ...
