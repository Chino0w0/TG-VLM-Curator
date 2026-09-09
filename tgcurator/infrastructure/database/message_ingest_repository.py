from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects import postgresql

from tgcurator.application.media.archive_wakeups import (
    IMAGE_ARCHIVE_QUEUE,
    VIDEO_ARCHIVE_QUEUE,
)
from tgcurator.domain.messages import (
    MediaKind,
    NormalizedTelegramMessage,
    TelegramMessagePart,
    media_assets_from_json,
    media_assets_to_json,
    normalized_from_parts,
)
from tgcurator.shared import DomainValidationError

from .models import (
    DurableWakeupRecord,
    ImageAssetRecord,
    MessagePartRecord,
    MessageRecord,
    VideoAssetRecord,
)
from .session import AsyncDatabase


def message_upsert_statement(message: NormalizedTelegramMessage):
    """Resolve and lock one parent through its stable regular/grouped identity."""

    statement = postgresql.insert(MessageRecord).values(
        id=uuid4(),
        source_channel_id=UUID(message.source_channel_id),
        telegram_message_ids=list(message.telegram_message_ids),
        telegram_grouped_id=message.telegram_grouped_id,
        primary_telegram_message_id=message.primary_telegram_message_id,
        published_at=message.published_at,
        edited_at=message.edited_at,
        original_text=message.original_text,
        telegram_metadata=dict(message.telegram_metadata),
        media_count=message.media_count,
        visual_fingerprint=message.visual_fingerprint,
    )
    # The no-op update is intentional: PostgreSQL locks an already-existing parent and returns its
    # UUID. The complete parent snapshot is recomputed from all retained parts later in the same
    # transaction, so a partial album replay cannot temporarily become business truth.
    no_op_update = {"source_channel_id": MessageRecord.source_channel_id}
    if message.telegram_grouped_id is None:
        return statement.on_conflict_do_update(
            index_elements=[
                MessageRecord.source_channel_id,
                MessageRecord.primary_telegram_message_id,
            ],
            index_where=MessageRecord.telegram_grouped_id.is_(None),
            set_=no_op_update,
        ).returning(MessageRecord.id)
    return statement.on_conflict_do_update(
        index_elements=[MessageRecord.source_channel_id, MessageRecord.telegram_grouped_id],
        index_where=MessageRecord.telegram_grouped_id.is_not(None),
        set_=no_op_update,
    ).returning(MessageRecord.id)


def message_parts_upsert_statement(*, message_id: UUID, message: NormalizedTelegramMessage):
    """Upsert rich part snapshots without changing ownership or accepting an older edit."""

    statement = postgresql.insert(MessagePartRecord).values(
        [
            {
                "id": uuid4(),
                "message_id": message_id,
                "source_channel_id": UUID(message.source_channel_id),
                "telegram_message_id": part.telegram_message_id,
                "published_at": part.published_at,
                "edited_at": part.edited_at,
                "original_text": part.original_text,
                "telegram_metadata": dict(part.telegram_metadata or {}),
                "media": media_assets_to_json(part.media),
                "media_count": len(part.media),
            }
            for part in message.parts
        ]
    )
    incoming_is_not_stale = or_(
        MessagePartRecord.edited_at.is_(None),
        and_(
            statement.excluded.edited_at.is_not(None),
            MessagePartRecord.edited_at <= statement.excluded.edited_at,
        ),
    )
    return statement.on_conflict_do_update(
        constraint="uq_message_part_source_telegram",
        set_={
            "published_at": func.least(
                MessagePartRecord.published_at, statement.excluded.published_at
            ),
            "edited_at": statement.excluded.edited_at,
            "original_text": statement.excluded.original_text,
            "telegram_metadata": statement.excluded.telegram_metadata,
            "media": statement.excluded.media,
            "media_count": statement.excluded.media_count,
            "updated_at": func.now(),
        },
        where=and_(
            MessagePartRecord.message_id == statement.excluded.message_id,
            incoming_is_not_stale,
        ),
    )


