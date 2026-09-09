from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from tgcurator.application.media import (
    VideoArchiveWorker,
    VideoFrameEvidence,
    VideoSamplingProfile,
    VideoSamplingResult,
)
from tgcurator.application.media.downloads import TelegramProtectedContentError
from tgcurator.application.ports.media import (
    ClaimedVideoArchive,
    ImageNormalizationProfile,
    NormalizedImageArtifact,
    TelegramMediaDownloadRequest,
    VideoArchiveReadyMetadata,
    VideoArchiveWorkItem,
    VideoProbeMetadata,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class VideoArchiveWorkerTests(unittest.TestCase):
    def test_publishes_all_frames_before_atomic_ready_transition(self) -> None:
        repository = _Repository(_claim())
        storage = _Storage()
        worker = _worker(repository=repository, storage=storage)

        self.assertTrue(asyncio.run(worker.process(video_asset_id=repository.asset_id, now=NOW)))

        self.assertEqual(
            storage.keys,
            [
                f"videos/{repository.asset_id}/cover.webp",
                f"videos/{repository.asset_id}/representative-00.webp",
            ],
        )
        self.assertEqual(len(repository.ready), 1)
        metadata = repository.ready[0]
        self.assertEqual(metadata.source_size_bytes, len(b"video-source"))
        self.assertEqual(metadata.probe.width, 1920)
        self.assertEqual(
            tuple(frame.frame_role for frame in metadata.frames), ("cover", "representative")
        )
        self.assertEqual(repository.events, ["ready", "completed"])

    def test_protected_content_is_terminal_without_sampling(self) -> None:
        repository = _Repository(_claim())
        downloader = _Downloader(error=TelegramProtectedContentError())
        sampling = _SamplingService()
        worker = _worker(repository=repository, downloader=downloader, sampling=sampling)

        self.assertTrue(asyncio.run(worker.process(video_asset_id=repository.asset_id, now=NOW)))
        self.assertEqual(
            repository.failures[0][:3],
            ("TELEGRAM_PROTECTED_CONTENT", "TelegramProtectedContent", False),
        )
        self.assertEqual(sampling.calls, [])

    def test_frame_publication_failure_is_retryable_and_never_marks_ready(self) -> None:
        repository = _Repository(_claim())
        worker = _worker(repository=repository, storage=_Storage(size_delta=1))

        self.assertTrue(asyncio.run(worker.process(video_asset_id=repository.asset_id, now=NOW)))
        self.assertEqual(
            repository.failures[0][:3], ("VIDEO_ARCHIVE_FAILED", "VideoArchiveError", True)
        )
        self.assertEqual(repository.ready, [])

    def test_unclaimable_terminal_work_repairs_wakeup_without_external_io(self) -> None:
        repository = _Repository(_claim())
        repository.claim_result = None
        downloader = _Downloader(content=b"video-source")
        worker = _worker(repository=repository, downloader=downloader)

        self.assertFalse(asyncio.run(worker.process(video_asset_id=repository.asset_id, now=NOW)))
        self.assertEqual(downloader.requests, [])
        self.assertEqual(repository.events, ["completed"])


class _Repository:
    def __init__(self, claim: ClaimedVideoArchive) -> None:
        self.claim_result = claim
        self.asset_id = claim.work_item.video_asset_id
        self.ready: list[VideoArchiveReadyMetadata] = []
        self.failures: list[tuple[str, str, bool, datetime, datetime]] = []
        self.events: list[str] = []

    async def claim(self, **_: object) -> ClaimedVideoArchive | None:
        return self.claim_result

    async def mark_ready(
        self, *, claim: ClaimedVideoArchive, metadata: VideoArchiveReadyMetadata
    ) -> bool:
        self.ready.append(metadata)
        self.events.append("ready")
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
        self.failures.append((error_code, error_type, retryable, retry_at, now))
        return True

    async def complete_wakeup_if_terminal(self, *, video_asset_id: str, now: datetime) -> bool:
        self.events.append("completed")
        return True


class _Downloader:
    def __init__(self, *, content: bytes | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.requests: list[TelegramMediaDownloadRequest] = []

    async def download(self, *, request: TelegramMediaDownloadRequest) -> bytes:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.content or b""


class _Probe:
    async def probe(self, *, content: bytes) -> VideoProbeMetadata:
        return VideoProbeMetadata(
            duration_seconds=12,
            width=1920,
            height=1080,
            content_type="video/mp4",
        )


class _SamplingService:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    async def sample(self, *, content: bytes, **_: object) -> VideoSamplingResult:
        self.calls.append(content)
        return VideoSamplingResult(
            duration_seconds=12,
            cover=VideoFrameEvidence(timestamp_seconds=0, artifact=_artifact("0" * 16)),
            representative_frames=(
                VideoFrameEvidence(timestamp_seconds=6, artifact=_artifact("f" * 16)),
            ),
        )


class _Storage:
    def __init__(self, *, size_delta: int = 0) -> None:
        self.objects: dict[str, bytes] = {}
        self.keys: list[str] = []
        self.size_delta = size_delta

    async def put(self, *, key: str, content: bytes, content_type: str) -> int:
        self.keys.append(key)
        self.objects[key] = content
        return len(content)

    async def size(self, *, key: str) -> int:
        return len(self.objects[key]) + self.size_delta


def _artifact(perceptual_hash: str) -> NormalizedImageArtifact:
    return NormalizedImageArtifact(
        content=b"webp-" + perceptual_hash.encode(),
        content_type="image/webp",
        width=100,
        height=80,
        source_sha256="a" * 64,
        archive_sha256="b" * 64,
        perceptual_hash=perceptual_hash,
    )


def _claim() -> ClaimedVideoArchive:
    return ClaimedVideoArchive(
        work_item=VideoArchiveWorkItem(
            video_asset_id=str(uuid4()),
            source_channel_id=str(uuid4()),
            source_telegram_message_id=17,
            source_asset_id="video-17",
        ),
        lease_token=str(uuid4()),
    )


def _worker(
    *,
    repository: _Repository,
    downloader: _Downloader | None = None,
    sampling: _SamplingService | None = None,
    storage: _Storage | None = None,
) -> VideoArchiveWorker:
    return VideoArchiveWorker(
        repository=repository,
        media_downloader=downloader or _Downloader(content=b"video-source"),
        metadata_probe=_Probe(),
        sampling_service=sampling or _SamplingService(),
        archive_storage=storage or _Storage(),
        sampling_profile=VideoSamplingProfile(),
        image_profile=ImageNormalizationProfile(max_side_pixels=1280, quality=82),
        storage_backend="local-volume",
    )


if __name__ == "__main__":
    unittest.main()
