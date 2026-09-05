from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from tgcurator.domain.processing import ProcessingRange, RangeExecution


@dataclass(frozen=True, slots=True)
class ScheduledProcessingRange:
    """Persisted range state required to freeze its next finite execution window."""

    processing_range: ProcessingRange
    source_channel_id: str
    source_profile_version_id: str
    processing_watermark_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClaimedWakeup:
    """A short-lived scheduler lease over one durable wake-up signal."""

    wakeup_id: str
    queue: str
    entity_id: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class ClaimedRangeExecution:
    """A short-lived worker lease over a finite, immutable execution window."""

    execution: RangeExecution
    source_channel_id: str
    source_profile_version_id: str
    lease_token: str


class ProcessingRangeScheduleRepository(Protocol):
    """Transactional range/execution persistence boundary."""

    async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
        """Return enabled ranges and their durable processing watermark."""

    async def create_execution_and_wakeup(
        self,
        *,
        execution: RangeExecution,
        source_profile_version_id: str,
        queue: str,
        available_at: datetime,
    ) -> bool:
        """Atomically insert a finite execution and its durable wake-up if both are new."""


class DurableWakeupRepository(Protocol):
    """Database-backed wake-up leases; dispatcher delivery is deliberately at-least-once."""

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedWakeup, ...]:
        """Claim due or expired wake-ups without waiting on competing schedulers."""

    async def acknowledge_dispatch(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        now: datetime,
        repair_after: datetime,
    ) -> bool:
        """Release a dispatched signal for future repair while its entity remains unfinished."""

    async def reschedule_after_failure(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        retry_after: datetime,
    ) -> bool:
        """Release a failed dispatch lease without retaining exception details."""

    async def complete_for_entity(self, *, queue: str, entity_id: str, now: datetime) -> bool:
        """Stop repair wake-ups once the entity reaches a durable terminal state."""


class RangeExecutionWorkerRepository(Protocol):
    """Lease and persist finite execution progress without exposing an ORM session."""

    async def claim_execution(
        self,
        *,
        execution_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedRangeExecution | None:
        """Claim a pending or expired execution lease, or return None if another worker owns it."""

    async def advance_watermark(
        self,
        *,
        execution_id: str,
        lease_token: str,
        watermark_at: datetime,
        now: datetime,
    ) -> bool:
        """Persist a monotonic in-window watermark for an active worker lease."""

    async def complete_execution(
        self,
        *,
        execution_id: str,
        lease_token: str,
        now: datetime,
    ) -> bool:
        """Atomically complete a fully-watermarked execution and advance its range watermark."""