def message_parts_ownership_statement(*, message: NormalizedTelegramMessage):
    """Lock and return ownership for every component supplied by this ingestion call."""

    return (
        select(MessagePartRecord.telegram_message_id, MessagePartRecord.message_id)
        .where(
            MessagePartRecord.source_channel_id == UUID(message.source_channel_id),
            MessagePartRecord.telegram_message_id.in_(message.telegram_message_ids),
        )
        .with_for_update()
    )


def message_parts_for_parent_statement(*, message_id: UUID):
    """Reload every retained part so the parent is never rebuilt from a partial replay."""

    return (
        select(MessagePartRecord)
        .where(MessagePartRecord.message_id == message_id)
        .order_by(MessagePartRecord.telegram_message_id)
        .with_for_update()
    )


def message_parent_update_statement(*, message_id: UUID, message: NormalizedTelegramMessage):
    """Replace only the current source snapshot while retaining processing/lifecycle history."""

    return (
        update(MessageRecord)
        .where(MessageRecord.id == message_id)
        .values(
            telegram_message_ids=list(message.telegram_message_ids),
            primary_telegram_message_id=message.primary_telegram_message_id,
            published_at=message.published_at,
            edited_at=message.edited_at,
            original_text=message.original_text,
            telegram_metadata=dict(message.telegram_metadata),
            media_count=message.media_count,
            visual_fingerprint=message.visual_fingerprint,
        )
    )


def image_assets_upsert_statement(*, message_id: UUID, message: NormalizedTelegramMessage):
    """Upsert current image source facts without resetting archive lifecycle or history."""

    image_assets = tuple(asset for asset in message.media if asset.kind is MediaKind.IMAGE)
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
                statement.excluded.source_telegram_message_id,
                ImageAssetRecord.source_telegram_message_id,
            ),
        },
    )


def video_assets_upsert_statement(*, message_id: UUID, message: NormalizedTelegramMessage):
    """Upsert current video source facts without resetting archive lifecycle or frame history."""

    video_assets = tuple(asset for asset in message.media if asset.kind is MediaKind.VIDEO)
    if not video_assets:
        return None
    statement = postgresql.insert(VideoAssetRecord).values(
        [
            {
                "id": uuid4(),
                "message_id": message_id,
                "source_asset_id": asset.asset_id,
                "source_telegram_message_id": asset.source_telegram_message_id,
                "source_cover_phash": asset.video_cover_phash,
                "archive_state": "pending",
            }
            for asset in video_assets
        ]
    )
    return statement.on_conflict_do_update(
        constraint="uq_video_asset_message_source",
        set_={
            "source_cover_phash": func.coalesce(
                statement.excluded.source_cover_phash, VideoAssetRecord.source_cover_phash
            ),
            "source_telegram_message_id": func.coalesce(
                statement.excluded.source_telegram_message_id,
                VideoAssetRecord.source_telegram_message_id,
            ),
        },
    )


def archive_wakeups_insert_statement(*, queue: str, entity_ids: tuple[UUID, ...]):
    """Create reconstructable UUID-only archive wake-ups in the same ingest transaction."""

    if not entity_ids:
        return None
    return (
        postgresql.insert(DurableWakeupRecord)
        .values(
            [
                {
                    "id": uuid4(),
                    "queue": queue,
                    "entity_id": entity_id,
                    "status": "pending",
                    "next_attempt_at": func.now(),
                    "dispatch_attempts": 0,
                }
                for entity_id in entity_ids
            ]
        )
        .on_conflict_do_nothing(constraint="uq_durable_wakeup_queue_entity")
    )


def image_archive_wakeups_insert_statement(*, image_asset_ids: tuple[UUID, ...]):
    return archive_wakeups_insert_statement(queue=IMAGE_ARCHIVE_QUEUE, entity_ids=image_asset_ids)


def video_archive_wakeups_insert_statement(*, video_asset_ids: tuple[UUID, ...]):
    return archive_wakeups_insert_statement(queue=VIDEO_ARCHIVE_QUEUE, entity_ids=video_asset_ids)


def telegram_part_from_record(record: MessagePartRecord) -> TelegramMessagePart:
    """Reconstruct one canonical Telegram part from its rich durable snapshot."""

    return TelegramMessagePart(
        telegram_message_id=record.telegram_message_id,
        published_at=record.published_at,
        edited_at=record.edited_at,
        original_text=record.original_text,
        telegram_metadata=dict(record.telegram_metadata),
        media=media_assets_from_json(record.media),
    )


