from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tgcurator.application.ports.contracts import ArchiveStorage
from tgcurator.application.ports.media import (
    ImageArchiveMetadataRepository,
    ImageArchiveReadyMetadata,
    ImageNormalizationProfile,
    ImageProcessor,
    NormalizedImageArtifact,
)


class ImageArchiveMetadataPersistenceError(RuntimeError):
    """Storage published bytes but the matching image asset could not become READY."""


@dataclass(slots=True)
class ImageArchiveService:
    """Normalize, atomically archive, then durably record image READY metadata.

    Archive I/O intentionally happens before the short metadata transaction. If a process crashes
    between those steps, the immutable object can be written again safely and the still-pending
    database asset can be reconciled later; a READY row is never written before storage succeeds.
    """

    image_processor: ImageProcessor
    archive_storage: ArchiveStorage

    async def normalize_and_archive(
        self,
        *,
        content: bytes,
        storage_key: str,
        profile: ImageNormalizationProfile,
    ) -> NormalizedImageArtifact:
        artifact = await self.image_processor.normalize(content=content, profile=profile)
        await self.archive_storage.put(
            key=storage_key,
            content=artifact.content,
            content_type=artifact.content_type,
        )
        return artifact

    async def normalize_archive_and_mark_ready(
        self,
        *,
        image_asset_id: str,
        storage_backend: str,
        storage_key: str,
        content: bytes,
        profile: ImageNormalizationProfile,
        archived_at: datetime,
        metadata_repository: ImageArchiveMetadataRepository,
    ) -> NormalizedImageArtifact:
        artifact = await self.normalize_and_archive(
            content=content,
            storage_key=storage_key,
            profile=profile,
        )
        archive_size_bytes = await self.archive_storage.size(key=storage_key)
        if archive_size_bytes != len(artifact.content):
            raise ImageArchiveMetadataPersistenceError(
                "archive size does not match the normalized image evidence"
            )
        metadata = ImageArchiveReadyMetadata.from_artifact(
            image_asset_id=image_asset_id,
            storage_backend=storage_backend,
            storage_key=storage_key,
            artifact=artifact,
            archive_size_bytes=archive_size_bytes,
            archived_at=archived_at,
        )
        if not await metadata_repository.mark_ready(metadata=metadata):
            raise ImageArchiveMetadataPersistenceError(
                "image asset is missing, deleted, or conflicts with immutable archive metadata"
            )
        return artifact
