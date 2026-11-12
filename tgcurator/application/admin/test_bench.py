from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.application.analysis import (
    AnalysisInputError,
    AnalysisOrchestrator,
    AnalysisTargetInput,
)
from tgcurator.application.ports.analysis import InferenceProvider
from tgcurator.domain.analysis import AnalysisStageTemplateVersion
from tgcurator.shared import DomainValidationError

from .errors import AdminValidationError


@dataclass(frozen=True, slots=True)
class PromptTestBenchResult:
    """Complete side-effect-free inspection result for a Prompt Test Bench execution."""

    run_mode: str
    status: str
    system_prompt: str | None
    user_prompt: str
    input_manifest: Mapping[str, Any]
    input_manifest_hash: str
    structured_output_schema: Mapping[str, Any]
    structured_output_schema_hash: str
    raw_response: Mapping[str, Any] | None
    parsed_results: list[dict[str, Any]]
    invalid_targets: dict[str, str]
    missing_target_ids: list[str]
    negative_gate_would_trigger: bool
    token_usage: Mapping[str, Any] | None
    latency_ms: float | None
    error_code: str | None = None
    error_type: str | None = None


class PromptTestBenchService:
    """Execute `run_mode='test'` prompt/schema/provider trials with zero formal side effects."""

    def __init__(self, *, provider: InferenceProvider | None = None) -> None:
        self._orchestrator = AnalysisOrchestrator(provider=provider)

    async def run(
        self,
        *,
        stage: AnalysisStageTemplateVersion,
        targets: tuple[AnalysisTargetInput, ...],
        source_channel_context: Mapping[str, Any] | None = None,
        invoke_provider: bool = True,
    ) -> PromptTestBenchResult:
        if not targets:
            raise AdminValidationError("Prompt Test Bench requires at least one target input")
        try:
            prepared = self._orchestrator.prepare(
                stage=stage,
                targets=targets,
                source_channel_context=source_channel_context,
                attempt=1,
            )
        except (AnalysisInputError, DomainValidationError) as error:
            raise AdminValidationError(str(error)) from error

        manifest_dict = prepared.request.input_manifest.content
        manifest_hash = prepared.request.input_manifest.input_manifest_hash
        schema_doc = prepared.schema.document
        schema_hash = prepared.schema.schema_hash

        if not invoke_provider:
            return PromptTestBenchResult(
                run_mode="test",
                status="preview_only",
                system_prompt=prepared.request.system_prompt,
                user_prompt=prepared.request.user_prompt,
                input_manifest=manifest_dict,
                input_manifest_hash=manifest_hash,
                structured_output_schema=schema_doc,
                structured_output_schema_hash=schema_hash,
                raw_response=None,
                parsed_results=[],
                invalid_targets={},
                missing_target_ids=[],
                negative_gate_would_trigger=False,
                token_usage=None,
                latency_ms=None,
            )

        invocation = await self._orchestrator.invoke(prepared)
        if invocation.failure is not None:
            failure = invocation.failure
            return PromptTestBenchResult(
                run_mode="test",
                status="provider_failed",
                system_prompt=prepared.request.system_prompt,
                user_prompt=prepared.request.user_prompt,
                input_manifest=manifest_dict,
                input_manifest_hash=manifest_hash,
                structured_output_schema=schema_doc,
                structured_output_schema_hash=schema_hash,
                raw_response=(
                    dict(failure.raw_response) if failure.raw_response is not None else None
                ),
                parsed_results=[],
                invalid_targets={},
                missing_target_ids=[],
                negative_gate_would_trigger=False,
                token_usage=dict(failure.token_usage) if failure.token_usage is not None else None,
                latency_ms=failure.latency_ms,
                error_code=failure.code.value,
                error_type=failure.error_type,
            )

        assert invocation.response is not None
        try:
            validated = self._orchestrator.validate(
                stage=stage,
                targets=targets,
                response=invocation.response,
            )
        except DomainValidationError as error:
            return PromptTestBenchResult(
                run_mode="test",
                status="validation_failed",
                system_prompt=prepared.request.system_prompt,
                user_prompt=prepared.request.user_prompt,
                input_manifest=manifest_dict,
                input_manifest_hash=manifest_hash,
                structured_output_schema=schema_doc,
                structured_output_schema_hash=schema_hash,
                raw_response=dict(invocation.response.raw_response),
                parsed_results=[],
                invalid_targets={},
                missing_target_ids=[],
                negative_gate_would_trigger=False,
                token_usage=dict(invocation.response.token_usage),
                latency_ms=invocation.response.latency_ms,
                error_code="invalid_structured_output",
                error_type=type(error).__name__,
            )

        assert validated.validation is not None
        parsed_items: list[dict[str, Any]] = []
        for target_result in validated.validation.valid_results:
            parsed_items.append(
                {
                    "target_id": target_result.target_id,
                    "reason": target_result.reason,
                    "evidence": list(target_result.evidence),
                    "labels": [
                        {
                            "label_definition_version_id": item.label_definition_version_id,
                            "label_key": item.key,
                            "scope": item.scope.value,
                            "score": item.score,
                            "activated": item.activated,
                            "negative": item.negative,
                        }
                        for item in target_result.labels
                    ],
                }
            )

        invalid_targets = {
            issue.target_id or "_payload": f"{issue.code.value}:{issue.error_type}"
            for issue in validated.validation.issues
        }
        return PromptTestBenchResult(
            run_mode="test",
            status="completed" if validated.validation.complete else "validation_failed",
            system_prompt=prepared.request.system_prompt,
            user_prompt=prepared.request.user_prompt,
            input_manifest=manifest_dict,
            input_manifest_hash=manifest_hash,
            structured_output_schema=schema_doc,
            structured_output_schema_hash=schema_hash,
            raw_response=dict(invocation.response.raw_response),
            parsed_results=parsed_items,
            invalid_targets=invalid_targets,
            missing_target_ids=list(validated.validation.retry_target_ids),
            negative_gate_would_trigger=validated.negative_gate_hit,
            token_usage=dict(invocation.response.token_usage),
            latency_ms=invocation.response.latency_ms,
        )
