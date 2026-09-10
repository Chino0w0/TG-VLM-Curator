from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tgcurator.application.ports.analysis import (
    AnalysisCommitResult,
    AnalysisStageRunRepository,
    AnalysisStageSnapshot,
    ClaimedAnalysisStageRun,
)
from tgcurator.domain.analysis import AnalysisErrorCode
from tgcurator.shared import DomainValidationError

from .input_composer import AnalysisInputError, AnalysisTargetInput
from .orchestrator import AnalysisExecutionFailure, AnalysisOrchestrator

ANALYSIS_QUEUE = "analysis"


@dataclass(slots=True)
class AnalysisStageRunWorker:
    """Durable stage worker that keeps provider I/O outside database transactions."""

    repository: AnalysisStageRunRepository
    orchestrator: AnalysisOrchestrator
    lease_duration: timedelta = timedelta(minutes=5)

    async def process(self, *, stage_run_id: str, now: datetime) -> bool:
        claim = await self.repository.claim(
            stage_run_id=stage_run_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if claim is None:
            return False

        try:
            snapshot = await self.repository.load_stage_snapshot(claim=claim)
            targets = _analysis_inputs(snapshot)
            prepared = self.orchestrator.prepare(
                stage=snapshot.stage,
                targets=targets,
                source_channel_context=snapshot.source_channel_context,
                attempt=claim.attempt_count,
            )
        except AnalysisInputError as error:
            await self._fail_claim(
                claim=claim,
                failure=AnalysisExecutionFailure(error.code, error.error_type, False),
                now=now,
            )
            await self.repository.complete_analysis_run(
                analysis_run_id=claim.analysis_run_id, now=now
            )
            return True
        except DomainValidationError as error:
            await self._fail_claim(
                claim=claim,
                failure=AnalysisExecutionFailure(
                    AnalysisErrorCode.INPUT_BUILD_ERROR,
                    type(error).__name__,
                    False,
                ),
                now=now,
            )
            await self.repository.complete_analysis_run(
                analysis_run_id=claim.analysis_run_id, now=now
            )
            return True
        except Exception as error:
            await self._fail_claim(
                claim=claim,
                failure=AnalysisExecutionFailure(
                    AnalysisErrorCode.INTERNAL_ERROR,
                    type(error).__name__,
                    True,
                ),
                now=now,
            )
            await self.repository.complete_analysis_run(
                analysis_run_id=claim.analysis_run_id, now=now
            )
            return True

        if prepared.cache_key is not None:
            hit = await self.repository.find_cache_hit(
                snapshot=snapshot, cache_key=prepared.cache_key
            )
            if hit is not None:
                committed = await self.repository.commit_cache_hit(
                    snapshot=snapshot,
                    manifest=prepared.request.input_manifest,
                    cache_key=prepared.cache_key,
                    hit=hit,
                    now=now,
                )
                await self._finish(snapshot=snapshot, committed=committed, now=now)
                return True

        started = await self.repository.start_inference(
            snapshot=snapshot,
            manifest=prepared.request.input_manifest,
            response_schema=prepared.schema.document,
            response_schema_hash=prepared.schema.schema_hash,
            cache_key=prepared.cache_key,
            request_summary={
                "target_ids": [target.target_id for target in targets],
                "prompt_version_id": snapshot.stage.prompt.version_id,
                "label_set_version_id": snapshot.stage.label_set.version_id,
                "inference_profile_version_id": snapshot.stage.inference_profile.version_id,
                "input_manifest_hash": prepared.request.input_manifest.input_manifest_hash,
            },
            now=now,
        )
        if started is None:
            return False

        invocation = await self.orchestrator.invoke(prepared)
        if invocation.failure is not None:
            failure = invocation.failure
            await self.repository.record_inference_failure(
                started=started,
                code=failure.code,
                error_type=failure.error_type,
                retryable=failure.retryable,
                raw_response=failure.raw_response,
                latency_ms=failure.latency_ms,
                http_status=failure.http_status,
                token_usage=failure.token_usage,
                now=now,
            )
            await self._fail_targets(snapshot=snapshot, failure=failure, now=now)
            await self.repository.complete_analysis_run(
                analysis_run_id=claim.analysis_run_id, now=now
            )
            return True

        assert invocation.response is not None
        response = invocation.response
        await self.repository.record_inference_success(
            started=started,
            response=response,
            now=now,
        )
        execution = self.orchestrator.validate(
            stage=snapshot.stage,
            targets=targets,
            response=response,
        )
        assert execution.validation is not None
        committed = await self.repository.commit_validated_targets(
            snapshot=snapshot,
            started=started,
            results=execution.validation.valid_results,
            now=now,
        )
        if execution.validation.retry_target_ids:
            fallback_issue = next(
                (issue for issue in execution.validation.issues if issue.target_id is None),
                execution.validation.issues[0],
            )
            for target_id in execution.validation.retry_target_ids:
                issue = next(
                    (
                        issue
                        for issue in execution.validation.issues
                        if issue.target_id == target_id
                    ),
                    fallback_issue,
                )
                await self.repository.retry_or_fail_targets(
                    snapshot=snapshot,
                    target_ids=(target_id,),
                    code=issue.code,
                    error_type=issue.error_type,
                    retryable=True,
                    retry_at=_retry_at(snapshot=snapshot, now=now),
                    now=now,
                )
        await self._finish(snapshot=snapshot, committed=committed, now=now)
        return True

    async def _finish(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        committed: AnalysisCommitResult,
        now: datetime,
    ) -> None:
        if snapshot.claim.run_mode != "test" and committed.negative_gate_assignments:
            await self.repository.apply_negative_gate(
                analysis_run_id=snapshot.claim.analysis_run_id,
                assignments=committed.negative_gate_assignments,
                now=now,
            )
        await self.repository.complete_analysis_run(
            analysis_run_id=snapshot.claim.analysis_run_id,
            now=now,
        )

    async def _fail_targets(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        failure: AnalysisExecutionFailure,
        now: datetime,
    ) -> None:
        await self.repository.retry_or_fail_targets(
            snapshot=snapshot,
            target_ids=tuple(target.target_id for target in snapshot.targets),
            code=failure.code,
            error_type=failure.error_type,
            retryable=failure.retryable,
            retry_at=_retry_at(snapshot=snapshot, now=now),
            now=now,
        )

    async def _fail_claim(
        self,
        *,
        claim: ClaimedAnalysisStageRun,
        failure: AnalysisExecutionFailure,
        now: datetime,
    ) -> None:
        delay = min(300, 5 * (2 ** max(0, claim.attempt_count - 1)))
        await self.repository.retry_or_fail_claim(
            claim=claim,
            code=failure.code,
            error_type=failure.error_type,
            retryable=failure.retryable,
            retry_at=now + timedelta(seconds=delay),
            now=now,
        )


def _analysis_inputs(snapshot: AnalysisStageSnapshot) -> tuple[AnalysisTargetInput, ...]:
    return tuple(
        AnalysisTargetInput(
            target_id=target.target_id,
            message_id=target.message_id,
            asset_id=target.asset_id,
            text=target.text,
            telegram_metadata=target.telegram_metadata,
            media=target.media,
            previous_results=target.previous_results,
            visual_identity=target.visual_identity,
        )
        for target in snapshot.targets
    )


def _retry_at(*, snapshot: AnalysisStageSnapshot, now: datetime) -> datetime:
    policy = snapshot.stage.retry_policy
    delay = min(
        policy.max_delay_seconds,
        policy.base_delay_seconds * (2 ** max(0, snapshot.claim.attempt_count - 1)),
    )
    return now + timedelta(seconds=delay)
