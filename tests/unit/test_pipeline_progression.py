from __future__ import annotations

import unittest

from tgcurator.domain.analysis import (
    AnalysisRunStatus,
    CachePolicy,
    ExistingNodeTargetRun,
    LabelScope,
    MediaAssetTarget,
    MessageMediaReadiness,
    PipelineNodeActionKind,
    PipelineNodeSpec,
    pipeline_has_prescreen_root,
    plan_pipeline_progression,
)
from tgcurator.shared import DomainValidationError


def make_node(
    key: str,
    *,
    scope: LabelScope = LabelScope.GLOBAL,
    requires_media: bool = False,
    depends_on: tuple[str, ...] = (),
    run_if: dict | None = None,
) -> PipelineNodeSpec:
    return PipelineNodeSpec(
        node_id=f"node-{key}",
        node_key=key,
        stage_version_id=f"stage-{key}",
        target_scope=scope,
        input_requires_media=requires_media,
        is_text_only_prescreen=(scope is LabelScope.GLOBAL and not requires_media),
        cache_policy=CachePolicy.MESSAGE if scope is LabelScope.GLOBAL else CachePolicy.ASSET,
        max_attempts=3,
        depends_on=depends_on,
        run_if=run_if,
    )


class PipelineProgressionTests(unittest.TestCase):
    def test_prescreen_root_detection_and_topological_ordering_with_media_gate(self) -> None:
        prescreen = make_node("prescreen", scope=LabelScope.GLOBAL, requires_media=False)
        vision = make_node(
            "vision",
            scope=LabelScope.MEDIA,
            requires_media=True,
            depends_on=("prescreen",),
            run_if={"fact": "global.keep", "op": "gte", "value": 0.5},
        )
        summary = make_node(
            "summary",
            scope=LabelScope.GLOBAL,
            requires_media=False,
            depends_on=("vision",),
        )
        # Deliberately pass nodes in reverse topological order.
        nodes = (summary, vision, prescreen)
        self.assertTrue(pipeline_has_prescreen_root(nodes))

        pending_media = MessageMediaReadiness(
            (MediaAssetTarget("image_asset", "img-1", "pending"),)
        )
        first = plan_pipeline_progression(
            message_id="msg-1",
            nodes=nodes,
            existing_runs_by_node={},
            media=pending_media,
            facts={},
            blocked_from_analysis=False,
        )
        self.assertEqual(len(first.planned_targets), 1)
        self.assertEqual(first.planned_targets[0].node_key, "prescreen")
        self.assertEqual(first.planned_targets[0].action, PipelineNodeActionKind.SCHEDULE)
        self.assertEqual(first.run_status, AnalysisRunStatus.RUNNING)

        # After prescreen succeeds with keep=0.9, vision waits on pending media archive.
        waiting_media = plan_pipeline_progression(
            message_id="msg-1",
            nodes=nodes,
            existing_runs_by_node={
                "prescreen": (ExistingNodeTargetRun("message", "msg-1", "succeeded"),)
            },
            media=pending_media,
            facts={"global": {"keep": 0.9}},
            blocked_from_analysis=False,
        )
        self.assertEqual(waiting_media.planned_targets, ())
        self.assertTrue(waiting_media.waiting_for_media)
        self.assertEqual(waiting_media.run_status, AnalysisRunStatus.RUNNING)

        # Once media is ready, vision schedules per ready asset while summary waits upstream.
        ready_media = MessageMediaReadiness(
            (
                MediaAssetTarget("image_asset", "img-1", "ready"),
                MediaAssetTarget("video_asset", "vid-1", "ready"),
            )
        )
        vision_step = plan_pipeline_progression(
            message_id="msg-1",
            nodes=nodes,
            existing_runs_by_node={
                "prescreen": (ExistingNodeTargetRun("message", "msg-1", "succeeded"),)
            },
            media=ready_media,
            facts={"global": {"keep": 0.9}},
            blocked_from_analysis=False,
        )
        self.assertEqual(
            [
                (item.node_key, item.target_kind, item.target_id)
                for item in vision_step.planned_targets
            ],
            [
                ("vision", "image_asset", "img-1"),
                ("vision", "video_asset", "vid-1"),
            ],
        )

    def test_false_run_if_records_skip_condition_and_zero_media_vacuously_satisfies_media_stage(
        self,
    ) -> None:
        prescreen = make_node("prescreen")
        vision = make_node(
            "vision",
            scope=LabelScope.MEDIA,
            requires_media=True,
            depends_on=("prescreen",),
            run_if={"fact": "global.keep", "op": "gte", "value": 0.8},
        )
        followup = make_node("followup", depends_on=("vision",))

        # False run_if emits SKIP_CONDITION for assets and immediately schedules followup!
        media = MessageMediaReadiness((MediaAssetTarget("image_asset", "img-1", "ready"),))
        skipped_plan = plan_pipeline_progression(
            message_id="msg-1",
            nodes=(prescreen, vision, followup),
            existing_runs_by_node={
                "prescreen": (ExistingNodeTargetRun("message", "msg-1", "succeeded"),)
            },
            media=media,
            facts={"global": {"keep": 0.2}},
            blocked_from_analysis=False,
        )
        self.assertEqual(
            [(t.node_key, t.action) for t in skipped_plan.planned_targets],
            [
                ("vision", PipelineNodeActionKind.SKIP_CONDITION),
                ("followup", PipelineNodeActionKind.SCHEDULE),
            ],
        )

        # Text-only message (0 media assets) with run_if=True vacuously satisfies MEDIA stage
        # without creating an invalid StageRun target!
        text_only_plan = plan_pipeline_progression(
            message_id="msg-text",
            nodes=(prescreen, vision, followup),
            existing_runs_by_node={
                "prescreen": (ExistingNodeTargetRun("message", "msg-text", "succeeded"),)
            },
            media=MessageMediaReadiness(()),
            facts={"global": {"keep": 0.95}},
            blocked_from_analysis=False,
        )
        self.assertEqual(
            [(t.node_key, t.target_kind, t.action) for t in text_only_plan.planned_targets],
            [("followup", "message", PipelineNodeActionKind.SCHEDULE)],
        )

    def test_negative_gate_and_cycle_validation(self) -> None:
        n1 = make_node("n1")
        blocked = plan_pipeline_progression(
            message_id="msg-blocked",
            nodes=(n1,),
            existing_runs_by_node={},
            media=MessageMediaReadiness(()),
            facts={},
            blocked_from_analysis=True,
        )
        self.assertEqual(blocked.planned_targets, ())
        self.assertEqual(blocked.run_status, AnalysisRunStatus.BLOCKED_NEGATIVE_GATE)

        c1 = make_node("c1", depends_on=("c2",))
        c2 = make_node("c2", depends_on=("c1",))
        with self.assertRaises(DomainValidationError):
            plan_pipeline_progression(
                message_id="msg-1",
                nodes=(c1, c2),
                existing_runs_by_node={},
                media=MessageMediaReadiness(()),
                facts={},
                blocked_from_analysis=False,
            )


if __name__ == "__main__":
    unittest.main()
