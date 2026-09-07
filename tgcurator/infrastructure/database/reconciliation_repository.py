from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.sql.dml import Update

from .models import SourceChannel
from .session import AsyncDatabase


def source_reconciliation_cursor_advance_statement(
    *, source_channel_id: UUID, telegram_message_id: int
) -> Update:
    """Build the atomic compare-and-set update that prevents cursor regression."""

    return (
        update(SourceChannel)
        .where(
            SourceChannel.id == source_channel_id,
            or_(
                SourceChannel.last_seen_message_id.is_(None),
                SourceChannel.last_seen_message_id < telegram_message_id,
            ),
        )
        .values(last_seen_message_id=telegram_message_id)
    )


class SqlAlchemySourceReconciliationCursorRepository:
    """PostgreSQL persistence adapter for source-channel reconnect cursors."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    source_reconciliation_cursor_advance_statement(
                        source_channel_id=UUID(source_channel_id),
                        telegram_message_id=telegram_message_id,
                    )
                )
                return result.rowcount == 1

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        async with self._database.session() as session:
            return await session.scalar(
                select(SourceChannel.last_seen_message_id).where(
                    SourceChannel.id == UUID(source_channel_id)
                )
            )
