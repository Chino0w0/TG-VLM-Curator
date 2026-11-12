from __future__ import annotations

import unittest
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from tgcurator.application import MessagePipelineCoordinator
from tgcurator.application.ports.pipeline import (
    ActiveRangeExecutionSnapshot,
    AnalysisRunDagSnapshot,
    WakeupRepairReport,
    WindowMessageSnapshot,
)
from tgcurator.application.ports.routing import (
    PublicationIntentDraft,
    RoutingExecutionResult,
    RoutingRequest,
)
from tgcurator.domain.analysis import (
    CachePolicy,
    ExistingNodeTargetRun,
    LabelScope,
    MediaAssetTarget,
    MessageMediaReadiness,
    PipelineNodeActionKind,
    PipelineNodeSpec,
    PlannedNodeTarget,
)
from tgcurator.domain.messages import (
    DuplicateMatchResult,
    DuplicatePolicy,
    DuplicateStatus,
    VisualCandidate,
    compute_archived_visual_identity,
)
from tgcurator.domain.processing import (
    MessageFailureStage,
    MessageTerminalReason,
    SourceChannelProfileConfig,
    WindowMessageProgress,
)
from tgcurator.domain.routing import RoutingDecision

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@dataclass
class FakeStageRun:
    node_key: str
    stage_version_id: str
    target_kind: str
    target_id: str
    status: str
    result_origin: str | None = None
    reused_from: str | None = None


