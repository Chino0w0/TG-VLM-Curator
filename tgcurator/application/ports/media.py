from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ImageNormalizationProfile:
    """Frozen image-output parameters supplied by a published media profile later on."""

    max_side_pixels: int
    quality: int

    def __post_init__(self) -> None:
        if self.max_side_pixels <= 0:
            raise ValueError("max_side_pixels must be greater than zero")
        if not 1 <= self.quality <= 100:
            raise ValueError("quality must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class NormalizedImageArtifact:
    """Visual evidence produced before archive persistence; never contains a host path."""

    content: bytes
    content_type: str
    width: int
    height: int
    source_sha256: str
    archive_sha256: str
    perceptual_hash: str

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("normalized image content must not be empty")
        if self.content_type != "image/webp":
            raise ValueError("normalized images must use image/webp")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("normalized image dimensions must be positive")
        for field, value in (
            ("source_sha256", self.source_sha256),
            ("archive_sha256", self.archive_sha256),
            ("perceptual_hash", self.perceptual_hash),
        ):
            if not value or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{field} must be lowercase hexadecimal")
        if len(self.source_sha256) != 64 or len(self.archive_sha256) != 64:
            raise ValueError("SHA-256 values must have 64 hexadecimal characters")
        if len(self.perceptual_hash) != 16:
            raise ValueError("perceptual_hash must have 16 hexadecimal characters")


class ImageProcessor(Protocol):
    """CPU-bound image conversion boundary; implementation details stay in infrastructure."""

    async def normalize(
        self, *, content: bytes, profile: ImageNormalizationProfile
    ) -> NormalizedImageArtifact:
        """Decode, orient, resize, normalize, and visually fingerprint one source image."""
