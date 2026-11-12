from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.publishing import ClaimedPublicationIntent
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
    stable_random_id,
    stable_random_ids,
)
from tgcurator.infrastructure.database.models import (
    DestinationChannel,
    MessageRecord,
    PublicationIntentRecord,
    PublishAttemptRecord,
    PublishRecord,
    SourceChannel,
)
from tgcurator.infrastructure.database.publication_repository import (
    SqlAlchemyPublicationRepository,
    publication_complete_wakeup_statement,
    publication_intent_claim_statement,
    publication_repair_candidates_statement,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
RETRY_AT = NOW + timedelta(minutes=17)
INTENT_ID = UUID("11111111-1111-4111-8111-111111111111")
MESSAGE_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
DESTINATION_ID = UUID("44444444-4444-4444-8444-444444444444")
IDENTITY_ID = UUID("55555555-5555-4555-8555-555555555555")
EVALUATION_ID = UUID("66666666-6666-4666-8666-666666666666")
POLICY_ID = UUID("77777777-7777-4777-8777-777777777777")
RULE_ID = UUID("88888888-8888-4888-8888-888888888888")
ACTION_ID = UUID("99999999-9999-4999-8999-999999999999")
TEMPLATE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
LEASE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ATTEMPT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
RECORD_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
WAKEUP_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


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


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: object) -> dict[str, object]:
    return statement.compile(dialect=postgresql.dialect()).params


def _message(*, telegram_message_ids: list[int] | None = None) -> MessageRecord:
    values = telegram_message_ids or [101, 102]
    return MessageRecord(
        id=MESSAGE_ID,
        source_channel_id=SOURCE_ID,
        telegram_message_ids=values,
        primary_telegram_message_id=values[0],
        published_at=NOW - timedelta(days=1),
    )


def _source() -> SourceChannel:
    return SourceChannel(id=SOURCE_ID, telegram_peer_id=-1001234567890, display_name="source")


def _destination() -> DestinationChannel:
    return DestinationChannel(
        id=DESTINATION_ID,
        name="destination",
        telegram_peer_id=-1009876543210,
        default_publish_identity_id=IDENTITY_ID,
    )


def _intent(
    *,
    mode: PublicationMode = PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT,
    status: str = "pending",
    attempt_count: int = 0,
    max_attempts: int = 5,
    lease_token: UUID | None = None,
    lease_expires_at: datetime | None = None,
    next_retry_at: datetime | None = None,
    completed_parts: list[str] | None = None,
    main_remote_message_ids: list[int] | None = None,
    in_flight_part: str | None = None,
    in_flight_operation: str | None = None,
    main_random_ids: list[int] | None = None,
    supplement_random_id: int | None = None,
) -> PublicationIntentRecord:
    rendered = mode is not PublicationMode.FORWARD_ONLY
    return PublicationIntentRecord(
        id=INTENT_ID,
        routing_evaluation_id=EVALUATION_ID,
        routing_request_id="route-request",
        message_id=MESSAGE_ID,
        routing_policy_version_id=POLICY_ID,
        routing_rule_id=RULE_ID,
        routing_action_id=ACTION_ID,
        destination_channel_id=DESTINATION_ID,
        publish_identity_id=IDENTITY_ID,
        publication_mode=mode.value,
        protected_content_fallback=ProtectedContentFallback.METADATA_ONLY.value,
        supplement_as_reply=True,
        rendering_template_version_id=TEMPLATE_ID if rendered else None,
        rendered_text="summary" if rendered else None,
        rendered_context={"message": {"id": str(MESSAGE_ID)}} if rendered else {},
        rendered_snapshot_hash="a" * 64 if rendered else None,
        business_idempotency_key="publication:v2:test",
        status=status,
        main_random_ids=list(main_random_ids or []),
        supplement_random_id=supplement_random_id,
        completed_parts=list(completed_parts or []),
        main_remote_message_ids=list(main_remote_message_ids or []),
        in_flight_part=in_flight_part,
        in_flight_operation=in_flight_operation,
        in_flight_started_at=NOW if in_flight_part is not None else None,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        next_retry_at=next_retry_at,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        started_at=NOW if attempt_count else None,
    )


