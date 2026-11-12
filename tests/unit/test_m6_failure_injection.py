from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from apps.scheduler.runtime import SchedulerRuntime
from apps.worker.runtime import WorkerRuntime
from tgcurator.application.cleanup import ArchiveCleanupService, ArchiveCleanupWorker
from tgcurator.application.ports.cleanup import (
    ArchiveCleanupDryRun,
    ArchiveCleanupEstimate,
    ArchiveCleanupFilter,
    ClaimedArchiveCleanupJob,
    ClaimedCleanupItem,
    CleanupAsset,
    CleanupBlob,
    ConfirmedArchiveCleanupJob,
    MappingArchiveStorageResolver,
)
from tgcurator.application.ports.operations import BacklogMetricsSnapshot
from tgcurator.application.ports.processing import (
    ClaimedRangeExecution,
    ClaimedWakeup,
    PendingRangeExecution,
    ScheduledProcessingRange,
)
from tgcurator.application.ports.publishing import (
    ClaimedPublicationIntent,
    TelegramFloodWait,
    TelegramPublicationError,
    TelegramPublishRequest,
    TelegramPublishResult,
)
from tgcurator.application.ports.routing import (
    PublicationIntentDraft,
    RoutingEvaluationDraft,
    RoutingExecutionResult,
    RoutingRequest,
    RoutingSnapshot,
)
from tgcurator.application.processing import DurableWakeupDispatcher, ProcessingRangeScheduler
from tgcurator.application.publishing import PublicationWorker
from tgcurator.application.routing import RoutingService
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
    PublicationStatus,
    publication_plan,
    publication_status_for_parts,
    stable_random_id,
    stable_random_ids,
)
from tgcurator.domain.review import ReviewStatus
from tgcurator.domain.routing import PublicationAction, RoutingDecision, RoutingPolicy, RoutingRule
from tgcurator.infrastructure.observability import OperationsMonitor

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
ADMIN_ID = str(UUID("00000000-0000-4000-8000-000000000001"))
SOURCE_CHANNEL_ID = str(UUID("00000000-0000-4000-8000-000000000002"))
DESTINATION_CHANNEL_ID = str(UUID("00000000-0000-4000-8000-000000000003"))
PUBLISH_IDENTITY_ID = str(UUID("00000000-0000-4000-8000-000000000004"))
POLICY_VERSION_ID = str(UUID("00000000-0000-4000-8000-000000000005"))
RULE_ID = str(UUID("00000000-0000-4000-8000-000000000006"))
ACTION_ID = str(UUID("00000000-0000-4000-8000-000000000007"))
TEMPLATE_VERSION_ID = str(UUID("00000000-0000-4000-8000-000000000008"))


@dataclass
class _StoredWakeup:
    wakeup_id: str
    queue: str
    entity_id: str
    status: str = "pending"
    next_attempt_at: datetime = NOW
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    dispatch_attempts: int = 0
    completed_at: datetime | None = None


@dataclass
class _StoredIntent:
    intent_id: str
    message_id: str
    source_message_ids: tuple[int, ...]
    mode: PublicationMode
    protected_content_fallback: ProtectedContentFallback
    supplement_as_reply: bool
    rendered_text: str | None
    rendered_snapshot_hash: str | None
    business_idempotency_key: str
    status: str = "pending"
    main_random_ids: tuple[int, ...] = ()
    supplement_random_id: int | None = None
    completed_parts: list[PublicationPart] = field(default_factory=list)
    main_remote_message_ids: tuple[int, ...] = ()
    in_flight_part: PublicationPart | None = None
    in_flight_operation: PublicationOperation | None = None
    in_flight_started_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_retry_at: datetime | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    last_error_code: str | None = None
    last_error_type: str | None = None
    completed_at: datetime | None = None
    created_at: datetime = NOW


@dataclass
class _StoredAttempt:
    attempt_id: str
    intent_id: str
    attempt_number: int
    lease_token: str
    status: str = "sending"
    error_code: str | None = None
    error_type: str | None = None
    reconciled: bool = False
    next_retry_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class _StoredPublishRecord:
    record_id: str
    intent_id: str
    attempt_id: str
    part: PublicationPart
    operation: PublicationOperation
    remote_message_ids: tuple[int, ...]
    random_ids: tuple[int, ...]
    reconciled: bool
    sent_at: datetime


@dataclass
class _StoredAsset:
    asset_id: str
    asset_kind: str
    message_id: str
    blobs: tuple[CleanupBlob, ...]
    archive_state: str = "ready"
    source_sha256: str = "a" * 64
    archive_sha256: str = "b" * 64
    perceptual_hash: str = "f0f0f0f0f0f0f0f0"
    cleanup_job_id: str | None = None
    previous_archive_size: int | None = None
    archive_deleted_at: datetime | None = None


@dataclass
class _StoredCleanupItem:
    item_id: str
    job_id: str
    asset_id: str
    asset_kind: str
    message_id: str
    blobs: tuple[CleanupBlob, ...]
    previous_archive_size: int
    status: str = "pending"
    deletion_token: str | None = None
    deleted_at: datetime | None = None
    skipped_at: datetime | None = None
    last_error_code: str | None = None


@dataclass
class _StoredCleanupJob:
    job_id: str
    dry_run_id: str
    filter_hash: str
    estimate: ArchiveCleanupEstimate
    item_ids: tuple[str, ...]
    status: str = "pending"
    attempt_count: int = 0
    max_attempts: int = 5
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_retry_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = NOW


class _StatefulPostgresStore:
    """Deterministic in-memory model of PostgreSQL M6 tables, leases, and constraints."""

    def __init__(self) -> None:
        self.messages: dict[str, tuple[int, ...]] = {}
        self.visual_fingerprints: dict[str, str] = {}
        self.evaluations: dict[str, RoutingExecutionResult] = {}
        self.intents: dict[str, _StoredIntent] = {}
        self.attempts: dict[str, _StoredAttempt] = {}
        self.publish_records: list[_StoredPublishRecord] = []
        self.wakeups: dict[tuple[str, str], _StoredWakeup] = {}
        self.assets: dict[str, _StoredAsset] = {}
        self.running_stage_targets: set[tuple[str, str]] = set()
        self.dry_runs: dict[str, ArchiveCleanupDryRun] = {}
        self.cleanup_jobs: dict[str, _StoredCleanupJob] = {}
        self.cleanup_items: dict[str, _StoredCleanupItem] = {}
        self.audit_events: list[tuple[str, str, str]] = []
        self.fail_next_record_part = False
        self.fail_next_cleanup_finalize = False

    def seed_message(
        self,
        *,
        message_id: str | None = None,
        telegram_message_ids: tuple[int, ...] = (101, 102),
    ) -> str:
        msg_id = message_id or str(uuid4())
        self.messages[msg_id] = telegram_message_ids
        self.visual_fingerprints[msg_id] = "c" * 64
        return msg_id

    def seed_asset(
        self,
        *,
        message_id: str,
        asset_kind: str = "image",
        blobs: tuple[CleanupBlob, ...] | None = None,
    ) -> _StoredAsset:
        asset_id = str(uuid4())
        frozen_blobs = blobs or (
            CleanupBlob(
                storage_backend="archive",
                storage_key=f"{asset_kind}/{asset_id}.webp",
                size_bytes=128,
            ),
        )
        asset = _StoredAsset(
            asset_id=asset_id,
            asset_kind=asset_kind,
            message_id=message_id,
            blobs=frozen_blobs,
        )
        self.assets[asset_id] = asset
        return asset

    def upsert_wakeup(self, *, queue: str, entity_id: str, next_attempt_at: datetime) -> bool:
        key = (queue, entity_id)
        existing = self.wakeups.get(key)
        if existing is not None:
            if (
                existing.status == "leased"
                and existing.lease_expires_at is not None
                and existing.lease_expires_at > next_attempt_at
            ):
                return False
            existing.status = "pending"
            existing.next_attempt_at = next_attempt_at
            existing.lease_token = None
            existing.lease_expires_at = None
            existing.completed_at = None
            return True
        self.wakeups[key] = _StoredWakeup(
            wakeup_id=str(uuid4()),
            queue=queue,
            entity_id=entity_id,
            status="pending",
            next_attempt_at=next_attempt_at,
        )
        return True

    def wipe_wakeups(self) -> None:
        self.wakeups.clear()


