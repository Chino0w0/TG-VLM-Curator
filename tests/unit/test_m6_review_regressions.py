"""M6 regressions: real ORM FK writes and offline migration/publication contracts.

SQLite checks FK ordering/rollback, not PostgreSQL trigger semantics. Native
PostgreSQL gates live in tests/integration/test_m6_postgres_regressions.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    JSON,
    Column,
    ForeignKeyConstraint,
    MetaData,
    Table,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from tests.unit import test_cleanup_repository as cleanup
from tests.unit import test_publication_repository as pubrepo
from tests.unit import test_publication_worker as publication
from tgcurator.application.ports.cleanup import ArchiveCleanupFilter
from tgcurator.application.ports.publishing import (
    TelegramFloodWait,
    TelegramProtectedContent,
    TelegramPublishResult,
)
from tgcurator.application.publishing import PublicationWorker
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
)
from tgcurator.infrastructure.database.cleanup_repository import SqlAlchemyArchiveCleanupRepository
from tgcurator.infrastructure.database.models import (
    ArchiveCleanupItemRecord,
    ArchiveCleanupJobRecord,
    AuditEvent,
    DurableWakeupRecord,
    PublishAttemptRecord,
)
from tgcurator.infrastructure.database.publication_repository import SqlAlchemyPublicationRepository


@pytest.mark.parametrize("fail_item_insert", [False, True])
def test_nonempty_confirmation_inserts_parent_first_and_is_atomic(fail_item_insert):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    # Production mappers/UoW, not a fake add() recorder. Adapt only SQLite DDL.
    metadata = MetaData()
    models = (ArchiveCleanupJobRecord, ArchiveCleanupItemRecord, DurableWakeupRecord, AuditEvent)
    for model in models:
        original = model.__table__
        columns = [
            Column(
                column.name,
                JSON() if isinstance(column.type, JSONB) else column.type,
                primary_key=column.primary_key,
                nullable=column.nullable,
            )
            for column in original.columns
        ]
        constraints = (
            [ForeignKeyConstraint(["cleanup_job_id"], ["archive_cleanup_jobs.id"])]
            if model is ArchiveCleanupItemRecord
            else []
        )
        Table(original.name, metadata, *columns, *constraints)
    metadata.create_all(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT"):
            statements.append(statement)
        if fail_item_insert and statement.startswith("INSERT INTO archive_cleanup_items"):
            raise RuntimeError("injected item insert failure")

    cleanup_filter = ArchiveCleanupFilter(media_types=("image", "video"))
    with Session(engine) as real_session:

        class SessionAdapter:
            def __init__(self):
                self.scalar_results = [cleanup._dry_run(cleanup_filter), None]
                self.execute_results = [
                    cleanup._Rows((cleanup._candidate_row(),)),
                    cleanup._Rows((cleanup._candidate_row(asset_kind="video"),)),
                ]

            @asynccontextmanager
            async def begin(self):
                with real_session.begin():
                    yield

            async def scalar(self, statement):
                return self.scalar_results.pop(0)

            async def execute(self, statement):
                return self.execute_results.pop(0)

            def add(self, value):
                real_session.add(value)

            async def flush(self):
                real_session.flush()

        class Database:
            @asynccontextmanager
            async def session(self):
                yield adapter

        adapter = SessionAdapter()
        repository = SqlAlchemyArchiveCleanupRepository(Database())
        call = repository.confirm(
            dry_run_id=str(cleanup.DRY_RUN_ID),
            expected_filter_hash=cleanup_filter.sha256,
            confirmed_by_admin_user_id=str(cleanup.ADMIN_ID),
            now=cleanup.NOW,
        )
        try:
            if fail_item_insert:
                with pytest.raises(RuntimeError, match="injected item insert failure"):
                    asyncio.run(call)
            else:
                result = asyncio.run(call)
                assert result.item_count == 2
            job_index = next(
                index
                for index, sql in enumerate(statements)
                if sql.startswith("INSERT INTO archive_cleanup_jobs")
            )
            item_index = next(
                index
                for index, sql in enumerate(statements)
                if sql.startswith("INSERT INTO archive_cleanup_items")
            )
            assert job_index < item_index
            counts = [real_session.query(model).count() for model in models]
            assert counts == ([0, 0, 0, 0] if fail_item_insert else [1, 2, 1, 1])
        finally:
            real_session.rollback()
    engine.dispose()


def _migration_sql(direction):
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/e6d4a8b2c951_m6_publication_cleanup_operations.py"
    )
    spec = importlib.util.spec_from_file_location("m6_review_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, direction)()
    return output.getvalue()


def test_legacy_intent_backfill_is_locked_and_trigger_bypass_is_transaction_scoped():
    sql = _migration_sql("upgrade")
    ordered = [
        "BEGIN;",
        "LOCK TABLE publication_intents IN ACCESS EXCLUSIVE MODE;",
        "ALTER TABLE publication_intents DISABLE TRIGGER "
        "trg_publication_intents_identity_immutable;",
        "UPDATE publication_intents SET rendered_text = '', rendered_snapshot_hash = ",
        "ALTER TABLE publication_intents ENABLE TRIGGER "
        "trg_publication_intents_identity_immutable;",
        "CREATE OR REPLACE FUNCTION tgcurator_enforce_publication_intent_identity()",
        "COMMIT;",
    ]
    positions = [sql.index(statement) for statement in ordered]
    assert positions == sorted(positions)
    assert sql.count("DISABLE TRIGGER") == sql.count("ENABLE TRIGGER") == 1
    assert "WHERE publication_mode <> 'forward_only' AND rendered_text IS NULL" in sql
    assert "session_replication_role" not in sql
    assert sql.count("COMMIT;") == 1


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_restores_identity_and_delete_protection(direction):
    sql = _migration_sql(direction)
    function = sql.split(
        "CREATE OR REPLACE FUNCTION tgcurator_enforce_publication_intent_identity()", 1
    )[1].split("END;'", 1)[0]
    assert "IF TG_OP = ''DELETE''" in function
    assert "publication intents cannot be deleted" in function
    assert "publication intent identity is immutable" in function
    assert "IS DISTINCT FROM" in function
    assert "to_jsonb(NEW) - ''status'' - ''updated_at''" in function
    assert "to_jsonb(OLD) - ''status'' - ''updated_at''" in function
    for immutable in ("rendered_text", "rendered_snapshot_hash", "publication_mode"):
        assert f"- ''{immutable}''" not in function
    assert ("- ''main_random_ids''" in function) is (direction == "upgrade")
    if direction == "downgrade":
        assert "DISABLE TRIGGER" not in sql
        assert sql.index("CREATE OR REPLACE FUNCTION") < sql.index(
            "ALTER TABLE publication_intents DROP COLUMN"
        )


@pytest.mark.parametrize("phase", ["first", "reconcile", "resend"])
@pytest.mark.parametrize("fallback", list(ProtectedContentFallback))
@pytest.mark.parametrize(
    "mode, operation",
    [
        (PublicationMode.COPY_WITH_CAPTION, PublicationOperation.COPY_WITH_CAPTION),
        (PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT, PublicationOperation.NATIVE_FORWARD),
    ],
)
def test_protected_fallback_matches_first_execution_during_recovery(
    phase, fallback, mode, operation
):
    claim = publication._claim(
        mode=mode,
        protected_content_fallback=fallback,
        in_flight_part=PublicationPart.MAIN if phase != "first" else None,
        in_flight_operation=operation if phase != "first" else None,
    )
    events = []
    repository = publication._Repository(claim=claim, events=events)
    publisher = publication._Publisher(
        reconcile_results=[TelegramProtectedContent()] if phase == "reconcile" else [None],
        publish_results=([] if phase == "reconcile" else [TelegramProtectedContent()])
        + [TelegramPublishResult((991,)), TelegramPublishResult((992,))],
        events=events,
    )
    assert asyncio.run(
        PublicationWorker(repository, publisher).process(
            intent_id=claim.intent_id, now=publication.NOW
        )
    )
    assert len(publisher.reconcile_requests) == (0 if phase == "first" else 1)
    if fallback is ProtectedContentFallback.METADATA_ONLY:
        assert repository.completed
        assert not repository.failures and not repository.cancellations
        assert repository.records[0] == (
            PublicationPart.MAIN,
            PublicationOperation.SEND_TEXT,
            (991,),
            False,
        )
        assert events.index(("begin", PublicationOperation.SEND_TEXT)) < events.index(
            ("publish", PublicationOperation.SEND_TEXT)
        )
        text_request = next(
            request
            for request in publisher.publish_requests
            if request.operation is PublicationOperation.SEND_TEXT
        )
        assert text_request.random_ids == (claim.main_random_ids[0],)
        assert text_request.text == claim.rendered_text
        assert len(repository.records) == 1
        assert [
            request.part
            for request in publisher.publish_requests
            if request.operation is PublicationOperation.SEND_TEXT
        ] == [PublicationPart.MAIN]
    else:
        assert not repository.completed and not repository.records
        assert bool(repository.failures) is (fallback is ProtectedContentFallback.FAIL)
        assert bool(repository.cancellations) is (fallback is ProtectedContentFallback.SKIP)
        assert not any(
            request.operation is PublicationOperation.SEND_TEXT
            for request in publisher.publish_requests
        )


@pytest.mark.parametrize(
    "mode, operation",
    [
        (PublicationMode.COPY_WITH_CAPTION, PublicationOperation.SEND_TEXT),
        (PublicationMode.FORWARD_ONLY, PublicationOperation.NATIVE_FORWARD),
    ],
)
def test_recovered_metadata_fallback_does_not_loop_without_a_usable_rendering(mode, operation):
    claim = publication._claim(
        mode=mode,
        protected_content_fallback=ProtectedContentFallback.METADATA_ONLY,
        in_flight_part=PublicationPart.MAIN,
        in_flight_operation=operation,
    )
    repository = publication._Repository(claim=claim)
    publisher = publication._Publisher(reconcile_results=[TelegramProtectedContent()])
    assert asyncio.run(
        PublicationWorker(repository, publisher).process(
            intent_id=claim.intent_id, now=publication.NOW
        )
    )
    assert len(repository.failures) == 1
    assert not publisher.publish_requests


def test_recovery_of_persisted_text_fallback_reconciles_without_resending_media():
    claim = publication._claim(
        mode=PublicationMode.COPY_WITH_CAPTION,
        protected_content_fallback=ProtectedContentFallback.METADATA_ONLY,
        in_flight_part=PublicationPart.MAIN,
        in_flight_operation=PublicationOperation.SEND_TEXT,
    )
    repository = publication._Repository(claim=claim)
    publisher = publication._Publisher(reconcile_results=[TelegramPublishResult((991,))])
    assert asyncio.run(
        PublicationWorker(repository, publisher).process(
            intent_id=claim.intent_id, now=publication.NOW
        )
    )
    assert repository.records == [
        (PublicationPart.MAIN, PublicationOperation.SEND_TEXT, (991,), True)
    ]
    assert publisher.reconcile_requests[0].random_ids == (claim.main_random_ids[0],)
    assert not publisher.publish_requests
    assert repository.completed


def test_flood_wait_from_recovered_fallback_preserves_retry_policy():
    claim = publication._claim(
        mode=PublicationMode.COPY_WITH_CAPTION,
        protected_content_fallback=ProtectedContentFallback.METADATA_ONLY,
        in_flight_part=PublicationPart.MAIN,
        in_flight_operation=PublicationOperation.COPY_WITH_CAPTION,
    )
    repository = publication._Repository(claim=claim)
    publisher = publication._Publisher(
        reconcile_results=[TelegramProtectedContent()],
        publish_results=[TelegramFloodWait(retry_at=publication.NOW)],
    )
    assert asyncio.run(
        PublicationWorker(repository, publisher).process(
            intent_id=claim.intent_id, now=publication.NOW
        )
    )
    assert repository.failures[0][0] == "TELEGRAM_FLOOD_WAIT"
    assert repository.failures[0][2] is True
    assert repository.failures[0][3] == publication.NOW
    assert not repository.records and not repository.completed


@pytest.mark.parametrize("scenario", ["fresh", "manual_reset", "expired", "exhausted"])
def test_claim_audit_sequence_is_independent_of_retry_budget_and_closes_only_its_lease(scenario):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for model in (PublishAttemptRecord, DurableWakeupRecord):
        original = model.__table__
        columns = [
            Column(
                c.name,
                c.type,
                primary_key=c.primary_key,
                nullable=c.nullable,
                server_default=c.server_default,
            )
            for c in original.columns
        ]
        constraints = (
            [
                UniqueConstraint("publication_intent_id", "attempt_number"),
                UniqueConstraint("lease_token"),
            ]
            if model is PublishAttemptRecord
            else []
        )
        Table(original.name, metadata, *columns, *constraints)
    metadata.create_all(engine)
    expired = scenario in ("expired", "exhausted")
    old_lease = uuid4()
    intent = pubrepo._intent(
        mode=PublicationMode.COPY_WITH_CAPTION,
        status="sending" if expired else "pending",
        attempt_count=(5 if scenario == "exhausted" else 1) if expired else 0,
        lease_token=old_lease if expired else None,
        lease_expires_at=pubrepo.NOW - timedelta(seconds=1) if expired else None,
        in_flight_part="main" if expired else None,
        in_flight_operation="copy_with_caption" if expired else None,
    )
    new_lease, new_attempt_id = uuid4(), uuid4()
    with Session(engine, expire_on_commit=False) as real_session:

        def attempt(number, status="failed", *, lease=None, intent_id=pubrepo.INTENT_ID):
            return PublishAttemptRecord(
                id=uuid4(),
                publication_intent_id=intent_id,
                attempt_number=number,
                lease_token=lease or uuid4(),
                status=status,
                reconciled=False,
                started_at=pubrepo.NOW - timedelta(minutes=10),
                completed_at=None if status == "sending" else pubrepo.NOW,
            )

        history = [] if scenario == "fresh" else [attempt(1), attempt(7)]
        # Deliberate gap plus another intent proves max-per-intent, not count/global-max.
        unrelated = attempt(99, intent_id=uuid4())
        old_open = attempt(8, "sending", lease=old_lease) if expired else None
        other_open = attempt(3, "sending") if expired else None
        with real_session.begin():
            real_session.add_all(history + [unrelated])
            if expired:
                real_session.add_all([old_open, other_open])

        class SessionAdapter:
            selected = False

            @asynccontextmanager
            async def begin(self):
                with real_session.begin():
                    yield

            async def execute(self, statement):
                sql = pubrepo._sql(statement)
                if "FROM publication_intents JOIN messages" in sql:
                    assert "FOR UPDATE OF publication_intents SKIP LOCKED" in sql
                    self.selected = True
                    return pubrepo._Result(
                        (intent, pubrepo._message(), pubrepo._source(), pubrepo._destination())
                    )
                return real_session.execute(statement)

            async def scalar(self, statement):
                # The audit max must be read after obtaining the parent row lock.
                assert self.selected
                return real_session.scalar(statement)

            def add(self, value):
                real_session.add(value)

        adapter = SessionAdapter()
        repository = SqlAlchemyPublicationRepository(
            pubrepo._Database(adapter),
            id_factory=pubrepo._IdFactory(new_lease, new_attempt_id),
        )
        claim = asyncio.run(
            repository.claim(
                intent_id=str(intent.id), now=pubrepo.NOW, lease_duration=timedelta(minutes=5)
            )
        )
        real_session.expire_all()
        assert intent.max_attempts == 5
        if scenario == "exhausted":
            assert claim is None
            assert intent.status == "failed"
            assert real_session.get(PublishAttemptRecord, new_attempt_id) is None
            assert old_open.status == "failed"
            assert old_open.error_code == "PUBLICATION_ATTEMPTS_EXHAUSTED"
        else:
            assert claim is not None
            audit = real_session.get(PublishAttemptRecord, new_attempt_id)
            assert audit.attempt_number == {"fresh": 1, "manual_reset": 8, "expired": 9}[scenario]
            # ClaimedPublicationIntent.attempt_number remains the current budget ordinal:
            # worker retry telemetry and <= max_attempts validation depend on that contract.
            assert claim.attempt_number == (2 if expired else 1)
            assert intent.attempt_count == claim.attempt_number
            assert audit.lease_token == new_lease
            if expired:
                assert old_open.status == "retry_wait"
                assert old_open.error_code == "PUBLICATION_LEASE_EXPIRED"
                assert claim.in_flight_part is PublicationPart.MAIN
                assert claim.in_flight_operation is PublicationOperation.COPY_WITH_CAPTION
        assert unrelated.attempt_number == 99 and unrelated.status == "failed"
        for previous in history:
            assert previous.status == "failed"
            assert previous.error_code is None
        if expired:
            assert other_open.status == "sending" and other_open.error_code is None
    engine.dispose()


def test_routing_action_m6_columns_defaults_check_and_downgrade_are_present():
    upgrade = _migration_sql("upgrade")
    assert "ALTER TABLE routing_actions ADD COLUMN protected_content_fallback " in upgrade
    assert "VARCHAR(24) DEFAULT 'fail' NOT NULL" in upgrade
    assert "ALTER TABLE routing_actions ADD COLUMN supplement_as_reply " in upgrade
    assert "BOOLEAN DEFAULT true NOT NULL" in upgrade
    check_name = "ck_routing_actions_routing_action_protected_fallback"
    assert f"ADD CONSTRAINT {check_name} CHECK" in upgrade
    assert "protected_content_fallback IN ('fail', 'metadata_only', 'skip')" in upgrade
    downgrade = _migration_sql("downgrade")
    assert f"ALTER TABLE routing_actions DROP CONSTRAINT {check_name}" in downgrade
    assert "ALTER TABLE routing_actions DROP COLUMN supplement_as_reply" in downgrade
    assert "ALTER TABLE routing_actions DROP COLUMN protected_content_fallback" in downgrade
