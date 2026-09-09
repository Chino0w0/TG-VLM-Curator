from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import (
    ExecutionMode,
    StructuredOutputMode,
    canonical_hash,
    canonical_json,
    validate_non_blank,
)
from .labels import LabelSetVersion

_BATCH_MODES = frozenset({ExecutionMode.BATCH_MESSAGES, ExecutionMode.BATCH_ASSETS})
_TARGET_FIELD_BY_MODE = {
    ExecutionMode.BATCH_MESSAGES: "message_id",
    ExecutionMode.BATCH_ASSETS: "asset_id",
}


@dataclass(frozen=True, slots=True)
class StructuredOutputPolicy:
    mode: StructuredOutputMode = StructuredOutputMode.DENSE_SCORES
    include_reason: bool = False
    include_evidence: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, StructuredOutputMode):
            raise DomainValidationError("structured output mode is invalid")
        if not isinstance(self.include_reason, bool) or not isinstance(self.include_evidence, bool):
            raise DomainValidationError("reason and evidence flags must be booleans")


@dataclass(frozen=True, slots=True)
class StructuredOutputSchema:
    document: dict[str, Any]
    canonical_json: str
    schema_hash: str


class StructuredOutputSchemaBuilder:
    SCHEMA_VERSION = 1

    def build(
        self,
        *,
        stage_template_version_id: str,
        label_set: LabelSetVersion,
        execution_mode: ExecutionMode,
        policy: StructuredOutputPolicy,
        expected_target_ids: tuple[str, ...],
    ) -> StructuredOutputSchema:
        validate_non_blank(stage_template_version_id, field="stage template version_id")
        if not isinstance(label_set, LabelSetVersion):
            raise DomainValidationError("label_set must be a LabelSetVersion")
        if not isinstance(execution_mode, ExecutionMode):
            raise DomainValidationError("execution_mode must be an ExecutionMode")
        if not isinstance(policy, StructuredOutputPolicy):
            raise DomainValidationError("policy must be a StructuredOutputPolicy")
        targets = _validate_targets(expected_target_ids)

        item_schema = self._result_item_schema(label_set=label_set, policy=policy)
        if execution_mode in _BATCH_MODES:
            target_field = _TARGET_FIELD_BY_MODE[execution_mode]
            item_schema["properties"] = {
                target_field: {"type": "string", "enum": list(targets)},
                **item_schema["properties"],
            }
            item_schema["required"] = [target_field, *item_schema["required"]]
            document: dict[str, Any] = {
                "\u0024schema": "https://json-schema.org/draft/2020-12/schema",
                "title": "TGCuratorAnalysisBatchV1",
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "results": {
                        "type": "array",
                        "items": item_schema,
                        "minItems": 0,
                        "maxItems": len(targets),
                    }
                },
                "required": ["results"],
                "x-tgcurator": {
                    "schema_version": self.SCHEMA_VERSION,
                    "stage_template_version_id": stage_template_version_id,
                    "label_set_version_id": label_set.version_id,
                    "execution_mode": execution_mode.value,
                    "structured_output_mode": policy.mode.value,
                },
            }
        else:
            if len(targets) != 1:
                raise DomainValidationError("non-batch execution requires exactly one target")
            document = {
                "\u0024schema": "https://json-schema.org/draft/2020-12/schema",
                "title": "TGCuratorAnalysisResultV1",
                "type": "object",
                "additionalProperties": False,
                "properties": {"result": item_schema},
                "required": ["result"],
                "x-tgcurator": {
                    "schema_version": self.SCHEMA_VERSION,
                    "stage_template_version_id": stage_template_version_id,
                    "label_set_version_id": label_set.version_id,
                    "execution_mode": execution_mode.value,
                    "structured_output_mode": policy.mode.value,
                },
            }

        serialized = canonical_json(document)
        return StructuredOutputSchema(
            document=document,
            canonical_json=serialized,
            schema_hash=canonical_hash(document),
        )

    def _result_item_schema(
        self, *, label_set: LabelSetVersion, policy: StructuredOutputPolicy
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        if policy.mode is StructuredOutputMode.DENSE_SCORES:
            properties["scores"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    key: {"type": "number", "minimum": 0.0, "maximum": 1.0}
                    for key in label_set.label_keys
                },
                "required": list(label_set.label_keys),
            }
            required.append("scores")
        else:
            properties["matches"] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label_key": {"type": "string", "enum": list(label_set.label_keys)},
                        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["label_key", "score"],
                },
            }
            required.append("matches")
        if policy.include_reason:
            properties["reason"] = {"type": "string"}
            required.append("reason")
        if policy.include_evidence:
            properties["evidence"] = {
                "type": "array",
                "items": {"type": "string"},
            }
            required.append("evidence")
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }


def target_field_for_mode(execution_mode: ExecutionMode) -> str | None:
    return _TARGET_FIELD_BY_MODE.get(execution_mode)


def is_batch_mode(execution_mode: ExecutionMode) -> bool:
    return execution_mode in _BATCH_MODES


def _validate_targets(target_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_ids, tuple) or not target_ids:
        raise DomainValidationError("expected target IDs must be a non-empty tuple")
    normalized = tuple(validate_non_blank(target_id, field="target ID") for target_id in target_ids)
    if len(set(normalized)) != len(normalized):
        raise DomainValidationError("expected target IDs must be unique")
    return normalized