class _RoutingRepoAdapter:
    def __init__(
        self,
        store: _StatefulPostgresStore,
        *,
        mode: PublicationMode = PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT,
    ) -> None:
        self._store = store
        self._mode = mode

    async def find_by_request_id(self, *, routing_request_id: str) -> RoutingExecutionResult | None:
        return self._store.evaluations.get(routing_request_id)

    async def load_snapshot(
        self,
        *,
        message_id: str,
        routing_policy_version_id: str,
    ) -> RoutingSnapshot:
        template_id = None if self._mode is PublicationMode.FORWARD_ONLY else TEMPLATE_VERSION_ID
        policy = RoutingPolicy(
            policy_version_id=routing_policy_version_id,
            rules=(
                RoutingRule(
                    rule_id=RULE_ID,
                    priority=100,
                    condition={"fact": "blocked_from_analysis", "op": "eq", "value": False},
                    actions=(
                        PublicationAction(
                            action_id=ACTION_ID,
                            destination_channel_id=DESTINATION_CHANNEL_ID,
                            publication_mode=self._mode.value,
                            rendering_template_version_id=template_id,
                            publish_identity_id=PUBLISH_IDENTITY_ID,
                            protected_content_fallback=ProtectedContentFallback.METADATA_ONLY.value,
                            supplement_as_reply=True,
                        ),
                    ),
                    stop_on_match=True,
                ),
            ),
        )
        return RoutingSnapshot(
            message_id=message_id,
            message={"id": message_id, "text": "curated clip", "blocked_from_analysis": False},
            source_channel={"id": SOURCE_CHANNEL_ID, "display_name": "Source"},
            duplicate_state={"is_duplicate": False},
            media=(),
            review_status=ReviewStatus.REVIEWED,
            model_assignments=(),
            manual_events=(),
            policy=policy,
            destination_publish_identity_ids={DESTINATION_CHANNEL_ID: PUBLISH_IDENTITY_ID},
            rendering_template_texts={TEMPLATE_VERSION_ID: "Curated: {{ message.text }}"},
        )

    async def persist_evaluation(
        self,
        *,
        evaluation: RoutingEvaluationDraft,
        decision: RoutingDecision,
        intents: tuple[PublicationIntentDraft, ...],
    ) -> RoutingExecutionResult:
        for draft in intents:
            self._store.intents[draft.intent_id] = _StoredIntent(
                intent_id=draft.intent_id,
                message_id=draft.message_id,
                source_message_ids=self._store.messages[draft.message_id],
                mode=PublicationMode(draft.publication_mode),
                protected_content_fallback=ProtectedContentFallback(
                    draft.protected_content_fallback
                ),
                supplement_as_reply=draft.supplement_as_reply,
                rendered_text=draft.rendered_text,
                rendered_snapshot_hash=draft.rendered_snapshot_hash,
                business_idempotency_key=draft.business_idempotency_key,
            )
            self._store.upsert_wakeup(
                queue="publication",
                entity_id=draft.intent_id,
                next_attempt_at=NOW,
            )
        result = RoutingExecutionResult(
            evaluation_id=evaluation.evaluation_id,
            routing_request_id=evaluation.routing_request_id,
            message_id=evaluation.message_id,
            routing_policy_version_id=evaluation.routing_policy_version_id,
            facts_snapshot=evaluation.facts_snapshot,
            facts_hash=evaluation.facts_hash,
            decision=decision,
            intents=intents,
            persisted=True,
        )
        self._store.evaluations[evaluation.routing_request_id] = result
        return result


