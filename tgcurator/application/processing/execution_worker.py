from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tgcurator.application.ports.processing import (
    ClaimedRangeExecution,
    RangeExecutionWorkerRepository,
)
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration


@dataclass(slots=True)
class RangeExecutionWorker:
    """Lease a finite execution and persist monotonic message-ID progress."""

    repository: RangeExecutionWorkerRepository
    lease_duration: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        ensure_positive_duration(self.lease_duration, field="lease_duration")

    async def claim(self, *, execution_id: str, now: datetime) -> ClaimedRangeExecution | None:
        ensure_aware(now, field="now")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise DomainValidationError("execution_id must not be blank")
        return await self.repository.claim_execution(
            execution_id=execution_id,
            now=now,
            lease_duration=self.lease_duration,
        )

    async def advance_watermark(
        self,
        *,
        execution_id: str,
        lease_token: str,
        watermark_message_id: int,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        self._validate_identity(execution_id=execution_id, lease_token=lease_token)
        if isinstance(watermark_message_id, bool) or not isinstance(watermark_message_id, int):
            raise DomainValidationError("watermark_message_id must be an integer")
        if watermark_message_id < 0:
            raise DomainValidationError("watermark_message_id must not be negative")
        return await self.repository.advance_watermark(
            execution_id=execution_id,
            lease_token=lease_token,
            watermark_message_id=watermark_message_id,
            now=now,
        )

    async def complete(self, *, execution_id: str, lease_token: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        self._validate_identity(execution_id=execution_id, lease_token=lease_token)
        return await self.repository.complete_execution(
            execution_id=execution_id,
            lease_token=lease_token,
            now=now,
        )

    async def fail(
        self,
        *,
        execution_id: str,
        lease_token: str,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_delay: timedelta,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        ensure_positive_duration(retry_delay, field="retry_delay")
        self._validate_identity(execution_id=execution_id, lease_token=lease_token)
        normalized_code = self._validate_error_label(error_code, field="error_code")
        normalized_type = self._validate_error_label(error_type, field="error_type")
        if not isinstance(retryable, bool):
            raise DomainValidationError("retryable must be a boolean")
        return await self.repository.fail_execution(
            execution_id=execution_id,
            lease_token=lease_token,
            error_code=normalized_code,
            error_type=normalized_type,
            retryable=retryable,
            retry_at=now + retry_delay,
            now=now,
        )

    @staticmethod
    def _validate_identity(*, execution_id: str, lease_token: str) -> None:
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise DomainValidationError("execution_id must not be blank")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise DomainValidationError("lease_token must not be blank")

    @staticmethod
    def _validate_error_label(value: str, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise DomainValidationError(f"{field} must not be blank")
        normalized = value.strip()
        if len(normalized) > 128:
            raise DomainValidationError(f"{field} must be at most 128 characters")
        return normalized
