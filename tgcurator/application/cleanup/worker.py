from __future__ import annotations

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

    def __post_init__(self) -> None:
        ensure_positive_duration(self.lease_duration, field="lease_duration")
        ensure_positive_duration(self.failure_retry_delay, field="failure_retry_delay")

    async def process(self, *, job_id: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        _require_uuid(job_id, field="job_id")
        claim = await self.repository.claim(
            job_id=job_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if claim is None:
            await self.repository.complete_wakeup_if_terminal(job_id=job_id, now=now)
            return False

        while True:
            item = await self.repository.claim_next_item(claim=claim, now=now)
            if item is None:
                return await self.repository.complete(claim=claim, now=now)
            try:
                await self._delete_blobs(item=item)
            except Exception as error:
                return await self._fail(
                    claim=claim,
                    item=item,
                    error_code="ARCHIVE_CLEANUP_DELETE_FAILED",
                    error=error,
                    now=now,
                )
            try:
                finalized = await self.repository.finalize_item_deleted(
                    claim=claim,
                    item=item,
                    now=now,
                )
            except Exception as error:
                return await self._fail(
                    claim=claim,
                    item=item,
                    error_code="ARCHIVE_CLEANUP_FINALIZE_FAILED",
                    error=error,
                    now=now,
                )
            if not finalized:
                return False

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
