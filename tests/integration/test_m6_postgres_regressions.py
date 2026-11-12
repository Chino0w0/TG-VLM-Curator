"""Opt-in native PostgreSQL gates for the M6 baseline (no Telegram calls)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.postgres_support import migrate_database, temporary_postgres_database
from tgcurator.application.ports.cleanup import ArchiveCleanupFilter
from tgcurator.domain.publishing import PublicationOperation, PublicationPart
from tgcurator.infrastructure.database.cleanup_repository import SqlAlchemyArchiveCleanupRepository
from tgcurator.infrastructure.database.models import RoutingActionRecord
from tgcurator.infrastructure.database.publication_repository import SqlAlchemyPublicationRepository
from tgcurator.infrastructure.database.session import AsyncDatabase

M5 = "d7b3f9a1e6c2"
M6 = "e6d4a8b2c951"
NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


async def _seed_m5(url):
    """Insert valid historical M5 data with every FK/check/trigger enabled."""
    engine = create_async_engine(url)
    ids = {
        name: uuid4()
        for name in (
            "admin",
            "secret",
            "identity",
            "source",
            "destination",
            "message",
            "image",
            "policy",
            "policy_version",
            "rule",
            "template",
            "template_version",
            "evaluation",
        )
    }
    try:
        async with engine.begin() as connection:

            async def insert(table_name, **values):
                table = await connection.run_sync(
                    lambda sync: Table(table_name, MetaData(), autoload_with=sync)
                )
                await connection.execute(table.insert().values(**values))

            await insert("admin_users", id=ids["admin"], username="m6-test", password_hash="unused")
            await insert(
                "encrypted_secrets",
                id=ids["secret"],
                secret_type="test-only",
                ciphertext=b"x" * 16,
                nonce=b"x" * 12,
                key_id="not-a-real-key",
            )
            await insert(
                "telegram_identities",
                id=ids["identity"],
                name="test-identity",
                identity_type="mtproto_user",
                secret_id=ids["secret"],
            )
            await insert(
                "source_channels",
                id=ids["source"],
                telegram_peer_id=-100123,
                display_name="test-source",
                default_read_identity_id=ids["identity"],
            )
            await insert(
                "destination_channels",
                id=ids["destination"],
                name="test-destination",
                telegram_peer_id=-100456,
                default_publish_identity_id=ids["identity"],
            )
            await insert(
                "messages",
                id=ids["message"],
                source_channel_id=ids["source"],
                telegram_message_ids=[1],
                primary_telegram_message_id=1,
                published_at=NOW,
                media_count=1,
            )
            await insert(
                "image_assets",
                id=ids["image"],
                message_id=ids["message"],
                source_asset_id="m6-test-image",
                archive_state="ready",
                storage_backend="archive",
                storage_key="m6-test.webp",
                content_type="image/webp",
                width=1,
                height=1,
                source_sha256="a" * 64,
                archive_sha256="b" * 64,
                perceptual_hash="0" * 16,
                archive_size_bytes=37,
                archive_ready_at=NOW,
            )
            await insert("routing_policies", id=ids["policy"], name="m6-test-policy")
            await insert(
                "routing_policy_versions",
                id=ids["policy_version"],
                routing_policy_id=ids["policy"],
                version_number=1,
            )
            await insert(
                "routing_rules",
                id=ids["rule"],
                routing_policy_version_id=ids["policy_version"],
                priority=1,
                condition={},
            )
            await insert("rendering_templates", id=ids["template"], name="m6-test-template")
            await insert(
                "rendering_template_versions",
                id=ids["template_version"],
                rendering_template_id=ids["template"],
                version_number=1,
                template_text="metadata",
                content_hash="c" * 64,
            )
            await insert(
                "routing_evaluations",
                id=ids["evaluation"],
                routing_request_id="m6-test",
                message_id=ids["message"],
                routing_policy_version_id=ids["policy_version"],
                facts_snapshot={},
                facts_hash="d" * 64,
                rule_outcomes=[],
            )
            for order, mode in enumerate(
                (
                    "copy_with_caption",
                    "forward_only",
                    "metadata_only",
                    "native_forward_with_supplement",
                )
            ):
                action_id = uuid4()
                ids[mode] = uuid4()
                template = None if mode == "forward_only" else ids["template_version"]
                await insert(
                    "routing_actions",
                    id=action_id,
                    routing_rule_id=ids["rule"],
                    destination_channel_id=ids["destination"],
                    publication_mode=mode,
                    rendering_template_version_id=template,
                    output_order=order,
                )
                await insert(
                    "publication_intents",
                    id=ids[mode],
                    routing_evaluation_id=ids["evaluation"],
                    routing_request_id="m6-test",
                    message_id=ids["message"],
                    routing_policy_version_id=ids["policy_version"],
                    routing_rule_id=ids["rule"],
                    routing_action_id=action_id,
                    destination_channel_id=ids["destination"],
                    publish_identity_id=ids["identity"],
                    publication_mode=mode,
                    rendering_template_version_id=template,
                    business_idempotency_key=mode,
                )
    finally:
        await engine.dispose()
    return ids


class _TestDatabaseUrl(str):
    def __repr__(self):
        return "'<isolated-test-database-url>'"


@pytest.fixture
def legacy_database():
    with temporary_postgres_database("m6", revision=M5) as url:
        yield _TestDatabaseUrl(url), asyncio.run(_seed_m5(url))


@pytest.fixture
def m6_database(legacy_database):
    url, ids = legacy_database
    migrate_database(url, M6)
    return url, ids


async def _assert_identity_protection(url, intent_id, *, m6):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            enabled = await connection.scalar(
                text(
                    "SELECT tgenabled::text FROM pg_trigger WHERE "
                    "tgname='trg_publication_intents_identity_immutable'"
                )
            )
            assert enabled == "O"
            updates = ["business_idempotency_key='changed'"]
            if m6:
                updates += [
                    "rendered_text='changed'",
                    "rendered_snapshot_hash='" + "e" * 64 + "'",
                    "max_attempts=max_attempts+1",
                ]
            for change in updates:
                with pytest.raises(DBAPIError, match="publication intent identity is immutable"):
                    async with connection.begin_nested():
                        await connection.execute(
                            text(f"UPDATE publication_intents SET {change} WHERE id=:id"),
                            {"id": intent_id},
                        )
            with pytest.raises(DBAPIError, match="publication intents cannot be deleted"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("DELETE FROM publication_intents WHERE id=:id"), {"id": intent_id}
                    )
            await connection.execute(
                text("UPDATE publication_intents SET updated_at=:now WHERE id=:id"),
                {"id": intent_id, "now": NOW},
            )
    finally:
        await engine.dispose()


async def _assert_backfilled(url):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT publication_mode, rendered_text, rendered_snapshot_hash, status "
                        "FROM publication_intents ORDER BY publication_mode"
                    )
                )
            ).all()
            assert len(rows) == 4
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM routing_actions "
                        "WHERE protected_content_fallback='fail' "
                        "AND supplement_as_reply IS TRUE"
                    )
                )
                == 4
            )
            for mode, rendered, digest, status in rows:
                assert status == "pending"
                if mode == "forward_only":
                    assert rendered is None and digest is None
                else:
                    assert rendered == ""
                    assert (
                        digest == "b77f40b58e5a500b7e66561203197c25a9ff3095f75edaa72e849e7790938aab"
                    )
    finally:
        await engine.dispose()


def test_pg_m5_history_upgrade_downgrade_and_protection(legacy_database):
    url, ids = legacy_database
    asyncio.run(_assert_identity_protection(url, ids["copy_with_caption"], m6=False))

    async def publish_historical_policy():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE routing_policy_versions SET state='published', "
                        "published_at=:now "
                        "WHERE id=:id"
                    ),
                    {"now": NOW, "id": ids["policy_version"]},
                )
        finally:
            await engine.dispose()

    asyncio.run(publish_historical_policy())
    migrate_database(url, M6)
    asyncio.run(_assert_backfilled(url))
    asyncio.run(_assert_identity_protection(url, ids["copy_with_caption"], m6=True))
    environment = dict(os.environ, TGCURATOR_DATABASE_URL=url, TGCURATOR_ENVIRONMENT="test")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", M5],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    error = result.stderr.replace(url, "<test-url>")
    password = make_url(url).password
    if password:
        error = error.replace(password, "<redacted>")
    assert result.returncode == 0, error

    async def assert_removed_columns():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_schema='public' "
                            "AND table_name='routing_actions' "
                            "AND column_name IN ('protected_content_fallback', "
                            "'supplement_as_reply')"
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(assert_removed_columns())
    asyncio.run(_assert_identity_protection(url, ids["copy_with_caption"], m6=False))
    # Downgrade followed by another upgrade must retain all four immutable intents.
    migrate_database(url, M6)
    asyncio.run(_assert_backfilled(url))


def test_pg_failed_backfill_rolls_back_trigger_data_and_ddl(legacy_database):
    url, ids = legacy_database
    path = ROOT / "migrations/versions/e6d4a8b2c951_m6_publication_cleanup_operations.py"
    spec = importlib.util.spec_from_file_location("m6_rollback_probe", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async def fail_and_check():
        engine = create_async_engine(url)
        try:

            def fail_after_update(connection):
                operations = Operations(MigrationContext.configure(connection))

                class InjectFailure:
                    def __getattr__(self, name):
                        return getattr(operations, name)

                    def execute(self, statement):
                        result = operations.execute(statement)
                        if str(statement).startswith("UPDATE publication_intents "):
                            assert (
                                connection.scalar(
                                    text(
                                        "SELECT tgenabled::text FROM pg_trigger WHERE "
                                        "tgname='trg_publication_intents_identity_immutable'"
                                    )
                                )
                                == "D"
                            )
                            assert (
                                connection.scalar(
                                    text(
                                        "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() "
                                        "AND relation='publication_intents'::regclass "
                                        "AND mode='AccessExclusiveLock' AND granted"
                                    )
                                )
                                == 1
                            )
                            raise RuntimeError("injected failure after protected backfill")
                        return result

                migration.op = InjectFailure()
                migration.upgrade()

            with pytest.raises(RuntimeError, match="injected failure after protected backfill"):
                async with engine.begin() as connection:
                    await connection.run_sync(fail_after_update)
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns WHERE "
                            "table_schema='public' AND table_name='publication_intents' "
                            "AND column_name='rendered_text'"
                        )
                    )
                    == 0
                )
                assert (
                    await connection.scalar(text("SELECT count(*) FROM publication_intents")) == 4
                )
                assert (
                    await connection.scalar(text("SELECT version_num FROM alembic_version")) == M5
                )
        finally:
            await engine.dispose()

    asyncio.run(fail_and_check())
    asyncio.run(_assert_identity_protection(url, ids["copy_with_caption"], m6=False))
    migrate_database(url, M6)
    asyncio.run(_assert_backfilled(url))


@pytest.mark.parametrize("inject_failure", [False, True])
def test_pg_nonempty_cleanup_confirm_is_atomic_and_idempotent(m6_database, inject_failure):
    url, ids = m6_database

    async def run():
        database = AsyncDatabase(url)
        await database.connect()
        repository = SqlAlchemyArchiveCleanupRepository(database)
        try:
            preview = await repository.create_dry_run(
                cleanup_filter=ArchiveCleanupFilter(media_types=("image",)),
                actor_admin_user_id=str(ids["admin"]),
                now=NOW,
            )
            assert preview.estimate.asset_count == 1
            kwargs = dict(
                dry_run_id=preview.dry_run_id,
                expected_filter_hash=preview.filter_hash,
                confirmed_by_admin_user_id=str(ids["admin"]),
                now=NOW,
            )
            if inject_failure:

                def fail_item(connection, cursor, statement, parameters, context, executemany):
                    if statement.startswith("INSERT INTO archive_cleanup_items"):
                        raise RuntimeError("injected native item failure")

                event.listen(database._engine.sync_engine, "before_cursor_execute", fail_item)
                try:
                    with pytest.raises(RuntimeError, match="injected native item failure"):
                        await repository.confirm(**kwargs)
                finally:
                    event.remove(database._engine.sync_engine, "before_cursor_execute", fail_item)
                async with database.session() as session:
                    for table in (
                        "archive_cleanup_jobs",
                        "archive_cleanup_items",
                        "durable_wakeups",
                    ):
                        assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM audit_events "
                                "WHERE event_type='archive_cleanup.job_confirmed'"
                            )
                        )
                        == 0
                    )
            job = await repository.confirm(**kwargs)
            assert job.item_count == 1
            assert (await repository.confirm(**kwargs)).job_id == job.job_id
            async with database.session() as session:
                for table in ("archive_cleanup_jobs", "archive_cleanup_items"):
                    assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 1
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM durable_wakeups WHERE entity_id=:id"),
                        {"id": UUID(job.job_id)},
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM audit_events "
                            "WHERE event_type='archive_cleanup.job_confirmed' AND entity_id=:id"
                        ),
                        {"id": UUID(job.job_id)},
                    )
                    == 1
                )
        finally:
            await database.dispose()

    asyncio.run(run())


def test_pg_audit_history_survives_budget_resets_and_expired_lease(m6_database):
    url, ids = m6_database

    async def run():
        database = AsyncDatabase(url)
        repository = SqlAlchemyPublicationRepository(database)
        intent_id = str(ids["copy_with_caption"])
        try:

            async def claim(now=NOW):
                return await repository.claim(
                    intent_id=intent_id, now=now, lease_duration=timedelta(minutes=5)
                )

            async def audit_rows():
                async with database.session() as session:
                    return (
                        await session.execute(
                            text(
                                "SELECT attempt_number, status, error_code, lease_token "
                                "FROM publish_attempts WHERE publication_intent_id=:id "
                                "ORDER BY attempt_number"
                            ),
                            {"id": ids["copy_with_caption"]},
                        )
                    ).all()

            for number in range(1, 7):
                if number == 1:
                    # Independent sessions competing for the same parent row must
                    # never allocate the same history number or both obtain a claim.
                    claims = await asyncio.gather(claim(), claim())
                    winners = [value for value in claims if value is not None]
                    assert len(winners) == 1
                    current = winners[0]
                else:
                    current = await claim()
                assert current.attempt_number == 1 and current.max_attempts == 5
                rows = await audit_rows()
                assert [row.attempt_number for row in rows] == list(range(1, number + 1))
                assert await repository.retry_or_fail(
                    claim=current,
                    error_code="TEST_TERMINAL",
                    error_type="TestFailure",
                    retryable=False,
                    retry_at=NOW,
                    now=NOW,
                )
                # This is the M6-compatible state transition used by an admin retry;
                # max_attempts, frozen identity, random IDs and all audit rows survive.
                async with database.session() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "UPDATE publication_intents SET status='pending', attempt_count=0, "
                                "completed_at=NULL, next_retry_at=NULL, last_error_code=NULL, "
                                "last_error_type=NULL, last_failure_at=NULL WHERE id=:id"
                            ),
                            {"id": ids["copy_with_caption"]},
                        )

            seventh = await claim()
            assert seventh.attempt_number == 1
            assert (await audit_rows())[-1].attempt_number == 7
            retry_at = NOW + timedelta(minutes=1)
            assert await repository.retry_or_fail(
                claim=seventh,
                error_code="TEST_RETRY",
                error_type="TestRetry",
                retryable=True,
                retry_at=retry_at,
                now=NOW,
            )
            assert (await audit_rows())[-1].status == "retry_wait"
            eighth = await claim(retry_at)
            assert eighth.attempt_number == 2
            assert await repository.begin_part(
                claim=eighth,
                part=PublicationPart.MAIN,
                operation=PublicationOperation.COPY_WITH_CAPTION,
                now=retry_at,
            )
            async with database.session() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "UPDATE publication_intents SET lease_expires_at=:expired WHERE id=:id"
                        ),
                        {
                            "id": ids["copy_with_caption"],
                            "expired": retry_at - timedelta(seconds=1),
                        },
                    )
            ninth = await claim(retry_at)
            assert ninth.attempt_number == 3
            assert ninth.in_flight_part is PublicationPart.MAIN
            assert ninth.main_random_ids == eighth.main_random_ids == seventh.main_random_ids
            rows = await audit_rows()
            assert [row.attempt_number for row in rows] == list(range(1, 10))
            assert rows[7].error_code == "PUBLICATION_LEASE_EXPIRED"
            assert rows[7].lease_token == UUID(eighth.lease_token)
            assert rows[8].status == "sending"
            assert rows[6].error_code == "TEST_RETRY"
            assert not await repository.complete(claim=eighth, now=retry_at)
            async with database.session() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "UPDATE publication_intents SET attempt_count=max_attempts, "
                            "lease_expires_at=:expired WHERE id=:id"
                        ),
                        {
                            "id": ids["copy_with_caption"],
                            "expired": retry_at - timedelta(seconds=1),
                        },
                    )
            assert await claim(retry_at) is None
            rows = await audit_rows()
            assert len(rows) == 9
            assert rows[8].status == "failed"
            assert rows[8].error_code == "PUBLICATION_ATTEMPTS_EXHAUSTED"
            assert rows[7].error_code == "PUBLICATION_LEASE_EXPIRED"
            async with database.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT max_attempts FROM publication_intents WHERE id=:id"),
                        {"id": ids["copy_with_caption"]},
                    )
                    == 5
                )
        finally:
            await database.dispose()

    asyncio.run(run())


def test_pg_routing_action_schema_defaults_and_real_mapper_insert(m6_database):
    url, ids = m6_database

    async def run():
        database = AsyncDatabase(url)
        explicit_id, default_id = uuid4(), uuid4()
        try:
            async with database.session() as session:
                async with session.begin():
                    columns = (
                        (
                            await session.execute(
                                text(
                                    "SELECT column_name, is_nullable, column_default, "
                                    "character_maximum_length "
                                    "FROM information_schema.columns WHERE table_schema='public' "
                                    "AND table_name='routing_actions' "
                                    "AND column_name IN ('protected_content_fallback', "
                                    "'supplement_as_reply')"
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    definitions = {column["column_name"]: column for column in columns}
                    fallback = definitions["protected_content_fallback"]
                    reply = definitions["supplement_as_reply"]
                    assert fallback["is_nullable"] == reply["is_nullable"] == "NO"
                    assert fallback["column_default"].startswith("'fail'")
                    assert fallback["character_maximum_length"] == 24
                    assert reply["column_default"] == "true"
                    legacy = (await session.execute(select(RoutingActionRecord))).scalars().all()
                    assert len(legacy) == 4
                    assert all(row.protected_content_fallback == "fail" for row in legacy)
                    assert all(row.supplement_as_reply is True for row in legacy)
                    common = dict(
                        routing_rule_id=ids["rule"],
                        destination_channel_id=ids["destination"],
                        publication_mode="copy_with_caption",
                        rendering_template_version_id=ids["template_version"],
                    )
                    session.add(
                        RoutingActionRecord(
                            id=explicit_id,
                            output_order=4,
                            protected_content_fallback="metadata_only",
                            supplement_as_reply=False,
                            **common,
                        )
                    )
                    session.add(RoutingActionRecord(id=default_id, output_order=5, **common))
            async with database.session() as session:
                explicit = await session.get(RoutingActionRecord, explicit_id)
                default = await session.get(RoutingActionRecord, default_id)
                assert explicit.protected_content_fallback == "metadata_only"
                assert explicit.supplement_as_reply is False
                assert default.protected_content_fallback == "fail"
                assert default.supplement_as_reply is True
            async with database.session() as session:
                async with session.begin():
                    for change, error in (
                        (
                            "protected_content_fallback='invalid'",
                            "routing_action_protected_fallback",
                        ),
                        ("protected_content_fallback=NULL", "not-null constraint"),
                        ("supplement_as_reply=NULL", "not-null constraint"),
                    ):
                        with pytest.raises(DBAPIError, match=error):
                            async with session.begin_nested():
                                await session.execute(
                                    text(f"UPDATE routing_actions SET {change} WHERE id=:id"),
                                    {"id": explicit_id},
                                )
                    await session.execute(
                        text(
                            "UPDATE routing_policy_versions SET state='published', "
                            "published_at=:now "
                            "WHERE id=:id"
                        ),
                        {"id": ids["policy_version"], "now": NOW},
                    )
            async with database.session() as session:
                with pytest.raises(DBAPIError, match="draft"):
                    async with session.begin():
                        await session.execute(
                            text(
                                "UPDATE routing_actions SET supplement_as_reply=true WHERE id=:id"
                            ),
                            {"id": explicit_id},
                        )
        finally:
            await database.dispose()

    asyncio.run(run())
