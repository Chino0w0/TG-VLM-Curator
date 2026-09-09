from __future__ import annotations

from dataclasses import dataclass

from tgcurator.shared import DomainValidationError

from .common import (
    CachePolicy,
    ExecutionMode,
    LabelScope,
    VersionStatus,
    VisualCompositionPolicy,
    validate_identifier,
    validate_non_blank,
    validate_positive_int,
)
from .labels import LabelSetVersion
from .profiles import InferenceProfileVersion
from .prompt import PromptTemplateVersion
from .schema import StructuredOutputPolicy


@dataclass(frozen=True, slots=True)
class StageInputPolicy:
    text: bool = False
    telegram_metadata: bool = False
    media: bool = False
    previous_results: bool = False
    source_channel_context: bool = False

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, bool)
            for value in (
                self.text,
                self.telegram_metadata,
                self.media,
                self.previous_results,
                self.source_channel_context,
            )
        ):
            raise DomainValidationError("stage input policy fields must be booleans")
        if not any(
            (
                self.text,
                self.telegram_metadata,
                self.media,
                self.previous_results,
                self.source_channel_context,
            )
        ):
            raise DomainValidationError("stage input policy must enable at least one input")

    @property
    def available_prompt_variables(self) -> frozenset[str]:
        values = {"label_definitions"}
        if self.text:
            values.add("message_text")
        if self.media:
            values.add("asset_manifest")
        if self.previous_results:
            values.add("previous_results")
        if self.source_channel_context or self.telegram_metadata:
            values.add("source_channel_context")
        return frozenset(values)


@dataclass(frozen=True, slots=True)
class StageRetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: int = 5
    max_delay_seconds: int = 300

    def __post_init__(self) -> None:
        validate_positive_int(self.max_attempts, field="stage max_attempts")
        validate_positive_int(self.base_delay_seconds, field="stage base_delay_seconds")
        validate_positive_int(self.max_delay_seconds, field="stage max_delay_seconds")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise DomainValidationError("stage max delay cannot be less than base delay")


@dataclass(frozen=True, slots=True)
class AnalysisStageTemplateVersion:
    version_id: str
    stage_template_id: str
    version_number: int
    name: str
    target_scope: LabelScope
    execution_mode: ExecutionMode
    prompt: PromptTemplateVersion
    label_set: LabelSetVersion
    structured_output_policy: StructuredOutputPolicy
    input_policy: StageInputPolicy
    visual_composition_policy: VisualCompositionPolicy
    inference_profile: InferenceProfileVersion
    cache_policy: CachePolicy
    timeout_seconds: float
    retry_policy: StageRetryPolicy
    max_batch_size: int = 1
    max_concurrency: int = 1
    status: VersionStatus = VersionStatus.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_id", validate_non_blank(self.version_id, field="stage version_id")
        )
        object.__setattr__(
            self,
            "stage_template_id",
            validate_non_blank(self.stage_template_id, field="stage template id"),
        )
        validate_positive_int(self.version_number, field="stage version number")
        object.__setattr__(self, "name", validate_identifier(self.name, field="stage name"))
        if not isinstance(self.target_scope, LabelScope):
            raise DomainValidationError("stage target_scope must be a LabelScope")
        if not isinstance(self.execution_mode, ExecutionMode):
            raise DomainValidationError("stage execution_mode must be an ExecutionMode")
        if not isinstance(self.prompt, PromptTemplateVersion):
            raise DomainValidationError("stage prompt must be a PromptTemplateVersion")
        if not isinstance(self.label_set, LabelSetVersion):
            raise DomainValidationError("stage label_set must be a LabelSetVersion")
        if not isinstance(self.structured_output_policy, StructuredOutputPolicy):
            raise DomainValidationError("stage structured_output_policy is invalid")
        if not isinstance(self.input_policy, StageInputPolicy):
            raise DomainValidationError("stage input_policy is invalid")
        if not isinstance(self.visual_composition_policy, VisualCompositionPolicy):
            raise DomainValidationError("stage visual_composition_policy is invalid")
        if not isinstance(self.inference_profile, InferenceProfileVersion):
            raise DomainValidationError("stage inference_profile is invalid")
        if not isinstance(self.cache_policy, CachePolicy):
            raise DomainValidationError("stage cache_policy is invalid")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise DomainValidationError("stage timeout_seconds must be positive")
        if not isinstance(self.retry_policy, StageRetryPolicy):
            raise DomainValidationError("stage retry_policy is invalid")
        validate_positive_int(self.max_batch_size, field="stage max_batch_size")
        validate_positive_int(self.max_concurrency, field="stage max_concurrency")
        if not isinstance(self.status, VersionStatus):
            raise DomainValidationError("stage status must be a VersionStatus")

        if self.target_scope is LabelScope.GLOBAL and self.execution_mode not in {
            ExecutionMode.BATCH_MESSAGES,
            ExecutionMode.SINGLE_MESSAGE,
        }:
            raise DomainValidationError("GLOBAL stages require a message execution mode")
        if self.target_scope is LabelScope.MEDIA and self.execution_mode not in {
            ExecutionMode.PER_ASSET,
            ExecutionMode.BATCH_ASSETS,
        }:
            raise DomainValidationError("MEDIA stages require an asset execution mode")
        if (
            self.execution_mode
            in {
                ExecutionMode.SINGLE_MESSAGE,
                ExecutionMode.PER_ASSET,
            }
            and self.max_batch_size != 1
        ):
            raise DomainValidationError("non-batch stage max_batch_size must be one")
        if self.cache_policy is CachePolicy.ASSET and self.target_scope is not LabelScope.MEDIA:
            raise DomainValidationError("asset cache policy requires MEDIA target scope")
        if self.cache_policy in {CachePolicy.MESSAGE, CachePolicy.MESSAGE_VISUAL} and (
            self.target_scope is not LabelScope.GLOBAL
        ):
            raise DomainValidationError("message cache policies require GLOBAL target scope")
        undeclared = set(self.prompt.declared_variables) - set(
            self.input_policy.available_prompt_variables
        )
        if undeclared:
            raise DomainValidationError(
                f"prompt variables are unavailable under the input policy: {sorted(undeclared)!r}"
            )
        if self.input_policy.media and "vision" not in self.inference_profile.capabilities:
            raise DomainValidationError("media input requires a vision-capable inference profile")

    @property
    def is_text_only_prescreen(self) -> bool:
        return (
            self.target_scope is LabelScope.GLOBAL
            and self.execution_mode is ExecutionMode.BATCH_MESSAGES
            and self.input_policy.text
            and not self.input_policy.media
        )

    def validate_for_publication(self) -> None:
        if self.prompt.status is not VersionStatus.PUBLISHED:
            raise DomainValidationError("published stage must reference a published prompt")
        if self.label_set.status is not VersionStatus.PUBLISHED:
            raise DomainValidationError("published stage must reference a published label set")
        if self.inference_profile.status is not VersionStatus.PUBLISHED:
            raise DomainValidationError(
                "published stage must reference a published inference profile"
            )
        if not self.inference_profile.structured_output_support:
            raise DomainValidationError("stage inference profile must support structured output")
        self.label_set.validate_for_publication(target_scope=self.target_scope)
