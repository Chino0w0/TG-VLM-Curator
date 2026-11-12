from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.application.ports.analysis import InferenceImage
from tgcurator.domain.analysis import (
    AnalysisErrorCode,
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    InputManifest,
    RenderedPrompt,
    StructuredOutputSchema,
    canonical_hash,
)
from tgcurator.shared import DomainValidationError


class AnalysisInputError(RuntimeError):
    def __init__(
        self, *, code: AnalysisErrorCode, error_type: str, retryable: bool = False
    ) -> None:
        super().__init__(error_type)
        self.code = code
        self.error_type = error_type
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AnalysisTargetInput:
    target_id: str
    message_id: str
    asset_id: str | None = None
    text: str | None = None
    telegram_metadata: Mapping[str, Any] | None = None
    media: tuple[Mapping[str, Any], ...] = ()
    previous_results: Mapping[str, Any] | None = None
    visual_identity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not self.target_id.strip():
            raise DomainValidationError("analysis target_id must not be blank")
        if not isinstance(self.message_id, str) or not self.message_id.strip():
            raise DomainValidationError("analysis message_id must not be blank")
        if self.asset_id is not None and (
            not isinstance(self.asset_id, str) or not self.asset_id.strip()
        ):
            raise DomainValidationError("analysis asset_id must be null or non-blank")
        if self.text is not None and not isinstance(self.text, str):
            raise DomainValidationError("analysis target text must be a string or null")
        if not isinstance(self.media, tuple) or any(
            not isinstance(item, Mapping) for item in self.media
        ):
            raise DomainValidationError("analysis target media must be a tuple of objects")


@dataclass(frozen=True, slots=True)
class ComposedAnalysisInput:
    rendered_prompt: RenderedPrompt
    input_manifest: InputManifest
    cache_scope_identity: str | None
    visual_inputs: tuple[InferenceImage, ...] = ()


class AnalysisInputComposer:
    MANIFEST_VERSION = 2

    def compose(
        self,
        *,
        stage: AnalysisStageTemplateVersion,
        targets: tuple[AnalysisTargetInput, ...],
        schema: StructuredOutputSchema,
        source_channel_context: Mapping[str, Any] | None = None,
        visual_inputs: tuple[InferenceImage, ...] = (),
    ) -> ComposedAnalysisInput:
        if not isinstance(stage, AnalysisStageTemplateVersion):
            raise DomainValidationError("stage must be an AnalysisStageTemplateVersion")
        _validate_targets(stage=stage, targets=targets)
        _validate_visual_inputs(stage=stage, targets=targets, visual_inputs=visual_inputs)
        visual_manifest = [image.manifest_record(i) for i, image in enumerate(visual_inputs)]
        context = dict(source_channel_context or {})
        prompt_values: dict[str, Any] = {}
        if "label_definitions" in stage.prompt.declared_variables:
            prompt_values["label_definitions"] = stage.label_set.prompt_definitions()
        if "message_text" in stage.prompt.declared_variables:
            prompt_values["message_text"] = _message_text(stage.execution_mode, targets)
        if "asset_manifest" in stage.prompt.declared_variables:
            prompt_values["asset_manifest"] = _asset_manifest(targets)
        if "previous_results" in stage.prompt.declared_variables:
            prompt_values["previous_results"] = {
                target.target_id: dict(target.previous_results or {}) for target in targets
            }
        if "source_channel_context" in stage.prompt.declared_variables:
            prompt_values["source_channel_context"] = context

        try:
            rendered = stage.prompt.render(prompt_values)
            if visual_manifest:
                rendered = RenderedPrompt(
                    system_prompt=rendered.system_prompt,
                    user_prompt=rendered.user_prompt
                    + "\nVisual input map (image indices are zero-based; "
                    "region is [left, top, right, bottom] in sent-image pixels):\n"
                    + json.dumps(visual_manifest, ensure_ascii=False, sort_keys=True),
                )
            manifest_inputs = _manifest_inputs(stage=stage, targets=targets, context=context)
            manifest_inputs["media_required"] = stage.input_policy.media
            manifest_inputs["visual_policy"] = stage.visual_composition_policy.value
            manifest_inputs["visual_inputs"] = visual_manifest
            manifest_inputs["rendered_prompt"] = {
                "system": rendered.system_prompt,
                "user": rendered.user_prompt,
            }
            manifest = InputManifest.create(
                manifest_version=self.MANIFEST_VERSION,
                target_scope=stage.target_scope,
                execution_mode=stage.execution_mode,
                target_ids=tuple(target.target_id for target in targets),
                prompt_version_id=stage.prompt.version_id,
                structured_output_schema_hash=schema.schema_hash,
                inference_profile_version_id=stage.inference_profile.version_id,
                inputs=manifest_inputs,
                provider_parameters=stage.inference_profile.sampling_parameters,
            )
        except DomainValidationError as error:
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                error_type=type(error).__name__,
            ) from error
        return ComposedAnalysisInput(
            rendered_prompt=rendered,
            input_manifest=manifest,
            cache_scope_identity=_cache_scope_identity(stage.cache_policy, targets),
            visual_inputs=visual_inputs,
        )