class InMemoryPipelineRepository:
    def __init__(self) -> None:
        self.executions: dict[str, ActiveRangeExecutionSnapshot] = {}
        self.messages: dict[str, WindowMessageSnapshot] = {}
        self.pipeline_nodes: dict[str, tuple[PipelineNodeSpec, ...]] = {}
        self.stage_requires_text: dict[str, bool] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.stage_runs_by_run: dict[str, list[FakeStageRun]] = {}
        self.canonical_visual_cache: set[tuple[str, str, str]] = set()
        self.media_wakeups: dict[str, int] = {}
        self.cancelled_media_wakeups: list[str] = []
        self.duplicate_matches: dict[str, DuplicateMatchResult] = {}
        self.candidates: list[VisualCandidate] = []
        self.processing_range_watermark: int | None = None

    async def handoff_execution(
        self, *, execution_id: str, lease_token: str, now: datetime
    ) -> bool:
        _ = (lease_token, now)
        ex = self.executions.get(execution_id)
        if ex is None or ex.status != "running":
            return False
        self.executions[execution_id] = replace(ex, status="awaiting_pipeline")
        return True

    async def list_awaiting_execution_ids(self, *, limit: int) -> tuple[str, ...]:
        return tuple(
            ex_id
            for ex_id, ex in list(self.executions.items())[:limit]
            if ex.status == "awaiting_pipeline"
        )

    async def find_awaiting_execution_ids_for_message(self, *, message_id: str) -> tuple[str, ...]:
        msg = self.messages.get(message_id)
        if msg is None:
            return ()
        return tuple(
            ex.execution_id
            for ex in self.executions.values()
            if ex.status == "awaiting_pipeline"
            and ex.source_channel_id == msg.source_channel_id
            and ex.from_message_id_exclusive
            < msg.primary_telegram_message_id
            <= ex.to_message_id_inclusive
        )

    async def find_message_id_for_image_asset(self, *, image_asset_id: str) -> str | None:
        for msg in self.messages.values():
            if any(a.asset_id == image_asset_id for a in msg.media.assets):
                return msg.message_id
        return None

    async def find_message_id_for_video_asset(self, *, video_asset_id: str) -> str | None:
        for msg in self.messages.values():
            if any(a.asset_id == video_asset_id for a in msg.media.assets):
                return msg.message_id
        return None

    async def find_message_ids_for_stage_run(self, *, stage_run_id: str) -> tuple[str, ...]:
        return (stage_run_id,) if stage_run_id in self.messages else ()

    async def find_message_id_for_publication_intent(self, *, intent_id: str) -> str | None:
        return intent_id if intent_id in self.messages else None

    async def load_execution(self, *, execution_id: str) -> ActiveRangeExecutionSnapshot | None:
        return self.executions.get(execution_id)

    async def load_window_messages(
        self, *, execution: ActiveRangeExecutionSnapshot
    ) -> tuple[WindowMessageSnapshot, ...]:
        items = [
            m
            for m in self.messages.values()
            if m.source_channel_id == execution.source_channel_id
            and execution.from_message_id_exclusive
            < m.primary_telegram_message_id
            <= execution.to_message_id_inclusive
        ]
        items.sort(key=lambda m: m.primary_telegram_message_id)
        return tuple(items)

    async def load_pipeline_nodes(
        self, *, pipeline_version_id: str
    ) -> tuple[PipelineNodeSpec, ...]:
        return self.pipeline_nodes.get(pipeline_version_id, ())

    async def ensure_formal_analysis_run(
        self, *, message_id: str, pipeline_version_id: str, now: datetime
    ) -> str:
        _ = now
        msg = self.messages[message_id]
        if msg.formal_analysis_run_id is not None:
            return msg.formal_analysis_run_id
        run_id = f"run-{message_id}"
        self.runs[run_id] = {
            "message_id": message_id,
            "pipeline_version_id": pipeline_version_id,
            "status": "running",
            "facts": {},
        }
        self.stage_runs_by_run[run_id] = []
        self.messages[message_id] = replace(
            msg,
            formal_analysis_run_id=run_id,
            formal_analysis_run_status="running",
        )
        return run_id

    async def load_analysis_dag_snapshot(
        self, *, analysis_run_id: str
    ) -> AnalysisRunDagSnapshot | None:
        run = self.runs.get(analysis_run_id)
        if run is None:
            return None
        msg = self.messages[run["message_id"]]
        nodes = self.pipeline_nodes.get(run["pipeline_version_id"], ())
        by_node: dict[str, list[ExistingNodeTargetRun]] = {}
        for sr in self.stage_runs_by_run.get(analysis_run_id, []):
            by_node.setdefault(sr.node_key, []).append(
                ExistingNodeTargetRun(sr.target_kind, sr.target_id, sr.status)
            )
        return AnalysisRunDagSnapshot(
            analysis_run_id=analysis_run_id,
            message_id=msg.message_id,
            pipeline_version_id=run["pipeline_version_id"],
            status=run["status"],
            blocked_from_analysis=msg.blocked_from_analysis,
            nodes=nodes,
            existing_runs_by_node={k: tuple(v) for k, v in by_node.items()},
            facts=dict(run["facts"]),
            media=msg.media,
        )

    async def materialize_planned_targets(
        self,
        *,
        analysis_run_id: str,
        message_id: str,
        canonical_message_id: str | None,
        duplicate_status: str,
        planned_targets: tuple[PlannedNodeTarget, ...],
        run_status: str,
        now: datetime,
    ) -> tuple[int, int, bool]:
        _ = now
        scheduled = 0
        cached = 0
        existing = self.stage_runs_by_run.setdefault(analysis_run_id, [])
        existing_keys = {(sr.node_key, sr.target_id) for sr in existing}

        for item in planned_targets:
            if (item.node_key, item.target_id) in existing_keys:
                continue
            if item.action is PipelineNodeActionKind.SKIP_CONDITION:
                existing.append(
                    FakeStageRun(
                        node_key=item.node_key,
                        stage_version_id=item.stage_version_id,
                        target_kind=item.target_kind,
                        target_id=item.target_id,
                        status="skipped_condition",
                    )
                )
                continue

            can_reuse_canonical = (
                duplicate_status == DuplicateStatus.DUPLICATE_RECLASSIFY.value
                and canonical_message_id is not None
                and item.cache_policy in {CachePolicy.ASSET, CachePolicy.MESSAGE_VISUAL}
                and not self.stage_requires_text.get(item.stage_version_id, False)
                and (canonical_message_id, item.stage_version_id, item.target_kind)
                in self.canonical_visual_cache
            )
            if can_reuse_canonical:
                existing.append(
                    FakeStageRun(
                        node_key=item.node_key,
                        stage_version_id=item.stage_version_id,
                        target_kind=item.target_kind,
                        target_id=item.target_id,
                        status="succeeded",
                        result_origin="cache",
                        reused_from=f"canonical:{canonical_message_id}",
                    )
                )
                cached += 1
                continue

            existing.append(
                FakeStageRun(
                    node_key=item.node_key,
                    stage_version_id=item.stage_version_id,
                    target_kind=item.target_kind,
                    target_id=item.target_id,
                    status="pending",
                )
            )
            scheduled += 1

        self.runs[analysis_run_id]["status"] = run_status
        msg = self.messages[message_id]
        self.messages[message_id] = replace(msg, formal_analysis_run_status=run_status)
        return scheduled, cached, False

    async def set_analysis_run_status(
        self, *, analysis_run_id: str, status: str, now: datetime
    ) -> None:
        _ = now
        run = self.runs[analysis_run_id]
        run["status"] = status
        msg = self.messages[run["message_id"]]
        self.messages[msg.message_id] = replace(msg, formal_analysis_run_status=status)

    async def ensure_media_archive_wakeups(self, *, message_id: str, now: datetime) -> int:
        _ = now
        msg = self.messages[message_id]
        pending = sum(1 for a in msg.media.assets if a.archive_state == "pending")
        if message_id in self.media_wakeups:
            return 0
        self.media_wakeups[message_id] = pending
        return pending

    async def cancel_pending_media_wakeups(self, *, message_id: str, now: datetime) -> int:
        _ = now
        self.cancelled_media_wakeups.append(message_id)
        return self.media_wakeups.pop(message_id, 0)

    async def load_visual_duplicate_candidates(
        self,
        *,
        message_id: str,
        published_at: datetime,
        visual_fingerprint: str | None,
        limit: int = 500,
    ) -> tuple[VisualCandidate, ...]:
        _ = (visual_fingerprint, limit)
        return tuple(
            c
            for c in self.candidates
            if c.message_id != message_id and c.published_at <= published_at
        )

    async def record_visual_deduplication(
        self,
        *,
        message_id: str,
        visual_fingerprint: str | None,
        match: DuplicateMatchResult,
        now: datetime,
    ) -> None:
        _ = now
        self.duplicate_matches[message_id] = match
        msg = self.messages[message_id]
        self.messages[message_id] = replace(
            msg,
            visual_fingerprint=visual_fingerprint or msg.visual_fingerprint,
            duplicate_status=match.status.value,
            canonical_message_id=match.canonical_message_id,
            duplicate_distance=match.hamming_distance,
        )

    async def mark_message_processing(self, *, message_id: str, now: datetime) -> None:
        _ = now
        msg = self.messages[message_id]
        if msg.processing_status == "ingested":
            self.messages[message_id] = replace(msg, processing_status="processing")

    async def mark_message_processed(
        self, *, message_id: str, terminal_reason: str, now: datetime
    ) -> None:
        _ = now
        msg = self.messages[message_id]
        self.messages[message_id] = replace(
            msg,
            processing_status="processed",
            terminal_reason=terminal_reason,
            failure_stage=None,
            last_error_code=None,
        )

    async def mark_message_failed(
        self, *, message_id: str, failure_stage: str, error_code: str, now: datetime
    ) -> None:
        _ = now
        msg = self.messages[message_id]
        self.messages[message_id] = replace(
            msg,
            processing_status="failed",
            terminal_reason=None,
            failure_stage=failure_stage,
            last_error_code=error_code,
        )

    async def persist_watermark_advancement(
        self,
        *,
        execution_id: str,
        next_watermark_message_id: int,
        window_completed: bool,
        progress_snapshot: tuple[WindowMessageProgress, ...],
        now: datetime,
    ) -> tuple[bool, bool]:
        _ = (progress_snapshot, now)
        ex = self.executions[execution_id]
        advanced = next_watermark_message_id > ex.watermark_message_id
        status = "completed" if window_completed else ex.status
        self.executions[execution_id] = replace(
            ex,
            watermark_message_id=next_watermark_message_id,
            status=status,
        )
        self.processing_range_watermark = next_watermark_message_id
        return advanced, window_completed

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> WakeupRepairReport:
        _ = (now, limit)
        return WakeupRepairReport()


