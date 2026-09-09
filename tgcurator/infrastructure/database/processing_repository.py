from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.processing import (
    ClaimedRangeExecution,
    ClaimedWakeup,
    PendingRangeExecution,
    ScheduledProcessingRange,
)
from tgcurator.infrastructure.database.models import (
    DurableWakeupRecord,
    ProcessingRangeRecord,
    RangeExecutionRecord,
    SourceChannel,
)
from tgcurator.infrastructure.database.session import AsyncDatabase

RANGE_EXECUTION_QUEUE = "range_execution"


def due_wakeup_claim_statement(*, now: datetime, limit: int):
    return (
        select(DurableWakeupRecord)
        .where(
            or_(
                and_(
                    DurableWakeupRecord.status == "pending",
                    DurableWakeupRecord.next_attempt_at <= now,
                ),
                and_(
                    DurableWakeupRecord.status == "leased",
                    DurableWakeupRecord.lease_expires_at <= now,
                ),
            )
        )
        .order_by(DurableWakeupRecord.next_attempt_at, DurableWakeupRecord.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def range_execution_claim_statement(*, execution_id: UUID):
    return (
        select(RangeExecutionRecord, ProcessingRangeRecord.source_channel_id)
        .join(
            ProcessingRangeRecord,
            ProcessingRangeRecord.id == RangeExecutionRecord.processing_range_id,
        )
        .where(RangeExecutionRecord.id == execution_id)
        .with_for_update(skip_locked=True)
    )


def processing_range_freeze_statement(*, processing_range_id: UUID):
    return (
        select(
            ProcessingRangeRecord,
            SourceChannel.active_profile_version_id,
            SourceChannel.enabled,
        )
        .join(SourceChannel, SourceChannel.id == ProcessingRangeRecord.source_channel_id)
        .where(ProcessingRangeRecord.id == processing_range_id)
        .with_for_update()
    )


class SqlAlchemyProcessingRangeScheduleRepository:
    """Persist immutable message-ID windows and wake-ups in one transaction."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
        async with self._database.session() as session:
            result = await session.execute(
                select(ProcessingRangeRecord, SourceChannel.active_profile_version_id)
                .join(SourceChannel, SourceChannel.id == ProcessingRangeRecord.source_channel_id)
                .where(
                    ProcessingRangeRecord.enabled.is_(True),
                    ProcessingRangeRecord.status.in_(("pending", "active")),
                    ProcessingRangeRecord.active_high_watermark_message_id.is_(None),
                    ProcessingRangeRecord.resolved_start_message_id.is_not(None),
                    SourceChannel.enabled.is_(True),
                    SourceChannel.active_profile_version_id.is_not(None),
                )
                .order_by(ProcessingRangeRecord.created_at, ProcessingRangeRecord.id)
            )
            scheduled: list[ScheduledProcessingRange] = []
            for row, profile_version_id in result:
                if row.end_mode == "fixed" and row.resolved_fixed_end_message_id is None:
                    continue
                scheduled.append(
                    ScheduledProcessingRange(
                        range_id=str(row.id),
                        source_channel_id=str(row.source_channel_id),
                        source_profile_version_id=str(profile_version_id),
                        end_mode=row.end_mode,
                        resolved_start_message_id=row.resolved_start_message_id,
                        resolved_fixed_end_message_id=row.resolved_fixed_end_message_id,
                        processing_watermark_message_id=row.processing_watermark_message_id,
                        steady_after_seconds=row.steady_after_seconds,
                    )
                )
            return tuple(scheduled)

    async def create_execution_and_wakeup(
        self,
        *,
        execution: PendingRangeExecution,
        queue: str,
        available_at: datetime,
    ) -> bool:
        execution_id = UUID(execution.execution_id)
        processing_range_id = UUID(execution.processing_range_id)
        source_profile_version_id = UUID(execution.source_profile_version_id)
        statement = (
            postgresql.insert(RangeExecutionRecord)
            .values(
                id=execution_id,
                processing_range_id=processing_range_id,
                source_profile_version_id=source_profile_version_id,
                from_message_id_exclusive=execution.from_message_id_exclusive,
                to_message_id_inclusive=execution.to_message_id_inclusive,
                watermark_message_id=execution.from_message_id_exclusive,
                status="pending",
                attempt_count=0,
                max_attempts=execution.max_attempts,
            )
            .on_conflict_do_nothing()
            .returning(RangeExecutionRecord.id)
        )
        async with self._database.session() as session:
            async with session.begin():
                freeze_result = await session.execute(
                    processing_range_freeze_statement(processing_range_id=processing_range_id)
                )
                locked = freeze_result.one_or_none()
                if locked is None:
                    return False
                processing_range, active_profile_version_id, source_channel_enabled = locked
                if not self._can_freeze(
                    processing_range=processing_range,
                    active_profile_version_id=active_profile_version_id,
                    source_channel_enabled=source_channel_enabled,
                    execution=execution,
                    source_profile_version_id=source_profile_version_id,
                ):
                    return False

                inserted_execution_id = await session.scalar(statement)
                if inserted_execution_id is None:
                    return False

                processing_range.active_high_watermark_message_id = (
                    execution.to_message_id_inclusive
                )
                processing_range.status = "active"
                processing_range.updated_at = available_at
                session.add(
                    DurableWakeupRecord(
                        id=uuid4(),
                        queue=queue,
                        entity_id=inserted_execution_id,
                        status="pending",
                        next_attempt_at=available_at,
                        dispatch_attempts=0,
                    )
                )
        return True

    @staticmethod
    def _can_freeze(
        *,
        processing_range: ProcessingRangeRecord,
        active_profile_version_id: UUID | None,
        source_channel_enabled: bool,
        execution: PendingRangeExecution,
        source_profile_version_id: UUID,
    ) -> bool:
        if (
            not source_channel_enabled
            or not processing_range.enabled
            or processing_range.status not in {"pending", "active"}
            or processing_range.active_high_watermark_message_id is not None
            or processing_range.resolved_start_message_id is None
            or active_profile_version_id != source_profile_version_id
        ):
            return False
        expected_from = (
            processing_range.processing_watermark_message_id
            if processing_range.processing_watermark_message_id is not None
            else processing_range.resolved_start_message_id - 1
        )
        if execution.from_message_id_exclusive != expected_from:
            return False
        if execution.to_message_id_inclusive <= expected_from:
            return False
        return not (
            processing_range.end_mode == "fixed"
            and execution.to_message_id_inclusive != processing_range.resolved_fixed_end_message_id
        )


class SqlAlchemyDurableWakeupRepository:
    """Lease reconstructable broker signals without holding a transaction during dispatch."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedWakeup, ...]:
        async with self._database.session() as session:
            async with session.begin():
                rows = tuple(
                    await session.scalars(due_wakeup_claim_statement(now=now, limit=limit))
                )
                claims: list[ClaimedWakeup] = []
                for row in rows:
                    token = uuid4()
                    row.status = "leased"
                    row.lease_token = token
                    row.lease_expires_at = now + lease_duration
                    row.dispatch_attempts += 1
                    row.updated_at = now
                    claims.append(
                        ClaimedWakeup(
                            wakeup_id=str(row.id),
                            queue=row.queue,
                            entity_id=str(row.entity_id),
                            lease_token=str(token),
                        )
                    )
                return tuple(claims)

    async def acknowledge_dispatch(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        now: datetime,
        repair_after: datetime,
    ) -> bool:
        return await self._release_claim(
            wakeup_id=wakeup_id,
            lease_token=lease_token,
            next_attempt_at=repair_after,
            now=now,
            last_dispatched_at=now,
        )

    async def reschedule_after_failure(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        now: datetime,
        retry_after: datetime,
    ) -> bool:
        return await self._release_claim(
            wakeup_id=wakeup_id,
            lease_token=lease_token,
            next_attempt_at=retry_after,
            now=now,
            last_dispatched_at=None,
        )

    async def _release_claim(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        next_attempt_at: datetime,
        now: datetime,
        last_dispatched_at: datetime | None,
    ) -> bool:
        values: dict[str, object] = {
            "status": "pending",
            "next_attempt_at": next_attempt_at,
            "lease_token": None,
            "lease_expires_at": None,
            "updated_at": now,
        }
        if last_dispatched_at is not None:
            values["last_dispatched_at"] = last_dispatched_at
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    update(DurableWakeupRecord)
                    .where(
                        DurableWakeupRecord.id == UUID(wakeup_id),
                        DurableWakeupRecord.status == "leased",
                        DurableWakeupRecord.lease_token == UUID(lease_token),
                    )
                    .values(**values)
                )
                return result.rowcount == 1

    async def complete_for_entity(self, *, queue: str, entity_id: str, now: datetime) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    update(DurableWakeupRecord)
                    .where(
                        DurableWakeupRecord.queue == queue,
                        DurableWakeupRecord.entity_id == UUID(entity_id),
                        DurableWakeupRecord.status.in_(("pending", "leased")),
                    )
                    .values(
                        status="completed",
                        lease_token=None,
                        lease_expires_at=None,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                return result.rowcount == 1


class SqlAlchemyRangeExecutionWorkerRepository:
    """Lease executions and persist progress with compare-by-lease semantics."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def claim_execution(
        self,
        *,
        execution_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedRangeExecution | None:
        entity_id = UUID(execution_id)
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    range_execution_claim_statement(execution_id=entity_id)
                )
                claimed_row = result.one_or_none()
                if claimed_row is None:
                    return None
                row, source_channel_id = claimed_row
                if row.status in {"completed", "failed"}:
                    return None
                eligible = (
                    row.status == "pending"
                    or (
                        row.status == "retry_wait"
                        and row.next_retry_at is not None
                        and row.next_retry_at <= now
                    )
                    or (
                        row.status == "running"
                        and row.lease_expires_at is not None
                        and row.lease_expires_at <= now
                    )
                )
                if not eligible:
                    return None
                if row.attempt_count >= row.max_attempts:
                    await self._terminalize_exhausted_lease(session=session, row=row, now=now)
                    return None

                lease_token = uuid4()
                row.status = "running"
                row.attempt_count += 1
                row.next_retry_at = None
                row.lease_token = lease_token
                row.lease_expires_at = now + lease_duration
                row.started_at = row.started_at or now
                row.updated_at = now
                return self._claimed(
                    row=row,
                    source_channel_id=source_channel_id,
                    lease_token=lease_token,
                )

    async def advance_watermark(
        self,
        *,
        execution_id: str,
        lease_token: str,
        watermark_message_id: int,
        now: datetime,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                row = await self._locked_active_row(
                    session=session,
                    execution_id=UUID(execution_id),
                    lease_token=UUID(lease_token),
                    now=now,
                )
                if row is None:
                    return False
                if (
                    not row.watermark_message_id
                    <= watermark_message_id
                    <= row.to_message_id_inclusive
                ):
                    return False
                row.watermark_message_id = watermark_message_id
                row.updated_at = now
                return True

    async def complete_execution(
        self,
        *,
        execution_id: str,
        lease_token: str,
        now: datetime,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                row = await self._locked_active_row(
                    session=session,
                    execution_id=UUID(execution_id),
                    lease_token=UUID(lease_token),
                    now=now,
                )
                if row is None or row.watermark_message_id != row.to_message_id_inclusive:
                    return False
                processing_range = await session.scalar(
                    select(ProcessingRangeRecord)
                    .where(ProcessingRangeRecord.id == row.processing_range_id)
                    .with_for_update()
                )
                if processing_range is None:
                    return False
                expected_watermark = (
                    processing_range.processing_watermark_message_id
                    if processing_range.processing_watermark_message_id is not None
                    else processing_range.resolved_start_message_id - 1
                )
                if (
                    expected_watermark != row.from_message_id_exclusive
                    or processing_range.active_high_watermark_message_id
                    != row.to_message_id_inclusive
                ):
                    return False

                row.status = "completed"
                row.lease_token = None
                row.lease_expires_at = None
                row.completed_at = now
                row.updated_at = now
                processing_range.processing_watermark_message_id = row.to_message_id_inclusive
                processing_range.active_high_watermark_message_id = None
                processing_range.status = (
                    "completed"
                    if processing_range.end_mode == "fixed"
                    and row.to_message_id_inclusive
                    == processing_range.resolved_fixed_end_message_id
                    else "active"
                )
                processing_range.updated_at = now
                await self._set_wakeup_terminal(
                    session=session,
                    entity_id=row.id,
                    now=now,
                )
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
        async with self._database.session() as session:
            async with session.begin():
                row = await self._locked_active_row(
                    session=session,
                    execution_id=UUID(execution_id),
                    lease_token=UUID(lease_token),
                    now=now,
                )
                if row is None:
                    return False
                row.last_error_code = error_code
                row.last_error_type = error_type
                row.last_failure_at = now
                row.lease_token = None
                row.lease_expires_at = None
                row.updated_at = now

                if retryable and row.attempt_count < row.max_attempts:
                    row.status = "retry_wait"
                    row.next_retry_at = retry_at
                    await self._reschedule_wakeup(
                        session=session,
                        entity_id=row.id,
                        retry_at=retry_at,
                        now=now,
                    )
                else:
                    row.status = "failed"
                    row.next_retry_at = None
                    processing_range = await session.scalar(
                        select(ProcessingRangeRecord)
                        .where(ProcessingRangeRecord.id == row.processing_range_id)
                        .with_for_update()
                    )
                    if processing_range is not None:
                        processing_range.status = "failed"
                        processing_range.updated_at = now
                    await self._set_wakeup_terminal(
                        session=session,
                        entity_id=row.id,
                        now=now,
                    )
                return True

    @staticmethod
    async def _locked_active_row(*, session, execution_id: UUID, lease_token: UUID, now: datetime):
        return await session.scalar(
            select(RangeExecutionRecord)
            .where(
                RangeExecutionRecord.id == execution_id,
                RangeExecutionRecord.status == "running",
                RangeExecutionRecord.lease_token == lease_token,
                RangeExecutionRecord.lease_expires_at > now,
            )
            .with_for_update()
        )

    @staticmethod
    async def _reschedule_wakeup(*, session, entity_id: UUID, retry_at: datetime, now: datetime):
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == RANGE_EXECUTION_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
                DurableWakeupRecord.status != "cancelled",
            )
            .values(
                status="pending",
                next_attempt_at=retry_at,
                lease_token=None,
                lease_expires_at=None,
                completed_at=None,
                updated_at=now,
            )
        )

    @staticmethod
    async def _set_wakeup_terminal(*, session, entity_id: UUID, now: datetime):
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == RANGE_EXECUTION_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
                DurableWakeupRecord.status != "cancelled",
            )
            .values(
                status="completed",
                lease_token=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )

    async def _terminalize_exhausted_lease(self, *, session, row, now: datetime) -> None:
        row.status = "failed"
        row.next_retry_at = None
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error_code = "LEASE_EXPIRED"
        row.last_error_type = "WorkerLeaseExpired"
        row.last_failure_at = now
        row.updated_at = now
        processing_range = await session.scalar(
            select(ProcessingRangeRecord)
            .where(ProcessingRangeRecord.id == row.processing_range_id)
            .with_for_update()
        )
        if processing_range is not None:
            processing_range.status = "failed"
            processing_range.updated_at = now
        await self._set_wakeup_terminal(session=session, entity_id=row.id, now=now)

    @staticmethod
    def _claimed(*, row, source_channel_id: UUID, lease_token: UUID) -> ClaimedRangeExecution:
        return ClaimedRangeExecution(
            execution_id=str(row.id),
            processing_range_id=str(row.processing_range_id),
            source_channel_id=str(source_channel_id),
            source_profile_version_id=str(row.source_profile_version_id),
            from_message_id_exclusive=row.from_message_id_exclusive,
            to_message_id_inclusive=row.to_message_id_inclusive,
            watermark_message_id=row.watermark_message_id,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            lease_token=str(lease_token),
        )
