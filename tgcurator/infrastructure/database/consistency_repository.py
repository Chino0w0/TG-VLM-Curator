from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from tgcurator.domain.archive import ExpectedArchiveBlob

from .models import ImageAssetRecord, VideoFrameRecord
from .session import AsyncDatabase


@dataclass(slots=True)
class SqlAlchemyArchiveConsistencyRepository:
    """Read-only SQLAlchemy repository returning expected archive blob records."""

    database: AsyncDatabase
    batch_size: int = 1000

    async def list_archive_blob_records(
        self,
        *,
        storage_backend: str,
    ) -> tuple[ExpectedArchiveBlob, ...]:
        records: list[ExpectedArchiveBlob] = []
        page_size = max(1, int(self.batch_size))
        async with self.database.session() as session:
            last_img_id: UUID | None = None
            while True:
                stmt = select(
                    ImageAssetRecord.id,
                    ImageAssetRecord.storage_backend,
                    ImageAssetRecord.storage_key,
                    ImageAssetRecord.archive_state,
                    ImageAssetRecord.archive_size_bytes,
                    ImageAssetRecord.archive_sha256,
                ).where(
                    ImageAssetRecord.storage_backend == storage_backend,
                    ImageAssetRecord.storage_key.is_not(None),
                )
                if last_img_id is not None:
                    stmt = stmt.where(ImageAssetRecord.id > last_img_id)
                batch = (
                    await session.execute(stmt.order_by(ImageAssetRecord.id).limit(page_size))
                ).all()
                if not batch:
                    break
                for (
                    img_id,
                    img_backend,
                    img_key,
                    img_state,
                    img_size,
                    img_sha256,
                ) in batch:
                    last_img_id = img_id
                    if img_backend and img_key:
                        records.append(
                            ExpectedArchiveBlob(
                                entity_kind="image_asset",
                                entity_id=str(img_id),
                                storage_backend=img_backend,
                                storage_key=img_key,
                                archive_state=img_state,
                                expected_size_bytes=img_size,
                                expected_sha256=img_sha256,
                                parent_asset_id=None,
                            )
                        )
                if len(batch) < page_size:
                    break

            last_frame_id: UUID | None = None
            while True:
                frame_stmt = select(
                    VideoFrameRecord.id,
                    VideoFrameRecord.storage_backend,
                    VideoFrameRecord.storage_key,
                    VideoFrameRecord.archive_state,
                    VideoFrameRecord.archive_size_bytes,
                    VideoFrameRecord.archive_sha256,
                    VideoFrameRecord.video_asset_id,
                ).where(
                    VideoFrameRecord.storage_backend == storage_backend,
                )
                if last_frame_id is not None:
                    frame_stmt = frame_stmt.where(VideoFrameRecord.id > last_frame_id)
                frame_batch = (
                    await session.execute(frame_stmt.order_by(VideoFrameRecord.id).limit(page_size))
                ).all()
                if not frame_batch:
                    break
                for (
                    frame_id,
                    frame_backend,
                    frame_key,
                    frame_state,
                    frame_size,
                    frame_sha256,
                    video_asset_id,
                ) in frame_batch:
                    last_frame_id = frame_id
                    records.append(
                        ExpectedArchiveBlob(
                            entity_kind="video_frame",
                            entity_id=str(frame_id),
                            storage_backend=frame_backend,
                            storage_key=frame_key,
                            archive_state=frame_state,
                            expected_size_bytes=frame_size,
                            expected_sha256=frame_sha256,
                            parent_asset_id=str(video_asset_id),
                        )
                    )
                if len(frame_batch) < page_size:
                    break
        return tuple(records)