class _PublicationRepoAdapter:
    def __init__(self, store: _StatefulPostgresStore) -> None:
        self._store = store

    async def claim(
        self,
        *,
        intent_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedPublicationIntent | None:
        intent = self._store.intents.get(intent_id)
        if intent is None:
            return None
        claimable = (
            intent.status in {"pending", "partial"}
            or (
                intent.status == "retry_wait"
                and intent.next_retry_at is not None
                and intent.next_retry_at <= now
            )
            or (
                intent.status == "sending"
                and intent.lease_expires_at is not None
                and intent.lease_expires_at <= now
            )
        )
        if not claimable:
            return None
        if intent.attempt_count >= intent.max_attempts:
            intent.status = "failed"
            intent.last_error_code = "PUBLICATION_ATTEMPTS_EXHAUSTED"
            intent.completed_at = now
            await self.complete_wakeup_if_terminal(intent_id=intent_id, now=now)
            return None
        if intent.status == "sending" and intent.lease_token is not None:
            for attempt in self._store.attempts.values():
                if (
                    attempt.intent_id == intent_id
                    and attempt.lease_token == intent.lease_token
                    and attempt.status == "sending"
                ):
                    attempt.status = "retry_wait"
                    attempt.error_code = "PUBLICATION_LEASE_EXPIRED"
                    attempt.error_type = "PublicationLeaseExpired"
                    attempt.next_retry_at = now
                    attempt.completed_at = now
        if not intent.main_random_ids:
            count = (
                1
                if intent.mode is PublicationMode.METADATA_ONLY
                else len(intent.source_message_ids)
            )
            intent.main_random_ids = stable_random_ids(
                intent_id=intent.intent_id,
                part=PublicationPart.MAIN,
                count=count,
            )
        if (
            intent.mode is PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT
            and intent.supplement_random_id is None
        ):
            intent.supplement_random_id = stable_random_id(
                intent_id=intent.intent_id,
                part=PublicationPart.SUPPLEMENT,
            )
        lease_token = str(uuid4())
        attempt_id = str(uuid4())
        intent.status = "sending"
        intent.attempt_count += 1
        intent.next_retry_at = None
        intent.lease_token = lease_token
        intent.lease_expires_at = now + lease_duration
        self._store.attempts[attempt_id] = _StoredAttempt(
            attempt_id=attempt_id,
            intent_id=intent.intent_id,
            attempt_number=intent.attempt_count,
            lease_token=lease_token,
        )
        return ClaimedPublicationIntent(
            intent_id=intent.intent_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            attempt_number=intent.attempt_count,
            max_attempts=intent.max_attempts,
            message_id=intent.message_id,
            source_channel_id=SOURCE_CHANNEL_ID,
            source_peer_id=-1001001,
            source_message_ids=intent.source_message_ids,
            destination_channel_id=DESTINATION_CHANNEL_ID,
            destination_peer_id=-1002002,
            publish_identity_id=PUBLISH_IDENTITY_ID,
            mode=intent.mode,
            protected_content_fallback=intent.protected_content_fallback,
            rendered_text=intent.rendered_text,
            main_random_ids=intent.main_random_ids,
            supplement_random_id=intent.supplement_random_id,
            supplement_as_reply=intent.supplement_as_reply,
            completed_parts=tuple(intent.completed_parts),
            main_remote_message_ids=intent.main_remote_message_ids,
            in_flight_part=intent.in_flight_part,
            in_flight_operation=intent.in_flight_operation,
        )

    def _active(self, claim: ClaimedPublicationIntent, now: datetime) -> _StoredIntent | None:
        intent = self._store.intents.get(claim.intent_id)
        if (
            intent is None
            or intent.status != "sending"
            or intent.lease_token != claim.lease_token
            or intent.lease_expires_at is None
            or intent.lease_expires_at <= now
        ):
            return None
        return intent

    async def begin_part(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        now: datetime,
    ) -> bool:
        intent = self._active(claim, now)
        if intent is None or part in intent.completed_parts:
            return False
        if intent.in_flight_part is not None and intent.in_flight_part is not part:
            return False
        intent.in_flight_part = part
        intent.in_flight_operation = operation
        intent.in_flight_started_at = now
        return True

    async def record_part(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        remote_message_ids: tuple[int, ...],
        reconciled: bool,
        now: datetime,
    ) -> bool:
        if self._store.fail_next_record_part:
            self._store.fail_next_record_part = False
            raise RuntimeError("simulated DB commit crash after remote Telegram send")
        intent = self._active(claim, now)
        if (
            intent is None
            or intent.in_flight_part is not part
            or intent.in_flight_operation is not operation
            or part in intent.completed_parts
        ):
            return False
        random_ids = claim.random_ids_for(part)
        if operation is PublicationOperation.SEND_TEXT:
            random_ids = (random_ids[0],)
        self._store.publish_records.append(
            _StoredPublishRecord(
                record_id=str(uuid4()),
                intent_id=intent.intent_id,
                attempt_id=claim.attempt_id,
                part=part,
                operation=operation,
                remote_message_ids=remote_message_ids,
                random_ids=random_ids,
                reconciled=reconciled,
                sent_at=now,
            )
        )
        intent.completed_parts.append(part)
        if part is PublicationPart.MAIN:
            intent.main_remote_message_ids = remote_message_ids
        intent.in_flight_part = None
        intent.in_flight_operation = None
        intent.in_flight_started_at = None
        if reconciled:
            intent.last_reconciled_at = now
            self._store.attempts[claim.attempt_id].reconciled = True
        return True

    async def complete(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool:
        intent = self._active(claim, now)
        if intent is None:
            return False
        plan = publication_plan(intent.mode)
        if not set(plan.required_parts).issubset(set(intent.completed_parts)):
            return False
        intent.status = publication_status_for_parts(
            plan=plan,
            completed_parts=intent.completed_parts,
        ).value
        intent.lease_token = None
        intent.lease_expires_at = None
        intent.completed_at = now
        self._store.attempts[claim.attempt_id].status = "sent"
        self._store.attempts[claim.attempt_id].completed_at = now
        await self.complete_wakeup_if_terminal(intent_id=intent.intent_id, now=now)
        return True

    async def release(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool:
        intent = self._active(claim, now)
        if intent is None or intent.in_flight_part is not None:
            return False
        plan = publication_plan(intent.mode)
        next_status = publication_status_for_parts(
            plan=plan,
            completed_parts=intent.completed_parts,
        )
        if next_status is PublicationStatus.SENT:
            return False
        intent.status = next_status.value
        intent.lease_token = None
        intent.lease_expires_at = None
        intent.next_retry_at = None
        attempt = self._store.attempts[claim.attempt_id]
        attempt.status = "retry_wait"
        attempt.error_code = "PUBLICATION_LEASE_RELEASED"
        attempt.next_retry_at = now
        attempt.completed_at = now
        self._store.upsert_wakeup(
            queue="publication",
            entity_id=intent.intent_id,
            next_attempt_at=now,
        )
        return True

    async def retry_or_fail(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        intent = self._active(claim, now)
        if intent is None:
            return False
        intent.last_error_code = error_code
        intent.last_error_type = error_type
        intent.lease_token = None
        intent.lease_expires_at = None
        can_retry = retryable and intent.attempt_count < intent.max_attempts
        attempt = self._store.attempts[claim.attempt_id]
        attempt.error_code = error_code
        attempt.error_type = error_type
        attempt.completed_at = now
        if can_retry:
            intent.status = "retry_wait"
            intent.next_retry_at = retry_at
            attempt.status = "retry_wait"
            attempt.next_retry_at = retry_at
            self._store.upsert_wakeup(
                queue="publication",
                entity_id=intent.intent_id,
                next_attempt_at=retry_at,
            )
        else:
            intent.status = "failed"
            intent.next_retry_at = None
            intent.completed_at = now
            attempt.status = "failed"
            await self.complete_wakeup_if_terminal(intent_id=intent.intent_id, now=now)
        return True

    async def cancel(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        now: datetime,
    ) -> bool:
        intent = self._active(claim, now)
        if intent is None:
            return False
        intent.status = "cancelled"
        intent.lease_token = None
        intent.lease_expires_at = None
        intent.in_flight_part = None
        intent.in_flight_operation = None
        intent.last_error_code = error_code
        intent.last_error_type = error_type
        intent.completed_at = now
        attempt = self._store.attempts[claim.attempt_id]
        attempt.status = "cancelled"
        attempt.error_code = error_code
        attempt.completed_at = now
        await self.complete_wakeup_if_terminal(intent_id=intent.intent_id, now=now)
        return True

    async def complete_wakeup_if_terminal(self, *, intent_id: str, now: datetime) -> bool:
        intent = self._store.intents.get(intent_id)
        wakeup = self._store.wakeups.get(("publication", intent_id))
        if (
            intent is None
            or intent.status not in {"sent", "failed", "cancelled"}
            or wakeup is None
            or wakeup.status not in {"pending", "leased"}
        ):
            return False
        wakeup.status = "completed"
        wakeup.lease_token = None
        wakeup.lease_expires_at = None
        wakeup.completed_at = now
        return True

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int:
        repaired = 0
        for intent in list(self._store.intents.values())[:limit]:
            needs_wakeup = (
                intent.status in {"pending", "partial"}
                or (
                    intent.status == "retry_wait"
                    and intent.next_retry_at is not None
                    and intent.next_retry_at <= now
                )
                or (
                    intent.status == "sending"
                    and intent.lease_expires_at is not None
                    and intent.lease_expires_at <= now
                )
            )
            if needs_wakeup and self._store.upsert_wakeup(
                queue="publication",
                entity_id=intent.intent_id,
                next_attempt_at=now,
            ):
                repaired += 1
        return repaired


class _CleanupRepoAdapter:
    def __init__(self, store: _StatefulPostgresStore) -> None:
        self._store = store

    def _matching_ready_assets(
        self, cleanup_filter: ArchiveCleanupFilter
    ) -> tuple[_StoredAsset, ...]:
        allowed = (
            set(cleanup_filter.media_types) if cleanup_filter.media_types else {"image", "video"}
        )
        return tuple(
            asset
            for asset in self._store.assets.values()
            if asset.archive_state == "ready" and asset.asset_kind in allowed
        )

    async def create_dry_run(
        self,
        *,
        cleanup_filter: ArchiveCleanupFilter,
        actor_admin_user_id: str,
        now: datetime,
    ) -> ArchiveCleanupDryRun:
        assets = self._matching_ready_assets(cleanup_filter)
        estimate = ArchiveCleanupEstimate(
            asset_count=len(assets),
            message_count=len({asset.message_id for asset in assets}),
            bytes=sum(sum(blob.size_bytes for blob in asset.blobs) for asset in assets),
        )
        dry_run_id = str(uuid4())
        dry_run = ArchiveCleanupDryRun(
            dry_run_id=dry_run_id,
            actor_admin_user_id=actor_admin_user_id,
            filter_snapshot=cleanup_filter.snapshot(),
            filter_hash=cleanup_filter.sha256,
            estimate=estimate,
            created_at=now,
        )
        self._store.dry_runs[dry_run_id] = dry_run
        self._store.audit_events.append(
            ("archive_cleanup.dry_run_created", "archive_cleanup_dry_run", dry_run_id)
        )
        return dry_run

    async def confirm(
        self,
        *,
        dry_run_id: str,
        expected_filter_hash: str,
        confirmed_by_admin_user_id: str,
        now: datetime,
        max_attempts: int = 5,
    ) -> ConfirmedArchiveCleanupJob:
        dry_run = self._store.dry_runs[dry_run_id]
        assert dry_run.filter_hash == expected_filter_hash
        cleanup_filter = ArchiveCleanupFilter.from_snapshot(dry_run.filter_snapshot)
        assets = self._matching_ready_assets(cleanup_filter)
        job_id = str(uuid4())
        item_ids: list[str] = []
        for asset in assets:
            item_id = str(uuid4())
            item_ids.append(item_id)
            self._store.cleanup_items[item_id] = _StoredCleanupItem(
                item_id=item_id,
                job_id=job_id,
                asset_id=asset.asset_id,
                asset_kind=asset.asset_kind,
                message_id=asset.message_id,
                blobs=asset.blobs,
                previous_archive_size=sum(blob.size_bytes for blob in asset.blobs),
            )
        estimate = ArchiveCleanupEstimate(
            asset_count=len(assets),
            message_count=len({asset.message_id for asset in assets}),
            bytes=sum(sum(blob.size_bytes for blob in asset.blobs) for asset in assets),
        )
        job = _StoredCleanupJob(
            job_id=job_id,
            dry_run_id=dry_run_id,
            filter_hash=dry_run.filter_hash,
            estimate=estimate,
            item_ids=tuple(item_ids),
            max_attempts=max_attempts,
            created_at=now,
        )
        self._store.cleanup_jobs[job_id] = job
        self._store.upsert_wakeup(queue="maintenance", entity_id=job_id, next_attempt_at=now)
        self._store.audit_events.append(
            ("archive_cleanup.job_confirmed", "archive_cleanup_job", job_id)
        )
        return ConfirmedArchiveCleanupJob(
            job_id=job_id,
            dry_run_id=dry_run_id,
            filter_hash=job.filter_hash,
            estimate=estimate,
            item_count=len(item_ids),
            status="pending",
            created_at=now,
        )

    def _active_job(
        self, claim: ClaimedArchiveCleanupJob, now: datetime
    ) -> _StoredCleanupJob | None:
        job = self._store.cleanup_jobs.get(claim.job_id)
        if (
            job is None
            or job.status != "running"
            or job.lease_token != claim.lease_token
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            return None
        return job

    async def claim(
        self,
        *,
        job_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedArchiveCleanupJob | None:
        job = self._store.cleanup_jobs.get(job_id)
        if job is None:
            return None
        claimable = (
            job.status == "pending"
            or (
                job.status == "retry_wait"
                and job.next_retry_at is not None
                and job.next_retry_at <= now
            )
            or (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at <= now
            )
        )
        if not claimable:
            return None
        lease_token = str(uuid4())
        job.status = "running"
        job.attempt_count += 1
        job.next_retry_at = None
        job.lease_token = lease_token
        job.lease_expires_at = now + lease_duration
        return ClaimedArchiveCleanupJob(
            job_id=job.job_id,
            lease_token=lease_token,
            attempt_number=job.attempt_count,
            max_attempts=job.max_attempts,
        )

    def _asset_in_use(self, asset: _StoredAsset) -> bool:
        if (f"{asset.asset_kind}_asset", asset.asset_id) in self._store.running_stage_targets:
            return True
        if ("message", asset.message_id) in self._store.running_stage_targets:
            return True
        return any(
            intent.message_id == asset.message_id
            and intent.status in {"pending", "sending", "partial", "retry_wait"}
            for intent in self._store.intents.values()
        )

    async def claim_next_item(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        now: datetime,
    ) -> ClaimedCleanupItem | None:
        job = self._active_job(claim, now)
        if job is None:
            return None
        for item_id in job.item_ids:
            item = self._store.cleanup_items[item_id]
            if item.status == "deleting" and item.deletion_token is not None:
                return ClaimedCleanupItem(
                    item_id=item.item_id,
                    deletion_token=item.deletion_token,
                    asset=CleanupAsset(
                        asset_kind=item.asset_kind,
                        asset_id=item.asset_id,
                        message_id=item.message_id,
                        blobs=item.blobs,
                        previous_archive_size=item.previous_archive_size,
                    ),
                )
        for item_id in job.item_ids:
            item = self._store.cleanup_items[item_id]
            if item.status != "pending":
                continue
            asset = self._store.assets[item.asset_id]
            if asset.archive_state != "ready" or self._asset_in_use(asset):
                item.status = "skipped_in_use"
                item.skipped_at = now
                continue
            token = str(uuid4())
            item.status = "deleting"
            item.deletion_token = token
            asset.archive_state = "deleting"
            asset.cleanup_job_id = job.job_id
            asset.previous_archive_size = item.previous_archive_size
            return ClaimedCleanupItem(
                item_id=item.item_id,
                deletion_token=token,
                asset=CleanupAsset(
                    asset_kind=item.asset_kind,
                    asset_id=item.asset_id,
                    message_id=item.message_id,
                    blobs=item.blobs,
                    previous_archive_size=item.previous_archive_size,
                ),
            )
        return None

    async def finalize_item_deleted(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        item: ClaimedCleanupItem,
        now: datetime,
    ) -> bool:
        if self._store.fail_next_cleanup_finalize:
            self._store.fail_next_cleanup_finalize = False
            raise RuntimeError("simulated DB crash after archive blob deletion")
        job = self._active_job(claim, now)
        record = self._store.cleanup_items.get(item.item_id)
        if (
            job is None
            or record is None
            or record.status != "deleting"
            or record.deletion_token != item.deletion_token
        ):
            return False
        asset = self._store.assets[record.asset_id]
        if asset.archive_state != "deleting" or asset.cleanup_job_id != job.job_id:
            return False
        asset.archive_state = "deleted"
        asset.archive_deleted_at = now
        record.status = "deleted"
        record.deletion_token = None
        record.deleted_at = now
        return True

    async def complete(self, *, claim: ClaimedArchiveCleanupJob, now: datetime) -> bool:
        job = self._active_job(claim, now)
        if job is None:
            return False
        if any(
            self._store.cleanup_items[item_id].status in {"pending", "deleting"}
            for item_id in job.item_ids
        ):
            return False
        job.status = "completed"
        job.lease_token = None
        job.lease_expires_at = None
        job.completed_at = now
        await self.complete_wakeup_if_terminal(job_id=job.job_id, now=now)
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
        job = self._active_job(claim, now)
        if job is None:
            return False
        job.lease_token = None
        job.lease_expires_at = None
        if retryable and job.attempt_count < job.max_attempts:
            job.status = "retry_wait"
            job.next_retry_at = retry_at
            self._store.upsert_wakeup(
                queue="maintenance",
                entity_id=job.job_id,
                next_attempt_at=retry_at,
            )
        else:
            job.status = "failed"
            job.completed_at = now
            if item is not None:
                record = self._store.cleanup_items[item.item_id]
                record.status = "failed"
                record.deletion_token = None
                record.last_error_code = error_code
            await self.complete_wakeup_if_terminal(job_id=job.job_id, now=now)
        return True

    async def complete_wakeup_if_terminal(self, *, job_id: str, now: datetime) -> bool:
        job = self._store.cleanup_jobs.get(job_id)
        wakeup = self._store.wakeups.get(("maintenance", job_id))
        if (
            job is None
            or job.status not in {"completed", "failed", "cancelled"}
            or wakeup is None
            or wakeup.status not in {"pending", "leased"}
        ):
            return False
        wakeup.status = "completed"
        wakeup.lease_token = None
        wakeup.lease_expires_at = None
        wakeup.completed_at = now
        return True

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int:
        repaired = 0
        for job in list(self._store.cleanup_jobs.values())[:limit]:
            needs_wakeup = (
                job.status == "pending"
                or (
                    job.status == "retry_wait"
                    and job.next_retry_at is not None
                    and job.next_retry_at <= now
                )
                or (
                    job.status == "running"
                    and job.lease_expires_at is not None
                    and job.lease_expires_at <= now
                )
            )
            if needs_wakeup and self._store.upsert_wakeup(
                queue="maintenance",
                entity_id=job.job_id,
                next_attempt_at=now,
            ):
                repaired += 1
        return repaired


class _WakeupRepoAdapter:
    def __init__(self, store: _StatefulPostgresStore) -> None:
        self._store = store

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedWakeup, ...]:
        claimed: list[ClaimedWakeup] = []
        for wakeup in self._store.wakeups.values():
            if len(claimed) >= limit:
                break
            due = (wakeup.status == "pending" and wakeup.next_attempt_at <= now) or (
                wakeup.status == "leased"
                and wakeup.lease_expires_at is not None
                and wakeup.lease_expires_at <= now
            )
            if not due:
                continue
            token = str(uuid4())
            wakeup.status = "leased"
            wakeup.lease_token = token
            wakeup.lease_expires_at = now + lease_duration
            wakeup.dispatch_attempts += 1
            claimed.append(
                ClaimedWakeup(
                    wakeup_id=wakeup.wakeup_id,
                    queue=wakeup.queue,
                    entity_id=wakeup.entity_id,
                    lease_token=token,
                )
            )
        return tuple(claimed)

    async def acknowledge_dispatch(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        now: datetime,
        repair_after: datetime,
    ) -> bool:
        for wakeup in self._store.wakeups.values():
            if wakeup.wakeup_id == wakeup_id and wakeup.lease_token == lease_token:
                wakeup.status = "pending"
                wakeup.next_attempt_at = repair_after
                wakeup.lease_token = None
                wakeup.lease_expires_at = None
                return True
        return False

    async def reschedule_after_failure(
        self,
        *,
        wakeup_id: str,
        lease_token: str,
        now: datetime,
        retry_after: datetime,
    ) -> bool:
        for wakeup in self._store.wakeups.values():
            if wakeup.wakeup_id == wakeup_id and wakeup.lease_token == lease_token:
                wakeup.status = "pending"
                wakeup.next_attempt_at = retry_after
                wakeup.lease_token = None
                wakeup.lease_expires_at = None
                return True
        return False

    async def complete_for_entity(self, *, queue: str, entity_id: str, now: datetime) -> bool:
        wakeup = self._store.wakeups.get((queue, entity_id))
        if wakeup is None:
            return False
        wakeup.status = "completed"
        wakeup.lease_token = None
        wakeup.lease_expires_at = None
        wakeup.completed_at = now
        return True


class _BacklogRepoAdapter:
    def __init__(self, store: _StatefulPostgresStore) -> None:
        self._store = store

    async def observe_backlog(self, *, now: datetime) -> BacklogMetricsSnapshot:
        pub_pending = sum(
            1
            for item in self._store.intents.values()
            if item.status == "pending" and not item.completed_parts
        )
        pub_partial = sum(
            1
            for item in self._store.intents.values()
            if item.status not in {"sent", "failed", "cancelled"}
            and (item.status == "partial" or bool(item.completed_parts))
        )
        pub_retry_due = sum(
            1
            for item in self._store.intents.values()
            if (
                item.status == "retry_wait"
                and item.next_retry_at is not None
                and item.next_retry_at <= now
            )
            or (
                item.status == "sending"
                and item.lease_expires_at is not None
                and item.lease_expires_at <= now
            )
        )
        clean_pending = sum(
            1 for job in self._store.cleanup_jobs.values() if job.status == "pending"
        )
        clean_retry_due = sum(
            1
            for job in self._store.cleanup_jobs.values()
            if (
                job.status == "retry_wait"
                and job.next_retry_at is not None
                and job.next_retry_at <= now
            )
            or (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at <= now
            )
        )
        wakeups_due = sum(
            1
            for wakeup in self._store.wakeups.values()
            if (wakeup.status == "pending" and wakeup.next_attempt_at <= now)
            or (
                wakeup.status == "leased"
                and wakeup.lease_expires_at is not None
                and wakeup.lease_expires_at <= now
            )
        )
        return BacklogMetricsSnapshot(
            publication_pending=pub_pending,
            publication_partial=pub_partial,
            publication_retry_due=pub_retry_due,
            cleanup_pending=clean_pending,
            cleanup_retry_due=clean_retry_due,
            durable_wakeups_due=wakeups_due,
            observed_at=now,
        )


class _NoopRangeScheduleRepo:
    async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
        return ()

    async def create_execution_and_wakeup(
        self, *, execution: PendingRangeExecution, queue: str, available_at: datetime
    ) -> bool:
        return False


class _NoopBoundarySource:
    async def newest_message(self, *, source_channel_id: str) -> None:
        return None


class _NoopRangeWorker:
    async def claim(self, *, execution_id: str, now: datetime) -> ClaimedRangeExecution | None:
        return None


class _NoopDatabase:
    async def dispose(self) -> None:
        return None


class _SimulatedRedisBroker:
    """UUID-only broker queue modeling Redis outages, loss, and duplicate consumption."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.available = True

    async def dispatch(self, *, queue: str, entity_id: str) -> None:
        UUID(entity_id)
        if not self.available:
            raise ConnectionError("Redis broker unavailable")
        self.messages.append((queue, entity_id))

    def wipe(self) -> None:
        self.messages.clear()

    async def drain(self, worker: WorkerRuntime, *, now: datetime) -> list[bool]:
        outcomes: list[bool] = []
        while self.messages:
            queue, entity_id = self.messages.pop(0)
            if queue == "publication":
                outcomes.append(await worker.handle_publication(intent_id=entity_id, now=now))
            elif queue == "maintenance":
                outcomes.append(await worker.handle_archive_cleanup(job_id=entity_id, now=now))
            else:
                raise AssertionError(f"unexpected queue: {queue}")
        return outcomes


class _RemoteTelegramBoundary:
    """Simulated Telegram channel enforcing MTProto random_id deduplication and reconciliation."""

    def __init__(self) -> None:
        self._next_message_id = 5000
        self.by_random_ids: dict[tuple[int, ...], tuple[int, ...]] = {}
        self.delivered_log: list[tuple[PublicationPart, PublicationOperation, int | None]] = []
        self.publish_calls = 0
        self.reconcile_calls = 0
        self.injected_errors: dict[PublicationPart, list[Exception]] = {}

    def inject_error(self, part: PublicationPart, error: Exception) -> None:
        self.injected_errors.setdefault(part, []).append(error)

    async def publish(self, *, request: TelegramPublishRequest) -> TelegramPublishResult:
        self.publish_calls += 1
        pending_errors = self.injected_errors.get(request.part)
        if pending_errors:
            raise pending_errors.pop(0)
        existing = self.by_random_ids.get(request.random_ids)
        if existing is not None:
            return TelegramPublishResult(existing)
        ids = tuple(self._allocate_message_id() for _ in range(len(request.random_ids)))
        self.by_random_ids[request.random_ids] = ids
        self.delivered_log.append((request.part, request.operation, request.reply_to_message_id))
        return TelegramPublishResult(ids)

    async def reconcile(self, *, request: TelegramPublishRequest) -> TelegramPublishResult | None:
        self.reconcile_calls += 1
        existing = self.by_random_ids.get(request.random_ids)
        return TelegramPublishResult(existing) if existing is not None else None

    def _allocate_message_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id


class _InMemoryArchiveStorage:
    def __init__(self, initial_keys: tuple[str, ...] = ()) -> None:
        self.blobs: set[str] = set(initial_keys)
        self.delete_calls: list[str] = []
        self.fail_once_keys: set[str] = set()

    async def delete(self, *, key: str) -> None:
        self.delete_calls.append(key)
        if key in self.fail_once_keys:
            self.fail_once_keys.remove(key)
            raise OSError(f"transient storage error on {key}")
        self.blobs.discard(key)


def _compose_stack(
    *,
    store: _StatefulPostgresStore,
    broker: _SimulatedRedisBroker,
    telegram: _RemoteTelegramBoundary,
    storage: _InMemoryArchiveStorage,
    monitor: OperationsMonitor | None = None,
) -> tuple[SchedulerRuntime, WorkerRuntime, OperationsMonitor]:
    telemetry = monitor or OperationsMonitor()
    pub_repo = _PublicationRepoAdapter(store)
    clean_repo = _CleanupRepoAdapter(store)
    scheduler = SchedulerRuntime(
        database=_NoopDatabase(),  # type: ignore[arg-type]
        range_scheduler=ProcessingRangeScheduler(
            repository=_NoopRangeScheduleRepo(),
            latest_boundary_source=_NoopBoundarySource(),
        ),
        wakeup_dispatcher=DurableWakeupDispatcher(
            repository=_WakeupRepoAdapter(store),
            task_dispatcher=broker,
            failure_retry_delay=timedelta(seconds=30),
        ),
        publication_repository=pub_repo,
        cleanup_repository=clean_repo,
        backlog_repository=_BacklogRepoAdapter(store),
        telemetry=telemetry,
    )
    worker = WorkerRuntime(
        database=_NoopDatabase(),  # type: ignore[arg-type]
        range_execution_worker=_NoopRangeWorker(),  # type: ignore[arg-type]
        publication_worker=PublicationWorker(
            repository=pub_repo,
            publisher=telegram,
            telemetry=telemetry,
            lease_duration=timedelta(minutes=5),
            failure_retry_delay=timedelta(minutes=1),
        ),
        cleanup_worker=ArchiveCleanupWorker(
            repository=clean_repo,
            storage_resolver=MappingArchiveStorageResolver({"archive": storage}),
            lease_duration=timedelta(minutes=5),
            failure_retry_delay=timedelta(minutes=1),
        ),
        telemetry=telemetry,
    )
    return scheduler, worker, telemetry


class M6FailureInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_1_send_succeeds_before_db_commit_recovers_via_reconciliation(self) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        telegram = _RemoteTelegramBoundary()
        storage = _InMemoryArchiveStorage()
        scheduler, worker, monitor = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )

        message_id = store.seed_message(telegram_message_ids=(101, 102))
        routing = RoutingService(_RoutingRepoAdapter(store))
        routed = await routing.evaluate(
            RoutingRequest(
                request_id="req-reconcile",
                message_id=message_id,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id

        # Scheduler enqueues the wake-up; remote MAIN send succeeds, then DB commit crashes!
        await scheduler.run_once(now=NOW)
        store.fail_next_record_part = True
        await broker.drain(worker, now=NOW)

        intent = store.intents[intent_id]
        self.assertEqual(len(telegram.delivered_log), 1)
        self.assertEqual(intent.completed_parts, [])
        self.assertEqual(intent.in_flight_part, PublicationPart.MAIN)
        self.assertEqual(intent.status, "retry_wait")

        # After retry delay, worker reconciles MAIN without resending it, then sends SUPPLEMENT.
        retry_time = NOW + timedelta(minutes=1)
        await scheduler.run_once(now=retry_time)
        outcomes = await broker.drain(worker, now=retry_time)

        self.assertEqual(outcomes, [True])
        self.assertEqual(intent.status, "sent")
        self.assertEqual(telegram.reconcile_calls, 1)
        # Only 1 MAIN delivery + 1 SUPPLEMENT delivery ever reached Telegram!
        self.assertEqual(
            telegram.delivered_log,
            [
                (PublicationPart.MAIN, PublicationOperation.NATIVE_FORWARD, None),
                (PublicationPart.SUPPLEMENT, PublicationOperation.SEND_TEXT, 5001),
            ],
        )
        self.assertTrue(store.publish_records[0].reconciled)
        self.assertFalse(store.publish_records[1].reconciled)
        snapshot = monitor.snapshot(now=retry_time)
        self.assertEqual(snapshot.publication.reconciled_parts, 1)
        self.assertEqual(snapshot.publication.sent, 1)

    async def test_2_intent_committed_before_broker_enqueue_is_recovered_by_scheduler(self) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        broker.available = False
        telegram = _RemoteTelegramBoundary()
        storage = _InMemoryArchiveStorage()
        scheduler, worker, monitor = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )

        message_id = store.seed_message()
        routed = await RoutingService(
            _RoutingRepoAdapter(store, mode=PublicationMode.COPY_WITH_CAPTION)
        ).evaluate(
            RoutingRequest(
                request_id="req-broker-down",
                message_id=message_id,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id

        # Broker is down during initial scheduler cycle: wakeup is rescheduled, not lost.
        first_cycle = await scheduler.run_once(now=NOW)
        self.assertEqual(first_cycle.wakeup_dispatch.failed_dispatches, 1)
        self.assertEqual(broker.messages, [])
        self.assertFalse(monitor.scheduler_health(now=NOW).ready)
        self.assertIn("broker_unavailable", monitor.scheduler_health(now=NOW).blockers)

        # Broker recovers; scheduler dispatches the durable wakeup and worker completes publication.
        broker.available = True
        later = NOW + timedelta(seconds=31)
        second_cycle = await scheduler.run_once(now=later)
        self.assertEqual(second_cycle.wakeup_dispatch.dispatched_wakeups, 1)
        self.assertTrue(monitor.scheduler_health(now=later).ready)
        await broker.drain(worker, now=later)
        self.assertEqual(store.intents[intent_id].status, "sent")

    async def test_3_redis_and_wakeup_loss_reconstructs_publication_and_cleanup_from_db(
        self,
    ) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        telegram = _RemoteTelegramBoundary()

        pub_msg = store.seed_message()
        clean_msg = store.seed_message()
        asset = store.seed_asset(message_id=clean_msg)
        storage = _InMemoryArchiveStorage((asset.blobs[0].storage_key,))
        scheduler, worker, _ = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )

        routed = await RoutingService(
            _RoutingRepoAdapter(store, mode=PublicationMode.FORWARD_ONLY)
        ).evaluate(
            RoutingRequest(
                request_id="req-redis-loss",
                message_id=pub_msg,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id

        cleanup_service = ArchiveCleanupService(repository=_CleanupRepoAdapter(store))
        dry_run = await cleanup_service.dry_run(
            cleanup_filter=ArchiveCleanupFilter(media_types=("image",)),
            actor_admin_user_id=ADMIN_ID,
            now=NOW,
        )
        confirmed = await cleanup_service.confirm(
            dry_run_id=dry_run.dry_run_id,
            expected_filter_hash=dry_run.filter_hash,
            confirmed_by_admin_user_id=ADMIN_ID,
            now=NOW,
        )

        # Dispatch into Redis, then catastrophically wipe Redis AND wakeup rows.
        await scheduler.run_once(now=NOW)
        self.assertEqual(len(broker.messages), 2)
        broker.wipe()
        store.wipe_wakeups()

        # Scheduler repairs UUID-only wakeups solely from PostgreSQL business truth.
        repair_time = NOW + timedelta(minutes=2)
        report = await scheduler.run_once(now=repair_time)
        self.assertEqual(report.repaired_publication_wakeups, 1)
        self.assertEqual(report.repaired_cleanup_wakeups, 1)
        self.assertEqual(len(broker.messages), 2)

        await broker.drain(worker, now=repair_time)
        self.assertEqual(store.intents[intent_id].status, "sent")
        self.assertEqual(store.cleanup_jobs[confirmed.job_id].status, "completed")
        self.assertEqual(store.assets[asset.asset_id].archive_state, "deleted")

    async def test_4_duplicate_consumption_is_idempotent_during_and_after_execution(self) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        telegram = _RemoteTelegramBoundary()
        storage = _InMemoryArchiveStorage()
        scheduler, worker, _ = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )

        msg_id = store.seed_message()
        routed = await RoutingService(
            _RoutingRepoAdapter(store, mode=PublicationMode.FORWARD_ONLY)
        ).evaluate(
            RoutingRequest(
                request_id="req-dup",
                message_id=msg_id,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id

        # Simulate active lease held by Worker A while Worker B receives a duplicate UUID.
        pub_repo = _PublicationRepoAdapter(store)
        active_claim = await pub_repo.claim(
            intent_id=intent_id, now=NOW, lease_duration=timedelta(minutes=5)
        )
        assert active_claim is not None
        concurrent = await worker.handle_publication(
            intent_id=intent_id, now=NOW + timedelta(seconds=10)
        )
        self.assertFalse(concurrent)
        self.assertEqual(len(store.attempts), 1)
        self.assertEqual(telegram.publish_calls, 0)

        # Release Worker A cleanly, then deliver the same UUID three times in the broker.
        self.assertTrue(await pub_repo.release(claim=active_claim, now=NOW + timedelta(seconds=20)))
        await scheduler.run_once(now=NOW + timedelta(seconds=20))
        broker.messages.append(("publication", intent_id))
        broker.messages.append(("publication", intent_id))
        outcomes = await broker.drain(worker, now=NOW + timedelta(seconds=21))

        self.assertEqual(outcomes, [True, False, False])
        self.assertEqual(telegram.publish_calls, 1)
        self.assertEqual(store.wakeups[("publication", intent_id)].status, "completed")

    async def test_5_expired_publication_and_cleanup_leases_reject_stale_tokens(self) -> None:
        store = _StatefulPostgresStore()
        pub_repo = _PublicationRepoAdapter(store)
        clean_repo = _CleanupRepoAdapter(store)

        msg_id = store.seed_message()
        routed = await RoutingService(
            _RoutingRepoAdapter(store, mode=PublicationMode.COPY_WITH_CAPTION)
        ).evaluate(
            RoutingRequest(
                request_id="req-lease-race",
                message_id=msg_id,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id

        clean_msg = store.seed_message()
        asset = store.seed_asset(message_id=clean_msg)
        cleanup_service = ArchiveCleanupService(repository=clean_repo)
        dry_run = await cleanup_service.dry_run(
            cleanup_filter=ArchiveCleanupFilter(media_types=("image",)),
            actor_admin_user_id=ADMIN_ID,
            now=NOW,
        )
        confirmed = await cleanup_service.confirm(
            dry_run_id=dry_run.dry_run_id,
            expected_filter_hash=dry_run.filter_hash,
            confirmed_by_admin_user_id=ADMIN_ID,
            now=NOW,
        )

        # Worker A claims both leases, then stalls past lease_duration.
        stale_pub_claim = await pub_repo.claim(
            intent_id=intent_id, now=NOW, lease_duration=timedelta(minutes=5)
        )
        stale_clean_claim = await clean_repo.claim(
            job_id=confirmed.job_id, now=NOW, lease_duration=timedelta(minutes=5)
        )
        assert stale_pub_claim is not None and stale_clean_claim is not None
        stale_item = await clean_repo.claim_next_item(claim=stale_clean_claim, now=NOW)
        assert stale_item is not None

        # After lease expiry, Worker B takes over both leases.
        takeover_at = NOW + timedelta(minutes=6)
        fresh_pub_claim = await pub_repo.claim(
            intent_id=intent_id, now=takeover_at, lease_duration=timedelta(minutes=5)
        )
        fresh_clean_claim = await clean_repo.claim(
            job_id=confirmed.job_id, now=takeover_at, lease_duration=timedelta(minutes=5)
        )
        assert fresh_pub_claim is not None and fresh_clean_claim is not None
        self.assertEqual(
            store.attempts[stale_pub_claim.attempt_id].error_code,
            "PUBLICATION_LEASE_EXPIRED",
        )

        # Zombie Worker A wakes up and every mutation with its stale token is rejected.
        self.assertFalse(
            await pub_repo.begin_part(
                claim=stale_pub_claim,
                part=PublicationPart.MAIN,
                operation=PublicationOperation.COPY_WITH_CAPTION,
                now=takeover_at,
            )
        )
        self.assertFalse(await pub_repo.complete(claim=stale_pub_claim, now=takeover_at))
        self.assertFalse(
            await clean_repo.finalize_item_deleted(
                claim=stale_clean_claim,
                item=stale_item,
                now=takeover_at,
            )
        )

        # Fresh Worker B completes both cleanly.
        replayed_item = await clean_repo.claim_next_item(claim=fresh_clean_claim, now=takeover_at)
        assert replayed_item is not None
        self.assertTrue(
            await clean_repo.finalize_item_deleted(
                claim=fresh_clean_claim,
                item=replayed_item,
                now=takeover_at,
            )
        )
        self.assertTrue(await clean_repo.complete(claim=fresh_clean_claim, now=takeover_at))
        self.assertEqual(store.assets[asset.asset_id].archive_state, "deleted")

    async def test_6_worker_and_scheduler_restart_recovers_from_postgresql_truth(self) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        telegram = _RemoteTelegramBoundary()
        storage = _InMemoryArchiveStorage()

        msg_id = store.seed_message()
        routed = await RoutingService(_RoutingRepoAdapter(store)).evaluate(
            RoutingRequest(
                request_id="req-restart",
                message_id=msg_id,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id

        # Generation 1 claims intent, begins MAIN, sends remotely, then process dies before commit!
        _, worker_gen1, monitor_gen1 = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )
        store.fail_next_record_part = True
        await worker_gen1.handle_publication(intent_id=intent_id, now=NOW)
        self.assertEqual(monitor_gen1.snapshot(now=NOW).publication.claimed, 1)

        # Generation 2 starts with brand-new SchedulerRuntime, WorkerRuntime, and OperationsMonitor.
        broker.wipe()
        restart_at = NOW + timedelta(minutes=10)
        scheduler_gen2, worker_gen2, monitor_gen2 = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )
        await scheduler_gen2.run_once(now=restart_at)
        self.assertEqual(monitor_gen2.snapshot(now=restart_at).backlog.publication_retry_due, 1)

        await broker.drain(worker_gen2, now=restart_at)
        self.assertEqual(store.intents[intent_id].status, "sent")
        final_snapshot = monitor_gen2.snapshot(now=restart_at + timedelta(seconds=1))
        self.assertEqual(final_snapshot.publication.reconciled_parts, 1)
        self.assertEqual(final_snapshot.publication.sent, 1)
        self.assertTrue(final_snapshot.worker.ready)
        self.assertTrue(final_snapshot.scheduler.ready)

    async def test_7_partial_publication_retries_only_missing_supplement_without_duplicate_forward(
        self,
    ) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        telegram = _RemoteTelegramBoundary()
        storage = _InMemoryArchiveStorage()
        scheduler, worker, monitor = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )

        msg_id = store.seed_message(telegram_message_ids=(301, 302))
        routed = await RoutingService(_RoutingRepoAdapter(store)).evaluate(
            RoutingRequest(
                request_id="req-partial",
                message_id=msg_id,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id

        # SUPPLEMENT fails transiently after MAIN forward succeeds and commits its PublishRecord.
        telegram.inject_error(
            PublicationPart.SUPPLEMENT,
            TelegramPublicationError("TransientNetworkError"),
        )
        await scheduler.run_once(now=NOW)
        await broker.drain(worker, now=NOW)

        intent = store.intents[intent_id]
        self.assertEqual(intent.completed_parts, [PublicationPart.MAIN])
        self.assertEqual(intent.status, "retry_wait")
        self.assertEqual(len(store.publish_records), 1)
        self.assertEqual(store.publish_records[0].part, PublicationPart.MAIN)

        # Observe backlog while partially completed intent awaits supplement retry.
        await scheduler.run_once(now=NOW + timedelta(seconds=10))
        self.assertEqual(
            monitor.snapshot(now=NOW + timedelta(seconds=10)).backlog.publication_partial,
            1,
        )

        # Retry after failure_retry_delay sends ONLY the supplement replying to the first forward.
        retry_at = NOW + timedelta(minutes=1)
        await scheduler.run_once(now=retry_at)
        await broker.drain(worker, now=retry_at)

        self.assertEqual(intent.status, "sent")
        self.assertEqual(len(store.publish_records), 2)
        self.assertEqual(
            telegram.delivered_log,
            [
                (PublicationPart.MAIN, PublicationOperation.NATIVE_FORWARD, None),
                (PublicationPart.SUPPLEMENT, PublicationOperation.SEND_TEXT, 5001),
            ],
        )

    async def test_8_flood_wait_persists_exact_retry_at_without_worker_sleep_or_early_claim(
        self,
    ) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        telegram = _RemoteTelegramBoundary()
        storage = _InMemoryArchiveStorage()
        scheduler, worker, monitor = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )

        msg_id = store.seed_message()
        routed = await RoutingService(
            _RoutingRepoAdapter(store, mode=PublicationMode.FORWARD_ONLY)
        ).evaluate(
            RoutingRequest(
                request_id="req-floodwait",
                message_id=msg_id,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )
        intent_id = routed.intents[0].intent_id
        flood_until = NOW + timedelta(hours=4, minutes=15)
        telegram.inject_error(PublicationPart.MAIN, TelegramFloodWait(retry_at=flood_until))

        await scheduler.run_once(now=NOW)
        await broker.drain(worker, now=NOW)

        intent = store.intents[intent_id]
        self.assertEqual(intent.status, "retry_wait")
        self.assertEqual(intent.next_retry_at, flood_until)
        self.assertEqual(store.wakeups[("publication", intent_id)].next_attempt_at, flood_until)
        self.assertEqual(monitor.snapshot(now=NOW).flood_wait.wait_seconds_max, 15300.0)

        # Before flood_until: scheduler dispatches nothing, and even a direct worker wakeup no-ops.
        early = flood_until - timedelta(seconds=1)
        early_report = await scheduler.run_once(now=early)
        self.assertEqual(early_report.wakeup_dispatch.claimed_wakeups, 0)
        self.assertFalse(await worker.handle_publication(intent_id=intent_id, now=early))

        # At flood_until: scheduler dispatches and worker delivers cleanly.
        due_report = await scheduler.run_once(now=flood_until)
        self.assertEqual(due_report.wakeup_dispatch.dispatched_wakeups, 1)
        await broker.drain(worker, now=flood_until)
        self.assertEqual(intent.status, "sent")

    async def test_9_cleanup_usage_races_skip_active_assets_and_replay_post_delete_crash(
        self,
    ) -> None:
        store = _StatefulPostgresStore()
        broker = _SimulatedRedisBroker()
        telegram = _RemoteTelegramBoundary()

        msg_in_analysis = store.seed_message()
        msg_in_publish = store.seed_message()
        msg_free = store.seed_message()

        img_analysis = store.seed_asset(message_id=msg_in_analysis, asset_kind="image")
        img_publish = store.seed_asset(message_id=msg_in_publish, asset_kind="image")
        video_free = store.seed_asset(
            message_id=msg_free,
            asset_kind="video",
            blobs=(
                CleanupBlob("archive", "video/cover.webp", 64),
                CleanupBlob("archive", "video/rep_0.webp", 96),
            ),
        )
        all_keys = (
            img_analysis.blobs[0].storage_key,
            img_publish.blobs[0].storage_key,
            "video/cover.webp",
            "video/rep_0.webp",
        )
        storage = _InMemoryArchiveStorage(all_keys)
        scheduler, worker, _ = _compose_stack(
            store=store, broker=broker, telegram=telegram, storage=storage
        )

        # Step 1: Dry-run and confirm when all 3 assets are idle & READY.
        cleanup_service = ArchiveCleanupService(repository=_CleanupRepoAdapter(store))
        dry_run = await cleanup_service.dry_run(
            cleanup_filter=ArchiveCleanupFilter(media_types=("image", "video")),
            actor_admin_user_id=ADMIN_ID,
            now=NOW,
        )
        self.assertEqual(dry_run.estimate.asset_count, 3)
        confirmed = await cleanup_service.confirm(
            dry_run_id=dry_run.dry_run_id,
            expected_filter_hash=dry_run.filter_hash,
            confirmed_by_admin_user_id=ADMIN_ID,
            now=NOW,
        )

        # Step 2: Concurrent work begins BEFORE cleanup worker claims items!
        store.running_stage_targets.add(("image_asset", img_analysis.asset_id))
        await RoutingService(_RoutingRepoAdapter(store)).evaluate(
            RoutingRequest(
                request_id="req-concurrent-pub",
                message_id=msg_in_publish,
                routing_policy_version_id=POLICY_VERSION_ID,
            )
        )

        # Step 3: Inject crash AFTER video blobs are deleted from storage, BEFORE DB finalize!
        store.fail_next_cleanup_finalize = True
        await scheduler.run_once(now=NOW)
        # Drain only the maintenance task first to isolate the cleanup race & crash.
        maintenance_tasks = [item for item in broker.messages if item[0] == "maintenance"]
        broker.messages = [item for item in broker.messages if item[0] != "maintenance"]
        for _, job_id in maintenance_tasks:
            await worker.handle_archive_cleanup(job_id=job_id, now=NOW)

        # In-use assets stay untouched in storage; video_free blobs were deleted and retry awaits.
        self.assertIn(img_analysis.blobs[0].storage_key, storage.blobs)
        self.assertIn(img_publish.blobs[0].storage_key, storage.blobs)
        self.assertNotIn("video/cover.webp", storage.blobs)
        self.assertNotIn("video/rep_0.webp", storage.blobs)
        self.assertEqual(store.cleanup_jobs[confirmed.job_id].status, "retry_wait")
        self.assertEqual(store.assets[video_free.asset_id].archive_state, "deleting")

        # Step 4: Retry replays the deleting item idempotently and completes the job, preserving
        # hashes, visual fingerprints, messages, and audit trail!
        retry_at = NOW + timedelta(minutes=1)
        await scheduler.run_once(now=retry_at)
        for queue, entity_id in list(broker.messages):
            if queue == "maintenance":
                broker.messages.remove((queue, entity_id))
                self.assertTrue(await worker.handle_archive_cleanup(job_id=entity_id, now=retry_at))

        self.assertEqual(store.cleanup_jobs[confirmed.job_id].status, "completed")
        self.assertEqual(store.assets[img_analysis.asset_id].archive_state, "ready")
        self.assertEqual(store.assets[img_publish.asset_id].archive_state, "ready")
        deleted_video = store.assets[video_free.asset_id]
        self.assertEqual(deleted_video.archive_state, "deleted")
        self.assertEqual(deleted_video.cleanup_job_id, confirmed.job_id)
        self.assertEqual(deleted_video.previous_archive_size, 160)
        self.assertEqual(deleted_video.source_sha256, "a" * 64)
        self.assertEqual(deleted_video.archive_sha256, "b" * 64)
        self.assertEqual(deleted_video.perceptual_hash, "f0f0f0f0f0f0f0f0")
        self.assertEqual(store.visual_fingerprints[msg_free], "c" * 64)
        self.assertEqual(len(store.audit_events), 2)


if __name__ == "__main__":
    unittest.main()
