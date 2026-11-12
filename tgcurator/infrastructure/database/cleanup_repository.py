from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, cast, exists, false, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import aliased

from tgcurator.application.ports.cleanup import (
    ArchiveCleanupDryRun,
    ArchiveCleanupEstimate,
    ArchiveCleanupFilter,
    ClaimedArchiveCleanupJob,
    ClaimedCleanupItem,
    CleanupAsset,
    CleanupBlob,
    ConfirmedArchiveCleanupJob,
)
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration

from .analysis_repository import sanitize_json
from .models import (
    AnalysisRunRecord,
    ArchiveCleanupDryRunRecord,
    ArchiveCleanupItemRecord,
    ArchiveCleanupJobRecord,
    AuditEvent,
    DurableWakeupRecord,
    ImageAssetRecord,
    LabelDefinitionVersionRecord,
    ManualLabelAssignmentRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    PublicationIntentRecord,
    StageRunRecord,
    VideoAssetRecord,
    VideoFrameRecord,
)
from .session import AsyncDatabase

CLEANUP_QUEUE = "maintenance"
CLEANUP_TASK_NAME = "tgcurator.archive_cleanup"
_ACTIVE_PUBLICATION_STATUSES = ("pending", "sending", "partial", "retry_wait")
_TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
_ACTIVE_WAKEUP_STATUSES = ("pending", "leased")


@dataclass(frozen=True, slots=True)
class _CleanupCandidate:
    asset_kind: str
    asset_id: UUID
    message_id: UUID
    previous_archive_size: int
    blob_snapshot: tuple[dict[str, object], ...]


def _latest_analysis_run_id():
    return (
        select(AnalysisRunRecord.id)
        .where(
            AnalysisRunRecord.message_id == MessageRecord.id,
            AnalysisRunRecord.run_mode.in_(("formal", "reanalysis")),
            AnalysisRunRecord.status.in_(("completed", "blocked_negative_gate")),
        )
        .order_by(AnalysisRunRecord.created_at.desc(), AnalysisRunRecord.id.desc())
        .limit(1)
        .correlate(MessageRecord)
        .scalar_subquery()
    )


def _label_target_predicate(record: Any, *, asset_id: Any):
    return or_(
        and_(record.target_scope == "global", record.target_id == MessageRecord.id),
        and_(record.target_scope == "media", record.target_id == asset_id),
    )


def _scope_predicate(record: Any, cleanup_filter: ArchiveCleanupFilter):
    if cleanup_filter.label_scopes:
        return record.target_scope.in_(cleanup_filter.label_scopes)
    return literal(True)


def _is_latest_manual(manual: Any):
    newer = aliased(ManualLabelAssignmentRecord)
    return ~exists(
        select(newer.id).where(
            newer.message_id == manual.message_id,
            newer.target_id == manual.target_id,
            newer.label_definition_version_id == manual.label_definition_version_id,
            or_(
                newer.created_at > manual.created_at,
                and_(newer.created_at == manual.created_at, newer.id > manual.id),
            ),
        )
    )


def _model_label_exists(*, asset_id: Any, cleanup_filter: ArchiveCleanupFilter):
    predicates = [
        ModelLabelAssignmentRecord.analysis_run_id == _latest_analysis_run_id(),
        ModelLabelAssignmentRecord.message_id == MessageRecord.id,
        ModelLabelAssignmentRecord.activated.is_(True),
        _label_target_predicate(ModelLabelAssignmentRecord, asset_id=asset_id),
        _scope_predicate(ModelLabelAssignmentRecord, cleanup_filter),
    ]
    if cleanup_filter.model_label_keys:
        predicates.append(ModelLabelAssignmentRecord.label_key.in_(cleanup_filter.model_label_keys))
    return exists(select(ModelLabelAssignmentRecord.id).where(*predicates))


def _manual_label_exists(*, asset_id: Any, cleanup_filter: ArchiveCleanupFilter):
    manual = aliased(ManualLabelAssignmentRecord)
    predicates = [
        manual.message_id == MessageRecord.id,
        manual.operation == "set",
        manual.activated.is_(True),
        _label_target_predicate(manual, asset_id=asset_id),
        _scope_predicate(manual, cleanup_filter),
        _is_latest_manual(manual),
    ]
    if cleanup_filter.manual_label_keys:
        predicates.append(manual.label_key.in_(cleanup_filter.manual_label_keys))
    return exists(select(manual.id).where(*predicates))


