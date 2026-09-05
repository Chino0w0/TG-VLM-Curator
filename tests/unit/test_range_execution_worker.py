from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.application.processing import RangeExecutionWorker
from tgcurator.domain.processing import RangeExecution, RangeExecutionStatus
from tgcurator.infrastructure.database.range_execution_repository import (
    range_execution_claim_statement,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FROM_AT = NOW - timedelta(hours=1)
TO_AT = NOW


class FakeRangeExecutionWorkerRepository:
    def __init__(self, claim: ClaimedRangeExecution | None) -> None:
        self.claim = claim
        self.claim_requests: list[tuple[str, datetime, timedelta]] = []
        self.advance_requests: list[tuple[str, str, datetime, datetime]] = []
        self.complete_requests: list[tuple[str, str, datetime]] = []

    async def claim_execution(
        self,
        *,
        execution_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedRangeExecution | None:
        self.claim_requests.append((execution_id, now, lease_duration))
        return self.claim

    async def advance_watermark(
        self,
        *,
        execution_id: str,
        lease_token: str,
        watermark_at: datetime,
        now: datetime,
    ) -> bool:
        self.advance_requests.append((execution_id, lease_token, watermark_at, now))
        return True

    async def complete_execution(
        self,
        *,
        execution_id: str,
        lease_token: str,
        now: datetime,
    ) -> bool:
        self.complete_requests.append((execution_id, lease_token, now))
        return True


class RangeExecutionWorkerTests(unittest.TestCase):
    def test_claim_and_progress_calls_use_short_database_owned_lease(self) -> None:
        execution_id = str(uuid4())
        lease_token = str(uuid4())
        claimed = ClaimedRangeExecution(
            execution=RangeExecution(
                execution_id=execution_id,
                range_id=str(uuid4()),
                from_at=FROM_AT,
                to_at=TO_AT,
                status=RangeExecutionStatus.RUNNING,
            ),
            source_channel_id=str(uuid4()),
            source_profile_version_id=str(uuid4()),
            lease_token=lease_token,
        )
        repository = FakeRangeExecutionWorkerRepository(claimed)
        worker = RangeExecutionWorker(repository)

        actual_claim = asyncio.run(worker.claim(execution_id=execution_id, now=NOW))
        advanced = asyncio.run(
            worker.advance_watermark(
                execution_id=execution_id,
                lease_token=lease_token,
                watermark_at=TO_AT,
                now=NOW,
            )
        )
        completed = asyncio.run(
            worker.complete(execution_id=execution_id, lease_token=lease_token, now=NOW)
        )

        self.assertEqual(actual_claim, claimed)
        self.assertTrue(advanced)
        self.assertTrue(completed)
        self.assertEqual(
            repository.claim_requests,
            [(execution_id, NOW, timedelta(minutes=5))],
        )
        self.assertEqual(
            repository.advance_requests,
            [(execution_id, lease_token, TO_AT, NOW)],
        )
        self.assertEqual(repository.complete_requests, [(execution_id, lease_token, NOW)])

    def test_rejects_blank_identifiers_before_repository_access(self) -> None:
        repository = FakeRangeExecutionWorkerRepository(None)
        worker = RangeExecutionWorker(repository)

        with self.assertRaises(DomainValidationError):
            asyncio.run(worker.claim(execution_id=" ", now=NOW))
        with self.assertRaises(DomainValidationError):
            asyncio.run(
                worker.advance_watermark(
                    execution_id=" ",
                    lease_token=str(uuid4()),
                    watermark_at=NOW,
                    now=NOW,
                )
            )
        self.assertEqual(repository.claim_requests, [])
        self.assertEqual(repository.advance_requests, [])

    def test_postgresql_execution_claim_query_uses_skip_locked(self) -> None:
        statement = range_execution_claim_statement(execution_id=uuid4(), now=NOW)
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        self.assertIn("FROM range_executions", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)


if __name__ == "__main__":
    unittest.main()
