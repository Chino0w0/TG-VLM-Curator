from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.analysis import (
    ClaimedAnalysisStageRun,
    NegativeGateAssignment,
    StartedInferenceCall,
)
from tgcurator.domain.analysis import (
    AnalysisErrorCode,
    CachePolicy,
    LabelScope,
    ResolvedLabel,
    StageCacheKey,
    ValidatedTargetResult,
)
from tgcurator.infrastructure.database.analysis_repository import (
    SqlAlchemyAnalysisStageRunRepository,
    _copy_assignment,
    _exact_cache_source,
    analysis_stage_run_claim_statement,
    sanitize_error_code,
    sanitize_error_type,
    sanitize_identifier,
    sanitize_json,
)
from tgcurator.infrastructure.database.models import (
    AnalysisRunRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    StageRunRecord,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeResult:
    rowcount = 1


class FakeSession:
    def __init__(
        self,
        *,
        scalar_results: list[object | None] | None = None,
        scalar_batches: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.scalar_results = list(scalar_results or [])
        self.scalar_batches = list(scalar_batches or [])
        self.scalar_statements: list[object] = []
        self.scalars_statements: list[object] = []
        self.execute_statements: list[object] = []
        self.added: list[object] = []
        self.flush_count = 0

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

    async def execute(self, statement: object) -> FakeResult:
        self.execute_statements.append(statement)
        return FakeResult()

    async def flush(self) -> None:
        self.flush_count += 1

    def add(self, value: object) -> None:
        self.added.append(value)


class FakeDatabase:
    def __init__(self, session: FakeSession) -> None:
        self.fake_session = session

    @asynccontextmanager
    async def session(self):
        yield self.fake_session


def stage_run(
    *,
    status: str = "pending",
    attempt_count: int = 0,
    max_attempts: int = 3,
    lease_token: UUID | None = None,
    lease_expires_at: datetime | None = None,
) -> StageRunRecord:
    message_id = uuid4()
    return StageRunRecord(
        id=uuid4(),
        analysis_run_id=uuid4(),
        pipeline_stage_node_id=None,
        analysis_stage_template_version_id=uuid4(),
        message_id=message_id,
        target_scope="global",
        target_kind="message",
        target_id=message_id,
        image_asset_id=None,
        video_asset_id=None,
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        cache_policy="none",
    )


def analysis_run(row: StageRunRecord, *, mode: str = "formal") -> AnalysisRunRecord:
    return AnalysisRunRecord(
        id=row.analysis_run_id,
        message_id=row.message_id,
        analysis_pipeline_version_id=uuid4(),
        run_mode=mode,
        status="pending",
        facts_snapshot={},
    )


def claim_for(row: StageRunRecord, *, mode: str = "formal") -> ClaimedAnalysisStageRun:
    assert row.lease_token is not None
    return ClaimedAnalysisStageRun(
        stage_run_id=str(row.id),
        analysis_run_id=str(row.analysis_run_id),
        message_id=str(row.message_id),
        target_id=str(row.target_id),
        analysis_stage_template_version_id=str(row.analysis_stage_template_version_id),
        lease_token=str(row.lease_token),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        run_mode=mode,
    )


def compiled_sql(statement: object) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class AnalysisRepositoryClaimTests(unittest.IsolatedAsyncioTestCase):
    def test_claim_statement_covers_due_and_expired_states_with_skip_locked(self) -> None:
        sql = compiled_sql(analysis_stage_run_claim_statement(stage_run_id=uuid4(), now=NOW))

        self.assertIn("stage_runs.status = 'pending'", sql)
        self.assertIn("stage_runs.status = 'retry_wait'", sql)
        self.assertIn("stage_runs.next_retry_at <=", sql)
        self.assertIn("stage_runs.status = 'running'", sql)
        self.assertIn("stage_runs.lease_expires_at <=", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)

    async def test_pending_claim_creates_lease_and_duplicate_or_ineligible_claim_is_ignored(
        self,
    ) -> None:
        row = stage_run()
        run = analysis_run(row)
        session = FakeSession(scalar_results=[row, run])
        repository = SqlAlchemyAnalysisStageRunRepository(FakeDatabase(session))  # type: ignore[arg-type]

        claim = await repository.claim(
            stage_run_id=str(row.id),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(row.status, "running")
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(row.lease_expires_at, NOW + timedelta(minutes=5))
        self.assertEqual(claim.lease_token, str(row.lease_token))
        self.assertEqual(run.status, "running")

        duplicate_session = FakeSession(scalar_results=[None])
        duplicate_repository = SqlAlchemyAnalysisStageRunRepository(
            FakeDatabase(duplicate_session)  # type: ignore[arg-type]
        )
        self.assertIsNone(
            await duplicate_repository.claim(
                stage_run_id=str(row.id),
                now=NOW,
                lease_duration=timedelta(minutes=5),
            )
        )

    async def test_expired_lease_is_reclaimed_with_a_new_token(self) -> None:
        old_token = uuid4()
        row = stage_run(
            status="running",
            attempt_count=1,
            lease_token=old_token,
            lease_expires_at=NOW - timedelta(seconds=1),
        )
        run = analysis_run(row)
        run.status = "running"
        run.started_at = NOW - timedelta(minutes=1)
        session = FakeSession(scalar_results=[row, run])
        repository = SqlAlchemyAnalysisStageRunRepository(FakeDatabase(session))  # type: ignore[arg-type]

        claim = await repository.claim(
            stage_run_id=str(row.id),
            now=NOW,
            lease_duration=timedelta(minutes=2),
        )

        self.assertIsNotNone(claim)
        self.assertEqual(row.attempt_count, 2)
        self.assertNotEqual(row.lease_token, old_token)
        self.assertEqual(row.lease_expires_at, NOW + timedelta(minutes=2))

    async def test_expired_final_attempt_is_terminalized_without_another_claim(self) -> None:
        row = stage_run(
            status="running",
            attempt_count=3,
            max_attempts=3,
            lease_token=uuid4(),
            lease_expires_at=NOW - timedelta(seconds=1),
        )
        run = analysis_run(row)
        run.status = "running"
        run.started_at = NOW - timedelta(minutes=1)
        session = FakeSession(
            scalar_results=[row, run],
            scalar_batches=[("failed",)],
        )
        repository = SqlAlchemyAnalysisStageRunRepository(FakeDatabase(session))  # type: ignore[arg-type]

        claimed = await repository.claim(
            stage_run_id=str(row.id),
            now=NOW,
            lease_duration=timedelta(minutes=5),
        )

        self.assertIsNone(claimed)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.last_error_code, "LEASE_EXPIRED")
        self.assertEqual(row.last_error_type, "WorkerLeaseExpired")
        self.assertFalse(row.retryable)
        self.assertEqual(run.status, "failed")
        self.assertEqual(session.flush_count, 1)

    async def test_retry_claim_query_validates_live_lease_token_and_expiry(self) -> None:
        row = stage_run(
            status="running",
            attempt_count=1,
            lease_token=uuid4(),
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        claim = claim_for(row)
        session = FakeSession(scalar_results=[None])
        repository = SqlAlchemyAnalysisStageRunRepository(FakeDatabase(session))  # type: ignore[arg-type]

        changed = await repository.retry_or_fail_claim(
            claim=claim,
            code=AnalysisErrorCode.PROVIDER_ERROR,
            error_type="ProviderUnavailable",
            retryable=True,
            retry_at=NOW + timedelta(seconds=30),
            now=NOW,
        )

        self.assertFalse(changed)
        sql = compiled_sql(session.scalar_statements[0])
        self.assertIn("stage_runs.status = 'running'", sql)
        self.assertIn(f"stage_runs.lease_token = '{claim.lease_token}'", sql)
        self.assertIn("stage_runs.lease_expires_at >", sql)

    async def test_retry_is_bounded_and_final_attempt_becomes_terminal(self) -> None:
        row = stage_run(
            status="running",
            attempt_count=3,
            max_attempts=3,
            lease_token=uuid4(),
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        session = FakeSession(scalar_results=[row])
        repository = SqlAlchemyAnalysisStageRunRepository(FakeDatabase(session))  # type: ignore[arg-type]

        changed = await repository.retry_or_fail_claim(
            claim=claim_for(row),
            code=AnalysisErrorCode.PROVIDER_ERROR,
            error_type="ProviderUnavailable",
            retryable=True,
            retry_at=NOW + timedelta(seconds=30),
            now=NOW,
        )

        self.assertTrue(changed)
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.next_retry_at)
        self.assertIsNone(row.lease_token)
        self.assertEqual(row.completed_at, NOW)
        self.assertEqual(row.last_error_code, "PROVIDER_ERROR")


class AnalysisRepositoryInvariantTests(unittest.IsolatedAsyncioTestCase):
    def test_identifiers_errors_and_audit_json_are_bounded_and_redacted(self) -> None:
        with self.assertRaises(DomainValidationError):
            sanitize_identifier("   ", field="provider", max_length=8)
        with self.assertRaises(DomainValidationError):
            sanitize_identifier("too-long-value", field="provider", max_length=8)
        self.assertEqual(len(sanitize_error_code("x" * 100)), 64)
        self.assertEqual(len(sanitize_error_type("y" * 200)), 128)

        sanitized = sanitize_json(
            {
                "api_key": "secret-value",
                "nested": {"Authorization": "Bearer hidden"},
                "url": "https://user:password@example.test/v1",
                "query": "https://example.test/v1?token=hidden",
                "token_usage": {"input_tokens": 5},
            }
        )
        self.assertEqual(sanitized["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["Authorization"], "[REDACTED]")
        self.assertEqual(sanitized["url"], "[REDACTED_URL]")
        self.assertEqual(sanitized["query"], "[REDACTED_URL]")
        self.assertEqual(sanitized["token_usage"], {"input_tokens": 5})
        self.assertNotIn("secret-value", repr(sanitized))

    def test_exact_cache_reuse_requires_all_semantic_fields(self) -> None:
        key = StageCacheKey(CachePolicy.MESSAGE, "message-a", "a" * 64)
        source = stage_run(status="succeeded")
        source.parsed_result = {"target_id": "old"}
        source.semantic_cache_key = key.value
        source.cache_policy = key.policy.value
        source.cache_scope_identity = key.scope_identity

        self.assertTrue(_exact_cache_source(source=source, cache_key=key))
        source.cache_scope_identity = "message-b"
        self.assertFalse(_exact_cache_source(source=source, cache_key=key))
        source.cache_scope_identity = key.scope_identity
        source.status = "failed"
        self.assertFalse(_exact_cache_source(source=source, cache_key=key))

    def test_cached_assignment_copy_uses_current_target_identities(self) -> None:
        source = ModelLabelAssignmentRecord(
            id=uuid4(),
            stage_run_id=uuid4(),
            analysis_run_id=uuid4(),
            message_id=uuid4(),
            target_scope="media",
            target_kind="image_asset",
            target_id=uuid4(),
            image_asset_id=uuid4(),
            video_asset_id=None,
            label_definition_version_id=uuid4(),
            label_key="keep",
            score=0.91,
            activated=True,
            negative=False,
            reason="source reason",
            evidence=["source evidence"],
        )
        target = stage_run()
        target.target_scope = "media"
        target.target_kind = "video_asset"
        target.target_id = uuid4()
        target.image_asset_id = None
        target.video_asset_id = target.target_id

        copied = _copy_assignment(source=source, stage_run=target, now=NOW)

        self.assertEqual(copied.stage_run_id, target.id)
        self.assertEqual(copied.analysis_run_id, target.analysis_run_id)
        self.assertEqual(copied.message_id, target.message_id)
        self.assertEqual(copied.target_id, target.target_id)
        self.assertEqual(copied.video_asset_id, target.video_asset_id)
        self.assertIsNone(copied.image_asset_id)
        self.assertEqual(copied.label_definition_version_id, source.label_definition_version_id)
        self.assertEqual(copied.evidence, ["source evidence"])
        self.assertIsNot(copied.evidence, source.evidence)

    async def test_test_run_commits_result_without_formal_assignments(self) -> None:
        row = stage_run(
            status="running",
            attempt_count=1,
            lease_token=uuid4(),
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        inference_call_id = uuid4()
        row.inference_call_id = inference_call_id
        claim = claim_for(row, mode="test")
        target = SimpleNamespace(
            stage_run_id=str(row.id),
            lease_token=str(row.lease_token),
            target_id=str(row.target_id),
        )
        snapshot = SimpleNamespace(claim=claim, targets=(target,))
        result = ValidatedTargetResult(
            target_id=str(row.target_id),
            labels=(
                ResolvedLabel(
                    label_definition_version_id=str(uuid4()),
                    key="keep",
                    scope=LabelScope.GLOBAL,
                    score=0.95,
                    activated=True,
                    negative=False,
                ),
            ),
        )
        session = FakeSession(scalar_batches=[(row,)])
        repository = SqlAlchemyAnalysisStageRunRepository(FakeDatabase(session))  # type: ignore[arg-type]

        committed = await repository.commit_validated_targets(
            snapshot=snapshot,  # type: ignore[arg-type]
            started=StartedInferenceCall(str(inference_call_id), str(uuid4())),
            results=(result,),
            now=NOW,
        )

        self.assertEqual(committed.committed_target_ids, (str(row.target_id),))
        self.assertEqual(committed.negative_gate_assignments, ())
        self.assertEqual(session.added, [])
        self.assertEqual(row.status, "succeeded")

    async def test_negative_gate_preserves_the_first_recorded_message_cause(self) -> None:
        row = stage_run()
        run = analysis_run(row)
        run.status = "running"
        run.started_at = NOW - timedelta(minutes=1)
        requested_assignment = ModelLabelAssignmentRecord(
            id=uuid4(),
            stage_run_id=row.id,
            analysis_run_id=run.id,
            message_id=row.message_id,
            target_scope="global",
            target_kind="message",
            target_id=row.message_id,
            image_asset_id=None,
            video_asset_id=None,
            label_definition_version_id=uuid4(),
            label_key="block",
            score=0.99,
            activated=True,
            negative=True,
            evidence=[],
        )
        original_stage_id = uuid4()
        original_assignment_id = uuid4()
        message = MessageRecord(
            id=row.message_id,
            source_channel_id=uuid4(),
            telegram_message_ids=[1],
            primary_telegram_message_id=1,
            published_at=NOW - timedelta(days=1),
            telegram_metadata={},
            processing_status="processing",
            media_count=0,
            blocked_from_analysis=True,
            blocked_at=NOW - timedelta(hours=1),
            blocked_by_stage_run_id=original_stage_id,
            blocked_by_label_assignment_id=original_assignment_id,
        )
        remaining = stage_run(status="pending")
        remaining.analysis_run_id = run.id
        remaining.message_id = row.message_id
        session = FakeSession(
            scalar_results=[run],
            scalar_batches=[(requested_assignment,), (message,), (remaining,)],
        )
        repository = SqlAlchemyAnalysisStageRunRepository(FakeDatabase(session))  # type: ignore[arg-type]
        assignment = NegativeGateAssignment(
            message_id=str(row.message_id),
            stage_run_id=str(row.id),
            assignment_id=str(requested_assignment.id),
        )

        applied = await repository.apply_negative_gate(
            analysis_run_id=str(run.id),
            assignments=(assignment,),
            now=NOW,
        )

        self.assertTrue(applied)
        self.assertEqual(message.blocked_by_stage_run_id, original_stage_id)
        self.assertEqual(message.blocked_by_label_assignment_id, original_assignment_id)
        self.assertEqual(message.blocked_at, NOW - timedelta(hours=1))
        self.assertEqual(remaining.status, "skipped_negative_gate")
        self.assertEqual(run.status, "blocked_negative_gate")

    async def test_analysis_run_aggregation_uses_active_failure_blocked_success_precedence(
        self,
    ) -> None:
        cases = (
            (("failed", "retry_wait"), "running", None),
            (("succeeded", "failed"), "failed", "PROVIDER_ERROR"),
            (("succeeded", "skipped_negative_gate"), "blocked_negative_gate", None),
            (("succeeded",), "completed", None),
        )
        for statuses, expected_status, expected_error in cases:
            with self.subTest(statuses=statuses):
                seed = stage_run()
                run = analysis_run(seed)
                stages = []
                for status in statuses:
                    stages.append(
                        SimpleNamespace(
                            id=uuid4(),
                            status=status,
                            completed_at=NOW
                            if status not in {"pending", "running", "retry_wait"}
                            else None,
                            last_error_code="PROVIDER_ERROR" if status == "failed" else None,
                            last_error_type="ProviderUnavailable" if status == "failed" else None,
                        )
                    )
                session = FakeSession(
                    scalar_results=[run],
                    scalar_batches=[tuple(stages)],
                )
                repository = SqlAlchemyAnalysisStageRunRepository(
                    FakeDatabase(session)  # type: ignore[arg-type]
                )

                completed = await repository.complete_analysis_run(
                    analysis_run_id=str(run.id),
                    now=NOW,
                )

                self.assertTrue(completed)
                self.assertEqual(run.status, expected_status)
                self.assertEqual(run.last_error_code, expected_error)
                if expected_status == "running":
                    self.assertIsNone(run.completed_at)
                else:
                    self.assertEqual(run.completed_at, NOW)


if __name__ == "__main__":
    unittest.main()
