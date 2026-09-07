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
    """Worker-facing state-machine use case.

    Media, Telegram, and inference effects intentionally stay outside this use case. A caller first
    obtains a committed lease, performs bounded external work, then records progress or completion
    in a separate short transaction.
    """

    repository: RangeExecutionWorkerRepository
    lease_duration: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        ensure_positive_duration(self.lease_duration, field="lease_duration")

    async def claim(self, *, execution_id: str, now: datetime) -> ClaimedRangeExecution | None:
        ensure_aware(now, field="now")
        if not execution_id.strip():
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
        watermark_at: datetime,
        now: datetime,
    ) -> bool:
        ensure_aware(watermark_at, field="watermark_at")
        ensure_aware(now, field="now")
        if not execution_id.strip() or not lease_token.strip():
            raise DomainValidationError("execution_id and lease_token must not be blank")
        return await self.repository.advance_watermark(
            execution_id=execution_id,
            lease_token=lease_token,
            watermark_at=watermark_at,
            now=now,
        )

    async def complete(self, *, execution_id: str, lease_token: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        if not execution_id.strip() or not lease_token.strip():
            raise DomainValidationError("execution_id and lease_token must not be blank")
        return await self.repository.complete_execution(
            execution_id=execution_id,
            lease_token=lease_token,
            now=now,
        )
