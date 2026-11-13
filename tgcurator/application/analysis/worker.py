from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tgcurator.application.ports.analysis import (
    AnalysisCommitResult,
    AnalysisStageRunRepository,
    AnalysisStageSnapshot,
    ClaimedAnalysisStageRun,
)
from tgcurator.domain.analysis import AnalysisErrorCode
from tgcurator.shared import DomainValidationError

from .input_composer import AnalysisInputError, AnalysisTargetInput
from .orchestrator import AnalysisExecutionFailure, AnalysisOrchestrator, PreparedAnalysisCall

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

        loaded_snapshot: AnalysisStageSnapshot | None = None
        probed_cache_key = None
        try:
            loaded_snapshot = await self.repository.load_stage_snapshot(claim=claim)
            snapshot = loaded_snapshot
            targets = _analysis_inputs(snapshot)
            probe = self.orchestrator.prepare_cache_probe(
                stage=snapshot.stage,
                targets=targets,
                source_channel_context=snapshot.source_channel_context,
                attempt=claim.attempt_count,
            )
            if probe is not None and probe.cache_key is not None:
                probed_cache_key = probe.cache_key
                hit = await self.repository.find_cache_hit(
                    snapshot=snapshot, cache_key=probe.cache_key
                )
                if hit is not None:
                    committed = await self.repository.commit_cache_hit(
                        snapshot=snapshot,
                        manifest=probe.request.input_manifest,
                        cache_key=probe.cache_key,
                        hit=hit,
                        now=now,
                    )
                    if committed.committed_target_ids:
                        await self._finish(snapshot=snapshot, committed=committed, now=now)
                        return True
            if probe is not None and not snapshot.stage.input_policy.media:
                prepared = probe
            else:
                prepared = await self.orchestrator.prepare_async(
                    stage=snapshot.stage,
                    targets=targets,
                    source_channel_context=snapshot.source_channel_context,
                    attempt=claim.attempt_count,
                )
        except AnalysisInputError as error:
            failure = AnalysisExecutionFailure(error.code, error.error_type, error.retryable)
            isolated = await self._isolate_batch_prepare_failure(
                claim=claim,
                loaded_snapshot=loaded_snapshot,
                failure=failure,
                now=now,
            )
            if isolated is None:
                return True
            snapshot, targets, prepared = isolated
            probed_cache_key = None
        except DomainValidationError as error:
            failure = AnalysisExecutionFailure(
                AnalysisErrorCode.INPUT_BUILD_ERROR,
                type(error).__name__,
                False,
            )
            isolated = await self._isolate_batch_prepare_failure(
                claim=claim,
                loaded_snapshot=loaded_snapshot,
                failure=failure,
                now=now,
            )
            if isolated is None:
                return True
            snapshot, targets, prepared = isolated
            probed_cache_key = None
        except Exception as error:
            failure = AnalysisExecutionFailure(
                AnalysisErrorCode.INTERNAL_ERROR,
                type(error).__name__,
                True,
            )
            await self._fail_prepare_error(
                claim=claim,
                loaded_snapshot=loaded_snapshot,
                failure=failure,
                now=now,
            )
            return True

        if prepared.cache_key is not None and prepared.cache_key != probed_cache_key:
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
                if committed.committed_target_ids:
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
            await self._complete_snapshot_runs(snapshot=snapshot, now=now)
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
        run_by_message = {
            target.message_id: (target.analysis_run_id or snapshot.claim.analysis_run_id)
            for target in snapshot.targets
        }
        if snapshot.claim.run_mode != "test" and committed.negative_gate_assignments:
            grouped: dict[str, list] = {}
            for assignment in committed.negative_gate_assignments:
                run_id = run_by_message.get(assignment.message_id, snapshot.claim.analysis_run_id)
                grouped.setdefault(run_id, []).append(assignment)
            for run_id, run_assignments in grouped.items():
                await self.repository.apply_negative_gate(
                    analysis_run_id=run_id,
                    assignments=tuple(run_assignments),
                    now=now,
                )
        await self._complete_snapshot_runs(snapshot=snapshot, now=now)

    async def _complete_snapshot_runs(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        now: datetime,
    ) -> None:
        seen_runs: list[str] = []
        for target in snapshot.targets:
            run_id = target.analysis_run_id or snapshot.claim.analysis_run_id
            if run_id not in seen_runs:
                seen_runs.append(run_id)
        if not seen_runs:
            seen_runs.append(snapshot.claim.analysis_run_id)
        for run_id in seen_runs:
            await self.repository.complete_analysis_run(
                analysis_run_id=run_id,
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

    async def _isolate_batch_prepare_failure(
        self,
        *,
        claim: ClaimedAnalysisStageRun,
        loaded_snapshot: AnalysisStageSnapshot | None,
        failure: AnalysisExecutionFailure,
        now: datetime,
    ) -> tuple[AnalysisStageSnapshot, tuple[AnalysisTargetInput, ...], PreparedAnalysisCall] | None:
        if loaded_snapshot is None or len(loaded_snapshot.targets) <= 1:
            await self._fail_prepare_error(
                claim=claim,
                loaded_snapshot=loaded_snapshot,
                failure=failure,
                now=now,
            )
            return None

        healthy_snaps = []
        failed_run_ids: list[str] = []
        for target_snap in loaded_snapshot.targets:
            single_input = (_single_analysis_input(target_snap),)
            try:
                await self.orchestrator.prepare_async(
                    stage=loaded_snapshot.stage,
                    targets=single_input,
                    source_channel_context=loaded_snapshot.source_channel_context,
                    attempt=claim.attempt_count,
                )
            except AnalysisInputError as err:
                target_failure = AnalysisExecutionFailure(err.code, err.error_type, err.retryable)
            except DomainValidationError as err:
                target_failure = AnalysisExecutionFailure(
                    AnalysisErrorCode.INPUT_BUILD_ERROR,
                    type(err).__name__,
                    False,
                )
            except Exception as err:
                target_failure = AnalysisExecutionFailure(
                    AnalysisErrorCode.INTERNAL_ERROR,
                    type(err).__name__,
                    True,
                )
            else:
                healthy_snaps.append(target_snap)
                continue

            await self.repository.retry_or_fail_targets(
                snapshot=loaded_snapshot,
                target_ids=(target_snap.target_id,),
                code=target_failure.code,
                error_type=target_failure.error_type,
                retryable=target_failure.retryable,
                retry_at=_retry_at(snapshot=loaded_snapshot, now=now),
                now=now,
            )
            run_id = target_snap.analysis_run_id or loaded_snapshot.claim.analysis_run_id
            if run_id not in failed_run_ids:
                failed_run_ids.append(run_id)

        if not healthy_snaps:
            await self._complete_snapshot_runs(snapshot=loaded_snapshot, now=now)
            return None

        if len(healthy_snaps) == len(loaded_snapshot.targets):
            # Combined batch exceeded a cross-target budget; run the primary target solo
            # and reschedule co-leased siblings immediately.
            overflow = healthy_snaps[1:]
            healthy_snaps = [healthy_snaps[0]]
            if overflow:
                await self.repository.retry_or_fail_targets(
                    snapshot=loaded_snapshot,
                    target_ids=tuple(item.target_id for item in overflow),
                    code=failure.code,
                    error_type=failure.error_type,
                    retryable=True,
                    retry_at=now,
                    now=now,
                )
                for item in overflow:
                    run_id = item.analysis_run_id or loaded_snapshot.claim.analysis_run_id
                    if run_id not in failed_run_ids:
                        failed_run_ids.append(run_id)

        narrowed_snapshot = AnalysisStageSnapshot(
            claim=loaded_snapshot.claim,
            stage=loaded_snapshot.stage,
            targets=tuple(healthy_snaps),
            source_channel_context=loaded_snapshot.source_channel_context,
        )
        healthy_run_ids = {
            item.analysis_run_id or loaded_snapshot.claim.analysis_run_id for item in healthy_snaps
        }
        for run_id in failed_run_ids:
            if run_id not in healthy_run_ids:
                await self.repository.complete_analysis_run(analysis_run_id=run_id, now=now)

        narrowed_targets = _analysis_inputs(narrowed_snapshot)
        try:
            narrowed_prepared = await self.orchestrator.prepare_async(
                stage=narrowed_snapshot.stage,
                targets=narrowed_targets,
                source_channel_context=narrowed_snapshot.source_channel_context,
                attempt=claim.attempt_count,
            )
        except Exception as err:
            fallback = (
                AnalysisExecutionFailure(err.code, err.error_type, err.retryable)
                if isinstance(err, AnalysisInputError)
                else AnalysisExecutionFailure(
                    AnalysisErrorCode.INTERNAL_ERROR,
                    type(err).__name__,
                    True,
                )
            )
            await self._fail_targets(snapshot=narrowed_snapshot, failure=fallback, now=now)
            await self._complete_snapshot_runs(snapshot=narrowed_snapshot, now=now)
            return None
        return narrowed_snapshot, narrowed_targets, narrowed_prepared

    async def _fail_prepare_error(
        self,
        *,
        claim: ClaimedAnalysisStageRun,
        loaded_snapshot: AnalysisStageSnapshot | None,
        failure: AnalysisExecutionFailure,
        now: datetime,
    ) -> None:
        if loaded_snapshot is not None and len(loaded_snapshot.targets) > 1:
            await self._fail_targets(snapshot=loaded_snapshot, failure=failure, now=now)
            await self._complete_snapshot_runs(snapshot=loaded_snapshot, now=now)
            return
        await self._fail_claim(claim=claim, failure=failure, now=now)
        await self.repository.complete_analysis_run(analysis_run_id=claim.analysis_run_id, now=now)

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


def _single_analysis_input(target: Any) -> AnalysisTargetInput:
    return AnalysisTargetInput(
        target_id=target.target_id,
        message_id=target.message_id,
        asset_id=target.asset_id,
        text=target.text,
        telegram_metadata=target.telegram_metadata,
        media=target.media,
        previous_results=target.previous_results,
        visual_identity=target.visual_identity,
    )


def _analysis_inputs(snapshot: AnalysisStageSnapshot) -> tuple[AnalysisTargetInput, ...]:
    return tuple(_single_analysis_input(target) for target in snapshot.targets)


def _retry_at(*, snapshot: AnalysisStageSnapshot, now: datetime) -> datetime:
    policy = snapshot.stage.retry_policy
    delay = min(
        policy.max_delay_seconds,
        policy.base_delay_seconds * (2 ** max(0, snapshot.claim.attempt_count - 1)),
    )
    return now + timedelta(seconds=delay)
