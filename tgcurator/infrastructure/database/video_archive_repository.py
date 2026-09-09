from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, or_, select, update

from tgcurator.application.media.archive_wakeups import VIDEO_ARCHIVE_QUEUE
from tgcurator.application.ports.media import (
    ClaimedVideoArchive,
    VideoArchiveReadyMetadata,
    VideoArchiveWorkItem,
)

from .models import DurableWakeupRecord, MessageRecord, VideoAssetRecord, VideoFrameRecord
from .session import AsyncDatabase


def video_archive_claim_statement(*, video_asset_id: UUID, now: datetime):
    return (
        select(
            VideoAssetRecord,
            MessageRecord.source_channel_id,
            MessageRecord.source_deleted_at,
        )
        .join(MessageRecord, MessageRecord.id == VideoAssetRecord.message_id)
        .where(
            VideoAssetRecord.id == video_asset_id,
            VideoAssetRecord.source_telegram_message_id.is_not(None),
            or_(
                VideoAssetRecord.archive_state == "pending",
                and_(
                    VideoAssetRecord.archive_state == "retry_wait",
                    VideoAssetRecord.archive_next_retry_at <= now,
                ),
                and_(
                    VideoAssetRecord.archive_state == "processing",
                    VideoAssetRecord.archive_lease_expires_at <= now,
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    )


def video_archive_complete_wakeup_statement(*, video_asset_id: UUID, now: datetime):
    terminal_video_exists = (
        select(VideoAssetRecord.id)
        .where(
            VideoAssetRecord.id == video_asset_id,
            VideoAssetRecord.archive_state.in_(("ready", "deleted", "failed")),
        )
        .exists()
    )
    return (
        update(DurableWakeupRecord)
        .where(
            DurableWakeupRecord.queue == VIDEO_ARCHIVE_QUEUE,
            DurableWakeupRecord.entity_id == video_asset_id,
            DurableWakeupRecord.status.in_(("pending", "leased")),
            terminal_video_exists,
        )
        .values(
            status="completed",
            lease_token=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
    )


class SqlAlchemyVideoArchiveWorkRepository:
    """PostgreSQL video leases, bounded retries, and atomic READY/frame persistence."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def claim(
        self, *, video_asset_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedVideoArchive | None:
        entity_id = UUID(video_asset_id)
        async with self._database.session() as session:
            async with session.begin():
                claimed = (
                    await session.execute(
                        video_archive_claim_statement(video_asset_id=entity_id, now=now)
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
                return ClaimedVideoArchive(
                    work_item=VideoArchiveWorkItem(
                        video_asset_id=str(row.id),
                        source_channel_id=str(source_channel_id),
                        source_telegram_message_id=int(row.source_telegram_message_id),
                        source_asset_id=str(row.source_asset_id),
                        source_deleted_at=source_deleted_at,
                    ),
                    lease_token=str(lease_token),
                )

    async def mark_ready(
        self, *, claim: ClaimedVideoArchive, metadata: VideoArchiveReadyMetadata
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(VideoAssetRecord)
                    .where(
                        VideoAssetRecord.id == UUID(claim.work_item.video_asset_id),
                        VideoAssetRecord.id == UUID(metadata.video_asset_id),
                        VideoAssetRecord.archive_state == "processing",
                        VideoAssetRecord.archive_lease_token == UUID(claim.lease_token),
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                await session.execute(
                    delete(VideoFrameRecord).where(VideoFrameRecord.video_asset_id == row.id)
                )
                for frame in metadata.frames:
                    session.add(
                        VideoFrameRecord(
                            video_asset_id=row.id,
                            frame_role=frame.frame_role,
                            frame_index=frame.frame_index,
                            timestamp_seconds=frame.timestamp_seconds,
                            storage_backend=frame.storage_backend,
                            storage_key=frame.storage_key,
                            content_type=frame.artifact.content_type,
                            width=frame.artifact.width,
                            height=frame.artifact.height,
                            source_sha256=frame.artifact.source_sha256,
                            archive_sha256=frame.artifact.archive_sha256,
                            perceptual_hash=frame.artifact.perceptual_hash,
                            archive_size_bytes=frame.archive_size_bytes,
                        )
                    )
                row.archive_state = "ready"
                row.archive_next_retry_at = None
                row.archive_lease_token = None
                row.archive_lease_expires_at = None
                row.archive_last_error_code = None
                row.archive_last_error_type = None
                row.archive_last_failure_at = None
                row.source_content_type = metadata.source_content_type
                row.source_size_bytes = metadata.source_size_bytes
                row.source_sha256 = metadata.source_sha256
                row.duration_seconds = metadata.probe.duration_seconds
                row.source_width = metadata.probe.width
                row.source_height = metadata.probe.height
                row.archive_ready_at = metadata.archived_at
                row.archive_deleted_at = None
                row.updated_at = metadata.archived_at
                return True

    async def fail(
        self,
        *,
        claim: ClaimedVideoArchive,
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
                    select(VideoAssetRecord)
                    .where(
                        VideoAssetRecord.id == UUID(claim.work_item.video_asset_id),
                        VideoAssetRecord.archive_state == "processing",
                        VideoAssetRecord.archive_lease_token == UUID(claim.lease_token),
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
                        session=session, entity_id=row.id, retry_at=retry_at, now=now
                    )
                else:
                    row.archive_state = "failed"
                    row.archive_next_retry_at = None
                    await self._set_wakeup_terminal(session=session, entity_id=row.id, now=now)
                return True

    async def complete_wakeup_if_terminal(self, *, video_asset_id: str, now: datetime) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    video_archive_complete_wakeup_statement(
                        video_asset_id=UUID(video_asset_id), now=now
                    )
                )
                return result.rowcount == 1

    @staticmethod
    def _terminalize_exhausted(*, row: VideoAssetRecord, now: datetime) -> None:
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
                DurableWakeupRecord.queue == VIDEO_ARCHIVE_QUEUE,
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
                DurableWakeupRecord.queue == VIDEO_ARCHIVE_QUEUE,
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
