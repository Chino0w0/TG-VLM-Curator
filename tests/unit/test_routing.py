import unittest

from tgcurator.domain.routing import (
    PublicationAction,
    RoutingPolicy,
    RoutingRule,
    evaluate_condition,
    evaluate_routing,
    validate_condition,
)
from tgcurator.shared import DomainValidationError


class RoutingDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = {
            "effective.global.real_ugc": 0.82,
            "message.blocked_from_analysis": False,
            "message.source_language": "zh-CN",
            "message.tags": ["product", "demo"],
        }

    def test_dsl_handles_nested_safe_conditions(self) -> None:
        condition = {
            "all": [
                {"label_score": {"fact": "effective.global.real_ugc", "op": "gte", "value": 0.7}},
                {"fact": "message.blocked_from_analysis", "op": "eq", "value": False},
                {"fact": "message.tags", "op": "contains", "value": "demo"},
            ]
        }
        validate_condition(condition, allowed_fact_keys=set(self.facts))
        self.assertTrue(evaluate_condition(condition, self.facts))
        self.assertFalse(evaluate_condition({"fact": "unknown.hidden", "op": "exists"}, self.facts))

    def test_invalid_ast_is_rejected_and_never_matches(self) -> None:
        invalid = {"python": "__import__('os').system('bad')"}
        with self.assertRaises(DomainValidationError):
            validate_condition(invalid)
        self.assertFalse(evaluate_condition(invalid, self.facts))

    def test_priority_stop_and_multiple_destinations_are_deterministic(self) -> None:
        low_action = PublicationAction("low", "destination-low", "metadata_only")
        first_action = PublicationAction("first", "destination-a", "forward_only")
        second_action = PublicationAction("second", "destination-b", "copy_with_caption")
        policy = RoutingPolicy(
            "routing-v1",
            (
                RoutingRule(
                    "low", 1, {"fact": "message.source_language", "op": "exists"}, (low_action,)
                ),
                RoutingRule(
                    "first",
                    10,
                    {"label_present": {"fact": "effective.global.real_ugc", "minimum_score": 0.8}},
                    (first_action, second_action),
                    stop_on_match=True,
                ),
                RoutingRule(
                    "same-priority-but-later",
                    10,
                    {"fact": "message.tags", "op": "exists"},
                    (low_action,),
                ),
            ),
        )
        decision = evaluate_routing(policy, self.facts)
        self.assertEqual([outcome.rule_id for outcome in decision.outcomes], ["first"])
        self.assertEqual([action.action_id for action in decision.actions], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
