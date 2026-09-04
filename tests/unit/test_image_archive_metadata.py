from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.media import ImageArchiveMetadataPersistenceError, ImageArchiveService
from tgcurator.application.ports.media import (
    ImageArchiveReadyMetadata,
    ImageNormalizationProfile,
    NormalizedImageArtifact,
)
from tgcurator.domain.messages import (
    MediaAsset,
    MediaKind,
    MessageContent,
    NormalizedTelegramMessage,
)
from tgcurator.infrastructure.database.image_archive_repository import (
    image_asset_mark_ready_statement,
)
from tgcurator.infrastructure.database.message_ingest_repository import (
    image_assets_upsert_statement,
)
from tgcurator.infrastructure.database.models import Base

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class ImageArchiveReadyMetadataTests(unittest.TestCase):
    def test_rejects_invalid_ready_metadata(self) -> None:
        metadata = _metadata()
        invalid_values = (
            {"image_asset_id": "not-a-uuid"},
            {"storage_key": "/absolute/image.webp"},
            {"storage_key": "images\\windows.webp"},
            {"storage_key": "images/../escape.webp"},
            {"storage_key": "C:/drive.webp"},
            {"content_type": "image/png"},
            {"width": 0},
            {"archive_size_bytes": 0},
            {"source_sha256": "A" * 64},
            {"archive_sha256": "a" * 63},
            {"perceptual_hash": "b" * 15},
            {"archived_at": NOW.replace(tzinfo=None)},
        )

        for changes in invalid_values:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(metadata, **changes)

    def test_ready_metadata_from_artifact_preserves_verified_facts(self) -> None:
        artifact = _artifact()

        metadata = ImageArchiveReadyMetadata.from_artifact(
            image_asset_id=str(uuid4()),
            storage_backend="local-volume",
            storage_key="source/7/message/11/image.webp",
            artifact=artifact,
            archive_size_bytes=len(artifact.content),
            archived_at=NOW,
        )

        self.assertEqual(metadata.content_type, artifact.content_type)
        self.assertEqual(metadata.width, artifact.width)
        self.assertEqual(metadata.height, artifact.height)
        self.assertEqual(metadata.source_sha256, artifact.source_sha256)
        self.assertEqual(metadata.archive_sha256, artifact.archive_sha256)
        self.assertEqual(metadata.perceptual_hash, artifact.perceptual_hash)


class ImageArchiveServiceReadyTests(unittest.TestCase):
    def test_archives_before_persisting_ready_metadata(self) -> None:
        artifact = _artifact()
        archive = _RecordingArchiveStorage(size=len(artifact.content))
        repository = _RecordingMetadataRepository(result=True, events=archive.events)

        result = asyncio.run(
            ImageArchiveService(
                _RecordingImageProcessor(artifact), archive
            ).normalize_archive_and_mark_ready(
                image_asset_id=str(uuid4()),
                storage_backend="local-volume",
                storage_key="source/7/message/11/image.webp",
                content=b"source-image",
                profile=ImageNormalizationProfile(max_side_pixels=100, quality=80),
                archived_at=NOW,
                metadata_repository=repository,
            )
        )

        self.assertIs(result, artifact)
        self.assertEqual([event[0] for event in archive.events], ["put", "size", "ready"])
        self.assertEqual(len(repository.calls), 1)
        metadata = repository.calls[0]
        self.assertEqual(metadata.storage_backend, "local-volume")
        self.assertEqual(metadata.storage_key, "source/7/message/11/image.webp")
        self.assertEqual(metadata.archive_size_bytes, len(artifact.content))
        self.assertEqual(metadata.archive_sha256, artifact.archive_sha256)
        self.assertEqual(metadata.perceptual_hash, artifact.perceptual_hash)

    def test_raises_when_ready_persistence_rejects_asset(self) -> None:
        artifact = _artifact()
        archive = _RecordingArchiveStorage(size=len(artifact.content))
        repository = _RecordingMetadataRepository(result=False, events=archive.events)

        with self.assertRaises(ImageArchiveMetadataPersistenceError):
            asyncio.run(
                ImageArchiveService(
                    _RecordingImageProcessor(artifact), archive
                ).normalize_archive_and_mark_ready(
                    image_asset_id=str(uuid4()),
                    storage_backend="local-volume",
                    storage_key="source/7/message/11/image.webp",
                    content=b"source-image",
                    profile=ImageNormalizationProfile(max_side_pixels=100, quality=80),
                    archived_at=NOW,
                    metadata_repository=repository,
                )
            )

        self.assertEqual([event[0] for event in archive.events], ["put", "size", "ready"])

    def test_raises_without_metadata_write_when_archive_size_mismatches(self) -> None:
        artifact = _artifact()
        archive = _RecordingArchiveStorage(size=len(artifact.content) + 1)
        repository = _RecordingMetadataRepository(result=True, events=archive.events)

        with self.assertRaises(ImageArchiveMetadataPersistenceError):
            asyncio.run(
                ImageArchiveService(
                    _RecordingImageProcessor(artifact), archive
                ).normalize_archive_and_mark_ready(
                    image_asset_id=str(uuid4()),
                    storage_backend="local-volume",
                    storage_key="source/7/message/11/image.webp",
                    content=b"source-image",
                    profile=ImageNormalizationProfile(max_side_pixels=100, quality=80),
                    archived_at=NOW,
                    metadata_repository=repository,
                )
            )

        self.assertEqual([event[0] for event in archive.events], ["put", "size"])
        self.assertEqual(repository.calls, [])


