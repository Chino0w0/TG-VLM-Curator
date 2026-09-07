from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID


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


@dataclass(frozen=True, slots=True)
class ImageArchiveReadyMetadata:
    """Verified immutable archive facts to persist only after storage publication succeeds."""

    image_asset_id: str
    storage_backend: str
    storage_key: str
    content_type: str
    width: int
    height: int
    source_sha256: str
    archive_sha256: str
    perceptual_hash: str
    archive_size_bytes: int
    archived_at: datetime

    @classmethod
    def from_artifact(
        cls,
        *,
        image_asset_id: str,
        storage_backend: str,
        storage_key: str,
        artifact: NormalizedImageArtifact,
        archive_size_bytes: int,
        archived_at: datetime,
    ) -> ImageArchiveReadyMetadata:
        return cls(
            image_asset_id=image_asset_id,
            storage_backend=storage_backend,
            storage_key=storage_key,
            content_type=artifact.content_type,
            width=artifact.width,
            height=artifact.height,
            source_sha256=artifact.source_sha256,
            archive_sha256=artifact.archive_sha256,
            perceptual_hash=artifact.perceptual_hash,
            archive_size_bytes=archive_size_bytes,
            archived_at=archived_at,
        )

    def __post_init__(self) -> None:
        try:
            UUID(self.image_asset_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("image_asset_id must be a UUID") from error
        if not isinstance(self.storage_backend, str) or not self.storage_backend.strip():
            raise ValueError("storage_backend must not be blank")
        if not isinstance(self.storage_key, str):
            raise ValueError("storage_key must be a relative POSIX key")
        normalized_key = PurePosixPath(self.storage_key)
        if (
            not self.storage_key.strip()
            or "\\" in self.storage_key
            or normalized_key.is_absolute()
            or normalized_key == PurePosixPath(".")
            or any(part == ".." for part in normalized_key.parts)
            or any(":" in part for part in normalized_key.parts)
        ):
            raise ValueError("storage_key must be a safe relative POSIX key")
        if self.content_type != "image/webp":
            raise ValueError("archived images must use image/webp")
        if self.width <= 0 or self.height <= 0 or self.archive_size_bytes <= 0:
            raise ValueError("image dimensions and archive_size_bytes must be positive")
        for field, value, expected_length in (
            ("source_sha256", self.source_sha256, 64),
            ("archive_sha256", self.archive_sha256, 64),
            ("perceptual_hash", self.perceptual_hash, 16),
        ):
            if (
                not isinstance(value, str)
                or len(value) != expected_length
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(
                    f"{field} must be {expected_length} lowercase hexadecimal characters"
                )
        if not isinstance(self.archived_at, datetime) or (
            self.archived_at.tzinfo is None or self.archived_at.utcoffset() is None
        ):
            raise ValueError("archived_at must be timezone-aware")


class ImageArchiveMetadataRepository(Protocol):
    """Persist the archive READY transition after storage publication succeeds."""

    async def mark_ready(self, *, metadata: ImageArchiveReadyMetadata) -> bool:
        """Persist matching READY metadata, or return False for a missing/conflicting asset."""


@dataclass(frozen=True, slots=True)
class TelegramMediaDownloadRequest:
    """Stable Telegram source reference for one durable media asset download."""

    source_channel_id: str
    telegram_message_id: int
    source_asset_id: str

    def __post_init__(self) -> None:
        try:
            UUID(self.source_channel_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("source_channel_id must be a UUID") from error
        if (
            not isinstance(self.telegram_message_id, int)
            or isinstance(self.telegram_message_id, bool)
            or self.telegram_message_id <= 0
        ):
            raise ValueError("telegram_message_id must be a positive integer")
        if not isinstance(self.source_asset_id, str) or not self.source_asset_id.strip():
            raise ValueError("source_asset_id must not be blank")


class TelegramMediaDownloader(Protocol):
    """Download one source-media object without exposing a Telegram SDK to application callers."""

    async def download(self, *, request: TelegramMediaDownloadRequest) -> bytes:
        """Return non-empty media bytes or raise a typed application error."""


@dataclass(frozen=True, slots=True)
class ImageArchiveWorkItem:
    """Stable database facts needed to retrieve one exact Telegram image component."""

    image_asset_id: str
    source_channel_id: str
    source_telegram_message_id: int
    source_asset_id: str
    source_deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("image_asset_id", self.image_asset_id),
            ("source_channel_id", self.source_channel_id),
        ):
            try:
                UUID(value)
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError(f"{field} must be a UUID") from error
        if (
            not isinstance(self.source_telegram_message_id, int)
            or isinstance(self.source_telegram_message_id, bool)
            or self.source_telegram_message_id <= 0
        ):
            raise ValueError("source_telegram_message_id must be a positive integer")
        if not isinstance(self.source_asset_id, str) or not self.source_asset_id.strip():
            raise ValueError("source_asset_id must not be blank")
        if self.source_deleted_at is not None and (
            self.source_deleted_at.tzinfo is None or self.source_deleted_at.utcoffset() is None
        ):
            raise ValueError("source_deleted_at must be timezone-aware when present")


@dataclass(frozen=True, slots=True)
class ClaimedImageArchive:
    """A finite archive lease; external effects intentionally occur after its DB transaction."""

    work_item: ImageArchiveWorkItem
    lease_token: str

    def __post_init__(self) -> None:
        try:
            UUID(self.lease_token)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("lease_token must be a UUID") from error


class ImageArchiveWorkRepository(Protocol):
    """Short-transaction state changes for an at-least-once image archive worker."""

    async def claim(
        self, *, image_asset_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedImageArchive | None:
        """Lease a pending or expired archive asset, without holding a transaction for I/O."""

    async def mark_ready(
        self, *, claim: ClaimedImageArchive, metadata: ImageArchiveReadyMetadata
    ) -> bool:
        """Commit immutable READY metadata only if this worker still owns the claim."""

    async def mark_failed(self, *, claim: ClaimedImageArchive, reason: str, now: datetime) -> bool:
        """Commit a known terminal retrieval failure only if this worker still owns the claim."""

    async def release(self, *, claim: ClaimedImageArchive, now: datetime) -> bool:
        """Return a retryable failed claim to pending without storing raw exception details."""