def _effective_label_exists(*, asset_id: Any, cleanup_filter: ArchiveCleanupFilter):
    manual = aliased(ManualLabelAssignmentRecord)
    manual_definition = aliased(LabelDefinitionVersionRecord)
    manual_predicates = [
        manual.message_id == MessageRecord.id,
        manual.operation == "set",
        manual.activated.is_(True),
        _label_target_predicate(manual, asset_id=asset_id),
        _scope_predicate(manual, cleanup_filter),
        _is_latest_manual(manual),
        manual_definition.id == manual.label_definition_version_id,
    ]
    if cleanup_filter.effective_label_keys:
        manual_predicates.append(manual.label_key.in_(cleanup_filter.effective_label_keys))
    if cleanup_filter.negative_label is not None:
        manual_predicates.append(manual_definition.negative.is_(cleanup_filter.negative_label))

    current_manual = aliased(ManualLabelAssignmentRecord)
    model_predicates = [
        ModelLabelAssignmentRecord.analysis_run_id == _latest_analysis_run_id(),
        ModelLabelAssignmentRecord.message_id == MessageRecord.id,
        ModelLabelAssignmentRecord.activated.is_(True),
        _label_target_predicate(ModelLabelAssignmentRecord, asset_id=asset_id),
        _scope_predicate(ModelLabelAssignmentRecord, cleanup_filter),
        ~exists(
            select(current_manual.id).where(
                current_manual.message_id == ModelLabelAssignmentRecord.message_id,
                current_manual.target_id == ModelLabelAssignmentRecord.target_id,
                current_manual.label_definition_version_id
                == ModelLabelAssignmentRecord.label_definition_version_id,
                current_manual.operation == "set",
                _is_latest_manual(current_manual),
            )
        ),
    ]
    if cleanup_filter.effective_label_keys:
        model_predicates.append(
            ModelLabelAssignmentRecord.label_key.in_(cleanup_filter.effective_label_keys)
        )
    if cleanup_filter.negative_label is not None:
        model_predicates.append(
            ModelLabelAssignmentRecord.negative.is_(cleanup_filter.negative_label)
        )
    return or_(
        exists(select(manual.id).select_from(manual, manual_definition).where(*manual_predicates)),
        exists(select(ModelLabelAssignmentRecord.id).where(*model_predicates)),
    )


def _duplicate_predicate(statuses: tuple[str, ...]):
    other = aliased(MessageRecord)
    earlier = aliased(MessageRecord)
    has_other = exists(
        select(other.id).where(
            other.visual_fingerprint == MessageRecord.visual_fingerprint,
            other.id != MessageRecord.id,
        )
    )
    has_earlier = exists(
        select(earlier.id).where(
            earlier.visual_fingerprint == MessageRecord.visual_fingerprint,
            or_(
                earlier.published_at < MessageRecord.published_at,
                and_(
                    earlier.published_at == MessageRecord.published_at,
                    earlier.id < MessageRecord.id,
                ),
            ),
        )
    )
    predicates = []
    if "unknown" in statuses:
        predicates.append(MessageRecord.visual_fingerprint.is_(None))
    if "unique" in statuses:
        predicates.append(and_(MessageRecord.visual_fingerprint.is_not(None), ~has_other))
    if "canonical" in statuses:
        predicates.append(
            and_(MessageRecord.visual_fingerprint.is_not(None), has_other, ~has_earlier)
        )
    if "duplicate" in statuses:
        predicates.append(and_(MessageRecord.visual_fingerprint.is_not(None), has_earlier))
    return or_(*predicates) if predicates else literal(True)


def _candidate_filters(*, asset: Any, asset_kind: str, cleanup_filter: ArchiveCleanupFilter):
    predicates: list[Any] = [asset.archive_state == "ready"]
    if cleanup_filter.media_types and asset_kind not in cleanup_filter.media_types:
        predicates.append(false())
    if cleanup_filter.archived_from is not None:
        predicates.append(asset.archive_ready_at >= cleanup_filter.archived_from)
    if cleanup_filter.archived_before is not None:
        predicates.append(asset.archive_ready_at < cleanup_filter.archived_before)
    if cleanup_filter.source_channel_ids:
        predicates.append(
            MessageRecord.source_channel_id.in_(
                tuple(UUID(value) for value in cleanup_filter.source_channel_ids)
            )
        )
    if cleanup_filter.message_published_from is not None:
        predicates.append(MessageRecord.published_at >= cleanup_filter.message_published_from)
    if cleanup_filter.message_published_before is not None:
        predicates.append(MessageRecord.published_at < cleanup_filter.message_published_before)
    if cleanup_filter.review_statuses:
        predicates.append(MessageRecord.review_status.in_(cleanup_filter.review_statuses))
    if cleanup_filter.processing_statuses:
        predicates.append(MessageRecord.processing_status.in_(cleanup_filter.processing_statuses))
    if cleanup_filter.blocked_from_analysis is not None:
        predicates.append(
            MessageRecord.blocked_from_analysis.is_(cleanup_filter.blocked_from_analysis)
        )
    if cleanup_filter.duplicate_statuses:
        predicates.append(_duplicate_predicate(cleanup_filter.duplicate_statuses))
    if cleanup_filter.model_label_keys:
        predicates.append(_model_label_exists(asset_id=asset.id, cleanup_filter=cleanup_filter))
    if cleanup_filter.manual_label_keys:
        predicates.append(_manual_label_exists(asset_id=asset.id, cleanup_filter=cleanup_filter))
    if (
        cleanup_filter.effective_label_keys
        or cleanup_filter.label_scopes
        or cleanup_filter.negative_label is not None
    ):
        predicates.append(_effective_label_exists(asset_id=asset.id, cleanup_filter=cleanup_filter))
    return tuple(predicates)


