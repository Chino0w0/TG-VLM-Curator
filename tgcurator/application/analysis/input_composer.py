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
    cache_manifest_hash: str | None = None
    cache_schema_hash: str | None = None


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
        allow_metadata_probe: bool = False,
    ) -> ComposedAnalysisInput:
        if not isinstance(stage, AnalysisStageTemplateVersion):
            raise DomainValidationError("stage must be an AnalysisStageTemplateVersion")
        _validate_targets(stage=stage, targets=targets)
        _validate_visual_inputs(
            stage=stage,
            targets=targets,
            visual_inputs=visual_inputs,
            allow_metadata_probe=allow_metadata_probe,
        )
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
            scope_identity = _cache_scope_identity(
                stage=stage,
                targets=targets,
                visual_inputs=visual_inputs,
            )
            cache_manifest_hash, cache_schema_hash = _cache_projection_hashes(
                stage=stage,
                targets=targets,
                manifest=manifest,
                schema=schema,
            )
        except DomainValidationError as error:
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                error_type=type(error).__name__,
            ) from error
        return ComposedAnalysisInput(
            rendered_prompt=rendered,
            input_manifest=manifest,
            cache_scope_identity=scope_identity,
            visual_inputs=visual_inputs,
            cache_manifest_hash=cache_manifest_hash,
            cache_schema_hash=cache_schema_hash,
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


def _is_sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _media_item_content_hash(item: Mapping[str, Any]) -> str | None:
    archive_sha = item.get("archive_sha256")
    if _is_sha256_hex(archive_sha):
        return str(archive_sha)
    frames = item.get("frames")
    frame_hashes: list[list[Any]] = []
    if isinstance(frames, (list, tuple)):
        for frame in frames:
            if isinstance(frame, Mapping) and _is_sha256_hex(frame.get("archive_sha256")):
                frame_hashes.append(
                    [
                        frame.get("frame_role"),
                        frame.get("frame_index"),
                        frame.get("archive_sha256"),
                    ]
                )
    source_sha = item.get("source_sha256")
    if frame_hashes:
        return canonical_hash(
            {
                "source_sha256": source_sha if _is_sha256_hex(source_sha) else None,
                "frames": frame_hashes,
            }
        )
    if _is_sha256_hex(source_sha):
        return str(source_sha)
    return None


def _target_content_sha256(
    *,
    target: AnalysisTargetInput,
    visual_inputs: tuple[InferenceImage, ...],
) -> str | None:
    media_hashes = [
        digest for item in target.media if (digest := _media_item_content_hash(item)) is not None
    ]
    if len(media_hashes) == 1 and len(target.media) == 1:
        return media_hashes[0]
    if media_hashes and len(media_hashes) == len(target.media):
        return canonical_hash(media_hashes)

    resolved_sources = [
        source.get("sha256")
        for image in visual_inputs
        for source in image.sources
        if source.get("target_id") == target.target_id and _is_sha256_hex(source.get("sha256"))
    ]
    if len(resolved_sources) == 1:
        return str(resolved_sources[0])
    if resolved_sources:
        return canonical_hash(resolved_sources)

    if _is_sha256_hex(target.visual_identity):
        return str(target.visual_identity)
    return None


def _cache_scope_identity(
    *,
    stage: AnalysisStageTemplateVersion,
    targets: tuple[AnalysisTargetInput, ...],
    visual_inputs: tuple[InferenceImage, ...],
) -> str | None:
    policy = stage.cache_policy
    if policy is CachePolicy.NONE:
        return None
    if policy is CachePolicy.MESSAGE:
        identities = [target.message_id for target in targets]
    elif policy is CachePolicy.MESSAGE_VISUAL:
        identities = []
        for target in targets:
            digest = _target_content_sha256(target=target, visual_inputs=visual_inputs)
            if digest is None:
                raise AnalysisInputError(
                    code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                    error_type="MissingVisualCacheIdentity",
                )
            identities.append(digest)
    else:
        if any(target.asset_id is None for target in targets):
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                error_type="MissingAssetCacheIdentity",
            )
        identities = []
        for target in targets:
            digest = _target_content_sha256(target=target, visual_inputs=visual_inputs)
            if digest is None:
                if stage.input_policy.media:
                    raise AnalysisInputError(
                        code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                        error_type="MissingAssetCacheIdentity",
                    )
                digest = str(target.asset_id)
            identities.append(digest)
    return identities[0] if len(identities) == 1 else canonical_hash(identities)


