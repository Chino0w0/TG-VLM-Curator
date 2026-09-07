from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apps.worker.runtime import WorkerRuntime
from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.domain.processing import RangeExecution, RangeExecutionStatus
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeDatabase:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class FakeRangeExecutionWorker:
    def __init__(self, claim: ClaimedRangeExecution | None) -> None:
        self.claim_result = claim
        self.requests: list[tuple[str, datetime]] = []

    async def claim(self, *, execution_id: str, now: datetime) -> ClaimedRangeExecution | None:
        self.requests.append((execution_id, now))
        return self.claim_result


class FakeRangeExecutionHistoryIngestion:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[ClaimedRangeExecution, datetime]] = []

    async def process(self, *, claim: ClaimedRangeExecution, now: datetime) -> bool:
        self.requests.append((claim, now))
        return self._result


class FakeImageArchiveWorker:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[str, datetime]] = []

    async def process(self, *, image_asset_id: str, now: datetime) -> bool:
        self.requests.append((image_asset_id, now))
        return self._result


class WorkerRuntimeTests(unittest.TestCase):
    def test_claims_one_normalized_execution_uuid_without_configured_history_processing(
        self,
    ) -> None:
        database = FakeDatabase()
        worker = FakeRangeExecutionWorker(claim=_claim())
        runtime = WorkerRuntime(database=database, range_execution_worker=worker)
        execution_id = str(uuid4()).upper()

        claimed = asyncio.run(runtime.handle_range_execution(execution_id=execution_id, now=NOW))
        asyncio.run(runtime.close())

        self.assertTrue(claimed)
        self.assertEqual(worker.requests, [(execution_id.lower(), NOW)])
        self.assertTrue(database.disposed)

    def test_processes_the_claim_with_an_injected_history_orchestrator(self) -> None:
        claim = _claim()
        worker = FakeRangeExecutionWorker(claim=claim)
        ingestion = FakeRangeExecutionHistoryIngestion(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=worker,
            range_execution_history_ingestion=ingestion,
        )

        processed = asyncio.run(
            runtime.handle_range_execution(execution_id=claim.execution.execution_id, now=NOW)
        )

        self.assertTrue(processed)
        self.assertEqual(worker.requests, [(claim.execution.execution_id, NOW)])
        self.assertEqual(ingestion.requests, [(claim, NOW)])

    def test_does_not_process_when_the_execution_cannot_be_claimed(self) -> None:
        worker = FakeRangeExecutionWorker(claim=None)
        ingestion = FakeRangeExecutionHistoryIngestion()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=worker,
            range_execution_history_ingestion=ingestion,
        )
        execution_id = str(uuid4())

        processed = asyncio.run(runtime.handle_range_execution(execution_id=execution_id, now=NOW))

        self.assertFalse(processed)
        self.assertEqual(worker.requests, [(execution_id, NOW)])
        self.assertEqual(ingestion.requests, [])

    def test_rejects_non_uuid_payload_before_worker_claim(self) -> None:
        worker = FakeRangeExecutionWorker(claim=None)
        runtime = WorkerRuntime(database=FakeDatabase(), range_execution_worker=worker)

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_range_execution(execution_id="not-a-uuid", now=NOW))

        self.assertEqual(worker.requests, [])

    def test_processes_normalized_image_uuid_with_an_injected_archive_worker(self) -> None:
        image_worker = FakeImageArchiveWorker(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            image_archive_worker=image_worker,  # type: ignore[arg-type]
        )
        image_asset_id = str(uuid4()).upper()

        processed = asyncio.run(
            runtime.handle_image_archive(image_asset_id=image_asset_id, now=NOW)
        )

        self.assertTrue(processed)
        self.assertEqual(image_worker.requests, [(image_asset_id.lower(), NOW)])

    def test_unconfigured_image_archive_runtime_never_fakes_completion(self) -> None:
        runtime = WorkerRuntime(
            database=FakeDatabase(), range_execution_worker=FakeRangeExecutionWorker(claim=None)
        )

        processed = asyncio.run(runtime.handle_image_archive(image_asset_id=str(uuid4()), now=NOW))

        self.assertFalse(processed)

    def test_rejects_non_uuid_image_archive_payload_before_worker_call(self) -> None:
        image_worker = FakeImageArchiveWorker()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            image_archive_worker=image_worker,  # type: ignore[arg-type]
        )

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_image_archive(image_asset_id="not-a-uuid", now=NOW))

        self.assertEqual(image_worker.requests, [])


def _claim() -> ClaimedRangeExecution:
    return ClaimedRangeExecution(
        execution=RangeExecution(
            execution_id=str(uuid4()),
            range_id=str(uuid4()),
            from_at=NOW - timedelta(minutes=15),
            to_at=NOW,
            status=RangeExecutionStatus.RUNNING,
        ),
        source_channel_id=str(uuid4()),
        source_profile_version_id=str(uuid4()),
        lease_token=str(uuid4()),
    )


if __name__ == "__main__":
    unittest.main()
