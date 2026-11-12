from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgcurator.application.cleanup import ArchiveCleanupWorker
from tgcurator.application.ports.cleanup import (
    ClaimedArchiveCleanupJob,
    ClaimedCleanupItem,
    CleanupAsset,
    CleanupBlob,
    MappingArchiveStorageResolver,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def cleanup_claim(*, attempt_number: int = 1, max_attempts: int = 3) -> ClaimedArchiveCleanupJob:
    return ClaimedArchiveCleanupJob(
        job_id=str(uuid4()),
        lease_token=str(uuid4()),
        attempt_number=attempt_number,
        max_attempts=max_attempts,
    )


def cleanup_item(
    *,
    blobs: tuple[CleanupBlob, ...] | None = None,
    asset_kind: str = "image",
) -> ClaimedCleanupItem:
    frozen_blobs = blobs or (
        CleanupBlob(storage_backend="archive", storage_key="a.webp", size_bytes=11),
    )
    return ClaimedCleanupItem(
        item_id=str(uuid4()),
        deletion_token=str(uuid4()),
        asset=CleanupAsset(
            asset_kind=asset_kind,
            asset_id=str(uuid4()),
            message_id=str(uuid4()),
            blobs=frozen_blobs,
            previous_archive_size=sum(blob.size_bytes for blob in frozen_blobs),
        ),
    )


class FakeRepository:
    def __init__(
        self,
        *,
        claim: ClaimedArchiveCleanupJob | None,
        items: tuple[ClaimedCleanupItem, ...] = (),
        finalize_results: tuple[bool | Exception, ...] = (),
        complete_result: bool = True,
        retry_result: bool = True,
    ) -> None:
        self.claim_result = claim
        self.items = list(items)
        self.finalize_results = list(finalize_results)
        self.complete_result = complete_result
        self.retry_result = retry_result
        self.events: list[tuple[object, ...]] = []

    async def claim(self, *, job_id: str, now: datetime, lease_duration: timedelta):
        self.events.append(("claim", job_id, now, lease_duration))
        return self.claim_result

    async def claim_next_item(self, *, claim: ClaimedArchiveCleanupJob, now: datetime):
        self.events.append(("claim_item", claim, now))
        return self.items.pop(0) if self.items else None

    async def finalize_item_deleted(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        item: ClaimedCleanupItem,
        now: datetime,
    ) -> bool:
        self.events.append(("finalize", claim, item, now))
        if not self.finalize_results:
            return True
        outcome = self.finalize_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def complete(self, *, claim: ClaimedArchiveCleanupJob, now: datetime) -> bool:
        self.events.append(("complete", claim, now))
        return self.complete_result

    async def retry_or_fail(self, **kwargs: object) -> bool:
        self.events.append(("retry", kwargs))
        return self.retry_result

    async def complete_wakeup_if_terminal(self, *, job_id: str, now: datetime) -> bool:
        self.events.append(("complete_wakeup", job_id, now))
        return True


class FakeStorage:
    def __init__(self, events: list[tuple[object, ...]], *, fail_key: str | None = None) -> None:
        self.events = events
        self.fail_key = fail_key

    async def delete(self, *, key: str) -> None:
        self.events.append(("delete", key))
        if key == self.fail_key:
            raise OSError("storage unavailable")


class CleanupWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_transaction_finishes_before_storage_delete_and_metadata_finalize(
        self,
    ) -> None:
        claim = cleanup_claim()
        item = cleanup_item(
            asset_kind="video",
            blobs=(
                CleanupBlob(storage_backend="archive", storage_key="first.webp", size_bytes=10),
                CleanupBlob(storage_backend="archive", storage_key="second.webp", size_bytes=20),
            ),
        )
        repository = FakeRepository(claim=claim, items=(item,))
        storage = FakeStorage(repository.events)
        worker = ArchiveCleanupWorker(
            repository=repository,  # type: ignore[arg-type]
            storage_resolver=MappingArchiveStorageResolver({"archive": storage}),
        )

        completed = await worker.process(job_id=claim.job_id, now=NOW)

        self.assertTrue(completed)
        event_names = [event[0] for event in repository.events]
        self.assertEqual(
            event_names,
            ["claim", "claim_item", "delete", "delete", "finalize", "claim_item", "complete"],
        )
        self.assertEqual(repository.events[2], ("delete", "first.webp"))
        self.assertEqual(repository.events[3], ("delete", "second.webp"))

    async def test_delete_error_schedules_bounded_retry_without_finalizing(self) -> None:
        claim = cleanup_claim(attempt_number=1, max_attempts=3)
        item = cleanup_item()
        repository = FakeRepository(claim=claim, items=(item,))
        storage = FakeStorage(repository.events, fail_key="a.webp")
        worker = ArchiveCleanupWorker(
            repository=repository,  # type: ignore[arg-type]
            storage_resolver=MappingArchiveStorageResolver({"archive": storage}),
            failure_retry_delay=timedelta(seconds=45),
        )

        processed = await worker.process(job_id=claim.job_id, now=NOW)

        self.assertTrue(processed)
        event_names = [event[0] for event in repository.events]
        self.assertEqual(event_names, ["claim", "claim_item", "delete", "retry"])
        retry = repository.events[-1][1]
        assert isinstance(retry, dict)
        self.assertEqual(retry["retry_at"], NOW + timedelta(seconds=45))
        self.assertEqual(retry["error_code"], "ARCHIVE_CLEANUP_DELETE_FAILED")
        self.assertEqual(retry["error_type"], "OSError")
        self.assertTrue(retry["retryable"])

    async def test_crash_after_remote_delete_is_replayed_idempotently(self) -> None:
        claim = cleanup_claim()
        item = cleanup_item()
        repository = FakeRepository(
            claim=claim,
            items=(item,),
            finalize_results=(False,),
        )
        storage = FakeStorage(repository.events)
        worker = ArchiveCleanupWorker(
            repository=repository,  # type: ignore[arg-type]
            storage_resolver=MappingArchiveStorageResolver({"archive": storage}),
        )

        first = await worker.process(job_id=claim.job_id, now=NOW)
        self.assertFalse(first)

        repository.items.append(item)
        repository.finalize_results.append(True)
        second = await worker.process(job_id=claim.job_id, now=NOW)

        self.assertTrue(second)
        self.assertEqual(
            [event for event in repository.events if event[0] == "delete"],
            [("delete", "a.webp"), ("delete", "a.webp")],
        )

    async def test_finalize_error_schedules_bounded_retry_for_idempotent_replay(self) -> None:
        claim = cleanup_claim(attempt_number=1, max_attempts=3)
        item = cleanup_item()
        repository = FakeRepository(
            claim=claim,
            items=(item,),
            finalize_results=(RuntimeError("db commit failed"),),
        )
        storage = FakeStorage(repository.events)
        worker = ArchiveCleanupWorker(
            repository=repository,  # type: ignore[arg-type]
            storage_resolver=MappingArchiveStorageResolver({"archive": storage}),
            failure_retry_delay=timedelta(seconds=30),
        )

        processed = await worker.process(job_id=claim.job_id, now=NOW)

        self.assertTrue(processed)
        self.assertEqual(
            [event[0] for event in repository.events],
            ["claim", "claim_item", "delete", "finalize", "retry"],
        )
        retry = repository.events[-1][1]
        assert isinstance(retry, dict)
        self.assertEqual(retry["retry_at"], NOW + timedelta(seconds=30))
        self.assertEqual(retry["error_code"], "ARCHIVE_CLEANUP_FINALIZE_FAILED")
        self.assertEqual(retry["error_type"], "RuntimeError")
        self.assertTrue(retry["retryable"])

    async def test_unclaimable_terminal_wakeup_is_completed_safely(self) -> None:
        job_id = str(uuid4())
        repository = FakeRepository(claim=None)
        worker = ArchiveCleanupWorker(
            repository=repository,  # type: ignore[arg-type]
            storage_resolver=MappingArchiveStorageResolver({}),
        )

        processed = await worker.process(job_id=job_id, now=NOW)

        self.assertFalse(processed)
        self.assertEqual([event[0] for event in repository.events], ["claim", "complete_wakeup"])

    async def test_invalid_job_id_is_rejected_before_repository_access(self) -> None:
        repository = FakeRepository(claim=None)
        worker = ArchiveCleanupWorker(
            repository=repository,  # type: ignore[arg-type]
            storage_resolver=MappingArchiveStorageResolver({}),
        )

        with self.assertRaises(DomainValidationError):
            await worker.process(job_id="not-a-uuid", now=NOW)

        self.assertEqual(repository.events, [])


if __name__ == "__main__":
    unittest.main()
