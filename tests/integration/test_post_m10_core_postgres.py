"""Post-M10 native PostgreSQL 16 integration regressions for core domain/DB invariants."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.postgres_support import migrate_database, temporary_postgres_database
from tgcurator.application.ports.analysis import AnalysisCacheHit
from tgcurator.application.ports.cleanup import ArchiveCleanupFilter
from tgcurator.domain.analysis import (
    CachePolicy,
    InputManifest,
    LabelScope,
    ResolvedLabel,
    StageCacheKey,
    ValidatedTargetResult,
    canonical_hash,
)
from tgcurator.domain.messages import (
    DuplicateMatchResult,
    DuplicatePolicy,
    DuplicateStatus,
    MediaAsset,
    MediaKind,
    TelegramMessagePart,
    normalized_from_parts,
)
from tgcurator.domain.publishing import PublicationOperation, PublicationPart
from tgcurator.infrastructure.database.analysis_repository import (
    SqlAlchemyAnalysisStageRunRepository,
)
from tgcurator.infrastructure.database.base import Base
from tgcurator.infrastructure.database.cleanup_repository import SqlAlchemyArchiveCleanupRepository
from tgcurator.infrastructure.database.consistency_repository import (
    SqlAlchemyArchiveConsistencyRepository,
)
from tgcurator.infrastructure.database.message_ingest_repository import (
    SqlAlchemyTelegramMessageIngestRepository,
)
from tgcurator.infrastructure.database.models import (
    AdminUser,
    AnalysisPipelineRecord,
    AnalysisPipelineVersionRecord,
    AnalysisRunRecord,
    AnalysisStageTemplateRecord,
    AnalysisStageTemplateVersionRecord,
    AuditEvent,
    DestinationChannel,
    DuplicateMatchRecord,
    DurableWakeupRecord,
    EncryptedSecret,
    ImageAssetRecord,
    InferenceProfileRecord,
    InferenceProfileVersionRecord,
    InputManifestRecord,
    LabelBindingRecord,
    LabelDefinitionRecord,
    LabelDefinitionVersionRecord,
    LabelSetRecord,
    LabelSetVersionRecord,
    ManualLabelAssignmentRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    PipelineStageNodeRecord,
    PromptTemplateRecord,
    PromptTemplateVersionRecord,
    PublicationIntentRecord,
    PublishAttemptRecord,
    PublishRecord,
    RenderingTemplateRecord,
    RenderingTemplateVersionRecord,
    RoutingActionRecord,
    RoutingEvaluationRecord,
    RoutingPolicyRecord,
    RoutingPolicyVersionRecord,
    RoutingRuleRecord,
    ServiceHeartbeatRecord,
    SourceChannel,
    StageRunRecord,
    TelegramIdentity,
)
from tgcurator.infrastructure.database.pipeline_repository import (
    SqlAlchemyMessagePipelineRepository,
)
from tgcurator.infrastructure.database.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from tgcurator.infrastructure.database.reconciliation_repository import (
    SqlAlchemySourceReconciliationCursorRepository,
)
from tgcurator.infrastructure.database.session import AsyncDatabase
from tgcurator.infrastructure.database.source_lifecycle_repository import (
    SqlAlchemySourceMessageLifecycleRepository,
)

ROOT = Path(__file__).resolve().parents[2]
M5 = "d7b3f9a1e6c2"
M6 = "e6d4a8b2c951"
HEAD = "b9c3e5f7a106"
NOW = datetime(2026, 11, 13, 8, 0, tzinfo=UTC)


def _alembic_cmd(database_url: str, *args: str) -> None:
    env = os.environ.copy()
    env.update(TGCURATOR_DATABASE_URL=database_url, TGCURATOR_ENVIRONMENT="test")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        output = result.stdout.replace(database_url, "[TEST_DATABASE_URL]")
        raise AssertionError(f"alembic {' '.join(args)} failed:\n{output}")


async def _seed_core_entities(db: AsyncDatabase) -> dict[str, UUID]:
    ids = {
        name: uuid4()
        for name in (
            "admin",
            "secret",
            "identity",
            "source",
            "destination",
        )
    }
    async with db.session() as session:
        async with session.begin():
            session.add(
                AdminUser(
                    id=ids["admin"],
                    username=f"admin-{ids['admin'].hex[:8]}",
                    password_hash="argon2id$test",
                )
            )
            session.add(
                EncryptedSecret(
                    id=ids["secret"],
                    secret_type="telegram_session",
                    ciphertext=b"c" * 16,
                    nonce=b"n" * 12,
                    key_id="k1",
                )
            )
            await session.flush()
            session.add(
                TelegramIdentity(
                    id=ids["identity"],
                    name=f"id-{ids['identity'].hex[:8]}",
                    identity_type="mtproto_user",
                    secret_id=ids["secret"],
                )
            )
            await session.flush()
            session.add(
                SourceChannel(
                    id=ids["source"],
                    telegram_peer_id=-100987654,
                    display_name="source-chan",
                    default_read_identity_id=ids["identity"],
                )
            )
            session.add(
                DestinationChannel(
                    id=ids["destination"],
                    name=f"dest-{ids['destination'].hex[:8]}",
                    telegram_peer_id=-100654321,
                    default_publish_identity_id=ids["identity"],
                )
            )
    return ids


def test_migration_chain_m6_deleting_downgrade_base_upgrade_head_and_catalog_parity() -> None:
    with temporary_postgres_database("post_m10_mig", revision=M6) as url:

        async def seed_m6_deleting_rows() -> tuple[UUID, UUID]:
            engine = create_async_engine(url)
            admin_id = uuid4()
            secret_id = uuid4()
            identity_id = uuid4()
            source_id = uuid4()
            msg_id = uuid4()
            img_id = uuid4()
            vid_id = uuid4()
            dry_run_id = uuid4()
            job_id = uuid4()
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO admin_users (id, username, password_hash) "
                            "VALUES (:id, 'm6-down', 'hash')"
                        ),
                        {"id": admin_id},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO encrypted_secrets "
                            "(id, secret_type, ciphertext, nonce, key_id) "
                            "VALUES (:id, 'telegram_session', :ct, :nc, 'k1')"
                        ),
                        {"id": secret_id, "ct": b"x" * 16, "nc": b"y" * 12},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO telegram_identities "
                            "(id, name, identity_type, secret_id) "
                            "VALUES (:id, 'm6-id', 'mtproto_user', :sec)"
                        ),
                        {"id": identity_id, "sec": secret_id},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO source_channels "
                            "(id, telegram_peer_id, display_name, default_read_identity_id) "
                            "VALUES (:id, -100111, 'src', :ident)"
                        ),
                        {"id": source_id, "ident": identity_id},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO messages "
                            "(id, source_channel_id, telegram_message_ids, "
                            "primary_telegram_message_id, published_at, media_count) "
                            "VALUES (:id, :src, ARRAY[1]::bigint[], 1, :now, 2)"
                        ),
                        {"id": msg_id, "src": source_id, "now": NOW},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO archive_cleanup_dry_runs "
                            "(id, actor_admin_user_id, filter_snapshot, filter_hash, "
                            "estimated_asset_count, estimated_message_count, estimated_bytes) "
                            "VALUES (:id, :adm, '{}'::jsonb, :fh, 2, 1, 512)"
                        ),
                        {"id": dry_run_id, "adm": admin_id, "fh": "a" * 64},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO archive_cleanup_jobs "
                            "(id, dry_run_id, confirmed_by_admin_user_id, filter_snapshot, "
                            "filter_hash, estimated_asset_count, estimated_message_count, "
                            "estimated_bytes, item_count, status) "
                            "VALUES (:id, :dr, :adm, '{}'::jsonb, :fh, 2, 1, 512, 2, 'pending')"
                        ),
                        {"id": job_id, "dr": dry_run_id, "adm": admin_id, "fh": "a" * 64},
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO image_assets "
                            "(id, message_id, source_asset_id, archive_state, storage_backend, "
                            "storage_key, content_type, width, height, source_sha256, "
                            "archive_sha256, perceptual_hash, archive_size_bytes, "
                            "archive_ready_at, cleanup_job_id, previous_archive_size) "
                            "VALUES (:id, :msg, 'img-src', 'deleting', 'archive', 'i.webp', "
                            "'image/webp', 32, 32, :s1, :s2, '0123456789abcdef', 128, :now, "
                            ":job, 128)"
                        ),
                        {
                            "id": img_id,
                            "msg": msg_id,
                            "s1": "1" * 64,
                            "s2": "2" * 64,
                            "now": NOW,
                            "job": job_id,
                        },
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO video_assets "
                            "(id, message_id, source_asset_id, archive_state, "
                            "source_content_type, source_size_bytes, source_sha256, "
                            "duration_seconds, source_width, source_height, "
                            "archive_ready_at, cleanup_job_id, previous_archive_size) "
                            "VALUES (:id, :msg, 'vid-src', 'deleting', 'video/mp4', 1024, :s3, "
                            "5.0, 320, 240, :now, :job, 384)"
                        ),
                        {
                            "id": vid_id,
                            "msg": msg_id,
                            "s3": "3" * 64,
                            "now": NOW,
                            "job": job_id,
                        },
                    )
            finally:
                await engine.dispose()
            return img_id, vid_id

        img_id, vid_id = asyncio.run(seed_m6_deleting_rows())

        # Downgrade M6 -> M5 with 'deleting' rows: must normalize cleanly without CheckViolation.
        _alembic_cmd(url, "downgrade", M5)

        async def verify_m5_normalized() -> None:
            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    img_state = await conn.scalar(
                        text("SELECT archive_state FROM image_assets WHERE id = :id"),
                        {"id": img_id},
                    )
                    vid_state = await conn.scalar(
                        text("SELECT archive_state FROM video_assets WHERE id = :id"),
                        {"id": vid_id},
                    )
                    assert img_state == "ready"
                    assert vid_state == "ready"
            finally:
                await engine.dispose()

        asyncio.run(verify_m5_normalized())

        # Full downgrade to base and full upgrade back to HEAD.
        _alembic_cmd(url, "downgrade", "base")

        async def verify_empty_base() -> None:
            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    user_tables = set(
                        await conn.scalars(
                            text(
                                "SELECT tablename FROM pg_tables "
                                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                            )
                        )
                    )
                    assert user_tables == set()
            finally:
                await engine.dispose()

        asyncio.run(verify_empty_base())

        migrate_database(url, HEAD)

        async def verify_head_catalog_parity() -> None:
            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:

                    def inspect_catalog(
                        sync_conn,
                    ) -> tuple[set[str], dict[str, set[str]], set[str]]:
                        insp = inspect(sync_conn)
                        tables = set(insp.get_table_names()) - {"alembic_version"}
                        cols = {
                            table: {col["name"] for col in insp.get_columns(table)}
                            for table in tables
                        }
                        idxs = {
                            idx["name"]
                            for table in tables
                            for idx in insp.get_indexes(table)
                            if idx.get("name")
                        }
                        return tables, cols, idxs

                    live_tables, live_cols, live_indexes = await conn.run_sync(inspect_catalog)
                    expected_tables = set(Base.metadata.tables.keys())
                    assert live_tables == expected_tables
                    for table_name, table_obj in Base.metadata.tables.items():
                        assert live_cols[table_name] == {col.name for col in table_obj.columns}, (
                            f"column mismatch on {table_name}"
                        )
                    expected_indexes = {
                        idx.name
                        for table_obj in Base.metadata.tables.values()
                        for idx in table_obj.indexes
                        if idx.name
                    }
                    assert expected_indexes <= live_indexes

                    triggers = set(
                        await conn.scalars(
                            text(
                                "SELECT tgname FROM pg_trigger "
                                "WHERE NOT tgisinternal AND tgenabled = 'O'"
                            )
                        )
                    )
                    assert {
                        "trg_audit_events_append_only",
                        "trg_publish_records_append_only",
                        "trg_m7_reopen_album_components",
                        "trg_publication_intents_identity_immutable",
                    } <= triggers
            finally:
                await engine.dispose()

        asyncio.run(verify_head_catalog_parity())


def test_append_only_triggers_and_service_heartbeats_persistence() -> None:
    with temporary_postgres_database("post_m10_triggers", revision=HEAD) as url:

        async def exercise() -> None:
            db = AsyncDatabase(url)
            try:
                ids = await _seed_core_entities(db)
                audit_id = uuid4()
                msg_id = uuid4()
                policy_id = uuid4()
                policy_ver_id = uuid4()
                rule_id = uuid4()
                action_id = uuid4()
                eval_id = uuid4()
                intent_id = uuid4()
                attempt_id = uuid4()
                pub_record_id = uuid4()

                async with db.session() as session:
                    async with session.begin():
                        session.add(
                            ServiceHeartbeatRecord(
                                service_name="worker",
                                database_ready=True,
                                broker_ready=True,
                                last_heartbeat_at=NOW,
                                updated_at=NOW,
                            )
                        )
                        session.add(
                            AuditEvent(
                                id=audit_id,
                                actor_admin_user_id=ids["admin"],
                                event_type="test.created",
                                entity_type="test_entity",
                                entity_id=uuid4(),
                                payload={"ok": True},
                                created_at=NOW,
                            )
                        )
                        session.add(
                            MessageRecord(
                                id=msg_id,
                                source_channel_id=ids["source"],
                                telegram_message_ids=[10],
                                primary_telegram_message_id=10,
                                published_at=NOW,
                                media_count=0,
                            )
                        )
                        session.add(RoutingPolicyRecord(id=policy_id, name="pol-append"))
                        await session.flush()
                        session.add(
                            RoutingPolicyVersionRecord(
                                id=policy_ver_id,
                                routing_policy_id=policy_id,
                                version_number=1,
                            )
                        )
                        await session.flush()
                        session.add(
                            RoutingRuleRecord(
                                id=rule_id,
                                routing_policy_version_id=policy_ver_id,
                                priority=1,
                                condition={},
                            )
                        )
                        await session.flush()
                        session.add(
                            RoutingActionRecord(
                                id=action_id,
                                routing_rule_id=rule_id,
                                destination_channel_id=ids["destination"],
                                publication_mode="forward_only",
                                output_order=0,
                            )
                        )
                        session.add(
                            RoutingEvaluationRecord(
                                id=eval_id,
                                routing_request_id="req-append",
                                message_id=msg_id,
                                routing_policy_version_id=policy_ver_id,
                                facts_snapshot={},
                                facts_hash="a" * 64,
                                rule_outcomes=[],
                            )
                        )
                        await session.flush()
                        session.add(
                            PublicationIntentRecord(
                                id=intent_id,
                                routing_evaluation_id=eval_id,
                                routing_request_id="req-append",
                                message_id=msg_id,
                                routing_policy_version_id=policy_ver_id,
                                routing_rule_id=rule_id,
                                routing_action_id=action_id,
                                destination_channel_id=ids["destination"],
                                publish_identity_id=ids["identity"],
                                publication_mode="forward_only",
                                business_idempotency_key="idem-append",
                            )
                        )
                        await session.flush()
                        session.add(
                            PublishAttemptRecord(
                                id=attempt_id,
                                publication_intent_id=intent_id,
                                attempt_number=1,
                                lease_token=uuid4(),
                                status="sent",
                                started_at=NOW,
                                completed_at=NOW,
                            )
                        )
                        await session.flush()
                        session.add(
                            PublishRecord(
                                id=pub_record_id,
                                publication_intent_id=intent_id,
                                publish_attempt_id=attempt_id,
                                part="main",
                                operation="native_forward",
                                remote_message_ids=[501],
                                random_ids=[999],
                                sent_at=NOW,
                            )
                        )

                async with db.session() as session:
                    async with session.begin():
                        with pytest.raises(DBAPIError, match="audit events are append-only"):
                            async with session.begin_nested():
                                await session.execute(
                                    text(
                                        "UPDATE audit_events SET event_type = 'tampered' "
                                        "WHERE id = :id"
                                    ),
                                    {"id": audit_id},
                                )
                        with pytest.raises(DBAPIError, match="audit events are append-only"):
                            async with session.begin_nested():
                                await session.execute(
                                    text("DELETE FROM audit_events WHERE id = :id"),
                                    {"id": audit_id},
                                )
                        with pytest.raises(DBAPIError, match="publish records are append-only"):
                            async with session.begin_nested():
                                await session.execute(
                                    text(
                                        "UPDATE publish_records SET reconciled = true "
                                        "WHERE id = :id"
                                    ),
                                    {"id": pub_record_id},
                                )
                        with pytest.raises(DBAPIError, match="publish records are append-only"):
                            async with session.begin_nested():
                                await session.execute(
                                    text("DELETE FROM publish_records WHERE id = :id"),
                                    {"id": pub_record_id},
                                )
                        with pytest.raises(IntegrityError):
                            async with session.begin_nested():
                                session.add(
                                    ServiceHeartbeatRecord(
                                        service_name="   ",
                                        database_ready=True,
                                        broker_ready=False,
                                        last_heartbeat_at=NOW,
                                        updated_at=NOW,
                                    )
                                )
                                await session.flush()
            finally:
                await db.dispose()

        asyncio.run(exercise())


def test_ingest_activity_cursors_lifecycle_deferral_consistency_and_wakeup_repair() -> None:
    with temporary_postgres_database("post_m10_ingest", revision=HEAD) as url:

        async def exercise() -> None:
            db = AsyncDatabase(url)
            try:
                ids = await _seed_core_entities(db)
                ingest_repo = SqlAlchemyTelegramMessageIngestRepository(db)
                cursor_repo = SqlAlchemySourceReconciliationCursorRepository(db)
                lifecycle_repo = SqlAlchemySourceMessageLifecycleRepository(
                    db, defer_media_wakeups=True
                )
                pipeline_repo = SqlAlchemyMessagePipelineRepository(db)
                consistency_repo = SqlAlchemyArchiveConsistencyRepository(database=db, batch_size=2)

                source_str = str(ids["source"])
                msg_1 = normalized_from_parts(
                    source_channel_id=source_str,
                    telegram_grouped_id=None,
                    parts=(
                        TelegramMessagePart(
                            telegram_message_id=20,
                            published_at=NOW,
                            original_text="first post",
                        ),
                    ),
                )
                await ingest_repo.upsert_message(
                    message=msg_1,
                    defer_media_wakeups=False,
                )

                # Earlier message must NOT regress last_activity_at or latest_seen_message_id.
                msg_older = normalized_from_parts(
                    source_channel_id=source_str,
                    telegram_grouped_id=None,
                    parts=(
                        TelegramMessagePart(
                            telegram_message_id=12,
                            published_at=NOW - timedelta(minutes=10),
                            original_text="older backfill",
                        ),
                    ),
                )
                await ingest_repo.upsert_message(message=msg_older)

                async with db.session() as session:
                    src = await session.get(SourceChannel, ids["source"])
                    assert src is not None
                    assert src.last_activity_at == NOW
                    assert src.latest_seen_message_id == 20

                assert await cursor_repo.advance_last_seen_message_id(
                    source_channel_id=source_str, telegram_message_id=35
                )
                assert (
                    await cursor_repo.get_latest_seen_message_id(source_channel_id=source_str) == 35
                )
                assert not await cursor_repo.advance_latest_seen_message_id(
                    source_channel_id=source_str, telegram_message_id=25
                )

                # Mark message 20 processed, then edit -> source_changed_after_processing=True
                # and verify idempotent replay preserves source_changed_after_processing=True.
                async with db.session() as session:
                    async with session.begin():
                        row = await session.scalar(
                            select(MessageRecord).where(
                                MessageRecord.source_channel_id == ids["source"],
                                MessageRecord.primary_telegram_message_id == 20,
                            )
                        )
                        assert row is not None
                        msg_20_id = row.id
                        row.processing_status = "processed"

                edited_msg = normalized_from_parts(
                    source_channel_id=source_str,
                    telegram_grouped_id=None,
                    parts=(
                        TelegramMessagePart(
                            telegram_message_id=20,
                            published_at=NOW,
                            edited_at=NOW + timedelta(minutes=2),
                            original_text="edited post",
                        ),
                    ),
                )
                await ingest_repo.upsert_message(message=edited_msg)
                await ingest_repo.upsert_message(message=edited_msg)

                async with db.session() as session:
                    row = await session.get(MessageRecord, msg_20_id)
                    assert row is not None
                    assert row.source_changed_after_processing is True

                # Lifecycle edit with defer_media_wakeups=True creates pending media rows
                # without wakeups; repair_wakeups reconstructs both pending image & video wakeups.
                assert await lifecycle_repo.record_edit(
                    source_channel_id=source_str,
                    telegram_message_id=20,
                    original_text="edited with media",
                    telegram_metadata={},
                    media=(
                        MediaAsset(
                            kind=MediaKind.IMAGE,
                            asset_id="tg-photo-20",
                            source_telegram_message_id=20,
                        ),
                        MediaAsset(
                            kind=MediaKind.VIDEO,
                            asset_id="tg-video-20",
                            source_telegram_message_id=20,
                        ),
                    ),
                    edited_at=NOW + timedelta(minutes=5),
                )

                async with db.session() as session:
                    wakeups_before = tuple(await session.scalars(select(DurableWakeupRecord)))
                    assert wakeups_before == ()

                report = await pipeline_repo.repair_wakeups(now=NOW + timedelta(minutes=6))
                assert report.repaired_image_archives == 1
                assert report.repaired_video_archives == 1
                async with db.session() as session:
                    queues = {
                        w.queue
                        for w in await session.scalars(
                            select(DurableWakeupRecord).where(
                                DurableWakeupRecord.status == "pending"
                            )
                        )
                    }
                    assert queues == {"image_archive", "video_archive"}

                blobs = await consistency_repo.list_archive_blob_records(storage_backend="archive")
                assert blobs == ()
            finally:
                await db.dispose()

        asyncio.run(exercise())


def test_album_reopen_and_visual_dedup_remove_stale_duplicate_matches() -> None:
    with temporary_postgres_database("post_m10_dedup", revision=HEAD) as url:

        async def exercise() -> None:
            db = AsyncDatabase(url)
            try:
                ids = await _seed_core_entities(db)
                ingest_repo = SqlAlchemyTelegramMessageIngestRepository(db)
                pipeline_repo = SqlAlchemyMessagePipelineRepository(db)
                source_str = str(ids["source"])

                await ingest_repo.upsert_message(
                    message=normalized_from_parts(
                        source_channel_id=source_str,
                        telegram_grouped_id=None,
                        parts=(
                            TelegramMessagePart(
                                telegram_message_id=1,
                                published_at=NOW - timedelta(minutes=5),
                                original_text="canonical",
                            ),
                        ),
                    )
                )
                await ingest_repo.upsert_message(
                    message=normalized_from_parts(
                        source_channel_id=source_str,
                        telegram_grouped_id=777,
                        parts=(
                            TelegramMessagePart(
                                telegram_message_id=50,
                                published_at=NOW,
                                original_text="album p1",
                            ),
                        ),
                    )
                )

                async with db.session() as session:
                    async with session.begin():
                        canon_row = await session.scalar(
                            select(MessageRecord).where(
                                MessageRecord.source_channel_id == ids["source"],
                                MessageRecord.primary_telegram_message_id == 1,
                            )
                        )
                        album = await session.scalar(
                            select(MessageRecord).where(
                                MessageRecord.source_channel_id == ids["source"],
                                MessageRecord.primary_telegram_message_id == 50,
                            )
                        )
                        assert canon_row is not None and album is not None
                        canon_id = canon_row.id
                        album_id = album.id
                        album.duplicate_status = "duplicate_skipped"
                        album.canonical_message_id = canon_id
                        album.duplicate_distance = 0
                        album.processing_status = "processed"
                        album.terminal_reason = "duplicate_skipped"
                        session.add(
                            DuplicateMatchRecord(
                                id=uuid4(),
                                message_id=album_id,
                                canonical_message_id=canon_id,
                                match_kind="exact_fingerprint",
                                hamming_distance=0,
                                duplicate_policy="skip",
                                created_at=NOW,
                            )
                        )

                # Appending late component 51 fires trg_m7_reopen_album_components
                # and deletes stale duplicate_matches.
                await ingest_repo.upsert_message(
                    message=normalized_from_parts(
                        source_channel_id=source_str,
                        telegram_grouped_id=777,
                        parts=(
                            TelegramMessagePart(
                                telegram_message_id=50,
                                published_at=NOW,
                                original_text="album p1",
                            ),
                            TelegramMessagePart(
                                telegram_message_id=51,
                                published_at=NOW,
                                original_text="album p2",
                            ),
                        ),
                    )
                )

                async with db.session() as session:
                    reopened = await session.get(MessageRecord, album_id)
                    assert reopened is not None
                    assert reopened.duplicate_status == "unchecked"
                    assert reopened.canonical_message_id is None
                    stale_match = await session.scalar(
                        select(DuplicateMatchRecord).where(
                            DuplicateMatchRecord.message_id == album_id
                        )
                    )
                    assert stale_match is None

                # Verify record_visual_deduplication(UNIQUE) deletes pre-existing match rows.
                async with db.session() as session:
                    async with session.begin():
                        session.add(
                            DuplicateMatchRecord(
                                id=uuid4(),
                                message_id=album_id,
                                canonical_message_id=canon_id,
                                match_kind="perceptual_multiset",
                                hamming_distance=2,
                                duplicate_policy="reclassify_with_text",
                                created_at=NOW,
                            )
                        )

                await pipeline_repo.record_visual_deduplication(
                    message_id=str(album_id),
                    visual_fingerprint="f" * 64,
                    match=DuplicateMatchResult(
                        status=DuplicateStatus.UNIQUE,
                        canonical_message_id=None,
                        match_kind=None,
                        hamming_distance=None,
                        duplicate_policy=DuplicatePolicy.SKIP,
                    ),
                    now=NOW + timedelta(seconds=10),
                )

                async with db.session() as session:
                    remaining = await session.scalar(
                        select(DuplicateMatchRecord).where(
                            DuplicateMatchRecord.message_id == album_id
                        )
                    )
                    assert remaining is None
            finally:
                await db.dispose()

        asyncio.run(exercise())


def test_batch_cache_hit_per_target_label_isolation_and_formal_multistage_dag_completion() -> None:
    with temporary_postgres_database("post_m10_analysis", revision=HEAD) as url:

        async def exercise() -> None:
            db = AsyncDatabase(url)
            try:
                ids = await _seed_core_entities(db)
                stage_repo = SqlAlchemyAnalysisStageRunRepository(db)

                keep_def_id, keep_ver_id = uuid4(), uuid4()
                block_def_id, block_ver_id = uuid4(), uuid4()
                ls_id, lsv_id = uuid4(), uuid4()
                pt_id, ptv_id = uuid4(), uuid4()
                ip_id, ipv_id = uuid4(), uuid4()
                st1_id, st1_v_id = uuid4(), uuid4()
                st2_id, st2_v_id = uuid4(), uuid4()
                pipe_id, pipe_v_id = uuid4(), uuid4()
                node1_id, node2_id = uuid4(), uuid4()
                donor_a, donor_b = uuid4(), uuid4()
                recip_1, recip_2 = uuid4(), uuid4()
                run_da, run_db, run_r1, run_r2 = uuid4(), uuid4(), uuid4(), uuid4()
                sr_da_1, sr_db_1 = uuid4(), uuid4()
                sr_r1_1, sr_r2_1 = uuid4(), uuid4()

                async with db.session() as session:
                    async with session.begin():
                        session.add_all(
                            [
                                LabelDefinitionRecord(id=keep_def_id, name="Keep"),
                                LabelDefinitionRecord(id=block_def_id, name="Block"),
                                LabelSetRecord(id=ls_id, name="ls-batch"),
                                PromptTemplateRecord(id=pt_id, name="pt-batch"),
                                InferenceProfileRecord(id=ip_id, name="ip-batch"),
                                AnalysisStageTemplateRecord(id=st1_id, name="stage-1"),
                                AnalysisStageTemplateRecord(id=st2_id, name="stage-2"),
                                AnalysisPipelineRecord(id=pipe_id, name="pipe-dag"),
                            ]
                        )
                        await session.flush()
                        lsv_row = LabelSetVersionRecord(
                            id=lsv_id,
                            label_set_id=ls_id,
                            version_number=1,
                            state="draft",
                        )
                        pipe_v_row = AnalysisPipelineVersionRecord(
                            id=pipe_v_id,
                            analysis_pipeline_id=pipe_id,
                            version_number=1,
                            state="draft",
                        )
                        session.add_all(
                            [
                                LabelDefinitionVersionRecord(
                                    id=keep_ver_id,
                                    label_definition_id=keep_def_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW,
                                    key="keep",
                                    display_name="Keep",
                                    description="Keep",
                                    scope="global",
                                    negative=False,
                                ),
                                LabelDefinitionVersionRecord(
                                    id=block_ver_id,
                                    label_definition_id=block_def_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW,
                                    key="block",
                                    display_name="Block",
                                    description="Block",
                                    scope="global",
                                    negative=True,
                                ),
                                lsv_row,
                                PromptTemplateVersionRecord(
                                    id=ptv_id,
                                    prompt_template_id=pt_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW,
                                    system_prompt="Classify {{ label_definitions }}",
                                    user_prompt_template="Msg {{ message_text }}",
                                    declared_variables=["label_definitions", "message_text"],
                                ),
                                InferenceProfileVersionRecord(
                                    id=ipv_id,
                                    inference_profile_id=ip_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW,
                                    provider_adapter="openai_compatible",
                                    base_url="https://api.example.com/v1",
                                    model_name="qwen-vl",
                                    api_secret_reference=str(ids["secret"]),
                                    capabilities=["text"],
                                    timeout_seconds=30,
                                    max_concurrency=2,
                                    max_images=4,
                                    max_input_tokens=4096,
                                    structured_output_support=True,
                                    sampling_parameters={},
                                ),
                                pipe_v_row,
                            ]
                        )
                        await session.flush()
                        session.add_all(
                            [
                                LabelBindingRecord(
                                    id=uuid4(),
                                    label_set_version_id=lsv_id,
                                    label_definition_version_id=keep_ver_id,
                                    activation_threshold=0.5,
                                    output_order=0,
                                ),
                                LabelBindingRecord(
                                    id=uuid4(),
                                    label_set_version_id=lsv_id,
                                    label_definition_version_id=block_ver_id,
                                    activation_threshold=0.8,
                                    output_order=1,
                                ),
                            ]
                        )
                        await session.flush()
                        lsv_row.state = "published"
                        lsv_row.published_at = NOW
                        await session.flush()

                        session.add_all(
                            [
                                AnalysisStageTemplateVersionRecord(
                                    id=st1_v_id,
                                    analysis_stage_template_id=st1_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW,
                                    name="batch_prescreen",
                                    target_scope="global",
                                    execution_mode="batch_messages",
                                    prompt_template_version_id=ptv_id,
                                    label_set_version_id=lsv_id,
                                    structured_output_policy={},
                                    input_policy={"text": True, "media": False},
                                    visual_composition_policy="adaptive",
                                    inference_profile_version_id=ipv_id,
                                    cache_policy="message",
                                    timeout_seconds=20,
                                    retry_policy={"max_attempts": 3},
                                    max_batch_size=4,
                                    max_concurrency=2,
                                ),
                                AnalysisStageTemplateVersionRecord(
                                    id=st2_v_id,
                                    analysis_stage_template_id=st2_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW,
                                    name="second_stage",
                                    target_scope="global",
                                    execution_mode="single_message",
                                    prompt_template_version_id=ptv_id,
                                    label_set_version_id=lsv_id,
                                    structured_output_policy={},
                                    input_policy={"text": True, "media": False},
                                    visual_composition_policy="adaptive",
                                    inference_profile_version_id=ipv_id,
                                    cache_policy="none",
                                    timeout_seconds=20,
                                    retry_policy={"max_attempts": 3},
                                    max_batch_size=1,
                                    max_concurrency=2,
                                ),
                            ]
                        )
                        await session.flush()
                        session.add_all(
                            [
                                PipelineStageNodeRecord(
                                    id=node1_id,
                                    analysis_pipeline_version_id=pipe_v_id,
                                    node_key="prescreen",
                                    output_order=0,
                                    analysis_stage_template_version_id=st1_v_id,
                                    depends_on=[],
                                    run_if=None,
                                ),
                                PipelineStageNodeRecord(
                                    id=node2_id,
                                    analysis_pipeline_version_id=pipe_v_id,
                                    node_key="deep_text",
                                    output_order=1,
                                    analysis_stage_template_version_id=st2_v_id,
                                    depends_on=["prescreen"],
                                    run_if=None,
                                ),
                            ]
                        )
                        await session.flush()
                        pipe_v_row.state = "published"
                        pipe_v_row.published_at = NOW
                        await session.flush()

                        for idx, (m_id, text_val) in enumerate(
                            (
                                (donor_a, "good article"),
                                (donor_b, "spam link"),
                                (recip_1, "good article"),
                                (recip_2, "spam link"),
                            ),
                            start=1,
                        ):
                            session.add(
                                MessageRecord(
                                    id=m_id,
                                    source_channel_id=ids["source"],
                                    telegram_message_ids=[idx],
                                    primary_telegram_message_id=idx,
                                    published_at=NOW,
                                    original_text=text_val,
                                    media_count=0,
                                    processing_status="processing",
                                )
                            )
                        await session.flush()
                        for r_id, m_id in (
                            (run_da, donor_a),
                            (run_db, donor_b),
                            (run_r1, recip_1),
                            (run_r2, recip_2),
                        ):
                            session.add(
                                AnalysisRunRecord(
                                    id=r_id,
                                    message_id=m_id,
                                    analysis_pipeline_version_id=pipe_v_id,
                                    run_mode="formal",
                                    status="pending",
                                    facts_snapshot={},
                                )
                            )
                        await session.flush()
                        for sr_id, r_id, m_id in (
                            (sr_da_1, run_da, donor_a),
                            (sr_db_1, run_db, donor_b),
                        ):
                            session.add(
                                StageRunRecord(
                                    id=sr_id,
                                    analysis_run_id=r_id,
                                    pipeline_stage_node_id=node1_id,
                                    analysis_stage_template_version_id=st1_v_id,
                                    message_id=m_id,
                                    target_scope="global",
                                    target_kind="message",
                                    target_id=m_id,
                                    status="pending",
                                    attempt_count=0,
                                    max_attempts=3,
                                    created_at=NOW,
                                    updated_at=NOW,
                                )
                            )

                # 1. Claim batch (donor_a + co-leased donor_b) and commit distinct labels.
                claim_donor = await stage_repo.claim(
                    stage_run_id=str(sr_da_1),
                    now=NOW,
                    lease_duration=timedelta(minutes=2),
                )
                assert claim_donor is not None
                snap_donor = await stage_repo.load_stage_snapshot(claim=claim_donor)
                assert [t.target_id for t in snap_donor.targets] == [str(donor_a), str(donor_b)]

                cache_key = StageCacheKey(
                    policy=CachePolicy.MESSAGE,
                    scope_identity="batch:good+spam",
                    value="c" * 64,
                )
                donor_manifest_doc = {
                    "manifest_version": 2,
                    "target_ids": [str(donor_a), str(donor_b)],
                    "targets": [],
                }
                manifest = InputManifest(
                    manifest_version=2,
                    content_json=json.dumps(donor_manifest_doc, separators=(",", ":")),
                    input_manifest_hash=canonical_hash(donor_manifest_doc),
                )
                started = await stage_repo.start_inference(
                    snapshot=snap_donor,
                    manifest=manifest,
                    response_schema={"type": "object"},
                    response_schema_hash="d" * 64,
                    cache_key=cache_key,
                    request_summary={"ok": True},
                    now=NOW,
                )
                assert started is not None
                committed_donor = await stage_repo.commit_validated_targets(
                    snapshot=snap_donor,
                    started=started,
                    results=(
                        ValidatedTargetResult(
                            target_id=str(donor_a),
                            labels=(
                                ResolvedLabel(
                                    label_definition_version_id=str(keep_ver_id),
                                    key="keep",
                                    scope=LabelScope.GLOBAL,
                                    score=0.9,
                                    activated=True,
                                    negative=False,
                                ),
                                ResolvedLabel(
                                    label_definition_version_id=str(block_ver_id),
                                    key="block",
                                    scope=LabelScope.GLOBAL,
                                    score=0.05,
                                    activated=False,
                                    negative=True,
                                ),
                            ),
                            reason="clean",
                            evidence=(),
                        ),
                        ValidatedTargetResult(
                            target_id=str(donor_b),
                            labels=(
                                ResolvedLabel(
                                    label_definition_version_id=str(keep_ver_id),
                                    key="keep",
                                    scope=LabelScope.GLOBAL,
                                    score=0.1,
                                    activated=False,
                                    negative=False,
                                ),
                                ResolvedLabel(
                                    label_definition_version_id=str(block_ver_id),
                                    key="block",
                                    scope=LabelScope.GLOBAL,
                                    score=0.95,
                                    activated=True,
                                    negative=True,
                                ),
                            ),
                            reason="spam",
                            evidence=(),
                        ),
                    ),
                    now=NOW,
                )
                assert len(committed_donor.negative_gate_assignments) == 1
                await stage_repo.apply_negative_gate(
                    analysis_run_id=str(run_db),
                    assignments=committed_donor.negative_gate_assignments,
                    now=NOW,
                )

                # B8: donor_a finished node1, but node2 in the formal DAG is not scheduled yet!
                # complete_analysis_run MUST NOT mark run_da 'completed' prematurely.
                assert await stage_repo.complete_analysis_run(
                    analysis_run_id=str(run_da),
                    now=NOW,
                )
                async with db.session() as session:
                    da_run_row = await session.get(AnalysisRunRecord, run_da)
                    assert da_run_row is not None
                    assert da_run_row.status == "running"
                    assert da_run_row.completed_at is None

                # Schedule & complete node2 for donor_a -> complete_analysis_run marks 'completed'.
                sr_da_2 = uuid4()
                man2_id = uuid4()
                async with db.session() as session:
                    async with session.begin():
                        session.add(
                            InputManifestRecord(
                                id=man2_id,
                                analysis_stage_template_version_id=st2_v_id,
                                manifest_version=2,
                                content={"manifest_version": 2, "targets": []},
                                input_manifest_hash="e" * 64,
                                created_at=NOW,
                            )
                        )
                        await session.flush()
                        session.add(
                            StageRunRecord(
                                id=sr_da_2,
                                analysis_run_id=run_da,
                                pipeline_stage_node_id=node2_id,
                                analysis_stage_template_version_id=st2_v_id,
                                message_id=donor_a,
                                target_scope="global",
                                target_kind="message",
                                target_id=donor_a,
                                input_manifest_id=man2_id,
                                status="succeeded",
                                attempt_count=1,
                                max_attempts=3,
                                parsed_result={"ok": True},
                                result_origin="inference",
                                created_at=NOW,
                                updated_at=NOW,
                                completed_at=NOW,
                            )
                        )
                assert await stage_repo.complete_analysis_run(
                    analysis_run_id=str(run_da),
                    now=NOW + timedelta(seconds=5),
                )
                async with db.session() as session:
                    da_run_row = await session.get(AnalysisRunRecord, run_da)
                    assert da_run_row is not None
                    assert da_run_row.status == "completed"

                # B3: Now schedule batch stage runs for (recip_1, recip_2) and commit_cache_hit!
                async with db.session() as session:
                    async with session.begin():
                        for sr_id, r_id, m_id in (
                            (sr_r1_1, run_r1, recip_1),
                            (sr_r2_1, run_r2, recip_2),
                        ):
                            session.add(
                                StageRunRecord(
                                    id=sr_id,
                                    analysis_run_id=r_id,
                                    pipeline_stage_node_id=node1_id,
                                    analysis_stage_template_version_id=st1_v_id,
                                    message_id=m_id,
                                    target_scope="global",
                                    target_kind="message",
                                    target_id=m_id,
                                    status="pending",
                                    attempt_count=0,
                                    max_attempts=3,
                                    created_at=NOW + timedelta(seconds=10),
                                    updated_at=NOW + timedelta(seconds=10),
                                )
                            )

                claim_recip = await stage_repo.claim(
                    stage_run_id=str(sr_r1_1),
                    now=NOW + timedelta(seconds=11),
                    lease_duration=timedelta(minutes=2),
                )
                assert claim_recip is not None
                snap_recip = await stage_repo.load_stage_snapshot(claim=claim_recip)
                assert [t.target_id for t in snap_recip.targets] == [str(recip_1), str(recip_2)]

                recip_manifest_doc = {
                    "manifest_version": 2,
                    "target_ids": [str(recip_1), str(recip_2)],
                    "targets": [],
                }
                recip_manifest = InputManifest(
                    manifest_version=2,
                    content_json=json.dumps(recip_manifest_doc, separators=(",", ":")),
                    input_manifest_hash=canonical_hash(recip_manifest_doc),
                )
                hit = await stage_repo.find_cache_hit(snapshot=snap_recip, cache_key=cache_key)
                assert isinstance(hit, AnalysisCacheHit)
                cache_committed = await stage_repo.commit_cache_hit(
                    snapshot=snap_recip,
                    manifest=recip_manifest,
                    cache_key=cache_key,
                    hit=hit,
                    now=NOW + timedelta(seconds=12),
                )
                assert set(cache_committed.committed_target_ids) == {
                    str(recip_1),
                    str(recip_2),
                }
                # Negative gate assignment MUST belong ONLY to recip_2 (not recip_1)!
                assert [ng.message_id for ng in cache_committed.negative_gate_assignments] == [
                    str(recip_2)
                ]

                async with db.session() as session:
                    r1_labels = {
                        (lbl.label_key, lbl.activated)
                        for lbl in await session.scalars(
                            select(ModelLabelAssignmentRecord).where(
                                ModelLabelAssignmentRecord.message_id == recip_1
                            )
                        )
                    }
                    r2_labels = {
                        (lbl.label_key, lbl.activated)
                        for lbl in await session.scalars(
                            select(ModelLabelAssignmentRecord).where(
                                ModelLabelAssignmentRecord.message_id == recip_2
                            )
                        )
                    }
                    assert r1_labels == {("keep", True), ("block", False)}
                    assert r2_labels == {("keep", False), ("block", True)}
            finally:
                await db.dispose()

        asyncio.run(exercise())


def test_publication_degraded_metadata_only_and_cleanup_guards_overrides_and_recovery() -> None:
    with temporary_postgres_database("post_m10_pub_clean", revision=HEAD) as url:

        async def exercise() -> None:
            db = AsyncDatabase(url)
            try:
                ids = await _seed_core_entities(db)
                pub_repo = SqlAlchemyPublicationRepository(db)
                clean_repo = SqlAlchemyArchiveCleanupRepository(db)

                # --- Part A: B7 degraded METADATA_ONLY single-part terminal completion ---
                msg_pub = uuid4()
                pol_id, pol_v_id, rule_id, act_id, eval_id, intent_id = (uuid4() for _ in range(6))
                tmpl_id, tmpl_v_id = uuid4(), uuid4()
                async with db.session() as session:
                    async with session.begin():
                        session.add_all(
                            [
                                MessageRecord(
                                    id=msg_pub,
                                    source_channel_id=ids["source"],
                                    telegram_message_ids=[77],
                                    primary_telegram_message_id=77,
                                    published_at=NOW - timedelta(days=2),
                                    media_count=0,
                                    processing_status="processed",
                                ),
                                RoutingPolicyRecord(id=pol_id, name="pol-deg"),
                                RenderingTemplateRecord(id=tmpl_id, name="tmpl-deg"),
                            ]
                        )
                        await session.flush()
                        session.add_all(
                            [
                                RoutingPolicyVersionRecord(
                                    id=pol_v_id, routing_policy_id=pol_id, version_number=1
                                ),
                                RenderingTemplateVersionRecord(
                                    id=tmpl_v_id,
                                    rendering_template_id=tmpl_id,
                                    version_number=1,
                                    template_text="Fallback metadata",
                                    content_hash="b" * 64,
                                ),
                            ]
                        )
                        await session.flush()
                        session.add(
                            RoutingRuleRecord(
                                id=rule_id,
                                routing_policy_version_id=pol_v_id,
                                priority=1,
                                condition={},
                            )
                        )
                        await session.flush()
                        session.add(
                            RoutingActionRecord(
                                id=act_id,
                                routing_rule_id=rule_id,
                                destination_channel_id=ids["destination"],
                                publication_mode="native_forward_with_supplement",
                                protected_content_fallback="metadata_only",
                                rendering_template_version_id=tmpl_v_id,
                                output_order=0,
                            )
                        )
                        session.add(
                            RoutingEvaluationRecord(
                                id=eval_id,
                                routing_request_id="req-deg",
                                message_id=msg_pub,
                                routing_policy_version_id=pol_v_id,
                                facts_snapshot={},
                                facts_hash="e" * 64,
                                rule_outcomes=[],
                            )
                        )
                        await session.flush()
                        session.add(
                            PublicationIntentRecord(
                                id=intent_id,
                                routing_evaluation_id=eval_id,
                                routing_request_id="req-deg",
                                message_id=msg_pub,
                                routing_policy_version_id=pol_v_id,
                                routing_rule_id=rule_id,
                                routing_action_id=act_id,
                                destination_channel_id=ids["destination"],
                                publish_identity_id=ids["identity"],
                                publication_mode="native_forward_with_supplement",
                                protected_content_fallback="metadata_only",
                                rendering_template_version_id=tmpl_v_id,
                                rendered_text="Fallback metadata",
                                rendered_snapshot_hash="f" * 64,
                                business_idempotency_key="idem-deg",
                            )
                        )

                pub_claim = await pub_repo.claim(
                    intent_id=str(intent_id),
                    now=NOW,
                    lease_duration=timedelta(seconds=30),
                )
                assert pub_claim is not None
                assert await pub_repo.begin_part(
                    claim=pub_claim,
                    part=PublicationPart.MAIN,
                    operation=PublicationOperation.SEND_TEXT,
                    now=NOW,
                )
                assert await pub_repo.record_part(
                    claim=pub_claim,
                    part=PublicationPart.MAIN,
                    operation=PublicationOperation.SEND_TEXT,
                    remote_message_ids=(808,),
                    reconciled=False,
                    now=NOW,
                )
                # Simulate crash after MAIN SEND_TEXT before complete():
                # expired lease re-claim must expose main_degraded_to_text=True!
                reclaimed = await pub_repo.claim(
                    intent_id=str(intent_id),
                    now=NOW + timedelta(seconds=60),
                    lease_duration=timedelta(seconds=30),
                )
                assert reclaimed is not None
                assert reclaimed.main_degraded_to_text is True
                assert await pub_repo.complete(
                    claim=reclaimed,
                    now=NOW + timedelta(seconds=61),
                )
                async with db.session() as session:
                    final_intent = await session.get(PublicationIntentRecord, intent_id)
                    assert final_intent is not None
                    assert final_intent.status == "sent"
                    assert final_intent.completed_parts == ["main"]

                # --- Part B: B6 active-use guard, B10 manual override, B11 pHash, B12 recovery ---
                nsfw_def, nsfw_v1, nsfw_v2 = uuid4(), uuid4(), uuid4()
                pipe_id, pipe_v_id = uuid4(), uuid4()
                msg_canon, msg_phash_dup, msg_override_cleared, msg_active_proc = (
                    uuid4() for _ in range(4)
                )
                img_canon, img_phash_dup, img_override_cleared, img_active_proc = (
                    uuid4() for _ in range(4)
                )
                run_dup, run_cleared = uuid4(), uuid4()
                sr_dup, sr_cleared = uuid4(), uuid4()
                man_dup, man_cleared = uuid4(), uuid4()
                st_id, st_v_id, ls_id, lsv_id, pt_id, ptv_id, ip_id, ipv_id = (
                    uuid4() for _ in range(8)
                )

                async with db.session() as session:
                    async with session.begin():
                        session.add_all(
                            [
                                LabelDefinitionRecord(id=nsfw_def, name="NSFW"),
                                LabelSetRecord(id=ls_id, name="ls-clean"),
                                PromptTemplateRecord(id=pt_id, name="pt-clean"),
                                InferenceProfileRecord(id=ip_id, name="ip-clean"),
                                AnalysisStageTemplateRecord(id=st_id, name="st-clean"),
                                AnalysisPipelineRecord(id=pipe_id, name="pipe-clean"),
                            ]
                        )
                        await session.flush()
                        session.add_all(
                            [
                                LabelDefinitionVersionRecord(
                                    id=nsfw_v1,
                                    label_definition_id=nsfw_def,
                                    version_number=1,
                                    state="archived",
                                    published_at=NOW - timedelta(days=10),
                                    archived_at=NOW - timedelta(days=5),
                                    key="nsfw",
                                    display_name="NSFW v1",
                                    description="v1",
                                    scope="global",
                                    negative=True,
                                ),
                                LabelDefinitionVersionRecord(
                                    id=nsfw_v2,
                                    label_definition_id=nsfw_def,
                                    version_number=2,
                                    state="published",
                                    published_at=NOW - timedelta(days=5),
                                    key="nsfw",
                                    display_name="NSFW v2",
                                    description="v2",
                                    scope="global",
                                    negative=True,
                                ),
                                LabelSetVersionRecord(
                                    id=lsv_id,
                                    label_set_id=ls_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW - timedelta(days=5),
                                ),
                                PromptTemplateVersionRecord(
                                    id=ptv_id,
                                    prompt_template_id=pt_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW - timedelta(days=5),
                                    system_prompt="sys",
                                    user_prompt_template="usr",
                                    declared_variables=[],
                                ),
                                InferenceProfileVersionRecord(
                                    id=ipv_id,
                                    inference_profile_id=ip_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW - timedelta(days=5),
                                    provider_adapter="openai_compatible",
                                    base_url="https://api.example.com/v1",
                                    model_name="m",
                                    api_secret_reference=str(ids["secret"]),
                                    capabilities=["text"],
                                    timeout_seconds=10,
                                    max_concurrency=1,
                                    max_images=1,
                                    max_input_tokens=1024,
                                    structured_output_support=True,
                                    sampling_parameters={},
                                ),
                                AnalysisPipelineVersionRecord(
                                    id=pipe_v_id,
                                    analysis_pipeline_id=pipe_id,
                                    version_number=1,
                                    state="published",
                                    published_at=NOW - timedelta(days=5),
                                ),
                            ]
                        )
                        await session.flush()
                        session.add(
                            AnalysisStageTemplateVersionRecord(
                                id=st_v_id,
                                analysis_stage_template_id=st_id,
                                version_number=1,
                                state="published",
                                published_at=NOW - timedelta(days=5),
                                name="clean_stage",
                                target_scope="global",
                                execution_mode="single_message",
                                prompt_template_version_id=ptv_id,
                                label_set_version_id=lsv_id,
                                structured_output_policy={},
                                input_policy={"text": True},
                                visual_composition_policy="adaptive",
                                inference_profile_version_id=ipv_id,
                                cache_policy="none",
                                timeout_seconds=10,
                                retry_policy={"max_attempts": 2},
                                max_batch_size=1,
                                max_concurrency=1,
                            )
                        )
                        session.add_all(
                            [
                                MessageRecord(
                                    id=msg_canon,
                                    source_channel_id=ids["source"],
                                    telegram_message_ids=[201],
                                    primary_telegram_message_id=201,
                                    published_at=NOW - timedelta(days=5),
                                    media_count=1,
                                    visual_fingerprint="1" * 64,
                                    duplicate_status="unique",
                                    processing_status="processed",
                                ),
                                MessageRecord(
                                    id=msg_phash_dup,
                                    source_channel_id=ids["source"],
                                    telegram_message_ids=[202],
                                    primary_telegram_message_id=202,
                                    published_at=NOW - timedelta(days=4),
                                    media_count=1,
                                    visual_fingerprint="2"
                                    * 64,  # Different SHA-256, matched via pHash!
                                    duplicate_status="duplicate_skipped",
                                    canonical_message_id=msg_canon,
                                    duplicate_distance=3,
                                    processing_status="processed",
                                    terminal_reason="duplicate_skipped",
                                ),
                                MessageRecord(
                                    id=msg_override_cleared,
                                    source_channel_id=ids["source"],
                                    telegram_message_ids=[203],
                                    primary_telegram_message_id=203,
                                    published_at=NOW - timedelta(days=4),
                                    media_count=1,
                                    visual_fingerprint="3" * 64,
                                    duplicate_status="unique",
                                    processing_status="processed",
                                ),
                                MessageRecord(
                                    id=msg_active_proc,
                                    source_channel_id=ids["source"],
                                    telegram_message_ids=[204],
                                    primary_telegram_message_id=204,
                                    published_at=NOW - timedelta(days=4),
                                    media_count=1,
                                    visual_fingerprint="4" * 64,
                                    duplicate_status="unique",
                                    processing_status="processing",  # Active unblocked message!
                                    blocked_from_analysis=False,
                                ),
                            ]
                        )
                        await session.flush()
                        for img_id, m_id, key_name in (
                            (img_canon, msg_canon, "canon.webp"),
                            (img_phash_dup, msg_phash_dup, "dup.webp"),
                            (img_override_cleared, msg_override_cleared, "cleared.webp"),
                            (img_active_proc, msg_active_proc, "active.webp"),
                        ):
                            session.add(
                                ImageAssetRecord(
                                    id=img_id,
                                    message_id=m_id,
                                    source_asset_id=f"sa-{key_name}",
                                    archive_state="ready",
                                    storage_backend="archive",
                                    storage_key=key_name,
                                    content_type="image/webp",
                                    width=32,
                                    height=32,
                                    source_sha256="a" * 64,
                                    archive_sha256="b" * 64,
                                    perceptual_hash="0123456789abcdef",
                                    archive_size_bytes=100,
                                    archive_ready_at=NOW - timedelta(days=4),
                                )
                            )
                        session.add(
                            DuplicateMatchRecord(
                                id=uuid4(),
                                message_id=msg_phash_dup,
                                canonical_message_id=msg_canon,
                                match_kind="perceptual_multiset",
                                hamming_distance=3,
                                duplicate_policy="skip",
                                created_at=NOW - timedelta(days=4),
                            )
                        )
                        session.add_all(
                            [
                                AnalysisRunRecord(
                                    id=run_dup,
                                    message_id=msg_phash_dup,
                                    analysis_pipeline_version_id=pipe_v_id,
                                    run_mode="formal",
                                    status="completed",
                                    started_at=NOW - timedelta(days=4),
                                    completed_at=NOW - timedelta(days=4),
                                    facts_snapshot={"_pipeline_input_revision": "202"},
                                ),
                                AnalysisRunRecord(
                                    id=run_cleared,
                                    message_id=msg_override_cleared,
                                    analysis_pipeline_version_id=pipe_v_id,
                                    run_mode="formal",
                                    status="completed",
                                    started_at=NOW - timedelta(days=4),
                                    completed_at=NOW - timedelta(days=4),
                                    facts_snapshot={"_pipeline_input_revision": "203"},
                                ),
                                InputManifestRecord(
                                    id=man_dup,
                                    analysis_stage_template_version_id=st_v_id,
                                    manifest_version=2,
                                    content={"manifest_version": 2, "targets": []},
                                    input_manifest_hash="1" * 64,
                                    created_at=NOW - timedelta(days=4),
                                ),
                                InputManifestRecord(
                                    id=man_cleared,
                                    analysis_stage_template_version_id=st_v_id,
                                    manifest_version=2,
                                    content={"manifest_version": 2, "targets": []},
                                    input_manifest_hash="2" * 64,
                                    created_at=NOW - timedelta(days=4),
                                ),
                            ]
                        )
                        await session.flush()
                        session.add_all(
                            [
                                StageRunRecord(
                                    id=sr_dup,
                                    analysis_run_id=run_dup,
                                    analysis_stage_template_version_id=st_v_id,
                                    message_id=msg_phash_dup,
                                    target_scope="global",
                                    target_kind="message",
                                    target_id=msg_phash_dup,
                                    input_manifest_id=man_dup,
                                    status="succeeded",
                                    attempt_count=1,
                                    max_attempts=2,
                                    parsed_result={"ok": True},
                                    result_origin="inference",
                                    completed_at=NOW - timedelta(days=4),
                                ),
                                StageRunRecord(
                                    id=sr_cleared,
                                    analysis_run_id=run_cleared,
                                    analysis_stage_template_version_id=st_v_id,
                                    message_id=msg_override_cleared,
                                    target_scope="global",
                                    target_kind="message",
                                    target_id=msg_override_cleared,
                                    input_manifest_id=man_cleared,
                                    status="succeeded",
                                    attempt_count=1,
                                    max_attempts=2,
                                    parsed_result={"ok": True},
                                    result_origin="inference",
                                    completed_at=NOW - timedelta(days=4),
                                ),
                            ]
                        )
                        await session.flush()
                        # msg_override_cleared had model nsfw(v1)=True,
                        # overridden by manual SET nsfw(v2)=False!
                        session.add(
                            ModelLabelAssignmentRecord(
                                id=uuid4(),
                                stage_run_id=sr_cleared,
                                analysis_run_id=run_cleared,
                                message_id=msg_override_cleared,
                                target_scope="global",
                                target_kind="message",
                                target_id=msg_override_cleared,
                                label_definition_version_id=nsfw_v1,
                                label_key="nsfw",
                                score=0.95,
                                activated=True,
                                negative=True,
                            )
                        )
                        session.add(
                            ManualLabelAssignmentRecord(
                                id=uuid4(),
                                message_id=msg_override_cleared,
                                target_scope="global",
                                target_kind="message",
                                target_id=msg_override_cleared,
                                label_definition_version_id=nsfw_v2,
                                label_key="nsfw",
                                operation="set",
                                score=0.05,
                                activated=False,
                                actor_admin_user_id=ids["admin"],
                                reason="false positive on v2",
                                created_at=NOW - timedelta(days=1),
                            )
                        )

                # B10 check: effective_label_keys=('nsfw',) must equal 0 because
                # v2 manual SET deactivated v1 model label!
                dr_nsfw = await clean_repo.create_dry_run(
                    cleanup_filter=ArchiveCleanupFilter(effective_label_keys=("nsfw",)),
                    actor_admin_user_id=str(ids["admin"]),
                    now=NOW,
                )
                assert dr_nsfw.estimate.asset_count == 0

                # B11 check: pHash duplicate matches ('duplicate_skipped',)
                # and canonical target matches ('canonical',)!
                dr_canon = await clean_repo.create_dry_run(
                    cleanup_filter=ArchiveCleanupFilter(duplicate_statuses=("canonical",)),
                    actor_admin_user_id=str(ids["admin"]),
                    now=NOW,
                )
                assert dr_canon.estimate.asset_count == 1

                dr_dup = await clean_repo.create_dry_run(
                    cleanup_filter=ArchiveCleanupFilter(duplicate_statuses=("duplicate_skipped",)),
                    actor_admin_user_id=str(ids["admin"]),
                    now=NOW,
                )
                assert dr_dup.estimate.asset_count == 1

                # B6 check: unfiltered dry-run includes recent 3, confirm creates items,
                # while claim_next_item skips msg_active_proc!
                dr_active = await clean_repo.create_dry_run(
                    cleanup_filter=ArchiveCleanupFilter(
                        source_channel_ids=(str(ids["source"]),),
                        message_published_from=NOW - timedelta(days=4, hours=12),
                    ),
                    actor_admin_user_id=str(ids["admin"]),
                    now=NOW,
                )
                assert dr_active.estimate.asset_count == 3  # dup + cleared + active_proc
                confirmed = await clean_repo.confirm(
                    dry_run_id=dr_active.dry_run_id,
                    expected_filter_hash=dr_active.filter_hash,
                    confirmed_by_admin_user_id=str(ids["admin"]),
                    now=NOW,
                    max_attempts=1,
                )
                job_claim = await clean_repo.claim(
                    job_id=confirmed.job_id,
                    now=NOW,
                    lease_duration=timedelta(minutes=5),
                )
                assert job_claim is not None

                # Claim first item at NOW + 2m: lease_expires_at must renew,
                # and terminal failure must restore 'deleting' -> 'ready'!
                item_1 = await clean_repo.claim_next_item(
                    claim=job_claim,
                    now=NOW + timedelta(minutes=2),
                )
                assert item_1 is not None
                assert UUID(item_1.asset.asset_id) != img_active_proc

                async with db.session() as session:
                    wedged_img = await session.get(ImageAssetRecord, UUID(item_1.asset.asset_id))
                    assert wedged_img is not None
                    assert wedged_img.archive_state == "deleting"
                    renewed_exp = await session.scalar(
                        text("SELECT lease_expires_at FROM archive_cleanup_jobs WHERE id = :id"),
                        {"id": UUID(confirmed.job_id)},
                    )
                    assert renewed_exp > NOW + timedelta(minutes=5)

                # Terminal failure (max_attempts=1, retryable=False) MUST restore
                # wedged 'deleting' asset back to 'ready'!
                assert await clean_repo.retry_or_fail(
                    claim=job_claim,
                    item=item_1,
                    error_code="storage_error",
                    error_type="DiskIOError",
                    retryable=False,
                    retry_at=NOW + timedelta(minutes=3),
                    now=NOW + timedelta(minutes=3),
                )
                async with db.session() as session:
                    restored = await session.get(ImageAssetRecord, UUID(item_1.asset.asset_id))
                    assert restored is not None
                    assert restored.archive_state == "ready"
                    assert restored.cleanup_job_id is None
                    assert restored.previous_archive_size is None
            finally:
                await db.dispose()

        asyncio.run(exercise())
