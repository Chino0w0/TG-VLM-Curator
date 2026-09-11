from __future__ import annotations

import unittest

from tgcurator.domain.routing import (
    PublicationAction,
    RoutingPolicy,
    RoutingRule,
    canonical_facts_json,
    canonical_facts_snapshot,
    evaluate_condition,
    evaluate_routing,
    facts_snapshot_hash,
    validate_condition,
)
from tgcurator.shared import DomainValidationError


class RoutingDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = {
            "message": {
                "blocked_from_analysis": False,
                "source_language": "zh-CN",
                "tags": ["product", "demo"],
                "review_status": "unreviewed",
            },
            "labels": {
                "model": {
                    "global": {
                        "real_ugc": {"score": 0.82, "activated": True},
                        "advertisement": {"score": 0.25, "activated": False},
                    },
                    "media": {},
                },
                "manual": {
                    "global": {
                        "real_ugc": {"score": 0.2, "activated": False},
                    },
                    "media": {},
                },
                "effective": {
                    "global": {
                        "real_ugc": {"score": 0.2, "activated": False},
                        "advertisement": {"score": 0.25, "activated": False},
                    },
                    "media": {
                        "image-1": {
                            "target_kind": "image_asset",
                            "labels": {
                                "quality": {"score": 0.91, "activated": True},
                                "unsafe": {"score": 0.1, "activated": False},
                            },
                        },
                        "video-1": {
                            "target_kind": "video_asset",
                            "labels": {
                                "quality": {"score": 0.4, "activated": False},
                                "unsafe": {"score": 0.2, "activated": False},
                            },
                        },
                    },
                },
            },
            "effective.global.real_ugc": 0.82,
        }

    def test_dsl_handles_nested_and_flat_facts(self) -> None:
        condition = {
            "all": [
                {
                    "label_score": {
                        "fact": "effective.global.real_ugc",
                        "op": "gte",
                        "value": 0.7,
                    }
                },
                {"fact": "message.blocked_from_analysis", "op": "eq", "value": False},
                {"fact": "message.tags", "op": "contains", "value": "demo"},
            ]
        }
        validate_condition(
            condition,
            allowed_fact_keys={
                "effective.global.real_ugc",
                "message.blocked_from_analysis",
                "message.tags",
            },
        )
        self.assertTrue(evaluate_condition(condition, self.facts))
        self.assertFalse(evaluate_condition({"fact": "unknown.hidden", "op": "exists"}, self.facts))

    def test_shaped_labels_require_explicit_namespace_and_use_requested_namespace(self) -> None:
        model = {
            "label": {
                "namespace": "model",
                "scope": "global",
                "key": "real_ugc",
                "score_gte": 0.8,
            }
        }
        effective = {
            "label": {
                "namespace": "effective",
                "scope": "global",
                "key": "real_ugc",
                "score_gte": 0.8,
            }
        }
        self.assertTrue(evaluate_condition(model, self.facts))
        self.assertFalse(evaluate_condition(effective, self.facts))
        with self.assertRaises(DomainValidationError):
            validate_condition({"label": {"scope": "global", "key": "real_ugc"}})
        with self.assertRaises(DomainValidationError):
            validate_condition({"label": {"namespace": "effective", "key": "real_ugc"}})

    def test_any_media_and_none_media_are_deterministic(self) -> None:
        high_quality = {"label": {"namespace": "effective", "key": "quality", "score_gte": 0.9}}
        unsafe = {"label": {"namespace": "effective", "key": "unsafe"}}
        self.assertTrue(evaluate_condition({"any_media": high_quality}, self.facts))
        self.assertTrue(evaluate_condition({"none_media": unsafe}, self.facts))
        self.assertFalse(evaluate_condition({"none_media": high_quality}, self.facts))

    def test_unknown_or_incompatible_facts_cannot_be_inverted_into_matches(self) -> None:
        for condition in (
            {"not": {"fact": "unknown.hidden", "op": "exists"}},
            {"not": {"fact": "message.tags", "op": "gt", "value": 1}},
            {
                "not": {
                    "label": {
                        "namespace": "effective",
                        "scope": "global",
                        "key": "missing",
                    }
                }
            },
            {
                "not": {
                    "any_media": {
                        "label": {
                            "namespace": "effective",
                            "key": "missing",
                        }
                    }
                }
            },
        ):
            with self.subTest(condition=condition):
                self.assertFalse(evaluate_condition(condition, self.facts))

    def test_invalid_ast_is_rejected_and_never_matches(self) -> None:
        invalid = {"python": "__import__('os').system('bad')"}
        with self.assertRaises(DomainValidationError):
            validate_condition(invalid)
        self.assertFalse(evaluate_condition(invalid, self.facts))
        self.assertFalse(
            evaluate_condition(
                {"fact": "message.source_language", "op": "eq", "value": object()},
                self.facts,
            )
        )

    def test_disabled_priority_stop_and_action_identity_are_deterministic(self) -> None:
        disabled_action = PublicationAction(
            "disabled",
            "destination-x",
            "metadata_only",
            rendering_template_version_id="template-v1",
        )
        first_action = PublicationAction("first", "destination-a", "forward_only")
        second_action = PublicationAction(
            "second",
            "destination-b",
            "copy_with_caption",
            rendering_template_version_id="template-v1",
        )
        later_action = PublicationAction(
            "later",
            "destination-low",
            "metadata_only",
            rendering_template_version_id="template-v1",
        )
        policy = RoutingPolicy(
            "routing-v1",
            (
                RoutingRule(
                    "later",
                    1,
                    {"fact": "message.source_language", "op": "exists"},
                    (later_action,),
                ),
                RoutingRule(
                    "disabled-first",
                    20,
                    {"fact": "message.source_language", "op": "exists"},
                    (disabled_action,),
                    stop_on_match=True,
                    enabled=False,
                ),
                RoutingRule(
                    "first",
                    10,
                    {"fact": "message.tags", "op": "exists"},
                    (first_action, second_action),
                    stop_on_match=True,
                ),
                RoutingRule(
                    "same-priority-but-later",
                    10,
                    {"fact": "message.tags", "op": "exists"},
                    (later_action,),
                ),
            ),
        )

        decision = evaluate_routing(policy, self.facts)

        self.assertEqual(
            [outcome.rule_id for outcome in decision.outcomes],
            ["disabled-first", "first"],
        )
        self.assertFalse(decision.outcomes[0].enabled)
        self.assertFalse(decision.outcomes[0].evaluated)
        self.assertTrue(decision.outcomes[0].checked)
        self.assertEqual(decision.stopped_at_rule_id, "first")
        self.assertEqual([action.action_id for action in decision.actions], ["first", "second"])
        self.assertEqual(
            [action.routing_rule_id for action in decision.actions],
            ["first", "first"],
        )

    def test_review_status_is_not_an_implicit_routing_gate(self) -> None:
        policy = RoutingPolicy(
            "routing-v1",
            (
                RoutingRule(
                    "always",
                    1,
                    {"fact": "message.source_language", "op": "exists"},
                    (
                        PublicationAction(
                            "a",
                            "destination",
                            "metadata_only",
                            rendering_template_version_id="template-v1",
                        ),
                    ),
                ),
            ),
        )
        decision = evaluate_routing(policy, self.facts)
        self.assertEqual([action.action_id for action in decision.actions], ["a"])

    def test_canonical_facts_hash_is_stable_and_snapshot_is_detached(self) -> None:
        facts = {"z": [2, 1], "a": {"unicode": "鍐呭��", "value": 1}}
        reordered = {"a": {"value": 1, "unicode": "鍐呭��"}, "z": [2, 1]}
        snapshot = canonical_facts_snapshot(facts)
        self.assertEqual(canonical_facts_json(facts), canonical_facts_json(reordered))
        self.assertEqual(facts_snapshot_hash(facts), facts_snapshot_hash(reordered))
        facts["a"]["value"] = 2
        self.assertEqual(snapshot["a"]["value"], 1)
        with self.assertRaises(DomainValidationError):
            canonical_facts_snapshot({"bad": float("nan")})

    def test_routing_actions_reject_unsupported_publication_modes(self) -> None:
        with self.assertRaises(DomainValidationError):
            PublicationAction("bad", "destination", "unsupported")

    def test_routing_actions_require_templates_only_for_generated_text_modes(self) -> None:
        for mode in (
            "native_forward_with_supplement",
            "copy_with_caption",
            "metadata_only",
        ):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(DomainValidationError, "required"):
                    PublicationAction("action", "destination", mode)

        with self.assertRaisesRegex(DomainValidationError, "must be omitted"):
            PublicationAction(
                "action",
                "destination",
                "forward_only",
                rendering_template_version_id="template-v1",
            )


if __name__ == "__main__":
    unittest.main()
