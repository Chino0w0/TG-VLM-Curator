from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.processing import PendingRangeExecution
from tgcurator.infrastructure.database.models import (
    DurableWakeupRecord,
    ProcessingRangeRecord,
    RangeExecutionRecord,
)
from tgcurator.infrastructure.database.processing_repository import (
    SqlAlchemyDurableWakeupRepository,
    SqlAlchemyProcessingRangeScheduleRepository,
    SqlAlchemyRangeExecutionWorkerRepository,
    processing_range_freeze_statement,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
PROFILE_ID = uuid4()


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeResult:
    def __init__(self, value: object | None = None, *, rowcount: int = 1) -> None:
        self.value = value
        self.rowcount = rowcount

    def one_or_none(self) -> object | None:
        return self.value


class FakeSession:
    def __init__(
        self,
        *,
        scalar_results: list[object | None] | None = None,
        execute_results: list[FakeResult] | None = None,
        scalar_batches: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.scalar_results = list(scalar_results or [])
        self.execute_results = list(execute_results or [])
        self.scalar_batches = list(scalar_batches or [])
        self.scalar_statements: list[object] = []
        self.execute_statements: list[object] = []
        self.scalars_statements: list[object] = []
        self.added: list[object] = []

    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    async def scalar(self, statement: object) -> object | None:
        self.scalar_statements.append(statement)
        if not self.scalar_results:
            raise AssertionError("unexpected scalar call")
        return self.scalar_results.pop(0)

    async def execute(self, statement: object) -> FakeResult:
        self.execute_statements.append(statement)
        if self.execute_results:
            return self.execute_results.pop(0)
        return FakeResult()

    async def scalars(self, statement: object) -> tuple[object, ...]:
        self.scalars_statements.append(statement)
        if not self.scalar_batches:
            raise AssertionError("unexpected scalars call")
        return self.scalar_batches.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)


class FakeDatabase:
    def __init__(self, session: FakeSession) -> None:
        self.fake_session = session

    @asynccontextmanager
    async def session(self):
        yield self.fake_session


def processing_range(
    *,
    status: str = "pending",
    end_mode: str = "latest",
    resolved_start: int = 100,
    fixed_end: int | None = None,
    watermark: int | None = None,
    active_high: int | None = None,
    enabled: bool = True,
) -> ProcessingRangeRecord:
    return ProcessingRangeRecord(
        id=uuid4(),
        source_channel_id=uuid4(),
        start_at=NOW - timedelta(days=1),
        end_mode=end_mode,
        end_at=NOW if end_mode == "fixed" else None,
        resolved_start_message_id=resolved_start,
        resolved_fixed_end_message_id=fixed_end,
        processing_watermark_message_id=watermark,
        active_high_watermark_message_id=active_high,
        steady_after_seconds=300 if end_mode == "latest" else None,
        status=status,
        enabled=enabled,
    )


def execution_row(
    *,
    processing_range_id: UUID,
    status: str = "pending",
    from_message_id: int = 99,
    to_message_id: int = 125,
    watermark: int = 99,
    attempt_count: int = 0,
    max_attempts: int = 5,
    lease_token: UUID | None = None,
    lease_expires_at: datetime | None = None,
) -> RangeExecutionRecord:
    return RangeExecutionRecord(
        id=uuid4(),
        processing_range_id=processing_range_id,
        source_profile_version_id=PROFILE_ID,
        from_message_id_exclusive=from_message_id,
        to_message_id_inclusive=to_message_id,
        watermark_message_id=watermark,
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
    )


def pending_execution(item: ProcessingRangeRecord, *, to_message_id: int = 125):
    from_message_id = (
        item.processing_watermark_message_id
        if item.processing_watermark_message_id is not None
        else item.resolved_start_message_id - 1
    )
    return PendingRangeExecution(
        execution_id=str(uuid4()),
        processing_range_id=str(item.id),
        source_profile_version_id=str(PROFILE_ID),
        from_message_id_exclusive=from_message_id,
        to_message_id_inclusive=to_message_id,
    )


def statement_params(statement: object) -> dict[str, object]:
    return statement.compile(dialect=postgresql.dialect()).params


class ProcessingRangeScheduleRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_freezes_range_and_adds_wakeup_in_the_same_transaction(self) -> None:
        item = processing_range()
        request = pending_execution(item)
        inserted_id = UUID(request.execution_id)
        session = FakeSession(
            execute_results=[FakeResult((item, PROFILE_ID, True))],
            scalar_results=[inserted_id],
        )
        repository = SqlAlchemyProcessingRangeScheduleRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        created = await repository.create_execution_and_wakeup(
            execution=request,
            queue="range_execution",
            available_at=NOW,
        )

        self.assertTrue(created)
        self.assertEqual(item.status, "active")
        self.assertEqual(item.active_high_watermark_message_id, 125)
        self.assertEqual(item.updated_at, NOW)
        self.assertEqual(len(session.added), 1)
        wakeup = session.added[0]
        self.assertIsInstance(wakeup, DurableWakeupRecord)
        self.assertEqual(wakeup.entity_id, inserted_id)
        self.assertEqual(wakeup.queue, "range_execution")
        self.assertEqual(wakeup.next_attempt_at, NOW)

    async def test_stale_range_snapshot_is_rejected_before_execution_insert(self) -> None:
        item = processing_range(active_high=130)
        request = pending_execution(item)
        session = FakeSession(execute_results=[FakeResult((item, PROFILE_ID, True))])
        repository = SqlAlchemyProcessingRangeScheduleRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        created = await repository.create_execution_and_wakeup(
            execution=request,
            queue="range_execution",
            available_at=NOW,
        )

        self.assertFalse(created)
        self.assertEqual(session.scalar_statements, [])
        self.assertEqual(session.added, [])

    async def test_disabled_source_is_rejected_under_the_freeze_lock(self) -> None:
        item = processing_range()
        request = pending_execution(item)
        session = FakeSession(execute_results=[FakeResult((item, PROFILE_ID, False))])
        repository = SqlAlchemyProcessingRangeScheduleRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        created = await repository.create_execution_and_wakeup(
            execution=request,
            queue="range_execution",
            available_at=NOW,
        )

        self.assertFalse(created)
        self.assertEqual(session.scalar_statements, [])
        self.assertEqual(session.added, [])

    async def test_insert_conflict_does_not_activate_range_or_add_wakeup(self) -> None:
        item = processing_range()
        request = pending_execution(item)
        session = FakeSession(
            execute_results=[FakeResult((item, PROFILE_ID, True))],
            scalar_results=[None],
        )
        repository = SqlAlchemyProcessingRangeScheduleRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        created = await repository.create_execution_and_wakeup(
            execution=request,
            queue="range_execution",
            available_at=NOW,
        )

        self.assertFalse(created)
        self.assertEqual(item.status, "pending")
        self.assertIsNone(item.active_high_watermark_message_id)
        self.assertEqual(session.added, [])

    def test_freeze_query_locks_range_and_profile_snapshot(self) -> None:
        sql = str(
            processing_range_freeze_statement(processing_range_id=uuid4()).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("JOIN source_channels", sql)
        self.assertIn("FOR UPDATE", sql)


class DurableWakeupRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_due_leases_each_signal_and_increments_attempts(self) -> None:
        wakeup = DurableWakeupRecord(
            id=uuid4(),
            queue="range_execution",
            entity_id=uuid4(),
            status="pending",
            next_attempt_at=NOW,
            dispatch_attempts=2,
        )
        session = FakeSession(scalar_batches=[(wakeup,)])
        repository = SqlAlchemyDurableWakeupRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        claims = await repository.claim_due(
            now=NOW,
            lease_duration=timedelta(minutes=1),
            limit=10,
        )

        self.assertEqual(len(claims), 1)
        self.assertEqual(wakeup.status, "leased")
        self.assertEqual(wakeup.dispatch_attempts, 3)
        self.assertEqual(wakeup.lease_expires_at, NOW + timedelta(minutes=1))
        self.assertEqual(claims[0].lease_token, str(wakeup.lease_token))

    async def test_failed_dispatch_uses_observation_time_for_updated_at(self) -> None:
        session = FakeSession(execute_results=[FakeResult(rowcount=1)])
        repository = SqlAlchemyDurableWakeupRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )
        retry_after = NOW + timedelta(seconds=30)

        updated = await repository.reschedule_after_failure(
            wakeup_id=str(uuid4()),
            lease_token=str(uuid4()),
            now=NOW,
            retry_after=retry_after,
        )

        self.assertTrue(updated)
        params = statement_params(session.execute_statements[0])
        self.assertEqual(params["updated_at"], NOW)
        self.assertEqual(params["next_attempt_at"], retry_after)
        self.assertEqual(params["status"], "pending")


class RangeExecutionWorkerRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_pending_execution_creates_short_lived_lease(self) -> None:
        item = processing_range(status="active", active_high=125)
        row = execution_row(processing_range_id=item.id)
        session = FakeSession(execute_results=[FakeResult((row, item.source_channel_id))])
        repository = SqlAlchemyRangeExecutionWorkerRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        claim = await repository.claim_execution(
            execution_id=str(row.id),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(row.status, "running")
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(row.lease_expires_at, NOW + timedelta(minutes=5))
        self.assertEqual(claim.lease_token, str(row.lease_token))

    async def test_watermark_must_be_monotonic_and_inside_frozen_window(self) -> None:
        item = processing_range(status="active", active_high=125)
        token = uuid4()
        row = execution_row(
            processing_range_id=item.id,
            status="running",
            watermark=110,
            attempt_count=1,
            lease_token=token,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        session = FakeSession(scalar_results=[row, row, row])
        repository = SqlAlchemyRangeExecutionWorkerRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        self.assertFalse(
            await repository.advance_watermark(
                execution_id=str(row.id),
                lease_token=str(token),
                watermark_message_id=109,
                now=NOW,
            )
        )
        self.assertFalse(
            await repository.advance_watermark(
                execution_id=str(row.id),
                lease_token=str(token),
                watermark_message_id=126,
                now=NOW,
            )
        )
        self.assertTrue(
            await repository.advance_watermark(
                execution_id=str(row.id),
                lease_token=str(token),
                watermark_message_id=120,
                now=NOW,
            )
        )
        self.assertEqual(row.watermark_message_id, 120)

    async def test_completion_advances_range_watermark_and_finishes_fixed_range(self) -> None:
        item = processing_range(
            status="active",
            end_mode="fixed",
            fixed_end=125,
            active_high=125,
        )
        token = uuid4()
        row = execution_row(
            processing_range_id=item.id,
            status="running",
            watermark=125,
            attempt_count=1,
            lease_token=token,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        session = FakeSession(scalar_results=[row, item])
        repository = SqlAlchemyRangeExecutionWorkerRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        completed = await repository.complete_execution(
            execution_id=str(row.id),
            lease_token=str(token),
            now=NOW,
        )

        self.assertTrue(completed)
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.completed_at, NOW)
        self.assertIsNone(row.lease_token)
        self.assertEqual(item.processing_watermark_message_id, 125)
        self.assertIsNone(item.active_high_watermark_message_id)
        self.assertEqual(item.status, "completed")
        params = statement_params(session.execute_statements[0])
        self.assertEqual(params["status"], "completed")

    async def test_completion_rejects_a_stale_active_high_watermark(self) -> None:
        item = processing_range(status="active", active_high=130)
        token = uuid4()
        row = execution_row(
            processing_range_id=item.id,
            status="running",
            watermark=125,
            attempt_count=1,
            lease_token=token,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        session = FakeSession(scalar_results=[row, item])
        repository = SqlAlchemyRangeExecutionWorkerRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        completed = await repository.complete_execution(
            execution_id=str(row.id),
            lease_token=str(token),
            now=NOW,
        )

        self.assertFalse(completed)
        self.assertEqual(row.status, "running")
        self.assertIsNone(item.processing_watermark_message_id)
        self.assertEqual(item.active_high_watermark_message_id, 130)
        self.assertEqual(session.execute_statements, [])

    async def test_retryable_failure_releases_lease_and_reschedules_wakeup(self) -> None:
        item = processing_range(status="active", active_high=125)
        token = uuid4()
        row = execution_row(
            processing_range_id=item.id,
            status="running",
            attempt_count=1,
            lease_token=token,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        session = FakeSession(scalar_results=[row])
        repository = SqlAlchemyRangeExecutionWorkerRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )
        retry_at = NOW + timedelta(seconds=30)

        failed = await repository.fail_execution(
            execution_id=str(row.id),
            lease_token=str(token),
            error_code="DOWNLOAD_FAILED",
            error_type="TransientDownloadError",
            retryable=True,
            retry_at=retry_at,
            now=NOW,
        )

        self.assertTrue(failed)
        self.assertEqual(row.status, "retry_wait")
        self.assertEqual(row.next_retry_at, retry_at)
        self.assertIsNone(row.lease_token)
        self.assertEqual(row.last_error_code, "DOWNLOAD_FAILED")
        params = statement_params(session.execute_statements[0])
        self.assertEqual(params["next_attempt_at"], retry_at)
        self.assertEqual(params["status"], "pending")

    async def test_nonretryable_failure_marks_execution_and_range_terminal(self) -> None:
        item = processing_range(status="active", active_high=125)
        token = uuid4()
        row = execution_row(
            processing_range_id=item.id,
            status="running",
            attempt_count=1,
            lease_token=token,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        session = FakeSession(scalar_results=[row, item])
        repository = SqlAlchemyRangeExecutionWorkerRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        failed = await repository.fail_execution(
            execution_id=str(row.id),
            lease_token=str(token),
            error_code="UNSUPPORTED",
            error_type="PermanentProcessingError",
            retryable=False,
            retry_at=NOW + timedelta(seconds=30),
            now=NOW,
        )

        self.assertTrue(failed)
        self.assertEqual(row.status, "failed")
        self.assertEqual(item.status, "failed")
        self.assertEqual(row.last_error_type, "PermanentProcessingError")
        params = statement_params(session.execute_statements[0])
        self.assertEqual(params["status"], "completed")

    async def test_expired_final_lease_is_terminalized_without_another_attempt(self) -> None:
        item = processing_range(status="active", active_high=125)
        row = execution_row(
            processing_range_id=item.id,
            status="running",
            attempt_count=5,
            max_attempts=5,
            lease_token=uuid4(),
            lease_expires_at=NOW - timedelta(seconds=1),
        )
        session = FakeSession(
            scalar_results=[item],
            execute_results=[FakeResult((row, item.source_channel_id))],
        )
        repository = SqlAlchemyRangeExecutionWorkerRepository(
            FakeDatabase(session)  # type: ignore[arg-type]
        )

        claim = await repository.claim_execution(
            execution_id=str(row.id),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        self.assertIsNone(claim)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.last_error_code, "LEASE_EXPIRED")
        self.assertEqual(row.last_error_type, "WorkerLeaseExpired")
        self.assertEqual(item.status, "failed")


if __name__ == "__main__":
    unittest.main()
