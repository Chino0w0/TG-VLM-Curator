from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from tgcurator.domain.analysis import (
    AnalysisErrorCode,
    AnalysisStageTemplateVersion,
    InferenceProfileVersion,
    InputManifest,
    StageCacheKey,
    ValidatedTargetResult,
)


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    profile: InferenceProfileVersion
    system_prompt: str
    user_prompt: str
    input_manifest: InputManifest
    response_schema: Mapping[str, Any]
    timeout_seconds: float
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    payload: Mapping[str, Any]
    provider_adapter: str
    model_name: str
    raw_response: Mapping[str, Any]
    latency_ms: float | None = None
    http_status: int | None = None
    token_usage: Mapping[str, Any] | None = None


class InferenceProviderFailure(RuntimeError):
    """Sanitized adapter failure carrying stable retry semantics."""

    def __init__(
        self,
        *,
        code: AnalysisErrorCode,
        error_type: str,
        retryable: bool,
        raw_response: Mapping[str, Any] | None = None,
        latency_ms: float | None = None,
        http_status: int | None = None,
        token_usage: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(error_type)
        self.code = code
        self.error_type = error_type
        self.retryable = retryable
        self.raw_response = dict(raw_response) if raw_response is not None else None
        self.latency_ms = latency_ms
        self.http_status = http_status
        self.token_usage = dict(token_usage) if token_usage is not None else None


class InferenceProvider(Protocol):
    async def infer(self, request: InferenceRequest) -> InferenceResponse: ...


class InferenceSecretResolver(Protocol):
    async def resolve(self, *, secret_reference: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ClaimedAnalysisStageRun:
    stage_run_id: str
    analysis_run_id: str
    message_id: str
    target_id: str
    analysis_stage_template_version_id: str
    lease_token: str
    attempt_count: int
    max_attempts: int
    run_mode: str


@dataclass(frozen=True, slots=True)
class AnalysisTargetSnapshot:
    stage_run_id: str
    lease_token: str
    target_id: str
    message_id: str
    target_kind: str
    asset_id: str | None = None
    text: str | None = None
    telegram_metadata: Mapping[str, Any] | None = None
    media: tuple[Mapping[str, Any], ...] = ()
    previous_results: Mapping[str, Any] | None = None
    visual_identity: str | None = None


@dataclass(frozen=True, slots=True)
class AnalysisStageSnapshot:
    claim: ClaimedAnalysisStageRun
    stage: AnalysisStageTemplateVersion
    targets: tuple[AnalysisTargetSnapshot, ...]
    source_channel_context: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AnalysisCacheHit:
    source_stage_run_id: str
    parsed_result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StartedInferenceCall:
    inference_call_id: str
    input_manifest_id: str


@dataclass(frozen=True, slots=True)
class NegativeGateAssignment:
    message_id: str
    stage_run_id: str
    assignment_id: str


@dataclass(frozen=True, slots=True)
class AnalysisCommitResult:
    committed_target_ids: tuple[str, ...]
    negative_gate_assignments: tuple[NegativeGateAssignment, ...] = ()


class AnalysisStageRunRepository(Protocol):
    async def claim(
        self,
        *,
        stage_run_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedAnalysisStageRun | None: ...

    async def load_stage_snapshot(
        self, *, claim: ClaimedAnalysisStageRun
    ) -> AnalysisStageSnapshot: ...

    async def find_cache_hit(
        self, *, snapshot: AnalysisStageSnapshot, cache_key: StageCacheKey
    ) -> AnalysisCacheHit | None: ...

    async def commit_cache_hit(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        manifest: InputManifest,
        cache_key: StageCacheKey,
        hit: AnalysisCacheHit,
        now: datetime,
    ) -> AnalysisCommitResult: ...

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
    ) -> StartedInferenceCall | None: ...

    async def record_inference_success(
        self,
        *,
        started: StartedInferenceCall,
        response: InferenceResponse,
        now: datetime,
    ) -> bool: ...

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
    ) -> bool: ...

    async def commit_validated_targets(
        self,
        *,
        snapshot: AnalysisStageSnapshot,
        started: StartedInferenceCall,
        results: tuple[ValidatedTargetResult, ...],
        now: datetime,
    ) -> AnalysisCommitResult: ...

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
    ) -> bool: ...

    async def retry_or_fail_claim(
        self,
        *,
        claim: ClaimedAnalysisStageRun,
        code: AnalysisErrorCode,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool: ...

    async def apply_negative_gate(
        self,
        *,
        analysis_run_id: str,
        assignments: tuple[NegativeGateAssignment, ...],
        now: datetime,
    ) -> bool: ...

    async def complete_analysis_run(self, *, analysis_run_id: str, now: datetime) -> bool: ...
