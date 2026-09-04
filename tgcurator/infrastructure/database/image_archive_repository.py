from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.sql.dml import Update

from tgcurator.application.ports.media import (
    ClaimedImageArchive,
    ImageArchiveReadyMetadata,
    ImageArchiveWorkItem,
)

from .models import ImageAssetRecord, MessageRecord
from .session import AsyncDatabase

_TERMINAL_FAILURE_REASONS = frozenset(
    {"media_unavailable", "protected_content", "source_message_deleted"}
)


def image_asset_mark_ready_statement(*, metadata: ImageArchiveReadyMetadata) -> Update:
    """Move an unleased pending image to READY, while allowing an exact immutable replay only."""

    pending = and_(
        ImageAssetRecord.archive_state == "pending",
        ImageAssetRecord.archive_lease_token.is_(None),
        ImageAssetRecord.archive_lease_expires_at.is_(None),
    )
    exact_ready_replay = and_(
        ImageAssetRecord.archive_state == "ready",
        ImageAssetRecord.storage_backend == metadata.storage_backend,
        ImageAssetRecord.storage_key == metadata.storage_key,
        ImageAssetRecord.content_type == metadata.content_type,
        ImageAssetRecord.width == metadata.width,
        ImageAssetRecord.height == metadata.height,
        ImageAssetRecord.source_sha256 == metadata.source_sha256,
        ImageAssetRecord.archive_sha256 == metadata.archive_sha256,
        ImageAssetRecord.perceptual_hash == metadata.perceptual_hash,
        ImageAssetRecord.archive_size_bytes == metadata.archive_size_bytes,
    )
    return (
        update(ImageAssetRecord)
        .where(
            ImageAssetRecord.id == UUID(metadata.image_asset_id),
            ImageAssetRecord.archive_state.in_(("pending", "ready")),
            or_(pending, exact_ready_replay),
        )
        .values(
            archive_state="ready",
            archive_lease_token=None,
            archive_lease_expires_at=None,
            storage_backend=metadata.storage_backend,
            storage_key=metadata.storage_key,
            content_type=metadata.content_type,
            width=metadata.width,
            height=metadata.height,
            source_sha256=metadata.source_sha256,
            archive_sha256=metadata.archive_sha256,
            perceptual_hash=metadata.perceptual_hash,
            archive_size_bytes=metadata.archive_size_bytes,
            archive_ready_at=case(
                (pending, metadata.archived_at), else_=ImageAssetRecord.archive_ready_at
            ),
            archive_deleted_at=None,
            archive_failure_reason=None,
            updated_at=case((pending, func.now()), else_=ImageAssetRecord.updated_at),
        )
    )


def image_archive_claim_statement(*, image_asset_id: UUID, now: datetime):
    """Find one claimable source image using a PostgreSQL non-blocking row lock."""

    claimable_lease = or_(
        ImageAssetRecord.archive_lease_token.is_(None),
        ImageAssetRecord.archive_lease_expires_at <= now,
    )
    return (
        select(
            ImageAssetRecord.id.label("image_asset_id"),
            ImageAssetRecord.source_asset_id,
            ImageAssetRecord.source_telegram_message_id,
            MessageRecord.source_channel_id,
            MessageRecord.source_deleted_at,
        )
        .join(MessageRecord, MessageRecord.id == ImageAssetRecord.message_id)
        .where(
            ImageAssetRecord.id == image_asset_id,
            ImageAssetRecord.archive_state == "pending",
            ImageAssetRecord.source_telegram_message_id.is_not(None),
            claimable_lease,
        )
        .with_for_update(skip_locked=True)
    )


def image_archive_mark_ready_claim_statement(
    *, claim: ClaimedImageArchive, metadata: ImageArchiveReadyMetadata
) -> Update:
    """Publish READY metadata only when the supplied image archive lease is still current."""

    return (
        update(ImageAssetRecord)
        .where(
            ImageAssetRecord.id == UUID(claim.work_item.image_asset_id),
            ImageAssetRecord.id == UUID(metadata.image_asset_id),
            ImageAssetRecord.archive_state == "pending",
            ImageAssetRecord.archive_lease_token == UUID(claim.lease_token),
        )
        .values(
            archive_state="ready",
            archive_lease_token=None,
            archive_lease_expires_at=None,
            storage_backend=metadata.storage_backend,
            storage_key=metadata.storage_key,
            content_type=metadata.content_type,
            width=metadata.width,
            height=metadata.height,
            source_sha256=metadata.source_sha256,
            archive_sha256=metadata.archive_sha256,
            perceptual_hash=metadata.perceptual_hash,
            archive_size_bytes=metadata.archive_size_bytes,
            archive_ready_at=metadata.archived_at,
            archive_deleted_at=None,
            archive_failure_reason=None,
            updated_at=func.now(),
        )
    )


