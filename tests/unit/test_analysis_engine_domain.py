from __future__ import annotations

import unittest

from tgcurator.domain.analysis import (
    AnalysisPipelineVersion,
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    InferenceProfileVersion,
    InputManifest,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    PipelineStageNode,
    PromptTemplateVersion,
    StageCacheKeyBuilder,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputMode,
    StructuredOutputPolicy,
    StructuredOutputSchemaBuilder,
    StructuredOutputValidator,
    VersionStatus,
    VisualCompositionPolicy,
    evaluate_run_if,
    evaluate_stage_negative_gate,
)
from tgcurator.shared import DomainValidationError


class AnalysisEngineDomainTests(unittest.TestCase):
    def test_label_set_resolves_threshold_scope_and_negative_semantics(self) -> None:
        label_set = _label_set(scope=LabelScope.GLOBAL)

        labels = label_set.resolve_scores({"real_ugc": 0.75, "advertisement": 0.91})

        self.assertEqual(tuple(label.key for label in labels), ("real_ugc", "advertisement"))
        self.assertTrue(labels[0].activated)
        self.assertFalse(labels[0].negative)
        self.assertTrue(labels[1].activated)
        self.assertTrue(labels[1].negative)

    def test_published_label_set_rejects_scope_mismatch_and_unpublished_labels(self) -> None:
        draft_label = _label(
            version_id="label-draft-v1",
            key="draft_label",
            scope=LabelScope.GLOBAL,
            status=VersionStatus.DRAFT,
        )
        label_set = LabelSetVersion(
            version_id="set-v1",
            label_set_id="set",
            version_number=1,
            bindings=(LabelBinding(draft_label, 0.5, 0),),
            status=VersionStatus.PUBLISHED,
        )

        with self.assertRaises(DomainValidationError):
            label_set.validate_for_publication(target_scope=LabelScope.GLOBAL)
        with self.assertRaises(DomainValidationError):
            _label_set(scope=LabelScope.MEDIA).validate_for_publication(
                target_scope=LabelScope.GLOBAL
            )

    def test_prompt_renderer_allows_only_declared_plain_placeholders(self) -> None:
        prompt = PromptTemplateVersion(
            version_id="prompt-v1",
            prompt_template_id="prompt",
            version_number=1,
            system_prompt="Classify {{ label_definitions }}",
            user_prompt_template="Text: {{ message_text }}",
            declared_variables=("label_definitions", "message_text"),
        )

        rendered = prompt.render(
            {
                "label_definitions": [{"key": "real_ugc"}],
                "message_text": "hello",
            }
        )

        self.assertIn('[{"key":"real_ugc"}]', rendered.system_prompt)
        self.assertEqual(rendered.user_prompt, "Text: hello")
        for unsafe in (
            "{{ __import__('os') }}",
            "{{ message_text.upper() }}",
            "{% for x in values %}{{ x }}{% endfor %}",
            "{{ env }}",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(DomainValidationError):
                PromptTemplateVersion(
                    version_id="unsafe-v1",
                    prompt_template_id="unsafe",
                    version_number=1,
                    system_prompt="",
                    user_prompt_template=unsafe,
                    declared_variables=("message_text",),
                )

    def test_dynamic_dense_and_sparse_batch_schemas_use_stable_target_ids(self) -> None:
        label_set = _label_set(scope=LabelScope.GLOBAL)
        builder = StructuredOutputSchemaBuilder()

        dense = builder.build(
            stage_template_version_id="stage-v1",
            label_set=label_set,
            execution_mode=ExecutionMode.BATCH_MESSAGES,
            policy=StructuredOutputPolicy(StructuredOutputMode.DENSE_SCORES),
            expected_target_ids=("message-a", "message-b"),
        )
        sparse = builder.build(
            stage_template_version_id="stage-v2",
            label_set=label_set,
            execution_mode=ExecutionMode.BATCH_MESSAGES,
            policy=StructuredOutputPolicy(StructuredOutputMode.SPARSE_MATCHES),
            expected_target_ids=("message-a",),
        )

        dense_item = dense.document["properties"]["results"]["items"]
        self.assertEqual(dense_item["properties"]["message_id"]["enum"], ["message-a", "message-b"])
        self.assertEqual(
            tuple(dense_item["properties"]["scores"]["properties"]),
            label_set.label_keys,
        )
        self.assertIn("matches", sparse.document["properties"]["results"]["items"]["properties"])
        self.assertNotEqual(dense.schema_hash, sparse.schema_hash)
        self.assertEqual(len(dense.schema_hash), 64)

    def test_batch_validator_preserves_valid_targets_and_retries_only_missing_target(self) -> None:
        label_set = _label_set(scope=LabelScope.GLOBAL)
        validation = StructuredOutputValidator().validate(
            payload={
                "results": [
                    {
                        "message_id": "message-a",
                        "scores": {"real_ugc": 0.9, "advertisement": 0.1},
                    }
                ]
            },
            execution_mode=ExecutionMode.BATCH_MESSAGES,
            policy=StructuredOutputPolicy(StructuredOutputMode.DENSE_SCORES),
            label_set=label_set,
            expected_target_ids=("message-a", "message-b"),
        )

        self.assertEqual(
            tuple(result.target_id for result in validation.valid_results), ("message-a",)
        )
        self.assertEqual(validation.retry_target_ids, ("message-b",))
        self.assertEqual(validation.issues[0].code.value, "MISSING_TARGET_RESULT")

    def test_batch_validator_rejects_unknown_and_duplicate_targets(self) -> None:
        label_set = _label_set(scope=LabelScope.GLOBAL)
        validation = StructuredOutputValidator().validate(
            payload={
                "results": [
                    {
                        "message_id": "message-a",
                        "scores": {"real_ugc": 0.9, "advertisement": 0.1},
                    },
                    {
                        "message_id": "message-a",
                        "scores": {"real_ugc": 0.8, "advertisement": 0.2},
                    },
                    {
                        "message_id": "unknown",
                        "scores": {"real_ugc": 0.8, "advertisement": 0.2},
                    },
                ]
            },
            execution_mode=ExecutionMode.BATCH_MESSAGES,
            policy=StructuredOutputPolicy(),
            label_set=label_set,
            expected_target_ids=("message-a",),
        )

        self.assertEqual(validation.valid_results, ())
        self.assertEqual(validation.retry_target_ids, ("message-a",))
        self.assertEqual(
            {issue.error_type for issue in validation.issues},
            {"duplicate_target_result", "unknown_target_result"},
        )

    def test_sparse_validator_rejects_duplicate_or_unknown_labels(self) -> None:
        label_set = _label_set(scope=LabelScope.MEDIA)
        validator = StructuredOutputValidator()
        for matches in (
            [
                {"label_key": "real_ugc", "score": 0.9},
                {"label_key": "real_ugc", "score": 0.8},
            ],
            [{"label_key": "unknown", "score": 0.8}],
        ):
            with self.subTest(matches=matches):
                validation = validator.validate(
                    payload={"result": {"matches": matches}},
                    execution_mode=ExecutionMode.PER_ASSET,
                    policy=StructuredOutputPolicy(StructuredOutputMode.SPARSE_MATCHES),
                    label_set=label_set,
                    expected_target_ids=("asset-a",),
                )
                self.assertEqual(validation.valid_results, ())
                self.assertEqual(validation.retry_target_ids, ("asset-a",))

    def test_input_manifest_and_cache_identity_are_deterministic_and_version_isolated(self) -> None:
        schema_hash = "a" * 64
        manifest_a = InputManifest.create(
            manifest_version=1,
            target_scope=LabelScope.GLOBAL,
            execution_mode=ExecutionMode.SINGLE_MESSAGE,
            target_ids=("message-a",),
            prompt_version_id="prompt-v1",
            structured_output_schema_hash=schema_hash,
            inference_profile_version_id="profile-v1",
            inputs={"b": 2, "a": 1},
        )
        manifest_b = InputManifest.create(
            manifest_version=1,
            target_scope=LabelScope.GLOBAL,
            execution_mode=ExecutionMode.SINGLE_MESSAGE,
            target_ids=("message-a",),
            prompt_version_id="prompt-v1",
            structured_output_schema_hash=schema_hash,
            inference_profile_version_id="profile-v1",
            inputs={"a": 1, "b": 2},
        )
        builder = StageCacheKeyBuilder()
        common = {
            "policy": CachePolicy.MESSAGE,
            "scope_identity": "message-a",
            "stage_template_version_id": "stage-v1",
            "prompt_version_id": "prompt-v1",
            "label_set_version_id": "set-v1",
            "structured_output_schema_hash": schema_hash,
            "inference_profile_version_id": "profile-v1",
            "input_manifest_hash": manifest_a.input_manifest_hash,
        }

        self.assertEqual(manifest_a, manifest_b)
        cache_a = builder.build(**common)
        cache_b = builder.build(**{**common, "prompt_version_id": "prompt-v2"})
        self.assertIsNotNone(cache_a)
        self.assertIsNotNone(cache_b)
        self.assertNotEqual(cache_a.value, cache_b.value)  # type: ignore[union-attr]
        self.assertIsNone(builder.build(**{**common, "policy": CachePolicy.NONE}))

    def test_run_if_unknown_state_cannot_be_inverted_into_a_match(self) -> None:
        condition = {"not": {"fact": "global.real_ugc", "op": "gte", "value": 0.7}}

        self.assertFalse(evaluate_run_if(condition, {}))
        self.assertFalse(evaluate_run_if(condition, {"global.real_ugc": "high"}))
        self.assertTrue(evaluate_run_if(condition, {"global.real_ugc": 0.2}))

    def test_pipeline_publication_validates_references_conditions_and_dag(self) -> None:
        first = _stage(name="prescreen", scope=LabelScope.GLOBAL, published=True)
        second = _stage(name="media", scope=LabelScope.MEDIA, published=True)
        pipeline = AnalysisPipelineVersion(
            version_id="pipeline-v1",
            analysis_pipeline_id="pipeline",
            version_number=1,
            nodes=(
                PipelineStageNode("prescreen", first),
                PipelineStageNode(
                    "media",
                    second,
                    depends_on=("prescreen",),
                    run_if={
                        "all": [
                            {"fact": "global.real_ugc", "op": "gte", "value": 0.7},
                            {
                                "fact": "message.blocked_from_analysis",
                                "op": "eq",
                                "value": False,
                            },
                        ]
                    },
                ),
            ),
            status=VersionStatus.PUBLISHED,
        )

        self.assertEqual(pipeline.topological_node_ids(), ("prescreen", "media"))
        with self.assertRaises(DomainValidationError):
            AnalysisPipelineVersion(
                version_id="bad-pipeline-v1",
                analysis_pipeline_id="bad-pipeline",
                version_number=1,
                nodes=(
                    PipelineStageNode("prescreen", first),
                    PipelineStageNode(
                        "bad",
                        second,
                        depends_on=("prescreen",),
                        run_if={"fact": "global.unknown", "op": "gte", "value": 0.5},
                    ),
                ),
                status=VersionStatus.PUBLISHED,
            )

    def test_negative_gate_blocks_parent_for_any_global_or_media_target(self) -> None:
        label_set = _label_set(scope=LabelScope.MEDIA)
        validation = StructuredOutputValidator().validate(
            payload={
                "results": [
                    {
                        "asset_id": "asset-a",
                        "scores": {"real_ugc": 0.2, "advertisement": 0.95},
                    },
                    {
                        "asset_id": "asset-b",
                        "scores": {"real_ugc": 0.9, "advertisement": 0.1},
                    },
                ]
            },
            execution_mode=ExecutionMode.BATCH_ASSETS,
            policy=StructuredOutputPolicy(),
            label_set=label_set,
            expected_target_ids=("asset-a", "asset-b"),
        )

        gate = evaluate_stage_negative_gate(validation.valid_results)
        self.assertTrue(gate.blocked)
        self.assertEqual(gate.matches[0].target_id, "asset-a")
        self.assertEqual(gate.matches[0].label_key, "advertisement")


def _label(
    *,
    version_id: str,
    key: str,
    scope: LabelScope,
    negative: bool = False,
    status: VersionStatus = VersionStatus.PUBLISHED,
) -> LabelDefinitionVersion:
    return LabelDefinitionVersion(
        version_id=version_id,
        definition_id=f"definition-{key}",
        version_number=1,
        key=key,
        display_name=key.replace("_", " ").title(),
        description=f"Definition for {key}",
        scope=scope,
        negative=negative,
        status=status,
    )


def _label_set(*, scope: LabelScope) -> LabelSetVersion:
    return LabelSetVersion(
        version_id=f"{scope.value}-set-v1",
        label_set_id=f"{scope.value}-set",
        version_number=1,
        bindings=(
            LabelBinding(
                _label(version_id=f"{scope.value}-real-v1", key="real_ugc", scope=scope), 0.7, 0
            ),
            LabelBinding(
                _label(
                    version_id=f"{scope.value}-ad-v1",
                    key="advertisement",
                    scope=scope,
                    negative=True,
                ),
                0.9,
                1,
            ),
        ),
        status=VersionStatus.PUBLISHED,
    )


def _stage(*, name: str, scope: LabelScope, published: bool) -> AnalysisStageTemplateVersion:
    batch = scope is LabelScope.GLOBAL
    prompt = PromptTemplateVersion(
        version_id=f"{name}-prompt-v1",
        prompt_template_id=f"{name}-prompt",
        version_number=1,
        system_prompt="Use labels {{ label_definitions }}",
        user_prompt_template=(
            "Messages {{ message_text }}" if batch else "Assets {{ asset_manifest }}"
        ),
        declared_variables=(
            ("label_definitions", "message_text")
            if batch
            else ("label_definitions", "asset_manifest")
        ),
        status=VersionStatus.PUBLISHED if published else VersionStatus.DRAFT,
    )
    profile = InferenceProfileVersion(
        version_id=f"{name}-profile-v1",
        inference_profile_id=f"{name}-profile",
        version_number=1,
        provider_adapter="openai_compatible",
        base_url="https://model.example/v1",
        model_name="test-model",
        api_secret_reference="secret-ref",
        capabilities=("text", "vision") if scope is LabelScope.MEDIA else ("text",),
        timeout_seconds=30,
        max_concurrency=2,
        max_images=8,
        max_input_tokens=4096,
        structured_output_support=True,
        sampling_parameters={"temperature": 0},
        status=VersionStatus.PUBLISHED if published else VersionStatus.DRAFT,
    )
    return AnalysisStageTemplateVersion(
        version_id=f"{name}-stage-v1",
        stage_template_id=f"{name}-stage",
        version_number=1,
        name=name,
        target_scope=scope,
        execution_mode=(ExecutionMode.BATCH_MESSAGES if batch else ExecutionMode.BATCH_ASSETS),
        prompt=prompt,
        label_set=_label_set(scope=scope),
        structured_output_policy=StructuredOutputPolicy(),
        input_policy=StageInputPolicy(text=batch, media=not batch),
        visual_composition_policy=VisualCompositionPolicy.ADAPTIVE,
        inference_profile=profile,
        cache_policy=CachePolicy.NONE,
        timeout_seconds=20,
        retry_policy=StageRetryPolicy(),
        max_batch_size=64 if batch else 8,
        max_concurrency=2,
        status=VersionStatus.PUBLISHED if published else VersionStatus.DRAFT,
    )


if __name__ == "__main__":
    unittest.main()
