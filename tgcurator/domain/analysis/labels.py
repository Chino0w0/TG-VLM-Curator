from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import (
    LabelScope,
    VersionStatus,
    validate_identifier,
    validate_non_blank,
    validate_positive_int,
    validate_score,
)


@dataclass(frozen=True, slots=True)
class LabelDefinitionVersion:
    version_id: str
    definition_id: str
    version_number: int
    key: str
    display_name: str
    description: str
    scope: LabelScope
    negative: bool = False
    enabled: bool = True
    status: VersionStatus = VersionStatus.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_id", validate_non_blank(self.version_id, field="label version_id")
        )
        object.__setattr__(
            self,
            "definition_id",
            validate_non_blank(self.definition_id, field="label definition_id"),
        )
        validate_positive_int(self.version_number, field="label version_number")
        object.__setattr__(self, "key", validate_identifier(self.key, field="label key"))
        object.__setattr__(
            self, "display_name", validate_non_blank(self.display_name, field="label display_name")
        )
        if not isinstance(self.description, str):
            raise DomainValidationError("label description must be a string")
        if not isinstance(self.scope, LabelScope):
            raise DomainValidationError("label scope must be a LabelScope")
        if not isinstance(self.negative, bool) or not isinstance(self.enabled, bool):
            raise DomainValidationError("label negative and enabled flags must be booleans")
        if not isinstance(self.status, VersionStatus):
            raise DomainValidationError("label status must be a VersionStatus")


@dataclass(frozen=True, slots=True)
class LabelBinding:
    label: LabelDefinitionVersion
    activation_threshold: float
    output_order: int
    prompt_hint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.label, LabelDefinitionVersion):
            raise DomainValidationError("label binding label must be a LabelDefinitionVersion")
        object.__setattr__(
            self,
            "activation_threshold",
            validate_score(self.activation_threshold, field="activation threshold"),
        )
        if not isinstance(self.output_order, int) or isinstance(self.output_order, bool):
            raise DomainValidationError("label output_order must be an integer")
        if self.output_order < 0:
            raise DomainValidationError("label output_order must be non-negative")
        if not isinstance(self.prompt_hint, str):
            raise DomainValidationError("label prompt_hint must be a string")


@dataclass(frozen=True, slots=True)
class ResolvedLabel:
    label_definition_version_id: str
    key: str
    scope: LabelScope
    score: float
    activated: bool
    negative: bool


@dataclass(frozen=True, slots=True)
class LabelSetVersion:
    version_id: str
    label_set_id: str
    version_number: int
    bindings: tuple[LabelBinding, ...]
    status: VersionStatus = VersionStatus.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_id", validate_non_blank(self.version_id, field="label set version_id")
        )
        object.__setattr__(
            self, "label_set_id", validate_non_blank(self.label_set_id, field="label set id")
        )
        validate_positive_int(self.version_number, field="label set version number")
        if not isinstance(self.bindings, tuple) or any(
            not isinstance(binding, LabelBinding) for binding in self.bindings
        ):
            raise DomainValidationError("label set bindings must be a tuple of LabelBinding values")
        if not self.bindings:
            raise DomainValidationError("label set must contain at least one binding")
        if not isinstance(self.status, VersionStatus):
            raise DomainValidationError("label set status must be a VersionStatus")

        version_ids = [binding.label.version_id for binding in self.bindings]
        keys = [binding.label.key for binding in self.bindings]
        orders = [binding.output_order for binding in self.bindings]
        if len(set(version_ids)) != len(version_ids):
            raise DomainValidationError("label set cannot repeat a label definition version")
        if len(set(keys)) != len(keys):
            raise DomainValidationError("label set cannot repeat a label key")
        if len(set(orders)) != len(orders):
            raise DomainValidationError("label set cannot repeat output_order")

    @property
    def ordered_bindings(self) -> tuple[LabelBinding, ...]:
        return tuple(sorted(self.bindings, key=lambda item: (item.output_order, item.label.key)))

    @property
    def label_keys(self) -> tuple[str, ...]:
        return tuple(binding.label.key for binding in self.ordered_bindings)

    @property
    def scopes(self) -> frozenset[LabelScope]:
        return frozenset(binding.label.scope for binding in self.bindings)

    def validate_for_publication(self, *, target_scope: LabelScope | None = None) -> None:
        if any(not binding.label.enabled for binding in self.bindings):
            raise DomainValidationError("a published label set cannot bind disabled labels")
        if any(binding.label.status is not VersionStatus.PUBLISHED for binding in self.bindings):
            raise DomainValidationError("a published label set must reference published labels")
        if target_scope is not None and self.scopes != frozenset({target_scope}):
            raise DomainValidationError(
                "label set scope is incompatible with the stage target scope"
            )

    def prompt_definitions(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "key": binding.label.key,
                "display_name": binding.label.display_name,
                "description": binding.label.description,
                "scope": binding.label.scope.value,
                "negative": binding.label.negative,
                "prompt_hint": binding.prompt_hint,
            }
            for binding in self.ordered_bindings
        )

    def resolve_scores(self, scores: Mapping[str, float]) -> tuple[ResolvedLabel, ...]:
        if not isinstance(scores, Mapping):
            raise DomainValidationError("label scores must be an object")
        unknown = set(scores) - set(self.label_keys)
        if unknown:
            raise DomainValidationError(f"label scores contain unknown keys: {sorted(unknown)!r}")
        binding_by_key = {binding.label.key: binding for binding in self.bindings}
        resolved: list[ResolvedLabel] = []
        for key in self.label_keys:
            if key not in scores:
                continue
            binding = binding_by_key[key]
            score = validate_score(scores[key], field=f"score for {key}")
            resolved.append(
                ResolvedLabel(
                    label_definition_version_id=binding.label.version_id,
                    key=key,
                    scope=binding.label.scope,
                    score=score,
                    activated=score >= binding.activation_threshold,
                    negative=binding.label.negative,
                )
            )
        return tuple(resolved)