def cleanup_image_candidates_statement(*, cleanup_filter: ArchiveCleanupFilter):
    """Select READY image blobs matching the frozen cleanup filter."""

    blob_snapshot = func.jsonb_build_array(
        func.jsonb_build_object(
            "storage_backend",
            ImageAssetRecord.storage_backend,
            "storage_key",
            ImageAssetRecord.storage_key,
            "size_bytes",
            ImageAssetRecord.archive_size_bytes,
        )
    )
    return (
        select(
            literal("image").label("asset_kind"),
            ImageAssetRecord.id.label("asset_id"),
            ImageAssetRecord.message_id.label("message_id"),
            ImageAssetRecord.archive_size_bytes.label("previous_archive_size"),
            blob_snapshot.label("blob_snapshot"),
        )
        .join(MessageRecord, MessageRecord.id == ImageAssetRecord.message_id)
        .where(
            *_candidate_filters(
                asset=ImageAssetRecord,
                asset_kind="image",
                cleanup_filter=cleanup_filter,
            )
        )
        .order_by(ImageAssetRecord.created_at, ImageAssetRecord.id)
    )


def cleanup_video_candidates_statement(*, cleanup_filter: ArchiveCleanupFilter):
    """Select READY videos and a deterministic snapshot of their READY frame blobs."""

    ready_frames = (
        select(
            VideoFrameRecord.video_asset_id.label("video_asset_id"),
            func.coalesce(func.sum(VideoFrameRecord.archive_size_bytes), 0).label(
                "previous_archive_size"
            ),
            func.coalesce(
                func.jsonb_agg(
                    aggregate_order_by(
                        func.jsonb_build_object(
                            "storage_backend",
                            VideoFrameRecord.storage_backend,
                            "storage_key",
                            VideoFrameRecord.storage_key,
                            "size_bytes",
                            VideoFrameRecord.archive_size_bytes,
                        ),
                        VideoFrameRecord.frame_role,
                        VideoFrameRecord.frame_index,
                        VideoFrameRecord.id,
                    )
                ),
                cast(literal("[]"), JSONB),
            ).label("blob_snapshot"),
        )
        .where(VideoFrameRecord.archive_state == "ready")
        .group_by(VideoFrameRecord.video_asset_id)
        .subquery()
    )
    return (
        select(
            literal("video").label("asset_kind"),
            VideoAssetRecord.id.label("asset_id"),
            VideoAssetRecord.message_id.label("message_id"),
            func.coalesce(ready_frames.c.previous_archive_size, 0).label("previous_archive_size"),
            func.coalesce(
                ready_frames.c.blob_snapshot,
                cast(literal("[]"), JSONB),
            ).label("blob_snapshot"),
        )
        .join(MessageRecord, MessageRecord.id == VideoAssetRecord.message_id)
        .outerjoin(ready_frames, ready_frames.c.video_asset_id == VideoAssetRecord.id)
        .where(
            *_candidate_filters(
                asset=VideoAssetRecord,
                asset_kind="video",
                cleanup_filter=cleanup_filter,
            )
        )
        .order_by(VideoAssetRecord.created_at, VideoAssetRecord.id)
    )


