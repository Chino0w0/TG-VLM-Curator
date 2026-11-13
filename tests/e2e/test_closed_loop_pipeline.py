from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from PIL import Image

from apps.api import create_app
from apps.scheduler.runtime import SchedulerRuntime
from tgcurator.application import Settings
from tgcurator.application.admin import (
    AdminApplicationService,
    AdminSessionService,
    InMemoryAdminBackend,
    InMemorySecretVault,
    PromptTestBenchService,
)
from tgcurator.application.analysis import (
    AnalysisOrchestrator,
    AnalysisTargetInput,
)
from tgcurator.application.cleanup import ArchiveCleanupService, ArchiveCleanupWorker
from tgcurator.application.maintenance import StorageConsistencyScanner
from tgcurator.application.media import ImageArchiveService, ImageArchiveWorker
from tgcurator.application.ports.analysis import InferenceRequest, InferenceResponse
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
    cleanup_filter_hash,
)
from tgcurator.application.ports.consistency import ExpectedArchiveBlob
from tgcurator.application.ports.media import (
    ClaimedImageArchive,
    ImageArchiveReadyMetadata,
    ImageArchiveWorkItem,
    ImageNormalizationProfile,
    TelegramMediaDownloadRequest,
)
from tgcurator.application.ports.processing import (
    ClaimedWakeup,
    LatestMessageBoundary,
    PendingRangeExecution,
    ScheduledProcessingRange,
)
from tgcurator.application.ports.publishing import (
    ClaimedPublicationIntent,
    TelegramFloodWait,
    TelegramPublishRequest,
    TelegramPublishResult,
)
from tgcurator.application.ports.routing import (
    PublicationIntentDraft,
    RoutingExecutionResult,
    RoutingRequest,
    RoutingSnapshot,
)
from tgcurator.application.processing import (
    DurableWakeupDispatcher,
    ProcessingRangeScheduler,
)
from tgcurator.application.publishing import PublicationWorker
from tgcurator.application.review import ReviewService
from tgcurator.application.routing import RoutingService
from tgcurator.domain.analysis import (
    AnalysisRunStatus,
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    ExistingNodeTargetRun,
    InferenceProfileVersion,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    MediaAssetTarget,
    MessageMediaReadiness,
    PipelineNodeActionKind,
    PipelineNodeSpec,
    PromptTemplateVersion,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputPolicy,
    VersionStatus,
    VisualCompositionPolicy,
    evaluate_run_if,
    plan_pipeline_progression,
)
from tgcurator.domain.messages import (
    DuplicateMatchKind,
    DuplicatePolicy,
    DuplicateStatus,
    MediaAsset,
    MediaKind,
    TelegramMessage,
    VisualCandidate,
    compute_archived_visual_identity,
    evaluate_visual_duplicate,
    normalize_telegram_messages,
)
from tgcurator.domain.processing import (
    WindowMessageProgress,
    compute_continuous_watermark,
)
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
    stable_random_id,
    stable_random_ids,
)
from tgcurator.domain.review import (
    LabelValue,
    ManualLabelEvent,
    MessageReviewEvent,
    ReviewStatus,
    resolve_label_namespaces,
)
from tgcurator.domain.routing import PublicationAction, RoutingPolicy, RoutingRule
from tgcurator.infrastructure.archive import LocalArchiveStorage
from tgcurator.infrastructure.media import PillowImageProcessor
from tgcurator.infrastructure.security import Argon2idPasswordHasher

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _render_synthetic_png(*, pattern: str) -> bytes:
    """Create deterministic distinct visual content for Pillow pHash & WebP normalization."""

    img = Image.new("RGB", (64, 64), (240, 240, 240))
    for y in range(64):
        for x in range(64):
            if pattern == "diagonal":
                val = 30 if (x + y) % 16 < 8 else 225
                img.putpixel((x, y), (val, 80, 255 - val))
            elif pattern == "quadrant":
                val = 20 if (x < 32) ^ (y < 32) else 235
                img.putpixel((x, y), (val, val, 255 - val))
            else:
                val = int((x / 63.0) * 255)
                img.putpixel((x, y), (val, int((y / 63.0) * 255), 120))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeTelegramAdapter:
    """Deterministic Telegram source/downloader/publisher test harness (Section 45)."""

    def __init__(self) -> None:
        self.boundaries: dict[str, LatestMessageBoundary | None] = {}
        self.media_blobs: dict[tuple[str, int, str], bytes] = {}
        self.download_requests: list[TelegramMediaDownloadRequest] = []
        self.publish_requests: list[TelegramPublishRequest] = []
        self.reconcile_requests: list[TelegramPublishRequest] = []
        self._remote_by_random_key: dict[tuple[str, tuple[int, ...]], tuple[int, ...]] = {}
        self._next_remote_id = 5000
        self.flood_wait_once_intents: set[str] = set()
        self.drop_after_remote_send_parts: set[tuple[str, PublicationPart]] = set()

    async def newest_message(self, *, source_channel_id: str) -> LatestMessageBoundary | None:
        return self.boundaries.get(source_channel_id)

    async def download(self, *, request: TelegramMediaDownloadRequest) -> bytes:
        self.download_requests.append(request)
        key = (request.source_channel_id, request.telegram_message_id, request.source_asset_id)
        if key not in self.media_blobs:
            raise KeyError(f"missing fake telegram blob for {key!r}")
        return self.media_blobs[key]

    async def publish(self, *, request: TelegramPublishRequest) -> TelegramPublishResult:
        self.publish_requests.append(request)
        if request.intent_id in self.flood_wait_once_intents:
            self.flood_wait_once_intents.remove(request.intent_id)
            raise TelegramFloodWait(retry_at=NOW + timedelta(seconds=45))

        dedup_key = (
            str(request.destination_peer_id),
            request.random_ids or (hash((request.intent_id, request.part.value)),),
        )
        remote_ids = self._remote_by_random_key.get(dedup_key)
        if remote_ids is None:
            count = (
                max(1, len(request.source_message_ids))
                if request.operation is PublicationOperation.NATIVE_FORWARD
                else 1
            )
            remote_ids = tuple(range(self._next_remote_id, self._next_remote_id + count))
            self._next_remote_id += count
            self._remote_by_random_key[dedup_key] = remote_ids

        crash_marker = (request.intent_id, request.part)
        if crash_marker in self.drop_after_remote_send_parts:
            self.drop_after_remote_send_parts.remove(crash_marker)
            raise RuntimeError("simulated network/worker crash after remote Telegram commit")

        return TelegramPublishResult(remote_message_ids=remote_ids)

    async def reconcile(self, *, request: TelegramPublishRequest) -> TelegramPublishResult | None:
        self.reconcile_requests.append(request)
        dedup_key = (
            str(request.destination_peer_id),
            request.random_ids or (hash((request.intent_id, request.part.value)),),
        )
        remote_ids = self._remote_by_random_key.get(dedup_key)
        if remote_ids is None:
            return None
        return TelegramPublishResult(remote_message_ids=remote_ids)


class FakeInferenceProvider:
    """Deterministic OpenAI-compatible VLM/LLM provider harness without bundling models."""

    def __init__(self) -> None:
        self.requests: list[InferenceRequest] = []
        self.corrupt_asset_once: set[str] = set()

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        content = request.input_manifest.content
        mode = ExecutionMode(content["execution_mode"])
        target_ids = list(content["target_ids"])

        if mode is ExecutionMode.BATCH_MESSAGES:
            items = content["inputs"].get("targets", [])
            results: list[dict[str, Any]] = []
            for entry in items:
                msg_id = entry["message_id"]
                text = (entry.get("text") or "").lower()
                is_spam = "casino" in text or "promo spam" in text
                results.append(
                    {
                        "message_id": msg_id,
                        "scores": {
                            "curated_topic": 0.08 if is_spam else 0.88,
                            "spam_ad": 0.96 if is_spam else 0.03,
                        },
                    }
                )
            payload: dict[str, Any] = {"results": results}
        elif mode is ExecutionMode.BATCH_ASSETS:
            results = []
            for asset_id in target_ids:
                if asset_id in self.corrupt_asset_once:
                    self.corrupt_asset_once.remove(asset_id)
                    # Out-of-range score > 1.0 triggers partial target retry isolation
                    results.append(
                        {
                            "asset_id": asset_id,
                            "scores": {"illustration": 1.75, "nsfw_block": 0.01},
                        }
                    )
                else:
                    results.append(
                        {
                            "asset_id": asset_id,
                            "scores": {"illustration": 0.93, "nsfw_block": 0.02},
                        }
                    )
            payload = {"results": results}
        elif mode is ExecutionMode.PER_ASSET:
            payload = {
                "result": {
                    "scores": {"illustration": 0.94, "nsfw_block": 0.01},
                }
            }
        else:
            schema_props = (
                request.response_schema.get("properties", {})
                .get("result", {})
                .get("properties", {})
                .get("scores", {})
                .get("properties", {})
            )
            scores = {
                key: (0.02 if "spam" in key or "block" in key else 0.91) for key in schema_props
            }
            payload = {"result": {"scores": scores}}

        return InferenceResponse(
            payload=payload,
            provider_adapter=request.profile.provider_adapter,
            model_name=request.profile.model_name,
            raw_response={"id": f"fake-cmpl-{len(self.requests)}", "object": "chat.completion"},
            latency_ms=9.5,
            http_status=200,
            token_usage={"prompt_tokens": 64, "completion_tokens": 24, "total_tokens": 88},
        )