def _validate_targets(
    *, stage: AnalysisStageTemplateVersion, targets: tuple[AnalysisTargetInput, ...]
) -> None:
    if not isinstance(targets, tuple) or not targets:
        raise DomainValidationError("analysis targets must be a non-empty tuple")
    if any(not isinstance(target, AnalysisTargetInput) for target in targets):
        raise DomainValidationError("analysis targets must contain AnalysisTargetInput values")
    if len({target.target_id for target in targets}) != len(targets):
        raise DomainValidationError("analysis target IDs must be unique")
    if len(targets) > stage.max_batch_size:
        raise DomainValidationError("analysis target count exceeds stage max_batch_size")
    if stage.execution_mode in {ExecutionMode.SINGLE_MESSAGE, ExecutionMode.PER_ASSET} and (
        len(targets) != 1
    ):
        raise DomainValidationError("non-batch analysis requires one target")
    if stage.execution_mode in {ExecutionMode.BATCH_MESSAGES, ExecutionMode.SINGLE_MESSAGE}:
        if any(target.target_id != target.message_id for target in targets):
            raise DomainValidationError("message execution target_id must equal message_id")
    else:
        if any(
            target.asset_id is None or target.target_id != target.asset_id for target in targets
        ):
            raise DomainValidationError("asset execution target_id must equal asset_id")
    if stage.input_policy.media and any(not target.media for target in targets):
        raise AnalysisInputError(
            code=AnalysisErrorCode.MEDIA_UNAVAILABLE,
            error_type="RequiredMediaUnavailable",
        )


def _message_text(execution_mode: ExecutionMode, targets: tuple[AnalysisTargetInput, ...]) -> Any:
    if execution_mode is ExecutionMode.SINGLE_MESSAGE:
        return targets[0].text or ""
    return [{"message_id": target.message_id, "text": target.text or ""} for target in targets]


def _asset_manifest(targets: tuple[AnalysisTargetInput, ...]) -> list[dict[str, Any]]:
    return [
        {
            "target_id": target.target_id,
            "message_id": target.message_id,
            "asset_id": target.asset_id,
            "media": [_safe_media(item) for item in target.media],
        }
        for target in targets
    ]


