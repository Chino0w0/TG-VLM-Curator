from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

from tgcurator.application.ports.operations import (
    BacklogMetricsSnapshot,
    FloodWaitMetricsSnapshot,
    OperationsSnapshot,
    PublicationMetric,
    PublicationMetricsSnapshot,
    ServiceHealthSnapshot,
)
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration


@dataclass(slots=True)
class _ServiceState:
    last_heartbeat_at: datetime | None = None
    database_ready: bool = False
    broker_ready: bool = False


class OperationsMonitor:
    """Thread-safe, process-local publication and service operations telemetry.

    PostgreSQL remains the business source of truth. These counters and gauges are deliberately
    reconstructable and may reset on process restart. Worker and scheduler readiness only model the
    required PostgreSQL and broker dependencies; a degraded Telegram identity or inference provider
    is a business-capability signal and does not make either process unready.
    """

    def __init__(self, *, liveness_timeout: timedelta = timedelta(minutes=2)) -> None:
        ensure_positive_duration(liveness_timeout, field="liveness_timeout")
        self._liveness_timeout = liveness_timeout
        self._lock = Lock()
        self._publication = {event: 0 for event in PublicationMetric}
        self._flood_wait_events = 0
        self._flood_wait_seconds_total = 0.0
        self._flood_wait_seconds_max = 0.0
        self._last_flood_wait_observed_at: datetime | None = None
        self._last_flood_wait_retry_at: datetime | None = None
        self._backlog = BacklogMetricsSnapshot()
        self._worker = _ServiceState()
        self._scheduler = _ServiceState()

    def record_publication(
        self,
        event: PublicationMetric | str,
        *,
        count: int = 1,
    ) -> None:
        normalized = _publication_metric(event)
        increment = _nonnegative_int(count, field="count")
        if increment == 0:
            return
        with self._lock:
            self._publication[normalized] += increment

    def record_flood_wait(self, *, observed_at: datetime, retry_at: datetime) -> None:
        ensure_aware(observed_at, field="observed_at")
        ensure_aware(retry_at, field="retry_at")
        wait_seconds = (retry_at - observed_at).total_seconds()
        if wait_seconds < 0:
            raise DomainValidationError("retry_at must not be before observed_at")
        with self._lock:
            self._flood_wait_events += 1
            self._flood_wait_seconds_total += wait_seconds
            self._flood_wait_seconds_max = max(self._flood_wait_seconds_max, wait_seconds)
            self._last_flood_wait_observed_at = observed_at
            self._last_flood_wait_retry_at = retry_at

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
    ) -> None:
        ensure_aware(observed_at, field="observed_at")
        snapshot = BacklogMetricsSnapshot(
            publication_pending=_nonnegative_int(publication_pending, field="publication_pending"),
            publication_partial=_nonnegative_int(publication_partial, field="publication_partial"),
            publication_retry_due=_nonnegative_int(
                publication_retry_due, field="publication_retry_due"
            ),
            cleanup_pending=_nonnegative_int(cleanup_pending, field="cleanup_pending"),
            cleanup_retry_due=_nonnegative_int(cleanup_retry_due, field="cleanup_retry_due"),
            durable_wakeups_due=_nonnegative_int(durable_wakeups_due, field="durable_wakeups_due"),
            observed_at=observed_at,
        )
        with self._lock:
            self._backlog = snapshot

    def heartbeat_worker(
        self,
        *,
        at: datetime,
        database_ready: bool,
        broker_ready: bool,
    ) -> None:
        self._heartbeat(
            state=self._worker,
            at=at,
            database_ready=database_ready,
            broker_ready=broker_ready,
        )

    def heartbeat_scheduler(
        self,
        *,
        at: datetime,
        database_ready: bool,
        broker_ready: bool,
    ) -> None:
        self._heartbeat(
            state=self._scheduler,
            at=at,
            database_ready=database_ready,
            broker_ready=broker_ready,
        )

    def worker_health(self, *, now: datetime) -> ServiceHealthSnapshot:
        ensure_aware(now, field="now")
        with self._lock:
            return self._health_snapshot(service="worker", state=self._worker, now=now)

    def scheduler_health(self, *, now: datetime) -> ServiceHealthSnapshot:
        ensure_aware(now, field="now")
        with self._lock:
            return self._health_snapshot(service="scheduler", state=self._scheduler, now=now)

    def snapshot(self, *, now: datetime) -> OperationsSnapshot:
        ensure_aware(now, field="now")
        with self._lock:
            return OperationsSnapshot(
                publication=self._publication_snapshot(),
                flood_wait=self._flood_wait_snapshot(),
                backlog=self._backlog,
                worker=self._health_snapshot(service="worker", state=self._worker, now=now),
                scheduler=self._health_snapshot(
                    service="scheduler", state=self._scheduler, now=now
                ),
            )

    def _heartbeat(
        self,
        *,
        state: _ServiceState,
        at: datetime,
        database_ready: bool,
        broker_ready: bool,
    ) -> None:
        ensure_aware(at, field="at")
        if not isinstance(database_ready, bool):
            raise DomainValidationError("database_ready must be a boolean")
        if not isinstance(broker_ready, bool):
            raise DomainValidationError("broker_ready must be a boolean")
        with self._lock:
            if state.last_heartbeat_at is not None and at < state.last_heartbeat_at:
                at = state.last_heartbeat_at
            state.last_heartbeat_at = at
            state.database_ready = database_ready
            state.broker_ready = broker_ready

    def _publication_snapshot(self) -> PublicationMetricsSnapshot:
        return PublicationMetricsSnapshot(
            claimed=self._publication[PublicationMetric.CLAIMED],
            sent=self._publication[PublicationMetric.SENT],
            retry_scheduled=self._publication[PublicationMetric.RETRY_SCHEDULED],
            failed=self._publication[PublicationMetric.FAILED],
            cancelled=self._publication[PublicationMetric.CANCELLED],
            reconciled_parts=self._publication[PublicationMetric.RECONCILED_PART],
        )

    def _flood_wait_snapshot(self) -> FloodWaitMetricsSnapshot:
        return FloodWaitMetricsSnapshot(
            events=self._flood_wait_events,
            wait_seconds_total=self._flood_wait_seconds_total,
            wait_seconds_max=self._flood_wait_seconds_max,
            last_observed_at=self._last_flood_wait_observed_at,
            last_retry_at=self._last_flood_wait_retry_at,
        )

    def _health_snapshot(
        self,
        *,
        service: str,
        state: _ServiceState,
        now: datetime,
    ) -> ServiceHealthSnapshot:
        heartbeat = state.last_heartbeat_at
        alive = (
            heartbeat is not None and now >= heartbeat and now - heartbeat <= self._liveness_timeout
        )
        blockers: list[str] = []
        if heartbeat is None:
            blockers.append("heartbeat_missing")
        elif not alive:
            blockers.append("heartbeat_stale")
        if not state.database_ready:
            blockers.append("database_unavailable")
        if not state.broker_ready:
            blockers.append("broker_unavailable")
        ready = alive and state.database_ready and state.broker_ready
        return ServiceHealthSnapshot(
            service=service,
            checked_at=now,
            last_heartbeat_at=heartbeat,
            alive=alive,
            ready=ready,
            database_ready=state.database_ready,
            broker_ready=state.broker_ready,
            blockers=tuple(blockers),
        )


def _publication_metric(value: PublicationMetric | str) -> PublicationMetric:
    try:
        return value if isinstance(value, PublicationMetric) else PublicationMetric(value)
    except (TypeError, ValueError) as error:
        raise DomainValidationError("unsupported publication metric") from error


def _nonnegative_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DomainValidationError(f"{field} must be a non-negative integer")
    return value
