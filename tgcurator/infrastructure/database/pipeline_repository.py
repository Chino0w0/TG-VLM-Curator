from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tgcurator.application.media.archive_wakeups import (
    IMAGE_ARCHIVE_QUEUE,
    VIDEO_ARCHIVE_QUEUE,
)
from tgcurator.application.ports.pipeline import (
    ActiveRangeExecutionSnapshot,
    AnalysisRunDagSnapshot,
    PipelineNodeSpec,
    WakeupRepairReport,
    WindowMessageSnapshot,
)
from tgcurator.domain.analysis import (
    CachePolicy,
    ExistingNodeTargetRun,
    LabelScope,
    MediaAssetTarget,
    MessageMediaReadiness,
    PipelineNodeActionKind,
    PlannedNodeTarget,
    StageRetryPolicy,
)
from tgcurator.domain.analysis.pipeline import _ALLOWED_STAGE_OVERRIDES
from tgcurator.domain.messages import (
    DuplicateMatchResult,
    VisualCandidate,
    compute_archived_visual_identity,
)
from tgcurator.domain.processing import (
    SourceChannelProfileConfig,
    WindowMessageProgress,
)
from tgcurator.shared import DomainValidationError, ensure_aware

from .analysis_repository import ANALYSIS_QUEUE
from .models import (
    AnalysisRunRecord,
    AnalysisStageTemplateVersionRecord,
    DuplicateMatchRecord,
    DurableWakeupRecord,
    ImageAssetRecord,
    MessagePartRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    PipelineStageNodeRecord,
    ProcessingRangeRecord,
    PublicationIntentRecord,
    RangeExecutionRecord,
    RoutingEvaluationRecord,
    SourceChannelProfileVersion,
    StageRunRecord,
    VideoAssetRecord,
    VideoFrameRecord,
)
from .processing_repository import RANGE_EXECUTION_QUEUE
from .session import AsyncDatabase

_TERMINAL_RUN_STATUSES = ("completed", "blocked_negative_gate", "failed")