class ImageArchiveMetadataRepositoryStatementTests(unittest.TestCase):
    def test_ready_statement_allows_pending_or_exact_immutable_replay(self) -> None:
        sql = str(
            image_asset_mark_ready_statement(metadata=_metadata()).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
            )
        )

        self.assertIn("UPDATE image_assets SET", sql)
        self.assertIn("archive_state=", sql)
        self.assertIn("image_assets.archive_state IN", sql)
        self.assertIn("image_assets.storage_backend =", sql)
        self.assertIn("image_assets.storage_key =", sql)
        self.assertIn("image_assets.content_type =", sql)
        self.assertIn("image_assets.width =", sql)
        self.assertIn("image_assets.source_sha256 =", sql)
        self.assertIn("image_assets.archive_sha256 =", sql)
        self.assertIn("image_assets.perceptual_hash =", sql)
        self.assertIn("image_assets.archive_size_bytes =", sql)

    def test_image_asset_upsert_creates_only_image_assets(self) -> None:
        image_message = NormalizedTelegramMessage(
            source_channel_id=str(uuid4()),
            telegram_anchor_message_id=5,
            telegram_message_ids=(5,),
            sent_at=NOW,
            content=MessageContent(
                media=(MediaAsset("image-1", MediaKind.IMAGE, original_visual_phash="a" * 16),)
            ),
        )
        text_message = NormalizedTelegramMessage(
            source_channel_id=str(uuid4()),
            telegram_anchor_message_id=6,
            telegram_message_ids=(6,),
            sent_at=NOW,
            content=MessageContent(),
        )

        statement = image_assets_upsert_statement(message_id=uuid4(), message=image_message)

        self.assertIsNotNone(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("INSERT INTO image_assets", sql)
        self.assertIn("ON CONFLICT ON CONSTRAINT uq_image_asset_message_source", sql)
        self.assertIsNone(image_assets_upsert_statement(message_id=uuid4(), message=text_message))

    def test_image_asset_schema_contains_ready_invariants(self) -> None:
        table = Base.metadata.tables["image_assets"]
        constraints = {constraint.name for constraint in table.constraints}

        self.assertIn("perceptual_hash", table.c)
        self.assertIn("ck_image_asset_archive_state", constraints)
        self.assertIn("ck_image_asset_ready_metadata", constraints)
        self.assertIn("ck_image_asset_deleted_timestamp", constraints)
        self.assertIn("uq_image_asset_message_source", constraints)
        self.assertIn("ix_image_assets_archive_state", {index.name for index in table.indexes})


def _artifact() -> NormalizedImageArtifact:
    return NormalizedImageArtifact(
        content=b"normalized-webp",
        content_type="image/webp",
        width=10,
        height=20,
        source_sha256="a" * 64,
        archive_sha256="b" * 64,
        perceptual_hash="c" * 16,
    )


def _metadata() -> ImageArchiveReadyMetadata:
    artifact = _artifact()
    return ImageArchiveReadyMetadata.from_artifact(
        image_asset_id=str(uuid4()),
        storage_backend="local-volume",
        storage_key="source/7/message/11/image.webp",
        artifact=artifact,
        archive_size_bytes=len(artifact.content),
        archived_at=NOW,
    )


class _RecordingImageProcessor:
    def __init__(self, artifact: NormalizedImageArtifact) -> None:
        self._artifact = artifact

    async def normalize(
        self, *, content: bytes, profile: ImageNormalizationProfile
    ) -> NormalizedImageArtifact:
        return self._artifact


class _RecordingArchiveStorage:
    def __init__(self, *, size: int) -> None:
        self._size = size
        self.events: list[tuple[str, object]] = []

    async def put(self, *, key: str, content: bytes, content_type: str) -> int:
        self.events.append(("put", key))
        return len(content)

    async def size(self, *, key: str) -> int:
        self.events.append(("size", key))
        return self._size


class _RecordingMetadataRepository:
    def __init__(self, *, result: bool, events: list[tuple[str, object]]) -> None:
        self._result = result
        self._events = events
        self.calls: list[ImageArchiveReadyMetadata] = []

    async def mark_ready(self, *, metadata: ImageArchiveReadyMetadata) -> bool:
        self.calls.append(metadata)
        self._events.append(("ready", metadata))
        return self._result


if __name__ == "__main__":
    unittest.main()
