from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from tgcurator.application.ports.cleanup import (
    ArchiveCleanupRepository,
    ArchiveStorageResolver,
    ClaimedArchiveCleanupJob,
    ClaimedCleanupItem,
)
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration

CLEANUP_QUEUE = "maintenance"


@dataclass(slots=True)
class ArchiveCleanupWorker:
    """Delete frozen archive blobs outside database transactions and finalize metadata."""

    repository: ArchiveCleanupRepository
    storage_resolver: ArchiveStorageResolver
    lease_duration: timedelta = timedelta(minutes=5)
    failure_retry_delay: timedelta = timedelta(minutes=1)
    clock: Callable[[], datetime] | None = None

    def __post_init__(self) -> None:
        ensure_positive_duration(self.lease_duration, field="lease_duration")
        ensure_positive_duration(self.failure_retry_delay, field="failure_retry_delay")

    async def process(self, *, job_id: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        _require_uuid(job_id, field="job_id")
        started_monotonic = time.monotonic()
        claim = await self.repository.claim(
            job_id=job_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if claim is None:
            await self.repository.complete_wakeup_if_terminal(job_id=job_id, now=now)
            return False

        while True:
            step_now = self._current_now(initial_now=now, started_monotonic=started_monotonic)
            item = await self.repository.claim_next_item(claim=claim, now=step_now)
            if item is None:
                done_now = self._current_now(initial_now=now, started_monotonic=started_monotonic)
                return await self.repository.complete(claim=claim, now=done_now)
            try:
                await self._delete_blobs(item=item)
            except Exception as error:
                fail_now = self._current_now(initial_now=now, started_monotonic=started_monotonic)
                return await self._fail(
                    claim=claim,
                    item=item,
                    error_code="ARCHIVE_CLEANUP_DELETE_FAILED",
                    error=error,
                    now=fail_now,
                )
            finalize_now = self._current_now(initial_now=now, started_monotonic=started_monotonic)
            try:
                finalized = await self.repository.finalize_item_deleted(
                    claim=claim,
                    item=item,
                    now=finalize_now,
                )
            except Exception as error:
                fail_now = self._current_now(initial_now=now, started_monotonic=started_monotonic)
                return await self._fail(
                    claim=claim,
                    item=item,
                    error_code="ARCHIVE_CLEANUP_FINALIZE_FAILED",
                    error=error,
                    now=fail_now,
                )
            if not finalized:
                return False

    def _current_now(self, *, initial_now: datetime, started_monotonic: float) -> datetime:
        if self.clock is not None:
            observed = self.clock()
            ensure_aware(observed, field="clock")
            return observed if observed >= initial_now else initial_now
        elapsed = time.monotonic() - started_monotonic
        if elapsed >= 1.0:
            return initial_now + timedelta(seconds=elapsed)
        return initial_now

    async def _delete_blobs(self, *, item: ClaimedCleanupItem) -> None:
        for blob in item.asset.blobs:
            storage = self.storage_resolver.resolve(storage_backend=blob.storage_backend)
            await storage.delete(key=blob.storage_key)

    async def _fail(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        item: ClaimedCleanupItem,
        error_code: str,
        error: Exception,
        now: datetime,
    ) -> bool:
        return await self.repository.retry_or_fail(
            claim=claim,
            item=item,
            error_code=error_code,
            error_type=type(error).__name__,
            retryable=True,
            retry_at=now + self.failure_retry_delay,
            now=now,
        )


def _require_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
