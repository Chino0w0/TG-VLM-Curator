from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.media import (
    ClaimedVideoArchive,
    NormalizedImageArtifact,
    VideoArchiveReadyMetadata,
    VideoArchiveWorkItem,
    VideoFrameReadyMetadata,
    VideoProbeMetadata,
)
from tgcurator.infrastructure.database.models import Base, VideoAssetRecord, VideoFrameRecord
from tgcurator.infrastructure.database.video_archive_repository import (
    SqlAlchemyVideoArchiveWorkRepository,
    video_archive_claim_statement,
    video_archive_complete_wakeup_statement,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


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
        self.added: list[object] = []

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

    def add(self, value: object) -> None:
        self.added.append(value)


class _Database:
    def __init__(self, session: _Session) -> None:
        self.session_value = session

    @asynccontextmanager
    async def session(self):
        yield self.session_value


class VideoArchiveRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def test_claim_and_wakeup_statements_use_durable_video_state(self) -> None:
        claim_sql = _compile(video_archive_claim_statement(video_asset_id=uuid4(), now=NOW))
        wakeup_sql = _compile(
            video_archive_complete_wakeup_statement(video_asset_id=uuid4(), now=NOW)
        )

        self.assertIn("FROM video_assets JOIN messages", claim_sql)
        self.assertIn("video_assets.archive_next_retry_at <=", claim_sql)
        self.assertIn("video_assets.archive_lease_expires_at <=", claim_sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", claim_sql)
        self.assertIn("EXISTS (SELECT video_assets.id", wakeup_sql)
        self.assertIn("durable_wakeups.queue =", wakeup_sql)

    def test_schema_contains_video_retry_and_frame_archive_invariants(self) -> None:
        video = Base.metadata.tables["video_assets"]
        frames = Base.metadata.tables["video_frames"]
        video_constraints = {constraint.name for constraint in video.constraints}
        frame_constraints = {constraint.name for constraint in frames.constraints}

        self.assertIn("ck_video_assets_video_asset_archive_lease", video_constraints)
        self.assertIn("ck_video_assets_video_asset_ready_metadata", video_constraints)
        self.assertIn("ck_video_frames_video_frame_content_type", frame_constraints)
        self.assertIn("uq_video_frame_storage_key", frame_constraints)

    async def test_claim_increments_attempt_and_returns_exact_telegram_reference(self) -> None:
        row = _asset(state="retry_wait", attempts=1, max_attempts=3)
        row.archive_next_retry_at = NOW
        source_channel_id = uuid4()
        session = _Session(execute_results=[_Result((row, source_channel_id, None))])
        repository = SqlAlchemyVideoArchiveWorkRepository(_Database(session))  # type: ignore[arg-type]

        claim = await repository.claim(
            video_asset_id=str(row.id), now=NOW, lease_duration=timedelta(minutes=5)
        )

        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.work_item.source_channel_id, str(source_channel_id))
        self.assertEqual(claim.work_item.source_telegram_message_id, 17)
        self.assertEqual(row.archive_state, "processing")
        self.assertEqual(row.archive_attempt_count, 2)
        self.assertEqual(row.archive_lease_expires_at, NOW + timedelta(minutes=5))

    async def test_mark_ready_replaces_frames_and_commits_asset_metadata_atomically(self) -> None:
        row = _asset(state="processing", attempts=1, max_attempts=3)
        token = uuid4()
        row.archive_lease_token = token
        row.archive_lease_expires_at = NOW + timedelta(minutes=5)
        claim = _claim(video_asset_id=str(row.id), lease_token=str(token))
        metadata = _metadata(video_asset_id=str(row.id))
        session = _Session(scalar_results=[row], execute_results=[_Result()])
        repository = SqlAlchemyVideoArchiveWorkRepository(_Database(session))  # type: ignore[arg-type]

        self.assertTrue(await repository.mark_ready(claim=claim, metadata=metadata))

        self.assertEqual(row.archive_state, "ready")
        self.assertEqual(row.duration_seconds, 12)
        self.assertEqual(row.source_sha256, "d" * 64)
        self.assertIsNone(row.archive_lease_token)
        self.assertEqual(len(session.added), 2)
        self.assertTrue(all(isinstance(frame, VideoFrameRecord) for frame in session.added))
        self.assertIn("DELETE FROM video_frames", _compile(session.execute_statements[0]))

    async def test_retryable_failure_reschedules_video_wakeup(self) -> None:
        row = _asset(state="processing", attempts=1, max_attempts=3)
        token = uuid4()
        row.archive_lease_token = token
        row.archive_lease_expires_at = NOW + timedelta(minutes=5)
        claim = _claim(video_asset_id=str(row.id), lease_token=str(token))
        session = _Session(scalar_results=[row], execute_results=[_Result()])
        repository = SqlAlchemyVideoArchiveWorkRepository(_Database(session))  # type: ignore[arg-type]
        retry_at = NOW + timedelta(minutes=1)

        self.assertTrue(
            await repository.fail(
                claim=claim,
                error_code="VIDEO_ARCHIVE_FAILED",
                error_type="VideoArchiveError",
                retryable=True,
                retry_at=retry_at,
                now=NOW,
            )
        )

        self.assertEqual(row.archive_state, "retry_wait")
        self.assertEqual(row.archive_next_retry_at, retry_at)
        self.assertIn("next_attempt_at=", _compile(session.execute_statements[-1]))


def _compile(statement: object) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )


def _asset(*, state: str, attempts: int, max_attempts: int) -> VideoAssetRecord:
    return VideoAssetRecord(
        id=uuid4(),
        message_id=uuid4(),
        source_asset_id="video-17",
        source_telegram_message_id=17,
        archive_state=state,
        archive_attempt_count=attempts,
        archive_max_attempts=max_attempts,
    )


def _claim(*, video_asset_id: str, lease_token: str) -> ClaimedVideoArchive:
    return ClaimedVideoArchive(
        work_item=VideoArchiveWorkItem(
            video_asset_id=video_asset_id,
            source_channel_id=str(uuid4()),
            source_telegram_message_id=17,
            source_asset_id="video-17",
        ),
        lease_token=lease_token,
    )


def _metadata(*, video_asset_id: str) -> VideoArchiveReadyMetadata:
    frames = tuple(
        VideoFrameReadyMetadata(
            frame_role=role,
            frame_index=index,
            timestamp_seconds=timestamp,
            storage_backend="local-volume",
            storage_key=f"videos/{video_asset_id}/{role}-{index}.webp",
            artifact=NormalizedImageArtifact(
                content=f"frame-{index}".encode(),
                content_type="image/webp",
                width=100,
                height=80,
                source_sha256="a" * 64,
                archive_sha256="b" * 64,
                perceptual_hash=("c" if index == 0 else "e") * 16,
            ),
            archive_size_bytes=len(f"frame-{index}".encode()),
        )
        for role, index, timestamp in (("cover", 0, 0.0), ("representative", 0, 6.0))
    )
    return VideoArchiveReadyMetadata(
        video_asset_id=video_asset_id,
        source_content_type="video/mp4",
        source_size_bytes=123,
        source_sha256="d" * 64,
        probe=VideoProbeMetadata(duration_seconds=12, width=1920, height=1080),
        frames=frames,
        archived_at=NOW,
    )


if __name__ == "__main__":
    unittest.main()