class SqlAlchemyMessagePipelineRepository:
    """PostgreSQL persistence for M7 end-to-end message pipeline orchestration."""

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._database = database
        self._id_factory = id_factory or uuid4

    async def handoff_execution(
        self,
        *,
        execution_id: str,
        lease_token: str,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        exec_uuid = UUID(execution_id)
        token_uuid = UUID(lease_token)
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(RangeExecutionRecord)
                    .where(
                        RangeExecutionRecord.id == exec_uuid,
                        RangeExecutionRecord.status == "running",
                        RangeExecutionRecord.lease_token == token_uuid,
                        RangeExecutionRecord.lease_expires_at > now,
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                row.status = "awaiting_pipeline"
                row.lease_token = None
                row.lease_expires_at = None
                row.updated_at = now
                await session.execute(
                    update(DurableWakeupRecord)
                    .where(
                        DurableWakeupRecord.queue == RANGE_EXECUTION_QUEUE,
                        DurableWakeupRecord.entity_id == row.id,
                        DurableWakeupRecord.status != "cancelled",
                    )
                    .values(
                        status="completed",
                        lease_token=None,
                        lease_expires_at=None,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                return True

    async def list_awaiting_execution_ids(self, *, limit: int) -> tuple[str, ...]:
        async with self._database.session() as session:
            rows = tuple(
                await session.scalars(
                    select(RangeExecutionRecord.id)
                    .where(RangeExecutionRecord.status == "awaiting_pipeline")
                    .order_by(RangeExecutionRecord.created_at, RangeExecutionRecord.id)
                    .limit(limit)
                )
            )
            return tuple(str(item) for item in rows)

    async def find_awaiting_execution_ids_for_message(self, *, message_id: str) -> tuple[str, ...]:
        msg_uuid = UUID(message_id)
        async with self._database.session() as session:
            msg = await session.scalar(select(MessageRecord).where(MessageRecord.id == msg_uuid))
            if msg is None:
                return ()
            rows = tuple(
                await session.scalars(
                    select(RangeExecutionRecord.id)
                    .join(
                        ProcessingRangeRecord,
                        ProcessingRangeRecord.id == RangeExecutionRecord.processing_range_id,
                    )
                    .where(
                        ProcessingRangeRecord.source_channel_id == msg.source_channel_id,
                        RangeExecutionRecord.status == "awaiting_pipeline",
                        or_(
                            and_(
                                RangeExecutionRecord.from_message_id_exclusive
                                < msg.primary_telegram_message_id,
                                RangeExecutionRecord.to_message_id_inclusive
                                >= msg.primary_telegram_message_id,
                            ),
                            exists().where(
                                MessagePartRecord.message_id == msg_uuid,
                                MessagePartRecord.telegram_message_id
                                > RangeExecutionRecord.from_message_id_exclusive,
                                MessagePartRecord.telegram_message_id
                                <= RangeExecutionRecord.to_message_id_inclusive,
                            ),
                        ),
                    )
                    .order_by(RangeExecutionRecord.created_at, RangeExecutionRecord.id)
                )
            )
            return tuple(str(item) for item in rows)

    async def find_message_id_for_image_asset(self, *, image_asset_id: str) -> str | None:
        async with self._database.session() as session:
            msg_id = await session.scalar(
                select(ImageAssetRecord.message_id).where(
                    ImageAssetRecord.id == UUID(image_asset_id)
                )
            )
            return str(msg_id) if msg_id is not None else None

    async def find_message_id_for_video_asset(self, *, video_asset_id: str) -> str | None:
        async with self._database.session() as session:
            msg_id = await session.scalar(
                select(VideoAssetRecord.message_id).where(
                    VideoAssetRecord.id == UUID(video_asset_id)
                )
            )
            return str(msg_id) if msg_id is not None else None

    async def find_message_ids_for_stage_run(self, *, stage_run_id: str) -> tuple[str, ...]:
        async with self._database.session() as session:
            row = await session.scalar(
                select(StageRunRecord).where(StageRunRecord.id == UUID(stage_run_id))
            )
            if row is None:
                return ()
            if row.inference_call_id is None:
                return (str(row.message_id),)
            msg_ids = tuple(
                await session.scalars(
                    select(StageRunRecord.message_id)
                    .where(StageRunRecord.inference_call_id == row.inference_call_id)
                    .order_by(StageRunRecord.id)
                )
            )
            seen: list[str] = []
            for item in msg_ids:
                text = str(item)
                if text not in seen:
                    seen.append(text)
            return tuple(seen) if seen else (str(row.message_id),)

    async def find_message_id_for_publication_intent(self, *, intent_id: str) -> str | None:
        async with self._database.session() as session:
            msg_id = await session.scalar(
                select(PublicationIntentRecord.message_id).where(
                    PublicationIntentRecord.id == UUID(intent_id)
                )
            )
            return str(msg_id) if msg_id is not None else None

    async def load_execution(self, *, execution_id: str) -> ActiveRangeExecutionSnapshot | None:
        exec_uuid = UUID(execution_id)
        async with self._database.session() as session:
            selected = (
                await session.execute(
                    select(
                        RangeExecutionRecord,
                        ProcessingRangeRecord.source_channel_id,
                        SourceChannelProfileVersion.content,
                    )
                    .join(
                        ProcessingRangeRecord,
                        ProcessingRangeRecord.id == RangeExecutionRecord.processing_range_id,
                    )
                    .join(
                        SourceChannelProfileVersion,
                        SourceChannelProfileVersion.id
                        == RangeExecutionRecord.source_profile_version_id,
                    )
                    .where(RangeExecutionRecord.id == exec_uuid)
                )
            ).one_or_none()
            if selected is None:
                return None
            row, source_channel_id, content = selected
            profile = SourceChannelProfileConfig.from_content(content or {})
            return ActiveRangeExecutionSnapshot(
                execution_id=str(row.id),
                processing_range_id=str(row.processing_range_id),
                source_channel_id=str(source_channel_id),
                source_profile_version_id=str(row.source_profile_version_id),
                from_message_id_exclusive=row.from_message_id_exclusive,
                to_message_id_inclusive=row.to_message_id_inclusive,
                watermark_message_id=row.watermark_message_id,
                status=row.status,
                profile=profile,
            )

    async def load_window_messages(
        self,
        *,
        execution: ActiveRangeExecutionSnapshot,
    ) -> tuple[WindowMessageSnapshot, ...]:
        source_uuid = UUID(execution.source_channel_id)
        async with self._database.session() as session:
            messages = tuple(
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.source_channel_id == source_uuid,
                        or_(
                            and_(
                                MessageRecord.primary_telegram_message_id
                                > execution.from_message_id_exclusive,
                                MessageRecord.primary_telegram_message_id
                                <= execution.to_message_id_inclusive,
                            ),
                            exists().where(
                                MessagePartRecord.message_id == MessageRecord.id,
                                MessagePartRecord.telegram_message_id
                                > execution.from_message_id_exclusive,
                                MessagePartRecord.telegram_message_id
                                <= execution.to_message_id_inclusive,
                            ),
                        ),
                    )
                    .order_by(MessageRecord.primary_telegram_message_id, MessageRecord.id)
                )
            )
            if not messages:
                return ()
            msg_ids = tuple(msg.id for msg in messages)

            images_by_msg: dict[UUID, list[ImageAssetRecord]] = defaultdict(list)
            for img in await session.scalars(
                select(ImageAssetRecord)
                .where(ImageAssetRecord.message_id.in_(msg_ids))
                .order_by(ImageAssetRecord.id)
            ):
                images_by_msg[img.message_id].append(img)

            videos_by_msg: dict[UUID, list[VideoAssetRecord]] = defaultdict(list)
            video_ids: list[UUID] = []
            for vid in await session.scalars(
                select(VideoAssetRecord)
                .where(VideoAssetRecord.message_id.in_(msg_ids))
                .order_by(VideoAssetRecord.id)
            ):
                videos_by_msg[vid.message_id].append(vid)
                video_ids.append(vid.id)

            cover_phash_by_video: dict[UUID, str | None] = {}
            if video_ids:
                for frame in await session.scalars(
                    select(VideoFrameRecord)
                    .where(
                        VideoFrameRecord.video_asset_id.in_(tuple(video_ids)),
                        VideoFrameRecord.frame_role == "cover",
                    )
                    .order_by(VideoFrameRecord.frame_index)
                ):
                    cover_phash_by_video.setdefault(frame.video_asset_id, frame.perceptual_hash)

            formal_run_by_msg: dict[UUID, AnalysisRunRecord] = {}
            if execution.profile.analysis_pipeline_version_id is not None:
                pipeline_uuid = UUID(execution.profile.analysis_pipeline_version_id)
                for run in await session.scalars(
                    select(AnalysisRunRecord)
                    .where(
                        AnalysisRunRecord.message_id.in_(msg_ids),
                        AnalysisRunRecord.analysis_pipeline_version_id == pipeline_uuid,
                        AnalysisRunRecord.run_mode == "formal",
                    )
                    .order_by(AnalysisRunRecord.created_at.desc(), AnalysisRunRecord.id.desc())
                ):
                    message = next(msg for msg in messages if msg.id == run.message_id)
                    if (run.facts_snapshot or {}).get("_pipeline_input_revision") == (
                        _message_input_revision(message)
                    ):
                        formal_run_by_msg.setdefault(run.message_id, run)

            # Formal routing belongs to a message/policy, not a history-fetch window.
            # Include legacy execution requests so upgrades do not republish.
            eval_by_msg: dict[UUID, RoutingEvaluationRecord] = {}
            eval_ids: list[UUID] = []
            for ev in await session.scalars(
                select(RoutingEvaluationRecord)
                .where(
                    RoutingEvaluationRecord.message_id.in_(msg_ids),
                    RoutingEvaluationRecord.routing_policy_version_id
                    == (
                        UUID(execution.profile.routing_policy_version_id)
                        if execution.profile.routing_policy_version_id
                        else None
                    ),
                    RoutingEvaluationRecord.routing_request_id.like("formal:%"),
                )
                .order_by(
                    RoutingEvaluationRecord.created_at.desc(), RoutingEvaluationRecord.id.desc()
                )
            ):
                if ev.message_id not in eval_by_msg:
                    eval_by_msg[ev.message_id] = ev
                    eval_ids.append(ev.id)

            intents_by_eval: dict[UUID, list[PublicationIntentRecord]] = defaultdict(list)
            if eval_ids:
                for intent in await session.scalars(
                    select(PublicationIntentRecord)
                    .where(PublicationIntentRecord.routing_evaluation_id.in_(tuple(eval_ids)))
                    .order_by(PublicationIntentRecord.created_at, PublicationIntentRecord.id)
                ):
                    intents_by_eval[intent.routing_evaluation_id].append(intent)

        snapshots: list[WindowMessageSnapshot] = []
        for msg in messages:
            imgs = images_by_msg.get(msg.id, [])
            vids = videos_by_msg.get(msg.id, [])
            media_targets = tuple(
                [MediaAssetTarget("image_asset", str(img.id), img.archive_state) for img in imgs]
                + [MediaAssetTarget("video_asset", str(vid.id), vid.archive_state) for vid in vids]
            )
            img_phashes = tuple(img.perceptual_hash or img.source_phash for img in imgs)
            vid_phashes = tuple(
                cover_phash_by_video.get(vid.id) or vid.source_cover_phash for vid in vids
            )
            formal_run = formal_run_by_msg.get(msg.id)
            ev = eval_by_msg.get(msg.id)
            intents = intents_by_eval.get(ev.id, []) if ev is not None else []
            failed_intent_code = next(
                (
                    intent.last_error_code
                    for intent in intents
                    if intent.status == "failed" and intent.last_error_code
                ),
                None,
            )
            snapshots.append(
                WindowMessageSnapshot(
                    message_id=str(msg.id),
                    source_channel_id=str(msg.source_channel_id),
                    # Window-local component coordinate; never change the parent primary.
                    primary_telegram_message_id=min(
                        (
                            part_id
                            for part_id in msg.telegram_message_ids
                            if execution.from_message_id_exclusive
                            < part_id
                            <= execution.to_message_id_inclusive
                        ),
                        key=lambda part_id: (part_id <= execution.watermark_message_id, part_id),
                    ),
                    published_at=msg.published_at,
                    processing_status=msg.processing_status,
                    terminal_reason=getattr(msg, "terminal_reason", None),
                    failure_stage=getattr(msg, "failure_stage", None),
                    last_error_code=getattr(msg, "last_error_code", None),
                    blocked_from_analysis=msg.blocked_from_analysis,
                    duplicate_status=getattr(msg, "duplicate_status", None) or "unchecked",
                    canonical_message_id=(
                        str(msg.canonical_message_id)
                        if getattr(msg, "canonical_message_id", None) is not None
                        else None
                    ),
                    duplicate_distance=getattr(msg, "duplicate_distance", None),
                    visual_fingerprint=msg.visual_fingerprint,
                    image_phashes=img_phashes,
                    video_cover_phashes=vid_phashes,
                    media=MessageMediaReadiness(media_targets),
                    formal_analysis_run_id=str(formal_run.id) if formal_run is not None else None,
                    formal_analysis_run_status=(
                        formal_run.status if formal_run is not None else None
                    ),
                    formal_analysis_error_code=(
                        formal_run.last_error_code if formal_run is not None else None
                    ),
                    formal_routing_evaluation_id=str(ev.id) if ev is not None else None,
                    publication_intent_statuses=tuple(intent.status for intent in intents),
                    publication_failed_error_code=failed_intent_code,
                )
            )
        return tuple(snapshots)

    async def load_pipeline_nodes(
        self, *, pipeline_version_id: str
    ) -> tuple[PipelineNodeSpec, ...]:
        pipeline_uuid = UUID(pipeline_version_id)
        async with self._database.session() as session:
            return await _load_pipeline_nodes_in_session(
                session=session,
                pipeline_uuid=pipeline_uuid,
            )

    async def ensure_formal_analysis_run(
        self,
        *,
        message_id: str,
        pipeline_version_id: str,
        now: datetime,
    ) -> str:
        ensure_aware(now, field="now")
        msg_uuid = UUID(message_id)
        pipeline_uuid = UUID(pipeline_version_id)
        async with self._database.session() as session:
            async with session.begin():
                # A missing child row cannot be locked: serialize on the durable parent.
                message = await session.scalar(
                    select(MessageRecord).where(MessageRecord.id == msg_uuid).with_for_update()
                )
                if message is None:
                    raise DomainValidationError("message does not exist")
                input_revision = _message_input_revision(message)
                existing = await session.scalar(
                    select(AnalysisRunRecord)
                    .where(
                        AnalysisRunRecord.message_id == msg_uuid,
                        AnalysisRunRecord.analysis_pipeline_version_id == pipeline_uuid,
                        AnalysisRunRecord.run_mode == "formal",
                        func.coalesce(
                            AnalysisRunRecord.facts_snapshot["_pipeline_input_revision"].astext, ""
                        )
                        == input_revision,
                    )
                    .order_by(AnalysisRunRecord.created_at.desc(), AnalysisRunRecord.id.desc())
                )
                if existing is not None:
                    return str(existing.id)
                created = AnalysisRunRecord(
                    id=self._id_factory(),
                    message_id=msg_uuid,
                    analysis_pipeline_version_id=pipeline_uuid,
                    run_mode="formal",
                    status="running",
                    facts_snapshot={"_pipeline_input_revision": input_revision},
                    started_at=now,
                )
                session.add(created)
                return str(created.id)

    async def load_analysis_dag_snapshot(
        self,
        *,
        analysis_run_id: str,
    ) -> AnalysisRunDagSnapshot | None:
        run_uuid = UUID(analysis_run_id)
        async with self._database.session() as session:
            async with session.begin():
                # Match worker lock order: existing run first, then its message.
                # ensure_formal locks only the parent (its existing-run lookup is read-only).
                run = await session.scalar(
                    select(AnalysisRunRecord)
                    .where(AnalysisRunRecord.id == run_uuid)
                    .with_for_update()
                )
                if run is None:
                    return None
                msg = await session.scalar(
                    select(MessageRecord)
                    .where(MessageRecord.id == run.message_id)
                    .with_for_update()
                )
                if msg is None:
                    return None
                nodes = await _load_pipeline_nodes_in_session(
                    session=session,
                    pipeline_uuid=run.analysis_pipeline_version_id,
                )
                node_key_by_id = {UUID(node.node_id): node.node_key for node in nodes}

                stage_runs = tuple(
                    await session.scalars(
                        select(StageRunRecord)
                        .where(StageRunRecord.analysis_run_id == run.id)
                        .order_by(StageRunRecord.created_at, StageRunRecord.id)
                    )
                )
                existing_by_node: dict[str, list[ExistingNodeTargetRun]] = defaultdict(list)
                for sr in stage_runs:
                    if sr.pipeline_stage_node_id is None:
                        continue
                    node_key = node_key_by_id.get(sr.pipeline_stage_node_id)
                    if node_key is not None:
                        existing_by_node[node_key].append(
                            ExistingNodeTargetRun(
                                target_kind=sr.target_kind,
                                target_id=str(sr.target_id),
                                status=sr.status,
                            )
                        )

                assignments = tuple(
                    await session.scalars(
                        select(ModelLabelAssignmentRecord).where(
                            ModelLabelAssignmentRecord.analysis_run_id == run.id
                        )
                    )
                )
                facts = _build_analysis_facts(message=msg, assignments=assignments)
                if "_pipeline_input_revision" in (run.facts_snapshot or {}):
                    facts["_pipeline_input_revision"] = run.facts_snapshot[
                        "_pipeline_input_revision"
                    ]
                run.facts_snapshot = facts

                imgs = tuple(
                    await session.scalars(
                        select(ImageAssetRecord)
                        .where(ImageAssetRecord.message_id == msg.id)
                        .order_by(ImageAssetRecord.id)
                    )
                )
                vids = tuple(
                    await session.scalars(
                        select(VideoAssetRecord)
                        .where(VideoAssetRecord.message_id == msg.id)
                        .order_by(VideoAssetRecord.id)
                    )
                )
                media = MessageMediaReadiness(
                    tuple(
                        [
                            MediaAssetTarget("image_asset", str(img.id), img.archive_state)
                            for img in imgs
                        ]
                        + [
                            MediaAssetTarget("video_asset", str(vid.id), vid.archive_state)
                            for vid in vids
                        ]
                    )
                )

                return AnalysisRunDagSnapshot(
                    analysis_run_id=str(run.id),
                    message_id=str(msg.id),
                    pipeline_version_id=str(run.analysis_pipeline_version_id),
                    status=run.status,
                    blocked_from_analysis=msg.blocked_from_analysis,
                    nodes=nodes,
                    existing_runs_by_node={
                        key: tuple(items) for key, items in existing_by_node.items()
                    },
                    facts=facts,
                    media=media,
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
        ensure_aware(now, field="now")
        run_uuid = UUID(analysis_run_id)
        msg_uuid = UUID(message_id)

        scheduled_count = 0

        async with self._database.session() as session:
            async with session.begin():
                run = await session.scalar(
                    select(AnalysisRunRecord)
                    .where(AnalysisRunRecord.id == run_uuid)
                    .with_for_update()
                )
                msg = await session.scalar(
                    select(MessageRecord).where(MessageRecord.id == msg_uuid).with_for_update()
                )
                if run is None or msg is None or msg.blocked_from_analysis:
                    return 0, 0, bool(msg and msg.blocked_from_analysis)

                for item in planned_targets:
                    node_uuid = UUID(item.node_id)
                    stage_uuid = UUID(item.stage_version_id)
                    target_uuid = UUID(item.target_id)

                    existing = await session.scalar(
                        select(StageRunRecord.id).where(
                            StageRunRecord.analysis_run_id == run_uuid,
                            StageRunRecord.pipeline_stage_node_id == node_uuid,
                            StageRunRecord.analysis_stage_template_version_id == stage_uuid,
                            StageRunRecord.target_id == target_uuid,
                        )
                    )
                    if existing is not None:
                        continue

                    img_id = target_uuid if item.target_kind == "image_asset" else None
                    vid_id = target_uuid if item.target_kind == "video_asset" else None

                    if item.action is PipelineNodeActionKind.SKIP_CONDITION:
                        session.add(
                            StageRunRecord(
                                id=self._id_factory(),
                                analysis_run_id=run_uuid,
                                pipeline_stage_node_id=node_uuid,
                                analysis_stage_template_version_id=stage_uuid,
                                message_id=msg_uuid,
                                target_scope=item.target_scope.value,
                                target_kind=item.target_kind,
                                target_id=target_uuid,
                                image_asset_id=img_id,
                                video_asset_id=vid_id,
                                status="skipped_condition",
                                attempt_count=0,
                                max_attempts=item.max_attempts,
                                cache_policy=item.cache_policy.value,
                                completed_at=now,
                            )
                        )
                        continue

                    # The worker prepares the current Manifest and complete semantic key.
                    # Preserve cache_policy; do not copy a merely visually similar donor.
                    stage_run_id = self._id_factory()
                    session.add(
                        StageRunRecord(
                            id=stage_run_id,
                            analysis_run_id=run_uuid,
                            pipeline_stage_node_id=node_uuid,
                            analysis_stage_template_version_id=stage_uuid,
                            message_id=msg_uuid,
                            target_scope=item.target_scope.value,
                            target_kind=item.target_kind,
                            target_id=target_uuid,
                            image_asset_id=img_id,
                            video_asset_id=vid_id,
                            status="pending",
                            attempt_count=0,
                            max_attempts=item.max_attempts,
                            cache_policy=item.cache_policy.value,
                        )
                    )
                    await _upsert_pending_wakeup(
                        session=session,
                        wakeup_id=self._id_factory(),
                        queue=ANALYSIS_QUEUE,
                        entity_id=stage_run_id,
                        now=now,
                    )
                    scheduled_count += 1

                if scheduled_count > 0 or run_status == "running":
                    run.status = "running"
                    run.started_at = run.started_at or now
                    run.completed_at = None
                run.updated_at = now

        return scheduled_count, 0, False

    async def set_analysis_run_status(
        self,
        *,
        analysis_run_id: str,
        status: str,
        now: datetime,
    ) -> None:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                run = await session.scalar(
                    select(AnalysisRunRecord)
                    .where(AnalysisRunRecord.id == UUID(analysis_run_id))
                    .with_for_update()
                )
                if run is None:
                    return
                run.started_at = run.started_at or now
                run.status = status
                if status in _TERMINAL_RUN_STATUSES:
                    run.completed_at = run.completed_at or now
                    if status == "failed" and not run.last_error_code:
                        run.last_error_code = "ANALYSIS_STAGE_FAILED"
                        run.last_error_type = "AnalysisStageFailed"
                else:
                    run.completed_at = None
                run.updated_at = now

    async def ensure_media_archive_wakeups(
        self,
        *,
        message_id: str,
        now: datetime,
    ) -> int:
        ensure_aware(now, field="now")
        msg_uuid = UUID(message_id)
        created = 0
        async with self._database.session() as session:
            async with session.begin():
                image_ids = tuple(
                    await session.scalars(
                        select(ImageAssetRecord.id).where(
                            ImageAssetRecord.message_id == msg_uuid,
                            ImageAssetRecord.archive_state == "pending",
                            ImageAssetRecord.source_telegram_message_id.is_not(None),
                        )
                    )
                )
                for img_id in image_ids:
                    if await _upsert_pending_wakeup(
                        session=session,
                        wakeup_id=self._id_factory(),
                        queue=IMAGE_ARCHIVE_QUEUE,
                        entity_id=img_id,
                        now=now,
                    ):
                        created += 1

                video_ids = tuple(
                    await session.scalars(
                        select(VideoAssetRecord.id).where(
                            VideoAssetRecord.message_id == msg_uuid,
                            VideoAssetRecord.archive_state == "pending",
                            VideoAssetRecord.source_telegram_message_id.is_not(None),
                        )
                    )
                )
                for vid_id in video_ids:
                    if await _upsert_pending_wakeup(
                        session=session,
                        wakeup_id=self._id_factory(),
                        queue=VIDEO_ARCHIVE_QUEUE,
                        entity_id=vid_id,
                        now=now,
                    ):
                        created += 1
        return created

    async def cancel_pending_media_wakeups(
        self,
        *,
        message_id: str,
        now: datetime,
    ) -> int:
        ensure_aware(now, field="now")
        msg_uuid = UUID(message_id)
        async with self._database.session() as session:
            async with session.begin():
                image_ids = tuple(
                    await session.scalars(
                        select(ImageAssetRecord.id).where(
                            ImageAssetRecord.message_id == msg_uuid,
                            ImageAssetRecord.archive_state == "pending",
                        )
                    )
                )
                video_ids = tuple(
                    await session.scalars(
                        select(VideoAssetRecord.id).where(
                            VideoAssetRecord.message_id == msg_uuid,
                            VideoAssetRecord.archive_state == "pending",
                        )
                    )
                )
                cancelled = 0
                if image_ids:
                    res = await session.execute(
                        update(DurableWakeupRecord)
                        .where(
                            DurableWakeupRecord.queue == IMAGE_ARCHIVE_QUEUE,
                            DurableWakeupRecord.entity_id.in_(image_ids),
                            DurableWakeupRecord.status == "pending",
                        )
                        .values(
                            status="cancelled",
                            lease_token=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                    cancelled += int(res.rowcount or 0)
                if video_ids:
                    res = await session.execute(
                        update(DurableWakeupRecord)
                        .where(
                            DurableWakeupRecord.queue == VIDEO_ARCHIVE_QUEUE,
                            DurableWakeupRecord.entity_id.in_(video_ids),
                            DurableWakeupRecord.status == "pending",
                        )
                        .values(
                            status="cancelled",
                            lease_token=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                    cancelled += int(res.rowcount or 0)
                return cancelled

    async def load_visual_duplicate_candidates(
        self,
        *,
        message_id: str,
        published_at: datetime,
        visual_fingerprint: str | None,
        limit: int = 500,
    ) -> tuple[VisualCandidate, ...]:
        msg_uuid = UUID(message_id)
        async with self._database.session() as session:
            rows_by_id: dict[UUID, MessageRecord] = {}
            if visual_fingerprint is not None:
                for row in await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.id != msg_uuid,
                        MessageRecord.visual_fingerprint == visual_fingerprint,
                        or_(
                            MessageRecord.published_at < published_at,
                            and_(
                                MessageRecord.published_at == published_at,
                                MessageRecord.id < msg_uuid,
                            ),
                        ),
                        MessageRecord.duplicate_status == "unique",
                        MessageRecord.processing_status != "failed",
                    )
                    .order_by(MessageRecord.published_at, MessageRecord.id)
                    .limit(limit)
                ):
                    rows_by_id[row.id] = row

            for row in await session.scalars(
                select(MessageRecord)
                .where(
                    MessageRecord.id != msg_uuid,
                    MessageRecord.media_count > 0,
                    or_(
                        MessageRecord.published_at < published_at,
                        and_(
                            MessageRecord.published_at == published_at, MessageRecord.id < msg_uuid
                        ),
                    ),
                    MessageRecord.duplicate_status == "unique",
                    MessageRecord.processing_status != "failed",
                )
                .order_by(MessageRecord.published_at.desc(), MessageRecord.id.desc())
                .limit(limit)
            ):
                rows_by_id.setdefault(row.id, row)

            if not rows_by_id:
                return ()

            cand_ids = tuple(rows_by_id)
            img_phashes_by_msg: dict[UUID, list[str | None]] = defaultdict(list)
            for img in await session.scalars(
                select(ImageAssetRecord).where(ImageAssetRecord.message_id.in_(cand_ids))
            ):
                img_phashes_by_msg[img.message_id].append(img.perceptual_hash or img.source_phash)

            vids_by_msg: dict[UUID, list[VideoAssetRecord]] = defaultdict(list)
            vid_ids: list[UUID] = []
            for vid in await session.scalars(
                select(VideoAssetRecord).where(VideoAssetRecord.message_id.in_(cand_ids))
            ):
                vids_by_msg[vid.message_id].append(vid)
                vid_ids.append(vid.id)

            cover_by_vid: dict[UUID, str | None] = {}
            if vid_ids:
                for frame in await session.scalars(
                    select(VideoFrameRecord)
                    .where(
                        VideoFrameRecord.video_asset_id.in_(tuple(vid_ids)),
                        VideoFrameRecord.frame_role == "cover",
                    )
                    .order_by(VideoFrameRecord.frame_index)
                ):
                    cover_by_vid.setdefault(frame.video_asset_id, frame.perceptual_hash)

            candidates: list[VisualCandidate] = []
            for row_id, row in rows_by_id.items():
                fp, tokens = compute_archived_visual_identity(
                    image_phashes=img_phashes_by_msg.get(row_id, ()),
                    video_cover_phashes=(
                        cover_by_vid.get(vid.id) or vid.source_cover_phash
                        for vid in vids_by_msg.get(row_id, ())
                    ),
                )
                effective_fp = fp or row.visual_fingerprint
                if effective_fp is None and not tokens:
                    continue
                candidates.append(
                    VisualCandidate(
                        message_id=str(row.id),
                        visual_fingerprint=effective_fp,
                        visual_tokens=tokens,
                        published_at=row.published_at,
                    )
                )
            return tuple(candidates)

    async def record_visual_deduplication(
        self,
        *,
        message_id: str,
        visual_fingerprint: str | None,
        match: DuplicateMatchResult,
        now: datetime,
    ) -> None:
        ensure_aware(now, field="now")
        msg_uuid = UUID(message_id)
        async with self._database.session() as session:
            async with session.begin():
                msg = await session.scalar(
                    select(MessageRecord).where(MessageRecord.id == msg_uuid).with_for_update()
                )
                if msg is None:
                    return
                if visual_fingerprint is not None:
                    msg.visual_fingerprint = visual_fingerprint
                msg.duplicate_status = match.status.value
                if match.canonical_message_id is not None and match.hamming_distance is not None:
                    canonical_uuid = UUID(match.canonical_message_id)
                    msg.canonical_message_id = canonical_uuid
                    msg.duplicate_distance = match.hamming_distance
                    assert match.match_kind is not None
                    await session.execute(
                        postgresql_insert(DuplicateMatchRecord)
                        .values(
                            id=self._id_factory(),
                            message_id=msg_uuid,
                            canonical_message_id=canonical_uuid,
                            match_kind=match.match_kind.value,
                            hamming_distance=match.hamming_distance,
                            duplicate_policy=match.duplicate_policy.value,
                            created_at=now,
                        )
                        .on_conflict_do_update(
                            constraint="uq_duplicate_matches_message",
                            set_={
                                "canonical_message_id": canonical_uuid,
                                "match_kind": match.match_kind.value,
                                "hamming_distance": match.hamming_distance,
                                "duplicate_policy": match.duplicate_policy.value,
                            },
                        )
                    )
                else:
                    msg.canonical_message_id = None
                    msg.duplicate_distance = None
                msg.updated_at = now

    async def mark_message_processing(self, *, message_id: str, now: datetime) -> None:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(
                    update(MessageRecord)
                    .where(
                        MessageRecord.id == UUID(message_id),
                        MessageRecord.processing_status.in_(("ingested", "processed")),
                    )
                    .values(
                        processing_status="processing",
                        duplicate_status="unchecked",
                        canonical_message_id=None,
                        duplicate_distance=None,
                        terminal_reason=None,
                        failure_stage=None,
                        last_error_code=None,
                        updated_at=now,
                    )
                )

    async def mark_message_processed(
        self,
        *,
        message_id: str,
        terminal_reason: str,
        now: datetime,
    ) -> None:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(
                    update(MessageRecord)
                    .where(MessageRecord.id == UUID(message_id))
                    .values(
                        processing_status="processed",
                        terminal_reason=terminal_reason,
                        failure_stage=None,
                        last_error_code=None,
                        updated_at=now,
                    )
                )

    async def mark_message_failed(
        self,
        *,
        message_id: str,
        failure_stage: str,
        error_code: str,
        now: datetime,
    ) -> None:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(
                    update(MessageRecord)
                    .where(MessageRecord.id == UUID(message_id))
                    .values(
                        processing_status="failed",
                        terminal_reason=None,
                        failure_stage=failure_stage,
                        last_error_code=error_code[:128],
                        updated_at=now,
                    )
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
        ensure_aware(now, field="now")
        _ = progress_snapshot
        exec_uuid = UUID(execution_id)
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(RangeExecutionRecord)
                    .where(
                        RangeExecutionRecord.id == exec_uuid,
                        RangeExecutionRecord.status == "awaiting_pipeline",
                    )
                    .with_for_update()
                )
                if row is None:
                    return False, False
                if (
                    not row.watermark_message_id
                    <= next_watermark_message_id
                    <= row.to_message_id_inclusive
                ):
                    return False, False

                processing_range = await session.scalar(
                    select(ProcessingRangeRecord)
                    .where(ProcessingRangeRecord.id == row.processing_range_id)
                    .with_for_update()
                )
                if processing_range is None:
                    return False, False

                watermark_advanced = next_watermark_message_id > row.watermark_message_id
                row.watermark_message_id = next_watermark_message_id
                row.updated_at = now

                if (
                    processing_range.processing_watermark_message_id is None
                    or next_watermark_message_id > processing_range.processing_watermark_message_id
                ):
                    processing_range.processing_watermark_message_id = next_watermark_message_id
                    processing_range.updated_at = now

                completed = False
                if window_completed and next_watermark_message_id == row.to_message_id_inclusive:
                    row.status = "completed"
                    row.completed_at = now
                    processing_range.processing_watermark_message_id = row.to_message_id_inclusive
                    processing_range.active_high_watermark_message_id = None
                    processing_range.status = (
                        "completed"
                        if processing_range.end_mode == "fixed"
                        and row.to_message_id_inclusive
                        == processing_range.resolved_fixed_end_message_id
                        else "active"
                    )
                    processing_range.updated_at = now
                    completed = True

                return watermark_advanced, completed

    async def repair_wakeups(
        self,
        *,
        now: datetime,
        limit: int = 500,
    ) -> WakeupRepairReport:
        ensure_aware(now, field="now")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise DomainValidationError("limit must be a positive integer")

        async with self._database.session() as session:
            async with session.begin():
                exec_ids = tuple(
                    await session.scalars(
                        select(RangeExecutionRecord.id)
                        .where(
                            or_(
                                RangeExecutionRecord.status == "pending",
                                and_(
                                    RangeExecutionRecord.status == "retry_wait",
                                    RangeExecutionRecord.next_retry_at <= now,
                                ),
                                and_(
                                    RangeExecutionRecord.status == "running",
                                    RangeExecutionRecord.lease_expires_at <= now,
                                ),
                            )
                        )
                        .order_by(RangeExecutionRecord.created_at, RangeExecutionRecord.id)
                        .limit(limit)
                    )
                )
                repaired_exec = 0
                for entity_id in exec_ids:
                    if await _repair_wakeup_if_unleased(
                        session=session,
                        wakeup_id=self._id_factory(),
                        queue=RANGE_EXECUTION_QUEUE,
                        entity_id=entity_id,
                        now=now,
                    ):
                        repaired_exec += 1

                img_ids = tuple(
                    await session.scalars(
                        select(ImageAssetRecord.id)
                        .join(MessageRecord, MessageRecord.id == ImageAssetRecord.message_id)
                        .where(
                            ImageAssetRecord.source_telegram_message_id.is_not(None),
                            MessageRecord.blocked_from_analysis.is_(False),
                            or_(
                                and_(
                                    ImageAssetRecord.archive_state == "retry_wait",
                                    ImageAssetRecord.archive_next_retry_at <= now,
                                ),
                                and_(
                                    ImageAssetRecord.archive_state == "processing",
                                    ImageAssetRecord.archive_lease_expires_at <= now,
                                ),
                            ),
                        )
                        .order_by(ImageAssetRecord.id)
                        .limit(limit)
                    )
                )
                repaired_img = 0
                for entity_id in img_ids:
                    if await _repair_wakeup_if_unleased(
                        session=session,
                        wakeup_id=self._id_factory(),
                        queue=IMAGE_ARCHIVE_QUEUE,
                        entity_id=entity_id,
                        now=now,
                    ):
                        repaired_img += 1

                vid_ids = tuple(
                    await session.scalars(
                        select(VideoAssetRecord.id)
                        .join(MessageRecord, MessageRecord.id == VideoAssetRecord.message_id)
                        .where(
                            VideoAssetRecord.source_telegram_message_id.is_not(None),
                            MessageRecord.blocked_from_analysis.is_(False),
                            or_(
                                and_(
                                    VideoAssetRecord.archive_state == "retry_wait",
                                    VideoAssetRecord.archive_next_retry_at <= now,
                                ),
                                and_(
                                    VideoAssetRecord.archive_state == "processing",
                                    VideoAssetRecord.archive_lease_expires_at <= now,
                                ),
                            ),
                        )
                        .order_by(VideoAssetRecord.id)
                        .limit(limit)
                    )
                )
                repaired_vid = 0
                for entity_id in vid_ids:
                    if await _repair_wakeup_if_unleased(
                        session=session,
                        wakeup_id=self._id_factory(),
                        queue=VIDEO_ARCHIVE_QUEUE,
                        entity_id=entity_id,
                        now=now,
                    ):
                        repaired_vid += 1

                sr_ids = tuple(
                    await session.scalars(
                        select(StageRunRecord.id)
                        .join(
                            AnalysisRunRecord,
                            AnalysisRunRecord.id == StageRunRecord.analysis_run_id,
                        )
                        .where(
                            AnalysisRunRecord.status.not_in(_TERMINAL_RUN_STATUSES),
                            or_(
                                StageRunRecord.status == "pending",
                                and_(
                                    StageRunRecord.status == "retry_wait",
                                    StageRunRecord.next_retry_at <= now,
                                ),
                                and_(
                                    StageRunRecord.status == "running",
                                    StageRunRecord.lease_expires_at <= now,
                                ),
                            ),
                        )
                        .order_by(StageRunRecord.created_at, StageRunRecord.id)
                        .limit(limit)
                    )
                )
                repaired_sr = 0
                for entity_id in sr_ids:
                    if await _repair_wakeup_if_unleased(
                        session=session,
                        wakeup_id=self._id_factory(),
                        queue=ANALYSIS_QUEUE,
                        entity_id=entity_id,
                        now=now,
                    ):
                        repaired_sr += 1

        return WakeupRepairReport(
            repaired_range_executions=repaired_exec,
            repaired_image_archives=repaired_img,
            repaired_video_archives=repaired_vid,
            repaired_analysis_stages=repaired_sr,
        )


async def _load_pipeline_nodes_in_session(
    *,
    session: AsyncSession,
    pipeline_uuid: UUID,
) -> tuple[PipelineNodeSpec, ...]:
    rows = tuple(
        await session.execute(
            select(PipelineStageNodeRecord, AnalysisStageTemplateVersionRecord)
            .join(
                AnalysisStageTemplateVersionRecord,
                AnalysisStageTemplateVersionRecord.id
                == PipelineStageNodeRecord.analysis_stage_template_version_id,
            )
            .where(PipelineStageNodeRecord.analysis_pipeline_version_id == pipeline_uuid)
            .order_by(PipelineStageNodeRecord.output_order, PipelineStageNodeRecord.id)
        )
    )
    specs: list[PipelineNodeSpec] = []
    for node_row, stage_row in rows:
        input_policy = stage_row.input_policy or {}
        requires_media = bool(input_policy.get("media", False))
        overrides = node_row.parameter_overrides or {}
        unsupported = set(overrides) - _ALLOWED_STAGE_OVERRIDES
        if unsupported:
            raise DomainValidationError(
                f"unsupported pipeline stage overrides: {sorted(unsupported)}"
            )
        retry_override = overrides.get("retry_policy", {})
        if not isinstance(retry_override, dict):
            raise DomainValidationError("pipeline retry_policy override must be an object")
        retry_policy = {**(stage_row.retry_policy or {}), **retry_override}
        try:
            max_attempts = StageRetryPolicy(**retry_policy).max_attempts
        except TypeError as exc:
            raise DomainValidationError("invalid pipeline retry_policy override") from exc
        scope = LabelScope(stage_row.target_scope)
        specs.append(
            PipelineNodeSpec(
                node_id=str(node_row.id),
                node_key=node_row.node_key,
                stage_version_id=str(stage_row.id),
                target_scope=scope,
                input_requires_media=requires_media,
                is_text_only_prescreen=(scope is LabelScope.GLOBAL and not requires_media),
                cache_policy=CachePolicy(stage_row.cache_policy),
                max_attempts=max_attempts,
                depends_on=tuple(node_row.depends_on or ()),
                run_if=node_row.run_if,
            )
        )
    return tuple(specs)


def _build_analysis_facts(
    *,
    message: MessageRecord,
    assignments: tuple[ModelLabelAssignmentRecord, ...],
) -> dict[str, Any]:
    global_scores: dict[str, float] = {}
    media_scores: dict[str, float] = {}
    global_label_entries: dict[str, dict[str, Any]] = {}
    media_label_entries: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in assignments:
        score = float(item.score)
        entry = {"score": score, "activated": bool(item.activated)}
        if item.target_scope == "global":
            if item.label_key not in global_scores or score >= global_scores[item.label_key]:
                global_scores[item.label_key] = score
                global_label_entries[item.label_key] = entry
        elif item.target_scope == "media":
            media_scores[item.label_key] = max(media_scores.get(item.label_key, 0.0), score)
            target_labels = media_label_entries[str(item.target_id)]
            existing_entry = target_labels.get(item.label_key)
            if existing_entry is None or score >= float(existing_entry["score"]):
                target_labels[item.label_key] = entry

    sorted_global = {k: global_scores[k] for k in sorted(global_scores)}
    sorted_media = {k: media_scores[k] for k in sorted(media_scores)}
    sorted_global_labels = {k: global_label_entries[k] for k in sorted(global_label_entries)}
    sorted_media_labels = {
        tid: {"labels": {k: labels[k] for k in sorted(labels)}}
        for tid, labels in sorted(media_label_entries.items())
    }

    facts: dict[str, Any] = {
        "global": sorted_global,
        "media": sorted_media,
        "effective": {
            "global": dict(sorted_global),
            "media": dict(sorted_media),
        },
        "labels": {
            "model": {
                "global": sorted_global_labels,
                "media": sorted_media_labels,
            },
            "effective": {
                "global": dict(sorted_global_labels),
                "media": dict(sorted_media_labels),
            },
        },
        "message": {
            "blocked_from_analysis": bool(message.blocked_from_analysis),
            "media_count": int(message.media_count),
            "text_length": len(message.original_text or ""),
            "has_text": bool((message.original_text or "").strip()),
        },
        "duplicate": {
            "status": getattr(message, "duplicate_status", None) or "unchecked",
        },
    }
    return facts


async def _find_canonical_visual_stage_source(
    *,
    session: AsyncSession,
    stage_version_id: UUID,
    canonical_message_id: UUID,
    target_kind: str,
    current_target_id: UUID,
) -> StageRunRecord | None:
    """A donor cannot be proven safe without the current prepared input Manifest.

    Visual dedup equivalence is not semantic-cache equivalence. A video cover is not
    its sampled frame set; previous results/source context may also differ. Fail closed
    and schedule the normal worker, whose AnalysisOrchestrator compares the full key
    with cache_policy intact. Kept as a compatibility hook, never a bypass.
    """
    return None


def _message_input_revision(message: MessageRecord) -> str:
    """A formal run owns one component set, not every future album extension."""
    return ",".join(str(value) for value in sorted(message.telegram_message_ids))


async def _upsert_pending_wakeup(
    *,
    session: AsyncSession,
    wakeup_id: UUID,
    queue: str,
    entity_id: UUID,
    now: datetime,
) -> bool:
    inserted = await session.scalar(
        postgresql_insert(DurableWakeupRecord)
        .values(
            id=wakeup_id,
            queue=queue,
            entity_id=entity_id,
            status="pending",
            next_attempt_at=now,
            lease_token=None,
            lease_expires_at=None,
            dispatch_attempts=0,
            completed_at=None,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[DurableWakeupRecord.queue, DurableWakeupRecord.entity_id],
            set_={
                "status": "pending",
                "next_attempt_at": now,
                "lease_token": None,
                "lease_expires_at": None,
                "completed_at": None,
                "updated_at": now,
            },
            where=DurableWakeupRecord.status.in_(("completed", "cancelled")),
        )
        .returning(DurableWakeupRecord.id)
    )
    return inserted is not None


async def _repair_wakeup_if_unleased(
    *,
    session: AsyncSession,
    wakeup_id: UUID,
    queue: str,
    entity_id: UUID,
    now: datetime,
) -> bool:
    repaired = await session.scalar(
        postgresql_insert(DurableWakeupRecord)
        .values(
            id=wakeup_id,
            queue=queue,
            entity_id=entity_id,
            status="pending",
            next_attempt_at=now,
            lease_token=None,
            lease_expires_at=None,
            dispatch_attempts=0,
            completed_at=None,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[DurableWakeupRecord.queue, DurableWakeupRecord.entity_id],
            set_={
                "status": "pending",
                "next_attempt_at": now,
                "lease_token": None,
                "lease_expires_at": None,
                "completed_at": None,
                "updated_at": now,
            },
            where=or_(
                DurableWakeupRecord.status.in_(("completed", "cancelled")),
                and_(
                    DurableWakeupRecord.status == "leased",
                    DurableWakeupRecord.lease_expires_at <= now,
                ),
            ),
        )
        .returning(DurableWakeupRecord.id)
    )
    return repaired is not None
