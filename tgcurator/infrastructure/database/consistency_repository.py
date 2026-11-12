from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from tgcurator.domain.archive import ExpectedArchiveBlob

from .models import ImageAssetRecord, VideoFrameRecord
from .session import AsyncDatabase


@dataclass(slots=True)
class SqlAlchemyArchiveConsistencyRepository:
    """Read-only SQLAlchemy repository returning expected archive blob records."""

    database: AsyncDatabase

    async def list_archive_blob_records(
        self,
        *,
        storage_backend: str,
    ) -> tuple[ExpectedArchiveBlob, ...]:
        records: list[ExpectedArchiveBlob] = []
        async with self.database.session() as session:
            img_rows = (
                await session.execute(
                    select(ImageAssetRecord).where(
                        ImageAssetRecord.storage_backend == storage_backend,
                        ImageAssetRecord.storage_key.is_not(None),
                    )
                )
            ).scalars()
            for img in img_rows:
                if img.storage_backend and img.storage_key:
                    records.append(
                        ExpectedArchiveBlob(
                            entity_kind="image_asset",
                            entity_id=str(img.id),
                            storage_backend=img.storage_backend,
                            storage_key=img.storage_key,
                            archive_state=img.archive_state,
                            expected_size_bytes=img.archive_size_bytes,
                            expected_sha256=img.archive_sha256,
                            parent_asset_id=None,
                        )
                    )

            frame_rows = (
                await session.execute(
                    select(VideoFrameRecord).where(
                        VideoFrameRecord.storage_backend == storage_backend,
                    )
                )
            ).scalars()
            for frame in frame_rows:
                records.append(
                    ExpectedArchiveBlob(
                        entity_kind="video_frame",
                        entity_id=str(frame.id),
                        storage_backend=frame.storage_backend,
                        storage_key=frame.storage_key,
                        archive_state=frame.archive_state,
                        expected_size_bytes=frame.archive_size_bytes,
                        expected_sha256=frame.archive_sha256,
                        parent_asset_id=str(frame.video_asset_id),
                    )
                )
        return tuple(records)
