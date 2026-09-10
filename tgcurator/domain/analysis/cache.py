from __future__ import annotations

from dataclasses import dataclass

from tgcurator.shared import DomainValidationError

from .common import CachePolicy, canonical_hash, validate_hash, validate_non_blank


@dataclass(frozen=True, slots=True)
class StageCacheKey:
    policy: CachePolicy
    scope_identity: str
    value: str


class StageCacheKeyBuilder:
    """Build a cache identity that includes every result-defining semantic version."""

    def build(
        self,
        *,
        policy: CachePolicy,
        scope_identity: str | None,
        stage_template_version_id: str,
        prompt_version_id: str,
        label_set_version_id: str,
        structured_output_schema_hash: str,
        inference_profile_version_id: str,
        input_manifest_hash: str,
    ) -> StageCacheKey | None:
        if not isinstance(policy, CachePolicy):
            raise DomainValidationError("cache policy must be a CachePolicy")
        if policy is CachePolicy.NONE:
            return None
        identity = validate_non_blank(scope_identity or "", field="cache scope identity")
        validate_hash(structured_output_schema_hash, field="structured output schema hash")
        validate_hash(input_manifest_hash, field="input manifest hash")
        payload = {
            "cache_policy": policy.value,
            "scope_identity": identity,
            "stage_template_version_id": validate_non_blank(
                stage_template_version_id, field="stage template version_id"
            ),
            "prompt_version_id": validate_non_blank(prompt_version_id, field="prompt version_id"),
            "label_set_version_id": validate_non_blank(
                label_set_version_id, field="label set version_id"
            ),
            "structured_output_schema_hash": structured_output_schema_hash,
            "inference_profile_version_id": validate_non_blank(
                inference_profile_version_id, field="inference profile version_id"
            ),
            "input_manifest_hash": input_manifest_hash,
        }
        return StageCacheKey(policy=policy, scope_identity=identity, value=canonical_hash(payload))
