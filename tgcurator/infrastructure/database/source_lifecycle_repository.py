from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, or_, select, update
from sqlalchemy.sql.dml import Update

from .models import MessagePartRecord, MessageRecord
from .session import AsyncDatabase


def _message_with_component_predicate(*, source_channel_id: UUID, telegram_message_id: int):
    return (
        MessageRecord.source_channel_id == source_channel_id,
        exists(
            select(MessagePartRecord.id).where(
                MessagePartRecord.message_id == MessageRecord.id,
                MessagePartRecord.source_channel_id == source_channel_id,
                MessagePartRecord.telegram_message_id == telegram_message_id,
            )
        ),
    )


def source_message_edit_statement(
    *,
    source_channel_id: UUID,
    telegram_message_id: int,
    text: str | None,
    edited_at: datetime,
) -> Update:
    """Update only when the source edit timestamp is newer than our retained version."""

    return (
        update(MessageRecord)
        .where(
            *_message_with_component_predicate(
                source_channel_id=source_channel_id,
                telegram_message_id=telegram_message_id,
            ),
            or_(
                MessageRecord.source_edited_at.is_(None),
                MessageRecord.source_edited_at < edited_at,
            ),
        )
        .values(
            text=text,
            source_edited_at=edited_at,
            source_changed_after_processing=True,
        )
    )


def source_message_deletion_statement(
    *, source_channel_id: UUID, telegram_message_id: int, deleted_at: datetime
) -> Update:
    """Record the first observed source deletion without deleting business evidence."""

    return (
        update(MessageRecord)
        .where(
            *_message_with_component_predicate(
                source_channel_id=source_channel_id,
                telegram_message_id=telegram_message_id,
            ),
            MessageRecord.source_deleted_at.is_(None),
        )
        .values(
            source_deleted_at=deleted_at,
            source_changed_after_processing=True,
        )
    )


class SqlAlchemySourceMessageLifecycleRepository:
    """PostgreSQL adapter that finds logical messages through retained Telegram parts."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        text: str | None,
        edited_at: datetime,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    source_message_edit_statement(
                        source_channel_id=UUID(source_channel_id),
                        telegram_message_id=telegram_message_id,
                        text=text,
                        edited_at=edited_at,
                    )
                )
                return result.rowcount == 1

    async def record_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    source_message_deletion_statement(
                        source_channel_id=UUID(source_channel_id),
                        telegram_message_id=telegram_message_id,
                        deleted_at=deleted_at,
                    )
                )
                return result.rowcount == 1