async def synchronize_archive_assets(*, session, message_id: UUID, message) -> None:
    """Synchronize source media and durable wake-ups without resetting archive history."""

    await _upsert_image_assets(session=session, message_id=message_id, message=message)
    await _upsert_video_assets(session=session, message_id=message_id, message=message)


async def _upsert_image_assets(*, session, message_id: UUID, message) -> None:
    statement = image_assets_upsert_statement(message_id=message_id, message=message)
    if statement is None:
        return
    await session.execute(statement)
    source_asset_ids = tuple(
        asset.asset_id for asset in message.media if asset.kind is MediaKind.IMAGE
    )
    image_asset_ids = tuple(
        (
            await session.execute(
                select(ImageAssetRecord.id).where(
                    ImageAssetRecord.message_id == message_id,
                    ImageAssetRecord.source_asset_id.in_(source_asset_ids),
                    ImageAssetRecord.source_telegram_message_id.is_not(None),
                    ImageAssetRecord.archive_state == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    wakeup = image_archive_wakeups_insert_statement(image_asset_ids=image_asset_ids)
    if wakeup is not None:
        await session.execute(wakeup)


async def _upsert_video_assets(*, session, message_id: UUID, message) -> None:
    statement = video_assets_upsert_statement(message_id=message_id, message=message)
    if statement is None:
        return
    await session.execute(statement)
    source_asset_ids = tuple(
        asset.asset_id for asset in message.media if asset.kind is MediaKind.VIDEO
    )
    video_asset_ids = tuple(
        (
            await session.execute(
                select(VideoAssetRecord.id).where(
                    VideoAssetRecord.message_id == message_id,
                    VideoAssetRecord.source_asset_id.in_(source_asset_ids),
                    VideoAssetRecord.source_telegram_message_id.is_not(None),
                    VideoAssetRecord.archive_state == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    wakeup = video_archive_wakeups_insert_statement(video_asset_ids=video_asset_ids)
    if wakeup is not None:
        await session.execute(wakeup)


class SqlAlchemyTelegramMessageIngestRepository:
    """PostgreSQL message/part/media persistence in one short idempotent transaction."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def upsert_message(self, *, message: NormalizedTelegramMessage) -> None:
        async with self._database.session() as session:
            async with session.begin():
                parent_result = await session.execute(message_upsert_statement(message))
                message_id = parent_result.scalar_one()

                await session.execute(
                    message_parts_upsert_statement(message_id=message_id, message=message)
                )
                memberships = (
                    await session.execute(message_parts_ownership_statement(message=message))
                ).all()
                self._validate_component_ownership(
                    message_id=message_id,
                    requested_ids=message.telegram_message_ids,
                    memberships=memberships,
                )

                retained_records = (
                    (
                        await session.execute(
                            message_parts_for_parent_statement(message_id=message_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                retained_parts = tuple(
                    telegram_part_from_record(record) for record in retained_records
                )
                recomputed = normalized_from_parts(
                    source_channel_id=message.source_channel_id,
                    telegram_grouped_id=message.telegram_grouped_id,
                    parts=retained_parts,
                )
                await session.execute(
                    message_parent_update_statement(message_id=message_id, message=recomputed)
                )

                await synchronize_archive_assets(
                    session=session, message_id=message_id, message=recomputed
                )

    @staticmethod
    def _validate_component_ownership(
        *,
        message_id: UUID,
        requested_ids: tuple[int, ...],
        memberships: list[tuple[int, UUID]],
    ) -> None:
        owners = {telegram_message_id: owner_id for telegram_message_id, owner_id in memberships}
        wrong_owner_ids = sorted(
            telegram_message_id
            for telegram_message_id, owner_id in owners.items()
            if owner_id != message_id
        )
        if wrong_owner_ids:
            raise DomainValidationError(
                "Telegram component IDs already belong to a different logical message: "
                + ", ".join(str(value) for value in wrong_owner_ids)
            )
        missing_ids = sorted(set(requested_ids) - set(owners))
        if missing_ids:
            raise DomainValidationError(
                "Telegram component IDs could not be retained atomically: "
                + ", ".join(str(value) for value in missing_ids)
            )
