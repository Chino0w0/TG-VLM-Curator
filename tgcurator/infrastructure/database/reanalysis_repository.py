from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgcurator.domain.analysis import (
    AnalysisRunStatus,
    ExistingNodeTargetRun,
    MediaAssetTarget,
    MessageMediaReadiness,
    PipelineNodeActionKind,
    plan_pipeline_progression,
)
from tgcurator.shared import DomainValidationError, ensure_aware

from .models import (
    AnalysisRunRecord,
    DurableWakeupRecord,
    ImageAssetRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    StageRunRecord,
    VideoAssetRecord,
)
from .session import AsyncDatabase


async def advance_reanalysis_in_session(
    *, session: AsyncSession, run: AnalysisRunRecord, now: datetime
) -> tuple[str, ...]:
    """Advance only this reanalysis DAG under the caller's run-row lock.

    No routing, publication, watermark, or historical formal-run mutation occurs here.
    Both initial API creation and stage settlement use the same transaction-local plan.
    """
    from .pipeline_repository import _build_analysis_facts, _load_pipeline_nodes_in_session

    ensure_aware(now, field="now")
    if run.run_mode != "reanalysis":
        raise DomainValidationError("reanalysis progression requires a reanalysis run")
    message = await session.get(MessageRecord, run.message_id)
    if message is None:
        raise DomainValidationError("reanalysis message does not exist")
    nodes = await _load_pipeline_nodes_in_session(
        session=session, pipeline_uuid=run.analysis_pipeline_version_id
    )
    node_keys = {UUID(node.node_id): node.node_key for node in nodes}
    stages = tuple(
        await session.scalars(
            select(StageRunRecord)
            .where(StageRunRecord.analysis_run_id == run.id)
            .order_by(StageRunRecord.created_at, StageRunRecord.id)
            .with_for_update()
        )
    )
    existing: dict[str, list[ExistingNodeTargetRun]] = defaultdict(list)
    for stage in stages:
        key = node_keys.get(stage.pipeline_stage_node_id)
        if key is not None:
            existing[key].append(
                ExistingNodeTargetRun(stage.target_kind, str(stage.target_id), stage.status)
            )
    assignments = tuple(
        await session.scalars(
            select(ModelLabelAssignmentRecord).where(
                ModelLabelAssignmentRecord.analysis_run_id == run.id
            )
        )
    )
    facts = _build_analysis_facts(message=message, assignments=assignments)
    # A historical formal Negative Gate must not prevent an explicit new analysis.
    blocked = run.status == AnalysisRunStatus.BLOCKED_NEGATIVE_GATE.value
    facts["message"]["blocked_from_analysis"] = blocked
    images = tuple(
        await session.scalars(
            select(ImageAssetRecord)
            .where(ImageAssetRecord.message_id == run.message_id)
            .order_by(ImageAssetRecord.id)
        )
    )
    videos = tuple(
        await session.scalars(
            select(VideoAssetRecord)
            .where(VideoAssetRecord.message_id == run.message_id)
            .order_by(VideoAssetRecord.id)
        )
    )
    media = MessageMediaReadiness(
        tuple(
            [MediaAssetTarget("image_asset", str(a.id), a.archive_state) for a in images]
            + [MediaAssetTarget("video_asset", str(a.id), a.archive_state) for a in videos]
        )
    )
    plan = plan_pipeline_progression(
        message_id=str(run.message_id),
        nodes=nodes,
        existing_runs_by_node={key: tuple(values) for key, values in existing.items()},
        media=media,
        facts=facts,
        blocked_from_analysis=blocked,
    )
    new_stage_ids: list[str] = []
    wakeups: list[UUID] = []
    for target in plan.planned_targets:
        stage_id = uuid4()
        target_id = UUID(target.target_id)
        skipped = target.action is PipelineNodeActionKind.SKIP_CONDITION
        session.add(
            StageRunRecord(
                id=stage_id,
                analysis_run_id=run.id,
                pipeline_stage_node_id=UUID(target.node_id),
                analysis_stage_template_version_id=UUID(target.stage_version_id),
                message_id=run.message_id,
                target_scope=target.target_scope.value,
                target_kind=target.target_kind,
                target_id=target_id,
                image_asset_id=target_id if target.target_kind == "image_asset" else None,
                video_asset_id=target_id if target.target_kind == "video_asset" else None,
                status="skipped_condition" if skipped else "pending",
                attempt_count=0,
                max_attempts=target.max_attempts,
                cache_policy=target.cache_policy.value,
                completed_at=now if skipped else None,
                created_at=now,
                updated_at=now,
            )
        )
        new_stage_ids.append(str(stage_id))
        if not skipped:
            wakeups.append(stage_id)
    # Materialize stages before their reconstructable transport signals.
    await session.flush()
    for stage_id in wakeups:
        session.add(
            DurableWakeupRecord(
                id=uuid4(),
                queue="analysis",
                entity_id=stage_id,
                status="pending",
                next_attempt_at=now,
                dispatch_attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
    run.status = plan.run_status.value
    run.started_at = run.started_at or now
    run.completed_at = None if run.status == "running" else now
    run.facts_snapshot = facts
    run.updated_at = now
    if run.status == "failed":
        failed = next((stage for stage in stages if stage.status == "failed"), None)
        run.last_error_code = (
            failed.last_error_code if failed and failed.last_error_code else "MEDIA_UNAVAILABLE"
        )
        run.last_error_type = (
            failed.last_error_type if failed and failed.last_error_type else "ReanalysisMediaFailed"
        )
    else:
        run.last_error_code = None
        run.last_error_type = None
    await session.flush()
    return tuple(new_stage_ids)


class SqlAlchemyReanalysisRepository:
    """Recover independent reanalysis DAGs without a live ProcessingRange."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def drive_active(self, *, now: datetime, limit: int = 100) -> int:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                runs = tuple(
                    await session.scalars(
                        select(AnalysisRunRecord)
                        .where(
                            AnalysisRunRecord.run_mode == "reanalysis",
                            AnalysisRunRecord.status.in_(("pending", "running")),
                        )
                        .order_by(AnalysisRunRecord.created_at, AnalysisRunRecord.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                count = 0
                for run in runs:
                    count += len(
                        await advance_reanalysis_in_session(session=session, run=run, now=now)
                    )
                return count
