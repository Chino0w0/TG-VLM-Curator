from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import Enum
from math import isfinite
from numbers import Real
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tgcurator.application.ports.analysis import (
    AnalysisCacheHit,
    AnalysisCommitResult,
    AnalysisStageSnapshot,
    AnalysisTargetSnapshot,
    ClaimedAnalysisStageRun,
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
    InputManifest,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    PromptTemplateVersion,
    StageCacheKey,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputMode,
    StructuredOutputPolicy,
    ValidatedTargetResult,
    VersionStatus,
    VisualCompositionPolicy,
)
from tgcurator.shared import DomainValidationError

from .models import (
    AnalysisRunRecord,
    AnalysisStageTemplateVersionRecord,
    DurableWakeupRecord,
    ImageAssetRecord,
    InferenceCallRecord,
    InferenceProfileVersionRecord,
    InputManifestRecord,
    LabelBindingRecord,
    LabelDefinitionVersionRecord,
    LabelSetVersionRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    PromptTemplateVersionRecord,
    SourceChannel,
    StageRunRecord,
    VideoAssetRecord,
    VideoFrameRecord,
)
from .session import AsyncDatabase

ANALYSIS_QUEUE = "analysis"
_ACTIVE_STAGE_STATUSES = ("pending", "running", "retry_wait")
_TERMINAL_RUN_STATUSES = ("completed", "blocked_negative_gate", "failed")
_REDACTED = "[REDACTED]"
_REDACTED_URL = "[REDACTED_URL]"
_SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "password",
    "apikey",
    "authorization",
    "credential",
    "signature",
)


def sanitize_identifier(value: object, *, field: str, max_length: int) -> str:
    """Return a nonblank bounded identifier suitable for a constrained column."""

    if value is None:
        raise DomainValidationError(f"{field} must not be blank")
    normalized = str(value.value if isinstance(value, Enum) else value).strip()
    if not normalized:
        raise DomainValidationError(f"{field} must not be blank")
    if len(normalized) > max_length:
        raise DomainValidationError(f"{field} must be at most {max_length} characters")
    return normalized


def sanitize_error_code(value: object) -> str:
    if value is None:
        return AnalysisErrorCode.INTERNAL_ERROR.value
    normalized = str(value.value if isinstance(value, Enum) else value).strip()
    return (normalized or AnalysisErrorCode.INTERNAL_ERROR.value)[:64]


def sanitize_error_type(value: object) -> str:
    if value is None:
        return "UnknownAnalysisError"
    normalized = str(value.value if isinstance(value, Enum) else value).strip()
    return (normalized or "UnknownAnalysisError")[:128]


