from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from tgcurator.application.ports.pipeline import (
    ActiveRangeExecutionSnapshot,
    MessagePipelineRepository,
    PipelineProgressionReport,
    WindowMessageSnapshot,
)
from tgcurator.application.ports.routing import RoutingRequest
from tgcurator.application.routing import RoutingService
from tgcurator.domain.analysis import (
    AnalysisRunStatus,
    PipelineNodeActionKind,
    PipelineNodeSpec,
    pipeline_has_prescreen_root,
    plan_pipeline_progression,
)
from tgcurator.domain.messages import (
    DuplicateStatus,
    compute_archived_visual_identity,
    evaluate_visual_duplicate,
)
from tgcurator.domain.processing import (
    VALID_WATERMARK_TERMINAL_REASONS,
    MessageFailureStage,
    MessageTerminalReason,
    WindowMessageProgress,
    compute_continuous_watermark,
)
from tgcurator.shared import ensure_aware

_ACTIVE_INTENT_STATUSES = frozenset({"pending", "leased", "sending", "partial", "retry_wait"})


@dataclass(slots=True)
class MessagePipelineCoordinator:
    """Drive RangeExecution messages through PreScreen, Archive, Dedup, DAG, Routing, Watermark."""

    repository: MessagePipelineRepository
    routing_service: RoutingService | None = None

    async def handoff_and_drive_execution(
        self,
        *,
        execution_id: str,
        lease_token: str,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        handed_off = await self.repository.handoff_execution(
            execution_id=execution_id,
            lease_token=lease_token,
            now=now,
        )
        if not handed_off:
            return False
        await self.drive_execution(execution_id=execution_id, now=now)
        return True

    async def drive_active_executions(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> PipelineProgressionReport:
        ensure_aware(now, field="now")
        execution_ids = await self.repository.list_awaiting_execution_ids(limit=limit)
        aggregate = PipelineProgressionReport()
        for execution_id in execution_ids:
            report = await self.drive_execution(execution_id=execution_id, now=now)
            aggregate = aggregate.merged(report)
        return aggregate

    async def on_message_event(
        self,
        *,
        message_id: str,
        now: datetime,
    ) -> PipelineProgressionReport:
        ensure_aware(now, field="now")
        execution_ids = await self.repository.find_awaiting_execution_ids_for_message(
            message_id=message_id
        )
        aggregate = PipelineProgressionReport()
        for execution_id in execution_ids:
            report = await self.drive_execution(execution_id=execution_id, now=now)
            aggregate = aggregate.merged(report)
        return aggregate

    async def on_image_asset_event(
        self, *, image_asset_id: str, now: datetime
    ) -> PipelineProgressionReport:
        message_id = await self.repository.find_message_id_for_image_asset(
            image_asset_id=image_asset_id
        )
        if message_id is None:
            return PipelineProgressionReport()
        return await self.on_message_event(message_id=message_id, now=now)

    async def on_video_asset_event(
        self, *, video_asset_id: str, now: datetime
    ) -> PipelineProgressionReport:
        message_id = await self.repository.find_message_id_for_video_asset(
            video_asset_id=video_asset_id
        )
        if message_id is None:
            return PipelineProgressionReport()
        return await self.on_message_event(message_id=message_id, now=now)

    async def on_stage_run_event(
        self, *, stage_run_id: str, now: datetime
    ) -> PipelineProgressionReport:
        message_ids = await self.repository.find_message_ids_for_stage_run(
            stage_run_id=stage_run_id
        )
        aggregate = PipelineProgressionReport()
        seen_executions: set[str] = set()
        for message_id in message_ids:
            for execution_id in await self.repository.find_awaiting_execution_ids_for_message(
                message_id=message_id
            ):
                if execution_id in seen_executions:
                    continue
                seen_executions.add(execution_id)
                report = await self.drive_execution(execution_id=execution_id, now=now)
                aggregate = aggregate.merged(report)
        return aggregate

    async def on_publication_intent_event(
        self, *, intent_id: str, now: datetime
    ) -> PipelineProgressionReport:
        message_id = await self.repository.find_message_id_for_publication_intent(
            intent_id=intent_id
        )
        if message_id is None:
            return PipelineProgressionReport()
        return await self.on_message_event(message_id=message_id, now=now)

    async def drive_execution(
        self,
        *,
        execution_id: str,
        now: datetime,
    ) -> PipelineProgressionReport:
        ensure_aware(now, field="now")
        execution = await self.repository.load_execution(execution_id=execution_id)
        if execution is None or execution.status != "awaiting_pipeline":
            return PipelineProgressionReport()

        pipeline_nodes: tuple[PipelineNodeSpec, ...] = ()
        if execution.profile.analysis_pipeline_version_id is not None:
            pipeline_nodes = await self.repository.load_pipeline_nodes(
                pipeline_version_id=execution.profile.analysis_pipeline_version_id
            )

        messages = await self.repository.load_window_messages(execution=execution)
        counters = _ExecutionStepCounters()
        for message in messages:
            await self._advance_message(
                execution=execution,
                message=message,
                pipeline_nodes=pipeline_nodes,
                counters=counters,
                now=now,
            )

        refreshed = await self.repository.load_window_messages(execution=execution)
        progress = tuple(
            _to_window_progress(message=item, execution=execution) for item in refreshed
        )
        advancement = compute_continuous_watermark(
            from_message_id_exclusive=execution.from_message_id_exclusive,
            current_watermark_message_id=execution.watermark_message_id,
            to_message_id_inclusive=execution.to_message_id_inclusive,
            history_ingested=True,
            messages=progress,
        )
        advanced, completed = await self.repository.persist_watermark_advancement(
            execution_id=execution.execution_id,
            next_watermark_message_id=advancement.next_watermark_message_id,
            window_completed=advancement.window_completed,
            progress_snapshot=progress,
            now=now,
        )
        return PipelineProgressionReport(
            executions_checked=1,
            executions_completed=1 if completed else 0,
            watermarks_advanced=1 if advanced else 0,
            messages_terminalized=counters.messages_terminalized,
            stage_runs_scheduled=counters.stage_runs_scheduled,
            stage_runs_cached=counters.stage_runs_cached,
            media_wakeups_created=counters.media_wakeups_created,
            duplicates_detected=counters.duplicates_detected,
            routings_evaluated=counters.routings_evaluated,
        )

    async def _advance_message(
        self,
        *,
        execution: ActiveRangeExecutionSnapshot,
        message: WindowMessageSnapshot,
        pipeline_nodes: tuple[PipelineNodeSpec, ...],
        counters: _ExecutionStepCounters,
        now: datetime,
    ) -> None:
        if (
            message.processing_status == "processed"
            and message.terminal_reason in VALID_WATERMARK_TERMINAL_REASONS
        ):
            # An appended album component is new work even if the parent used to be
            # terminal. Explicit administrator/negative-gate skips remain intentional.
            if message.terminal_reason not in {
                MessageTerminalReason.ADMIN_SKIPPED.value,
                MessageTerminalReason.UNSUPPORTED.value,
                MessageTerminalReason.BLOCKED_NEGATIVE_GATE.value,
            } and (message.media.any_pending_archive or message.media.any_failed):
                await self.repository.mark_message_processing(
                    message_id=message.message_id, now=now
                )
                message = replace(
                    message,
                    processing_status="processing",
                    terminal_reason=None,
                    duplicate_status=DuplicateStatus.UNCHECKED.value,
                    canonical_message_id=None,
                    duplicate_distance=None,
                )
            else:
                if (
                    execution.profile.routing_policy_version_id is not None
                    and (
                        message.formal_routing_evaluation_id is None
                        or any(
                            status in _ACTIVE_INTENT_STATUSES or status == "failed"
                            for status in message.publication_intent_statuses
                        )
                    )
                    and message.terminal_reason
                    not in {
                        MessageTerminalReason.DUPLICATE_SKIPPED.value,
                        MessageTerminalReason.UNSUPPORTED.value,
                        MessageTerminalReason.ADMIN_SKIPPED.value,
                    }
                ):
                    await self._route_and_settle_publication(
                        execution=execution,
                        message=message,
                        terminal_reason=MessageTerminalReason(message.terminal_reason),
                        counters=counters,
                        now=now,
                    )
                return
        if message.processing_status == "failed":
            return

        if message.processing_status == "ingested":
            await self.repository.mark_message_processing(message_id=message.message_id, now=now)
            message = replace(message, processing_status="processing")

        profile = execution.profile
        has_prescreen = (
            profile.analysis_pipeline_version_id is not None
            and profile.prescreen_enabled
            and pipeline_has_prescreen_root(pipeline_nodes)
        )

        # 1. Negative Gate check (e.g. triggered during PreScreen or downstream stage).
        if (
            message.blocked_from_analysis
            or message.formal_analysis_run_status == AnalysisRunStatus.BLOCKED_NEGATIVE_GATE.value
        ):
            await self.repository.cancel_pending_media_wakeups(
                message_id=message.message_id,
                now=now,
            )
            await self._route_and_settle_publication(
                execution=execution,
                message=message,
                terminal_reason=MessageTerminalReason.BLOCKED_NEGATIVE_GATE,
                counters=counters,
                now=now,
            )
            return

        # 2. If PreScreen root stages exist and haven't run yet, run them BEFORE media download.
        if has_prescreen and message.duplicate_status == DuplicateStatus.UNCHECKED.value:
            assert profile.analysis_pipeline_version_id is not None
            run_id = message.formal_analysis_run_id
            if run_id is None:
                run_id = await self.repository.ensure_formal_analysis_run(
                    message_id=message.message_id,
                    pipeline_version_id=profile.analysis_pipeline_version_id,
                    now=now,
                )
            prescreen_settled = await self._run_prescreen_phase(
                analysis_run_id=run_id,
                message=message,
                counters=counters,
                now=now,
            )
            if not prescreen_settled:
                return
            # Reload message check if Negative Gate triggered synchronously via cache hit.
            dag = await self.repository.load_analysis_dag_snapshot(analysis_run_id=run_id)
            if dag is not None and dag.blocked_from_analysis:
                await self.repository.cancel_pending_media_wakeups(
                    message_id=message.message_id,
                    now=now,
                )
                await self._route_and_settle_publication(
                    execution=execution,
                    message=replace(message, blocked_from_analysis=True),
                    terminal_reason=MessageTerminalReason.BLOCKED_NEGATIVE_GATE,
                    counters=counters,
                    now=now,
                )
                return

        # 3. Media Archive Gate (only reached for messages not blocked by PreScreen).
        if message.media.has_media:
            if message.media.any_failed:
                await self.repository.mark_message_failed(
                    message_id=message.message_id,
                    failure_stage=MessageFailureStage.MEDIA_FAILED.value,
                    error_code="MEDIA_ARCHIVE_FAILED",
                    now=now,
                )
                return
            if message.media.any_pending_archive:
                created = await self.repository.ensure_media_archive_wakeups(
                    message_id=message.message_id,
                    now=now,
                )
                counters.media_wakeups_created += created
                return

        # 4. Message-level Visual Deduplication Gate once media archive is settled.
        if message.duplicate_status == DuplicateStatus.UNCHECKED.value:
            fingerprint, visual_tokens = compute_archived_visual_identity(
                image_phashes=message.image_phashes,
                video_cover_phashes=message.video_cover_phashes,
            )
            candidates = (
                await self.repository.load_visual_duplicate_candidates(
                    message_id=message.message_id,
                    published_at=message.published_at,
                    visual_fingerprint=fingerprint,
                )
                if (fingerprint is not None or visual_tokens)
                else ()
            )
            match = evaluate_visual_duplicate(
                message_id=message.message_id,
                visual_fingerprint=fingerprint,
                visual_tokens=visual_tokens,
                candidates=candidates,
                duplicate_policy=profile.duplicate_policy,
                phash_distance_threshold=profile.phash_distance_threshold,
            )
            await self.repository.record_visual_deduplication(
                message_id=message.message_id,
                visual_fingerprint=fingerprint,
                match=match,
                now=now,
            )
            if match.status in {
                DuplicateStatus.DUPLICATE_SKIPPED,
                DuplicateStatus.DUPLICATE_RECLASSIFY,
            }:
                counters.duplicates_detected += 1
            message = replace(
                message,
                visual_fingerprint=fingerprint or message.visual_fingerprint,
                duplicate_status=match.status.value,
                canonical_message_id=match.canonical_message_id,
                duplicate_distance=match.hamming_distance,
            )

        if message.duplicate_status == DuplicateStatus.DUPLICATE_SKIPPED.value:
            if message.formal_analysis_run_id is not None:
                await self.repository.set_analysis_run_status(
                    analysis_run_id=message.formal_analysis_run_id,
                    status=AnalysisRunStatus.COMPLETED.value,
                    now=now,
                )
            await self.repository.mark_message_processed(
                message_id=message.message_id,
                terminal_reason=MessageTerminalReason.DUPLICATE_SKIPPED.value,
                now=now,
            )
            counters.messages_terminalized += 1
            return

        # 5. Full Conditional Analysis DAG Progression.
        terminal_reason = MessageTerminalReason.NO_ELIGIBLE_STAGES
        if profile.analysis_pipeline_version_id is not None and pipeline_nodes:
            run_id = message.formal_analysis_run_id
            if run_id is None:
                run_id = await self.repository.ensure_formal_analysis_run(
                    message_id=message.message_id,
                    pipeline_version_id=profile.analysis_pipeline_version_id,
                    now=now,
                )
            dag_outcome = await self._progress_full_dag(
                analysis_run_id=run_id,
                message=message,
                counters=counters,
                now=now,
            )
            if dag_outcome == "waiting":
                return
            if dag_outcome == "failed":
                return
            if dag_outcome == "blocked_negative_gate":
                terminal_reason = MessageTerminalReason.BLOCKED_NEGATIVE_GATE
            elif dag_outcome == "no_eligible_stages":
                terminal_reason = MessageTerminalReason.NO_ELIGIBLE_STAGES
            else:
                terminal_reason = MessageTerminalReason.COMPLETED

        # 6. Formal Routing & Publication Settlement.
        await self._route_and_settle_publication(
            execution=execution,
            message=message,
            terminal_reason=terminal_reason,
            counters=counters,
            now=now,
        )

    async def _run_prescreen_phase(
        self,
        *,
        analysis_run_id: str,
        message: WindowMessageSnapshot,
        counters: _ExecutionStepCounters,
        now: datetime,
    ) -> bool:
        """Schedule root text-only PreScreen stages and return True once settled."""

        dag = await self.repository.load_analysis_dag_snapshot(analysis_run_id=analysis_run_id)
        if dag is None:
            return True
        if dag.blocked_from_analysis:
            return True

        prescreen_nodes = tuple(
            node for node in dag.nodes if not node.depends_on and node.is_text_only_prescreen
        )
        if not prescreen_nodes:
            return True

        plan = plan_pipeline_progression(
            message_id=message.message_id,
            nodes=prescreen_nodes,
            existing_runs_by_node=dag.existing_runs_by_node,
            media=dag.media,
            facts=dag.facts,
            blocked_from_analysis=dag.blocked_from_analysis,
        )
        if plan.planned_targets:
            scheduled, cached, _ = await self.repository.materialize_planned_targets(
                analysis_run_id=analysis_run_id,
                message_id=message.message_id,
                canonical_message_id=message.canonical_message_id,
                duplicate_status=message.duplicate_status,
                planned_targets=plan.planned_targets,
                run_status=AnalysisRunStatus.RUNNING.value,
                now=now,
            )
            counters.stage_runs_scheduled += scheduled
            counters.stage_runs_cached += cached
            if scheduled > 0:
                return False

        if plan.run_status is AnalysisRunStatus.FAILED:
            await self.repository.set_analysis_run_status(
                analysis_run_id=analysis_run_id,
                status=AnalysisRunStatus.FAILED.value,
                now=now,
            )
            await self.repository.mark_message_failed(
                message_id=message.message_id,
                failure_stage=MessageFailureStage.ANALYSIS_FAILED.value,
                error_code=message.formal_analysis_error_code or "PRESCREEN_STAGE_FAILED",
                now=now,
            )
            return False

        return not plan.waiting_for_active_stages

    async def _progress_full_dag(
        self,
        *,
        analysis_run_id: str,
        message: WindowMessageSnapshot,
        counters: _ExecutionStepCounters,
        now: datetime,
    ) -> str:
        # Loop bounded by node count + 1 so skipped/cached nodes immediately unlock children!
        for _ in range(32):
            dag = await self.repository.load_analysis_dag_snapshot(analysis_run_id=analysis_run_id)
            if dag is None:
                return "no_eligible_stages"
            if dag.blocked_from_analysis:
                await self.repository.set_analysis_run_status(
                    analysis_run_id=analysis_run_id,
                    status=AnalysisRunStatus.BLOCKED_NEGATIVE_GATE.value,
                    now=now,
                )
                return "blocked_negative_gate"

            plan = plan_pipeline_progression(
                message_id=message.message_id,
                nodes=dag.nodes,
                existing_runs_by_node=dag.existing_runs_by_node,
                media=dag.media,
                facts=dag.facts,
                blocked_from_analysis=dag.blocked_from_analysis,
            )
            if not plan.planned_targets:
                if plan.run_status is AnalysisRunStatus.RUNNING:
                    await self.repository.set_analysis_run_status(
                        analysis_run_id=analysis_run_id,
                        status=AnalysisRunStatus.RUNNING.value,
                        now=now,
                    )
                    return "waiting"
                if plan.run_status is AnalysisRunStatus.FAILED:
                    await self.repository.set_analysis_run_status(
                        analysis_run_id=analysis_run_id,
                        status=AnalysisRunStatus.FAILED.value,
                        now=now,
                    )
                    await self.repository.mark_message_failed(
                        message_id=message.message_id,
                        failure_stage=MessageFailureStage.ANALYSIS_FAILED.value,
                        error_code=message.formal_analysis_error_code or "ANALYSIS_STAGE_FAILED",
                        now=now,
                    )
                    return "failed"
                if plan.run_status is AnalysisRunStatus.BLOCKED_NEGATIVE_GATE:
                    await self.repository.set_analysis_run_status(
                        analysis_run_id=analysis_run_id,
                        status=AnalysisRunStatus.BLOCKED_NEGATIVE_GATE.value,
                        now=now,
                    )
                    return "blocked_negative_gate"

                await self.repository.set_analysis_run_status(
                    analysis_run_id=analysis_run_id,
                    status=AnalysisRunStatus.COMPLETED.value,
                    now=now,
                )
                return "no_eligible_stages" if plan.all_nodes_skipped_or_empty else "completed"

            (
                scheduled,
                cached,
                negative_triggered,
            ) = await self.repository.materialize_planned_targets(
                analysis_run_id=analysis_run_id,
                message_id=message.message_id,
                canonical_message_id=message.canonical_message_id,
                duplicate_status=message.duplicate_status,
                planned_targets=plan.planned_targets,
                run_status=AnalysisRunStatus.RUNNING.value,
                now=now,
            )
            counters.stage_runs_scheduled += scheduled
            counters.stage_runs_cached += cached
            if negative_triggered:
                return "blocked_negative_gate"
            if (
                scheduled > 0
                and not any(
                    item.action is PipelineNodeActionKind.SKIP_CONDITION
                    for item in plan.planned_targets
                )
                and cached == 0
            ):
                return "waiting"

        return "waiting"

    async def _route_and_settle_publication(
        self,
        *,
        execution: ActiveRangeExecutionSnapshot,
        message: WindowMessageSnapshot,
        terminal_reason: MessageTerminalReason,
        counters: _ExecutionStepCounters,
        now: datetime,
    ) -> None:
        policy_version_id = execution.profile.routing_policy_version_id
        intent_statuses = message.publication_intent_statuses

        if policy_version_id is not None and message.formal_routing_evaluation_id is None:
            if self.routing_service is None:
                return
            # Overlapping fetch windows share one formal message/policy evaluation.
            request_id = f"formal:policy:{policy_version_id}:msg:{message.message_id}"
            try:
                result = await self.routing_service.evaluate(
                    RoutingRequest(
                        request_id=request_id,
                        message_id=message.message_id,
                        routing_policy_version_id=policy_version_id,
                        dry_run=False,
                    )
                )
            except Exception as error:
                await self.repository.mark_message_failed(
                    message_id=message.message_id,
                    failure_stage=MessageFailureStage.ROUTING_FAILED.value,
                    error_code=type(error).__name__[:128],
                    now=now,
                )
                return
            if not result.reused:
                counters.routings_evaluated += 1
            intent_statuses = tuple(intent.status for intent in result.intents)

        if any(status == "failed" for status in intent_statuses):
            await self.repository.mark_message_failed(
                message_id=message.message_id,
                failure_stage=MessageFailureStage.PUBLISH_FAILED.value,
                error_code=message.publication_failed_error_code or "PUBLICATION_FAILED",
                now=now,
            )
            return

        if any(status in _ACTIVE_INTENT_STATUSES for status in intent_statuses):
            return

        await self.repository.mark_message_processed(
            message_id=message.message_id,
            terminal_reason=terminal_reason.value,
            now=now,
        )
        counters.messages_terminalized += 1


@dataclass(slots=True)
class _ExecutionStepCounters:
    messages_terminalized: int = 0
    stage_runs_scheduled: int = 0
    stage_runs_cached: int = 0
    media_wakeups_created: int = 0
    duplicates_detected: int = 0
    routings_evaluated: int = 0


def _to_window_progress(
    *,
    message: WindowMessageSnapshot,
    execution: ActiveRangeExecutionSnapshot,
) -> WindowMessageProgress:
    has_active_intents = any(
        status in _ACTIVE_INTENT_STATUSES for status in message.publication_intent_statuses
    )
    missing_formal_routing = (
        execution.profile.routing_policy_version_id is not None
        and message.terminal_reason
        not in {
            MessageTerminalReason.DUPLICATE_SKIPPED.value,
            MessageTerminalReason.UNSUPPORTED.value,
            MessageTerminalReason.ADMIN_SKIPPED.value,
        }
        and message.formal_routing_evaluation_id is None
    )
    media_unsettled = message.terminal_reason not in {
        MessageTerminalReason.ADMIN_SKIPPED.value,
        MessageTerminalReason.UNSUPPORTED.value,
        MessageTerminalReason.BLOCKED_NEGATIVE_GATE.value,
    } and (message.media.any_pending_archive or message.media.any_failed)
    has_blocking_work = (
        message.processing_status != "processed"
        or has_active_intents
        or "failed" in message.publication_intent_statuses
        or missing_formal_routing
        or media_unsettled
    )
    return WindowMessageProgress(
        primary_telegram_message_id=message.primary_telegram_message_id,
        processing_status=message.processing_status,
        terminal_reason=message.terminal_reason,
        has_blocking_work=has_blocking_work,
    )
