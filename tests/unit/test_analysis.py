import unittest

from tgcurator.domain.analysis import (
    LabelFact,
    NegativeGatePolicy,
    NegativeLabelRule,
    PipelineDefinition,
    PipelineStage,
)
from tgcurator.shared import DomainValidationError


class AnalysisDomainTests(unittest.TestCase):
    def test_pipeline_returns_stable_topological_order(self) -> None:
        pipeline = PipelineDefinition(
            "pipeline-v1",
            (
                PipelineStage("publish_facts", ("media", "text")),
                PipelineStage("text"),
                PipelineStage("media", ("text",)),
            ),
        )
        self.assertEqual(pipeline.topological_stage_ids(), ("text", "media", "publish_facts"))

    def test_pipeline_rejects_missing_and_cyclic_dependencies(self) -> None:
        with self.assertRaises(DomainValidationError):
            PipelineDefinition("pipeline-v1", (PipelineStage("a", ("missing",)),))
        with self.assertRaises(DomainValidationError):
            PipelineDefinition(
                "pipeline-v1",
                (PipelineStage("a", ("b",)), PipelineStage("b", ("a",))),
            )

    def test_negative_gate_preserves_facts_and_records_matching_rules(self) -> None:
        policy = NegativeGatePolicy(
            "negative-policy-v1",
            (
                NegativeLabelRule("effective.global.advertisement", 0.8),
                NegativeLabelRule("manual.global.blocked", 1.0),
            ),
        )
        decision = policy.evaluate(
            (
                LabelFact("effective.global.advertisement", 0.9),
                LabelFact("manual.global.blocked", 0.0),
            )
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.blocked_by_label_keys, ("effective.global.advertisement",))


if __name__ == "__main__":
    unittest.main()
