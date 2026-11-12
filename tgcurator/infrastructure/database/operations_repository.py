from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select

from tgcurator.application.ports.operations import BacklogMetricsSnapshot
from tgcurator.shared import ensure_aware

from .models import (
    ArchiveCleanupJobRecord,
    DurableWakeupRecord,
    PublicationIntentRecord,
)
from .session import AsyncDatabase

_TERMINAL_PUBLICATION_STATUSES = ("sent", "failed", "cancelled")


def operations_backlog_statement(*, now: datetime):
    """Aggregate reconstructable publication, cleanup, and wake-up backlog counts."""

    ensure_aware(now, field="now")
    publication_pending = (
        select(func.count(PublicationIntentRecord.id))
        .where(
            PublicationIntentRecord.status == "pending",
            func.cardinality(PublicationIntentRecord.completed_parts) == 0,
        )
        .scalar_subquery()
    )
    publication_partial = (
        select(func.count(PublicationIntentRecord.id))
        .where(
            PublicationIntentRecord.status.not_in(_TERMINAL_PUBLICATION_STATUSES),
            or_(
                PublicationIntentRecord.status == "partial",
                func.cardinality(PublicationIntentRecord.completed_parts) > 0,
            ),
        )
        .scalar_subquery()
    )
    publication_retry_due = (
        select(func.count(PublicationIntentRecord.id))
        .where(
            or_(
                and_(
                    PublicationIntentRecord.status == "retry_wait",
                    PublicationIntentRecord.next_retry_at <= now,
                ),
                and_(
                    PublicationIntentRecord.status == "sending",
                    PublicationIntentRecord.lease_expires_at <= now,
                ),
            )
        )
        .scalar_subquery()
    )
    cleanup_pending = (
        select(func.count(ArchiveCleanupJobRecord.id))
        .where(ArchiveCleanupJobRecord.status == "pending")
        .scalar_subquery()
    )
    cleanup_retry_due = (
        select(func.count(ArchiveCleanupJobRecord.id))
        .where(
            or_(
                and_(
                    ArchiveCleanupJobRecord.status == "retry_wait",
                    ArchiveCleanupJobRecord.next_retry_at <= now,
                ),
                and_(
                    ArchiveCleanupJobRecord.status == "running",
                    ArchiveCleanupJobRecord.lease_expires_at <= now,
                ),
            )
        )
        .scalar_subquery()
    )
    durable_wakeups_due = (
        select(func.count(DurableWakeupRecord.id))
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
        .scalar_subquery()
    )
    return select(
        publication_pending.label("publication_pending"),
        publication_partial.label("publication_partial"),
        publication_retry_due.label("publication_retry_due"),
        cleanup_pending.label("cleanup_pending"),
        cleanup_retry_due.label("cleanup_retry_due"),
        durable_wakeups_due.label("durable_wakeups_due"),
    )


class SqlAlchemyOperationsBacklogRepository:
    """Read-only PostgreSQL observer for reconstructable backlog gauges."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def observe_backlog(self, *, now: datetime) -> BacklogMetricsSnapshot:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            row = (await session.execute(operations_backlog_statement(now=now))).one()
            return _backlog_snapshot(row=row, observed_at=now)


def _backlog_snapshot(*, row: Any, observed_at: datetime) -> BacklogMetricsSnapshot:
    values: Mapping[str, Any]
    if hasattr(row, "_mapping"):
        values = row._mapping
    elif isinstance(row, Mapping):
        values = row
    else:
        (
            publication_pending,
            publication_partial,
            publication_retry_due,
            cleanup_pending,
            cleanup_retry_due,
            durable_wakeups_due,
        ) = row
        values = {
            "publication_pending": publication_pending,
            "publication_partial": publication_partial,
            "publication_retry_due": publication_retry_due,
            "cleanup_pending": cleanup_pending,
            "cleanup_retry_due": cleanup_retry_due,
            "durable_wakeups_due": durable_wakeups_due,
        }
    return BacklogMetricsSnapshot(
        publication_pending=int(values["publication_pending"] or 0),
        publication_partial=int(values["publication_partial"] or 0),
        publication_retry_due=int(values["publication_retry_due"] or 0),
        cleanup_pending=int(values["cleanup_pending"] or 0),
        cleanup_retry_due=int(values["cleanup_retry_due"] or 0),
        durable_wakeups_due=int(values["durable_wakeups_due"] or 0),
        observed_at=observed_at,
    )
