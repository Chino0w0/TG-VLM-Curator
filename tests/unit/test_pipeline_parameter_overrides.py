from __future__ import annotations

import unittest
from dataclasses import replace

from test_analysis_orchestrator import _stage

from tgcurator.domain.analysis import (
    ExecutionMode,
    LabelScope,
    VisualCompositionPolicy,
    apply_stage_parameter_overrides,
)
from tgcurator.shared import DomainValidationError


class PipelineParameterOverrideTests(unittest.TestCase):
    def setUp(self):
        self.stage = _stage(scope=LabelScope.GLOBAL)

    def test_scalar_overrides_create_validated_effective_stage_without_mutating_version(self):
        original = self.stage.timeout_seconds
        effective = apply_stage_parameter_overrides(
            replace(self.stage, execution_mode=ExecutionMode.BATCH_MESSAGES),
            {"timeout_seconds": 12.5, "max_batch_size": 4, "max_concurrency": 2},
        )
        self.assertEqual(effective.timeout_seconds, 12.5)
        self.assertEqual(effective.max_batch_size, 4)
        self.assertEqual(effective.max_concurrency, 2)
        self.assertEqual(self.stage.timeout_seconds, original)
        self.assertEqual(effective.version_id, self.stage.version_id)

    def test_retry_overrides_merge_with_frozen_stage_policy(self):
        effective = apply_stage_parameter_overrides(
            self.stage, {"retry_policy": {"max_attempts": 7}}
        )
        self.assertEqual(effective.retry_policy.max_attempts, 7)
        self.assertEqual(
            effective.retry_policy.base_delay_seconds, self.stage.retry_policy.base_delay_seconds
        )
        self.assertEqual(
            effective.retry_policy.max_delay_seconds, self.stage.retry_policy.max_delay_seconds
        )

    def test_visual_strategy_is_an_enum_not_an_ignored_json_field(self):
        effective = apply_stage_parameter_overrides(
            self.stage, {"visual_composition_policy": "adaptive"}
        )
        self.assertIs(effective.visual_composition_policy, VisualCompositionPolicy.ADAPTIVE)

    def test_identity_scope_input_and_cache_cannot_be_overridden(self):
        for key in ("version_id", "target_scope", "input_policy", "cache_policy"):
            with self.subTest(key=key), self.assertRaises(DomainValidationError):
                apply_stage_parameter_overrides(self.stage, {key: "anything"})

    def test_unknown_retry_fields_fail_closed(self):
        with self.assertRaises(DomainValidationError):
            apply_stage_parameter_overrides(self.stage, {"retry_policy": {"arbitrary": 1}})

    def test_invalid_visual_strategy_fails_closed(self):
        with self.assertRaises(DomainValidationError):
            apply_stage_parameter_overrides(self.stage, {"visual_composition_policy": "unknown"})

    def test_invalid_scalar_override_is_rejected_by_domain_constructor(self):
        for key in ("timeout_seconds", "max_batch_size", "max_concurrency"):
            with self.subTest(key=key), self.assertRaises(DomainValidationError):
                apply_stage_parameter_overrides(self.stage, {key: -1})

    def test_empty_override_preserves_frozen_stage(self):
        self.assertEqual(apply_stage_parameter_overrides(self.stage, {}), self.stage)

    def test_non_batch_execution_still_rejects_batch_size_override(self):
        with self.assertRaises(DomainValidationError):
            apply_stage_parameter_overrides(self.stage, {"max_batch_size": 4})