def _build_stage(
    *,
    stage_id: str,
    scope: LabelScope,
    execution_mode: ExecutionMode,
    labels: tuple[tuple[str, bool, float], ...],
    include_text: bool,
    include_media: bool,
    prompt_version_id: str = "prompt-v1",
    cache_policy: CachePolicy | None = None,
) -> AnalysisStageTemplateVersion:
    bindings = tuple(
        LabelBinding(
            LabelDefinitionVersion(
                version_id=f"{stage_id}-lbl-{key}-v1",
                definition_id=f"lbl-{key}",
                version_number=1,
                key=key,
                display_name=key.replace("_", " ").title(),
                description=f"Definition for {key}",
                scope=scope,
                negative=is_negative,
                status=VersionStatus.PUBLISHED,
            ),
            threshold,
            idx,
        )
        for idx, (key, is_negative, threshold) in enumerate(labels)
    )
    label_set = LabelSetVersion(
        version_id=f"{stage_id}-labelset-v1",
        label_set_id=f"{stage_id}-labelset",
        version_number=1,
        bindings=bindings,
        status=VersionStatus.PUBLISHED,
    )
    variables: list[str] = ["label_definitions"]
    user_tpl_parts: list[str] = []
    if include_text:
        variables.append("message_text")
        user_tpl_parts.append("Text: {{ message_text }}")
    if include_media:
        variables.append("asset_manifest")
        user_tpl_parts.append("Assets: {{ asset_manifest }}")
    if not user_tpl_parts:
        user_tpl_parts.append("Evaluate labels")

    prompt = PromptTemplateVersion(
        version_id=f"{stage_id}-{prompt_version_id}",
        prompt_template_id=f"{stage_id}-prompt",
        version_number=1 if prompt_version_id.endswith("v1") else 2,
        system_prompt="Schema labels: {{ label_definitions }}",
        user_prompt_template=" | ".join(user_tpl_parts),
        declared_variables=tuple(variables),
        status=VersionStatus.PUBLISHED,
    )
    profile = InferenceProfileVersion(
        version_id="inf-profile-v1",
        inference_profile_id="inf-profile",
        version_number=1,
        provider_adapter="openai_compatible",
        base_url="https://vlm.external.example/v1",
        model_name="qwen2.5-vl-72b",
        api_secret_reference="sec-vlm-key",
        capabilities=("text", "vision"),
        timeout_seconds=30,
        max_concurrency=4,
        max_images=8,
        max_input_tokens=8192,
        structured_output_support=True,
        sampling_parameters={"temperature": 0},
        status=VersionStatus.PUBLISHED,
    )
    resolved_cache = cache_policy or (
        CachePolicy.ASSET if scope is LabelScope.MEDIA else CachePolicy.MESSAGE
    )
    return AnalysisStageTemplateVersion(
        version_id=f"{stage_id}-v1",
        stage_template_id=stage_id,
        version_number=1,
        name=stage_id,
        target_scope=scope,
        execution_mode=execution_mode,
        prompt=prompt,
        label_set=label_set,
        structured_output_policy=StructuredOutputPolicy(),
        input_policy=StageInputPolicy(text=include_text, media=include_media),
        visual_composition_policy=VisualCompositionPolicy.ADAPTIVE,
        inference_profile=profile,
        cache_policy=resolved_cache,
        timeout_seconds=30,
        max_batch_size=(
            8 if execution_mode in {ExecutionMode.BATCH_MESSAGES, ExecutionMode.BATCH_ASSETS} else 1
        ),
        retry_policy=StageRetryPolicy(),
        status=VersionStatus.PUBLISHED,
    )


