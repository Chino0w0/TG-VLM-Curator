from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.sql.dml import Update

from tgcurator.domain.messages import MediaAsset, media_assets_to_json, normalized_from_parts

from .message_ingest_repository import (
    message_parent_update_statement,
    message_parts_for_parent_statement,
    synchronize_archive_assets,
    telegram_part_from_record,
)
from .models import MessagePartRecord, MessageRecord
from .session import AsyncDatabase


def source_message_with_component_statement(*, source_channel_id: UUID, telegram_message_id: int):
    """Lock one rich component and its logical parent for a lifecycle transition."""

    return (
        select(MessagePartRecord, MessageRecord)
        .join(MessageRecord, MessageRecord.id == MessagePartRecord.message_id)
        .where(
            MessagePartRecord.source_channel_id == source_channel_id,
            MessagePartRecord.telegram_message_id == telegram_message_id,
            MessageRecord.source_channel_id == source_channel_id,
        )
        .with_for_update()
    )


def source_message_edit_statement(
    *,
    message_part_id: UUID,
    original_text: str | None,
    telegram_metadata: Mapping[str, Any],
    media: tuple[MediaAsset, ...],
    edited_at: datetime,
) -> Update:
    """Replace one complete part snapshot after its monotonic lock-time check."""

    return (
        update(MessagePartRecord)
        .where(MessagePartRecord.id == message_part_id)
        .values(
            original_text=original_text,
            telegram_metadata=dict(telegram_metadata),
            media=media_assets_to_json(media),
            media_count=len(media),
            edited_at=edited_at,
        )
    )


def source_message_deletion_statement(
    *, message_id: UUID, deleted_at: datetime, changed_after_processing: bool
) -> Update:
    """Record the first observed source deletion without deleting business evidence."""

    return (
        update(MessageRecord)
        .where(
            MessageRecord.id == message_id,
            MessageRecord.source_deleted_at.is_(None),
        )
        .values(
            source_deleted=True,
            source_deleted_at=deleted_at,
            source_changed_after_processing=changed_after_processing,
        )
    )


class SqlAlchemySourceMessageLifecycleRepository:
    """PostgreSQL adapter that rebuilds logical messages from retained Telegram parts."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        original_text: str | None,
        telegram_metadata: Mapping[str, Any],
        media: tuple[MediaAsset, ...],
        edited_at: datetime,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                locked = (
                    await session.execute(
                        source_message_with_component_statement(
                            source_channel_id=UUID(source_channel_id),
                            telegram_message_id=telegram_message_id,
                        )
                    )
                ).one_or_none()
                if locked is None:
                    return False
                part, parent = locked
                if part.edited_at is not None and edited_at <= part.edited_at:
                    return False

                await session.execute(
                    source_message_edit_statement(
                        message_part_id=part.id,
                        original_text=original_text,
                        telegram_metadata=telegram_metadata,
                        media=media,
                        edited_at=edited_at,
                    )
                )
                part.original_text = original_text
                part.telegram_metadata = dict(telegram_metadata)
                part.media = media_assets_to_json(media)
                part.media_count = len(media)
                part.edited_at = edited_at

                retained_records = (
                    (
                        await session.execute(
                            message_parts_for_parent_statement(message_id=parent.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                recomputed = normalized_from_parts(
                    source_channel_id=source_channel_id,
                    telegram_grouped_id=parent.telegram_grouped_id,
                    parts=tuple(telegram_part_from_record(record) for record in retained_records),
                )
                await session.execute(
                    message_parent_update_statement(message_id=parent.id, message=recomputed)
                )
                if parent.processing_status == "processed":
                    parent.source_changed_after_processing = True
                await synchronize_archive_assets(
                    session=session, message_id=parent.id, message=recomputed
                )
                return True

    async def record_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                locked = (
                    await session.execute(
                        source_message_with_component_statement(
                            source_channel_id=UUID(source_channel_id),
                            telegram_message_id=telegram_message_id,
                        )
                    )
                ).one_or_none()
                if locked is None:
                    return False
                _, parent = locked
                if parent.source_deleted_at is not None:
                    return False
                result = await session.execute(
                    source_message_deletion_statement(
                        message_id=parent.id,
                        deleted_at=deleted_at,
                        changed_after_processing=(
                            parent.source_changed_after_processing
                            or parent.processing_status == "processed"
                        ),
                    )
                )
                return result.rowcount == 1
