from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.routing import (
    PublicationIntentDraft,
    RoutingEvaluationDraft,
)
from tgcurator.domain.routing import PublicationAction, RoutingDecision, RuleOutcome
from tgcurator.infrastructure.database.models import (
    AnalysisRunRecord,
    DestinationChannel,
    ManualLabelAssignmentRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    PublicationIntentRecord,
    RenderingTemplateVersionRecord,
    RoutingActionRecord,
    RoutingEvaluationRecord,
    RoutingPolicyVersionRecord,
    RoutingRuleRecord,
    SourceChannel,
    TelegramIdentity,
)
from tgcurator.infrastructure.database.routing_repository import (
    SqlAlchemyRoutingRepository,
    _load_execution_by_request,
    _rule_outcome_from_snapshot,
    routing_evaluation_insert_statement,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
MESSAGE_ID = UUID("11111111-1111-4111-8111-111111111111")
SOURCE_ID = UUID("12111111-1111-4111-8111-111111111111")
POLICY_ID = UUID("22222222-2222-4222-8222-222222222222")
POLICY_PARENT_ID = UUID("23222222-2222-4222-8222-222222222222")
RULE_ID = UUID("33333333-3333-4333-8333-333333333333")
ACTION_ID = UUID("44444444-4444-4444-8444-444444444444")
DESTINATION_ID = UUID("55555555-5555-4555-8555-555555555555")
IDENTITY_ID = UUID("66666666-6666-4666-8666-666666666666")
TEMPLATE_VERSION_ID = UUID("67666666-6666-4666-8666-666666666666")
TEMPLATE_ID = UUID("68666666-6666-4666-8666-666666666666")
EVALUATION_ID = UUID("77777777-7777-4777-8777-777777777777")
INTENT_ID = UUID("88888888-8888-4888-8888-888888888888")
ANALYSIS_RUN_ID = UUID("99999999-9999-4999-8999-999999999999")
LABEL_VERSION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeExecuteResult:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def one_or_none(self) -> object | None:
        return self.value


class FakeSession:
    def __init__(
        self,
        *,
        scalar_results: list[object | None] | None = None,
        scalar_batches: list[tuple[object, ...]] | None = None,
        execute_results: list[object | None] | None = None,
    ) -> None:
        self.scalar_results = list(scalar_results or [])
        self.scalar_batches = list(scalar_batches or [])
        self.execute_results = list(execute_results or [])
        self.scalar_statements: list[object] = []
        self.scalars_statements: list[object] = []
        self.execute_statements: list[object] = []
        self.added: list[object] = []

    def begin(self) -> FakeTransaction:
        return FakeTransaction()

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

    async def execute(self, statement: object) -> FakeExecuteResult:
        self.execute_statements.append(statement)
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return FakeExecuteResult(self.execute_results.pop(0))

    def add(self, value: object) -> None:
        self.added.append(value)


class FakeDatabase:
    def __init__(self, session: FakeSession) -> None:
        self.fake_session = session
        self.session_count = 0
        self.isolation_levels: list[str | None] = []

    @asynccontextmanager
    async def session(self, *, isolation_level: str | None = None):
        self.session_count += 1
        self.isolation_levels.append(isolation_level)
        yield self.fake_session


def compiled_sql(statement: object, *, literal_binds: bool = False) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": literal_binds},
        )
    )


def outcome() -> RuleOutcome:
    return RuleOutcome(
        rule_id=str(RULE_ID),
        enabled=True,
        checked=True,
        evaluated=True,
        matched=True,
        stopped_after_match=False,
    )


def action() -> PublicationAction:
    return PublicationAction(
        action_id=str(ACTION_ID),
        destination_channel_id=str(DESTINATION_ID),
        publication_mode="metadata_only",
        rendering_template_version_id=str(TEMPLATE_VERSION_ID),
        publish_identity_id=str(IDENTITY_ID),
        routing_rule_id=str(RULE_ID),
    )