def _cache_projection_hashes(
    *,
    stage: AnalysisStageTemplateVersion,
    targets: tuple[AnalysisTargetInput, ...],
    manifest: InputManifest,
    schema: StructuredOutputSchema,
) -> tuple[str, str]:
    if stage.cache_policy not in {CachePolicy.ASSET, CachePolicy.MESSAGE_VISUAL}:
        return manifest.input_manifest_hash, schema.schema_hash

    target_map = {target.target_id: f"$target_{idx}" for idx, target in enumerate(targets)}
    message_map: dict[str, str] = {}
    asset_map: dict[str, str] = {}
    for idx, target in enumerate(targets):
        message_map.setdefault(target.message_id, f"$message_{idx}")
        if target.asset_id is not None:
            asset_map.setdefault(target.asset_id, f"$asset_{idx}")
        for m_idx, item in enumerate(target.media):
            raw_asset_id = item.get("asset_id")
            if isinstance(raw_asset_id, str) and raw_asset_id:
                asset_map.setdefault(raw_asset_id, f"$media_{idx}_{m_idx}")

    normalized_targets = tuple(
        AnalysisTargetInput(
            target_id=target_map[target.target_id],
            message_id=message_map[target.message_id],
            asset_id=asset_map[target.asset_id] if target.asset_id is not None else None,
            text=target.text,
            telegram_metadata=target.telegram_metadata,
            media=tuple(
                {
                    **dict(item),
                    **(
                        {"asset_id": asset_map[str(item["asset_id"])]}
                        if isinstance(item.get("asset_id"), str)
                        and str(item["asset_id"]) in asset_map
                        else {}
                    ),
                }
                for item in target.media
            ),
            previous_results=target.previous_results,
            visual_identity=target.visual_identity,
        )
        for target in targets
    )

    schema_doc = json.loads(schema.canonical_json)
    props = schema_doc.get("properties", {})
    if "results" in props and isinstance(props["results"], dict):
        items_schema = props["results"].get("items", {})
        item_props = items_schema.get("properties", {})
        for field_name in ("message_id", "asset_id"):
            if field_name in item_props and isinstance(item_props[field_name], dict):
                item_props[field_name] = {
                    **item_props[field_name],
                    "enum": [target.target_id for target in normalized_targets],
                }
    cache_schema_hash = canonical_hash(schema_doc)

    manifest_doc = manifest.content
    manifest_doc["target_ids"] = [target.target_id for target in normalized_targets]
    manifest_doc["structured_output_schema_hash"] = cache_schema_hash
    raw_inputs = manifest_doc.get("inputs", {})
    context = raw_inputs.get("source_channel_context", {})
    rebuilt_inputs = _manifest_inputs(
        stage=stage,
        targets=normalized_targets,
        context=context if isinstance(context, Mapping) else {},
    )
    raw_visual = raw_inputs.get("visual_inputs", [])
    normalized_visual: list[dict[str, Any]] = []
    if isinstance(raw_visual, list):
        for entry in raw_visual:
            if not isinstance(entry, Mapping):
                continue
            sources_out: list[dict[str, Any]] = []
            for src in entry.get("sources", []):
                if not isinstance(src, Mapping):
                    continue
                src_copy = dict(src)
                if str(src_copy.get("target_id", "")) in target_map:
                    src_copy["target_id"] = target_map[str(src_copy["target_id"])]
                if str(src_copy.get("message_id", "")) in message_map:
                    src_copy["message_id"] = message_map[str(src_copy["message_id"])]
                if str(src_copy.get("asset_id", "")) in asset_map:
                    src_copy["asset_id"] = asset_map[str(src_copy["asset_id"])]
                sources_out.append(src_copy)
            normalized_visual.append({**dict(entry), "sources": sources_out})

    prompt_values: dict[str, Any] = {}
    if "label_definitions" in stage.prompt.declared_variables:
        prompt_values["label_definitions"] = stage.label_set.prompt_definitions()
    if "message_text" in stage.prompt.declared_variables:
        prompt_values["message_text"] = _message_text(stage.execution_mode, normalized_targets)
    if "asset_manifest" in stage.prompt.declared_variables:
        prompt_values["asset_manifest"] = _asset_manifest(normalized_targets)
    if "previous_results" in stage.prompt.declared_variables:
        prompt_values["previous_results"] = {
            target.target_id: dict(target.previous_results or {}) for target in normalized_targets
        }
    if "source_channel_context" in stage.prompt.declared_variables:
        prompt_values["source_channel_context"] = (
            dict(context) if isinstance(context, Mapping) else {}
        )
    rendered = stage.prompt.render(prompt_values)
    user_prompt = rendered.user_prompt
    if normalized_visual:
        user_prompt = (
            user_prompt + "\nVisual input map (image indices are zero-based; "
            "region is [left, top, right, bottom] in sent-image pixels):\n"
            + json.dumps(normalized_visual, ensure_ascii=False, sort_keys=True)
        )
    rebuilt_inputs["media_required"] = stage.input_policy.media
    rebuilt_inputs["visual_policy"] = stage.visual_composition_policy.value
    rebuilt_inputs["visual_inputs"] = normalized_visual
    rebuilt_inputs["rendered_prompt"] = {
        "system": rendered.system_prompt,
        "user": user_prompt,
    }
    manifest_doc["inputs"] = rebuilt_inputs
    return canonical_hash(manifest_doc), cache_schema_hash


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
    allow_metadata_probe: bool = False,
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
    if allow_metadata_probe:
        for image in visual_inputs:
            if not _is_sha256_hex(image.sha256) or (image.size_bytes or 0) <= 0:
                raise AnalysisInputError(
                    code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                    error_type="VisualPayloadInvalid",
                    retryable=True,
                )
        return
    for image in visual_inputs:
        if not image.content or hashlib.sha256(image.content).hexdigest() != image.sha256:
            raise AnalysisInputError(
                code=AnalysisErrorCode.INPUT_BUILD_ERROR,
                error_type="VisualPayloadInvalid",
                retryable=True,
            )
