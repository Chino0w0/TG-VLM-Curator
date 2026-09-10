from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from tgcurator.domain.publishing import PublicationMode
from tgcurator.domain.routing.dsl import evaluate_condition, validate_condition
from tgcurator.shared import DomainValidationError


@dataclass(frozen=True, slots=True)
class PublicationAction:
    action_id: str
    destination_channel_id: str
    publication_mode: str
    rendering_template_version_id: str | None = None
    publish_identity_id: str | None = None
    routing_rule_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise DomainValidationError("action_id must not be blank")
        if (
            not isinstance(self.destination_channel_id, str)
            or not self.destination_channel_id.strip()
        ):
            raise DomainValidationError("destination_channel_id must not be blank")
        try:
            mode = PublicationMode(self.publication_mode)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError("publication_mode is not supported") from exc
        for field, value in (
            ("rendering_template_version_id", self.rendering_template_version_id),
            ("publish_identity_id", self.publish_identity_id),
            ("routing_rule_id", self.routing_rule_id),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise DomainValidationError(f"{field} must not be blank")
        if mode is PublicationMode.FORWARD_ONLY:
            if self.rendering_template_version_id is not None:
                raise DomainValidationError(
                    "rendering_template_version_id must be omitted for forward_only"
                )
        elif self.rendering_template_version_id is None:
            raise DomainValidationError(
                "rendering_template_version_id is required for generated-text publication modes"
            )


@dataclass(frozen=True, slots=True)
class RoutingRule:
    rule_id: str
    priority: int
    condition: Mapping[str, Any]
    actions: tuple[PublicationAction, ...]
    stop_on_match: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise DomainValidationError("rule_id must not be blank")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise DomainValidationError("priority must be an integer")
        if not isinstance(self.actions, tuple) or any(
            not isinstance(action, PublicationAction) for action in self.actions
        ):
            raise DomainValidationError("actions must be a tuple of PublicationAction values")
        if not isinstance(self.stop_on_match, bool):
            raise DomainValidationError("stop_on_match must be a boolean")
        if not isinstance(self.enabled, bool):
            raise DomainValidationError("enabled must be a boolean")
        validate_condition(self.condition)
        if len({action.action_id for action in self.actions}) != len(self.actions):
            raise DomainValidationError("a rule cannot contain duplicate action_id values")
        if any(
            action.routing_rule_id is not None and action.routing_rule_id != self.rule_id
            for action in self.actions
        ):
            raise DomainValidationError("action routing_rule_id must match its owning rule")


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    policy_version_id: str
    rules: tuple[RoutingRule, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version_id, str) or not self.policy_version_id.strip():
            raise DomainValidationError("policy_version_id must not be blank")
        if not isinstance(self.rules, tuple) or any(
            not isinstance(rule, RoutingRule) for rule in self.rules
        ):
            raise DomainValidationError("rules must be a tuple of RoutingRule values")
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise DomainValidationError("a routing policy cannot contain duplicate rule_id values")


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    rule_id: str
    enabled: bool
    checked: bool
    evaluated: bool
    matched: bool
    stopped_after_match: bool


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    policy_version_id: str
    outcomes: tuple[RuleOutcome, ...]
    actions: tuple[PublicationAction, ...]
    stopped_at_rule_id: str | None = None


def evaluate_routing(policy: RoutingPolicy, facts: Mapping[str, Any]) -> RoutingDecision:
    """Evaluate in stable order and produce actions without external side effects."""
    if not isinstance(policy, RoutingPolicy):
        raise DomainValidationError("policy must be a RoutingPolicy")
    if not isinstance(facts, Mapping):
        raise DomainValidationError("facts must be a mapping")
    outcomes: list[RuleOutcome] = []
    actions: list[PublicationAction] = []
    stopped_at_rule_id: str | None = None
    for rule in sorted(policy.rules, key=lambda item: (-item.priority, item.rule_id)):
        if not rule.enabled:
            outcomes.append(
                RuleOutcome(
                    rule_id=rule.rule_id,
                    enabled=False,
                    checked=True,
                    evaluated=False,
                    matched=False,
                    stopped_after_match=False,
                )
            )
            continue
        matched = evaluate_condition(rule.condition, facts)
        should_stop = matched and rule.stop_on_match
        outcomes.append(
            RuleOutcome(
                rule_id=rule.rule_id,
                enabled=True,
                checked=True,
                evaluated=True,
                matched=matched,
                stopped_after_match=should_stop,
            )
        )
        if matched:
            actions.extend(replace(action, routing_rule_id=rule.rule_id) for action in rule.actions)
        if should_stop:
            stopped_at_rule_id = rule.rule_id
            break
    return RoutingDecision(
        policy_version_id=policy.policy_version_id,
        outcomes=tuple(outcomes),
        actions=tuple(actions),
        stopped_at_rule_id=stopped_at_rule_id,
    )