@dataclass
class FakeRoutingService:
    repository: InMemoryPipelineRepository
    intents_by_message: dict[str, tuple[str, ...]] = field(default_factory=dict)
    requests: list[RoutingRequest] = field(default_factory=list)

    async def evaluate(self, request: RoutingRequest) -> RoutingExecutionResult:
        self.requests.append(request)
        statuses = self.intents_by_message.get(request.message_id, ())
        intents = tuple(
            PublicationIntentDraft(
                intent_id=f"intent-{request.message_id}-{idx}",
                routing_evaluation_id=f"eval-{request.message_id}",
                routing_request_id=request.request_id,
                message_id=request.message_id,
                routing_policy_version_id=request.routing_policy_version_id,
                routing_rule_id="rule-1",
                routing_action_id=f"action-{idx}",
                destination_channel_id=f"dest-{idx}",
                publish_identity_id="ident-1",
                publication_mode="metadata_only",
                rendering_template_version_id="tmpl-1",
                business_idempotency_key=f"idem-{request.message_id}-{idx}",
                rendered_text="rendered",
                status=status,
            )
            for idx, status in enumerate(statuses)
        )
        msg = self.repository.messages[request.message_id]
        self.repository.messages[request.message_id] = replace(
            msg,
            formal_routing_evaluation_id=f"eval-{request.message_id}",
            publication_intent_statuses=statuses,
        )
        return RoutingExecutionResult(
            evaluation_id=f"eval-{request.message_id}",
            routing_request_id=request.request_id,
            message_id=request.message_id,
            routing_policy_version_id=request.routing_policy_version_id,
            facts_snapshot={},
            facts_hash="a" * 64,
            decision=RoutingDecision(
                policy_version_id=request.routing_policy_version_id,
                outcomes=(),
                actions=(),
            ),
            intents=intents,
            persisted=not request.dry_run,
            reused=False,
            dry_run=request.dry_run,
        )


