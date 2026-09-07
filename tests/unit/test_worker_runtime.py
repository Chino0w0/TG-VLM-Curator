from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from apps.worker.runtime import WorkerRuntime
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeDatabase:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class FakeRangeExecutionWorker:
    def __init__(self, claim: object | None) -> None:
        self.claim_result = claim
        self.requests: list[tuple[str, datetime]] = []

    async def claim(self, *, execution_id: str, now: datetime) -> object | None:
        self.requests.append((execution_id, now))
        return self.claim_result


class WorkerRuntimeTests(unittest.TestCase):
    def test_claims_one_normalized_execution_uuid_without_processing_it(self) -> None:
        database = FakeDatabase()
        worker = FakeRangeExecutionWorker(claim=object())
        runtime = WorkerRuntime(database=database, range_execution_worker=worker)
        execution_id = str(uuid4()).upper()

        claimed = asyncio.run(runtime.handle_range_execution(execution_id=execution_id, now=NOW))
        asyncio.run(runtime.close())

        self.assertTrue(claimed)
        self.assertEqual(worker.requests, [(execution_id.lower(), NOW)])
        self.assertTrue(database.disposed)

    def test_rejects_non_uuid_payload_before_worker_claim(self) -> None:
        worker = FakeRangeExecutionWorker(claim=None)
        runtime = WorkerRuntime(database=FakeDatabase(), range_execution_worker=worker)

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_range_execution(execution_id="not-a-uuid", now=NOW))

        self.assertEqual(worker.requests, [])


if __name__ == "__main__":
    unittest.main()
