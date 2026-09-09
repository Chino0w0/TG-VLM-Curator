from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.application.ports.analysis import (
    InferenceProvider,
    InferenceProviderFailure,
    InferenceRequest,
    InferenceResponse,
)
from tgcurator.domain.analysis import (
    AnalysisErrorCode,
    AnalysisStageTemplateVersion,
    StageCacheKey,
    StageCacheKeyBuilder,
    StructuredOutputSchema,
    StructuredOutputSchemaBuilder,
    StructuredOutputValidation,
    StructuredOutputValidator,
    evaluate_stage_negative_gate,
)
from tgcurator.shared import DomainValidationError

from .input_composer import AnalysisInputComposer, AnalysisInputError, AnalysisTargetInput


@dataclass(frozen=True, slots=True)
class AnalysisExecutionFailure:
    code: AnalysisErrorCode
    error_type: str
    retryable: bool
    raw_response: Mapping[str, Any] | None = None
    latency_ms: float | None = None
    http_status: int | None = None
    token_usage: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PreparedAnalysisCall:
    schema: StructuredOutputSchema
    request: InferenceRequest
    cache_key: StageCacheKey | None


@dataclass(frozen=True, slots=True)
class AnalysisInvocationResult:
    response: InferenceResponse | None
    failure: AnalysisExecutionFailure | None

    @property
    def succeeded(self) -> bool:
        return self.response is not None and self.failure is None


@dataclass(frozen=True, slots=True)
class AnalysisExecutionResult:
    validation: StructuredOutputValidation | None
    failure: AnalysisExecutionFailure | None
    response: InferenceResponse | None = None
    negative_gate_hit: bool = False

    @property
    def succeeded(self) -> bool:
        return self.failure is None and self.validation is not None


class AnalysisOrchestrator:
    """Provider-neutral preparation, inference, and post-validation flow."""

    def __init__(
        self,
        *,
        provider: InferenceProvider | None,
        schema_builder: StructuredOutputSchemaBuilder | None = None,
        input_composer: AnalysisInputComposer | None = None,
        validator: StructuredOutputValidator | None = None,
        cache_key_builder: StageCacheKeyBuilder | None = None,
    ) -> None:
        self._provider = provider
        self._schema_builder = schema_builder or StructuredOutputSchemaBuilder()
        self._input_composer = input_composer or AnalysisInputComposer()
        self._validator = validator or StructuredOutputValidator()
        self._cache_key_builder = cache_key_builder or StageCacheKeyBuilder()

    def prepare(
        self,
        *,
        stage: AnalysisStageTemplateVersion,
        targets: tuple[AnalysisTargetInput, ...],
        source_channel_context: Mapping[str, Any] | None = None,
        attempt: int = 1,
    ) -> PreparedAnalysisCall:
        schema = self._schema_builder.build(
            stage_template_version_id=stage.version_id,
            label_set=stage.label_set,
            execution_mode=stage.execution_mode,
            policy=stage.structured_output_policy,
            expected_target_ids=tuple(target.target_id for target in targets),
        )
        composed = self._input_composer.compose(
            stage=stage,
            targets=targets,
            schema=schema,
            source_channel_context=source_channel_context,
        )
        cache_key = self._cache_key_builder.build(
            policy=stage.cache_policy,
            scope_identity=composed.cache_scope_identity,
            stage_template_version_id=stage.version_id,
            prompt_version_id=stage.prompt.version_id,
            label_set_version_id=stage.label_set.version_id,
            structured_output_schema_hash=schema.schema_hash,
            inference_profile_version_id=stage.inference_profile.version_id,
            input_manifest_hash=composed.input_manifest.input_manifest_hash,
        )
        return PreparedAnalysisCall(
            schema=schema,
            request=InferenceRequest(
                profile=stage.inference_profile,
                system_prompt=composed.rendered_prompt.system_prompt,
                user_prompt=composed.rendered_prompt.user_prompt,
                input_manifest=composed.input_manifest,
                response_schema=schema.document,
                timeout_seconds=min(stage.timeout_seconds, stage.inference_profile.timeout_seconds),
                attempt=attempt,
            ),
            cache_key=cache_key,
        )

    async def invoke(self, prepared: PreparedAnalysisCall) -> AnalysisInvocationResult:
        """Invoke the provider without opening or retaining a database transaction."""

        if self._provider is None:
            return AnalysisInvocationResult(
                response=None,
                failure=AnalysisExecutionFailure(
                    AnalysisErrorCode.PROVIDER_ERROR,
                    "InferenceProviderNotConfigured",
                    True,
                ),
            )
        try:
            response = await self._provider.infer(prepared.request)
        except InferenceProviderFailure as error:
            return AnalysisInvocationResult(
                response=None,
                failure=AnalysisExecutionFailure(
                    error.code,
                    error.error_type,
                    error.retryable,
                    error.raw_response,
                    error.latency_ms,
                    error.http_status,
                    error.token_usage,
                ),
            )
        except Exception as error:
            return AnalysisInvocationResult(
                response=None,
                failure=AnalysisExecutionFailure(
                    AnalysisErrorCode.INTERNAL_ERROR,
                    type(error).__name__,
                    True,
                ),
            )
        return AnalysisInvocationResult(response=response, failure=None)

    def validate(
        self,
        *,
        stage: AnalysisStageTemplateVersion,
        targets: tuple[AnalysisTargetInput, ...],
        response: InferenceResponse,
    ) -> AnalysisExecutionResult:
        """Validate stable target mapping while retaining the complete provider audit response."""

        validation = self._validator.validate(
            payload=response.payload,
            execution_mode=stage.execution_mode,
            policy=stage.structured_output_policy,
            label_set=stage.label_set,
            expected_target_ids=tuple(target.target_id for target in targets),
        )
        gate = evaluate_stage_negative_gate(validation.valid_results)
        return AnalysisExecutionResult(
            validation=validation,
            failure=None,
            response=response,
            negative_gate_hit=gate.blocked,
        )

    async def execute(
        self,
        *,
        stage: AnalysisStageTemplateVersion,
        targets: tuple[AnalysisTargetInput, ...],
        source_channel_context: Mapping[str, Any] | None = None,
        attempt: int = 1,
    ) -> AnalysisExecutionResult:
        try:
            prepared = self.prepare(
                stage=stage,
                targets=targets,
                source_channel_context=source_channel_context,
                attempt=attempt,
            )
        except AnalysisInputError as error:
            return AnalysisExecutionResult(
                validation=None,
                failure=AnalysisExecutionFailure(error.code, error.error_type, False),
            )
        except DomainValidationError as error:
            return AnalysisExecutionResult(
                validation=None,
                failure=AnalysisExecutionFailure(
                    AnalysisErrorCode.INPUT_BUILD_ERROR,
                    type(error).__name__,
                    False,
                ),
            )
        except Exception as error:
            return AnalysisExecutionResult(
                validation=None,
                failure=AnalysisExecutionFailure(
                    AnalysisErrorCode.INTERNAL_ERROR,
                    type(error).__name__,
                    True,
                ),
            )

        invocation = await self.invoke(prepared)
        if invocation.failure is not None:
            return AnalysisExecutionResult(
                validation=None,
                failure=invocation.failure,
                response=None,
            )
        assert invocation.response is not None
        return self.validate(stage=stage, targets=targets, response=invocation.response)
