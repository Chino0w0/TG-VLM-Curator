from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from tgcurator.application.ports.analysis import (
    InferenceProviderFailure,
    InferenceRequest,
)
from tgcurator.domain.analysis import (
    ExecutionMode,
    InferenceProfileVersion,
    InputManifest,
    LabelScope,
    VersionStatus,
)
from tgcurator.infrastructure.inference import OpenAICompatibleInferenceProvider


class FakeSecretResolver:
    def __init__(self, value: str = "top-secret") -> None:
        self.value = value
        self.references: list[str] = []

    async def resolve(self, *, secret_reference: str) -> str:
        self.references.append(secret_reference)
        return self.value


class OpenAICompatibleInferenceProviderTests(unittest.TestCase):
    def test_sends_strict_schema_and_returns_parsed_payload(self) -> None:
        observed: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["authorization"] = request.headers.get("Authorization")
            observed["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps({"result": {"scores": {"keep": 0.9}}})}}
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                },
            )

        resolver = FakeSecretResolver()
        provider = OpenAICompatibleInferenceProvider(
            secret_resolver=resolver,
            transport=httpx.MockTransport(handler),
        )

        response = asyncio.run(provider.infer(_request()))

        self.assertEqual(response.payload, {"result": {"scores": {"keep": 0.9}}})
        self.assertEqual(resolver.references, ["provider-secret"])
        self.assertEqual(observed["authorization"], "Bearer top-secret")
        body = observed["body"]
        self.assertEqual(body["model"], "test-model")  # type: ignore[index]
        self.assertTrue(body["response_format"]["json_schema"]["strict"])  # type: ignore[index]
        self.assertEqual(body["temperature"], 0)  # type: ignore[index]

    def test_maps_timeout_and_rate_limit_to_sanitized_retryable_failures(self) -> None:
        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("sensitive timeout message", request=request)

        timeout_provider = OpenAICompatibleInferenceProvider(
            secret_resolver=FakeSecretResolver(),
            transport=httpx.MockTransport(timeout_handler),
        )
        with self.assertRaises(InferenceProviderFailure) as timeout_context:
            asyncio.run(timeout_provider.infer(_request()))
        self.assertEqual(timeout_context.exception.code.value, "TIMEOUT")
        self.assertTrue(timeout_context.exception.retryable)
        self.assertEqual(timeout_context.exception.error_type, "ReadTimeout")
        self.assertNotIn("sensitive", str(timeout_context.exception))

        rate_provider = OpenAICompatibleInferenceProvider(
            secret_resolver=FakeSecretResolver(),
            transport=httpx.MockTransport(lambda request: httpx.Response(429, json={})),
        )
        with self.assertRaises(InferenceProviderFailure) as rate_context:
            asyncio.run(rate_provider.infer(_request()))
        self.assertEqual(rate_context.exception.error_type, "ProviderHTTP429")
        self.assertTrue(rate_context.exception.retryable)
        self.assertEqual(rate_context.exception.raw_response, {})
        self.assertEqual(rate_context.exception.http_status, 429)
        self.assertIsNotNone(rate_context.exception.latency_ms)

    def test_preserves_safe_failure_audit_context(self) -> None:
        provider = OpenAICompatibleInferenceProvider(
            secret_resolver=FakeSecretResolver(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    503,
                    json={
                        "error": {"type": "overloaded"},
                        "usage": {"prompt_tokens": 7},
                    },
                )
            ),
        )

        with self.assertRaises(InferenceProviderFailure) as context:
            asyncio.run(provider.infer(_request()))

        failure = context.exception
        self.assertEqual(failure.error_type, "ProviderHTTP503")
        self.assertEqual(
            failure.raw_response,
            {"error": {"type": "overloaded"}, "usage": {"prompt_tokens": 7}},
        )
        self.assertEqual(failure.token_usage, {"prompt_tokens": 7})
        self.assertEqual(failure.http_status, 503)
        self.assertIsNotNone(failure.latency_ms)

    def test_invalid_json_records_status_and_latency_without_response_text(self) -> None:
        provider = OpenAICompatibleInferenceProvider(
            secret_resolver=FakeSecretResolver(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"secret non-json response")
            ),
        )

        with self.assertRaises(InferenceProviderFailure) as context:
            asyncio.run(provider.infer(_request()))

        failure = context.exception
        self.assertEqual(failure.error_type, "ProviderResponseNotJson")
        self.assertIsNone(failure.raw_response)
        self.assertEqual(failure.http_status, 200)
        self.assertIsNotNone(failure.latency_ms)
        self.assertNotIn("secret non-json", str(failure))

    def test_rejects_invalid_provider_content_without_assignments(self) -> None:
        provider = OpenAICompatibleInferenceProvider(
            secret_resolver=FakeSecretResolver(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"choices": [{"message": {"content": "not json"}}]}
                )
            ),
        )

        with self.assertRaises(InferenceProviderFailure) as context:
            asyncio.run(provider.infer(_request()))

        self.assertEqual(context.exception.code.value, "INVALID_STRUCTURED_OUTPUT")
        self.assertEqual(context.exception.error_type, "ProviderContentNotJson")
        self.assertEqual(
            context.exception.raw_response,
            {"choices": [{"message": {"content": "not json"}}]},
        )
        self.assertEqual(context.exception.http_status, 200)
        self.assertIsNotNone(context.exception.latency_ms)


def _request() -> InferenceRequest:
    profile = InferenceProfileVersion(
        version_id="profile-v1",
        inference_profile_id="profile",
        version_number=1,
        provider_adapter="openai_compatible",
        base_url="https://model.example/v1",
        model_name="test-model",
        api_secret_reference="provider-secret",
        capabilities=("text",),
        timeout_seconds=30,
        max_concurrency=2,
        max_images=4,
        max_input_tokens=4096,
        structured_output_support=True,
        sampling_parameters={"temperature": 0},
        status=VersionStatus.PUBLISHED,
    )
    schema = {
        "type": "object",
        "properties": {"result": {"type": "object"}},
        "required": ["result"],
    }
    manifest = InputManifest.create(
        manifest_version=1,
        target_scope=LabelScope.GLOBAL,
        execution_mode=ExecutionMode.SINGLE_MESSAGE,
        target_ids=("message-a",),
        prompt_version_id="prompt-v1",
        structured_output_schema_hash="a" * 64,
        inference_profile_version_id="profile-v1",
        inputs={"targets": [{"target_id": "message-a", "text": "hello"}]},
    )
    return InferenceRequest(
        profile=profile,
        system_prompt="system",
        user_prompt="user",
        input_manifest=manifest,
        response_schema=schema,
        timeout_seconds=5,
    )


if __name__ == "__main__":
    unittest.main()
