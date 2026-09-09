from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.sql.dml import Update

from .models import SourceChannel
from .session import AsyncDatabase


def _monotonic_cursor_advance_statement(
    *, source_channel_id: UUID, telegram_message_id: int, column
) -> Update:
    return (
        update(SourceChannel)
        .where(
            SourceChannel.id == source_channel_id,
            or_(column.is_(None), column < telegram_message_id),
        )
        .values({column.key: telegram_message_id})
    )


def source_latest_seen_cursor_advance_statement(
    *, source_channel_id: UUID, telegram_message_id: int
) -> Update:
    """Atomically retain the greatest Telegram boundary observed for a source."""

    return _monotonic_cursor_advance_statement(
        source_channel_id=source_channel_id,
        telegram_message_id=telegram_message_id,
        column=SourceChannel.latest_seen_message_id,
    )


def source_last_seen_cursor_advance_statement(
    *, source_channel_id: UUID, telegram_message_id: int
) -> Update:
    """Atomically retain the greatest Telegram message successfully ingested."""

    return _monotonic_cursor_advance_statement(
        source_channel_id=source_channel_id,
        telegram_message_id=telegram_message_id,
        column=SourceChannel.last_seen_message_id,
    )


class SqlAlchemySourceReconciliationCursorRepository:
    """PostgreSQL persistence adapter for source-channel reconnect cursors."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def advance_latest_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        return await self._advance(
            statement=source_latest_seen_cursor_advance_statement(
                source_channel_id=UUID(source_channel_id),
                telegram_message_id=telegram_message_id,
            ),
        )

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        return await self._advance(
            statement=source_last_seen_cursor_advance_statement(
                source_channel_id=UUID(source_channel_id),
                telegram_message_id=telegram_message_id,
            ),
        )

    async def get_latest_seen_message_id(self, *, source_channel_id: str) -> int | None:
        return await self._read(
            source_channel_id=source_channel_id,
            column=SourceChannel.latest_seen_message_id,
        )

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        return await self._read(
            source_channel_id=source_channel_id,
            column=SourceChannel.last_seen_message_id,
        )

    async def _advance(self, *, statement: Update) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(statement)
                return result.rowcount == 1

    async def _read(self, *, source_channel_id: str, column) -> int | None:
        async with self._database.session() as session:
            return await session.scalar(
                select(column).where(SourceChannel.id == UUID(source_channel_id))
            )
