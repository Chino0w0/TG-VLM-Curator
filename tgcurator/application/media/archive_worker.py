from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from uuid import UUID

from tgcurator.application.media.downloads import (
    TelegramMediaUnavailableError,
    TelegramProtectedContentError,
)
from tgcurator.application.media.images import (
    ImageArchiveMetadataPersistenceError,
    ImageArchiveService,
)
from tgcurator.application.ports.media import (
    ClaimedImageArchive,
    ImageArchiveReadyMetadata,
    ImageArchiveWorkRepository,
    ImageNormalizationProfile,
    TelegramMediaDownloader,
    TelegramMediaDownloadRequest,
)
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration


@dataclass(slots=True)
class ImageArchiveWorker:
    """Archive one source image with a short lease and PostgreSQL-owned retries."""

    repository: ImageArchiveWorkRepository
    media_downloader: TelegramMediaDownloader
    archive_service: ImageArchiveService
    normalization_profile: ImageNormalizationProfile
    storage_backend: str
    storage_key_prefix: str = "images"
    lease_duration: timedelta = timedelta(minutes=5)
    failure_retry_delay: timedelta = timedelta(minutes=1)

    def __post_init__(self) -> None:
        ensure_positive_duration(self.lease_duration, field="lease_duration")
        ensure_positive_duration(self.failure_retry_delay, field="failure_retry_delay")
        if not self.storage_backend.strip():
            raise ValueError("storage_backend must not be blank")
        prefix = PurePosixPath(self.storage_key_prefix)
        if (
            not self.storage_key_prefix.strip()
            or any(character == chr(92) for character in self.storage_key_prefix)
            or prefix.is_absolute()
            or any(part in {"", ".", ".."} for part in prefix.parts)
        ):
            raise ValueError("storage_key_prefix must be a safe relative POSIX path")

    async def process(self, *, image_asset_id: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        _require_uuid(image_asset_id, field="image_asset_id")
        claim = await self.repository.claim(
            image_asset_id=image_asset_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if claim is None:
            await self.repository.complete_wakeup_if_terminal(
                image_asset_id=image_asset_id, now=now
            )
            return False
        if claim.work_item.source_deleted_at is not None:
            return await self._fail(
                claim=claim,
                error_code="SOURCE_MESSAGE_DELETED",
                error_type="SourceMessageDeleted",
                retryable=False,
                now=now,
            )

        try:
            content = await self.media_downloader.download(
                request=TelegramMediaDownloadRequest(
                    source_channel_id=claim.work_item.source_channel_id,
                    telegram_message_id=claim.work_item.source_telegram_message_id,
                    source_asset_id=claim.work_item.source_asset_id,
                )
            )
        except TelegramProtectedContentError:
            return await self._fail(
                claim=claim,
                error_code="TELEGRAM_PROTECTED_CONTENT",
                error_type="TelegramProtectedContent",
                retryable=False,
                now=now,
            )
        except TelegramMediaUnavailableError:
            return await self._fail(
                claim=claim,
                error_code="TELEGRAM_MEDIA_UNAVAILABLE",
                error_type="TelegramMediaUnavailable",
                retryable=False,
                now=now,
            )
        except Exception:
            return await self._fail(
                claim=claim,
                error_code="TELEGRAM_DOWNLOAD_FAILED",
                error_type="TelegramDownloadError",
                retryable=True,
                now=now,
            )

        try:
            storage_key = f"{self.storage_key_prefix}/{claim.work_item.image_asset_id}.webp"
            artifact = await self.archive_service.normalize_and_archive(
                content=content,
                storage_key=storage_key,
                profile=self.normalization_profile,
            )
            archive_size_bytes = await self.archive_service.archive_storage.size(key=storage_key)
            if archive_size_bytes != len(artifact.content):
                raise ImageArchiveMetadataPersistenceError(
                    "archive size does not match the normalized image evidence"
                )
            metadata = ImageArchiveReadyMetadata.from_artifact(
                image_asset_id=claim.work_item.image_asset_id,
                storage_backend=self.storage_backend,
                storage_key=storage_key,
                artifact=artifact,
                archive_size_bytes=archive_size_bytes,
                archived_at=now,
            )
            if not await self.repository.mark_ready(claim=claim, metadata=metadata):
                raise ImageArchiveMetadataPersistenceError("image archive claim was lost")
        except Exception:
            return await self._fail(
                claim=claim,
                error_code="IMAGE_ARCHIVE_FAILED",
                error_type="ImageArchiveError",
                retryable=True,
                now=now,
            )

        await self.repository.complete_wakeup_if_terminal(
            image_asset_id=claim.work_item.image_asset_id, now=now
        )
        return True

    async def _fail(
        self,
        *,
        claim: ClaimedImageArchive,
        error_code: str,
        error_type: str,
        retryable: bool,
        now: datetime,
    ) -> bool:
        return await self.repository.fail(
            claim=claim,
            error_code=error_code,
            error_type=error_type,
            retryable=retryable,
            retry_at=now + self.failure_retry_delay,
            now=now,
        )


def _require_uuid(value: str, *, field: str) -> None:
    try:
        UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