def evaluation_draft() -> RoutingEvaluationDraft:
    rule_outcome = outcome()
    return RoutingEvaluationDraft(
        evaluation_id=str(EVALUATION_ID),
        routing_request_id="route-1",
        message_id=str(MESSAGE_ID),
        routing_policy_version_id=str(POLICY_ID),
        facts_snapshot={"message": {"id": str(MESSAGE_ID)}},
        facts_hash="b" * 64,
        rule_outcomes=(asdict(rule_outcome),),
        stopped_at_rule_id=None,
    )


def decision() -> RoutingDecision:
    return RoutingDecision(
        policy_version_id=str(POLICY_ID),
        outcomes=(outcome(),),
        actions=(action(),),
    )


def intent_draft() -> PublicationIntentDraft:
    return PublicationIntentDraft(
        intent_id=str(INTENT_ID),
        routing_evaluation_id=str(EVALUATION_ID),
        routing_request_id="route-1",
        message_id=str(MESSAGE_ID),
        routing_policy_version_id=str(POLICY_ID),
        routing_rule_id=str(RULE_ID),
        routing_action_id=str(ACTION_ID),
        destination_channel_id=str(DESTINATION_ID),
        publish_identity_id=str(IDENTITY_ID),
        publication_mode="metadata_only",
        rendering_template_version_id=str(TEMPLATE_VERSION_ID),
        business_idempotency_key="publication:v2:" + "c" * 64,
    )


def evaluation_record(
    *,
    message_id: UUID = MESSAGE_ID,
    policy_id: UUID = POLICY_ID,
) -> RoutingEvaluationRecord:
    draft = evaluation_draft()
    return RoutingEvaluationRecord(
        id=EVALUATION_ID,
        routing_request_id=draft.routing_request_id,
        message_id=message_id,
        routing_policy_version_id=policy_id,
        facts_snapshot=dict(draft.facts_snapshot),
        facts_hash=draft.facts_hash,
        rule_outcomes=[dict(value) for value in draft.rule_outcomes],
        stopped_at_rule_id=None,
    )


def intent_record() -> PublicationIntentRecord:
    draft = intent_draft()
    return PublicationIntentRecord(
        id=INTENT_ID,
        routing_evaluation_id=EVALUATION_ID,
        routing_request_id=draft.routing_request_id,
        message_id=MESSAGE_ID,
        routing_policy_version_id=POLICY_ID,
        routing_rule_id=RULE_ID,
        routing_action_id=ACTION_ID,
        destination_channel_id=DESTINATION_ID,
        publish_identity_id=IDENTITY_ID,
        publication_mode=draft.publication_mode,
        rendering_template_version_id=TEMPLATE_VERSION_ID,
        business_idempotency_key=draft.business_idempotency_key,
        status="pending",
    )


def action_record() -> RoutingActionRecord:
    return RoutingActionRecord(
        id=ACTION_ID,
        routing_rule_id=RULE_ID,
        destination_channel_id=DESTINATION_ID,
        publication_mode="metadata_only",
        rendering_template_version_id=TEMPLATE_VERSION_ID,
        publish_identity_id=IDENTITY_ID,
        output_order=0,
    )


class RoutingRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def test_insert_is_postgresql_request_id_upsert_with_returning(self) -> None:
        sql = compiled_sql(routing_evaluation_insert_statement(evaluation_draft()))

        self.assertIn("INSERT INTO routing_evaluations", sql)
        self.assertIn("ON CONFLICT (routing_request_id) DO NOTHING", sql)
        self.assertIn("RETURNING routing_evaluations.id", sql)

    async def test_snapshot_loads_published_policy_current_labels_and_destination_default(
        self,
    ) -> None:
        message = MessageRecord(
            id=MESSAGE_ID,
            source_channel_id=SOURCE_ID,
            telegram_message_ids=[10],
            telegram_grouped_id=None,
            primary_telegram_message_id=10,
            published_at=NOW,
            edited_at=None,
            original_text="hello",
            telegram_metadata={},
            processing_status="processed",
            review_status="needs_attention",
            media_count=0,
            visual_fingerprint=None,
            source_changed_after_processing=False,
            source_deleted=False,
            source_deleted_at=None,
            blocked_from_analysis=False,
        )
        source = SourceChannel(
            id=SOURCE_ID,
            telegram_peer_id=100,
            username="source",
            display_name="Source",
            default_read_identity_id=IDENTITY_ID,
            active_profile_version_id=None,
            enabled=True,
        )
        policy = RoutingPolicyVersionRecord(
            id=POLICY_ID,
            routing_policy_id=POLICY_PARENT_ID,
            version_number=1,
            state="published",
            description="",
            published_at=NOW,
            archived_at=None,
        )
        rule = RoutingRuleRecord(
            id=RULE_ID,
            routing_policy_version_id=POLICY_ID,
            priority=10,
            enabled=True,
            condition={"fact": "message.id", "op": "exists"},
            stop_on_match=False,
        )
        route_action = action_record()
        destination = DestinationChannel(
            id=DESTINATION_ID,
            name="Destination",
            telegram_peer_id=200,
            username="destination",
            default_publish_identity_id=IDENTITY_ID,
            enabled=True,
        )
        identity = TelegramIdentity(
            id=IDENTITY_ID,
            name="publisher",
            identity_type="bot",
            enabled=True,
            health_status="healthy",
            secret_id=UUID("abababab-abab-4bab-8bab-abababababab"),
        )
        template = RenderingTemplateVersionRecord(
            id=TEMPLATE_VERSION_ID,
            rendering_template_id=TEMPLATE_ID,
            version_number=1,
            state="published",
            template_text="{{ message.original_text }}",
            content_hash="b" * 64,
            published_at=NOW,
            archived_at=None,
        )
        run = AnalysisRunRecord(
            id=ANALYSIS_RUN_ID,
            message_id=MESSAGE_ID,
            analysis_pipeline_version_id=UUID("acacacac-acac-4cac-8cac-acacacacacac"),
            run_mode="formal",
            status="completed",
            facts_snapshot={},
            created_at=NOW,
        )
        assignment = ModelLabelAssignmentRecord(
            id=UUID("adadadad-adad-4dad-8dad-adadadadadad"),
            stage_run_id=UUID("aeaeaeae-aeae-4eae-8eae-aeaeaeaeaeae"),
            analysis_run_id=ANALYSIS_RUN_ID,
            message_id=MESSAGE_ID,
            target_scope="global",
            target_kind="message",
            target_id=MESSAGE_ID,
            image_asset_id=None,
            video_asset_id=None,
            label_definition_version_id=LABEL_VERSION_ID,
            label_key="keep",
            score=0.9,
            activated=True,
            negative=False,
            reason=None,
            evidence=[],
            created_at=NOW,
        )
        manual = ManualLabelAssignmentRecord(
            id=UUID("afafafaf-afaf-4faf-8faf-afafafafafaf"),
            message_id=MESSAGE_ID,
            target_scope="global",
            target_kind="message",
            target_id=MESSAGE_ID,
            image_asset_id=None,
            video_asset_id=None,
            label_definition_version_id=LABEL_VERSION_ID,
            label_key="keep",
            operation="clear",
            score=None,
            activated=None,
            actor_admin_user_id=UUID("bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc"),
            reason="use model",
            created_at=NOW,
        )
        session = FakeSession(
            execute_results=[(message, source)],
            scalar_results=[policy, run],
            scalar_batches=[
                (rule,),
                (route_action,),
                (destination,),
                (identity,),
                (template,),
                (assignment,),
                (manual,),
                (),
                (),
            ],
        )
        database = FakeDatabase(session)
        repository = SqlAlchemyRoutingRepository(database)  # type: ignore[arg-type]

        snapshot = await repository.load_snapshot(
            message_id=str(MESSAGE_ID),
            routing_policy_version_id=str(POLICY_ID),
        )

        self.assertEqual(database.isolation_levels, ["REPEATABLE READ"])
        self.assertEqual(snapshot.review_status.value, "needs_attention")
        self.assertEqual(snapshot.model_assignments[0].label_key, "keep")
        self.assertEqual(snapshot.manual_events[0].operation.value, "clear")
        self.assertEqual(
            snapshot.destination_publish_identity_ids[str(DESTINATION_ID)],
            str(IDENTITY_ID),
        )
        self.assertEqual(snapshot.policy.rules[0].actions[0].routing_rule_id, str(RULE_ID))
        self.assertEqual(
            snapshot.policy.rules[0].actions[0].rendering_template_version_id,
            str(TEMPLATE_VERSION_ID),
        )
        analysis_sql = compiled_sql(session.scalar_statements[1], literal_binds=True)
        self.assertIn("analysis_runs.run_mode IN ('formal', 'reanalysis')", analysis_sql)
        self.assertIn("'completed', 'blocked_negative_gate'", analysis_sql)

    async def test_persist_is_atomic_for_evaluation_and_pending_intents(self) -> None:
        session = FakeSession(scalar_results=[EVALUATION_ID])
        database = FakeDatabase(session)
        repository = SqlAlchemyRoutingRepository(database)  # type: ignore[arg-type]

        result = await repository.persist_evaluation(
            evaluation=evaluation_draft(),
            decision=decision(),
            intents=(intent_draft(),),
        )

        self.assertTrue(result.persisted)
        self.assertFalse(result.reused)
        self.assertEqual(len(session.added), 1)
        self.assertIsInstance(session.added[0], PublicationIntentRecord)
        assert isinstance(session.added[0], PublicationIntentRecord)
        self.assertEqual(session.added[0].status, "pending")
        self.assertEqual(database.session_count, 1)

    async def test_conflicting_request_reloads_and_reuses_existing_evaluation(self) -> None:
        session = FakeSession(
            scalar_results=[None, evaluation_record()],
            scalar_batches=[(intent_record(),), (action_record(),)],
        )
        repository = SqlAlchemyRoutingRepository(FakeDatabase(session))  # type: ignore[arg-type]

        result = await repository.persist_evaluation(
            evaluation=evaluation_draft(),
            decision=decision(),
            intents=(intent_draft(),),
        )

        self.assertTrue(result.reused)
        self.assertEqual(result.evaluation_id, str(EVALUATION_ID))
        self.assertEqual(result.intents[0].intent_id, str(INTENT_ID))
        self.assertEqual(session.added, [])

    async def test_conflicting_request_rejects_a_different_message_or_policy(self) -> None:
        conflicting_records = (
            evaluation_record(message_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")),
            evaluation_record(policy_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")),
        )
        for existing in conflicting_records:
            with self.subTest(existing=existing):
                session = FakeSession(
                    scalar_results=[None, existing],
                    scalar_batches=[(intent_record(),), (action_record(),)],
                )
                repository = SqlAlchemyRoutingRepository(  # type: ignore[arg-type]
                    FakeDatabase(session)
                )

                with self.assertRaisesRegex(DomainValidationError, "already bound"):
                    await repository.persist_evaluation(
                        evaluation=evaluation_draft(),
                        decision=decision(),
                        intents=(intent_draft(),),
                    )
                self.assertEqual(session.added, [])

    async def test_missing_action_and_non_boolean_stored_outcome_are_rejected(self) -> None:
        bad_outcome = asdict(outcome())
        bad_outcome["matched"] = "false"
        with self.assertRaisesRegex(DomainValidationError, "stored routing outcome"):
            _rule_outcome_from_snapshot(bad_outcome)

        session = FakeSession(
            scalar_results=[evaluation_record()],
            scalar_batches=[(intent_record(),), ()],
        )
        with self.assertRaisesRegex(DomainValidationError, "missing routing action"):
            await _load_execution_by_request(session=session, routing_request_id="route-1")  # type: ignore[arg-type]

    async def test_mismatched_intent_is_rejected_before_opening_transaction(self) -> None:
        bad = replace(
            intent_draft(),
            destination_channel_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        )
        session = FakeSession()
        database = FakeDatabase(session)
        repository = SqlAlchemyRoutingRepository(database)  # type: ignore[arg-type]

        with self.assertRaisesRegex(DomainValidationError, "does not match routing action"):
            await repository.persist_evaluation(
                evaluation=evaluation_draft(),
                decision=decision(),
                intents=(bad,),
            )
        self.assertEqual(database.session_count, 0)


if __name__ == "__main__":
    unittest.main()
