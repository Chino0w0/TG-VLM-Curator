from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class PublicationMetric(StrEnum):
    """Bounded publication events suitable for deterministic in-process counters."""

    CLAIMED = "claimed"
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILED_PART = "reconciled_part"


@dataclass(frozen=True, slots=True)
class PublicationMetricsSnapshot:
    claimed: int = 0
    sent: int = 0
    retry_scheduled: int = 0
    failed: int = 0
    cancelled: int = 0
    reconciled_parts: int = 0


@dataclass(frozen=True, slots=True)
class FloodWaitMetricsSnapshot:
    events: int = 0
    wait_seconds_total: float = 0.0
    wait_seconds_max: float = 0.0
    last_observed_at: datetime | None = None
    last_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BacklogMetricsSnapshot:
    publication_pending: int = 0
    publication_partial: int = 0
    publication_retry_due: int = 0
    cleanup_pending: int = 0
    cleanup_retry_due: int = 0
    durable_wakeups_due: int = 0
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ServiceHealthSnapshot:
    service: str
    checked_at: datetime
    last_heartbeat_at: datetime | None
    alive: bool
    ready: bool
    database_ready: bool
    broker_ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationsSnapshot:
    publication: PublicationMetricsSnapshot
    flood_wait: FloodWaitMetricsSnapshot
    backlog: BacklogMetricsSnapshot
    worker: ServiceHealthSnapshot
    scheduler: ServiceHealthSnapshot


class OperationsTelemetry(Protocol):
    """Framework-neutral runtime operations telemetry port."""

    def record_publication(
        self,
        event: PublicationMetric | str,
        *,
        count: int = 1,
    ) -> None: ...

    def record_flood_wait(self, *, observed_at: datetime, retry_at: datetime) -> None: ...

    def observe_backlog(
        self,
        *,
        publication_pending: int,
        publication_partial: int,
        publication_retry_due: int,
        cleanup_pending: int,
        cleanup_retry_due: int,
        durable_wakeups_due: int,
        observed_at: datetime,
    ) -> None: ...

    def heartbeat_worker(
        self,
        *,
        at: datetime,
        database_ready: bool,
        broker_ready: bool,
    ) -> None: ...

    def heartbeat_scheduler(
        self,
        *,
        at: datetime,
        database_ready: bool,
        broker_ready: bool,
    ) -> None: ...

    def worker_health(self, *, now: datetime) -> ServiceHealthSnapshot: ...

    def scheduler_health(self, *, now: datetime) -> ServiceHealthSnapshot: ...

    def snapshot(self, *, now: datetime) -> OperationsSnapshot: ...


class OperationsBacklogRepository(Protocol):
    """Read-only PostgreSQL backlog observer for reconstructable operational gauges."""

    async def observe_backlog(self, *, now: datetime) -> BacklogMetricsSnapshot: ...
