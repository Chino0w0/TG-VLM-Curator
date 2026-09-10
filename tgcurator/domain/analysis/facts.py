from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import LabelScope, canonical_json
from .result_validator import ValidatedTargetResult


@dataclass(frozen=True, slots=True)
class NegativeGateMatch:
    target_id: str
    label_definition_version_id: str
    label_key: str
    scope: LabelScope
    score: float


@dataclass(frozen=True, slots=True)
class StageNegativeGateDecision:
    blocked: bool
    matches: tuple[NegativeGateMatch, ...]


@dataclass(frozen=True, slots=True)
class AnalysisFacts:
    """Stable facts exposed to downstream run_if evaluation."""

    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.values, Mapping):
            raise DomainValidationError("analysis facts must be an object")
        normalized = _normalize_mapping(self.values)
        object.__setattr__(self, "values", MappingProxyType(normalized))

    def merged(self, additions: Mapping[str, Any]) -> AnalysisFacts:
        merged = dict(self.values)
        merged.update(_normalize_mapping(additions))
        return AnalysisFacts(merged)


def facts_from_results(
    *,
    scope: LabelScope,
    results: Iterable[ValidatedTargetResult],
    blocked_from_analysis: bool = False,
) -> AnalysisFacts:
    if not isinstance(scope, LabelScope):
        raise DomainValidationError("fact scope must be a LabelScope")
    highest_scores: dict[str, float] = {}
    for result in results:
        if not isinstance(result, ValidatedTargetResult):
            raise DomainValidationError("fact results must be ValidatedTargetResult values")
        for label in result.labels:
            highest_scores[label.key] = max(highest_scores.get(label.key, 0.0), label.score)
    prefix = "global" if scope is LabelScope.GLOBAL else "media"
    values: dict[str, Any] = {
        f"{prefix}.{key}": score for key, score in sorted(highest_scores.items())
    }
    values["message.blocked_from_analysis"] = blocked_from_analysis
    return AnalysisFacts(values)


def evaluate_stage_negative_gate(
    results: Iterable[ValidatedTargetResult],
) -> StageNegativeGateDecision:
    matches = tuple(
        NegativeGateMatch(
            target_id=result.target_id,
            label_definition_version_id=label.label_definition_version_id,
            label_key=label.key,
            scope=label.scope,
            score=label.score,
        )
        for result in results
        for label in result.activated_negative_labels
    )
    return StageNegativeGateDecision(blocked=bool(matches), matches=matches)


def _normalize_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    normalized = canonical_json(values)
    result = json.loads(normalized)
    if not isinstance(result, dict):
        raise DomainValidationError("analysis facts must be a JSON object")
    return result