def cleanup_job_claim_statement(*, job_id: UUID, now: datetime):
    """Lock one claimable cleanup job without waiting behind another worker."""

    return (
        select(ArchiveCleanupJobRecord)
        .where(
            ArchiveCleanupJobRecord.id == job_id,
            or_(
                ArchiveCleanupJobRecord.status == "pending",
                and_(
                    ArchiveCleanupJobRecord.status == "retry_wait",
                    ArchiveCleanupJobRecord.next_retry_at <= now,
                ),
                and_(
                    ArchiveCleanupJobRecord.status == "running",
                    ArchiveCleanupJobRecord.lease_expires_at <= now,
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    )


def cleanup_repair_candidates_statement(*, now: datetime, limit: int):
    """Find cleanup jobs whose UUID-only broker signal can be reconstructed."""

    return (
        select(ArchiveCleanupJobRecord)
        .where(
            or_(
                ArchiveCleanupJobRecord.status == "pending",
                and_(
                    ArchiveCleanupJobRecord.status == "retry_wait",
                    ArchiveCleanupJobRecord.next_retry_at <= now,
                ),
                and_(
                    ArchiveCleanupJobRecord.status == "running",
                    ArchiveCleanupJobRecord.lease_expires_at <= now,
                ),
            )
        )
        .order_by(ArchiveCleanupJobRecord.created_at, ArchiveCleanupJobRecord.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def cleanup_complete_wakeup_statement(*, job_id: UUID, now: datetime):
    """Complete a maintenance wake-up only after its cleanup job is terminal."""

    terminal_job = exists(
        select(ArchiveCleanupJobRecord.id).where(
            ArchiveCleanupJobRecord.id == job_id,
            ArchiveCleanupJobRecord.status.in_(_TERMINAL_JOB_STATUSES),
        )
    )
    return (
        update(DurableWakeupRecord)
        .where(
            DurableWakeupRecord.queue == CLEANUP_QUEUE,
            DurableWakeupRecord.entity_id == job_id,
            DurableWakeupRecord.status.in_(_ACTIVE_WAKEUP_STATUSES),
            terminal_job,
        )
        .values(
            status="completed",
            lease_token=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
    )


def cleanup_asset_in_use_statement(*, asset_kind: str, asset_id: UUID, message_id: UUID):
    """Conservatively revalidate analysis and publication use while the asset is locked."""

    media_use = and_(
        StageRunRecord.target_kind == f"{asset_kind}_asset",
        StageRunRecord.target_id == asset_id,
    )
    message_use = and_(
        StageRunRecord.target_kind == "message",
        StageRunRecord.message_id == message_id,
    )
    running_analysis = exists(
        select(StageRunRecord.id).where(
            StageRunRecord.status == "running",
            or_(media_use, message_use),
        )
    )
    active_publication = exists(
        select(PublicationIntentRecord.id).where(
            PublicationIntentRecord.message_id == message_id,
            PublicationIntentRecord.status.in_(_ACTIVE_PUBLICATION_STATUSES),
        )
    )
    return select(or_(running_analysis, active_publication))


class SqlAlchemyArchiveCleanupRepository:
    """Persist cleanup preview, confirmation, leasing, deletion, and recovery state."""

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._database = database
        self._id_factory = id_factory or uuid4

    async def create_dry_run(
        self,
        *,
        cleanup_filter: ArchiveCleanupFilter,
        actor_admin_user_id: str,
        now: datetime,
    ) -> ArchiveCleanupDryRun:
        ensure_aware(now, field="now")
        actor_id = _as_uuid(actor_admin_user_id, field="actor_admin_user_id")
        if not isinstance(cleanup_filter, ArchiveCleanupFilter):
            raise DomainValidationError("cleanup_filter must be an ArchiveCleanupFilter")

        async with self._database.session() as session:
            async with session.begin():
                candidates = await self._collect_candidates(
                    session=session,
                    cleanup_filter=cleanup_filter,
                )
                estimate = _estimate(candidates)
                dry_run_id = self._id_factory()
                session.add(
                    ArchiveCleanupDryRunRecord(
                        id=dry_run_id,
                        actor_admin_user_id=actor_id,
                        filter_snapshot=cleanup_filter.snapshot(),
                        filter_hash=cleanup_filter.sha256,
                        estimated_asset_count=estimate.asset_count,
                        estimated_message_count=estimate.message_count,
                        estimated_bytes=estimate.bytes,
                        created_at=now,
                    )
                )
                session.add(
                    AuditEvent(
                        id=self._id_factory(),
                        actor_admin_user_id=actor_id,
                        event_type="archive_cleanup.dry_run_created",
                        entity_type="archive_cleanup_dry_run",
                        entity_id=dry_run_id,
                        payload=sanitize_json(
                            {
                                "actor_type": "admin",
                                "action": "archive_cleanup.dry_run_created",
                                "before_snapshot": None,
                                "after_snapshot": {
                                    "dry_run_id": str(dry_run_id),
                                    "filter_hash": cleanup_filter.sha256,
                                    "filter_snapshot": cleanup_filter.snapshot(),
                                    "estimated_asset_count": estimate.asset_count,
                                    "estimated_message_count": estimate.message_count,
                                    "estimated_bytes": estimate.bytes,
                                },
                            }
                        ),
                        created_at=now,
                    )
                )
                return ArchiveCleanupDryRun(
                    dry_run_id=str(dry_run_id),
                    actor_admin_user_id=str(actor_id),
                    filter_snapshot=cleanup_filter.snapshot(),
                    filter_hash=cleanup_filter.sha256,
                    estimate=estimate,
                    created_at=now,
                )

    async def confirm(
        self,
        *,
        dry_run_id: str,
        expected_filter_hash: str,
        confirmed_by_admin_user_id: str,
        now: datetime,
        max_attempts: int = 5,
    ) -> ConfirmedArchiveCleanupJob:
        ensure_aware(now, field="now")
        dry_run_uuid = _as_uuid(dry_run_id, field="dry_run_id")
        confirmer_id = _as_uuid(
            confirmed_by_admin_user_id,
            field="confirmed_by_admin_user_id",
        )
        if not isinstance(expected_filter_hash, str) or len(expected_filter_hash) != 64:
            raise DomainValidationError("expected_filter_hash must be a SHA-256 digest")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
            raise DomainValidationError("max_attempts must be a positive integer")

        async with self._database.session() as session:
            async with session.begin():
                dry_run = await session.scalar(
                    select(ArchiveCleanupDryRunRecord)
                    .where(ArchiveCleanupDryRunRecord.id == dry_run_uuid)
                    .with_for_update()
                )
                if dry_run is None:
                    raise DomainValidationError("cleanup dry-run does not exist")
                if dry_run.filter_hash != expected_filter_hash:
                    raise DomainValidationError("cleanup dry-run filter hash changed")

                existing = await session.scalar(
                    select(ArchiveCleanupJobRecord)
                    .where(ArchiveCleanupJobRecord.dry_run_id == dry_run_uuid)
                    .with_for_update()
                )
                if existing is not None:
                    return _confirmed_job(existing)

                cleanup_filter = ArchiveCleanupFilter.from_snapshot(dry_run.filter_snapshot)
                candidates = await self._collect_candidates(
                    session=session,
                    cleanup_filter=cleanup_filter,
                )
                estimate = _estimate(candidates)
                job_id = self._id_factory()
                job = ArchiveCleanupJobRecord(
                    id=job_id,
                    dry_run_id=dry_run_uuid,
                    confirmed_by_admin_user_id=confirmer_id,
                    filter_snapshot=dict(dry_run.filter_snapshot),
                    filter_hash=dry_run.filter_hash,
                    estimated_asset_count=estimate.asset_count,
                    estimated_message_count=estimate.message_count,
                    estimated_bytes=estimate.bytes,
                    item_count=len(candidates),
                    status="pending",
                    attempt_count=0,
                    max_attempts=max_attempts,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
                for candidate in candidates:
                    session.add(
                        ArchiveCleanupItemRecord(
                            id=self._id_factory(),
                            cleanup_job_id=job_id,
                            asset_kind=candidate.asset_kind,
                            image_asset_id=(
                                candidate.asset_id if candidate.asset_kind == "image" else None
                            ),
                            video_asset_id=(
                                candidate.asset_id if candidate.asset_kind == "video" else None
                            ),
                            message_id=candidate.message_id,
                            status="pending",
                            blob_snapshot=list(candidate.blob_snapshot),
                            previous_archive_size=candidate.previous_archive_size,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                session.add(
                    DurableWakeupRecord(
                        id=self._id_factory(),
                        queue=CLEANUP_QUEUE,
                        entity_id=job_id,
                        status="pending",
                        next_attempt_at=now,
                        dispatch_attempts=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    AuditEvent(
                        id=self._id_factory(),
                        actor_admin_user_id=confirmer_id,
                        event_type="archive_cleanup.job_confirmed",
                        entity_type="archive_cleanup_job",
                        entity_id=job_id,
                        payload=sanitize_json(
                            {
                                "actor_type": "admin",
                                "action": "archive_cleanup.job_confirmed",
                                "before_snapshot": {
                                    "dry_run_id": str(dry_run_uuid),
                                    "filter_hash": dry_run.filter_hash,
                                    "estimated_asset_count": dry_run.estimated_asset_count,
                                    "estimated_message_count": dry_run.estimated_message_count,
                                    "estimated_bytes": dry_run.estimated_bytes,
                                },
                                "after_snapshot": {
                                    "job_id": str(job_id),
                                    "dry_run_id": str(dry_run_uuid),
                                    "filter_hash": dry_run.filter_hash,
                                    "estimated_asset_count": estimate.asset_count,
                                    "estimated_message_count": estimate.message_count,
                                    "estimated_bytes": estimate.bytes,
                                    "item_count": len(candidates),
                                    "status": "pending",
                                },
                            }
                        ),
                        created_at=now,
                    )
                )
                return _confirmed_job(job)

    async def claim(
        self,
        *,
        job_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedArchiveCleanupJob | None:
        ensure_aware(now, field="now")
        ensure_positive_duration(lease_duration, field="lease_duration")
        job_uuid = _as_uuid(job_id, field="job_id")
        async with self._database.session() as session:
            async with session.begin():
                job = await session.scalar(cleanup_job_claim_statement(job_id=job_uuid, now=now))
                if job is None:
                    return None
                if job.attempt_count >= job.max_attempts:
                    await self._terminalize_exhausted(session=session, job=job, now=now)
                    return None

                lease_token = self._id_factory()
                job.status = "running"
                job.attempt_count += 1
                job.next_retry_at = None
                job.lease_token = lease_token
                job.lease_expires_at = now + lease_duration
                job.started_at = job.started_at or now
                job.updated_at = now
                return ClaimedArchiveCleanupJob(
                    job_id=str(job.id),
                    lease_token=str(lease_token),
                    attempt_number=job.attempt_count,
                    max_attempts=job.max_attempts,
                )

    async def claim_next_item(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        now: datetime,
    ) -> ClaimedCleanupItem | None:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                job = await self._locked_active_job(session=session, claim=claim, now=now)
                if job is None:
                    return None

                replay = await session.scalar(
                    select(ArchiveCleanupItemRecord)
                    .where(
                        ArchiveCleanupItemRecord.cleanup_job_id == job.id,
                        ArchiveCleanupItemRecord.status == "deleting",
                    )
                    .order_by(ArchiveCleanupItemRecord.created_at, ArchiveCleanupItemRecord.id)
                    .with_for_update(skip_locked=True)
                )
                if replay is not None:
                    return _claimed_item(replay)

                while True:
                    item = await session.scalar(
                        select(ArchiveCleanupItemRecord)
                        .where(
                            ArchiveCleanupItemRecord.cleanup_job_id == job.id,
                            ArchiveCleanupItemRecord.status == "pending",
                        )
                        .order_by(ArchiveCleanupItemRecord.created_at, ArchiveCleanupItemRecord.id)
                        .with_for_update(skip_locked=True)
                    )
                    if item is None:
                        return None

                    asset = await self._lock_ready_asset(session=session, item=item)
                    if asset is None or await session.scalar(
                        cleanup_asset_in_use_statement(
                            asset_kind=item.asset_kind,
                            asset_id=_item_asset_id(item),
                            message_id=item.message_id,
                        )
                    ):
                        item.status = "skipped_in_use"
                        item.skipped_at = now
                        item.updated_at = now
                        continue

                    deletion_token = self._id_factory()
                    item.status = "deleting"
                    item.deletion_token = deletion_token
                    item.updated_at = now
                    asset.archive_state = "deleting"
                    asset.archive_deleted_at = None
                    asset.cleanup_job_id = job.id
                    asset.previous_archive_size = item.previous_archive_size
                    asset.updated_at = now
                    if item.asset_kind == "video":
                        frames = tuple(
                            await session.scalars(
                                select(VideoFrameRecord)
                                .where(
                                    VideoFrameRecord.video_asset_id == asset.id,
                                    VideoFrameRecord.archive_state == "ready",
                                )
                                .order_by(
                                    VideoFrameRecord.frame_role,
                                    VideoFrameRecord.frame_index,
                                    VideoFrameRecord.id,
                                )
                                .with_for_update()
                            )
                        )
                        for frame in frames:
                            frame.archive_state = "deleting"
                            frame.archive_deleted_at = None
                            frame.cleanup_job_id = job.id
                            frame.previous_archive_size = frame.archive_size_bytes
                            frame.updated_at = now
                    return _claimed_item(item)

    async def finalize_item_deleted(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        item: ClaimedCleanupItem,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                job = await self._locked_active_job(session=session, claim=claim, now=now)
                if job is None:
                    return False
                record = await session.scalar(
                    select(ArchiveCleanupItemRecord)
                    .where(
                        ArchiveCleanupItemRecord.id == _as_uuid(item.item_id, field="item_id"),
                        ArchiveCleanupItemRecord.cleanup_job_id == job.id,
                        ArchiveCleanupItemRecord.status == "deleting",
                        ArchiveCleanupItemRecord.deletion_token
                        == _as_uuid(item.deletion_token, field="deletion_token"),
                    )
                    .with_for_update()
                )
                if record is None:
                    return False
                asset = await self._lock_deleting_asset(session=session, item=record, job_id=job.id)
                if asset is None:
                    return False

                asset.archive_state = "deleted"
                asset.archive_deleted_at = now
                asset.updated_at = now
                if record.asset_kind == "video":
                    frames = tuple(
                        await session.scalars(
                            select(VideoFrameRecord)
                            .where(
                                VideoFrameRecord.video_asset_id == asset.id,
                                VideoFrameRecord.cleanup_job_id == job.id,
                                VideoFrameRecord.archive_state == "deleting",
                            )
                            .with_for_update()
                        )
                    )
                    for frame in frames:
                        frame.archive_state = "deleted"
                        frame.archive_deleted_at = now
                        frame.updated_at = now
                record.status = "deleted"
                record.deletion_token = None
                record.deleted_at = now
                record.updated_at = now
                return True

    async def complete(self, *, claim: ClaimedArchiveCleanupJob, now: datetime) -> bool:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                job = await self._locked_active_job(session=session, claim=claim, now=now)
                if job is None:
                    return False
                unfinished = await session.scalar(
                    select(
                        exists(
                            select(ArchiveCleanupItemRecord.id).where(
                                ArchiveCleanupItemRecord.cleanup_job_id == job.id,
                                ArchiveCleanupItemRecord.status.in_(("pending", "deleting")),
                            )
                        )
                    )
                )
                if unfinished:
                    return False
                job.status = "completed"
                job.next_retry_at = None
                job.lease_token = None
                job.lease_expires_at = None
                job.completed_at = now
                job.updated_at = now
                await self._set_wakeup_terminal(session=session, entity_id=job.id, now=now)
                return True

    async def retry_or_fail(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        item: ClaimedCleanupItem | None,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        ensure_aware(retry_at, field="retry_at")
        code = _error_label(error_code, field="error_code")
        error_name = _error_label(error_type, field="error_type")
        if not isinstance(retryable, bool):
            raise DomainValidationError("retryable must be a boolean")

        async with self._database.session() as session:
            async with session.begin():
                job = await self._locked_active_job(session=session, claim=claim, now=now)
                if job is None:
                    return False
                item_record = None
                if item is not None:
                    item_record = await session.scalar(
                        select(ArchiveCleanupItemRecord)
                        .where(
                            ArchiveCleanupItemRecord.id == _as_uuid(item.item_id, field="item_id"),
                            ArchiveCleanupItemRecord.cleanup_job_id == job.id,
                            ArchiveCleanupItemRecord.status == "deleting",
                            ArchiveCleanupItemRecord.deletion_token
                            == _as_uuid(item.deletion_token, field="deletion_token"),
                        )
                        .with_for_update()
                    )
                    if item_record is None:
                        return False

                job.last_error_code = code
                job.last_error_type = error_name
                job.last_failure_at = now
                job.lease_token = None
                job.lease_expires_at = None
                job.updated_at = now
                can_retry = retryable and job.attempt_count < job.max_attempts
                if can_retry:
                    job.status = "retry_wait"
                    job.next_retry_at = retry_at
                    await self._reschedule_wakeup(
                        session=session,
                        entity_id=job.id,
                        retry_at=retry_at,
                        now=now,
                    )
                else:
                    job.status = "failed"
                    job.next_retry_at = None
                    job.completed_at = now
                    if item_record is not None:
                        item_record.status = "failed"
                        item_record.deletion_token = None
                        item_record.last_error_code = code
                        item_record.last_error_type = error_name
                        item_record.last_failure_at = now
                        item_record.updated_at = now
                    await self._set_wakeup_terminal(session=session, entity_id=job.id, now=now)
                return True

    async def complete_wakeup_if_terminal(self, *, job_id: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    cleanup_complete_wakeup_statement(
                        job_id=_as_uuid(job_id, field="job_id"),
                        now=now,
                    )
                )
                return result.rowcount == 1

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int:
        ensure_aware(now, field="now")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise DomainValidationError("limit must be a positive integer")
        async with self._database.session() as session:
            async with session.begin():
                jobs = tuple(
                    await session.scalars(cleanup_repair_candidates_statement(now=now, limit=limit))
                )
                repaired = 0
                for job in jobs:
                    wakeup_id = await session.scalar(
                        postgresql_insert(DurableWakeupRecord)
                        .values(
                            id=self._id_factory(),
                            queue=CLEANUP_QUEUE,
                            entity_id=job.id,
                            status="pending",
                            next_attempt_at=now,
                            lease_token=None,
                            lease_expires_at=None,
                            dispatch_attempts=0,
                            completed_at=None,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=[
                                DurableWakeupRecord.queue,
                                DurableWakeupRecord.entity_id,
                            ],
                            set_={
                                "status": "pending",
                                "next_attempt_at": now,
                                "lease_token": None,
                                "lease_expires_at": None,
                                "completed_at": None,
                                "updated_at": now,
                            },
                            where=or_(
                                DurableWakeupRecord.status != "leased",
                                DurableWakeupRecord.lease_expires_at <= now,
                            ),
                        )
                        .returning(DurableWakeupRecord.id)
                    )
                    if wakeup_id is not None:
                        repaired += 1
                return repaired

    @staticmethod
    async def _collect_candidates(
        *,
        session: Any,
        cleanup_filter: ArchiveCleanupFilter,
    ) -> tuple[_CleanupCandidate, ...]:
        candidates: list[_CleanupCandidate] = []
        for statement in (
            cleanup_image_candidates_statement(cleanup_filter=cleanup_filter),
            cleanup_video_candidates_statement(cleanup_filter=cleanup_filter),
        ):
            result = await session.execute(statement)
            candidates.extend(_candidate_from_row(row) for row in result.all())
        return tuple(candidates)

    @staticmethod
    async def _locked_active_job(
        *,
        session: Any,
        claim: ClaimedArchiveCleanupJob,
        now: datetime,
    ) -> ArchiveCleanupJobRecord | None:
        return await session.scalar(
            select(ArchiveCleanupJobRecord)
            .where(
                ArchiveCleanupJobRecord.id == _as_uuid(claim.job_id, field="job_id"),
                ArchiveCleanupJobRecord.status == "running",
                ArchiveCleanupJobRecord.lease_token
                == _as_uuid(claim.lease_token, field="lease_token"),
                ArchiveCleanupJobRecord.lease_expires_at > now,
            )
            .with_for_update()
        )

    @staticmethod
    async def _lock_ready_asset(*, session: Any, item: ArchiveCleanupItemRecord):
        model = ImageAssetRecord if item.asset_kind == "image" else VideoAssetRecord
        return await session.scalar(
            select(model)
            .where(
                model.id == _item_asset_id(item),
                model.archive_state == "ready",
            )
            .with_for_update()
        )

    @staticmethod
    async def _lock_deleting_asset(
        *,
        session: Any,
        item: ArchiveCleanupItemRecord,
        job_id: UUID,
    ):
        model = ImageAssetRecord if item.asset_kind == "image" else VideoAssetRecord
        return await session.scalar(
            select(model)
            .where(
                model.id == _item_asset_id(item),
                model.archive_state == "deleting",
                model.cleanup_job_id == job_id,
            )
            .with_for_update()
        )

    @staticmethod
    async def _reschedule_wakeup(
        *,
        session: Any,
        entity_id: UUID,
        retry_at: datetime,
        now: datetime,
    ) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == CLEANUP_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
                DurableWakeupRecord.status != "cancelled",
            )
            .values(
                status="pending",
                next_attempt_at=retry_at,
                lease_token=None,
                lease_expires_at=None,
                completed_at=None,
                updated_at=now,
            )
        )

    @staticmethod
    async def _set_wakeup_terminal(
        *,
        session: Any,
        entity_id: UUID,
        now: datetime,
    ) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == CLEANUP_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
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

    async def _terminalize_exhausted(
        self,
        *,
        session: Any,
        job: ArchiveCleanupJobRecord,
        now: datetime,
    ) -> None:
        job.status = "failed"
        job.next_retry_at = None
        job.lease_token = None
        job.lease_expires_at = None
        job.last_error_code = "CLEANUP_ATTEMPTS_EXHAUSTED"
        job.last_error_type = "CleanupAttemptsExhausted"
        job.last_failure_at = now
        job.completed_at = now
        job.updated_at = now
        await self._set_wakeup_terminal(session=session, entity_id=job.id, now=now)


def _candidate_from_row(row: Any) -> _CleanupCandidate:
    values: Mapping[str, Any]
    if hasattr(row, "_mapping"):
        values = row._mapping
    elif isinstance(row, Mapping):
        values = row
    else:
        asset_kind, asset_id, message_id, previous_archive_size, blob_snapshot = row
        values = {
            "asset_kind": asset_kind,
            "asset_id": asset_id,
            "message_id": message_id,
            "previous_archive_size": previous_archive_size,
            "blob_snapshot": blob_snapshot,
        }
    raw_snapshot = values["blob_snapshot"] or []
    snapshot = tuple(
        {
            "storage_backend": str(blob["storage_backend"]),
            "storage_key": str(blob["storage_key"]),
            "size_bytes": int(blob["size_bytes"]),
        }
        for blob in raw_snapshot
    )
    return _CleanupCandidate(
        asset_kind=str(values["asset_kind"]),
        asset_id=values["asset_id"],
        message_id=values["message_id"],
        previous_archive_size=int(values["previous_archive_size"]),
        blob_snapshot=snapshot,
    )


def _estimate(candidates: Sequence[_CleanupCandidate]) -> ArchiveCleanupEstimate:
    return ArchiveCleanupEstimate(
        asset_count=len(candidates),
        message_count=len({candidate.message_id for candidate in candidates}),
        bytes=sum(candidate.previous_archive_size for candidate in candidates),
    )


def _confirmed_job(job: ArchiveCleanupJobRecord) -> ConfirmedArchiveCleanupJob:
    return ConfirmedArchiveCleanupJob(
        job_id=str(job.id),
        dry_run_id=str(job.dry_run_id),
        filter_hash=job.filter_hash,
        estimate=ArchiveCleanupEstimate(
            asset_count=job.estimated_asset_count,
            message_count=job.estimated_message_count,
            bytes=job.estimated_bytes,
        ),
        item_count=job.item_count,
        status=job.status,
        created_at=job.created_at,
    )


def _claimed_item(record: ArchiveCleanupItemRecord) -> ClaimedCleanupItem:
    if record.deletion_token is None:
        raise DomainValidationError("deleting cleanup item is missing its deletion token")
    blobs = tuple(
        CleanupBlob(
            storage_backend=str(blob["storage_backend"]),
            storage_key=str(blob["storage_key"]),
            size_bytes=int(blob["size_bytes"]),
        )
        for blob in record.blob_snapshot
    )
    return ClaimedCleanupItem(
        item_id=str(record.id),
        deletion_token=str(record.deletion_token),
        asset=CleanupAsset(
            asset_kind=record.asset_kind,
            asset_id=str(_item_asset_id(record)),
            message_id=str(record.message_id),
            blobs=blobs,
            previous_archive_size=record.previous_archive_size,
        ),
    )


def _item_asset_id(item: ArchiveCleanupItemRecord) -> UUID:
    asset_id = item.image_asset_id if item.asset_kind == "image" else item.video_asset_id
    if asset_id is None:
        raise DomainValidationError("cleanup item has no asset identity")
    return asset_id


def _error_label(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must not be blank")
    return value.strip()[:128]


def _as_uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
