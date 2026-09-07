from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from tgcurator.application.media import ImageArchiveService, ImageArchiveWorker
from tgcurator.application.media.downloads import (
    TelegramMediaUnavailableError,
    TelegramProtectedContentError,
)
from tgcurator.application.ports.media import (
    ClaimedImageArchive,
    ImageArchiveReadyMetadata,
    ImageArchiveWorkItem,
    ImageNormalizationProfile,
    NormalizedImageArtifact,
    TelegramMediaDownloadRequest,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class ImageArchiveWorkerTests(unittest.TestCase):
    def test_archives_claimed_media_then_records_ready_metadata(self) -> None:
        repository = _Repository(_claim())
        downloader = _Downloader(content=b"source")
        storage = _Storage()
        worker = _worker(repository=repository, downloader=downloader, storage=storage)

        self.assertTrue(asyncio.run(worker.process(image_asset_id=repository.asset_id, now=NOW)))
        self.assertEqual(downloader.requests[0].telegram_message_id, 17)
        self.assertEqual(storage.keys, [f"images/{repository.asset_id}.webp"])
        self.assertEqual(len(repository.ready), 1)
        self.assertEqual(repository.failed, [])
        self.assertEqual(repository.released, [])
        self.assertEqual(repository.completed_wakeups, [repository.asset_id])

    def test_marks_terminal_protected_and_unavailable_sources_without_archiving(self) -> None:
        for error, reason in (
            (TelegramProtectedContentError(), "protected_content"),
            (TelegramMediaUnavailableError(), "media_unavailable"),
        ):
            with self.subTest(reason=reason):
                repository = _Repository(_claim())
                worker = _worker(
                    repository=repository,
                    downloader=_Downloader(error=error),
                    storage=_Storage(),
                )

                self.assertTrue(
                    asyncio.run(worker.process(image_asset_id=repository.asset_id, now=NOW))
                )
                self.assertEqual(repository.failed, [reason])
                self.assertEqual(repository.ready, [])
                self.assertEqual(repository.released, [])
                self.assertEqual(repository.completed_wakeups, [repository.asset_id])

    def test_releases_retryable_download_failure_and_reraises(self) -> None:
        repository = _Repository(_claim())
        worker = _worker(
            repository=repository,
            downloader=_Downloader(error=RuntimeError("network unavailable")),
            storage=_Storage(),
        )

        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            asyncio.run(worker.process(image_asset_id=repository.asset_id, now=NOW))
        self.assertEqual(repository.released, [repository.asset_id])
        self.assertEqual(repository.failed, [])
        self.assertEqual(repository.completed_wakeups, [])

    def test_never_downloads_a_source_message_already_recorded_as_deleted(self) -> None:
        repository = _Repository(_claim(source_deleted_at=NOW))
        downloader = _Downloader(content=b"source")
        worker = _worker(repository=repository, downloader=downloader, storage=_Storage())

        self.assertTrue(asyncio.run(worker.process(image_asset_id=repository.asset_id, now=NOW)))
        self.assertEqual(downloader.requests, [])
        self.assertEqual(repository.failed, ["source_message_deleted"])
        self.assertEqual(repository.completed_wakeups, [repository.asset_id])

    def test_completes_an_existing_terminal_wakeup_when_an_asset_cannot_be_claimed(self) -> None:
        repository = _Repository(_claim())
        repository.claim_result = None
        worker = _worker(
            repository=repository, downloader=_Downloader(content=b"source"), storage=_Storage()
        )

        self.assertFalse(asyncio.run(worker.process(image_asset_id=repository.asset_id, now=NOW)))
        self.assertEqual(repository.completed_wakeups, [repository.asset_id])


class _Repository:
    def __init__(self, claim: ClaimedImageArchive) -> None:
        self.claim_result = claim
        self.asset_id = claim.work_item.image_asset_id
        self.ready: list[ImageArchiveReadyMetadata] = []
        self.failed: list[str] = []
        self.released: list[str] = []
        self.completed_wakeups: list[str] = []

    async def claim(self, **_: object) -> ClaimedImageArchive | None:
        return self.claim_result

    async def mark_ready(
        self, *, claim: ClaimedImageArchive, metadata: ImageArchiveReadyMetadata
    ) -> bool:
        self.ready.append(metadata)
        return True

    async def mark_failed(self, *, claim: ClaimedImageArchive, reason: str, now: datetime) -> bool:
        self.failed.append(reason)
        return True

    async def release(self, *, claim: ClaimedImageArchive, now: datetime) -> bool:
        self.released.append(claim.work_item.image_asset_id)
        return True

    async def complete_wakeup_if_terminal(self, *, image_asset_id: str, now: datetime) -> bool:
        self.completed_wakeups.append(image_asset_id)
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
        assert self.content is not None
        return self.content


class _Processor:
    async def normalize(
        self, *, content: bytes, profile: ImageNormalizationProfile
    ) -> NormalizedImageArtifact:
        return NormalizedImageArtifact(
            content=b"normalized-webp",
            content_type="image/webp",
            width=10,
            height=20,
            source_sha256="a" * 64,
            archive_sha256="b" * 64,
            perceptual_hash="c" * 16,
        )


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.keys: list[str] = []

    async def put(self, *, key: str, content: bytes, content_type: str) -> int:
        self.keys.append(key)
        self.objects[key] = content
        return len(content)

    async def size(self, *, key: str) -> int:
        return len(self.objects[key])


def _claim(*, source_deleted_at: datetime | None = None) -> ClaimedImageArchive:
    return ClaimedImageArchive(
        work_item=ImageArchiveWorkItem(
            image_asset_id=str(uuid4()),
            source_channel_id=str(uuid4()),
            source_telegram_message_id=17,
            source_asset_id="telegram-source-17",
            source_deleted_at=source_deleted_at,
        ),
        lease_token=str(uuid4()),
    )


def _worker(
    *, repository: _Repository, downloader: _Downloader, storage: _Storage
) -> ImageArchiveWorker:
    return ImageArchiveWorker(
        repository=repository,
        media_downloader=downloader,
        archive_service=ImageArchiveService(image_processor=_Processor(), archive_storage=storage),
        normalization_profile=ImageNormalizationProfile(max_side_pixels=100, quality=80),
        storage_backend="local-volume",
    )


if __name__ == "__main__":
    unittest.main()
