from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.application.processing import RangeExecutionWorker
from tgcurator.infrastructure.database import range_execution_claim_statement
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class FakeRangeExecutionWorkerRepository:
    def __init__(self, claim: ClaimedRangeExecution | None) -> None:
        self.claim_result = claim
        self.claim_requests: list[tuple[str, datetime, timedelta]] = []
        self.advance_requests: list[tuple[str, str, int, datetime]] = []
        self.complete_requests: list[tuple[str, str, datetime]] = []
        self.fail_requests: list[tuple[object, ...]] = []

    async def claim_execution(
        self, *, execution_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedRangeExecution | None:
        self.claim_requests.append((execution_id, now, lease_duration))
        return self.claim_result

    async def advance_watermark(
        self, *, execution_id: str, lease_token: str, watermark_message_id: int, now: datetime
    ) -> bool:
        self.advance_requests.append((execution_id, lease_token, watermark_message_id, now))
        return True

    async def complete_execution(
        self, *, execution_id: str, lease_token: str, now: datetime
    ) -> bool:
        self.complete_requests.append((execution_id, lease_token, now))
        return True

    async def fail_execution(
        self,
        *,
        execution_id: str,
        lease_token: str,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        self.fail_requests.append(
            (
                execution_id,
                lease_token,
                error_code,
                error_type,
                retryable,
                retry_at,
                now,
            )
        )
        return True


class RangeExecutionWorkerTests(unittest.TestCase):
    def test_claim_progress_complete_and_retry_are_repository_owned_transitions(self) -> None:
        execution_id = str(uuid4())
        lease_token = str(uuid4())
        claim = ClaimedRangeExecution(
            execution_id=execution_id,
            processing_range_id=str(uuid4()),
            source_profile_version_id=str(uuid4()),
            from_message_id_exclusive=99,
            to_message_id_inclusive=125,
            watermark_message_id=99,
            attempt_count=1,
            max_attempts=5,
            lease_token=lease_token,
        )
        repository = FakeRangeExecutionWorkerRepository(claim)
        worker = RangeExecutionWorker(repository)

        self.assertEqual(asyncio.run(worker.claim(execution_id=execution_id, now=NOW)), claim)
        self.assertTrue(
            asyncio.run(
                worker.advance_watermark(
                    execution_id=execution_id,
                    lease_token=lease_token,
                    watermark_message_id=110,
                    now=NOW,
                )
            )
        )
        self.assertTrue(
            asyncio.run(
                worker.complete(execution_id=execution_id, lease_token=lease_token, now=NOW)
            )
        )
        self.assertTrue(
            asyncio.run(
                worker.fail(
                    execution_id=execution_id,
                    lease_token=lease_token,
                    error_code="MEDIA_FAILED",
                    error_type="TransientDownloadError",
                    retryable=True,
                    retry_delay=timedelta(seconds=30),
                    now=NOW,
                )
            )
        )

        self.assertEqual(repository.claim_requests, [(execution_id, NOW, timedelta(minutes=5))])
        self.assertEqual(repository.advance_requests, [(execution_id, lease_token, 110, NOW)])
        self.assertEqual(repository.complete_requests, [(execution_id, lease_token, NOW)])
        self.assertEqual(
            repository.fail_requests[0],
            (
                execution_id,
                lease_token,
                "MEDIA_FAILED",
                "TransientDownloadError",
                True,
                NOW + timedelta(seconds=30),
                NOW,
            ),
        )

    def test_rejects_invalid_progress_and_failure_metadata_before_repository_access(self) -> None:
        repository = FakeRangeExecutionWorkerRepository(None)
        worker = RangeExecutionWorker(repository)
        with self.assertRaises(DomainValidationError):
            asyncio.run(worker.claim(execution_id=" ", now=NOW))
        with self.assertRaises(DomainValidationError):
            asyncio.run(
                worker.advance_watermark(
                    execution_id=str(uuid4()),
                    lease_token=str(uuid4()),
                    watermark_message_id=True,
                    now=NOW,
                )
            )
        with self.assertRaises(DomainValidationError):
            asyncio.run(
                worker.fail(
                    execution_id=str(uuid4()),
                    lease_token=str(uuid4()),
                    error_code=" ",
                    error_type="Error",
                    retryable=True,
                    retry_delay=timedelta(seconds=1),
                    now=NOW,
                )
            )
        self.assertEqual(repository.claim_requests, [])
        self.assertEqual(repository.advance_requests, [])
        self.assertEqual(repository.fail_requests, [])

    def test_postgresql_execution_claim_uses_skip_locked(self) -> None:
        sql = str(
            range_execution_claim_statement(execution_id=uuid4()).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        self.assertIn("FROM range_executions", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)


if __name__ == "__main__":
    unittest.main()
