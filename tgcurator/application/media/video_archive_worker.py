from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import UUID

from tgcurator.application.media.downloads import (
    TelegramMediaUnavailableError,
    TelegramProtectedContentError,
)
from tgcurator.application.media.videos import (
    VideoFrameSamplingService,
    VideoSamplingProfile,
)
from tgcurator.application.ports.contracts import ArchiveStorage
from tgcurator.application.ports.media import (
    ClaimedVideoArchive,
    ImageNormalizationProfile,
    TelegramMediaDownloader,
    TelegramMediaDownloadRequest,
    VideoArchiveReadyMetadata,
    VideoArchiveWorkRepository,
    VideoFrameReadyMetadata,
    VideoMetadataProbe,
)
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration


@dataclass(slots=True)
class VideoArchiveWorker:
    """Archive bounded video visual evidence under a short PostgreSQL lease."""

    repository: VideoArchiveWorkRepository
    media_downloader: TelegramMediaDownloader
    metadata_probe: VideoMetadataProbe
    sampling_service: VideoFrameSamplingService
    archive_storage: ArchiveStorage
    sampling_profile: VideoSamplingProfile
    image_profile: ImageNormalizationProfile
    storage_backend: str
    storage_key_prefix: str = "videos"
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
            or "\\" in self.storage_key_prefix
            or prefix.is_absolute()
            or any(part in {"", ".", ".."} for part in prefix.parts)
        ):
            raise ValueError("storage_key_prefix must be a safe relative POSIX path")

    async def process(self, *, video_asset_id: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        _require_uuid(video_asset_id, field="video_asset_id")
        claim = await self.repository.claim(
            video_asset_id=video_asset_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if claim is None:
            await self.repository.complete_wakeup_if_terminal(
                video_asset_id=video_asset_id, now=now
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
            probe = await self.metadata_probe.probe(content=content)
            sampled = await self.sampling_service.sample(
                content=content,
                sampling_profile=self.sampling_profile,
                image_profile=self.image_profile,
                duration_seconds=probe.duration_seconds,
            )
            frames: list[VideoFrameReadyMetadata] = []
            evidence = []
            if sampled.cover is not None:
                evidence.append(("cover", 0, sampled.cover))
            evidence.extend(
                ("representative", index, frame)
                for index, frame in enumerate(sampled.representative_frames)
            )
            if not evidence:
                raise RuntimeError("video sampling produced no durable evidence")
            for role, index, frame in evidence:
                storage_key = self._storage_key(
                    video_asset_id=claim.work_item.video_asset_id,
                    role=role,
                    index=index,
                )
                await self.archive_storage.put(
                    key=storage_key,
                    content=frame.artifact.content,
                    content_type=frame.artifact.content_type,
                )
                archive_size = await self.archive_storage.size(key=storage_key)
                frames.append(
                    VideoFrameReadyMetadata(
                        frame_role=role,
                        frame_index=index,
                        timestamp_seconds=frame.timestamp_seconds,
                        storage_backend=self.storage_backend,
                        storage_key=storage_key,
                        artifact=frame.artifact,
                        archive_size_bytes=archive_size,
                    )
                )
            metadata = VideoArchiveReadyMetadata(
                video_asset_id=claim.work_item.video_asset_id,
                source_content_type=probe.content_type,
                source_size_bytes=len(content),
                source_sha256=sha256(content).hexdigest(),
                probe=probe,
                frames=tuple(frames),
                archived_at=now,
            )
            if not await self.repository.mark_ready(claim=claim, metadata=metadata):
                raise RuntimeError("video archive claim was lost")
        except Exception:
            return await self._fail(
                claim=claim,
                error_code="VIDEO_ARCHIVE_FAILED",
                error_type="VideoArchiveError",
                retryable=True,
                now=now,
            )

        await self.repository.complete_wakeup_if_terminal(
            video_asset_id=claim.work_item.video_asset_id, now=now
        )
        return True

    def _storage_key(self, *, video_asset_id: str, role: str, index: int) -> str:
        if role == "cover":
            return f"{self.storage_key_prefix}/{video_asset_id}/cover.webp"
        return f"{self.storage_key_prefix}/{video_asset_id}/representative-{index:02d}.webp"

    async def _fail(
        self,
        *,
        claim: ClaimedVideoArchive,
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
