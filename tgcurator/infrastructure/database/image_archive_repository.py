from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, case, func, or_, update
from sqlalchemy.sql.dml import Update

from tgcurator.application.ports.media import ImageArchiveReadyMetadata

from .models import ImageAssetRecord
from .session import AsyncDatabase


def image_asset_mark_ready_statement(*, metadata: ImageArchiveReadyMetadata) -> Update:
    """Move a pending image to READY, while allowing an exact immutable replay only."""

    pending = ImageAssetRecord.archive_state == "pending"
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


class SqlAlchemyImageArchiveMetadataRepository:
    """PostgreSQL adapter for the archive-published-to-READY transition."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def mark_ready(self, *, metadata: ImageArchiveReadyMetadata) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(image_asset_mark_ready_statement(metadata=metadata))
                return result.rowcount == 1