def image_archive_mark_failed_statement(
    *, claim: ClaimedImageArchive, reason: str, now: datetime
) -> Update:
    """Record a known permanent source failure while atomically releasing the lease."""

    if reason not in _TERMINAL_FAILURE_REASONS:
        raise ValueError("reason is not a recognized terminal image archive failure")
    return (
        update(ImageAssetRecord)
        .where(
            ImageAssetRecord.id == UUID(claim.work_item.image_asset_id),
            ImageAssetRecord.archive_state == "pending",
            ImageAssetRecord.archive_lease_token == UUID(claim.lease_token),
        )
        .values(
            archive_state="failed",
            archive_lease_token=None,
            archive_lease_expires_at=None,
            archive_failure_reason=reason,
            updated_at=now,
        )
    )


def image_archive_release_statement(*, claim: ClaimedImageArchive, now: datetime) -> Update:
    """Release an active retryable image archive lease without persisting exception content."""

    return (
        update(ImageAssetRecord)
        .where(
            ImageAssetRecord.id == UUID(claim.work_item.image_asset_id),
            ImageAssetRecord.archive_state == "pending",
            ImageAssetRecord.archive_lease_token == UUID(claim.lease_token),
        )
        .values(
            archive_lease_token=None,
            archive_lease_expires_at=None,
            archive_failure_reason=None,
            updated_at=now,
        )
    )


class SqlAlchemyImageArchiveMetadataRepository:
    """PostgreSQL adapter for the archive-published-to-READY transition."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def mark_ready(self, *, metadata: ImageArchiveReadyMetadata) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(image_asset_mark_ready_statement(metadata=metadata))
                return result.rowcount == 1


class SqlAlchemyImageArchiveWorkRepository:
    """PostgreSQL image-archive leases; each method owns a short transaction only."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def claim(
        self, *, image_asset_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedImageArchive | None:
        lease_token = uuid4()
        async with self._database.session() as session:
            async with session.begin():
                row = (
                    (
                        await session.execute(
                            image_archive_claim_statement(
                                image_asset_id=UUID(image_asset_id),
                                now=now,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return None
                result = await session.execute(
                    update(ImageAssetRecord)
                    .where(
                        ImageAssetRecord.id == row["image_asset_id"],
                        ImageAssetRecord.archive_state == "pending",
                        or_(
                            ImageAssetRecord.archive_lease_token.is_(None),
                            ImageAssetRecord.archive_lease_expires_at <= now,
                        ),
                    )
                    .values(
                        archive_lease_token=lease_token,
                        archive_lease_expires_at=now + lease_duration,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    return None
                return ClaimedImageArchive(
                    work_item=ImageArchiveWorkItem(
                        image_asset_id=str(row["image_asset_id"]),
                        source_channel_id=str(row["source_channel_id"]),
                        source_telegram_message_id=int(row["source_telegram_message_id"]),
                        source_asset_id=str(row["source_asset_id"]),
                        source_deleted_at=row["source_deleted_at"],
                    ),
                    lease_token=str(lease_token),
                )

    async def mark_ready(
        self, *, claim: ClaimedImageArchive, metadata: ImageArchiveReadyMetadata
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    image_archive_mark_ready_claim_statement(claim=claim, metadata=metadata)
                )
                return result.rowcount == 1

    async def mark_failed(self, *, claim: ClaimedImageArchive, reason: str, now: datetime) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    image_archive_mark_failed_statement(claim=claim, reason=reason, now=now)
                )
                return result.rowcount == 1

    async def release(self, *, claim: ClaimedImageArchive, now: datetime) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    image_archive_release_statement(claim=claim, now=now)
                )
                return result.rowcount == 1
