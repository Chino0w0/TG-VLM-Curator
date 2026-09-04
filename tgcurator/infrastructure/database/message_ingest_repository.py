from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from tgcurator.domain.messages import MediaKind, NormalizedTelegramMessage
from tgcurator.shared import DomainValidationError

from .models import ImageAssetRecord, MessagePartRecord, MessageRecord
from .session import AsyncDatabase


def message_upsert_statement(message: NormalizedTelegramMessage):
    """Build the PostgreSQL idempotent upsert for one normalized logical message."""

    source_channel_id = UUID(message.source_channel_id)
    statement = postgresql.insert(MessageRecord).values(
        id=uuid4(),
        source_channel_id=source_channel_id,
        telegram_anchor_message_id=message.telegram_anchor_message_id,
        telegram_group_id=message.telegram_group_id,
        sent_at=message.sent_at,
        text=message.content.text,
        media_count=len(message.content.media),
        visual_fingerprint=message.content.visual_fingerprint,
    )
    updates = {
        "telegram_anchor_message_id": func.least(
            MessageRecord.telegram_anchor_message_id,
            statement.excluded.telegram_anchor_message_id,
        ),
        "sent_at": func.least(MessageRecord.sent_at, statement.excluded.sent_at),
        "text": func.coalesce(statement.excluded.text, MessageRecord.text),
        "media_count": func.greatest(MessageRecord.media_count, statement.excluded.media_count),
        "visual_fingerprint": func.coalesce(
            statement.excluded.visual_fingerprint, MessageRecord.visual_fingerprint
        ),
    }
    if message.telegram_group_id is None:
        return statement.on_conflict_do_update(
            constraint="uq_message_source_anchor", set_=updates
        ).returning(MessageRecord.id)
    return statement.on_conflict_do_update(
        index_elements=[MessageRecord.source_channel_id, MessageRecord.telegram_group_id],
        index_where=MessageRecord.telegram_group_id.is_not(None),
        set_=updates,
    ).returning(MessageRecord.id)


def image_assets_upsert_statement(*, message_id: UUID, message: NormalizedTelegramMessage):
    """Create missing image-asset metadata rows while preserving archive lifecycle state."""

    image_assets = tuple(asset for asset in message.content.media if asset.kind is MediaKind.IMAGE)
    if not image_assets:
        return None
    statement = postgresql.insert(ImageAssetRecord).values(
        [
            {
                "id": uuid4(),
                "message_id": message_id,
                "source_asset_id": asset.asset_id,
                "source_phash": asset.original_visual_phash,
                "source_telegram_message_id": asset.source_telegram_message_id,
                "archive_state": "pending",
            }
            for asset in image_assets
        ]
    )
    return statement.on_conflict_do_update(
        constraint="uq_image_asset_message_source",
        set_={
            "source_phash": func.coalesce(
                statement.excluded.source_phash, ImageAssetRecord.source_phash
            ),
            "source_telegram_message_id": func.coalesce(
                ImageAssetRecord.source_telegram_message_id,
                statement.excluded.source_telegram_message_id,
            ),
        },
    )


def message_parts_upsert_statement(*, message_id: UUID, message: NormalizedTelegramMessage):
    """Insert only newly observed component IDs; replays preserve their original ownership."""

    return (
        postgresql.insert(MessagePartRecord)
        .values(
            [
                {
                    "id": uuid4(),
                    "message_id": message_id,
                    "source_channel_id": UUID(message.source_channel_id),
                    "telegram_message_id": telegram_message_id,
                }
                for telegram_message_id in message.telegram_message_ids
            ]
        )
        .on_conflict_do_nothing(constraint="uq_message_part_source_telegram")
    )


class SqlAlchemyTelegramMessageIngestRepository:
    """PostgreSQL message/part persistence in short, idempotent transactions."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def upsert_message(self, *, message: NormalizedTelegramMessage) -> None:
        source_channel_id = UUID(message.source_channel_id)
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(message_upsert_statement(message))
                message_id = result.scalar_one()
                image_assets_statement = image_assets_upsert_statement(
                    message_id=message_id, message=message
                )
                if image_assets_statement is not None:
                    await session.execute(image_assets_statement)
                await session.execute(
                    message_parts_upsert_statement(message_id=message_id, message=message)
                )
                memberships = await session.execute(
                    select(
                        MessagePartRecord.telegram_message_id,
                        MessagePartRecord.message_id,
                    ).where(
                        MessagePartRecord.source_channel_id == source_channel_id,
                        MessagePartRecord.telegram_message_id.in_(message.telegram_message_ids),
                    )
                )
                wrong_owner_ids = sorted(
                    telegram_message_id
                    for telegram_message_id, owner_id in memberships
                    if owner_id != message_id
                )
                if wrong_owner_ids:
                    raise DomainValidationError(
                        "Telegram component IDs already belong to a different logical message: "
                        + ", ".join(str(message_id) for message_id in wrong_owner_ids)
                    )
