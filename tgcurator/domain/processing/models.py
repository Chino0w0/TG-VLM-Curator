from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration


class BoundaryKind(StrEnum):
    FIXED = "fixed"
    LATEST = "latest"


class RangeExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessingRange:
    range_id: str
    start_at: datetime
    boundary_kind: BoundaryKind
    fixed_end_at: datetime | None = None
    latest_quiet_period: timedelta | None = None

    def __post_init__(self) -> None:
        if not self.range_id.strip():
            raise DomainValidationError("range_id must not be blank")
        ensure_aware(self.start_at, field="start_at")
        if self.boundary_kind is BoundaryKind.FIXED:
            if self.fixed_end_at is None:
                raise DomainValidationError("a fixed range requires fixed_end_at")
            ensure_aware(self.fixed_end_at, field="fixed_end_at")
            if self.fixed_end_at <= self.start_at:
                raise DomainValidationError("fixed_end_at must be later than start_at")
            if self.latest_quiet_period is not None:
                raise DomainValidationError("a fixed range cannot define latest_quiet_period")
        else:
            if self.fixed_end_at is not None:
                raise DomainValidationError("a latest range cannot define fixed_end_at")
            if self.latest_quiet_period is None:
                raise DomainValidationError("a latest range requires latest_quiet_period")
            ensure_positive_duration(self.latest_quiet_period, field="latest_quiet_period")

    def create_execution(
        self,
        *,
        execution_id: str,
        from_at: datetime,
        observed_at: datetime | None = None,
        latest_message_at: datetime | None = None,
    ) -> RangeExecution | None:
        ensure_aware(from_at, field="from_at")
        if from_at < self.start_at:
            raise DomainValidationError("execution from_at cannot precede range start_at")
        if self.boundary_kind is BoundaryKind.FIXED:
            assert self.fixed_end_at is not None
            if from_at >= self.fixed_end_at:
                return None
            return RangeExecution(execution_id, self.range_id, from_at, self.fixed_end_at)

        if observed_at is None:
            raise DomainValidationError("a latest range requires observed_at")
        ensure_aware(observed_at, field="observed_at")
        if latest_message_at is None:
            return None
        ensure_aware(latest_message_at, field="latest_message_at")
        assert self.latest_quiet_period is not None
        if not latest_boundary_is_stable(latest_message_at, observed_at, self.latest_quiet_period):
            return None
        if latest_message_at <= from_at:
            return None
        return RangeExecution(execution_id, self.range_id, from_at, latest_message_at)


@dataclass(frozen=True, slots=True)
class RangeExecution:
    execution_id: str
    range_id: str
    from_at: datetime
    to_at: datetime
    watermark_at: datetime | None = None
    status: RangeExecutionStatus = RangeExecutionStatus.PENDING

    def __post_init__(self) -> None:
        if not self.execution_id.strip() or not self.range_id.strip():
            raise DomainValidationError("execution_id and range_id must not be blank")
        ensure_aware(self.from_at, field="from_at")
        ensure_aware(self.to_at, field="to_at")
        if self.from_at >= self.to_at:
            raise DomainValidationError("execution from_at must be earlier than to_at")
        if self.watermark_at is not None:
            ensure_aware(self.watermark_at, field="watermark_at")
            if not self.from_at <= self.watermark_at <= self.to_at:
                raise DomainValidationError("watermark_at must stay inside execution bounds")
        if self.status is RangeExecutionStatus.COMPLETED and self.watermark_at != self.to_at:
            raise DomainValidationError("a completed execution watermark must equal to_at")

    def advance_watermark(self, watermark_at: datetime) -> RangeExecution:
        ensure_aware(watermark_at, field="watermark_at")
        if self.status in {RangeExecutionStatus.COMPLETED, RangeExecutionStatus.FAILED}:
            raise DomainValidationError("a terminal execution cannot advance its watermark")
        if not self.from_at <= watermark_at <= self.to_at:
            raise DomainValidationError("watermark_at must stay inside execution bounds")
        if self.watermark_at is not None and watermark_at < self.watermark_at:
            raise DomainValidationError("watermark_at must be monotonic")
        return replace(self, watermark_at=watermark_at, status=RangeExecutionStatus.RUNNING)

    def complete(self) -> RangeExecution:
        if self.status is RangeExecutionStatus.FAILED:
            raise DomainValidationError("a failed execution cannot be completed")
        if self.watermark_at != self.to_at:
            raise DomainValidationError("execution cannot complete before its right boundary")
        return replace(self, status=RangeExecutionStatus.COMPLETED)


def latest_boundary_is_stable(
    latest_message_at: datetime,
    observed_at: datetime,
    quiet_period: timedelta,
) -> bool:
    """Whether the observed latest message has stayed unchanged long enough to freeze a window."""
    ensure_aware(latest_message_at, field="latest_message_at")
    ensure_aware(observed_at, field="observed_at")
    ensure_positive_duration(quiet_period, field="quiet_period")
    return observed_at >= latest_message_at + quiet_period
