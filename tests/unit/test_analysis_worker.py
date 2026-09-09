from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from tgcurator.application.analysis import AnalysisOrchestrator, AnalysisStageRunWorker
from tgcurator.application.ports.analysis import (
    AnalysisCacheHit,
    AnalysisCommitResult,
    AnalysisStageSnapshot,
    AnalysisTargetSnapshot,
    ClaimedAnalysisStageRun,
    InferenceRequest,
    InferenceResponse,
    NegativeGateAssignment,
    StartedInferenceCall,
)
from tgcurator.domain.analysis import (
    AnalysisErrorCode,
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    InferenceProfileVersion,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    PromptTemplateVersion,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputPolicy,
    VersionStatus,
    VisualCompositionPolicy,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class FakeProvider:
    def __init__(self, *, payload: dict[str, object], events: list[str]) -> None:
        self.payload = payload
        self.events = events
        self.requests: list[InferenceRequest] = []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.events.append("provider_infer")
        self.requests.append(request)
        return InferenceResponse(
            payload=self.payload,
            provider_adapter="openai_compatible",
            model_name="test-model",
            raw_response={"provider": "fake"},
            latency_ms=12.5,
            http_status=200,
            token_usage={"input_tokens": 10, "output_tokens": 4},
        )


class FakeRepository:
    def __init__(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        events: list[str],
        cache_hit: AnalysisCacheHit | None = None,
        cache_commit: AnalysisCommitResult | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.events = events
        self.cache_hit = cache_hit
        self.cache_commit = cache_commit
        self.start_calls: list[dict[str, Any]] = []
        self.inference_successes: list[dict[str, Any]] = []
        self.inference_failures: list[dict[str, Any]] = []
        self.committed_results: list[AnalysisCommitResult] = []
        self.retry_target_calls: list[dict[str, Any]] = []
        self.retry_claim_calls: list[dict[str, Any]] = []
        self.target_states: dict[str, str] = {}
        self.negative_gate_calls: list[tuple[NegativeGateAssignment, ...]] = []
        self.complete_calls: list[str] = []

    async def claim(
        self,
        *,
        stage_run_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedAnalysisStageRun | None:
        self.events.append("claim")
        if stage_run_id != self.snapshot.claim.stage_run_id:
            return None
        self.start_calls.append({"claim_now": now, "lease_duration": lease_duration})
        return self.snapshot.claim

    async def load_stage_snapshot(self, *, claim: ClaimedAnalysisStageRun) -> AnalysisStageSnapshot:
        self.events.append("load_snapshot")
        if claim != self.snapshot.claim:
            raise AssertionError("unexpected claim")
        return self.snapshot

    async def find_cache_hit(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        cache_key: object,
    ) -> AnalysisCacheHit | None:
        self.events.append("find_cache_hit")
        if snapshot is not self.snapshot:
            raise AssertionError("unexpected snapshot")
        if cache_key is None:
            raise AssertionError("cache lookup requires a cache key")
        return self.cache_hit

    async def commit_cache_hit(self, **kwargs: Any) -> AnalysisCommitResult:
        self.events.append("commit_cache_hit")
        if kwargs["snapshot"] is not self.snapshot:
            raise AssertionError("unexpected snapshot")
        if self.cache_commit is None:
            raise AssertionError("cache commit was not configured")
        self.committed_results.append(self.cache_commit)
        return self.cache_commit

    async def start_inference(self, **kwargs: Any) -> StartedInferenceCall | None:
        self.events.append("start_inference")
        self.start_calls.append(dict(kwargs))
        return StartedInferenceCall("call-1", "manifest-1")

    async def record_inference_success(self, **kwargs: Any) -> bool:
        self.events.append("record_inference_success")
        self.inference_successes.append(dict(kwargs))
        return True

    async def record_inference_failure(self, **kwargs: Any) -> bool:
        self.events.append("record_inference_failure")
        self.inference_failures.append(dict(kwargs))
        return True

    async def commit_validated_targets(self, **kwargs: Any) -> AnalysisCommitResult:
        self.events.append("commit_validated_targets")
        results = kwargs["results"]
        assignments: list[NegativeGateAssignment] = []
        targets = {target.target_id: target for target in self.snapshot.targets}
        for result in results:
            if any(label.activated and label.negative for label in result.labels):
                target = targets[result.target_id]
                assignments.append(
                    NegativeGateAssignment(
                        message_id=target.message_id,
                        stage_run_id=target.stage_run_id,
                        assignment_id=f"assignment-{target.target_id}",
                    )
                )
        committed = AnalysisCommitResult(
            committed_target_ids=tuple(result.target_id for result in results),
            negative_gate_assignments=tuple(assignments),
        )
        self.committed_results.append(committed)
        return committed

    async def retry_or_fail_targets(self, **kwargs: Any) -> bool:
        self.events.append("retry_or_fail_targets")
        call = dict(kwargs)
        self.retry_target_calls.append(call)
        terminal = (
            self.snapshot.claim.attempt_count >= self.snapshot.claim.max_attempts
            or not call["retryable"]
        )
        for target_id in call["target_ids"]:
            self.target_states[target_id] = "failed" if terminal else "retry_wait"
        return True

    async def retry_or_fail_claim(self, **kwargs: Any) -> bool:
        self.events.append("retry_or_fail_claim")
        self.retry_claim_calls.append(dict(kwargs))
        return True

    async def apply_negative_gate(
        self,
        *,
        analysis_run_id: str,
        assignments: tuple[NegativeGateAssignment, ...],
        now: datetime,
    ) -> bool:
        self.events.append("apply_negative_gate")
        if analysis_run_id != self.snapshot.claim.analysis_run_id or now != NOW:
            raise AssertionError("unexpected negative gate context")
        self.negative_gate_calls.append(assignments)
        return True

    async def complete_analysis_run(self, *, analysis_run_id: str, now: datetime) -> bool:
        self.events.append("complete_analysis_run")
        if now != NOW:
            raise AssertionError("unexpected completion time")
        self.complete_calls.append(analysis_run_id)
        return True


class AnalysisStageRunWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_inference_call_is_persisted_before_provider_invocation(self) -> None:
        events: list[str] = []
        snapshot = _snapshot()
        repository = FakeRepository(snapshot=snapshot, events=events)
        provider = FakeProvider(
            payload={"results": [_valid_result("message-a")]},
            events=events,
        )
        worker = AnalysisStageRunWorker(
            repository=repository,  # type: ignore[arg-type]
            orchestrator=AnalysisOrchestrator(provider=provider),
        )

        processed = await worker.process(stage_run_id="stage-a", now=NOW)

        self.assertTrue(processed)
        self.assertLess(events.index("start_inference"), events.index("provider_infer"))
        self.assertEqual(len(provider.requests), 1)
        start = repository.start_calls[-1]
        self.assertEqual(start["request_summary"]["target_ids"], ["message-a"])
        self.assertEqual(len(start["response_schema_hash"]), 64)
        self.assertEqual(repository.committed_results[0].committed_target_ids, ("message-a",))
        self.assertEqual(repository.complete_calls, ["run-1"])

    async def test_missing_provider_persists_bounded_retry_or_terminal_failure(self) -> None:
        for attempt_count, expected_state in ((1, "retry_wait"), (3, "failed")):
            with self.subTest(attempt_count=attempt_count):
                events: list[str] = []
                snapshot = _snapshot(attempt_count=attempt_count, max_attempts=3)
                repository = FakeRepository(snapshot=snapshot, events=events)
                worker = AnalysisStageRunWorker(
                    repository=repository,  # type: ignore[arg-type]
                    orchestrator=AnalysisOrchestrator(provider=None),
                )

                processed = await worker.process(stage_run_id="stage-a", now=NOW)

                self.assertTrue(processed)
                self.assertLess(
                    events.index("start_inference"),
                    events.index("record_inference_failure"),
                )
                failure = repository.inference_failures[0]
                self.assertEqual(failure["code"], AnalysisErrorCode.PROVIDER_ERROR)
                self.assertEqual(failure["error_type"], "InferenceProviderNotConfigured")
                self.assertTrue(failure["retryable"])
                retry = repository.retry_target_calls[0]
                self.assertEqual(retry["target_ids"], ("message-a",))
                self.assertEqual(retry["code"], AnalysisErrorCode.PROVIDER_ERROR)
                expected_delay = 5 * (2 ** (attempt_count - 1))
                self.assertEqual(retry["retry_at"], NOW + timedelta(seconds=expected_delay))
                self.assertEqual(repository.target_states["message-a"], expected_state)
                self.assertEqual(repository.complete_calls, ["run-1"])

    async def test_partial_batch_commits_valid_target_and_retries_bad_targets_independently(
        self,
    ) -> None:
        events: list[str] = []
        snapshot = _snapshot(target_ids=("message-a", "message-b", "message-c"))
        repository = FakeRepository(snapshot=snapshot, events=events)
        provider = FakeProvider(
            payload={
                "results": [
                    _valid_result("message-a"),
                    {
                        "message_id": "message-c",
                        "scores": {"keep": 0.8},
                    },
                ]
            },
            events=events,
        )
        worker = AnalysisStageRunWorker(
            repository=repository,  # type: ignore[arg-type]
            orchestrator=AnalysisOrchestrator(provider=provider),
        )

        processed = await worker.process(stage_run_id="stage-a", now=NOW)

        self.assertTrue(processed)
        self.assertEqual(repository.committed_results[0].committed_target_ids, ("message-a",))
        self.assertEqual(len(repository.retry_target_calls), 2)
        retries = {
            call["target_ids"][0]: (call["code"], call["error_type"])
            for call in repository.retry_target_calls
        }
        self.assertEqual(
            retries,
            {
                "message-b": (
                    AnalysisErrorCode.MISSING_TARGET_RESULT,
                    "missing_target_result",
                ),
                "message-c": (
                    AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
                    "dense_scores_incomplete_or_unknown",
                ),
            },
        )
        self.assertEqual(repository.target_states["message-b"], "retry_wait")
        self.assertEqual(repository.target_states["message-c"], "retry_wait")

    async def test_cache_hit_avoids_provider_io_and_inference_call(self) -> None:
        events: list[str] = []
        snapshot = _snapshot(cache_policy=CachePolicy.MESSAGE)
        cache_commit = AnalysisCommitResult(("message-a",))
        repository = FakeRepository(
            snapshot=snapshot,
            events=events,
            cache_hit=AnalysisCacheHit("source-stage", {"scores": {"keep": 0.9}}),
            cache_commit=cache_commit,
        )
        provider = FakeProvider(payload={}, events=events)
        worker = AnalysisStageRunWorker(
            repository=repository,  # type: ignore[arg-type]
            orchestrator=AnalysisOrchestrator(provider=provider),
        )

        processed = await worker.process(stage_run_id="stage-a", now=NOW)

        self.assertTrue(processed)
        self.assertIn("find_cache_hit", events)
        self.assertIn("commit_cache_hit", events)
        self.assertNotIn("start_inference", events)
        self.assertNotIn("provider_infer", events)
        self.assertEqual(provider.requests, [])
        self.assertEqual(repository.committed_results, [cache_commit])

    async def test_negative_gate_runs_only_for_formal_activated_negative_assignments(
        self,
    ) -> None:
        for run_mode, expected_gate_calls in (("formal", 1), ("test", 0)):
            with self.subTest(run_mode=run_mode):
                events: list[str] = []
                snapshot = _snapshot(run_mode=run_mode)
                repository = FakeRepository(snapshot=snapshot, events=events)
                provider = FakeProvider(
                    payload={
                        "results": [_valid_result("message-a", keep_score=0.1, block_score=0.95)]
                    },
                    events=events,
                )
                worker = AnalysisStageRunWorker(
                    repository=repository,  # type: ignore[arg-type]
                    orchestrator=AnalysisOrchestrator(provider=provider),
                )

                processed = await worker.process(stage_run_id="stage-a", now=NOW)

                self.assertTrue(processed)
                committed = repository.committed_results[0]
                self.assertEqual(len(committed.negative_gate_assignments), 1)
                self.assertEqual(len(repository.negative_gate_calls), expected_gate_calls)
                self.assertEqual(repository.complete_calls, ["run-1"])


def _snapshot(
    *,
    target_ids: tuple[str, ...] = ("message-a",),
    run_mode: str = "formal",
    attempt_count: int = 1,
    max_attempts: int = 3,
    cache_policy: CachePolicy = CachePolicy.NONE,
) -> AnalysisStageSnapshot:
    claim = ClaimedAnalysisStageRun(
        stage_run_id="stage-a",
        analysis_run_id="run-1",
        message_id=target_ids[0],
        target_id=target_ids[0],
        analysis_stage_template_version_id="stage-v1",
        lease_token="lease-a",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        run_mode=run_mode,
    )
    targets = tuple(
        AnalysisTargetSnapshot(
            stage_run_id=f"stage-{chr(ord('a') + index)}",
            lease_token=f"lease-{chr(ord('a') + index)}",
            target_id=target_id,
            message_id=target_id,
            target_kind="message",
            text=f"text for {target_id}",
        )
        for index, target_id in enumerate(target_ids)
    )
    return AnalysisStageSnapshot(
        claim=claim,
        stage=_stage(cache_policy=cache_policy),
        targets=targets,
        source_channel_context={},
    )


def _valid_result(
    target_id: str,
    *,
    keep_score: float = 0.95,
    block_score: float = 0.1,
) -> dict[str, object]:
    return {
        "message_id": target_id,
        "scores": {"keep": keep_score, "block": block_score},
    }


def _stage(*, cache_policy: CachePolicy) -> AnalysisStageTemplateVersion:
    labels = (
        LabelBinding(
            LabelDefinitionVersion(
                version_id="keep-v1",
                definition_id="keep",
                version_number=1,
                key="keep",
                display_name="Keep",
                description="Keep content",
                scope=LabelScope.GLOBAL,
                status=VersionStatus.PUBLISHED,
            ),
            0.7,
            0,
        ),
        LabelBinding(
            LabelDefinitionVersion(
                version_id="block-v1",
                definition_id="block",
                version_number=1,
                key="block",
                display_name="Block",
                description="Block content",
                scope=LabelScope.GLOBAL,
                negative=True,
                status=VersionStatus.PUBLISHED,
            ),
            0.9,
            1,
        ),
    )
    label_set = LabelSetVersion(
        version_id="set-v1",
        label_set_id="set",
        version_number=1,
        bindings=labels,
        status=VersionStatus.PUBLISHED,
    )
    prompt = PromptTemplateVersion(
        version_id="prompt-v1",
        prompt_template_id="prompt",
        version_number=1,
        system_prompt="Labels {{ label_definitions }}",
        user_prompt_template="Messages {{ message_text }}",
        declared_variables=("label_definitions", "message_text"),
        status=VersionStatus.PUBLISHED,
    )
    profile = InferenceProfileVersion(
        version_id="profile-v1",
        inference_profile_id="profile",
        version_number=1,
        provider_adapter="openai_compatible",
        base_url="https://model.example/v1",
        model_name="test-model",
        api_secret_reference="provider-key",
        capabilities=("text",),
        timeout_seconds=30,
        max_concurrency=4,
        max_images=8,
        max_input_tokens=4096,
        structured_output_support=True,
        sampling_parameters={"temperature": 0},
        status=VersionStatus.PUBLISHED,
    )
    return AnalysisStageTemplateVersion(
        version_id="stage-v1",
        stage_template_id="stage",
        version_number=1,
        name="text_analysis",
        target_scope=LabelScope.GLOBAL,
        execution_mode=ExecutionMode.BATCH_MESSAGES,
        prompt=prompt,
        label_set=label_set,
        structured_output_policy=StructuredOutputPolicy(),
        input_policy=StageInputPolicy(text=True),
        visual_composition_policy=VisualCompositionPolicy.ADAPTIVE,
        inference_profile=profile,
        cache_policy=cache_policy,
        timeout_seconds=20,
        retry_policy=StageRetryPolicy(max_attempts=3),
        max_batch_size=8,
        max_concurrency=2,
        status=VersionStatus.PUBLISHED,
    )


if __name__ == "__main__":
    unittest.main()
