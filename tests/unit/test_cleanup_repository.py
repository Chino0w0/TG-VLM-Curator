from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.cleanup import (
    ArchiveCleanupFilter,
    ClaimedArchiveCleanupJob,
    ClaimedCleanupItem,
    CleanupAsset,
    CleanupBlob,
)
from tgcurator.infrastructure.database.cleanup_repository import (
    CLEANUP_QUEUE,
    SqlAlchemyArchiveCleanupRepository,
    cleanup_asset_in_use_statement,
    cleanup_complete_wakeup_statement,
    cleanup_image_candidates_statement,
    cleanup_job_claim_statement,
    cleanup_repair_candidates_statement,
    cleanup_video_candidates_statement,
)
from tgcurator.infrastructure.database.models import (
    ArchiveCleanupDryRunRecord,
    ArchiveCleanupItemRecord,
    ArchiveCleanupJobRecord,
    AuditEvent,
    DurableWakeupRecord,
    ImageAssetRecord,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
DRY_RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ITEM_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE_ID = UUID("55555555-5555-4555-8555-555555555555")
ADMIN_ID = UUID("66666666-6666-4666-8666-666666666666")
WAKEUP_ID = UUID("77777777-7777-4777-8777-777777777777")
LEASE_ID = UUID("88888888-8888-4888-8888-888888888888")
DELETION_ID = UUID("99999999-9999-4999-8999-999999999999")
AUDIT_ID = UUID("abababab-abab-4aba-8aba-abababababab")


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _Rows:
    def __init__(self, rows: tuple[object, ...] = (), *, rowcount: int = 1) -> None:
        self.rows = rows
        self.rowcount = rowcount

    def all(self) -> tuple[object, ...]:
        return self.rows


class _Session:
    def __init__(
        self,
        *,
        execute_results: list[_Rows] | None = None,
        scalar_results: list[object | None] | None = None,
        scalar_batches: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.execute_results = list(execute_results or [])
        self.scalar_results = list(scalar_results or [])
        self.scalar_batches = list(scalar_batches or [])
        self.execute_statements: list[object] = []
        self.scalar_statements: list[object] = []
        self.scalars_statements: list[object] = []
        self.added: list[object] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object) -> _Rows:
        self.execute_statements.append(statement)
        return self.execute_results.pop(0) if self.execute_results else _Rows()

    async def scalar(self, statement: object) -> object | None:
        self.scalar_statements.append(statement)
        if not self.scalar_results:
            raise AssertionError("unexpected scalar call")
        return self.scalar_results.pop(0)

    async def scalars(self, statement: object) -> tuple[object, ...]:
        self.scalars_statements.append(statement)
        if not self.scalar_batches:
            raise AssertionError("unexpected scalars call")
        return self.scalar_batches.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)


class _Database:
    def __init__(self, session: _Session) -> None:
        self.session_value = session

    @asynccontextmanager
    async def session(self):
        yield self.session_value


class _IdFactory:
    def __init__(self, *values: UUID) -> None:
        self.values = list(values)

    def __call__(self) -> UUID:
        if not self.values:
            raise AssertionError("unexpected UUID allocation")
        return self.values.pop(0)


def _sql(statement: object, *, literal_binds: bool = False) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": literal_binds},
        )
    )


def _params(statement: object) -> dict[str, object]:
    return statement.compile(dialect=postgresql.dialect()).params


def _filter() -> ArchiveCleanupFilter:
    return ArchiveCleanupFilter(
        archived_from=NOW - timedelta(days=30),
        archived_before=NOW,
        source_channel_ids=(str(uuid4()),),
        message_published_from=NOW - timedelta(days=60),
        message_published_before=NOW - timedelta(days=1),
        model_label_keys=("model-label",),
        manual_label_keys=("manual-label",),
        effective_label_keys=("effective-label",),
        label_scopes=("global", "media"),
        negative_label=False,
        review_statuses=("reviewed",),
        processing_statuses=("processed",),
        duplicate_statuses=("canonical", "duplicate"),
        media_types=("image", "video"),
        blocked_from_analysis=False,
    )


