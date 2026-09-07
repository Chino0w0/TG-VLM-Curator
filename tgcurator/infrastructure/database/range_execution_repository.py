from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update

from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.domain.processing import RangeExecution, RangeExecutionStatus
from tgcurator.infrastructure.database.models import (
    DurableWakeup,
    ProcessingRange,
    RangeExecutionRecord,
)
from tgcurator.infrastructure.database.session import AsyncDatabase

RANGE_EXECUTION_QUEUE = "range_execution"


def range_execution_claim_statement(*, execution_id: UUID, now: datetime):
    """Build a non-blocking PostgreSQL lease claim for one idempotent worker task."""

    return (
        select(RangeExecutionRecord)
        .where(
            RangeExecutionRecord.id == execution_id,
            or_(
                RangeExecutionRecord.status == "pending",
                and_(
                    RangeExecutionRecord.status == "running",
                    RangeExecutionRecord.lease_expires_at <= now,
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    )


class SqlAlchemyRangeExecutionWorkerRepository:
    """Persist worker leases and contiguous range advancement in short transactions."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def claim_execution(
        self,
        *,
        execution_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedRangeExecution | None:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    range_execution_claim_statement(execution_id=UUID(execution_id), now=now)
                )
                if row is None:
                    return None
                lease_token = uuid4()
                row.status = "running"
                row.lease_token = lease_token
                row.lease_expires_at = now + lease_duration
                row.started_at = row.started_at or now
                row.updated_at = now
                return ClaimedRangeExecution(
                    execution=self._to_domain(row),
                    source_profile_version_id=str(row.source_profile_version_id),
                    lease_token=str(lease_token),
                )

    async def advance_watermark(
        self,
        *,
        execution_id: str,
        lease_token: str,
        watermark_at: datetime,
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
                advanced = self._to_domain(row).advance_watermark(watermark_at)
                row.watermark_at = advanced.watermark_at
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
                if row is None:
                    return False
                completed = self._to_domain(row).complete()
                processing_range = await session.scalar(
                    select(ProcessingRange)
                    .where(ProcessingRange.id == row.processing_range_id)
                    .with_for_update()
                )
                assert processing_range is not None
                if not self._range_can_advance(processing_range=processing_range, execution=row):
                    return False

                row.status = completed.status.value
                row.watermark_at = completed.watermark_at
                row.lease_token = None
                row.lease_expires_at = None
                row.completed_at = now
                row.updated_at = now
                processing_range.processing_watermark_at = completed.to_at
                processing_range.updated_at = now
                await session.execute(
                    update(DurableWakeup)
                    .where(
                        DurableWakeup.queue == RANGE_EXECUTION_QUEUE,
                        DurableWakeup.entity_id == row.id,
                        DurableWakeup.status.in_(("pending", "leased")),
                    )
                    .values(
                        status="completed",
                        lease_token=None,
                        lease_expires_at=None,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                return True

    @staticmethod
    async def _locked_active_row(
        *,
        session,
        execution_id: UUID,
        lease_token: UUID,
        now: datetime,
    ) -> RangeExecutionRecord | None:
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
    def _range_can_advance(
        *,
        processing_range: ProcessingRange,
        execution: RangeExecutionRecord,
    ) -> bool:
        if processing_range.processing_watermark_at is None:
            return execution.from_at == processing_range.start_at
        return execution.from_at == processing_range.processing_watermark_at

    @staticmethod
    def _to_domain(row: RangeExecutionRecord) -> RangeExecution:
        return RangeExecution(
            execution_id=str(row.id),
            range_id=str(row.processing_range_id),
            from_at=row.from_at,
            to_at=row.to_at,
            watermark_at=row.watermark_at,
            status=RangeExecutionStatus(row.status),
        )
