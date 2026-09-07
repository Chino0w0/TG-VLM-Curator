from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.processing import (
    ClaimedWakeup,
    ScheduledProcessingRange,
)
from tgcurator.domain.processing import (
    BoundaryKind,
    RangeExecution,
)
from tgcurator.domain.processing import (
    ProcessingRange as DomainProcessingRange,
)
from tgcurator.infrastructure.database.models import (
    DurableWakeup,
    ProcessingRange,
    RangeExecutionRecord,
)
from tgcurator.infrastructure.database.session import AsyncDatabase


def due_wakeup_claim_statement(*, now: datetime, limit: int):
    """Build the PostgreSQL non-blocking scheduler claim query."""

    return (
        select(DurableWakeup)
        .where(
            or_(
                and_(
                    DurableWakeup.status == "pending",
                    DurableWakeup.next_attempt_at <= now,
                ),
                and_(
                    DurableWakeup.status == "leased",
                    DurableWakeup.lease_expires_at <= now,
                ),
            )
        )
        .order_by(DurableWakeup.next_attempt_at, DurableWakeup.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


class SqlAlchemyProcessingRangeScheduleRepository:
    """PostgreSQL persistence for finite range windows and their durable wake-ups."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
        async with self._database.session() as session:
            result = await session.scalars(
                select(ProcessingRange)
                .where(ProcessingRange.enabled.is_(True))
                .order_by(ProcessingRange.created_at, ProcessingRange.id)
            )
            return tuple(self._scheduled_range(row) for row in result)

    async def create_execution_and_wakeup(
        self,
        *,
        execution: RangeExecution,
        source_profile_version_id: str,
        queue: str,
        available_at: datetime,
    ) -> bool:
        execution_id = UUID(execution.execution_id)
        statement = (
            postgresql.insert(RangeExecutionRecord)
            .values(
                id=execution_id,
                processing_range_id=UUID(execution.range_id),
                source_profile_version_id=UUID(source_profile_version_id),
                from_at=execution.from_at,
                to_at=execution.to_at,
                watermark_at=execution.watermark_at,
                status=execution.status.value,
            )
            .on_conflict_do_nothing(constraint="uq_range_execution_bounds")
            .returning(RangeExecutionRecord.id)
        )
        async with self._database.session() as session:
            async with session.begin():
                inserted_execution_id = await session.scalar(statement)
                if inserted_execution_id is None:
                    return False
                wakeup_statement = (
                    postgresql.insert(DurableWakeup)
                    .values(
                        id=uuid4(),
                        queue=queue,
                        entity_id=inserted_execution_id,
                        status="pending",
                        next_attempt_at=available_at,
                        dispatch_attempts=0,
                    )
                    .on_conflict_do_nothing(constraint="uq_durable_wakeup_queue_entity")
                )
                await session.execute(wakeup_statement)
        return True

    @staticmethod
    def _scheduled_range(row: ProcessingRange) -> ScheduledProcessingRange:
        latest_quiet_period = (
            timedelta(seconds=row.latest_quiet_seconds)
            if row.latest_quiet_seconds is not None
            else None
        )
        return ScheduledProcessingRange(
            processing_range=DomainProcessingRange(
                range_id=str(row.id),
                start_at=row.start_at,
                boundary_kind=BoundaryKind(row.boundary_kind),
                fixed_end_at=row.fixed_end_at,
                latest_quiet_period=latest_quiet_period,
            ),
            source_channel_id=str(row.source_channel_id),
            source_profile_version_id=str(row.source_profile_version_id),
            processing_watermark_at=row.processing_watermark_at,
        )


class SqlAlchemyDurableWakeupRepository:
    """Leases scheduler signals in short transactions, never around broker I/O."""

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
                wakeups = tuple(
                    (await session.scalars(due_wakeup_claim_statement(now=now, limit=limit))).all()
                )
                claims: list[ClaimedWakeup] = []
                for wakeup in wakeups:
                    lease_token = uuid4()
                    wakeup.status = "leased"
                    wakeup.lease_token = lease_token
                    wakeup.lease_expires_at = now + lease_duration
                    wakeup.updated_at = now
                    claims.append(
                        ClaimedWakeup(
                            wakeup_id=str(wakeup.id),
                            queue=wakeup.queue,
                            entity_id=str(wakeup.entity_id),
                            lease_token=str(lease_token),
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
        return await self._release_lease(
            wakeup_id=wakeup_id,
            lease_token=lease_token,
            next_attempt_at=repair_after,
            dispatched_at=now,
            increment_dispatch_attempts=True,
        )

    async def reschedule_after_failure(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        retry_after: datetime,
    ) -> bool:
        return await self._release_lease(
            wakeup_id=wakeup_id,
            lease_token=lease_token,
            next_attempt_at=retry_after,
            dispatched_at=None,
            increment_dispatch_attempts=False,
        )

    async def complete_for_entity(self, *, queue: str, entity_id: str, now: datetime) -> bool:
        statement = (
            update(DurableWakeup)
            .where(
                DurableWakeup.queue == queue,
                DurableWakeup.entity_id == UUID(entity_id),
                DurableWakeup.status.in_(("pending", "leased")),
            )
            .values(
                status="completed",
                lease_token=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
            .returning(DurableWakeup.id)
        )
        async with self._database.session() as session:
            async with session.begin():
                return await session.scalar(statement) is not None

    async def _release_lease(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        next_attempt_at: datetime,
        dispatched_at: datetime | None,
        increment_dispatch_attempts: bool,
    ) -> bool:
        values: dict[str, object] = {
            "status": "pending",
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": next_attempt_at,
            "updated_at": next_attempt_at,
        }
        if dispatched_at is not None:
            values["last_dispatched_at"] = dispatched_at
        if increment_dispatch_attempts:
            values["dispatch_attempts"] = DurableWakeup.dispatch_attempts + 1

        statement = (
            update(DurableWakeup)
            .where(
                DurableWakeup.id == UUID(wakeup_id),
                DurableWakeup.lease_token == UUID(lease_token),
                DurableWakeup.status == "leased",
            )
            .values(**values)
            .returning(DurableWakeup.id)
        )
        async with self._database.session() as session:
            async with session.begin():
                return await session.scalar(statement) is not None
