"""Opt-in native PostgreSQL PR28 regressions, built from actual Alembic migrations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from postgres_support import migrate_database, temporary_postgres_database
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from tgcurator.application.pipeline.coordinator import MessagePipelineCoordinator
from tgcurator.infrastructure.database.models import (
    AnalysisPipelineRecord,
    AnalysisPipelineVersionRecord,
    AnalysisRunRecord,
    DurableWakeupRecord,
    EncryptedSecret,
    ImageAssetRecord,
    MessagePartRecord,
    MessageRecord,
    ProcessingRangeRecord,
    RangeExecutionRecord,
    SourceChannel,
    SourceChannelProfile,
    SourceChannelProfileVersion,
    TelegramIdentity,
)
from tgcurator.infrastructure.database.pipeline_repository import (
    SqlAlchemyMessagePipelineRepository,
)
from tgcurator.infrastructure.database.session import AsyncDatabase

NOW = datetime(2026, 11, 12, tzinfo=UTC)
M6 = "e6d4a8b2c951"
M7 = "f1a7c3d9e204"


async def seed(db, *, m6=False):
    ids = {
        key: uuid4()
        for key in (
            "secret",
            "identity",
            "source",
            "profile",
            "profile_version",
            "pipeline",
            "pipeline_version",
            "message",
        )
    }
    async with db.session() as session:
        async with session.begin():
            session.add(
                EncryptedSecret(
                    id=ids["secret"],
                    secret_type="test",
                    ciphertext=b"0" * 16,
                    nonce=b"0" * 12,
                    key_id="test",
                )
            )
            await session.flush()
            session.add(
                TelegramIdentity(
                    id=ids["identity"],
                    name=str(ids["identity"]),
                    identity_type="bot",
                    secret_id=ids["secret"],
                )
            )
            await session.flush()
            session.add(
                SourceChannel(
                    id=ids["source"],
                    telegram_peer_id=123,
                    display_name="test",
                    default_read_identity_id=ids["identity"],
                )
            )
            session.add(AnalysisPipelineRecord(id=ids["pipeline"], name=str(ids["pipeline"])))
            await session.flush()
            session.add(
                SourceChannelProfile(
                    id=ids["profile"], source_channel_id=ids["source"], name="test"
                )
            )
            session.add(
                AnalysisPipelineVersionRecord(
                    id=ids["pipeline_version"],
                    analysis_pipeline_id=ids["pipeline"],
                    version_number=1,
                )
            )
            await session.flush()
            session.add(
                SourceChannelProfileVersion(
                    id=ids["profile_version"],
                    profile_id=ids["profile"],
                    version_number=1,
                    content={},
                    content_hash="0" * 64,
                )
            )
            # A raw INSERT works for both real M6 and M7 schema (no create_all shortcuts).
            await session.execute(
                text(
                    "INSERT INTO messages (id,source_channel_id,telegram_message_ids,telegram_group"
                    "ed_id,primary_telegram_message_id,published_at,media_count) VALUES "
                    "(:id,:source,ARRAY[100]::bigint[],500,100,:now,1)"
                ),
                dict(id=ids["message"], source=ids["source"], now=NOW),
            )
            session.add(
                MessagePartRecord(
                    id=uuid4(),
                    message_id=ids["message"],
                    source_channel_id=ids["source"],
                    telegram_message_id=100,
                    published_at=NOW,
                    media=[],
                    media_count=0,
                )
            )
    return ids


async def add_execution(db, ids, *, lower=99, upper=100):
    rid, eid = uuid4(), uuid4()
    async with db.session() as session:
        async with session.begin():
            session.add(
                ProcessingRangeRecord(
                    id=rid,
                    source_channel_id=ids["source"],
                    start_at=NOW,
                    end_mode="latest",
                    steady_after_seconds=1,
                    status="active",
                    processing_watermark_message_id=lower,
                    active_high_watermark_message_id=upper,
                )
            )
            await session.flush()
            session.add(
                RangeExecutionRecord(
                    id=eid,
                    processing_range_id=rid,
                    source_profile_version_id=ids["profile_version"],
                    from_message_id_exclusive=lower,
                    to_message_id_inclusive=upper,
                    watermark_message_id=lower,
                    status="awaiting_pipeline",
                )
            )
    return eid


def test_m6_to_m7_roundtrip_preserves_formal_history_and_constraint_names():
    with temporary_postgres_database("m7_migration", revision=M6) as url:

        async def old_data():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db, m6=True)
                async with db.session() as s:
                    async with s.begin():
                        for _ in range(2):
                            s.add(
                                AnalysisRunRecord(
                                    id=uuid4(),
                                    message_id=ids["message"],
                                    analysis_pipeline_version_id=ids["pipeline_version"],
                                    run_mode="formal",
                                    status="pending",
                                    facts_snapshot={},
                                )
                            )
                return ids
            finally:
                await db.dispose()

        ids = asyncio.run(old_data())
        migrate_database(url, M7)

        async def verify():
            db = AsyncDatabase(url)
            try:
                async with db.session() as s:
                    names = set(
                        await s.scalars(
                            text(
                                "SELECT conname FROM pg_constraint WHERE conrelid IN "
                                "('messages'::regclass,'range_executions'::regclass)"
                            )
                        )
                    )
                    assert "ck_range_executions_range_execution_status" in names
                    assert {
                        "ck_messages_message_duplicate_status",
                        "ck_messages_message_duplicate_canonical_state",
                        "ck_messages_message_terminal_reason_state",
                        "ck_messages_message_failure_stage_state",
                    } <= names
                    rows = tuple(await s.scalars(select(AnalysisRunRecord)))
                    assert len(rows) == 2
                    revisions = [r.facts_snapshot["_pipeline_input_revision"] for r in rows]
                    assert (
                        "100" in revisions and sum(r.startswith("legacy:") for r in revisions) == 1
                    )
                repo = SqlAlchemyMessagePipelineRepository(db)
                assert await repo.ensure_formal_analysis_run(
                    message_id=str(ids["message"]),
                    pipeline_version_id=str(ids["pipeline_version"]),
                    now=NOW,
                ) in {str(r.id) for r in rows}
            finally:
                await db.dispose()

        asyncio.run(verify())

        # Execute the actual downgrade implementation through Alembic's native connection.
        async def down():
            from importlib import import_module

            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            from tgcurator.infrastructure.database.base import Base

            db = AsyncDatabase(url)
            try:
                async with db.session() as s:
                    async with s.begin():
                        connection = await s.connection()

                        def apply(conn):
                            context = MigrationContext.configure(
                                conn, opts={"target_metadata": Base.metadata}
                            )
                            with Operations.context(context):
                                import_module(
                                    "migrations.versions.f1a7c3d9e204_m7_pipeline_dedup_watermarks"
                                ).downgrade()
                            conn.execute(
                                text("UPDATE alembic_version SET version_num=:v"), {"v": M6}
                            )

                        await connection.run_sync(apply)
            finally:
                await db.dispose()

        asyncio.run(down())
        migrate_database(url, M7)
        asyncio.run(verify())


def test_native_first_formal_concurrency_unique_scope_and_component_revision():
    with temporary_postgres_database("m7_concurrent", revision=M7) as url:

        async def scenario():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db)
                repo = SqlAlchemyMessagePipelineRepository(db)
                args = dict(
                    message_id=str(ids["message"]),
                    pipeline_version_id=str(ids["pipeline_version"]),
                    now=NOW,
                )
                # Hold the parent from a third connection: both first callers must wait.
                async with db.session() as locker:
                    async with locker.begin():
                        await locker.execute(
                            select(MessageRecord)
                            .where(MessageRecord.id == ids["message"])
                            .with_for_update()
                        )
                        first = asyncio.create_task(repo.ensure_formal_analysis_run(**args))
                        second = asyncio.create_task(repo.ensure_formal_analysis_run(**args))
                        await asyncio.sleep(0.15)
                        assert not first.done() and not second.done()
                    a, b = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
                assert a == b
                async with db.session() as s:
                    assert await s.scalar(select(func.count()).select_from(AnalysisRunRecord)) == 1
                # The database guard also protects writers which forget the parent lock.
                for snapshot in ({"_pipeline_input_revision": "100"}, {}):
                    with pytest.raises(IntegrityError):
                        async with db.session() as s:
                            async with s.begin():
                                s.add(
                                    AnalysisRunRecord(
                                        id=uuid4(),
                                        message_id=ids["message"],
                                        analysis_pipeline_version_id=ids["pipeline_version"],
                                        run_mode="formal",
                                        facts_snapshot=snapshot,
                                    )
                                )
                async with db.session() as s:
                    async with s.begin():
                        for _ in range(2):
                            s.add(
                                AnalysisRunRecord(
                                    id=uuid4(),
                                    message_id=ids["message"],
                                    analysis_pipeline_version_id=ids["pipeline_version"],
                                    run_mode="reanalysis",
                                    facts_snapshot={},
                                )
                            )
                        await s.execute(
                            update(MessageRecord)
                            .where(MessageRecord.id == ids["message"])
                            .values(
                                processing_status="processed",
                                terminal_reason="completed",
                                duplicate_status="unique",
                            )
                        )
                # Ingesting a later part changes neither parent UUID nor historical run.
                async with db.session() as s:
                    async with s.begin():
                        await s.execute(
                            update(MessageRecord)
                            .where(MessageRecord.id == ids["message"])
                            .values(telegram_message_ids=[100, 101], media_count=2)
                        )
                async with db.session() as s:
                    msg = await s.get(MessageRecord, ids["message"])
                    assert msg.processing_status == "ingested" and msg.terminal_reason is None
                    assert (
                        msg.duplicate_status == "unchecked" and msg.source_changed_after_processing
                    )
                new = await repo.ensure_formal_analysis_run(**args)
                assert new != a
                dag = await repo.load_analysis_dag_snapshot(analysis_run_id=new)
                assert dag.facts["_pipeline_input_revision"] == "100,101"
                assert await repo.ensure_formal_analysis_run(**args) == new
                async with db.session() as s:
                    assert await s.scalar(select(func.count()).select_from(AnalysisRunRecord)) == 4
            finally:
                await db.dispose()

        asyncio.run(scenario())


def test_native_album_cross_window_wakeup_and_watermark_block():
    with temporary_postgres_database("m7_album", revision=M7) as url:

        async def scenario():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db)
                repo = SqlAlchemyMessagePipelineRepository(db)
                await repo.mark_message_processed(
                    message_id=str(ids["message"]), terminal_reason="completed", now=NOW
                )
                new_asset = uuid4()
                async with db.session() as s:
                    async with s.begin():
                        await s.execute(
                            update(MessageRecord)
                            .where(MessageRecord.id == ids["message"])
                            .values(telegram_message_ids=[100, 101], media_count=2)
                        )
                        s.add(
                            MessagePartRecord(
                                id=uuid4(),
                                message_id=ids["message"],
                                source_channel_id=ids["source"],
                                telegram_message_id=101,
                                published_at=NOW,
                                media=[],
                                media_count=0,
                            )
                        )
                        s.add(
                            ImageAssetRecord(
                                id=new_asset,
                                message_id=ids["message"],
                                source_asset_id="part101",
                                source_telegram_message_id=101,
                                archive_state="pending",
                            )
                        )
                eid = await add_execution(db, ids, lower=100, upper=101)
                ex = await repo.load_execution(execution_id=str(eid))
                rows = await repo.load_window_messages(execution=ex)
                assert len(rows) == 1 and rows[0].primary_telegram_message_id == 101
                assert await repo.find_awaiting_execution_ids_for_message(
                    message_id=str(ids["message"])
                ) == (str(eid),)
                c = MessagePipelineCoordinator(repository=repo)
                await c.on_image_asset_event(image_asset_id=str(new_asset), now=NOW)
                async with db.session() as s:
                    wakeups = tuple(
                        await s.scalars(
                            select(DurableWakeupRecord).where(
                                DurableWakeupRecord.entity_id == new_asset
                            )
                        )
                    )
                    assert len(wakeups) == 1 and wakeups[0].status == "pending"
                    execution = await s.get(RangeExecutionRecord, eid)
                    assert (
                        execution.status == "awaiting_pipeline"
                        and execution.watermark_message_id == 100
                    )
                    msg = await s.get(MessageRecord, ids["message"])
                    assert (
                        msg.primary_telegram_message_id == 100
                        and msg.processing_status == "processing"
                    )
            finally:
                await db.dispose()

        asyncio.run(scenario())


@pytest.mark.parametrize("legacy_request", [False, True])
def test_native_overlapping_executions_share_routing_and_single_publication_intent(legacy_request):
    from tgcurator.application.routing import RoutingService
    from tgcurator.infrastructure.database.models import (
        DestinationChannel,
        PublicationIntentRecord,
        RoutingActionRecord,
        RoutingEvaluationRecord,
        RoutingPolicyRecord,
        RoutingPolicyVersionRecord,
        RoutingRuleRecord,
    )
    from tgcurator.infrastructure.database.routing_repository import SqlAlchemyRoutingRepository

    with temporary_postgres_database("m7_overlap", revision=M7) as url:

        async def scenario():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db)
                policy, pv, rule, dest, action = (uuid4() for _ in range(5))
                async with db.session() as s:
                    async with s.begin():
                        s.add(
                            DestinationChannel(
                                id=dest,
                                name="dest",
                                telegram_peer_id=456,
                                default_publish_identity_id=ids["identity"],
                            )
                        )
                        s.add(RoutingPolicyRecord(id=policy, name="policy"))
                        await s.flush()
                        s.add(
                            RoutingPolicyVersionRecord(
                                id=pv, routing_policy_id=policy, version_number=1
                            )
                        )
                        await s.flush()
                        s.add(
                            RoutingRuleRecord(
                                id=rule,
                                routing_policy_version_id=pv,
                                priority=1,
                                condition={"fact": "message.id", "op": "exists"},
                            )
                        )
                        await s.flush()
                        s.add(
                            RoutingActionRecord(
                                id=action,
                                routing_rule_id=rule,
                                destination_channel_id=dest,
                                publication_mode="forward_only",
                                publish_identity_id=ids["identity"],
                                protected_content_fallback="fail",
                                supplement_as_reply=False,
                                output_order=0,
                            )
                        )
                        await s.flush()
                        await s.execute(
                            update(RoutingPolicyVersionRecord)
                            .where(RoutingPolicyVersionRecord.id == pv)
                            .values(state="published", published_at=NOW)
                        )
                        await s.execute(
                            update(SourceChannelProfileVersion)
                            .where(SourceChannelProfileVersion.id == ids["profile_version"])
                            .values(content={"routing_policy_version_id": str(pv)})
                        )
                        await s.execute(
                            update(MessageRecord)
                            .where(MessageRecord.id == ids["message"])
                            .values(
                                processing_status="processed",
                                terminal_reason="completed",
                                duplicate_status="unique",
                            )
                        )
                e1 = await add_execution(db, ids)
                e2 = await add_execution(db, ids)
                repo = SqlAlchemyMessagePipelineRepository(db)
                c = MessagePipelineCoordinator(
                    repository=repo, routing_service=RoutingService(SqlAlchemyRoutingRepository(db))
                )
                await asyncio.wait_for(
                    asyncio.gather(
                        c.drive_execution(execution_id=str(e1), now=NOW),
                        c.drive_execution(execution_id=str(e2), now=NOW),
                    ),
                    timeout=10,
                )
                async with db.session() as s:
                    assert (
                        await s.scalar(select(func.count()).select_from(RoutingEvaluationRecord))
                        == 1
                    )
                    assert (
                        await s.scalar(select(func.count()).select_from(PublicationIntentRecord))
                        == 1
                    )
                    for eid in (e1, e2):
                        ex = await s.get(RangeExecutionRecord, eid)
                        assert ex.status == "awaiting_pipeline" and ex.watermark_message_id == 99
                # Synthetic publication completion; no Telegram call is made.
                async with db.session() as s:
                    async with s.begin():
                        await s.execute(
                            update(PublicationIntentRecord).values(
                                status="sent",
                                main_random_ids=[1],
                                main_remote_message_ids=[999],
                                completed_parts=["main"],
                                completed_at=NOW,
                            )
                        )
                await asyncio.wait_for(
                    asyncio.gather(
                        c.drive_execution(execution_id=str(e1), now=NOW),
                        c.drive_execution(execution_id=str(e2), now=NOW),
                    ),
                    timeout=10,
                )
                async with db.session() as s:
                    assert (
                        await s.scalar(select(func.count()).select_from(PublicationIntentRecord))
                        == 1
                    )
                    for eid in (e1, e2):
                        ex = await s.get(RangeExecutionRecord, eid)
                        assert ex.status == "completed" and ex.watermark_message_id == 100
            finally:
                await db.dispose()

        asyncio.run(scenario())


def test_native_same_second_visual_duplicates_keep_an_acyclic_canonical_root():
    with temporary_postgres_database("m7_canonical", revision=M7) as url:

        async def scenario():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db)
                later = UUID(int=(1 << 128) - 1)
                async with db.session() as s:
                    async with s.begin():
                        s.add(
                            MessageRecord(
                                id=later,
                                source_channel_id=ids["source"],
                                telegram_message_ids=[101],
                                primary_telegram_message_id=101,
                                published_at=NOW,
                                media_count=1,
                            )
                        )
                        await s.flush()
                        for mid, tg in ((ids["message"], 100), (later, 101)):
                            s.add(
                                ImageAssetRecord(
                                    id=uuid4(),
                                    message_id=mid,
                                    source_asset_id=str(tg),
                                    source_telegram_message_id=tg,
                                    archive_state="ready",
                                    storage_backend="test",
                                    storage_key=f"test/{tg}.jpg",
                                    content_type="image/jpeg",
                                    width=10,
                                    height=10,
                                    source_sha256="a" * 64,
                                    archive_sha256="b" * 64,
                                    perceptual_hash="0123456789abcdef",
                                    archive_size_bytes=10,
                                    archive_ready_at=NOW,
                                )
                            )
                eid = await add_execution(db, ids, lower=99, upper=101)
                repo = SqlAlchemyMessagePipelineRepository(db)
                await MessagePipelineCoordinator(repository=repo).drive_execution(
                    execution_id=str(eid),
                    now=NOW,
                )
                async with db.session() as s:
                    first = await s.get(MessageRecord, ids["message"])
                    second = await s.get(MessageRecord, later)
                    assert first.duplicate_status == "unique" and first.canonical_message_id is None
                    assert first.processing_status == "processed"
                    assert second.duplicate_status == "duplicate_skipped"
                    assert second.canonical_message_id == first.id
                    ex = await s.get(RangeExecutionRecord, eid)
                    assert ex.status == "completed" and ex.watermark_message_id == 101
            finally:
                await db.dispose()

        asyncio.run(scenario())
