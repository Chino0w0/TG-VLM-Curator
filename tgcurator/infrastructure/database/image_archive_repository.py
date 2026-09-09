from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.sql.dml import Update

from tgcurator.application.media.archive_wakeups import IMAGE_ARCHIVE_QUEUE
from tgcurator.application.ports.media import (
    ClaimedImageArchive,
    ImageArchiveReadyMetadata,
    ImageArchiveWorkItem,
)

from .models import DurableWakeupRecord, ImageAssetRecord, MessageRecord
from .session import AsyncDatabase


def image_asset_mark_ready_statement(*, metadata: ImageArchiveReadyMetadata) -> Update:
    """Move an unleased pending image to READY, while allowing an exact immutable replay."""

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
            archive_next_retry_at=None,
            archive_lease_token=None,
            archive_lease_expires_at=None,
            archive_last_error_code=None,
            archive_last_error_type=None,
            archive_last_failure_at=None,
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
            updated_at=case((pending, metadata.archived_at), else_=ImageAssetRecord.updated_at),
        )
    )


def image_archive_claim_statement(*, image_asset_id: UUID, now: datetime):
    """Lock one pending, due retry, or expired processing image without blocking peers."""

    return (
        select(
            ImageAssetRecord,
            MessageRecord.source_channel_id,
            MessageRecord.source_deleted_at,
        )
        .join(MessageRecord, MessageRecord.id == ImageAssetRecord.message_id)
        .where(
            ImageAssetRecord.id == image_asset_id,
            ImageAssetRecord.source_telegram_message_id.is_not(None),
            or_(
                ImageAssetRecord.archive_state == "pending",
                and_(
                    ImageAssetRecord.archive_state == "retry_wait",
                    ImageAssetRecord.archive_next_retry_at <= now,
                ),
                and_(
                    ImageAssetRecord.archive_state == "processing",
                    ImageAssetRecord.archive_lease_expires_at <= now,
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    )


def image_archive_mark_ready_claim_statement(
    *, claim: ClaimedImageArchive, metadata: ImageArchiveReadyMetadata
) -> Update:
    """Publish READY metadata only when the supplied processing lease is still current."""

    return (
        update(ImageAssetRecord)
        .where(
            ImageAssetRecord.id == UUID(claim.work_item.image_asset_id),
            ImageAssetRecord.id == UUID(metadata.image_asset_id),
            ImageAssetRecord.archive_state == "processing",
            ImageAssetRecord.archive_lease_token == UUID(claim.lease_token),
        )
        .values(
            archive_state="ready",
            archive_next_retry_at=None,
            archive_lease_token=None,
            archive_lease_expires_at=None,
            archive_last_error_code=None,
            archive_last_error_type=None,
            archive_last_failure_at=None,
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
            updated_at=metadata.archived_at,
        )
    )


def image_archive_complete_wakeup_statement(*, image_asset_id: UUID, now: datetime) -> Update:
    """Complete a durable image wake-up only after its archive has a terminal state."""

    terminal_image_exists = (
        select(ImageAssetRecord.id)
        .where(
            ImageAssetRecord.id == image_asset_id,
            ImageAssetRecord.archive_state.in_(("ready", "deleted", "failed")),
        )
        .exists()
    )
    return (
        update(DurableWakeupRecord)
        .where(
            DurableWakeupRecord.queue == IMAGE_ARCHIVE_QUEUE,
            DurableWakeupRecord.entity_id == image_asset_id,
            DurableWakeupRecord.status.in_(("pending", "leased")),
            terminal_image_exists,
        )
        .values(
            status="completed",
            lease_token=None,
            lease_expires_at=None,
            completed_at=now,
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
    """PostgreSQL image leases with bounded retries and durable wake-up repair."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def claim(
        self, *, image_asset_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedImageArchive | None:
        entity_id = UUID(image_asset_id)
        async with self._database.session() as session:
            async with session.begin():
                claimed = (
                    await session.execute(
                        image_archive_claim_statement(image_asset_id=entity_id, now=now)
                    )
                ).one_or_none()
                if claimed is None:
                    return None
                row, source_channel_id, source_deleted_at = claimed
                if row.archive_attempt_count >= row.archive_max_attempts:
                    self._terminalize_exhausted(row=row, now=now)
                    await self._set_wakeup_terminal(session=session, entity_id=row.id, now=now)
                    return None

                lease_token = uuid4()
                row.archive_state = "processing"
                row.archive_attempt_count += 1
                row.archive_next_retry_at = None
                row.archive_lease_token = lease_token
                row.archive_lease_expires_at = now + lease_duration
                row.updated_at = now
                return ClaimedImageArchive(
                    work_item=ImageArchiveWorkItem(
                        image_asset_id=str(row.id),
                        source_channel_id=str(source_channel_id),
                        source_telegram_message_id=int(row.source_telegram_message_id),
                        source_asset_id=str(row.source_asset_id),
                        source_deleted_at=source_deleted_at,
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

    async def fail(
        self,
        *,
        claim: ClaimedImageArchive,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        normalized_code = self._safe_error_label(error_code, field="error_code", max_length=64)
        normalized_type = self._safe_error_label(error_type, field="error_type", max_length=128)
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ImageAssetRecord)
                    .where(
                        ImageAssetRecord.id == UUID(claim.work_item.image_asset_id),
                        ImageAssetRecord.archive_state == "processing",
                        ImageAssetRecord.archive_lease_token == UUID(claim.lease_token),
                    )
                    .with_for_update()
                )
                if row is None:
                    return False

                row.archive_last_error_code = normalized_code
                row.archive_last_error_type = normalized_type
                row.archive_last_failure_at = now
                row.archive_lease_token = None
                row.archive_lease_expires_at = None
                row.updated_at = now
                if retryable and row.archive_attempt_count < row.archive_max_attempts:
                    row.archive_state = "retry_wait"
                    row.archive_next_retry_at = retry_at
                    await self._reschedule_wakeup(
                        session=session,
                        entity_id=row.id,
                        retry_at=retry_at,
                        now=now,
                    )
                else:
                    row.archive_state = "failed"
                    row.archive_next_retry_at = None
                    await self._set_wakeup_terminal(session=session, entity_id=row.id, now=now)
                return True

    async def complete_wakeup_if_terminal(self, *, image_asset_id: str, now: datetime) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    image_archive_complete_wakeup_statement(
                        image_asset_id=UUID(image_asset_id), now=now
                    )
                )
                return result.rowcount == 1

    @staticmethod
    def _terminalize_exhausted(*, row: ImageAssetRecord, now: datetime) -> None:
        row.archive_state = "failed"
        row.archive_next_retry_at = None
        row.archive_lease_token = None
        row.archive_lease_expires_at = None
        row.archive_last_error_code = "ARCHIVE_ATTEMPTS_EXHAUSTED"
        row.archive_last_error_type = "ArchiveAttemptsExhausted"
        row.archive_last_failure_at = now
        row.updated_at = now

    @staticmethod
    async def _reschedule_wakeup(
        *, session, entity_id: UUID, retry_at: datetime, now: datetime
    ) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == IMAGE_ARCHIVE_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
                DurableWakeupRecord.status.in_(("pending", "leased")),
            )
            .values(
                status="pending",
                next_attempt_at=retry_at,
                lease_token=None,
                lease_expires_at=None,
                completed_at=None,
                updated_at=now,
            )
        )

    @staticmethod
    async def _set_wakeup_terminal(*, session, entity_id: UUID, now: datetime) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == IMAGE_ARCHIVE_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
                DurableWakeupRecord.status.in_(("pending", "leased")),
            )
            .values(
                status="completed",
                lease_token=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    def _safe_error_label(value: str, *, field: str, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must not be blank")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise ValueError(f"{field} must be at most {max_length} characters")
        return normalized
