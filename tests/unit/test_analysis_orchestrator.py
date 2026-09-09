from __future__ import annotations

import asyncio
import unittest

from tgcurator.application.analysis import AnalysisOrchestrator, AnalysisTargetInput
from tgcurator.application.ports.analysis import InferenceResponse
from tgcurator.domain.analysis import (
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    InferenceProfileVersion,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    PromptTemplateVersion,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputPolicy,
    VersionStatus,
    VisualCompositionPolicy,
)


class FakeProvider:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests = []

    async def infer(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return InferenceResponse(
            payload=self.payload,
            provider_adapter="openai_compatible",
            model_name="test-model",
            raw_response={"choices": []},
        )


class AnalysisOrchestratorTests(unittest.TestCase):
    def test_unconfigured_provider_returns_explicit_not_ready_failure(self) -> None:
        result = asyncio.run(
            AnalysisOrchestrator(provider=None).execute(
                stage=_stage(scope=LabelScope.GLOBAL),
                targets=(
                    AnalysisTargetInput(
                        target_id="message-a", message_id="message-a", text="hello"
                    ),
                ),
            )
        )

        self.assertFalse(result.succeeded)
        self.assertIsNone(result.validation)
        self.assertEqual(result.failure.code.value, "PROVIDER_ERROR")  # type: ignore[union-attr]
        self.assertEqual(
            result.failure.error_type,
            "InferenceProviderNotConfigured",  # type: ignore[union-attr]
        )

    def test_validated_negative_result_is_reported_without_fabrication(self) -> None:
        provider = FakeProvider(
            {
                "result": {
                    "scores": {"keep": 0.1, "block": 0.95},
                }
            }
        )
        stage = _stage(scope=LabelScope.GLOBAL)
        target = AnalysisTargetInput(target_id="message-a", message_id="message-a", text="buy now")

        result = asyncio.run(
            AnalysisOrchestrator(provider=provider).execute(stage=stage, targets=(target,))
        )

        self.assertTrue(result.succeeded)
        self.assertTrue(result.negative_gate_hit)
        self.assertEqual(len(provider.requests), 1)
        request = provider.requests[0]
        self.assertEqual(request.profile.version_id, stage.inference_profile.version_id)
        self.assertEqual(len(request.input_manifest.input_manifest_hash), 64)
        self.assertIn("result", request.response_schema["properties"])

    def test_media_unavailable_is_an_explicit_input_failure(self) -> None:
        provider = FakeProvider({})
        result = asyncio.run(
            AnalysisOrchestrator(provider=provider).execute(
                stage=_stage(scope=LabelScope.MEDIA),
                targets=(
                    AnalysisTargetInput(
                        target_id="asset-a",
                        message_id="message-a",
                        asset_id="asset-a",
                    ),
                ),
            )
        )

        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure.code.value, "MEDIA_UNAVAILABLE")  # type: ignore[union-attr]
        self.assertEqual(provider.requests, [])


def _stage(*, scope: LabelScope) -> AnalysisStageTemplateVersion:
    labels = (
        LabelBinding(
            LabelDefinitionVersion(
                version_id=f"{scope.value}-keep-v1",
                definition_id=f"{scope.value}-keep",
                version_number=1,
                key="keep",
                display_name="Keep",
                description="Keep content",
                scope=scope,
                status=VersionStatus.PUBLISHED,
            ),
            0.7,
            0,
        ),
        LabelBinding(
            LabelDefinitionVersion(
                version_id=f"{scope.value}-block-v1",
                definition_id=f"{scope.value}-block",
                version_number=1,
                key="block",
                display_name="Block",
                description="Block content",
                scope=scope,
                negative=True,
                status=VersionStatus.PUBLISHED,
            ),
            0.9,
            1,
        ),
    )
    label_set = LabelSetVersion(
        version_id=f"{scope.value}-set-v1",
        label_set_id=f"{scope.value}-set",
        version_number=1,
        bindings=labels,
        status=VersionStatus.PUBLISHED,
    )
    media = scope is LabelScope.MEDIA
    prompt = PromptTemplateVersion(
        version_id=f"{scope.value}-prompt-v1",
        prompt_template_id=f"{scope.value}-prompt",
        version_number=1,
        system_prompt="Labels {{ label_definitions }}",
        user_prompt_template="Input {{ asset_manifest }}" if media else "Input {{ message_text }}",
        declared_variables=(
            ("label_definitions", "asset_manifest")
            if media
            else ("label_definitions", "message_text")
        ),
        status=VersionStatus.PUBLISHED,
    )
    profile = InferenceProfileVersion(
        version_id="profile-v1",
        inference_profile_id="profile",
        version_number=1,
        provider_adapter="openai_compatible",
        base_url="https://model.example/v1",
        model_name="test-model",
        api_secret_reference="provider-key",
        capabilities=("text", "vision"),
        timeout_seconds=30,
        max_concurrency=4,
        max_images=8,
        max_input_tokens=4096,
        structured_output_support=True,
        sampling_parameters={"temperature": 0},
        status=VersionStatus.PUBLISHED,
    )
    return AnalysisStageTemplateVersion(
        version_id=f"{scope.value}-stage-v1",
        stage_template_id=f"{scope.value}-stage",
        version_number=1,
        name=f"{scope.value}_analysis",
        target_scope=scope,
        execution_mode=(ExecutionMode.PER_ASSET if media else ExecutionMode.SINGLE_MESSAGE),
        prompt=prompt,
        label_set=label_set,
        structured_output_policy=StructuredOutputPolicy(),
        input_policy=StageInputPolicy(text=not media, media=media),
        visual_composition_policy=VisualCompositionPolicy.ADAPTIVE,
        inference_profile=profile,
        cache_policy=CachePolicy.ASSET if media else CachePolicy.MESSAGE,
        timeout_seconds=20,
        retry_policy=StageRetryPolicy(),
        status=VersionStatus.PUBLISHED,
    )


if __name__ == "__main__":
    unittest.main()