def make_message(
    message_id: str,
    tg_id: int,
    *,
    source_channel_id: str = "00000000-0000-0000-0000-000000000001",
    media_states: tuple[tuple[str, str, str], ...] = (),
    image_phashes: tuple[str | None, ...] = (),
) -> WindowMessageSnapshot:
    return WindowMessageSnapshot(
        message_id=message_id,
        source_channel_id=source_channel_id,
        primary_telegram_message_id=tg_id,
        published_at=NOW - timedelta(minutes=10),
        processing_status="ingested",
        terminal_reason=None,
        failure_stage=None,
        last_error_code=None,
        blocked_from_analysis=False,
        duplicate_status="unchecked",
        canonical_message_id=None,
        duplicate_distance=None,
        visual_fingerprint=None,
        image_phashes=image_phashes,
        video_cover_phashes=(),
        media=MessageMediaReadiness(
            tuple(MediaAssetTarget(k, aid, st) for k, aid, st in media_states)
        ),
        formal_analysis_run_id=None,
        formal_analysis_run_status=None,
        formal_analysis_error_code=None,
        formal_routing_evaluation_id=None,
        publication_intent_statuses=(),
        publication_failed_error_code=None,
    )


class PipelineCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_prescreen_runs_before_media_download_and_negative_gate_skips_download_yet_routes(
        self,
    ) -> None:
        repo = InMemoryPipelineRepository()
        routing = FakeRoutingService(repository=repo)
        coordinator = MessagePipelineCoordinator(repository=repo, routing_service=routing)

        pipeline_id = "00000000-0000-0000-0000-000000000010"
        routing_id = "00000000-0000-0000-0000-000000000020"
        source_id = "00000000-0000-0000-0000-000000000001"
        repo.pipeline_nodes[pipeline_id] = (
            PipelineNodeSpec(
                node_id="n-pre",
                node_key="prescreen",
                stage_version_id="sv-pre",
                target_scope=LabelScope.GLOBAL,
                input_requires_media=False,
                is_text_only_prescreen=True,
                cache_policy=CachePolicy.MESSAGE,
                max_attempts=3,
            ),
            PipelineNodeSpec(
                node_id="n-vis",
                node_key="vision",
                stage_version_id="sv-vis",
                target_scope=LabelScope.MEDIA,
                input_requires_media=True,
                is_text_only_prescreen=False,
                cache_policy=CachePolicy.ASSET,
                max_attempts=3,
                depends_on=("prescreen",),
            ),
        )
        repo.executions["exec-1"] = ActiveRangeExecutionSnapshot(
            execution_id="exec-1",
            processing_range_id="range-1",
            source_channel_id=source_id,
            source_profile_version_id="prof-1",
            from_message_id_exclusive=100,
            to_message_id_inclusive=105,
            watermark_message_id=100,
            status="running",
            profile=SourceChannelProfileConfig(
                analysis_pipeline_version_id=pipeline_id,
                routing_policy_version_id=routing_id,
                prescreen_enabled=True,
            ),
        )
        repo.messages["m1"] = make_message(
            "m1",
            101,
            media_states=(("image_asset", "img-1", "pending"),),
        )
        repo.messages["m2"] = make_message(
            "m2",
            103,
            media_states=(("image_asset", "img-2", "pending"),),
        )

        # Handoff schedules PreScreen for m1 & m2 BEFORE creating any media archive wakeups!
        self.assertTrue(
            await coordinator.handoff_and_drive_execution(
                execution_id="exec-1",
                lease_token="lease-1",
                now=NOW,
            )
        )
        self.assertEqual(repo.media_wakeups, {})
        self.assertEqual(len(repo.stage_runs_by_run["run-m1"]), 1)
        self.assertEqual(len(repo.stage_runs_by_run["run-m2"]), 1)

        # Simulate PreScreen worker completing: m1 triggers Negative Gate; m2 passes cleanly!
        repo.stage_runs_by_run["run-m1"][0].status = "succeeded"
        repo.runs["run-m1"]["status"] = "blocked_negative_gate"
        repo.messages["m1"] = replace(
            repo.messages["m1"],
            blocked_from_analysis=True,
            formal_analysis_run_status="blocked_negative_gate",
        )
        repo.stage_runs_by_run["run-m2"][0].status = "succeeded"

        step2 = await coordinator.drive_execution(execution_id="exec-1", now=NOW)
        # m1 NEVER created media wakeups, DID run formal routing, and reached blocked_negative_gate!
        self.assertNotIn("m1", repo.media_wakeups)
        self.assertEqual([r.message_id for r in routing.requests], ["m1"])
        self.assertEqual(repo.messages["m1"].processing_status, "processed")
        self.assertEqual(
            repo.messages["m1"].terminal_reason,
            MessageTerminalReason.BLOCKED_NEGATIVE_GATE.value,
        )
        # m2 passed PreScreen -> media archive wakeup unlocked, and watermark advanced to 102!
        self.assertEqual(repo.media_wakeups.get("m2"), 1)
        self.assertEqual(step2.media_wakeups_created, 1)
        self.assertEqual(repo.executions["exec-1"].watermark_message_id, 102)

    async def test_visual_dedup_skip_versus_reclassify_with_text_and_version_matched_cache(
        self,
    ) -> None:
        repo = InMemoryPipelineRepository()
        routing = FakeRoutingService(repository=repo)
        coordinator = MessagePipelineCoordinator(repository=repo, routing_service=routing)

        canonical_fp, canonical_tokens = compute_archived_visual_identity(
            image_phashes=("0000000000000000",),
            video_cover_phashes=(),
        )
        repo.candidates.append(
            VisualCandidate(
                message_id="msg-canonical",
                visual_fingerprint=canonical_fp,
                visual_tokens=canonical_tokens,
                published_at=NOW - timedelta(hours=1),
            )
        )
        repo.canonical_visual_cache.add(("msg-canonical", "sv-visual-v1", "image_asset"))

        pipeline_id = "00000000-0000-0000-0000-000000000011"
        routing_id = "00000000-0000-0000-0000-000000000021"
        source_id = "00000000-0000-0000-0000-000000000001"
        repo.stage_requires_text["sv-visual-v1"] = False
        repo.stage_requires_text["sv-visual-v2"] = False
        repo.stage_requires_text["sv-text-v1"] = True

        # Case A: duplicate_policy = skip -> skips analysis & formal routing, advances watermark!
        repo.pipeline_nodes[pipeline_id] = (
            PipelineNodeSpec(
                node_id="n-vis",
                node_key="vision",
                stage_version_id="sv-visual-v1",
                target_scope=LabelScope.MEDIA,
                input_requires_media=True,
                is_text_only_prescreen=False,
                cache_policy=CachePolicy.ASSET,
                max_attempts=3,
            ),
        )
        repo.executions["exec-skip"] = ActiveRangeExecutionSnapshot(
            execution_id="exec-skip",
            processing_range_id="range-skip",
            source_channel_id=source_id,
            source_profile_version_id="prof-skip",
            from_message_id_exclusive=10,
            to_message_id_inclusive=20,
            watermark_message_id=10,
            status="awaiting_pipeline",
            profile=SourceChannelProfileConfig(
                analysis_pipeline_version_id=pipeline_id,
                routing_policy_version_id=routing_id,
                duplicate_policy=DuplicatePolicy.SKIP,
                phash_distance_threshold=4,
            ),
        )
        repo.messages["m-dup-skip"] = make_message(
            "m-dup-skip",
            15,
            media_states=(("image_asset", "img-skip", "ready"),),
            image_phashes=("0000000000000003",),  # Hamming distance 2 <= 4
        )
        skip_report = await coordinator.drive_execution(execution_id="exec-skip", now=NOW)
        self.assertEqual(skip_report.duplicates_detected, 1)
        self.assertEqual(skip_report.stage_runs_scheduled, 0)
        self.assertEqual(routing.requests, [])
        self.assertEqual(
            repo.messages["m-dup-skip"].terminal_reason,
            MessageTerminalReason.DUPLICATE_SKIPPED.value,
        )
        self.assertEqual(repo.executions["exec-skip"].status, "completed")
        self.assertEqual(repo.executions["exec-skip"].watermark_message_id, 20)

        # Case B: reclassify_with_text reuses canonical visual stage (sv-visual-v1), does NOT reuse
        # mismatched version (sv-visual-v2), and runs text stage (sv-text-v1) fresh!
        pipeline_reclass = "00000000-0000-0000-0000-000000000012"
        repo.pipeline_nodes[pipeline_reclass] = (
            PipelineNodeSpec(
                node_id="n-vis-v1",
                node_key="vision_v1",
                stage_version_id="sv-visual-v1",
                target_scope=LabelScope.MEDIA,
                input_requires_media=True,
                is_text_only_prescreen=False,
                cache_policy=CachePolicy.ASSET,
                max_attempts=3,
            ),
            PipelineNodeSpec(
                node_id="n-vis-v2",
                node_key="vision_v2",
                stage_version_id="sv-visual-v2",
                target_scope=LabelScope.MEDIA,
                input_requires_media=True,
                is_text_only_prescreen=False,
                cache_policy=CachePolicy.ASSET,
                max_attempts=3,
            ),
            PipelineNodeSpec(
                node_id="n-txt",
                node_key="text_reclassify",
                stage_version_id="sv-text-v1",
                target_scope=LabelScope.GLOBAL,
                input_requires_media=False,
                is_text_only_prescreen=False,
                cache_policy=CachePolicy.MESSAGE,
                max_attempts=3,
                depends_on=("vision_v1",),
            ),
        )
        source_2 = "00000000-0000-0000-0000-000000000002"
        repo.executions["exec-reclass"] = ActiveRangeExecutionSnapshot(
            execution_id="exec-reclass",
            processing_range_id="range-reclass",
            source_channel_id=source_2,
            source_profile_version_id="prof-reclass",
            from_message_id_exclusive=100,
            to_message_id_inclusive=110,
            watermark_message_id=100,
            status="awaiting_pipeline",
            profile=SourceChannelProfileConfig(
                analysis_pipeline_version_id=pipeline_reclass,
                routing_policy_version_id=routing_id,
                duplicate_policy=DuplicatePolicy.RECLASSIFY_WITH_TEXT,
                phash_distance_threshold=4,
                prescreen_enabled=False,
            ),
        )
        repo.messages["m-reclass"] = make_message(
            "m-reclass",
            105,
            source_channel_id=source_2,
            media_states=(("image_asset", "img-r", "ready"),),
            image_phashes=("0000000000000000",),
        )

        reclass_step = await coordinator.drive_execution(execution_id="exec-reclass", now=NOW)
        self.assertEqual(reclass_step.duplicates_detected, 1)
        # vision_v1 was reused from canonical cache, which immediately unlocked text_reclassify,
        # while vision_v2 (version mismatch!) was scheduled fresh alongside text_reclassify!
        self.assertEqual(reclass_step.stage_runs_cached, 1)
        self.assertEqual(reclass_step.stage_runs_scheduled, 2)
        run_stages = {sr.node_key: sr for sr in repo.stage_runs_by_run["run-m-reclass"]}
        self.assertEqual(run_stages["vision_v1"].result_origin, "cache")
        self.assertEqual(run_stages["vision_v2"].status, "pending")
        self.assertEqual(run_stages["text_reclassify"].status, "pending")

    async def test_publication_settlement_holds_watermark_until_all_intents_complete(
        self,
    ) -> None:
        repo = InMemoryPipelineRepository()
        routing = FakeRoutingService(
            repository=repo,
            intents_by_message={"m-pub": ("pending",)},
        )
        coordinator = MessagePipelineCoordinator(repository=repo, routing_service=routing)

        routing_id = "00000000-0000-0000-0000-000000000022"
        source_id = "00000000-0000-0000-0000-000000000003"
        repo.executions["exec-pub"] = ActiveRangeExecutionSnapshot(
            execution_id="exec-pub",
            processing_range_id="range-pub",
            source_channel_id=source_id,
            source_profile_version_id="prof-pub",
            from_message_id_exclusive=200,
            to_message_id_inclusive=210,
            watermark_message_id=200,
            status="awaiting_pipeline",
            profile=SourceChannelProfileConfig(routing_policy_version_id=routing_id),
        )
        repo.messages["m-pub"] = make_message("m-pub", 205, source_channel_id=source_id)

        first = await coordinator.drive_execution(execution_id="exec-pub", now=NOW)
        self.assertEqual(first.routings_evaluated, 1)
        # Pending PublicationIntent holds watermark at 204 (m-pub is 205)!
        self.assertEqual(repo.messages["m-pub"].processing_status, "processing")
        self.assertEqual(repo.executions["exec-pub"].watermark_message_id, 204)
        self.assertEqual(repo.executions["exec-pub"].status, "awaiting_pipeline")

        # Simulate PublicationWorker completing the intent -> watermark advances to 210!
        repo.messages["m-pub"] = replace(
            repo.messages["m-pub"],
            publication_intent_statuses=("completed",),
        )
        settled = await coordinator.on_publication_intent_event(intent_id="m-pub", now=NOW)
        self.assertEqual(settled.executions_completed, 1)
        self.assertEqual(repo.messages["m-pub"].processing_status, "processed")
        self.assertEqual(
            repo.messages["m-pub"].terminal_reason,
            MessageTerminalReason.NO_ELIGIBLE_STAGES.value,
        )
        self.assertEqual(repo.executions["exec-pub"].watermark_message_id, 210)
        self.assertEqual(repo.executions["exec-pub"].status, "completed")

    async def test_failed_publication_marks_publish_failed_and_blocks_watermark(self) -> None:
        repo = InMemoryPipelineRepository()
        routing = FakeRoutingService(
            repository=repo,
            intents_by_message={"m-fail": ("failed",)},
        )
        coordinator = MessagePipelineCoordinator(repository=repo, routing_service=routing)

        routing_id = "00000000-0000-0000-0000-000000000023"
        source_id = "00000000-0000-0000-0000-000000000004"
        repo.executions["exec-fail"] = ActiveRangeExecutionSnapshot(
            execution_id="exec-fail",
            processing_range_id="range-fail",
            source_channel_id=source_id,
            source_profile_version_id="prof-fail",
            from_message_id_exclusive=300,
            to_message_id_inclusive=310,
            watermark_message_id=300,
            status="awaiting_pipeline",
            profile=SourceChannelProfileConfig(routing_policy_version_id=routing_id),
        )
        repo.messages["m-fail"] = make_message("m-fail", 304, source_channel_id=source_id)

        await coordinator.drive_execution(execution_id="exec-fail", now=NOW)
        self.assertEqual(repo.messages["m-fail"].processing_status, "failed")
        self.assertEqual(
            repo.messages["m-fail"].failure_stage,
            MessageFailureStage.PUBLISH_FAILED.value,
        )
        self.assertEqual(repo.executions["exec-fail"].watermark_message_id, 303)
        self.assertEqual(repo.executions["exec-fail"].status, "awaiting_pipeline")


if __name__ == "__main__":
    unittest.main()
