from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.media import (
    ClaimedImageArchive,
    ImageArchiveReadyMetadata,
    ImageArchiveWorkItem,
    NormalizedImageArtifact,
)
from tgcurator.infrastructure.database.image_archive_repository import (
    SqlAlchemyImageArchiveWorkRepository,
    image_archive_claim_statement,
    image_archive_complete_wakeup_statement,
    image_archive_mark_ready_claim_statement,
)
from tgcurator.infrastructure.database.models import Base, ImageAssetRecord

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _Result:
    def __init__(self, value: object | None = None, *, rowcount: int = 1) -> None:
        self.value = value
        self.rowcount = rowcount

    def one_or_none(self) -> object | None:
        return self.value


class _Session:
    def __init__(
        self,
        *,
        execute_results: list[_Result] | None = None,
        scalar_results: list[object | None] | None = None,
    ) -> None:
        self.execute_results = list(execute_results or [])
        self.scalar_results = list(scalar_results or [])
        self.execute_statements: list[object] = []
        self.scalar_statements: list[object] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object) -> _Result:
        self.execute_statements.append(statement)
        if self.execute_results:
            return self.execute_results.pop(0)
        return _Result()

    async def scalar(self, statement: object) -> object | None:
        self.scalar_statements.append(statement)
        if not self.scalar_results:
            raise AssertionError("unexpected scalar call")
        return self.scalar_results.pop(0)


class _Database:
    def __init__(self, session: _Session) -> None:
        self.session_value = session

    @asynccontextmanager
    async def session(self):
        yield self.session_value


class ImageArchiveWorkRepositoryStatementTests(unittest.TestCase):
    def test_claim_statement_uses_all_durable_claimable_states_and_skip_locked(self) -> None:
        sql = _compile(image_archive_claim_statement(image_asset_id=uuid4(), now=NOW))

        self.assertIn("FROM image_assets JOIN messages", sql)
        self.assertIn("image_assets.archive_state =", sql)
        self.assertIn("image_assets.archive_next_retry_at <=", sql)
        self.assertIn("image_assets.archive_lease_expires_at <=", sql)
        self.assertIn("image_assets.source_telegram_message_id IS NOT NULL", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)

    def test_ready_statement_requires_current_processing_lease_and_clears_retry_metadata(
        self,
    ) -> None:
        claim = _claim()
        sql = _compile(
            image_archive_mark_ready_claim_statement(
                claim=claim,
                metadata=_metadata(image_asset_id=claim.work_item.image_asset_id),
            )
        )

        self.assertIn("UPDATE image_assets SET", sql)
        self.assertIn("archive_state=", sql)
        self.assertIn("archive_next_retry_at=", sql)
        self.assertIn("archive_last_error_code=", sql)
        self.assertIn("archive_lease_token=", sql)
        self.assertIn("image_assets.archive_state =", sql)
        self.assertIn("image_assets.archive_lease_token =", sql)
        self.assertIn("archive_ready_at=", sql)
        self.assertNotIn("archive_failure_reason", sql)

    def test_terminal_wakeup_completion_requires_a_terminal_image_archive_state(self) -> None:
        sql = _compile(image_archive_complete_wakeup_statement(image_asset_id=uuid4(), now=NOW))

        self.assertIn("UPDATE durable_wakeups SET", sql)
        self.assertIn("durable_wakeups.queue =", sql)
        self.assertIn("durable_wakeups.entity_id =", sql)
        self.assertIn("durable_wakeups.status IN", sql)
        self.assertIn("EXISTS (SELECT image_assets.id", sql)
        self.assertIn("image_assets.archive_state IN", sql)
        self.assertIn("completed", sql)

    def test_image_asset_schema_contains_bounded_retry_and_lease_invariants(self) -> None:
        table = Base.metadata.tables["image_assets"]
        constraints = {constraint.name for constraint in table.constraints}
        indexes = {index.name for index in table.indexes}

        self.assertIn("archive_attempt_count", table.c)
        self.assertIn("archive_max_attempts", table.c)
        self.assertIn("archive_next_retry_at", table.c)
        self.assertIn("archive_last_error_code", table.c)
        self.assertIn("ck_image_assets_image_asset_archive_attempts", constraints)
        self.assertIn("ck_image_assets_image_asset_archive_lease", constraints)
        self.assertIn("ck_image_assets_image_asset_archive_retry_schedule", constraints)
        self.assertIn("ck_image_assets_image_asset_archive_failure_metadata", constraints)
        self.assertIn("ix_image_assets_archive_due", indexes)
        self.assertIn("ix_image_assets_archive_lease", indexes)


class ImageArchiveWorkRepositoryBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_moves_due_retry_to_processing_and_increments_attempt(self) -> None:
        row = _asset(state="retry_wait", attempts=1, max_attempts=3)
        row.archive_next_retry_at = NOW
        session = _Session(
            execute_results=[_Result((row, uuid4(), None))],
        )
        repository = SqlAlchemyImageArchiveWorkRepository(_Database(session))  # type: ignore[arg-type]

        claim = await repository.claim(
            image_asset_id=str(row.id), now=NOW, lease_duration=timedelta(minutes=5)
        )

        self.assertIsNotNone(claim)
        self.assertEqual(row.archive_state, "processing")
        self.assertEqual(row.archive_attempt_count, 2)
        self.assertIsNone(row.archive_next_retry_at)
        self.assertIsNotNone(row.archive_lease_token)
        self.assertEqual(row.archive_lease_expires_at, NOW + timedelta(minutes=5))

    async def test_exhausted_claim_is_terminalized_without_another_external_attempt(self) -> None:
        row = _asset(state="processing", attempts=3, max_attempts=3)
        row.archive_lease_token = uuid4()
        row.archive_lease_expires_at = NOW - timedelta(seconds=1)
        session = _Session(execute_results=[_Result((row, uuid4(), None)), _Result()])
        repository = SqlAlchemyImageArchiveWorkRepository(_Database(session))  # type: ignore[arg-type]

        claim = await repository.claim(
            image_asset_id=str(row.id), now=NOW, lease_duration=timedelta(minutes=5)
        )

        self.assertIsNone(claim)
        self.assertEqual(row.archive_state, "failed")
        self.assertEqual(row.archive_last_error_code, "ARCHIVE_ATTEMPTS_EXHAUSTED")
        self.assertIsNone(row.archive_lease_token)
        wakeup_sql = _compile(session.execute_statements[-1])
        self.assertIn("status=", wakeup_sql)
        self.assertIn("completed", wakeup_sql)

    async def test_retryable_failure_persists_safe_metadata_and_reschedules_wakeup(self) -> None:
        row = _asset(state="processing", attempts=1, max_attempts=3)
        token = uuid4()
        row.archive_lease_token = token
        row.archive_lease_expires_at = NOW + timedelta(minutes=5)
        claim = _claim(image_asset_id=str(row.id), lease_token=str(token))
        session = _Session(scalar_results=[row], execute_results=[_Result()])
        repository = SqlAlchemyImageArchiveWorkRepository(_Database(session))  # type: ignore[arg-type]
        retry_at = NOW + timedelta(minutes=1)

        self.assertTrue(
            await repository.fail(
                claim=claim,
                error_code="TELEGRAM_DOWNLOAD_FAILED",
                error_type="TelegramDownloadError",
                retryable=True,
                retry_at=retry_at,
                now=NOW,
            )
        )

        self.assertEqual(row.archive_state, "retry_wait")
        self.assertEqual(row.archive_next_retry_at, retry_at)
        self.assertEqual(row.archive_last_error_code, "TELEGRAM_DOWNLOAD_FAILED")
        self.assertEqual(row.archive_last_error_type, "TelegramDownloadError")
        self.assertIsNone(row.archive_lease_token)
        wakeup_sql = _compile(session.execute_statements[-1])
        self.assertIn("next_attempt_at=", wakeup_sql)
        self.assertIn("status=", wakeup_sql)

    async def test_exhausted_retryable_failure_becomes_terminal(self) -> None:
        row = _asset(state="processing", attempts=3, max_attempts=3)
        token = uuid4()
        row.archive_lease_token = token
        row.archive_lease_expires_at = NOW + timedelta(minutes=5)
        claim = _claim(image_asset_id=str(row.id), lease_token=str(token))
        session = _Session(scalar_results=[row], execute_results=[_Result()])
        repository = SqlAlchemyImageArchiveWorkRepository(_Database(session))  # type: ignore[arg-type]

        self.assertTrue(
            await repository.fail(
                claim=claim,
                error_code="IMAGE_ARCHIVE_FAILED",
                error_type="ImageArchiveError",
                retryable=True,
                retry_at=NOW + timedelta(minutes=1),
                now=NOW,
            )
        )

        self.assertEqual(row.archive_state, "failed")
        self.assertIsNone(row.archive_next_retry_at)
        wakeup_sql = _compile(session.execute_statements[-1])
        self.assertIn("completed", wakeup_sql)


def _compile(statement: object) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )


def _claim(
    *, image_asset_id: str | None = None, lease_token: str | None = None
) -> ClaimedImageArchive:
    return ClaimedImageArchive(
        work_item=ImageArchiveWorkItem(
            image_asset_id=image_asset_id or str(uuid4()),
            source_channel_id=str(uuid4()),
            source_telegram_message_id=17,
            source_asset_id="telegram-source-17",
        ),
        lease_token=lease_token or str(uuid4()),
    )


def _asset(*, state: str, attempts: int, max_attempts: int) -> ImageAssetRecord:
    return ImageAssetRecord(
        id=uuid4(),
        message_id=uuid4(),
        source_asset_id="telegram-source-17",
        source_telegram_message_id=17,
        archive_state=state,
        archive_attempt_count=attempts,
        archive_max_attempts=max_attempts,
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