def sanitize_json(value: Any) -> Any:
    """Normalize audit JSON recursively while removing credential-bearing values."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, Real):
        number = float(value)
        return number if isfinite(number) else None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return sanitize_json(value.value)
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            result[normalized_key] = (
                _REDACTED if _is_sensitive_key(normalized_key) else sanitize_json(item)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes:{len(value)}>"
    return str(value)


def analysis_stage_run_claim_statement(*, stage_run_id: UUID, now: datetime):
    """Lock one due stage run without waiting behind another worker."""

    return (
        select(StageRunRecord)
        .where(
            StageRunRecord.id == stage_run_id,
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
        .with_for_update(skip_locked=True)
    )


class SqlAlchemyAnalysisStageRunRepository:
    """PostgreSQL-backed, lease-safe persistence for provider-neutral analysis work."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def claim(
        self,
        *,
        stage_run_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedAnalysisStageRun | None:
        entity_id = UUID(stage_run_id)
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    analysis_stage_run_claim_statement(stage_run_id=entity_id, now=now)
                )
                if row is None:
                    return None
                run = await session.scalar(
                    select(AnalysisRunRecord)
                    .where(AnalysisRunRecord.id == row.analysis_run_id)
                    .with_for_update()
                )
                if run is None or run.status in _TERMINAL_RUN_STATUSES:
                    return None
                if row.attempt_count >= row.max_attempts:
                    await self._terminalize_exhausted(session=session, row=row, run=run, now=now)
                    return None

                lease_token = uuid4()
                row.status = "running"
                row.attempt_count += 1
                row.next_retry_at = None
                row.lease_token = lease_token
                row.lease_expires_at = now + lease_duration
                row.started_at = row.started_at or now
                row.updated_at = now
                run.status = "running"
                run.started_at = run.started_at or now
                run.completed_at = None
                run.updated_at = now
                return ClaimedAnalysisStageRun(
                    stage_run_id=str(row.id),
                    analysis_run_id=str(row.analysis_run_id),
                    message_id=str(row.message_id),
                    target_id=str(row.target_id),
                    analysis_stage_template_version_id=str(row.analysis_stage_template_version_id),
                    lease_token=str(lease_token),
                    attempt_count=row.attempt_count,
                    max_attempts=row.max_attempts,
                    run_mode=run.run_mode,
                )

    async def load_stage_snapshot(self, *, claim: ClaimedAnalysisStageRun) -> AnalysisStageSnapshot:
        stage_run_id = UUID(claim.stage_run_id)
        lease_token = UUID(claim.lease_token)
        async with self._database.session() as session:
            result = await session.execute(
                select(
                    StageRunRecord,
                    AnalysisRunRecord,
                    MessageRecord,
                    SourceChannel,
                    AnalysisStageTemplateVersionRecord,
                    PromptTemplateVersionRecord,
                    LabelSetVersionRecord,
                    InferenceProfileVersionRecord,
                )
                .join(AnalysisRunRecord, AnalysisRunRecord.id == StageRunRecord.analysis_run_id)
                .join(MessageRecord, MessageRecord.id == StageRunRecord.message_id)
                .join(SourceChannel, SourceChannel.id == MessageRecord.source_channel_id)
                .join(
                    AnalysisStageTemplateVersionRecord,
                    AnalysisStageTemplateVersionRecord.id
                    == StageRunRecord.analysis_stage_template_version_id,
                )
                .join(
                    PromptTemplateVersionRecord,
                    PromptTemplateVersionRecord.id
                    == AnalysisStageTemplateVersionRecord.prompt_template_version_id,
                )
                .join(
                    LabelSetVersionRecord,
                    LabelSetVersionRecord.id
                    == AnalysisStageTemplateVersionRecord.label_set_version_id,
                )
                .join(
                    InferenceProfileVersionRecord,
                    InferenceProfileVersionRecord.id
                    == AnalysisStageTemplateVersionRecord.inference_profile_version_id,
                )
                .where(
                    StageRunRecord.id == stage_run_id,
                    StageRunRecord.status == "running",
                    StageRunRecord.lease_token == lease_token,
                )
            )
            selected = result.one_or_none()
            if selected is None:
                raise DomainValidationError("analysis stage run lease is no longer active")
            (
                stage_run,
                analysis_run,
                message,
                source_channel,
                stage_version,
                prompt_version,
                label_set_version,
                inference_profile_version,
            ) = selected

            bindings = tuple(
                await session.execute(
                    select(LabelBindingRecord, LabelDefinitionVersionRecord)
                    .join(
                        LabelDefinitionVersionRecord,
                        LabelDefinitionVersionRecord.id
                        == LabelBindingRecord.label_definition_version_id,
                    )
                    .where(LabelBindingRecord.label_set_version_id == label_set_version.id)
                    .order_by(LabelBindingRecord.output_order, LabelBindingRecord.id)
                )
            )
            stage = _stage_from_records(
                stage=stage_version,
                prompt=prompt_version,
                label_set=label_set_version,
                inference_profile=inference_profile_version,
                bindings=bindings,
            )
            media, visual_identity = await _load_media_snapshot(
                session=session,
                stage_run=stage_run,
                message=message,
            )
            previous_results = await _load_previous_results(
                session=session,
                stage_run=stage_run,
                facts_snapshot=analysis_run.facts_snapshot,
            )

        target = AnalysisTargetSnapshot(
            stage_run_id=str(stage_run.id),
            lease_token=claim.lease_token,
            target_id=str(stage_run.target_id),
            message_id=str(stage_run.message_id),
            target_kind=stage_run.target_kind,
            asset_id=(str(stage_run.target_id) if stage_run.target_kind != "message" else None),
            text=message.original_text,
            telegram_metadata=sanitize_json(message.telegram_metadata),
            media=media,
            previous_results=previous_results,
            visual_identity=visual_identity,
        )
        return AnalysisStageSnapshot(
            claim=claim,
            stage=stage,
            targets=(target,),
            source_channel_context={
                "source_channel_id": str(source_channel.id),
                "telegram_peer_id": source_channel.telegram_peer_id,
                "username": source_channel.username,
                "display_name": source_channel.display_name,
            },
        )

    async def find_cache_hit(
        self, *, snapshot: AnalysisStageSnapshot, cache_key: StageCacheKey
    ) -> AnalysisCacheHit | None:
        async with self._database.session() as session:
            source = await session.scalar(
                select(StageRunRecord)
                .where(
                    StageRunRecord.id != UUID(snapshot.claim.stage_run_id),
                    StageRunRecord.status == "succeeded",
                    StageRunRecord.semantic_cache_key == cache_key.value,
                    StageRunRecord.cache_policy == cache_key.policy.value,
                    StageRunRecord.cache_scope_identity == cache_key.scope_identity,
                    StageRunRecord.parsed_result.is_not(None),
                )
                .order_by(StageRunRecord.completed_at.desc(), StageRunRecord.id.desc())
                .limit(1)
            )
            if source is None or source.parsed_result is None:
                return None
            parsed = sanitize_json(source.parsed_result)
            if not isinstance(parsed, dict):
                return None
            return AnalysisCacheHit(
                source_stage_run_id=str(source.id),
                parsed_result=parsed,
            )

    async def commit_cache_hit(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        manifest: InputManifest,
        cache_key: StageCacheKey,
        hit: AnalysisCacheHit,
        now: datetime,
    ) -> AnalysisCommitResult:
        source_id = UUID(hit.source_stage_run_id)
        async with self._database.session() as session:
            async with session.begin():
                source = await session.scalar(
                    select(StageRunRecord).where(StageRunRecord.id == source_id).with_for_update()
                )
                if not _exact_cache_source(source=source, cache_key=cache_key):
                    return AnalysisCommitResult(())
                rows = await self._lock_snapshot_targets(
                    session=session,
                    snapshot=snapshot,
                    now=now,
                )
                if not rows:
                    return AnalysisCommitResult(())
                manifest_row = _new_manifest(
                    stage_version_id=UUID(snapshot.stage.version_id),
                    manifest=manifest,
                    created_at=now,
                )
                session.add(manifest_row)
                source_assignments = tuple(
                    await session.scalars(
                        select(ModelLabelAssignmentRecord).where(
                            ModelLabelAssignmentRecord.stage_run_id == source_id
                        )
                    )
                )
                committed: list[str] = []
                negative: list[NegativeGateAssignment] = []
                for target in snapshot.targets:
                    row = rows.get(target.target_id)
                    if row is None:
                        continue
                    parsed = _rewrite_cached_target(
                        source.parsed_result or hit.parsed_result,
                        target_id=target.target_id,
                    )
                    row.status = "succeeded"
                    row.input_manifest_id = manifest_row.id
                    row.inference_call_id = None
                    row.parsed_result = parsed
                    row.cache_policy = cache_key.policy.value
                    row.cache_scope_identity = cache_key.scope_identity
                    row.semantic_cache_key = cache_key.value
                    row.result_origin = "cache"
                    row.reused_from_stage_run_id = source.id
                    _clear_stage_lease(row=row, now=now, completed=True)
                    committed.append(target.target_id)
                    if snapshot.claim.run_mode != "test":
                        for assignment in source_assignments:
                            copied = _copy_assignment(
                                source=assignment,
                                stage_run=row,
                                now=now,
                            )
                            session.add(copied)
                            if copied.activated and copied.negative:
                                negative.append(
                                    NegativeGateAssignment(
                                        message_id=str(row.message_id),
                                        stage_run_id=str(row.id),
                                        assignment_id=str(copied.id),
                                    )
                                )
                    await self._set_wakeup_terminal(
                        session=session,
                        entity_id=row.id,
                        now=now,
                    )
                return AnalysisCommitResult(tuple(committed), tuple(negative))

    async def start_inference(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        manifest: InputManifest,
        response_schema: Mapping[str, Any],
        response_schema_hash: str,
        cache_key: StageCacheKey | None,
        request_summary: Mapping[str, Any],
        now: datetime,
    ) -> StartedInferenceCall | None:
        profile = snapshot.stage.inference_profile
        provider_adapter = sanitize_identifier(
            profile.provider_adapter,
            field="provider adapter",
            max_length=128,
        )
        model_name = sanitize_identifier(
            profile.model_name,
            field="model name",
            max_length=256,
        )
        sanitized_summary = sanitize_json(request_summary)
        sanitized_schema = sanitize_json(response_schema)
        if not isinstance(sanitized_summary, dict) or not isinstance(sanitized_schema, dict):
            raise DomainValidationError("inference request audit values must be JSON objects")

        async with self._database.session() as session:
            async with session.begin():
                rows = await self._lock_snapshot_targets(
                    session=session,
                    snapshot=snapshot,
                    now=now,
                )
                if len(rows) != len(snapshot.targets):
                    return None
                manifest_row = _new_manifest(
                    stage_version_id=UUID(snapshot.stage.version_id),
                    manifest=manifest,
                    created_at=now,
                )
                inference_call = InferenceCallRecord(
                    id=uuid4(),
                    input_manifest_id=manifest_row.id,
                    analysis_stage_template_version_id=UUID(snapshot.stage.version_id),
                    provider_adapter=provider_adapter,
                    model_name=model_name,
                    request_summary=sanitized_summary,
                    structured_output_schema=sanitized_schema,
                    structured_output_schema_hash=response_schema_hash,
                    status="pending",
                    attempt=snapshot.claim.attempt_count,
                    raw_response=None,
                    token_usage=None,
                    latency_ms=None,
                    http_status=None,
                    error_code=None,
                    error_type=None,
                    retryable=None,
                    completed_at=None,
                    created_at=now,
                )
                session.add(manifest_row)
                session.add(inference_call)
                for row in rows.values():
                    row.input_manifest_id = manifest_row.id
                    row.inference_call_id = inference_call.id
                    row.cache_policy = snapshot.stage.cache_policy.value
                    row.cache_scope_identity = cache_key.scope_identity if cache_key else None
                    row.semantic_cache_key = cache_key.value if cache_key else None
                    row.updated_at = now
            return StartedInferenceCall(
                inference_call_id=str(inference_call.id),
                input_manifest_id=str(manifest_row.id),
            )

    async def record_inference_success(
        self,
        *,
        started: StartedInferenceCall,
        response: InferenceResponse,
        now: datetime,
    ) -> bool:
        raw_response = sanitize_json(response.raw_response)
        token_usage = (
            sanitize_json(response.token_usage) if response.token_usage is not None else None
        )
        if not isinstance(raw_response, dict):
            raw_response = {"value": raw_response}
        if token_usage is not None and not isinstance(token_usage, dict):
            token_usage = {"value": token_usage}
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(InferenceCallRecord)
                    .where(
                        InferenceCallRecord.id == UUID(started.inference_call_id),
                        InferenceCallRecord.input_manifest_id == UUID(started.input_manifest_id),
                        InferenceCallRecord.status == "pending",
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                row.status = "succeeded"
                row.raw_response = raw_response
                row.token_usage = token_usage
                row.latency_ms = _nonnegative_float(response.latency_ms)
                row.http_status = _http_status(response.http_status)
                row.error_code = None
                row.error_type = None
                row.retryable = None
                row.completed_at = now
                return True

    async def record_inference_failure(
        self,
        *,
        started: StartedInferenceCall,
        code: AnalysisErrorCode,
        error_type: str,
        retryable: bool,
        raw_response: Mapping[str, Any] | None,
        latency_ms: float | None,
        http_status: int | None,
        token_usage: Mapping[str, Any] | None,
        now: datetime,
    ) -> bool:
        sanitized_response = sanitize_json(raw_response) if raw_response is not None else None
        sanitized_usage = sanitize_json(token_usage) if token_usage is not None else None
        if sanitized_response is not None and not isinstance(sanitized_response, dict):
            sanitized_response = {"value": sanitized_response}
        if sanitized_usage is not None and not isinstance(sanitized_usage, dict):
            sanitized_usage = {"value": sanitized_usage}
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(InferenceCallRecord)
                    .where(
                        InferenceCallRecord.id == UUID(started.inference_call_id),
                        InferenceCallRecord.input_manifest_id == UUID(started.input_manifest_id),
                        InferenceCallRecord.status == "pending",
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                row.status = "failed"
                row.raw_response = sanitized_response
                row.token_usage = sanitized_usage
                row.latency_ms = _nonnegative_float(latency_ms)
                row.http_status = _http_status(http_status)
                row.error_code = sanitize_error_code(code)
                row.error_type = sanitize_error_type(error_type)
                row.retryable = bool(retryable)
                row.completed_at = now
                return True

    async def commit_validated_targets(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        started: StartedInferenceCall,
        results: tuple[ValidatedTargetResult, ...],
        now: datetime,
    ) -> AnalysisCommitResult:
        if not results:
            return AnalysisCommitResult(())
        target_ids = {result.target_id for result in results}
        async with self._database.session() as session:
            async with session.begin():
                rows = await self._lock_snapshot_targets(
                    session=session,
                    snapshot=snapshot,
                    now=now,
                    target_ids=target_ids,
                )
                committed: list[str] = []
                negative: list[NegativeGateAssignment] = []
                for result in results:
                    row = rows.get(result.target_id)
                    if row is None or row.inference_call_id != UUID(started.inference_call_id):
                        continue
                    parsed_result = _validated_result_document(result)
                    row.status = "succeeded"
                    row.parsed_result = parsed_result
                    row.result_origin = "inference"
                    row.reused_from_stage_run_id = None
                    _clear_stage_lease(row=row, now=now, completed=True)
                    committed.append(result.target_id)
                    if snapshot.claim.run_mode != "test":
                        for label in result.labels:
                            assignment = ModelLabelAssignmentRecord(
                                id=uuid4(),
                                stage_run_id=row.id,
                                analysis_run_id=row.analysis_run_id,
                                message_id=row.message_id,
                                target_scope=row.target_scope,
                                target_kind=row.target_kind,
                                target_id=row.target_id,
                                image_asset_id=row.image_asset_id,
                                video_asset_id=row.video_asset_id,
                                label_definition_version_id=UUID(label.label_definition_version_id),
                                label_key=label.key,
                                score=label.score,
                                activated=label.activated,
                                negative=label.negative,
                                reason=result.reason,
                                evidence=list(result.evidence),
                                created_at=now,
                            )
                            session.add(assignment)
                            if label.activated and label.negative:
                                negative.append(
                                    NegativeGateAssignment(
                                        message_id=str(row.message_id),
                                        stage_run_id=str(row.id),
                                        assignment_id=str(assignment.id),
                                    )
                                )
                    await self._set_wakeup_terminal(
                        session=session,
                        entity_id=row.id,
                        now=now,
                    )
                return AnalysisCommitResult(tuple(committed), tuple(negative))

    async def retry_or_fail_targets(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        target_ids: tuple[str, ...],
        code: AnalysisErrorCode,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        selected = set(target_ids)
        if not selected:
            return False
        async with self._database.session() as session:
            async with session.begin():
                rows = await self._lock_snapshot_targets(
                    session=session,
                    snapshot=snapshot,
                    now=now,
                    target_ids=selected,
                )
                changed = False
                for target_id in target_ids:
                    row = rows.get(target_id)
                    if row is None:
                        continue
                    await self._retry_or_fail_row(
                        session=session,
                        row=row,
                        code=code,
                        error_type=error_type,
                        retryable=retryable,
                        retry_at=retry_at,
                        now=now,
                    )
                    changed = True
                return changed

    async def retry_or_fail_claim(
        self,
        *,
        claim: ClaimedAnalysisStageRun,
        code: AnalysisErrorCode,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(StageRunRecord)
                    .where(
                        StageRunRecord.id == UUID(claim.stage_run_id),
                        StageRunRecord.status == "running",
                        StageRunRecord.lease_token == UUID(claim.lease_token),
                        StageRunRecord.lease_expires_at > now,
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                await self._retry_or_fail_row(
                    session=session,
                    row=row,
                    code=code,
                    error_type=error_type,
                    retryable=retryable,
                    retry_at=retry_at,
                    now=now,
                )
                return True

    async def apply_negative_gate(
        self,
        *,
        analysis_run_id: str,
        assignments: tuple[NegativeGateAssignment, ...],
        now: datetime,
    ) -> bool:
        if not assignments:
            return False
        run_id = UUID(analysis_run_id)
        requested_ids = [UUID(item.assignment_id) for item in assignments]
        async with self._database.session() as session:
            async with session.begin():
                run = await session.scalar(
                    select(AnalysisRunRecord)
                    .where(AnalysisRunRecord.id == run_id)
                    .with_for_update()
                )
                if run is None or run.run_mode == "test":
                    return False
                stored = tuple(
                    await session.scalars(
                        select(ModelLabelAssignmentRecord)
                        .where(
                            ModelLabelAssignmentRecord.id.in_(requested_ids),
                            ModelLabelAssignmentRecord.analysis_run_id == run_id,
                            ModelLabelAssignmentRecord.activated.is_(True),
                            ModelLabelAssignmentRecord.negative.is_(True),
                        )
                        .with_for_update()
                    )
                )
                by_id = {row.id: row for row in stored}
                if len(by_id) != len(set(requested_ids)):
                    return False

                causal_by_message: dict[UUID, ModelLabelAssignmentRecord] = {}
                for requested in assignments:
                    assignment = by_id[UUID(requested.assignment_id)]
                    if assignment.message_id != UUID(
                        requested.message_id
                    ) or assignment.stage_run_id != UUID(requested.stage_run_id):
                        return False
                    causal_by_message.setdefault(assignment.message_id, assignment)

                messages = tuple(
                    await session.scalars(
                        select(MessageRecord)
                        .where(MessageRecord.id.in_(tuple(causal_by_message)))
                        .with_for_update()
                    )
                )
                if len(messages) != len(causal_by_message):
                    return False
                for message in messages:
                    if not message.blocked_from_analysis:
                        cause = causal_by_message[message.id]
                        message.blocked_from_analysis = True
                        message.blocked_at = now
                        message.blocked_by_stage_run_id = cause.stage_run_id
                        message.blocked_by_label_assignment_id = cause.id
                        message.updated_at = now

                remaining = tuple(
                    await session.scalars(
                        select(StageRunRecord)
                        .where(
                            StageRunRecord.analysis_run_id == run_id,
                            StageRunRecord.message_id.in_(tuple(causal_by_message)),
                            StageRunRecord.status.in_(_ACTIVE_STAGE_STATUSES),
                        )
                        .with_for_update()
                    )
                )
                for row in remaining:
                    row.status = "skipped_negative_gate"
                    row.next_retry_at = None
                    row.lease_token = None
                    row.lease_expires_at = None
                    row.completed_at = now
                    row.updated_at = now
                    await self._set_wakeup_terminal(
                        session=session,
                        entity_id=row.id,
                        now=now,
                    )
                run.status = "blocked_negative_gate"
                run.started_at = run.started_at or now
                run.completed_at = now
                run.last_error_code = None
                run.last_error_type = None
                run.updated_at = now
                return True

    async def complete_analysis_run(self, *, analysis_run_id: str, now: datetime) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                run = await session.scalar(
                    select(AnalysisRunRecord)
                    .where(AnalysisRunRecord.id == UUID(analysis_run_id))
                    .with_for_update()
                )
                if run is None:
                    return False
                stages = tuple(
                    await session.scalars(
                        select(StageRunRecord)
                        .where(StageRunRecord.analysis_run_id == run.id)
                        .order_by(StageRunRecord.completed_at, StageRunRecord.id)
                        .with_for_update()
                    )
                )
                statuses = {row.status for row in stages}
                run.started_at = run.started_at or now
                if statuses.intersection(_ACTIVE_STAGE_STATUSES):
                    run.status = "running"
                    run.completed_at = None
                    run.last_error_code = None
                    run.last_error_type = None
                elif "failed" in statuses:
                    failure = next(row for row in stages if row.status == "failed")
                    run.status = "failed"
                    run.completed_at = now
                    run.last_error_code = sanitize_error_code(failure.last_error_code)
                    run.last_error_type = sanitize_error_type(failure.last_error_type)
                elif run.status == "blocked_negative_gate" or "skipped_negative_gate" in statuses:
                    run.status = "blocked_negative_gate"
                    run.completed_at = run.completed_at or now
                    run.last_error_code = None
                    run.last_error_type = None
                else:
                    run.status = "completed"
                    run.completed_at = now
                    run.last_error_code = None
                    run.last_error_type = None
                run.updated_at = now
                return True

    async def _lock_snapshot_targets(
        self,
        *,
        session: AsyncSession,
        snapshot: AnalysisStageSnapshot,
        now: datetime,
        target_ids: set[str] | None = None,
    ) -> dict[str, StageRunRecord]:
        selected_targets = tuple(
            target
            for target in snapshot.targets
            if target_ids is None or target.target_id in target_ids
        )
        if not selected_targets:
            return {}
        rows = tuple(
            await session.scalars(
                select(StageRunRecord)
                .where(
                    StageRunRecord.id.in_(
                        tuple(UUID(target.stage_run_id) for target in selected_targets)
                    ),
                    StageRunRecord.status == "running",
                    StageRunRecord.lease_expires_at > now,
                )
                .with_for_update()
            )
        )
        expected = {
            UUID(target.stage_run_id): (target.target_id, UUID(target.lease_token))
            for target in selected_targets
        }
        active: dict[str, StageRunRecord] = {}
        for row in rows:
            target_id, lease_token = expected[row.id]
            if row.lease_token == lease_token and str(row.target_id) == target_id:
                active[target_id] = row
        return active

    async def _retry_or_fail_row(
        self,
        *,
        session: AsyncSession,
        row: StageRunRecord,
        code: AnalysisErrorCode,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> None:
        row.last_error_code = sanitize_error_code(code)
        row.last_error_type = sanitize_error_type(error_type)
        row.retryable = bool(retryable)
        row.last_failure_at = now
        row.lease_token = None
        row.lease_expires_at = None
        row.updated_at = now
        if retryable and row.attempt_count < row.max_attempts:
            row.status = "retry_wait"
            row.next_retry_at = retry_at
            row.completed_at = None
            await self._reschedule_wakeup(
                session=session,
                entity_id=row.id,
                retry_at=retry_at,
                now=now,
            )
        else:
            row.status = "failed"
            row.next_retry_at = None
            row.completed_at = now
            await self._set_wakeup_terminal(
                session=session,
                entity_id=row.id,
                now=now,
            )

    async def _terminalize_exhausted(
        self,
        *,
        session: AsyncSession,
        row: StageRunRecord,
        run: AnalysisRunRecord,
        now: datetime,
    ) -> None:
        row.status = "failed"
        row.next_retry_at = None
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error_code = "LEASE_EXPIRED"
        row.last_error_type = "WorkerLeaseExpired"
        row.retryable = False
        row.last_failure_at = now
        row.completed_at = now
        row.updated_at = now
        await self._set_wakeup_terminal(session=session, entity_id=row.id, now=now)
        await session.flush()
        statuses = set(
            await session.scalars(
                select(StageRunRecord.status).where(StageRunRecord.analysis_run_id == run.id)
            )
        )
        run.started_at = run.started_at or now
        if statuses.intersection(_ACTIVE_STAGE_STATUSES):
            run.status = "running"
            run.completed_at = None
        else:
            run.status = "failed"
            run.completed_at = now
            run.last_error_code = row.last_error_code
            run.last_error_type = row.last_error_type
        run.updated_at = now

    @staticmethod
    async def _reschedule_wakeup(
        *,
        session: AsyncSession,
        entity_id: UUID,
        retry_at: datetime,
        now: datetime,
    ) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == ANALYSIS_QUEUE,
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
        session: AsyncSession,
        entity_id: UUID,
        now: datetime,
    ) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == ANALYSIS_QUEUE,
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


def _stage_from_records(
    *,
    stage: AnalysisStageTemplateVersionRecord,
    prompt: PromptTemplateVersionRecord,
    label_set: LabelSetVersionRecord,
    inference_profile: InferenceProfileVersionRecord,
    bindings: tuple[Any, ...],
) -> AnalysisStageTemplateVersion:
    label_bindings = tuple(
        LabelBinding(
            label=LabelDefinitionVersion(
                version_id=str(label.id),
                definition_id=str(label.label_definition_id),
                version_number=label.version_number,
                key=label.key,
                display_name=label.display_name,
                description=label.description,
                scope=LabelScope(label.scope),
                negative=label.negative,
                enabled=label.enabled,
                status=VersionStatus(label.state),
            ),
            activation_threshold=binding.activation_threshold,
            output_order=binding.output_order,
            prompt_hint=binding.prompt_hint,
        )
        for binding, label in bindings
    )
    input_policy = stage.input_policy
    output_policy = stage.structured_output_policy
    retry_policy = stage.retry_policy
    return AnalysisStageTemplateVersion(
        version_id=str(stage.id),
        stage_template_id=str(stage.analysis_stage_template_id),
        version_number=stage.version_number,
        name=stage.name,
        target_scope=LabelScope(stage.target_scope),
        execution_mode=ExecutionMode(stage.execution_mode),
        prompt=PromptTemplateVersion(
            version_id=str(prompt.id),
            prompt_template_id=str(prompt.prompt_template_id),
            version_number=prompt.version_number,
            system_prompt=prompt.system_prompt,
            user_prompt_template=prompt.user_prompt_template,
            declared_variables=tuple(prompt.declared_variables),
            status=VersionStatus(prompt.state),
        ),
        label_set=LabelSetVersion(
            version_id=str(label_set.id),
            label_set_id=str(label_set.label_set_id),
            version_number=label_set.version_number,
            bindings=label_bindings,
            status=VersionStatus(label_set.state),
        ),
        structured_output_policy=StructuredOutputPolicy(
            mode=StructuredOutputMode(output_policy.get("mode", "dense_scores")),
            include_reason=bool(output_policy.get("include_reason", False)),
            include_evidence=bool(output_policy.get("include_evidence", False)),
        ),
        input_policy=StageInputPolicy(
            text=bool(input_policy.get("text", False)),
            telegram_metadata=bool(input_policy.get("telegram_metadata", False)),
            media=bool(input_policy.get("media", False)),
            previous_results=bool(input_policy.get("previous_results", False)),
            source_channel_context=bool(input_policy.get("source_channel_context", False)),
        ),
        visual_composition_policy=VisualCompositionPolicy(stage.visual_composition_policy),
        inference_profile=InferenceProfileVersion(
            version_id=str(inference_profile.id),
            inference_profile_id=str(inference_profile.inference_profile_id),
            version_number=inference_profile.version_number,
            provider_adapter=inference_profile.provider_adapter,
            base_url=inference_profile.base_url,
            model_name=inference_profile.model_name,
            api_secret_reference=inference_profile.api_secret_reference,
            capabilities=tuple(inference_profile.capabilities),
            timeout_seconds=inference_profile.timeout_seconds,
            max_concurrency=inference_profile.max_concurrency,
            max_images=inference_profile.max_images,
            max_input_tokens=inference_profile.max_input_tokens,
            structured_output_support=inference_profile.structured_output_support,
            sampling_parameters=inference_profile.sampling_parameters,
            status=VersionStatus(inference_profile.state),
        ),
        cache_policy=CachePolicy(stage.cache_policy),
        timeout_seconds=stage.timeout_seconds,
        retry_policy=StageRetryPolicy(
            max_attempts=int(retry_policy.get("max_attempts", 3)),
            base_delay_seconds=int(retry_policy.get("base_delay_seconds", 5)),
            max_delay_seconds=int(retry_policy.get("max_delay_seconds", 300)),
        ),
        max_batch_size=stage.max_batch_size,
        max_concurrency=stage.max_concurrency,
        status=VersionStatus(stage.state),
    )


async def _load_media_snapshot(
    *,
    session: AsyncSession,
    stage_run: StageRunRecord,
    message: MessageRecord,
) -> tuple[tuple[Mapping[str, Any], ...], str | None]:
    image_statement = select(ImageAssetRecord).where(
        ImageAssetRecord.message_id == message.id,
        ImageAssetRecord.archive_state == "ready",
    )
    video_statement = select(VideoAssetRecord).where(
        VideoAssetRecord.message_id == message.id,
        VideoAssetRecord.archive_state == "ready",
    )
    if stage_run.target_kind == "image_asset":
        image_statement = image_statement.where(ImageAssetRecord.id == stage_run.image_asset_id)
        video_statement = video_statement.where(False)
    elif stage_run.target_kind == "video_asset":
        image_statement = image_statement.where(False)
        video_statement = video_statement.where(VideoAssetRecord.id == stage_run.video_asset_id)

    images = tuple(await session.scalars(image_statement.order_by(ImageAssetRecord.id)))
    videos = tuple(await session.scalars(video_statement.order_by(VideoAssetRecord.id)))
    frames_by_video: dict[UUID, list[VideoFrameRecord]] = {video.id: [] for video in videos}
    if videos:
        frames = tuple(
            await session.scalars(
                select(VideoFrameRecord)
                .where(VideoFrameRecord.video_asset_id.in_(tuple(frames_by_video)))
                .order_by(
                    VideoFrameRecord.video_asset_id,
                    VideoFrameRecord.frame_role,
                    VideoFrameRecord.frame_index,
                )
            )
        )
        for frame in frames:
            frames_by_video[frame.video_asset_id].append(frame)

    media: list[Mapping[str, Any]] = []
    media.extend(_image_manifest(image) for image in images)
    media.extend(_video_manifest(video, frames_by_video[video.id]) for video in videos)
    if stage_run.target_kind == "image_asset":
        identity = (images[0].archive_sha256 or images[0].perceptual_hash) if images else None
    elif stage_run.target_kind == "video_asset":
        identity = videos[0].source_sha256 if videos else None
    else:
        identity = message.visual_fingerprint
    return tuple(media), identity


async def _load_previous_results(
    *,
    session: AsyncSession,
    stage_run: StageRunRecord,
    facts_snapshot: Mapping[str, Any],
) -> Mapping[str, Any]:
    rows = tuple(
        await session.scalars(
            select(StageRunRecord)
            .where(
                StageRunRecord.analysis_run_id == stage_run.analysis_run_id,
                StageRunRecord.message_id == stage_run.message_id,
                StageRunRecord.id != stage_run.id,
                StageRunRecord.status == "succeeded",
                StageRunRecord.parsed_result.is_not(None),
            )
            .order_by(StageRunRecord.completed_at, StageRunRecord.id)
        )
    )
    stage_results: dict[str, Any] = {}
    for row in rows:
        key = str(row.analysis_stage_template_version_id)
        stage_results.setdefault(key, {})[str(row.target_id)] = sanitize_json(row.parsed_result)
    return {
        "facts_snapshot": sanitize_json(facts_snapshot),
        "stage_results": stage_results,
    }


def _image_manifest(image: ImageAssetRecord) -> Mapping[str, Any]:
    return {
        "kind": "image",
        "asset_id": str(image.id),
        "storage_backend": image.storage_backend,
        "storage_key": image.storage_key,
        "content_type": image.content_type,
        "width": image.width,
        "height": image.height,
        "source_sha256": image.source_sha256,
        "archive_sha256": image.archive_sha256,
        "perceptual_hash": image.perceptual_hash,
        "archive_size_bytes": image.archive_size_bytes,
    }


def _video_manifest(
    video: VideoAssetRecord,
    frames: Sequence[VideoFrameRecord],
) -> Mapping[str, Any]:
    return {
        "kind": "video",
        "asset_id": str(video.id),
        "source_content_type": video.source_content_type,
        "source_size_bytes": video.source_size_bytes,
        "source_sha256": video.source_sha256,
        "duration_seconds": video.duration_seconds,
        "source_width": video.source_width,
        "source_height": video.source_height,
        "frames": [
            {
                "frame_role": frame.frame_role,
                "frame_index": frame.frame_index,
                "timestamp_seconds": frame.timestamp_seconds,
                "storage_backend": frame.storage_backend,
                "storage_key": frame.storage_key,
                "content_type": frame.content_type,
                "width": frame.width,
                "height": frame.height,
                "source_sha256": frame.source_sha256,
                "archive_sha256": frame.archive_sha256,
                "perceptual_hash": frame.perceptual_hash,
                "archive_size_bytes": frame.archive_size_bytes,
            }
            for frame in frames
        ],
    }


def _new_manifest(
    *,
    stage_version_id: UUID,
    manifest: InputManifest,
    created_at: datetime,
) -> InputManifestRecord:
    return InputManifestRecord(
        id=uuid4(),
        analysis_stage_template_version_id=stage_version_id,
        manifest_version=manifest.manifest_version,
        content=manifest.content,
        input_manifest_hash=manifest.input_manifest_hash,
        created_at=created_at,
    )


def _validated_result_document(result: ValidatedTargetResult) -> dict[str, Any]:
    return {
        "target_id": result.target_id,
        "labels": [
            {
                "label_definition_version_id": label.label_definition_version_id,
                "key": label.key,
                "scope": label.scope.value,
                "score": label.score,
                "activated": label.activated,
                "negative": label.negative,
            }
            for label in result.labels
        ],
        "reason": result.reason,
        "evidence": list(result.evidence),
    }


def _rewrite_cached_target(value: Mapping[str, Any], *, target_id: str) -> dict[str, Any]:
    normalized = sanitize_json(value)
    if not isinstance(normalized, dict):
        normalized = {"value": normalized}
    normalized["target_id"] = target_id
    return normalized


def _copy_assignment(
    *,
    source: ModelLabelAssignmentRecord,
    stage_run: StageRunRecord,
    now: datetime,
) -> ModelLabelAssignmentRecord:
    return ModelLabelAssignmentRecord(
        id=uuid4(),
        stage_run_id=stage_run.id,
        analysis_run_id=stage_run.analysis_run_id,
        message_id=stage_run.message_id,
        target_scope=stage_run.target_scope,
        target_kind=stage_run.target_kind,
        target_id=stage_run.target_id,
        image_asset_id=stage_run.image_asset_id,
        video_asset_id=stage_run.video_asset_id,
        label_definition_version_id=source.label_definition_version_id,
        label_key=source.label_key,
        score=source.score,
        activated=source.activated,
        negative=source.negative,
        reason=source.reason,
        evidence=list(source.evidence),
        created_at=now,
    )


def _exact_cache_source(*, source: StageRunRecord | None, cache_key: StageCacheKey) -> bool:
    return bool(
        source is not None
        and source.status == "succeeded"
        and source.parsed_result is not None
        and source.semantic_cache_key == cache_key.value
        and source.cache_policy == cache_key.policy.value
        and source.cache_scope_identity == cache_key.scope_identity
    )


def _clear_stage_lease(*, row: StageRunRecord, now: datetime, completed: bool) -> None:
    row.next_retry_at = None
    row.lease_token = None
    row.lease_expires_at = None
    row.last_error_code = None
    row.last_error_type = None
    row.retryable = None
    row.last_failure_at = None
    row.completed_at = now if completed else None
    row.updated_at = now


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)
        or normalized == "token"
        or (normalized.endswith("token") and not normalized.endswith("tokens"))
    )


def _sanitize_string(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return value
    if parsed.username is not None or parsed.password is not None:
        return _REDACTED_URL
    sensitive_query_fragments = (
        "secret",
        "password",
        "apikey",
        "authorization",
        "token",
        "credential",
        "signature",
    )
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(fragment in normalized for fragment in sensitive_query_fragments):
            return _REDACTED_URL
    return value


def _nonnegative_float(value: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = float(value)
    return normalized if isfinite(normalized) and normalized >= 0 else None


def _http_status(value: int | None) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599
        else None
    )