def _manifest_inputs(
    *,
    stage: AnalysisStageTemplateVersion,
    targets: tuple[AnalysisTargetInput, ...],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for target in targets:
        item: dict[str, Any] = {
            "target_id": target.target_id,
            "message_id": target.message_id,
            "asset_id": target.asset_id,
        }
        if stage.input_policy.text:
            item["text"] = target.text or ""
        if stage.input_policy.telegram_metadata:
            item["telegram_metadata"] = dict(target.telegram_metadata or {})
        if stage.input_policy.media:
            item["media"] = [_safe_media(value) for value in target.media]
        if stage.input_policy.previous_results:
            item["previous_results"] = dict(target.previous_results or {})
        items.append(item)
    result: dict[str, Any] = {"targets": items}
    if stage.input_policy.source_channel_context:
        result["source_channel_context"] = dict(context)
    return result


def _cache_scope_identity(
    policy: CachePolicy, targets: tuple[AnalysisTargetInput, ...]
) -> str | None:
    if policy is CachePolicy.NONE:
        return None
    if policy is CachePolicy.MESSAGE:
        identities = [target.message_id for target in targets]
    elif policy is CachePolicy.MESSAGE_VISUAL:
        if any(not target.visual_identity for target in targets):
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                error_type="MissingVisualCacheIdentity",
            )
        identities = [str(target.visual_identity) for target in targets]
    else:
        if any(target.asset_id is None for target in targets):
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                error_type="MissingAssetCacheIdentity",
            )
        identities = [str(target.asset_id) for target in targets]
    return identities[0] if len(identities) == 1 else canonical_hash(identities)


def _safe_media(item: Mapping[str, Any]) -> dict[str, Any]:
    # Archive coordinates and URLs are not model input. In particular signed URLs,
    # arbitrary metadata, credentials and base64 must never enter the audit prompt.
    fields = {
        "kind",
        "asset_id",
        "width",
        "height",
        "source_width",
        "source_height",
        "archive_sha256",
        "source_sha256",
        "content_type",
        "source_content_type",
        "frame_role",
        "frame_index",
        "timestamp_seconds",
        "duration_seconds",
    }
    result = {
        key: value
        for key, value in item.items()
        if key in fields
        and (
            value is None
            or isinstance(value, (int, float, bool))
            or (
                isinstance(value, str)
                and len(value) <= 256
                and not value.startswith(("data:", "http:", "https:"))
            )
        )
    }
    frames = item.get("frames")
    if isinstance(frames, (list, tuple)):
        result["frames"] = [_safe_media(frame) for frame in frames if isinstance(frame, Mapping)]
    return result


def _validate_visual_inputs(
    *,
    stage: AnalysisStageTemplateVersion,
    targets: tuple[AnalysisTargetInput, ...],
    visual_inputs: tuple[InferenceImage, ...],
) -> None:
    if not stage.input_policy.media:
        if visual_inputs:
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR, error_type="UnexpectedVisualInputs"
            )
        return
    if not visual_inputs:
        raise AnalysisInputError(
            code=AnalysisErrorCode.MEDIA_UNAVAILABLE,
            error_type="UnresolvedVisualInputs",
            retryable=True,
        )
    if len(visual_inputs) > stage.inference_profile.max_images:
        raise AnalysisInputError(
            code=AnalysisErrorCode.INPUT_BUILD_ERROR,
            error_type="VisualImageBudgetExceeded",
            retryable=True,
        )
    represented = {source.get("target_id") for image in visual_inputs for source in image.sources}
    if represented != {target.target_id for target in targets}:
        raise AnalysisInputError(
            code=AnalysisErrorCode.MEDIA_UNAVAILABLE,
            error_type="VisualTargetMappingIncomplete",
            retryable=True,
        )

    from .visual_inputs import _sources

    expected_sources = _sources(targets)
    actual_sources = [source for image in visual_inputs for source in image.sources]
    if len(expected_sources) != len(actual_sources) or any(
        source.get("source_index") != index
        or any(source.get(key) != value for key, value in identity.items())
        for index, ((identity, _), source) in enumerate(
            zip(expected_sources, actual_sources, strict=True)
        )
    ):
        raise AnalysisInputError(
            code=AnalysisErrorCode.MEDIA_UNAVAILABLE,
            error_type="VisualSourceMappingIncomplete",
            retryable=True,
        )
    for image in visual_inputs:
        if not image.content or hashlib.sha256(image.content).hexdigest() != image.sha256:
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                error_type="VisualPayloadInvalid",
                retryable=True,
            )
