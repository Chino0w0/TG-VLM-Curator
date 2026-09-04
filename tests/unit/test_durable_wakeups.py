from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.processing import ClaimedWakeup
from tgcurator.application.processing import DurableWakeupDispatcher
from tgcurator.infrastructure.database.processing_repository import due_wakeup_claim_statement
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeWakeupRepository:
    def __init__(self, claims: tuple[ClaimedWakeup, ...]) -> None:
        self.claims = claims
        self.claim_requests: list[tuple[datetime, timedelta, int]] = []
        self.acknowledgements: list[tuple[str, str, datetime, datetime]] = []
        self.failures: list[tuple[str, str, datetime]] = []

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedWakeup, ...]:
        self.claim_requests.append((now, lease_duration, limit))
        return self.claims

    async def acknowledge_dispatch(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        now: datetime,
        repair_after: datetime,
    ) -> bool:
        self.acknowledgements.append((wakeup_id, lease_token, now, repair_after))
        return True

    async def reschedule_after_failure(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        retry_after: datetime,
    ) -> bool:
        self.failures.append((wakeup_id, lease_token, retry_after))
        return True

    async def complete_for_entity(self, *, queue: str, entity_id: str, now: datetime) -> bool:
        return True


class ControlledDispatcher:
    def __init__(self, failing_entity_ids: set[str] | None = None) -> None:
        self.failing_entity_ids = failing_entity_ids or set()
        self.calls: list[tuple[str, str]] = []

    async def dispatch(self, *, queue: str, entity_id: str) -> None:
        self.calls.append((queue, entity_id))
        if entity_id in self.failing_entity_ids:
            raise RuntimeError("broker unavailable")


class DurableWakeupDispatcherTests(unittest.TestCase):
    def test_dispatches_after_claim_then_keeps_successful_wakeup_repairable(self) -> None:
        entity_id = str(uuid4())
        claim = ClaimedWakeup(str(uuid4()), "range_execution", entity_id, str(uuid4()))
        repository = FakeWakeupRepository((claim,))
        dispatcher = ControlledDispatcher()

        report = asyncio.run(DurableWakeupDispatcher(repository, dispatcher).dispatch_due(now=NOW))

        self.assertEqual(report.claimed_wakeups, 1)
        self.assertEqual(report.dispatched_wakeups, 1)
        self.assertEqual(report.failed_dispatches, 0)
        self.assertEqual(dispatcher.calls, [("range_execution", entity_id)])
        self.assertEqual(repository.claim_requests, [(NOW, timedelta(minutes=1), 100)])
        self.assertEqual(
            repository.acknowledgements,
            [(claim.wakeup_id, claim.lease_token, NOW, NOW + timedelta(minutes=5))],
        )
        self.assertEqual(repository.failures, [])

    def test_failed_dispatch_releases_lease_for_retry_without_propagating_exception(self) -> None:
        entity_id = str(uuid4())
        claim = ClaimedWakeup(str(uuid4()), "range_execution", entity_id, str(uuid4()))
        repository = FakeWakeupRepository((claim,))
        dispatcher = ControlledDispatcher({entity_id})

        report = asyncio.run(DurableWakeupDispatcher(repository, dispatcher).dispatch_due(now=NOW))

        self.assertEqual(report.dispatched_wakeups, 0)
        self.assertEqual(report.failed_dispatches, 1)
        self.assertEqual(repository.acknowledgements, [])
        self.assertEqual(
            repository.failures,
            [(claim.wakeup_id, claim.lease_token, NOW + timedelta(seconds=30))],
        )

    def test_rejects_an_invalid_dispatch_limit(self) -> None:
        repository = FakeWakeupRepository(())
        with self.assertRaises(DomainValidationError):
            asyncio.run(
                DurableWakeupDispatcher(repository, ControlledDispatcher()).dispatch_due(
                    now=NOW, limit=0
                )
            )

    def test_postgresql_claim_query_uses_skip_locked(self) -> None:
        statement = due_wakeup_claim_statement(now=NOW, limit=10)
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("durable_wakeups", sql)


if __name__ == "__main__":
    unittest.main()
