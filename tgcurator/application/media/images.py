from __future__ import annotations

from dataclasses import dataclass

from tgcurator.application.ports.contracts import ArchiveStorage
from tgcurator.application.ports.media import (
    ImageNormalizationProfile,
    ImageProcessor,
    NormalizedImageArtifact,
)


@dataclass(slots=True)
class ImageArchiveService:
    """Normalize and write image evidence before a separate DB transaction records readiness."""

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