def _candidate_row(
    *,
    asset_kind: str = "image",
    asset_id: UUID = ASSET_ID,
    message_id: UUID = MESSAGE_ID,
    blobs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    frozen = (
        blobs
        if blobs is not None
        else [{"storage_backend": "archive", "storage_key": "image.webp", "size_bytes": 37}]
    )
    return {
        "asset_kind": asset_kind,
        "asset_id": asset_id,
        "message_id": message_id,
        "previous_archive_size": sum(int(blob["size_bytes"]) for blob in frozen),
        "blob_snapshot": frozen,
    }


def _dry_run(cleanup_filter: ArchiveCleanupFilter) -> ArchiveCleanupDryRunRecord:
    return ArchiveCleanupDryRunRecord(
        id=DRY_RUN_ID,
        actor_admin_user_id=ADMIN_ID,
        filter_snapshot=cleanup_filter.snapshot(),
        filter_hash=cleanup_filter.sha256,
        estimated_asset_count=1,
        estimated_message_count=1,
        estimated_bytes=37,
        created_at=NOW,
    )


def _job(
    *,
    status: str = "running",
    attempt_count: int = 1,
    max_attempts: int = 3,
    lease_token: UUID | None = LEASE_ID,
    lease_expires_at: datetime | None = NOW + timedelta(minutes=5),
) -> ArchiveCleanupJobRecord:
    return ArchiveCleanupJobRecord(
        id=JOB_ID,
        dry_run_id=DRY_RUN_ID,
        confirmed_by_admin_user_id=ADMIN_ID,
        filter_snapshot=ArchiveCleanupFilter(media_types=("image",)).snapshot(),
        filter_hash=ArchiveCleanupFilter(media_types=("image",)).sha256,
        estimated_asset_count=1,
        estimated_message_count=1,
        estimated_bytes=37,
        item_count=1,
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        started_at=NOW if attempt_count else None,
        created_at=NOW,
        updated_at=NOW,
    )


def _claim() -> ClaimedArchiveCleanupJob:
    return ClaimedArchiveCleanupJob(
        job_id=str(JOB_ID),
        lease_token=str(LEASE_ID),
        attempt_number=1,
        max_attempts=3,
    )


def _item(*, status: str = "deleting", deletion_token: UUID | None = DELETION_ID):
    return ArchiveCleanupItemRecord(
        id=ITEM_ID,
        cleanup_job_id=JOB_ID,
        asset_kind="image",
        image_asset_id=ASSET_ID,
        video_asset_id=None,
        message_id=MESSAGE_ID,
        status=status,
        deletion_token=deletion_token,
        blob_snapshot=[
            {"storage_backend": "archive", "storage_key": "image.webp", "size_bytes": 37}
        ],
        previous_archive_size=37,
        created_at=NOW,
        updated_at=NOW,
    )


class CleanupRepositoryStatementTests(unittest.TestCase):
    def test_candidate_sql_represents_every_filter_category_and_ready_only_assets(self) -> None:
        cleanup_filter = _filter()
        image = cleanup_image_candidates_statement(cleanup_filter=cleanup_filter)
        video = cleanup_video_candidates_statement(cleanup_filter=cleanup_filter)
        image_sql = _sql(image)
        video_sql = _sql(video)
        combined = image_sql + video_sql
        params = {**_params(image), **_params(video)}

        self.assertIn("image_assets.archive_state", image_sql)
        self.assertIn("video_assets.archive_state", video_sql)
        self.assertIn("video_frames.archive_state", video_sql)
        self.assertIn("source_channel_id", combined)
        self.assertIn("published_at", combined)
        self.assertIn("review_status", combined)
        self.assertIn("processing_status", combined)
        self.assertIn("blocked_from_analysis", combined)
        self.assertIn("visual_fingerprint", combined)
        self.assertIn("model_label_assignments", combined)
        self.assertIn("manual_label_assignments", combined)
        self.assertIn("label_definition_versions", combined)
        self.assertIn("archive_ready_at", combined)
        self.assertIn("jsonb_agg", video_sql)
        self.assertIn("ORDER BY video_frames.frame_role", video_sql)
        self.assertIn("GROUP BY video_frames.video_asset_id", video_sql)
        self.assertIn("[]", params.values())
        self.assertIn("ready", params.values())
        self.assertIn(["model-label"], params.values())
        self.assertIn(["manual-label"], params.values())
        self.assertIn(["effective-label"], params.values())

    def test_empty_ready_frame_set_is_coalesced_to_zero_bytes_and_empty_snapshot(self) -> None:
        statement = cleanup_video_candidates_statement(
            cleanup_filter=ArchiveCleanupFilter(media_types=("video",))
        )
        sql = _sql(statement)
        params = _params(statement)

        self.assertIn("LEFT OUTER JOIN", sql)
        self.assertIn("coalesce(anon_1.previous_archive_size", sql)
        self.assertIn("coalesce(anon_1.blob_snapshot", sql)
        self.assertIn(0, params.values())
        self.assertIn("[]", params.values())

    def test_effective_label_sql_allows_clear_fallback_but_suppresses_latest_manual_set(
        self,
    ) -> None:
        statement = cleanup_image_candidates_statement(
            cleanup_filter=ArchiveCleanupFilter(
                effective_label_keys=("safe",), negative_label=False
            )
        )
        sql = _sql(statement)

        self.assertIn("manual_label_assignments", sql)
        self.assertIn("operation", sql)
        self.assertIn("NOT (EXISTS", sql)
        self.assertIn("label_definition_version_id", sql)
        # The latest-row predicate is independent of activation, so an inactive latest SET still
        # suppresses model output while a latest CLEAR is excluded by the SET operation predicate.
        self.assertIn("created_at >", sql)
        self.assertIn("activated", sql)

    def test_claim_repair_usage_and_terminal_wakeup_statements_cover_races(self) -> None:
        claim_statement = cleanup_job_claim_statement(job_id=JOB_ID, now=NOW)
        claim_sql = _sql(claim_statement)
        claim_params = _params(claim_statement)
        repair_sql = _sql(cleanup_repair_candidates_statement(now=NOW, limit=50))
        usage_sql = _sql(
            cleanup_asset_in_use_statement(
                asset_kind="image", asset_id=ASSET_ID, message_id=MESSAGE_ID
            )
        )
        terminal_sql = _sql(cleanup_complete_wakeup_statement(job_id=JOB_ID, now=NOW))

        self.assertIn("retry_wait", claim_params.values())
        self.assertIn("running", claim_params.values())
        self.assertIn("lease_expires_at", claim_sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", claim_sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", repair_sql)
        self.assertIn("stage_runs.status", usage_sql)
        self.assertIn("stage_runs.target_kind", usage_sql)
        self.assertIn("publication_intents.status", usage_sql)
        usage_params = _params(
            cleanup_asset_in_use_statement(
                asset_kind="image", asset_id=ASSET_ID, message_id=MESSAGE_ID
            )
        )
        self.assertIn(["pending", "sending", "partial", "retry_wait"], usage_params.values())
        self.assertIn("UPDATE durable_wakeups SET", terminal_sql)
        self.assertIn("EXISTS (SELECT archive_cleanup_jobs.id", terminal_sql)


class CleanupRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_persists_canonical_snapshot_and_ready_estimate(self) -> None:
        cleanup_filter = ArchiveCleanupFilter(media_types=("image",))
        session = _Session(execute_results=[_Rows((_candidate_row(),)), _Rows(())])
        repository = SqlAlchemyArchiveCleanupRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(DRY_RUN_ID, AUDIT_ID),
        )

        dry_run = await repository.create_dry_run(
            cleanup_filter=cleanup_filter,
            actor_admin_user_id=str(ADMIN_ID),
            now=NOW,
        )

        self.assertEqual(dry_run.dry_run_id, str(DRY_RUN_ID))
        self.assertEqual(dry_run.filter_hash, cleanup_filter.sha256)
        self.assertEqual(dry_run.estimate.asset_count, 1)
        self.assertEqual(dry_run.estimate.message_count, 1)
        self.assertEqual(dry_run.estimate.bytes, 37)
        records = [row for row in session.added if isinstance(row, ArchiveCleanupDryRunRecord)]
        audits = [row for row in session.added if isinstance(row, AuditEvent)]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].filter_snapshot, cleanup_filter.snapshot())
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].id, AUDIT_ID)
        self.assertEqual(audits[0].actor_admin_user_id, ADMIN_ID)
        self.assertEqual(audits[0].event_type, "archive_cleanup.dry_run_created")
        self.assertEqual(audits[0].entity_type, "archive_cleanup_dry_run")
        self.assertEqual(audits[0].entity_id, DRY_RUN_ID)
        self.assertEqual(audits[0].payload["actor_type"], "admin")
        self.assertEqual(audits[0].payload["after_snapshot"]["estimated_bytes"], 37)

    async def test_confirmation_requires_exact_persisted_id_and_hash(self) -> None:
        cleanup_filter = ArchiveCleanupFilter(media_types=("image",))
        missing_session = _Session(scalar_results=[None])
        missing = SqlAlchemyArchiveCleanupRepository(_Database(missing_session))  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            await missing.confirm(
                dry_run_id=str(DRY_RUN_ID),
                expected_filter_hash=cleanup_filter.sha256,
                confirmed_by_admin_user_id=str(ADMIN_ID),
                now=NOW,
            )

        changed_session = _Session(scalar_results=[_dry_run(cleanup_filter)])
        changed = SqlAlchemyArchiveCleanupRepository(_Database(changed_session))  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            await changed.confirm(
                dry_run_id=str(DRY_RUN_ID),
                expected_filter_hash="f" * 64,
                confirmed_by_admin_user_id=str(ADMIN_ID),
                now=NOW,
            )
        self.assertEqual(changed_session.execute_statements, [])

    async def test_confirmation_reruns_candidates_and_atomically_adds_job_items_and_uuid_wakeup(
        self,
    ) -> None:
        cleanup_filter = ArchiveCleanupFilter(media_types=("image", "video"))
        image = _candidate_row()
        video_asset_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        video = _candidate_row(
            asset_kind="video",
            asset_id=video_asset_id,
            blobs=[
                {"storage_backend": "archive", "storage_key": "cover.webp", "size_bytes": 11},
                {"storage_backend": "archive", "storage_key": "frame.webp", "size_bytes": 13},
            ],
        )
        session = _Session(
            scalar_results=[_dry_run(cleanup_filter), None],
            execute_results=[_Rows((image,)), _Rows((video,))],
        )
        image_item_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        video_item_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
        repository = SqlAlchemyArchiveCleanupRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(JOB_ID, image_item_id, video_item_id, WAKEUP_ID, AUDIT_ID),
        )

        confirmed = await repository.confirm(
            dry_run_id=str(DRY_RUN_ID),
            expected_filter_hash=cleanup_filter.sha256,
            confirmed_by_admin_user_id=str(ADMIN_ID),
            now=NOW,
        )

        self.assertEqual(confirmed.job_id, str(JOB_ID))
        self.assertEqual(confirmed.item_count, 2)
        self.assertEqual(confirmed.estimate.asset_count, 2)
        self.assertEqual(confirmed.estimate.bytes, 61)
        jobs = [row for row in session.added if isinstance(row, ArchiveCleanupJobRecord)]
        items = [row for row in session.added if isinstance(row, ArchiveCleanupItemRecord)]
        wakeups = [row for row in session.added if isinstance(row, DurableWakeupRecord)]
        audits = [row for row in session.added if isinstance(row, AuditEvent)]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(items), 2)
        self.assertEqual(len(wakeups), 1)
        self.assertEqual(len(audits), 1)
        self.assertEqual(wakeups[0].queue, CLEANUP_QUEUE)
        self.assertEqual(wakeups[0].entity_id, JOB_ID)
        self.assertEqual(wakeups[0].next_attempt_at, NOW)
        self.assertEqual(items[1].blob_snapshot, video["blob_snapshot"])
        self.assertEqual(audits[0].id, AUDIT_ID)
        self.assertEqual(audits[0].actor_admin_user_id, ADMIN_ID)
        self.assertEqual(audits[0].event_type, "archive_cleanup.job_confirmed")
        self.assertEqual(audits[0].entity_type, "archive_cleanup_job")
        self.assertEqual(audits[0].entity_id, JOB_ID)
        self.assertEqual(audits[0].payload["after_snapshot"]["item_count"], 2)

    async def test_confirmation_is_idempotent_without_duplicate_rows(self) -> None:
        cleanup_filter = ArchiveCleanupFilter(media_types=("image",))
        existing = _job(status="pending", attempt_count=0, lease_token=None, lease_expires_at=None)
        session = _Session(scalar_results=[_dry_run(cleanup_filter), existing])
        repository = SqlAlchemyArchiveCleanupRepository(_Database(session))  # type: ignore[arg-type]

        confirmed = await repository.confirm(
            dry_run_id=str(DRY_RUN_ID),
            expected_filter_hash=cleanup_filter.sha256,
            confirmed_by_admin_user_id=str(ADMIN_ID),
            now=NOW,
        )

        self.assertEqual(confirmed.job_id, str(JOB_ID))
        self.assertEqual(session.added, [])
        self.assertEqual(session.execute_statements, [])

    async def test_claim_uses_bounded_attempts_and_reclaims_expired_lease(self) -> None:
        job = _job(
            status="running",
            attempt_count=1,
            lease_token=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            lease_expires_at=NOW - timedelta(seconds=1),
        )
        session = _Session(scalar_results=[job])
        repository = SqlAlchemyArchiveCleanupRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(LEASE_ID),
        )

        claim = await repository.claim(
            job_id=str(JOB_ID), now=NOW, lease_duration=timedelta(minutes=3)
        )

        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.attempt_number, 2)
        self.assertEqual(job.attempt_count, 2)
        self.assertEqual(job.lease_token, LEASE_ID)
        self.assertEqual(job.lease_expires_at, NOW + timedelta(minutes=3))

    async def test_in_use_asset_is_skipped_and_next_item_can_be_selected(self) -> None:
        job = _job()
        skipped = _item(status="pending", deletion_token=None)
        session = _Session(scalar_results=[job, None, skipped, object(), True, None])
        repository = SqlAlchemyArchiveCleanupRepository(_Database(session))  # type: ignore[arg-type]

        selected = await repository.claim_next_item(claim=_claim(), now=NOW)

        self.assertIsNone(selected)
        self.assertEqual(skipped.status, "skipped_in_use")
        self.assertEqual(skipped.skipped_at, NOW)
        usage_sql = _sql(session.scalar_statements[4])
        self.assertIn("stage_runs.status", usage_sql)
        self.assertIn("publication_intents.status", usage_sql)

    async def test_claim_next_item_marks_asset_deleting_and_freezes_token(self) -> None:
        job = _job()
        item = _item(status="pending", deletion_token=None)
        asset = ImageAssetRecord(
            id=ASSET_ID,
            message_id=MESSAGE_ID,
            source_asset_id="source:1",
            archive_state="ready",
            archive_attempt_count=1,
            archive_max_attempts=5,
            storage_backend="archive",
            storage_key="image.webp",
            content_type="image/webp",
            width=10,
            height=20,
            source_sha256="a" * 64,
            archive_sha256="b" * 64,
            perceptual_hash="p",
            archive_size_bytes=37,
            archive_ready_at=NOW,
        )
        session = _Session(scalar_results=[job, None, item, asset, False])
        repository = SqlAlchemyArchiveCleanupRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(DELETION_ID),
        )

        claimed = await repository.claim_next_item(claim=_claim(), now=NOW)

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.deletion_token, str(DELETION_ID))
        self.assertEqual(item.status, "deleting")
        self.assertEqual(asset.archive_state, "deleting")
        self.assertEqual(asset.cleanup_job_id, JOB_ID)
        self.assertEqual(asset.previous_archive_size, 37)

    async def test_stale_deletion_token_cannot_finalize(self) -> None:
        stale = ClaimedCleanupItem(
            item_id=str(ITEM_ID),
            deletion_token=str(uuid4()),
            asset=CleanupAsset(
                asset_kind="image",
                asset_id=str(ASSET_ID),
                message_id=str(MESSAGE_ID),
                blobs=(CleanupBlob("archive", "image.webp", 37),),
                previous_archive_size=37,
            ),
        )
        session = _Session(scalar_results=[_job(), None])
        repository = SqlAlchemyArchiveCleanupRepository(_Database(session))  # type: ignore[arg-type]

        finalized = await repository.finalize_item_deleted(claim=_claim(), item=stale, now=NOW)

        self.assertFalse(finalized)

    async def test_finalize_deletes_only_blob_lifecycle_metadata_and_preserves_evidence(
        self,
    ) -> None:
        job = _job()
        item = _item()
        asset = ImageAssetRecord(
            id=ASSET_ID,
            message_id=MESSAGE_ID,
            source_asset_id="source:1",
            source_phash="source-phash",
            perceptual_hash="archive-phash",
            archive_state="deleting",
            archive_attempt_count=1,
            archive_max_attempts=5,
            storage_backend="archive",
            storage_key="image.webp",
            content_type="image/webp",
            width=10,
            height=20,
            source_sha256="a" * 64,
            archive_sha256="b" * 64,
            archive_size_bytes=37,
            archive_ready_at=NOW - timedelta(days=1),
            cleanup_job_id=JOB_ID,
            previous_archive_size=37,
        )
        session = _Session(scalar_results=[job, item, asset])
        repository = SqlAlchemyArchiveCleanupRepository(_Database(session))  # type: ignore[arg-type]

        finalized = await repository.finalize_item_deleted(
            claim=_claim(),
            item=ClaimedCleanupItem(
                item_id=str(ITEM_ID),
                deletion_token=str(DELETION_ID),
                asset=CleanupAsset(
                    asset_kind="image",
                    asset_id=str(ASSET_ID),
                    message_id=str(MESSAGE_ID),
                    blobs=(CleanupBlob("archive", "image.webp", 37),),
                    previous_archive_size=37,
                ),
            ),
            now=NOW,
        )

        self.assertTrue(finalized)
        self.assertEqual(asset.archive_state, "deleted")
        self.assertEqual(asset.archive_deleted_at, NOW)
        self.assertEqual(asset.storage_backend, "archive")
        self.assertEqual(asset.storage_key, "image.webp")
        self.assertEqual(asset.archive_size_bytes, 37)
        self.assertEqual(asset.source_sha256, "a" * 64)
        self.assertEqual(asset.archive_sha256, "b" * 64)
        self.assertEqual(asset.perceptual_hash, "archive-phash")
        self.assertEqual(item.status, "deleted")
        self.assertIsNone(item.deletion_token)

    async def test_retry_keeps_deleting_item_replayable_and_terminal_failure_is_audited(
        self,
    ) -> None:
        retry_job = _job()
        retry_item = _item()
        retry_session = _Session(scalar_results=[retry_job, retry_item])
        retry_repository = SqlAlchemyArchiveCleanupRepository(_Database(retry_session))  # type: ignore[arg-type]
        claimed_item = ClaimedCleanupItem(
            item_id=str(ITEM_ID),
            deletion_token=str(DELETION_ID),
            asset=CleanupAsset(
                asset_kind="image",
                asset_id=str(ASSET_ID),
                message_id=str(MESSAGE_ID),
                blobs=(CleanupBlob("archive", "image.webp", 37),),
                previous_archive_size=37,
            ),
        )

        retried = await retry_repository.retry_or_fail(
            claim=_claim(),
            item=claimed_item,
            error_code="DELETE_FAILED",
            error_type="OSError",
            retryable=True,
            retry_at=NOW + timedelta(minutes=1),
            now=NOW,
        )

        self.assertTrue(retried)
        self.assertEqual(retry_job.status, "retry_wait")
        self.assertEqual(retry_item.status, "deleting")
        self.assertEqual(retry_item.deletion_token, DELETION_ID)

        failed_job = _job(attempt_count=3, max_attempts=3)
        failed_item = _item()
        failed_session = _Session(scalar_results=[failed_job, failed_item])
        failed_repository = SqlAlchemyArchiveCleanupRepository(_Database(failed_session))  # type: ignore[arg-type]
        failed = await failed_repository.retry_or_fail(
            claim=ClaimedArchiveCleanupJob(
                job_id=str(JOB_ID),
                lease_token=str(LEASE_ID),
                attempt_number=3,
                max_attempts=3,
            ),
            item=claimed_item,
            error_code="DELETE_FAILED",
            error_type="OSError",
            retryable=True,
            retry_at=NOW + timedelta(minutes=1),
            now=NOW,
        )

        self.assertTrue(failed)
        self.assertEqual(failed_job.status, "failed")
        self.assertEqual(failed_item.status, "failed")
        self.assertIsNone(failed_item.deletion_token)
        self.assertEqual(failed_item.last_error_code, "DELETE_FAILED")

    async def test_repair_recreates_only_uuid_maintenance_wakeup(self) -> None:
        pending = _job(status="pending", attempt_count=0, lease_token=None, lease_expires_at=None)
        session = _Session(
            scalar_batches=[(pending,)],
            scalar_results=[WAKEUP_ID],
        )
        repository = SqlAlchemyArchiveCleanupRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(WAKEUP_ID),
        )

        repaired = await repository.repair_wakeups(now=NOW, limit=10)

        self.assertEqual(repaired, 1)
        statement = session.scalar_statements[0]
        sql = _sql(statement)
        params = _params(statement)
        self.assertIn("INSERT INTO durable_wakeups", sql)
        self.assertIn("ON CONFLICT (queue, entity_id) DO UPDATE", sql)
        self.assertEqual(params["queue"], CLEANUP_QUEUE)
        self.assertEqual(params["entity_id"], JOB_ID)


if __name__ == "__main__":
    unittest.main()
