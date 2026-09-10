from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import (
    ExecutionMode,
    LabelScope,
    canonical_hash,
    canonical_json,
    load_json,
    require_json_object,
    validate_hash,
    validate_non_blank,
    validate_positive_int,
)


@dataclass(frozen=True, slots=True)
class InputManifest:
    """Canonical immutable snapshot of exactly what one inference call can observe."""

    manifest_version: int
    content_json: str
    input_manifest_hash: str

    def __post_init__(self) -> None:
        validate_positive_int(self.manifest_version, field="manifest version")
        content = load_json(self.content_json)
        if not isinstance(content, dict):
            raise DomainValidationError("input manifest content must be a JSON object")
        validate_hash(self.input_manifest_hash, field="input manifest hash")
        if canonical_json(content) != self.content_json:
            raise DomainValidationError("input manifest content must use canonical JSON")
        if canonical_hash(content) != self.input_manifest_hash:
            raise DomainValidationError("input manifest hash does not match its content")

    @property
    def content(self) -> dict[str, Any]:
        value = load_json(self.content_json)
        assert isinstance(value, dict)
        return value

    @classmethod
    def create(
        cls,
        *,
        manifest_version: int,
        target_scope: LabelScope,
        execution_mode: ExecutionMode,
        target_ids: tuple[str, ...],
        prompt_version_id: str,
        structured_output_schema_hash: str,
        inference_profile_version_id: str,
        inputs: Mapping[str, Any],
        provider_parameters: Mapping[str, Any] | None = None,
    ) -> InputManifest:
        validate_positive_int(manifest_version, field="manifest version")
        if not isinstance(target_scope, LabelScope):
            raise DomainValidationError("target_scope must be a LabelScope")
        if not isinstance(execution_mode, ExecutionMode):
            raise DomainValidationError("execution_mode must be an ExecutionMode")
        normalized_targets = _validate_target_ids(target_ids)
        prompt_version_id = validate_non_blank(prompt_version_id, field="prompt version_id")
        validate_hash(
            structured_output_schema_hash,
            field="structured output schema hash",
        )
        inference_profile_version_id = validate_non_blank(
            inference_profile_version_id, field="inference profile version_id"
        )
        normalized_inputs = require_json_object(inputs, field="manifest inputs")
        normalized_provider_parameters = require_json_object(
            provider_parameters or {}, field="provider parameters"
        )
        content = {
            "manifest_version": manifest_version,
            "target_scope": target_scope.value,
            "execution_mode": execution_mode.value,
            "target_ids": list(normalized_targets),
            "prompt_version_id": prompt_version_id,
            "structured_output_schema_hash": structured_output_schema_hash,
            "inference_profile_version_id": inference_profile_version_id,
            "inputs": normalized_inputs,
            "provider_parameters": normalized_provider_parameters,
        }
        serialized = canonical_json(content)
        return cls(
            manifest_version=manifest_version,
            content_json=serialized,
            input_manifest_hash=canonical_hash(content),
        )


def _validate_target_ids(target_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_ids, tuple) or not target_ids:
        raise DomainValidationError("manifest target_ids must be a non-empty tuple")
    normalized = tuple(
        validate_non_blank(target_id, field="manifest target ID") for target_id in target_ids
    )
    if len(set(normalized)) != len(normalized):
        raise DomainValidationError("manifest target_ids must be unique")
    return normalized
