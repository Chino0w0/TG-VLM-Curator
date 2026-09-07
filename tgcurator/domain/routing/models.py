from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.domain.routing.dsl import evaluate_condition, validate_condition
from tgcurator.shared import DomainValidationError


@dataclass(frozen=True, slots=True)
class PublicationAction:
    action_id: str
    destination_channel_id: str
    publication_mode: str
    rendering_template_version_id: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.destination_channel_id.strip():
            raise DomainValidationError("routing action identifiers must not be blank")
        if not self.publication_mode.strip():
            raise DomainValidationError("publication_mode must not be blank")


@dataclass(frozen=True, slots=True)
class RoutingRule:
    rule_id: str
    priority: int
    condition: Mapping[str, Any]
    actions: tuple[PublicationAction, ...]
    stop_on_match: bool = False

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise DomainValidationError("rule_id must not be blank")
        validate_condition(self.condition)
        if len({action.action_id for action in self.actions}) != len(self.actions):
            raise DomainValidationError("a rule cannot contain duplicate action_id values")


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    policy_version_id: str
    rules: tuple[RoutingRule, ...]

    def __post_init__(self) -> None:
        if not self.policy_version_id.strip():
            raise DomainValidationError("policy_version_id must not be blank")
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise DomainValidationError("a routing policy cannot contain duplicate rule_id values")


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    rule_id: str
    matched: bool
    stopped_after_match: bool


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    policy_version_id: str
    outcomes: tuple[RuleOutcome, ...]
    actions: tuple[PublicationAction, ...]


def evaluate_routing(policy: RoutingPolicy, facts: Mapping[str, Any]) -> RoutingDecision:
    """Evaluate in a stable order and produce actions without external side effects."""
    outcomes: list[RuleOutcome] = []
    actions: list[PublicationAction] = []
    for rule in sorted(policy.rules, key=lambda item: (-item.priority, item.rule_id)):
        matched = evaluate_condition(rule.condition, facts)
        should_stop = matched and rule.stop_on_match
        outcomes.append(RuleOutcome(rule.rule_id, matched, should_stop))
        if matched:
            actions.extend(rule.actions)
        if should_stop:
            break
    return RoutingDecision(policy.policy_version_id, tuple(outcomes), tuple(actions))