@dataclass
class _InMemoryImageStore:
    items: dict[str, ImageArchiveWorkItem] = field(default_factory=dict)
    message_id_by_asset: dict[str, str] = field(default_factory=dict)
    ready_metadata: dict[str, ImageArchiveReadyMetadata] = field(default_factory=dict)
    cleaned_ids: set[str] = field(default_factory=set)

    async def claim(
        self, *, image_asset_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedImageArchive | None:
        _ = (now, lease_duration)
        if image_asset_id in self.ready_metadata or image_asset_id not in self.items:
            return None
        return ClaimedImageArchive(
            work_item=self.items[image_asset_id],
            lease_token=str(uuid4()),
        )

    async def complete_wakeup_if_terminal(self, *, image_asset_id: str, now: datetime) -> bool:
        _ = (image_asset_id, now)
        return True

    async def mark_ready(
        self,
        *,
        metadata: ImageArchiveReadyMetadata,
        claim: ClaimedImageArchive | None = None,
    ) -> bool:
        _ = claim
        self.ready_metadata[metadata.image_asset_id] = metadata
        return True

    async def fail(self, **kwargs: Any) -> bool:
        _ = kwargs
        return True

    async def list_archive_blob_records(
        self, *, storage_backend: str
    ) -> tuple[ExpectedArchiveBlob, ...]:
        return tuple(
            ExpectedArchiveBlob(
                entity_kind="image_asset",
                entity_id=asset_id,
                storage_backend=meta.storage_backend,
                storage_key=meta.storage_key,
                archive_state="cleaned" if asset_id in self.cleaned_ids else "ready",
                expected_size_bytes=meta.archive_size_bytes,
                expected_sha256=meta.archive_sha256,
            )
            for asset_id, meta in sorted(self.ready_metadata.items())
            if meta.storage_backend == storage_backend
        )


@dataclass
class _InMemoryReviewRepo:
    manual_events: list[ManualLabelEvent] = field(default_factory=list)
    review_events: list[MessageReviewEvent] = field(default_factory=list)
    status_by_msg: dict[str, ReviewStatus] = field(default_factory=dict)

    async def append_manual_label(self, *, event: ManualLabelEvent) -> ManualLabelEvent:
        self.manual_events.append(event)
        return event

    async def transition_review_status(
        self,
        *,
        event_id: str,
        message_id: str,
        new_status: ReviewStatus,
        actor_admin_user_id: str,
        created_at: datetime,
        reason: str | None = None,
    ) -> MessageReviewEvent:
        old_status = self.status_by_msg.get(message_id, ReviewStatus.UNREVIEWED)
        event = MessageReviewEvent(
            event_id=event_id,
            message_id=message_id,
            old_status=old_status,
            new_status=new_status,
            actor_admin_user_id=actor_admin_user_id,
            created_at=created_at,
            reason=reason,
        )
        self.review_events.append(event)
        self.status_by_msg[message_id] = new_status
        return event

    async def list_manual_label_history(self, *, message_id: str) -> tuple[ManualLabelEvent, ...]:
        return tuple(e for e in self.manual_events if e.message_id == message_id)

    async def list_review_history(self, *, message_id: str) -> tuple[MessageReviewEvent, ...]:
        return tuple(e for e in self.review_events if e.message_id == message_id)


@dataclass
class _InMemoryRoutingRepo:
    snapshots: dict[str, RoutingSnapshot] = field(default_factory=dict)
    by_request: dict[str, RoutingExecutionResult] = field(default_factory=dict)
    persisted_intents: list[PublicationIntentDraft] = field(default_factory=list)

    async def find_by_request_id(self, *, routing_request_id: str) -> RoutingExecutionResult | None:
        return self.by_request.get(routing_request_id)

    async def load_snapshot(
        self, *, message_id: str, routing_policy_version_id: str
    ) -> RoutingSnapshot:
        _ = routing_policy_version_id
        return self.snapshots[message_id]

    async def persist_evaluation(
        self, *, evaluation: Any, decision: Any, intents: Any
    ) -> RoutingExecutionResult:
        res = RoutingExecutionResult(
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
        self.by_request[evaluation.routing_request_id] = res
        existing_keys = {i.business_idempotency_key for i in self.persisted_intents}
        for intent in intents:
            if intent.business_idempotency_key not in existing_keys:
                self.persisted_intents.append(intent)
                existing_keys.add(intent.business_idempotency_key)
        return res


@dataclass
class _InMemoryPublicationState:
    intent_id: str
    message_id: str
    source_channel_id: str
    destination_channel_id: str
    publish_identity_id: str
    source_message_ids: tuple[int, ...]
    mode: PublicationMode
    rendered_text: str | None
    supplement_as_reply: bool = True
    status: str = "pending"
    completed_parts: list[PublicationPart] = field(default_factory=list)
    main_remote_message_ids: tuple[int, ...] = ()
    in_flight_part: PublicationPart | None = None
    in_flight_operation: PublicationOperation | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_retry_at: datetime | None = None
    publish_records: list[tuple[PublicationPart, tuple[int, ...], bool]] = field(
        default_factory=list
    )


class _InMemoryPublicationRepo:
    def __init__(self, states: dict[str, _InMemoryPublicationState]) -> None:
        self.states = states

    async def claim(
        self, *, intent_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedPublicationIntent | None:
        _ = lease_duration
        st = self.states.get(intent_id)
        if st is None or st.status in {"published", "failed_permanent", "blocked_protected"}:
            return None
        if st.next_retry_at is not None and st.next_retry_at > now:
            return None
        st.attempt_count += 1
        main_count = (
            1 if st.mode is PublicationMode.METADATA_ONLY else max(1, len(st.source_message_ids))
        )
        main_rids = stable_random_ids(
            intent_id=st.intent_id,
            part=PublicationPart.MAIN,
            count=main_count,
        )
        supp_rid = (
            stable_random_id(intent_id=st.intent_id, part=PublicationPart.SUPPLEMENT)
            if st.mode is PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT
            else None
        )
        return ClaimedPublicationIntent(
            intent_id=st.intent_id,
            attempt_id=str(uuid4()),
            attempt_number=st.attempt_count,
            max_attempts=st.max_attempts,
            lease_token=str(uuid4()),
            message_id=st.message_id,
            source_channel_id=st.source_channel_id,
            source_peer_id=-1001001,
            destination_channel_id=st.destination_channel_id,
            destination_peer_id=-1002001,
            publish_identity_id=st.publish_identity_id,
            source_message_ids=st.source_message_ids,
            mode=st.mode,
            protected_content_fallback=ProtectedContentFallback.METADATA_ONLY,
            supplement_as_reply=st.supplement_as_reply,
            rendered_text=st.rendered_text,
            main_random_ids=main_rids,
            supplement_random_id=supp_rid,
            completed_parts=tuple(st.completed_parts),
            main_remote_message_ids=st.main_remote_message_ids,
            in_flight_part=st.in_flight_part,
            in_flight_operation=st.in_flight_operation,
        )

    async def complete_wakeup_if_terminal(self, *, intent_id: str, now: datetime) -> bool:
        _ = (intent_id, now)
        return True

    async def release(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool:
        _ = (claim, now)
        return True

    async def begin_part(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        now: datetime,
    ) -> bool:
        _ = now
        st = self.states[claim.intent_id]
        st.in_flight_part = part
        st.in_flight_operation = operation
        st.status = "publishing"
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
        _ = (operation, now)
        st = self.states[claim.intent_id]
        if part not in st.completed_parts:
            st.completed_parts.append(part)
        if part is PublicationPart.MAIN:
            st.main_remote_message_ids = remote_message_ids
        st.in_flight_part = None
        st.in_flight_operation = None
        st.publish_records.append((part, remote_message_ids, reconciled))
        st.status = "partial"
        return True

    async def complete(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool:
        _ = now
        st = self.states[claim.intent_id]
        st.status = "published"
        st.next_retry_at = None
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
        _ = (error_code, error_type, now)
        st = self.states[claim.intent_id]
        if not retryable or claim.attempt_number >= claim.max_attempts:
            st.status = "failed_permanent"
            return True
        st.next_retry_at = retry_at
        st.status = "partial" if st.completed_parts else "retry_pending"
        return True

    async def cancel(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        now: datetime,
    ) -> bool:
        _ = (error_code, error_type, now)
        self.states[claim.intent_id].status = "blocked_protected"
        return True


class ClosedLoopPipelineE2ETests(unittest.TestCase):
    """End-to-end closed-loop and operational resilience verification (Sections 45, 47-49)."""

    def test_fixed_and_latest_range_boundaries_steady_state_and_frozen_window_stability(
        self,
    ) -> None:
        async def _scenario() -> None:
            tg = FakeTelegramAdapter()
            src_fixed = str(uuid4())
            src_latest = str(uuid4())
            profile_ver = str(uuid4())

            fixed_range = ScheduledProcessingRange(
                range_id=str(uuid4()),
                source_channel_id=src_fixed,
                source_profile_version_id=profile_ver,
                end_mode="fixed",
                resolved_start_message_id=101,
                resolved_fixed_end_message_id=120,
                processing_watermark_message_id=None,
                steady_after_seconds=None,
            )
            latest_range = ScheduledProcessingRange(
                range_id=str(uuid4()),
                source_channel_id=src_latest,
                source_profile_version_id=profile_ver,
                end_mode="latest",
                resolved_start_message_id=101,
                resolved_fixed_end_message_id=None,
                processing_watermark_message_id=100,
                steady_after_seconds=300,
            )

            created_executions: list[PendingRangeExecution] = []

            class _ScheduleRepo:
                def __init__(self, items: tuple[ScheduledProcessingRange, ...]) -> None:
                    self.items = items

                async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
                    return self.items

                async def create_execution_and_wakeup(
                    self,
                    *,
                    execution: PendingRangeExecution,
                    queue: str,
                    available_at: datetime,
                ) -> bool:
                    _ = (queue, available_at)
                    created_executions.append(execution)
                    return True

            # 1. Channel was active 60s ago (< 300s steady_after): FIXED schedules immediately,
            #    LATEST waits without freezing a boundary.
            tg.boundaries[src_latest] = LatestMessageBoundary(
                message_id=105,
                published_at=NOW - timedelta(seconds=60),
            )
            scheduler = ProcessingRangeScheduler(
                _ScheduleRepo((fixed_range, latest_range)),
                tg,
            )
            first_report = await scheduler.schedule(now=NOW)
            self.assertEqual(first_report.created_executions, 1)
            self.assertEqual(first_report.skipped_ranges, 1)
            self.assertEqual(created_executions[0].processing_range_id, fixed_range.range_id)
            self.assertEqual(created_executions[0].from_message_id_exclusive, 100)
            self.assertEqual(created_executions[0].to_message_id_inclusive, 120)

            # 2. Once channel is quiet >= 300s, LATEST freezes active_high_watermark = 105.
            later = NOW + timedelta(seconds=260)
            second_scheduler = ProcessingRangeScheduler(_ScheduleRepo((latest_range,)), tg)
            second_report = await second_scheduler.schedule(now=later)
            self.assertEqual(second_report.created_executions, 1)
            frozen_exec = created_executions[1]
            self.assertEqual(frozen_exec.from_message_id_exclusive, 100)
            self.assertEqual(frozen_exec.to_message_id_inclusive, 105)

            # 3. Newer Telegram messages arriving (109) while 100..105 is active do NOT mutate
            #    the frozen finite window (Invariant 6).
            tg.boundaries[src_latest] = LatestMessageBoundary(
                message_id=109,
                published_at=later + timedelta(seconds=5),
            )
            self.assertEqual(frozen_exec.to_message_id_inclusive, 105)

        asyncio.run(_scenario())

    def test_closed_loop_prescreen_negative_gate_archive_dedup_conditional_dag_and_partial_batch(
        self,
    ) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tg = FakeTelegramAdapter()
                provider = FakeInferenceProvider()
                storage = LocalArchiveStorage(root=Path(tmp_dir) / "archive")
                processor = PillowImageProcessor()
                archive_service = ImageArchiveService(
                    image_processor=processor,
                    archive_storage=storage,
                )
                image_store = _InMemoryImageStore()
                norm_profile = ImageNormalizationProfile(max_side_pixels=512, quality=85)
                image_worker = ImageArchiveWorker(
                    repository=image_store,
                    media_downloader=tg,
                    archive_service=archive_service,
                    normalization_profile=norm_profile,
                    storage_backend="local",
                )

                source_channel_id = str(uuid4())
                png_diagonal = _render_synthetic_png(pattern="diagonal")
                png_quadrant = _render_synthetic_png(pattern="quadrant")
                png_gradient = _render_synthetic_png(pattern="gradient")

                # Populate source Telegram messages:
                # - Album (101, 102, grouped_id=9001): curated illustration
                # - Message 103: casino promo spam -> blocked by batch_messages PreScreen!
                # - Message 104: identical visual content to diagonal artwork (visual dedup)
                # - Message 105: 2-image post for partial batch_assets validation & retry
                tg.media_blobs[(source_channel_id, 101, "tg-img-101")] = png_diagonal
                tg.media_blobs[(source_channel_id, 102, "tg-img-102")] = png_quadrant
                tg.media_blobs[(source_channel_id, 103, "tg-img-103-spam")] = png_gradient
                tg.media_blobs[(source_channel_id, 104, "tg-img-104-dup")] = png_diagonal
                tg.media_blobs[(source_channel_id, 105, "tg-img-105-a")] = png_quadrant
                tg.media_blobs[(source_channel_id, 105, "tg-img-105-b")] = png_gradient

                normalized_msgs = normalize_telegram_messages(
                    (
                        TelegramMessage(
                            source_channel_id=source_channel_id,
                            telegram_message_id=101,
                            telegram_grouped_id=9001,
                            published_at=NOW,
                            original_text="Winter landscape illustration album",
                            media=(MediaAsset("tg-img-101", MediaKind.IMAGE),),
                        ),
                        TelegramMessage(
                            source_channel_id=source_channel_id,
                            telegram_message_id=102,
                            telegram_grouped_id=9001,
                            published_at=NOW + timedelta(seconds=1),
                            media=(MediaAsset("tg-img-102", MediaKind.IMAGE),),
                        ),
                        TelegramMessage(
                            source_channel_id=source_channel_id,
                            telegram_message_id=103,
                            published_at=NOW + timedelta(seconds=10),
                            original_text="VIP CASINO PROMO SPAM join immediately",
                            media=(MediaAsset("tg-img-103-spam", MediaKind.IMAGE),),
                        ),
                        TelegramMessage(
                            source_channel_id=source_channel_id,
                            telegram_message_id=104,
                            published_at=NOW + timedelta(seconds=20),
                            original_text="Reposted diagonal artwork with new commentary",
                            media=(MediaAsset("tg-img-104-dup", MediaKind.IMAGE),),
                        ),
                        TelegramMessage(
                            source_channel_id=source_channel_id,
                            telegram_message_id=105,
                            published_at=NOW + timedelta(seconds=30),
                            original_text="Dual study sketches",
                            media=(
                                MediaAsset("tg-img-105-a", MediaKind.IMAGE),
                                MediaAsset("tg-img-105-b", MediaKind.IMAGE),
                            ),
                        ),
                    )
                )
                self.assertEqual(len(normalized_msgs), 4)
                msg_album, _msg_spam, _msg_repost, msg_dual = normalized_msgs
                self.assertEqual(msg_album.telegram_message_ids, (101, 102))

                msg_uuids = {
                    101: str(uuid4()),
                    103: str(uuid4()),
                    104: str(uuid4()),
                    105: str(uuid4()),
                }

                # Step 1: Text-only batch_messages PreScreen BEFORE any media download!
                prescreen_stage = _build_stage(
                    stage_id="prescreen_text",
                    scope=LabelScope.GLOBAL,
                    execution_mode=ExecutionMode.BATCH_MESSAGES,
                    labels=(
                        ("curated_topic", False, 0.60),
                        ("spam_ad", True, 0.70),
                    ),
                    include_text=True,
                    include_media=False,
                )
                orchestrator = AnalysisOrchestrator(
                    provider=provider,
                    storage_resolver=MappingArchiveStorageResolver({"local": storage}),
                )
                prescreen_exec = await orchestrator.execute(
                    stage=prescreen_stage,
                    targets=tuple(
                        AnalysisTargetInput(
                            target_id=msg_uuids[m.primary_telegram_message_id],
                            message_id=msg_uuids[m.primary_telegram_message_id],
                            text=m.original_text or "",
                        )
                        for m in normalized_msgs
                    ),
                )
                self.assertTrue(prescreen_exec.succeeded)
                assert prescreen_exec.validation is not None
                self.assertEqual(len(tg.download_requests), 0)

                blocked_msg_ids: set[str] = set()
                for target_res in prescreen_exec.validation.valid_results:
                    if any(lbl.negative and lbl.activated for lbl in target_res.labels):
                        blocked_msg_ids.add(target_res.target_id)

                self.assertEqual(blocked_msg_ids, {msg_uuids[103]})

                # Step 2: Archive images only for non-blocked messages via Pillow + local storage
                asset_uuids_by_msg: dict[str, list[str]] = {}
                for m in normalized_msgs:
                    m_uuid = msg_uuids[m.primary_telegram_message_id]
                    if m_uuid in blocked_msg_ids:
                        continue
                    asset_uuids_by_msg[m_uuid] = []
                    for idx, media_item in enumerate(m.media):
                        a_uuid = str(uuid4())
                        part_tg_id = (
                            m.telegram_message_ids[idx]
                            if idx < len(m.telegram_message_ids)
                            else m.primary_telegram_message_id
                        )
                        image_store.message_id_by_asset[a_uuid] = m_uuid
                        image_store.items[a_uuid] = ImageArchiveWorkItem(
                            image_asset_id=a_uuid,
                            source_channel_id=source_channel_id,
                            source_telegram_message_id=part_tg_id,
                            source_asset_id=media_item.asset_id,
                            source_deleted_at=None,
                        )
                        processed = await image_worker.process(image_asset_id=a_uuid, now=NOW)
                        self.assertTrue(processed)
                        asset_uuids_by_msg[m_uuid].append(a_uuid)

                downloaded_asset_ids = {r.source_asset_id for r in tg.download_requests}
                self.assertNotIn("tg-img-103-spam", downloaded_asset_ids)
                self.assertEqual(len(downloaded_asset_ids), 5)

                # Step 3: Visual Fingerprint Deduplication (`skip` vs `reclassify_with_text`)
                repost_asset_id = asset_uuids_by_msg[msg_uuids[104]][0]
                repost_meta = image_store.ready_metadata[repost_asset_id]
                repost_fp, repost_tokens = compute_archived_visual_identity(
                    image_phashes=(repost_meta.perceptual_hash,),
                    video_cover_phashes=(),
                )
                canonical_msg_id = str(uuid4())
                canonical_candidate = VisualCandidate(
                    message_id=canonical_msg_id,
                    visual_fingerprint=repost_fp,
                    visual_tokens=repost_tokens,
                    published_at=NOW - timedelta(hours=1),
                )
                dup_skip = evaluate_visual_duplicate(
                    message_id=msg_uuids[104],
                    visual_fingerprint=repost_fp,
                    visual_tokens=repost_tokens,
                    candidates=(canonical_candidate,),
                    duplicate_policy=DuplicatePolicy.SKIP,
                    phash_distance_threshold=4,
                )
                self.assertEqual(dup_skip.status, DuplicateStatus.DUPLICATE_SKIPPED)
                self.assertEqual(dup_skip.canonical_message_id, canonical_msg_id)
                self.assertEqual(dup_skip.match_kind, DuplicateMatchKind.EXACT_FINGERPRINT)

                dup_reclassify = evaluate_visual_duplicate(
                    message_id=msg_uuids[104],
                    visual_fingerprint=repost_fp,
                    visual_tokens=repost_tokens,
                    candidates=(canonical_candidate,),
                    duplicate_policy=DuplicatePolicy.RECLASSIFY_WITH_TEXT,
                    phash_distance_threshold=4,
                )
                self.assertEqual(dup_reclassify.status, DuplicateStatus.DUPLICATE_RECLASSIFY)
                self.assertEqual(dup_reclassify.canonical_message_id, canonical_msg_id)

                # Verify StageCacheKey changes whenever prompt_version_id changes (Invariant 20)
                vlm_stage = _build_stage(
                    stage_id="vlm_classify",
                    scope=LabelScope.MEDIA,
                    execution_mode=ExecutionMode.PER_ASSET,
                    labels=(
                        ("illustration", False, 0.70),
                        ("nsfw_block", True, 0.85),
                    ),
                    include_text=False,
                    include_media=True,
                    cache_policy=CachePolicy.ASSET,
                )
                vlm_stage_v2 = _build_stage(
                    stage_id="vlm_classify",
                    scope=LabelScope.MEDIA,
                    execution_mode=ExecutionMode.PER_ASSET,
                    labels=(
                        ("illustration", False, 0.70),
                        ("nsfw_block", True, 0.85),
                    ),
                    include_text=False,
                    include_media=True,
                    prompt_version_id="prompt-v2",
                    cache_policy=CachePolicy.ASSET,
                )
                prep_v1 = await orchestrator.prepare_async(
                    stage=vlm_stage,
                    targets=(
                        AnalysisTargetInput(
                            target_id=repost_asset_id,
                            message_id=msg_uuids[104],
                            asset_id=repost_asset_id,
                            media=(
                                {
                                    "storage_key": repost_meta.storage_key,
                                    "storage_backend": "local",
                                    "archive_sha256": repost_meta.archive_sha256,
                                },
                            ),
                            visual_identity=repost_meta.archive_sha256,
                        ),
                    ),
                )
                prep_v2 = await orchestrator.prepare_async(
                    stage=vlm_stage_v2,
                    targets=(
                        AnalysisTargetInput(
                            target_id=repost_asset_id,
                            message_id=msg_uuids[104],
                            asset_id=repost_asset_id,
                            media=(
                                {
                                    "storage_key": repost_meta.storage_key,
                                    "storage_backend": "local",
                                    "archive_sha256": repost_meta.archive_sha256,
                                },
                            ),
                            visual_identity=repost_meta.archive_sha256,
                        ),
                    ),
                )
                assert prep_v1.cache_key is not None and prep_v2.cache_key is not None
                self.assertNotEqual(prep_v1.cache_key, prep_v2.cache_key)

                # Step 4: Conditional DAG planning + Partial Batch Target Validation & Retry!
                followup_stage = _build_stage(
                    stage_id="deep_art_tagger",
                    scope=LabelScope.GLOBAL,
                    execution_mode=ExecutionMode.SINGLE_MESSAGE,
                    labels=(("masterpiece", False, 0.75),),
                    include_text=True,
                    include_media=False,
                )
                dag_nodes = (
                    PipelineNodeSpec(
                        node_id="node-vlm",
                        node_key="vlm",
                        stage_version_id=vlm_stage.version_id,
                        target_scope=LabelScope.MEDIA,
                        input_requires_media=True,
                        is_text_only_prescreen=False,
                        cache_policy=CachePolicy.ASSET,
                        max_attempts=3,
                        depends_on=(),
                        run_if=None,
                    ),
                    PipelineNodeSpec(
                        node_id="node-followup",
                        node_key="followup",
                        stage_version_id=followup_stage.version_id,
                        target_scope=LabelScope.GLOBAL,
                        input_requires_media=False,
                        is_text_only_prescreen=False,
                        cache_policy=CachePolicy.MESSAGE,
                        max_attempts=3,
                        depends_on=("vlm",),
                        run_if={
                            "any_media": {
                                "label": {
                                    "namespace": "model",
                                    "key": "illustration",
                                }
                            }
                        },
                    ),
                )

                # Blocked message produces BLOCKED_NEGATIVE_GATE and schedules nothing
                blocked_plan = plan_pipeline_progression(
                    message_id=msg_uuids[103],
                    nodes=dag_nodes,
                    existing_runs_by_node={},
                    media=MessageMediaReadiness(assets=()),
                    facts={},
                    blocked_from_analysis=True,
                )
                self.assertEqual(blocked_plan.run_status, AnalysisRunStatus.BLOCKED_NEGATIVE_GATE)
                self.assertEqual(len(blocked_plan.planned_targets), 0)

                dual_assets = asset_uuids_by_msg[msg_uuids[105]]
                batch_vlm_stage = _build_stage(
                    stage_id="vlm_batch",
                    scope=LabelScope.MEDIA,
                    execution_mode=ExecutionMode.BATCH_ASSETS,
                    labels=(
                        ("illustration", False, 0.70),
                        ("nsfw_block", True, 0.85),
                    ),
                    include_text=False,
                    include_media=True,
                )
                provider.corrupt_asset_once.add(dual_assets[1])

                first_batch_res = await orchestrator.execute(
                    stage=batch_vlm_stage,
                    targets=tuple(
                        AnalysisTargetInput(
                            target_id=aid,
                            message_id=msg_uuids[105],
                            asset_id=aid,
                            media=(
                                {
                                    "storage_key": image_store.ready_metadata[aid].storage_key,
                                    "storage_backend": "local",
                                    "archive_sha256": image_store.ready_metadata[
                                        aid
                                    ].archive_sha256,
                                },
                            ),
                            visual_identity=image_store.ready_metadata[aid].archive_sha256,
                        )
                        for aid in dual_assets
                    ),
                )
                assert first_batch_res.validation is not None
                self.assertEqual(
                    [t.target_id for t in first_batch_res.validation.valid_results],
                    [dual_assets[0]],
                )
                self.assertEqual(
                    list(first_batch_res.validation.retry_target_ids),
                    [dual_assets[1]],
                )

                # Retry ONLY failed Asset 1 -> succeeds cleanly without re-inferring Asset 0!
                retry_batch_res = await orchestrator.execute(
                    stage=batch_vlm_stage,
                    targets=(
                        AnalysisTargetInput(
                            target_id=dual_assets[1],
                            message_id=msg_uuids[105],
                            asset_id=dual_assets[1],
                            media=(
                                {
                                    "storage_key": image_store.ready_metadata[
                                        dual_assets[1]
                                    ].storage_key,
                                    "storage_backend": "local",
                                    "archive_sha256": image_store.ready_metadata[
                                        dual_assets[1]
                                    ].archive_sha256,
                                },
                            ),
                            visual_identity=image_store.ready_metadata[
                                dual_assets[1]
                            ].archive_sha256,
                        ),
                    ),
                )
                self.assertTrue(retry_batch_res.succeeded)

                # Conditional follow-up node schedules once `vlm` succeeded & `illustration` active
                dag_facts = {
                    "labels": {
                        "model": {
                            "media": {
                                dual_assets[0]: {
                                    "labels": {
                                        "illustration": {"score": 0.93, "activated": True},
                                    }
                                }
                            }
                        }
                    }
                }
                assert dag_nodes[1].run_if is not None
                self.assertTrue(evaluate_run_if(dag_nodes[1].run_if, dag_facts))

                next_plan = plan_pipeline_progression(
                    message_id=msg_uuids[105],
                    nodes=dag_nodes,
                    existing_runs_by_node={
                        "vlm": tuple(
                            ExistingNodeTargetRun("image_asset", aid, "succeeded")
                            for aid in dual_assets
                        )
                    },
                    media=MessageMediaReadiness(
                        assets=tuple(
                            MediaAssetTarget("image_asset", aid, "ready") for aid in dual_assets
                        )
                    ),
                    facts=dag_facts,
                    blocked_from_analysis=False,
                )
                self.assertEqual(len(next_plan.planned_targets), 1)
                self.assertEqual(next_plan.planned_targets[0].node_key, "followup")
                self.assertEqual(
                    next_plan.planned_targets[0].action,
                    PipelineNodeActionKind.SCHEDULE,
                )

                followup_res = await orchestrator.execute(
                    stage=followup_stage,
                    targets=(
                        AnalysisTargetInput(
                            target_id=msg_uuids[105],
                            message_id=msg_uuids[105],
                            text=msg_dual.original_text or "",
                        ),
                    ),
                )
                self.assertTrue(followup_res.succeeded)

                # Verify StorageConsistencyScanner reports 100% consistent across all 5 WebPs
                consistency = await StorageConsistencyScanner(
                    repository=image_store,
                    storage=storage,
                ).scan(verify_sha256=True, now=NOW)
                self.assertTrue(consistency.consistent)
                self.assertEqual(consistency.checked_ready_count, 5)
                self.assertEqual(consistency.observed_file_count, 5)

        asyncio.run(_scenario())

    def test_review_layering_multirule_routing_all_four_publish_modes_floodwait_and_crash_recovery(
        self,
    ) -> None:
        async def _scenario() -> None:
            tg = FakeTelegramAdapter()
            admin_id = str(uuid4())
            msg_id = str(uuid4())
            src_channel_id = str(uuid4())
            pub_identity_id = str(uuid4())
            dest_vip = str(uuid4())
            dest_mirror = str(uuid4())
            dest_feed = str(uuid4())
            dest_digest = str(uuid4())
            tpl_id = str(uuid4())
            policy_id = str(uuid4())
            lbl_ill_ver = str(uuid4())
            lbl_feat_ver = str(uuid4())

            model_labels = (
                LabelValue(
                    message_id=msg_id,
                    target_scope="global",
                    target_kind="message",
                    target_id=msg_id,
                    label_definition_version_id=lbl_ill_ver,
                    label_key="illustration",
                    score=0.92,
                    activated=True,
                ),
            )
            review_repo = _InMemoryReviewRepo()
            review_service = ReviewService(review_repo, clock=lambda: NOW)
            await review_service.set_label(
                message_id=msg_id,
                target_scope="global",
                target_kind="message",
                target_id=msg_id,
                label_definition_version_id=lbl_feat_ver,
                label_key="featured",
                score=1.0,
                activated=True,
                actor_admin_user_id=admin_id,
                reason="Promote to VIP curated showcase",
            )
            await review_service.transition_status(
                message_id=msg_id,
                new_status=ReviewStatus.REVIEWED,
                actor_admin_user_id=admin_id,
                reason="Verified artwork attribution",
            )

            namespaces = resolve_label_namespaces(
                model_assignments=model_labels,
                manual_events=tuple(review_repo.manual_events),
            )
            self.assertEqual({item.label_key for item in namespaces.model}, {"illustration"})
            self.assertEqual({item.label_key for item in namespaces.manual}, {"featured"})
            self.assertEqual(
                {item.label_key for item in namespaces.effective},
                {"illustration", "featured"},
            )

            policy = RoutingPolicy(
                policy_version_id=policy_id,
                rules=(
                    RoutingRule(
                        rule_id=str(uuid4()),
                        priority=100,
                        stop_on_match=False,
                        condition={
                            "label": {
                                "namespace": "manual",
                                "scope": "global",
                                "key": "featured",
                            }
                        },
                        actions=(
                            PublicationAction(
                                action_id=str(uuid4()),
                                destination_channel_id=dest_vip,
                                publication_mode="native_forward_with_supplement",
                                rendering_template_version_id=tpl_id,
                            ),
                            PublicationAction(
                                action_id=str(uuid4()),
                                destination_channel_id=dest_mirror,
                                publication_mode="copy_with_caption",
                                rendering_template_version_id=tpl_id,
                            ),
                        ),
                    ),
                    RoutingRule(
                        rule_id=str(uuid4()),
                        priority=50,
                        stop_on_match=True,
                        condition={
                            "label": {
                                "namespace": "model",
                                "scope": "global",
                                "key": "illustration",
                            }
                        },
                        actions=(
                            PublicationAction(
                                action_id=str(uuid4()),
                                destination_channel_id=dest_feed,
                                publication_mode="forward_only",
                            ),
                            PublicationAction(
                                action_id=str(uuid4()),
                                destination_channel_id=dest_digest,
                                publication_mode="metadata_only",
                                rendering_template_version_id=tpl_id,
                            ),
                        ),
                    ),
                    RoutingRule(
                        rule_id=str(uuid4()),
                        priority=10,
                        stop_on_match=False,
                        condition={
                            "label": {
                                "namespace": "effective",
                                "scope": "global",
                                "key": "illustration",
                            }
                        },
                        actions=(
                            PublicationAction(
                                action_id=str(uuid4()),
                                destination_channel_id=str(uuid4()),
                                publication_mode="forward_only",
                            ),
                        ),
                    ),
                ),
            )

            routing_repo = _InMemoryRoutingRepo()
            routing_repo.snapshots[msg_id] = RoutingSnapshot(
                message_id=msg_id,
                message={
                    "id": msg_id,
                    "blocked_from_analysis": False,
                    "original_text": "Featured winter concept art",
                    "primary_telegram_message_id": 101,
                    "media_count": 2,
                },
                source_channel={"id": src_channel_id, "username": "art_source"},
                duplicate_state={"status": "unique"},
                media=(),
                review_status=ReviewStatus.REVIEWED,
                model_assignments=model_labels,
                manual_events=tuple(review_repo.manual_events),
                policy=policy,
                destination_publish_identity_ids={
                    dest_vip: pub_identity_id,
                    dest_mirror: pub_identity_id,
                    dest_feed: pub_identity_id,
                    dest_digest: pub_identity_id,
                },
                rendering_template_texts={
                    tpl_id: "{{ message.original_text }} | {{ telegram_source_link }}"
                },
            )
            routing_service = RoutingService(routing_repo)
            route_res = await routing_service.evaluate(
                RoutingRequest(
                    request_id=str(uuid4()),
                    message_id=msg_id,
                    routing_policy_version_id=policy_id,
                )
            )
            self.assertEqual(len(route_res.intents), 4)
            self.assertEqual(route_res.decision.stopped_at_rule_id, policy.rules[1].rule_id)

            pub_states: dict[str, _InMemoryPublicationState] = {}
            for draft in route_res.intents:
                pub_states[draft.intent_id] = _InMemoryPublicationState(
                    intent_id=draft.intent_id,
                    message_id=msg_id,
                    source_channel_id=src_channel_id,
                    destination_channel_id=draft.destination_channel_id,
                    publish_identity_id=draft.publish_identity_id,
                    source_message_ids=(101, 102),
                    mode=PublicationMode(draft.publication_mode),
                    rendered_text=draft.rendered_text,
                )

            vip_intent_id = route_res.intents[0].intent_id
            mirror_intent_id = route_res.intents[1].intent_id

            tg.flood_wait_once_intents.add(mirror_intent_id)
            tg.drop_after_remote_send_parts.add((vip_intent_id, PublicationPart.MAIN))

            pub_repo = _InMemoryPublicationRepo(pub_states)
            pub_worker = PublicationWorker(repository=pub_repo, publisher=tg)

            await pub_worker.process(intent_id=mirror_intent_id, now=NOW)
            self.assertEqual(pub_states[mirror_intent_id].status, "retry_pending")
            self.assertEqual(
                pub_states[mirror_intent_id].next_retry_at,
                NOW + timedelta(seconds=45),
            )

            await pub_worker.process(intent_id=vip_intent_id, now=NOW)
            self.assertEqual(pub_states[vip_intent_id].in_flight_part, PublicationPart.MAIN)

            retry_now = NOW + timedelta(minutes=2)
            for draft in route_res.intents:
                await pub_worker.process(intent_id=draft.intent_id, now=retry_now)

            for draft in route_res.intents:
                self.assertEqual(pub_states[draft.intent_id].status, "published")

            self.assertEqual(len(tg.reconcile_requests), 2)
            self.assertEqual(
                {req.intent_id for req in tg.reconcile_requests},
                {mirror_intent_id, vip_intent_id},
            )
            vip_records = pub_states[vip_intent_id].publish_records
            self.assertEqual(len(vip_records), 2)
            self.assertTrue(vip_records[0][2])
            self.assertFalse(vip_records[1][2])

            prev_pub_count = len(tg.publish_requests)
            dup_handled = await pub_worker.process(intent_id=vip_intent_id, now=retry_now)
            self.assertFalse(dup_handled)
            self.assertEqual(len(tg.publish_requests), prev_pub_count)

        asyncio.run(_scenario())

    def test_watermark_broker_wipe_recovery_two_step_cleanup_consistency_and_admin_api_invariants(
        self,
    ) -> None:
        async def _scenario() -> None:
            # 1. Gap-free continuous watermark advancement (Invariant 7)
            step1 = compute_continuous_watermark(
                from_message_id_exclusive=100,
                current_watermark_message_id=100,
                to_message_id_inclusive=103,
                history_ingested=True,
                messages=(
                    WindowMessageProgress(101, "processed", terminal_reason="completed"),
                    WindowMessageProgress(102, "in_progress"),
                    WindowMessageProgress(103, "processed", terminal_reason="completed"),
                ),
            )
            self.assertEqual(step1.next_watermark_message_id, 101)
            self.assertFalse(step1.window_completed)
            self.assertEqual(step1.blocked_at_message_id, 102)

            step2 = compute_continuous_watermark(
                from_message_id_exclusive=100,
                current_watermark_message_id=101,
                to_message_id_inclusive=103,
                history_ingested=True,
                messages=(
                    WindowMessageProgress(101, "processed", terminal_reason="completed"),
                    WindowMessageProgress(102, "processed", terminal_reason="admin_skipped"),
                    WindowMessageProgress(103, "processed", terminal_reason="completed"),
                ),
            )
            self.assertEqual(step2.next_watermark_message_id, 103)
            self.assertTrue(step2.window_completed)

            # 2. Simulated Redis/Celery broker wipe recovered from PostgreSQL via SchedulerRuntime
            dispatched_to_broker: list[tuple[str, str]] = []
            repaired_flag = False

            class _FakeBroker:
                async def dispatch(self, *, queue: str, entity_id: str) -> None:
                    dispatched_to_broker.append((queue, entity_id))

            target_intent_id = str(uuid4())
            wakeup_id = str(uuid4())
            wakeup_dispatched = False

            class _WakeupRepo:
                async def claim_due(
                    self, *, now: datetime, lease_duration: timedelta, limit: int
                ) -> tuple[ClaimedWakeup, ...]:
                    _ = (now, limit, lease_duration)
                    if not repaired_flag or wakeup_dispatched:
                        return ()
                    return (
                        ClaimedWakeup(
                            wakeup_id=wakeup_id,
                            queue="publication",
                            entity_id=target_intent_id,
                            lease_token=str(uuid4()),
                        ),
                    )

                async def acknowledge_dispatch(
                    self,
                    *,
                    wakeup_id: str,
                    lease_token: str,
                    now: datetime,
                    repair_after: datetime,
                ) -> bool:
                    nonlocal wakeup_dispatched
                    _ = (wakeup_id, lease_token, now, repair_after)
                    wakeup_dispatched = True
                    return True

                async def reschedule_after_failure(self, **kwargs: Any) -> bool:
                    _ = kwargs
                    return True

                async def complete_for_entity(self, **kwargs: Any) -> bool:
                    _ = kwargs
                    return True

            class _PubRepairRepo:
                async def repair_wakeups(self, *, now: datetime, limit: int) -> int:
                    nonlocal repaired_flag
                    _ = (now, limit)
                    repaired_flag = True
                    return 1

            class _EmptyScheduleRepo:
                async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
                    return ()

                async def create_execution_and_wakeup(self, **kwargs: Any) -> bool:
                    _ = kwargs
                    return False

            class _FakeDb:
                async def dispose(self) -> None:
                    return None

            scheduler_runtime = SchedulerRuntime(
                database=_FakeDb(),  # type: ignore[arg-type]
                range_scheduler=ProcessingRangeScheduler(
                    _EmptyScheduleRepo(),
                    FakeTelegramAdapter(),
                ),
                wakeup_dispatcher=DurableWakeupDispatcher(_WakeupRepo(), _FakeBroker()),
                publication_repository=_PubRepairRepo(),  # type: ignore[arg-type]
            )
            cycle = await scheduler_runtime.run_once(now=NOW)
            self.assertEqual(cycle.repaired_publication_wakeups, 1)
            self.assertEqual(dispatched_to_broker, [("publication", target_intent_id)])

            # 3. Two-step Archive Cleanup (Dry-Run -> Confirm -> Worker) + Consistency Scanner!
            with tempfile.TemporaryDirectory() as tmp_dir:
                storage = LocalArchiveStorage(root=Path(tmp_dir) / "archive")
                processor = PillowImageProcessor()
                archive_service = ImageArchiveService(
                    image_processor=processor,
                    archive_storage=storage,
                )
                image_store = _InMemoryImageStore()
                asset_id = str(uuid4())
                msg_id = str(uuid4())
                src_id = str(uuid4())
                image_store.message_id_by_asset[asset_id] = msg_id
                image_store.items[asset_id] = ImageArchiveWorkItem(
                    image_asset_id=asset_id,
                    source_channel_id=src_id,
                    source_telegram_message_id=201,
                    source_asset_id="tg-clean-201",
                    source_deleted_at=None,
                )
                await archive_service.normalize_archive_and_mark_ready(
                    image_asset_id=asset_id,
                    storage_backend="local",
                    storage_key=f"images/{asset_id}.webp",
                    content=_render_synthetic_png(pattern="diagonal"),
                    profile=ImageNormalizationProfile(max_side_pixels=256, quality=80),
                    archived_at=NOW,
                    metadata_repository=image_store,
                )
                meta_before = image_store.ready_metadata[asset_id]
                self.assertTrue(await storage.exists(key=meta_before.storage_key))

                dry_run_id = str(uuid4())
                job_id = str(uuid4())
                cleanup_filter = ArchiveCleanupFilter(archived_before=NOW + timedelta(days=1))
                expected_fhash = cleanup_filter_hash(cleanup_filter.snapshot())

                class _CleanupRepo:
                    def __init__(self) -> None:
                        self.pending_items = [
                            ClaimedCleanupItem(
                                item_id=str(uuid4()),
                                deletion_token=str(uuid4()),
                                asset=CleanupAsset(
                                    asset_kind="image",
                                    asset_id=asset_id,
                                    message_id=msg_id,
                                    blobs=(
                                        CleanupBlob(
                                            storage_backend="local",
                                            storage_key=meta_before.storage_key,
                                            size_bytes=meta_before.archive_size_bytes,
                                        ),
                                    ),
                                    previous_archive_size=meta_before.archive_size_bytes,
                                ),
                            )
                        ]
                        self.completed = False

                    async def create_dry_run(
                        self,
                        *,
                        cleanup_filter: ArchiveCleanupFilter,
                        actor_admin_user_id: str,
                        now: datetime,
                    ) -> ArchiveCleanupDryRun:
                        return ArchiveCleanupDryRun(
                            dry_run_id=dry_run_id,
                            actor_admin_user_id=actor_admin_user_id,
                            filter_snapshot=cleanup_filter.snapshot(),
                            filter_hash=expected_fhash,
                            estimate=ArchiveCleanupEstimate(
                                asset_count=1,
                                message_count=1,
                                bytes=meta_before.archive_size_bytes,
                            ),
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
                        if expected_filter_hash != expected_fhash:
                            raise ValueError("filter hash mismatch")
                        _ = (confirmed_by_admin_user_id, max_attempts)
                        return ConfirmedArchiveCleanupJob(
                            job_id=job_id,
                            dry_run_id=dry_run_id,
                            filter_hash=expected_filter_hash,
                            estimate=ArchiveCleanupEstimate(
                                asset_count=1,
                                message_count=1,
                                bytes=meta_before.archive_size_bytes,
                            ),
                            item_count=1,
                            status="pending",
                            created_at=now,
                        )

                    async def claim(
                        self, *, job_id: str, now: datetime, lease_duration: timedelta
                    ) -> ClaimedArchiveCleanupJob | None:
                        _ = (now, lease_duration)
                        return ClaimedArchiveCleanupJob(
                            job_id=job_id,
                            lease_token=str(uuid4()),
                            attempt_number=1,
                            max_attempts=5,
                        )

                    async def claim_next_item(
                        self, *, claim: ClaimedArchiveCleanupJob, now: datetime
                    ) -> ClaimedCleanupItem | None:
                        _ = (claim, now)
                        return self.pending_items.pop(0) if self.pending_items else None

                    async def finalize_item_deleted(
                        self,
                        *,
                        claim: ClaimedArchiveCleanupJob,
                        item: ClaimedCleanupItem,
                        now: datetime,
                    ) -> bool:
                        _ = (claim, now)
                        image_store.cleaned_ids.add(item.asset.asset_id)
                        return True

                    async def complete(
                        self, *, claim: ClaimedArchiveCleanupJob, now: datetime
                    ) -> bool:
                        _ = (claim, now)
                        self.completed = True
                        return True

                cleanup_repo = _CleanupRepo()
                cleanup_service = ArchiveCleanupService(repository=cleanup_repo)  # type: ignore[arg-type]
                preview = await cleanup_service.dry_run(
                    cleanup_filter=cleanup_filter,
                    actor_admin_user_id=str(uuid4()),
                    now=NOW,
                )
                confirmed = await cleanup_service.confirm(
                    dry_run_id=preview.dry_run_id,
                    expected_filter_hash=preview.filter_hash,
                    confirmed_by_admin_user_id=str(uuid4()),
                    now=NOW,
                )
                cleanup_worker = ArchiveCleanupWorker(
                    repository=cleanup_repo,  # type: ignore[arg-type]
                    storage_resolver=MappingArchiveStorageResolver({"local": storage}),
                )
                self.assertTrue(await cleanup_worker.process(job_id=confirmed.job_id, now=NOW))
                self.assertFalse(await storage.exists(key=meta_before.storage_key))
                self.assertEqual(
                    image_store.ready_metadata[asset_id].perceptual_hash,
                    meta_before.perceptual_hash,
                )
                post_cleanup_scan = await StorageConsistencyScanner(
                    repository=image_store,
                    storage=storage,
                ).scan(verify_sha256=True, now=NOW)
                self.assertTrue(post_cleanup_scan.consistent)
                self.assertEqual(post_cleanup_scan.checked_ready_count, 0)
                self.assertEqual(post_cleanup_scan.observed_file_count, 0)

        asyncio.run(_scenario())

        # 4. Admin API closed-loop invariants: CSRF, write-only secrets, published immutability,
        #    Archive Cleanup Dry-Run enforcement, and Prompt Test Bench side-effect isolation!
        hasher = Argon2idPasswordHasher()
        backend = InMemoryAdminBackend()
        backend.seed_admin(
            username="e2e-admin",
            password_hash=hasher.hash("e2e-strong-passphrase"),
            user_id=str(uuid4()),
        )
        provider = FakeInferenceProvider()
        admin_service = AdminApplicationService(
            sessions=AdminSessionService(
                repository=backend,
                password_hasher=hasher,
                csrf_secret=b"e2e-csrf-hmac-secret-32-bytes!!!",
            ),
            secrets=InMemorySecretVault(),
            backend=backend,
            test_bench=PromptTestBenchService(provider=provider),
        )
        master_key_b64 = base64.b64encode(b"fedcba9876543210fedcba9876543210").decode("ascii")
        settings = Settings(
            environment="test",
            app_name="tg-vlm-curator-e2e",
            app_master_key=master_key_b64,  # type: ignore[arg-type]
        )

        class _DbProbe:
            async def ping(self) -> bool:
                return True

            async def dispose(self) -> None:
                return None

        class _SyncAsgiClient:
            def __init__(self, app: Any) -> None:
                self.app = app
                self._cookie_jar = CookieJar()
                self._runner: asyncio.Runner | None = None
                self._lifespan_cm: Any = None
                self._client: httpx.AsyncClient | None = None

            def __enter__(self) -> _SyncAsgiClient:
                self._runner = asyncio.Runner()
                self._lifespan_cm = self.app.router.lifespan_context(self.app)
                self._runner.run(self._lifespan_cm.__aenter__())
                self._client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                    cookies=httpx.Cookies(self._cookie_jar),
                )
                self._runner.run(self._client.__aenter__())
                return self

            def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                if self._runner is not None:
                    try:
                        if self._client is not None:
                            self._runner.run(self._client.__aexit__(exc_type, exc_val, exc_tb))
                        if self._lifespan_cm is not None:
                            self._runner.run(self._lifespan_cm.__aexit__(exc_type, exc_val, exc_tb))
                    finally:
                        self._client = None
                        self._lifespan_cm = None
                        self._runner.close()
                        self._runner = None

            def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
                assert self._runner is not None and self._client is not None
                return self._runner.run(self._client.request(method, url, **kwargs))

            def get(self, url: str, **kwargs: Any) -> httpx.Response:
                return self.request("GET", url, **kwargs)

            def post(self, url: str, **kwargs: Any) -> httpx.Response:
                return self.request("POST", url, **kwargs)

            def patch(self, url: str, **kwargs: Any) -> httpx.Response:
                return self.request("PATCH", url, **kwargs)

        with _SyncAsgiClient(
            create_app(settings, _DbProbe(), admin_service=admin_service)
        ) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "e2e-admin", "password": "e2e-strong-passphrase"},
            )
            self.assertEqual(login.status_code, 200)
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}

            no_csrf = client.post(
                "/api/v1/secrets",
                json={"secret_type": "inference_api_key", "plaintext": "sk-top-secret-e2e"},
            )
            self.assertEqual(no_csrf.status_code, 403)

            sec_resp = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "inference_api_key", "plaintext": "sk-top-secret-e2e"},
            )
            self.assertEqual(sec_resp.status_code, 201)
            self.assertNotIn("sk-top-secret-e2e", json.dumps(sec_resp.json()))
            secret_id = sec_resp.json()["secret_id"]

            lbl_ent = client.post(
                "/api/v1/config/label-definitions",
                headers=headers,
                json={"name": "e2e_illustration"},
            ).json()
            lbl_draft = client.post(
                f"/api/v1/config/label-definitions/{lbl_ent['id']}/versions",
                headers=headers,
                json={
                    "key": "e2e_illustration",
                    "display_name": "E2E Illustration",
                    "description": "Artwork check",
                    "scope": "global",
                    "negative": False,
                },
            )
            self.assertEqual(lbl_draft.status_code, 201)
            lbl_ver_id = lbl_draft.json()["id"]
            lbl_pub = client.post(
                f"/api/v1/config/label-definitions/{lbl_ent['id']}/versions/{lbl_ver_id}/publish",
                headers=headers,
            )
            self.assertEqual(lbl_pub.status_code, 200)

            illegal_patch = client.patch(
                f"/api/v1/config/label-definitions/{lbl_ent['id']}/versions/{lbl_ver_id}",
                headers=headers,
                json={"display_name": "Mutated After Publish"},
            )
            self.assertEqual(illegal_patch.status_code, 409)

            bad_confirm = client.post(
                "/api/v1/archive-cleanup/jobs",
                headers=headers,
                json={"dry_run_id": str(uuid4()), "expected_filter_hash": "a" * 64},
            )
            self.assertEqual(bad_confirm.status_code, 404)

            ls_ent = client.post(
                "/api/v1/config/label-sets",
                headers=headers,
                json={"name": "e2e_set"},
            ).json()
            ls_ver = client.post(
                f"/api/v1/config/label-sets/{ls_ent['id']}/versions",
                headers=headers,
                json={
                    "bindings": [
                        {
                            "label_definition_version_id": lbl_ver_id,
                            "activation_threshold": 0.7,
                            "output_order": 0,
                        }
                    ]
                },
            ).json()["id"]
            client.post(
                f"/api/v1/config/label-sets/{ls_ent['id']}/versions/{ls_ver}/publish",
                headers=headers,
            )

            pt_ent = client.post(
                "/api/v1/config/prompt-templates",
                headers=headers,
                json={"name": "e2e_prompt"},
            ).json()
            pt_ver = client.post(
                f"/api/v1/config/prompt-templates/{pt_ent['id']}/versions",
                headers=headers,
                json={
                    "system_prompt": "Labels: {{ label_definitions }}",
                    "user_prompt_template": "Msg: {{ message_text }}",
                    "declared_variables": ["label_definitions", "message_text"],
                },
            ).json()["id"]
            client.post(
                f"/api/v1/config/prompt-templates/{pt_ent['id']}/versions/{pt_ver}/publish",
                headers=headers,
            )

            ip_ent = client.post(
                "/api/v1/config/inference-profiles",
                headers=headers,
                json={"name": "e2e_vlm"},
            ).json()
            ip_ver = client.post(
                f"/api/v1/config/inference-profiles/{ip_ent['id']}/versions",
                headers=headers,
                json={
                    "provider_adapter": "openai_compatible",
                    "base_url": "https://vlm.external.example/v1",
                    "model_name": "qwen2.5-vl-72b",
                    "api_secret_reference": secret_id,
                    "capabilities": ["text", "vision"],
                    "structured_output_support": True,
                },
            ).json()["id"]
            client.post(
                f"/api/v1/config/inference-profiles/{ip_ent['id']}/versions/{ip_ver}/publish",
                headers=headers,
            )

            st_ent = client.post(
                "/api/v1/config/analysis-stages",
                headers=headers,
                json={"name": "e2e_stage"},
            ).json()
            st_ver = client.post(
                f"/api/v1/config/analysis-stages/{st_ent['id']}/versions",
                headers=headers,
                json={
                    "name": "e2e_stage",
                    "target_scope": "global",
                    "execution_mode": "single_message",
                    "prompt_template_version_id": pt_ver,
                    "label_set_version_id": ls_ver,
                    "inference_profile_version_id": ip_ver,
                    "input_policy": {"text": True},
                    "structured_output_policy": {"mode": "dense_scores"},
                },
            ).json()["id"]

            formal_msg_id = str(uuid4())
            backend.messages[formal_msg_id] = {
                "id": formal_msg_id,
                "source_channel_id": str(uuid4()),
                "primary_telegram_message_id": 501,
                "published_at": "2026-11-12T08:00:00+00:00",
                "original_text": "Sample concept illustration post",
                "media_count": 0,
                "processing_status": "processed",
                "terminal_reason": "completed",
                "duplicate_status": "unique",
                "review_status": "unreviewed",
                "blocked_from_analysis": False,
                "blocked_by_label_key": None,
            }

            bench_resp = client.post(
                "/api/v1/analysis/test-bench",
                headers=headers,
                json={
                    "stage_version_id": st_ver,
                    "message_id": formal_msg_id,
                    "invoke_provider": True,
                },
            )
            self.assertEqual(bench_resp.status_code, 200)
            self.assertEqual(bench_resp.json()["run_mode"], "test")
            self.assertEqual(bench_resp.json()["status"], "completed")
            self.assertEqual(backend.model_labels.get(formal_msg_id, []), [])
            self.assertFalse(backend.messages[formal_msg_id]["blocked_from_analysis"])
            self.assertEqual(len(backend.routing_evaluations), 0)


if __name__ == "__main__":
    unittest.main()
