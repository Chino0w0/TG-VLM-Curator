from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite
from numbers import Real

from tgcurator.shared import DomainValidationError


@dataclass(frozen=True, slots=True)
class LabelFact:
    """A frozen label score in a named model/manual/effective namespace."""

    key: str
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise DomainValidationError("label key must not be blank")
        _validate_score(self.score, field="label score")


@dataclass(frozen=True, slots=True)
class NegativeLabelRule:
    label_key: str
    minimum_score: float

    def __post_init__(self) -> None:
        if not isinstance(self.label_key, str) or not self.label_key.strip():
            raise DomainValidationError("negative label key must not be blank")
        _validate_score(self.minimum_score, field="negative label threshold")


@dataclass(frozen=True, slots=True)
class NegativeGateDecision:
    blocked: bool
    matched_rules: tuple[NegativeLabelRule, ...]

    @property
    def blocked_by_label_keys(self) -> tuple[str, ...]:
        return tuple(rule.label_key for rule in self.matched_rules)


@dataclass(frozen=True, slots=True)
class NegativeGatePolicy:
    policy_version_id: str
    rules: tuple[NegativeLabelRule, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version_id, str) or not self.policy_version_id.strip():
            raise DomainValidationError("policy_version_id must not be blank")
        if not isinstance(self.rules, tuple) or any(
            not isinstance(rule, NegativeLabelRule) for rule in self.rules
        ):
            raise DomainValidationError("rules must be a tuple of NegativeLabelRule values")
        if len({rule.label_key for rule in self.rules}) != len(self.rules):
            raise DomainValidationError("a negative gate policy cannot repeat a label key")

    def evaluate(self, facts: Iterable[LabelFact]) -> NegativeGateDecision:
        """Evaluate without changing the source labels or collapsing their provenance."""
        highest_scores: dict[str, float] = {}
        for fact in facts:
            if not isinstance(fact, LabelFact):
                raise DomainValidationError("negative gate facts must be LabelFact values")
            highest_scores[fact.key] = max(highest_scores.get(fact.key, 0.0), fact.score)
        matched = tuple(
            rule
            for rule in self.rules
            if highest_scores.get(rule.label_key, 0.0) >= rule.minimum_score
        )
        return NegativeGateDecision(blocked=bool(matched), matched_rules=matched)


def _validate_score(value: float, *, field: str) -> None:
    if (
        not isinstance(value, Real)
        or isinstance(value, bool)
        or not isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise DomainValidationError(f"{field} must be a finite number in [0, 1]")