def _claim(
    *,
    mode: PublicationMode = PublicationMode.COPY_WITH_CAPTION,
    completed_parts: tuple[PublicationPart, ...] = (),
    in_flight_part: PublicationPart | None = None,
    in_flight_operation: PublicationOperation | None = None,
) -> ClaimedPublicationIntent:
    source_ids = (101, 102)
    main_ids = stable_random_ids(
        intent_id=str(INTENT_ID),
        part=PublicationPart.MAIN,
        count=1 if mode is PublicationMode.METADATA_ONLY else len(source_ids),
    )
    supplement_id = (
        stable_random_id(intent_id=str(INTENT_ID), part=PublicationPart.SUPPLEMENT)
        if mode is PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT
        else None
    )
    return ClaimedPublicationIntent(
        intent_id=str(INTENT_ID),
        attempt_id=str(ATTEMPT_ID),
        lease_token=str(LEASE_ID),
        attempt_number=1,
        max_attempts=5,
        message_id=str(MESSAGE_ID),
        source_channel_id=str(SOURCE_ID),
        source_peer_id=-1001234567890,
        source_message_ids=source_ids,
        destination_channel_id=str(DESTINATION_ID),
        destination_peer_id=-1009876543210,
        publish_identity_id=str(IDENTITY_ID),
        mode=mode,
        protected_content_fallback=ProtectedContentFallback.METADATA_ONLY,
        rendered_text=None if mode is PublicationMode.FORWARD_ONLY else "summary",
        main_random_ids=main_ids,
        supplement_random_id=supplement_id,
        supplement_as_reply=True,
        completed_parts=completed_parts,
        main_remote_message_ids=(9001, 9002) if PublicationPart.MAIN in completed_parts else (),
        in_flight_part=in_flight_part,
        in_flight_operation=in_flight_operation,
    )


class PublicationRepositoryStatementTests(unittest.TestCase):
    def test_claim_sql_joins_frozen_facts_and_uses_skip_locked(self) -> None:
        sql = _sql(publication_intent_claim_statement(intent_id=INTENT_ID, now=NOW))

        self.assertIn("FROM publication_intents JOIN messages", sql)
        self.assertIn("JOIN source_channels", sql)
        self.assertIn("JOIN destination_channels", sql)
        self.assertIn("publication_intents.next_retry_at <=", sql)
        self.assertIn("publication_intents.lease_expires_at <=", sql)
        self.assertIn("FOR UPDATE OF publication_intents SKIP LOCKED", sql)

    def test_terminal_wakeup_sql_requires_terminal_intent(self) -> None:
        sql = _sql(publication_complete_wakeup_statement(intent_id=INTENT_ID, now=NOW))

        self.assertIn("UPDATE durable_wakeups SET", sql)
        self.assertIn("EXISTS (SELECT publication_intents.id", sql)
        self.assertIn("publication_intents.status IN", sql)
        self.assertIn("durable_wakeups.status IN", sql)

    def test_repair_scan_includes_all_reconstructable_states(self) -> None:
        sql = _sql(publication_repair_candidates_statement(now=NOW, limit=50))

        self.assertIn("publication_intents.status IN", sql)
        self.assertIn("publication_intents.next_retry_at <=", sql)
        self.assertIn("publication_intents.lease_expires_at <=", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)


class PublicationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_persists_random_ids_and_one_attempt_before_returning(self) -> None:
        intent = _intent()
        message = _message()
        session = _Session(
            execute_results=[_Result((intent, message, _source(), _destination()))],
            scalar_results=[None],
        )
        repository = SqlAlchemyPublicationRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(LEASE_ID, ATTEMPT_ID),
        )

        claim = await repository.claim(
            intent_id=str(INTENT_ID),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        self.assertIsNotNone(claim)
        assert claim is not None
        expected_main = stable_random_ids(
            intent_id=str(INTENT_ID), part=PublicationPart.MAIN, count=2
        )
        expected_supplement = stable_random_id(
            intent_id=str(INTENT_ID), part=PublicationPart.SUPPLEMENT
        )
        self.assertEqual(intent.main_random_ids, list(expected_main))
        self.assertEqual(intent.supplement_random_id, expected_supplement)
        self.assertEqual(claim.main_random_ids, expected_main)
        self.assertEqual(claim.supplement_random_id, expected_supplement)
        self.assertEqual(intent.status, "sending")
        self.assertEqual(intent.attempt_count, 1)
        self.assertEqual(intent.lease_token, LEASE_ID)
        self.assertEqual(intent.lease_expires_at, NOW + timedelta(minutes=5))
        attempts = [value for value in session.added if isinstance(value, PublishAttemptRecord)]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].id, ATTEMPT_ID)
        self.assertEqual(attempts[0].attempt_number, 1)
        self.assertEqual(attempts[0].lease_token, LEASE_ID)

    async def test_metadata_only_claim_persists_exactly_one_main_random_id(self) -> None:
        intent = _intent(mode=PublicationMode.METADATA_ONLY)
        session = _Session(
            execute_results=[_Result((intent, _message(), _source(), _destination()))],
            scalar_results=[None],
        )
        repository = SqlAlchemyPublicationRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(LEASE_ID, ATTEMPT_ID),
        )

        claim = await repository.claim(
            intent_id=str(INTENT_ID),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        assert claim is not None
        self.assertEqual(len(claim.main_random_ids), 1)
        self.assertIsNone(claim.supplement_random_id)

    async def test_expired_lease_claim_preserves_unresolved_in_flight_state(self) -> None:
        old_lease = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
        intent = _intent(
            status="sending",
            attempt_count=1,
            lease_token=old_lease,
            lease_expires_at=NOW - timedelta(seconds=1),
            in_flight_part="main",
            in_flight_operation="native_forward",
            main_random_ids=list(
                stable_random_ids(intent_id=str(INTENT_ID), part=PublicationPart.MAIN, count=2)
            ),
            supplement_random_id=stable_random_id(
                intent_id=str(INTENT_ID), part=PublicationPart.SUPPLEMENT
            ),
        )
        session = _Session(
            execute_results=[_Result((intent, _message(), _source(), _destination()))],
            scalar_results=[1],
        )
        repository = SqlAlchemyPublicationRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(LEASE_ID, ATTEMPT_ID),
        )

        claim = await repository.claim(
            intent_id=str(INTENT_ID),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        assert claim is not None
        self.assertEqual(claim.attempt_number, 2)
        self.assertEqual(claim.in_flight_part, PublicationPart.MAIN)
        self.assertEqual(claim.in_flight_operation, PublicationOperation.NATIVE_FORWARD)
        self.assertEqual(intent.in_flight_part, "main")
        self.assertEqual(intent.in_flight_operation, "native_forward")
        previous_attempt_sql = _sql(session.execute_statements[1])
        previous_attempt_params = _params(session.execute_statements[1])
        self.assertIn("UPDATE publish_attempts SET", previous_attempt_sql)
        self.assertIn("PUBLICATION_LEASE_EXPIRED", previous_attempt_params.values())

    async def test_unclaimable_duplicate_delivery_does_not_create_attempt(self) -> None:
        session = _Session(execute_results=[_Result(None)])
        repository = SqlAlchemyPublicationRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(),
        )

        claim = await repository.claim(
            intent_id=str(INTENT_ID),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        self.assertIsNone(claim)
        self.assertEqual(session.added, [])

    async def test_begin_part_rejects_stale_lease_token(self) -> None:
        session = _Session(scalar_results=[None])
        repository = SqlAlchemyPublicationRepository(_Database(session))  # type: ignore[arg-type]

        updated = await repository.begin_part(
            claim=_claim(),
            part=PublicationPart.MAIN,
            operation=PublicationOperation.COPY_WITH_CAPTION,
            now=NOW,
        )

        self.assertFalse(updated)
        sql = _sql(session.scalar_statements[0])
        self.assertIn("publication_intents.lease_token =", sql)
        self.assertIn("publication_intents.lease_expires_at >", sql)

    async def test_begin_part_allows_protected_content_text_fallback_for_same_part(self) -> None:
        intent = _intent(
            mode=PublicationMode.COPY_WITH_CAPTION,
            status="sending",
            attempt_count=1,
            lease_token=LEASE_ID,
            lease_expires_at=NOW + timedelta(minutes=5),
            in_flight_part="main",
            in_flight_operation="copy_with_caption",
            main_random_ids=list(
                stable_random_ids(intent_id=str(INTENT_ID), part=PublicationPart.MAIN, count=2)
            ),
        )
        session = _Session(scalar_results=[intent])
        repository = SqlAlchemyPublicationRepository(_Database(session))  # type: ignore[arg-type]

        updated = await repository.begin_part(
            claim=_claim(),
            part=PublicationPart.MAIN,
            operation=PublicationOperation.SEND_TEXT,
            now=NOW,
        )

        self.assertTrue(updated)
        self.assertEqual(intent.in_flight_operation, "send_text")
        self.assertEqual(intent.in_flight_started_at, NOW)

    async def test_begin_part_rejects_text_fallback_when_policy_requires_fail(self) -> None:
        claim = _claim()
        fail_claim = ClaimedPublicationIntent(
            **{
                field: getattr(claim, field)
                for field in claim.__dataclass_fields__
                if field != "protected_content_fallback"
            },
            protected_content_fallback=ProtectedContentFallback.FAIL,
        )
        repository = SqlAlchemyPublicationRepository(_Database(_Session()))  # type: ignore[arg-type]

        with self.assertRaises(DomainValidationError):
            await repository.begin_part(
                claim=fail_claim,
                part=PublicationPart.MAIN,
                operation=PublicationOperation.SEND_TEXT,
                now=NOW,
            )

    async def test_record_part_inserts_incremental_record_and_updates_progress(self) -> None:
        random_ids = stable_random_ids(intent_id=str(INTENT_ID), part=PublicationPart.MAIN, count=2)
        intent = _intent(
            mode=PublicationMode.COPY_WITH_CAPTION,
            status="sending",
            attempt_count=1,
            lease_token=LEASE_ID,
            lease_expires_at=NOW + timedelta(minutes=5),
            in_flight_part="main",
            in_flight_operation="copy_with_caption",
            main_random_ids=list(random_ids),
        )
        session = _Session(scalar_results=[intent])
        repository = SqlAlchemyPublicationRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(RECORD_ID),
        )

        recorded = await repository.record_part(
            claim=_claim(),
            part=PublicationPart.MAIN,
            operation=PublicationOperation.COPY_WITH_CAPTION,
            remote_message_ids=(9001, 9002),
            reconciled=False,
            now=NOW,
        )

        self.assertTrue(recorded)
        self.assertEqual(intent.completed_parts, ["main"])
        self.assertEqual(intent.main_remote_message_ids, [9001, 9002])
        self.assertIsNone(intent.in_flight_part)
        records = [value for value in session.added if isinstance(value, PublishRecord)]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].id, RECORD_ID)
        self.assertEqual(records[0].publish_attempt_id, ATTEMPT_ID)
        self.assertEqual(records[0].random_ids, list(random_ids))
        self.assertEqual(records[0].rendered_text, "summary")
        self.assertEqual(records[0].rendered_snapshot_hash, "a" * 64)

    async def test_retry_preserves_in_flight_state_and_schedules_exact_floodwait(self) -> None:
        intent = _intent(
            mode=PublicationMode.COPY_WITH_CAPTION,
            status="sending",
            attempt_count=1,
            lease_token=LEASE_ID,
            lease_expires_at=NOW + timedelta(minutes=5),
            in_flight_part="main",
            in_flight_operation="copy_with_caption",
            main_random_ids=list(
                stable_random_ids(intent_id=str(INTENT_ID), part=PublicationPart.MAIN, count=2)
            ),
        )
        session = _Session(scalar_results=[intent])
        repository = SqlAlchemyPublicationRepository(_Database(session))  # type: ignore[arg-type]

        updated = await repository.retry_or_fail(
            claim=_claim(),
            error_code="TELEGRAM_FLOOD_WAIT",
            error_type="FloodWaitError",
            retryable=True,
            retry_at=RETRY_AT,
            now=NOW,
        )

        self.assertTrue(updated)
        self.assertEqual(intent.status, "retry_wait")
        self.assertEqual(intent.next_retry_at, RETRY_AT)
        self.assertEqual(intent.in_flight_part, "main")
        self.assertEqual(intent.in_flight_operation, "copy_with_caption")
        self.assertIsNone(intent.lease_token)
        wakeup_params = _params(session.execute_statements[1])
        self.assertEqual(wakeup_params["next_attempt_at"], RETRY_AT)
        self.assertEqual(wakeup_params["status"], "pending")

    async def test_complete_marks_attempt_intent_and_wakeup_terminal(self) -> None:
        random_ids = stable_random_ids(intent_id=str(INTENT_ID), part=PublicationPart.MAIN, count=2)
        intent = _intent(
            mode=PublicationMode.COPY_WITH_CAPTION,
            status="sending",
            attempt_count=1,
            lease_token=LEASE_ID,
            lease_expires_at=NOW + timedelta(minutes=5),
            completed_parts=["main"],
            main_remote_message_ids=[9001, 9002],
            main_random_ids=list(random_ids),
        )
        session = _Session(scalar_results=[intent])
        repository = SqlAlchemyPublicationRepository(_Database(session))  # type: ignore[arg-type]

        completed = await repository.complete(claim=_claim(), now=NOW)

        self.assertTrue(completed)
        self.assertEqual(intent.status, "sent")
        self.assertEqual(intent.completed_at, NOW)
        self.assertIsNone(intent.lease_token)
        attempt_params = _params(session.execute_statements[0])
        wakeup_params = _params(session.execute_statements[1])
        self.assertEqual(attempt_params["status"], "sent")
        self.assertEqual(wakeup_params["status"], "completed")
        self.assertEqual(wakeup_params["completed_at"], NOW)

    async def test_attempt_exhaustion_terminalizes_intent_without_new_attempt(self) -> None:
        intent = _intent(status="pending", attempt_count=5, max_attempts=5)
        session = _Session(
            execute_results=[_Result((intent, _message(), _source(), _destination()))]
        )
        repository = SqlAlchemyPublicationRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(),
        )

        claim = await repository.claim(
            intent_id=str(INTENT_ID),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        self.assertIsNone(claim)
        self.assertEqual(intent.status, "failed")
        self.assertEqual(intent.last_error_code, "PUBLICATION_ATTEMPTS_EXHAUSTED")
        self.assertEqual(intent.completed_at, NOW)
        self.assertEqual(session.added, [])
        wakeup_params = _params(session.execute_statements[1])
        self.assertEqual(wakeup_params["status"], "completed")

    async def test_repair_wakeups_recreates_uuid_only_signals_after_broker_loss(self) -> None:
        pending = _intent()
        expired = _intent(
            status="sending",
            attempt_count=1,
            lease_token=LEASE_ID,
            lease_expires_at=NOW - timedelta(seconds=1),
        )
        expired.id = UUID("12111111-1111-4111-8111-111111111111")
        session = _Session(
            scalar_batches=[(pending, expired)],
            scalar_results=[WAKEUP_ID, UUID("13111111-1111-4111-8111-111111111111")],
        )
        repository = SqlAlchemyPublicationRepository(
            _Database(session),  # type: ignore[arg-type]
            id_factory=_IdFactory(
                UUID("14111111-1111-4111-8111-111111111111"),
                UUID("15111111-1111-4111-8111-111111111111"),
            ),
        )

        repaired = await repository.repair_wakeups(now=NOW, limit=10)

        self.assertEqual(repaired, 2)
        self.assertEqual(len(session.scalar_statements), 2)
        for statement in session.scalar_statements:
            sql = _sql(statement)
            params = _params(statement)
            self.assertIn("INSERT INTO durable_wakeups", sql)
            self.assertIn("ON CONFLICT (queue, entity_id) DO UPDATE", sql)
            self.assertEqual(params["queue"], "publication")
            self.assertEqual(params["status"], "pending")
            self.assertEqual(params["next_attempt_at"], NOW)

    async def test_complete_wakeup_if_terminal_supports_duplicate_consumption(self) -> None:
        session = _Session(execute_results=[_Result(rowcount=1)])
        repository = SqlAlchemyPublicationRepository(_Database(session))  # type: ignore[arg-type]

        completed = await repository.complete_wakeup_if_terminal(
            intent_id=str(INTENT_ID),
            now=NOW,
        )

        self.assertTrue(completed)
        self.assertIn("EXISTS (SELECT publication_intents.id", _sql(session.execute_statements[0]))

    async def test_release_transitions_completed_main_part_to_partial_and_rejects_in_flight(
        self,
    ) -> None:
        main_ids = stable_random_ids(intent_id=str(INTENT_ID), part=PublicationPart.MAIN, count=2)
        supplement_id = stable_random_id(intent_id=str(INTENT_ID), part=PublicationPart.SUPPLEMENT)
        partial_intent = _intent(
            mode=PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT,
            status="sending",
            attempt_count=1,
            lease_token=LEASE_ID,
            lease_expires_at=NOW + timedelta(minutes=5),
            completed_parts=["main"],
            main_remote_message_ids=[9001, 9002],
            main_random_ids=list(main_ids),
            supplement_random_id=supplement_id,
        )
        session = _Session(scalar_results=[partial_intent])
        repository = SqlAlchemyPublicationRepository(_Database(session))  # type: ignore[arg-type]

        released = await repository.release(
            claim=_claim(
                mode=PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT,
                completed_parts=(PublicationPart.MAIN,),
            ),
            now=NOW,
        )

        self.assertTrue(released)
        self.assertEqual(partial_intent.status, "partial")
        self.assertIsNone(partial_intent.lease_token)
        self.assertIsNone(partial_intent.lease_expires_at)
        self.assertIsNone(partial_intent.next_retry_at)
        attempt_params = _params(session.execute_statements[0])
        wakeup_params = _params(session.execute_statements[1])
        self.assertEqual(attempt_params["error_code"], "PUBLICATION_LEASE_RELEASED")
        self.assertEqual(wakeup_params["next_attempt_at"], NOW)

        in_flight_intent = _intent(
            mode=PublicationMode.COPY_WITH_CAPTION,
            status="sending",
            attempt_count=1,
            lease_token=LEASE_ID,
            lease_expires_at=NOW + timedelta(minutes=5),
            in_flight_part="main",
            in_flight_operation="copy_with_caption",
            main_random_ids=list(main_ids),
        )
        in_flight_session = _Session(scalar_results=[in_flight_intent])
        in_flight_repo = SqlAlchemyPublicationRepository(_Database(in_flight_session))  # type: ignore[arg-type]
        self.assertFalse(await in_flight_repo.release(claim=_claim(), now=NOW))
        self.assertEqual(in_flight_intent.status, "sending")


if __name__ == "__main__":
    unittest.main()
