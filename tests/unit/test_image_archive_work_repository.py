from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.media import (
    ClaimedImageArchive,
    ImageArchiveReadyMetadata,
    ImageArchiveWorkItem,
    NormalizedImageArtifact,
)
from tgcurator.infrastructure.database.image_archive_repository import (
    image_archive_claim_statement,
    image_archive_mark_failed_statement,
    image_archive_mark_ready_claim_statement,
    image_archive_release_statement,
)
from tgcurator.infrastructure.database.models import Base

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class ImageArchiveWorkRepositoryStatementTests(unittest.TestCase):
    def test_claim_statement_uses_pending_asset_lease_and_skip_locked(self) -> None:
        sql = _compile(image_archive_claim_statement(image_asset_id=uuid4(), now=NOW))

        self.assertIn("FROM image_assets JOIN messages", sql)
        self.assertIn("messages.id = image_assets.message_id", sql)
        self.assertIn("image_assets.archive_state =", sql)
        self.assertIn("image_assets.source_telegram_message_id IS NOT NULL", sql)
        self.assertIn("image_assets.archive_lease_token IS NULL", sql)
        self.assertIn("image_assets.archive_lease_expires_at <=", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)

    def test_ready_statement_requires_current_lease_and_clears_it(self) -> None:
        claim = _claim()
        sql = _compile(
            image_archive_mark_ready_claim_statement(
                claim=claim,
                metadata=_metadata(image_asset_id=claim.work_item.image_asset_id),
            )
        )

        self.assertIn("UPDATE image_assets SET", sql)
        self.assertIn("archive_state=", sql)
        self.assertIn("archive_lease_token=", sql)
        self.assertIn("archive_lease_expires_at=", sql)
        self.assertIn("image_assets.archive_state =", sql)
        self.assertIn("image_assets.archive_lease_token =", sql)
        self.assertIn("archive_ready_at=", sql)

    def test_terminal_failure_requires_current_lease_and_stable_reason(self) -> None:
        sql = _compile(
            image_archive_mark_failed_statement(claim=_claim(), reason="protected_content", now=NOW)
        )

        self.assertIn("UPDATE image_assets SET", sql)
        self.assertIn("archive_state=", sql)
        self.assertIn("archive_failure_reason=", sql)
        self.assertIn("archive_lease_token=", sql)
        self.assertIn("archive_lease_expires_at=", sql)
        self.assertIn("image_assets.archive_state =", sql)
        self.assertIn("image_assets.archive_lease_token =", sql)

    def test_release_requires_current_lease_and_does_not_persist_exception_text(self) -> None:
        sql = _compile(image_archive_release_statement(claim=_claim(), now=NOW))

        self.assertIn("UPDATE image_assets SET", sql)
        self.assertIn("archive_lease_token=", sql)
        self.assertIn("archive_lease_expires_at=", sql)
        self.assertIn("archive_failure_reason=", sql)
        self.assertIn("image_assets.archive_state =", sql)
        self.assertIn("image_assets.archive_lease_token =", sql)
        self.assertNotIn("network unavailable", sql)

    def test_rejects_unrecognized_terminal_failure_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "recognized terminal"):
            image_archive_mark_failed_statement(
                claim=_claim(), reason="network unavailable", now=NOW
            )

    def test_image_asset_schema_contains_archive_lease_invariants(self) -> None:
        table = Base.metadata.tables["image_assets"]
        constraints = {constraint.name for constraint in table.constraints}
        indexes = {index.name for index in table.indexes}

        self.assertIn("archive_lease_token", table.c)
        self.assertIn("archive_lease_expires_at", table.c)
        self.assertIn("ck_image_asset_archive_lease", constraints)
        self.assertIn("ck_image_asset_failure_reason", constraints)
        self.assertIn("ix_image_assets_pending_archive_lease", indexes)


def _compile(statement: object) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )


def _claim() -> ClaimedImageArchive:
    return ClaimedImageArchive(
        work_item=ImageArchiveWorkItem(
            image_asset_id=str(uuid4()),
            source_channel_id=str(uuid4()),
            source_telegram_message_id=17,
            source_asset_id="telegram-source-17",
        ),
        lease_token=str(uuid4()),
    )


def _metadata(*, image_asset_id: str) -> ImageArchiveReadyMetadata:
    artifact = NormalizedImageArtifact(
        content=b"normalized-webp",
        content_type="image/webp",
        width=10,
        height=20,
        source_sha256="a" * 64,
        archive_sha256="b" * 64,
        perceptual_hash="c" * 16,
    )
    return ImageArchiveReadyMetadata.from_artifact(
        image_asset_id=str(UUID(image_asset_id)),
        storage_backend="local-volume",
        storage_key="source/7/message/17/image.webp",
        artifact=artifact,
        archive_size_bytes=len(artifact.content),
        archived_at=NOW,
    )


if __name__ == "__main__":
    unittest.main()
