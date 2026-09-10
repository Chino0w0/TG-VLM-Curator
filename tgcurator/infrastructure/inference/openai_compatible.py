from __future__ import annotations

import json
from collections.abc import Mapping
from time import monotonic
from typing import Any

import httpx

from tgcurator.application.ports.analysis import (
    InferenceProviderFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceSecretResolver,
)
from tgcurator.domain.analysis import AnalysisErrorCode


class OpenAICompatibleInferenceProvider:
    """OpenAI-compatible chat-completions adapter with strict structured output."""

    def __init__(
        self,
        *,
        secret_resolver: InferenceSecretResolver,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "tg-vlm-curator/0.1",
    ) -> None:
        self._secret_resolver = secret_resolver
        self._transport = transport
        self._user_agent = user_agent

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        if request.profile.provider_adapter != "openai_compatible":
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.PROVIDER_ERROR,
                error_type="UnsupportedProviderAdapter",
                retryable=False,
            )
        if not request.profile.structured_output_support:
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.PROVIDER_ERROR,
                error_type="StructuredOutputNotSupported",
                retryable=False,
            )
        try:
            secret = await self._secret_resolver.resolve(
                secret_reference=request.profile.api_secret_reference
            )
        except Exception as error:
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.PROVIDER_ERROR,
                error_type="InferenceSecretUnavailable",
                retryable=False,
            ) from error
        if not isinstance(secret, str) or not secret.strip():
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.PROVIDER_ERROR,
                error_type="InferenceSecretUnavailable",
                retryable=False,
            )

        endpoint = f"{request.profile.base_url}/chat/completions"
        body = _request_body(request)
        started = monotonic()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(request.timeout_seconds),
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                    "User-Agent": self._user_agent,
                },
            ) as client:
                response = await client.post(endpoint, json=body)
        except httpx.TimeoutException as error:
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.TIMEOUT,
                error_type=type(error).__name__,
                retryable=True,
                latency_ms=(monotonic() - started) * 1000.0,
            ) from error
        except httpx.RequestError as error:
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.PROVIDER_ERROR,
                error_type=type(error).__name__,
                retryable=True,
                latency_ms=(monotonic() - started) * 1000.0,
            ) from error
        latency_ms = (monotonic() - started) * 1000.0
        try:
            decoded = response.json()
        except ValueError as error:
            code = (
                AnalysisErrorCode.PROVIDER_ERROR
                if response.status_code < 200 or response.status_code >= 300
                else AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT
            )
            error_type = (
                f"ProviderHTTP{response.status_code}"
                if code is AnalysisErrorCode.PROVIDER_ERROR
                else "ProviderResponseNotJson"
            )
            raise InferenceProviderFailure(
                code=code,
                error_type=error_type,
                retryable=_is_retryable_status(response.status_code),
                latency_ms=latency_ms,
                http_status=response.status_code,
            ) from error

        raw = dict(decoded) if isinstance(decoded, Mapping) else None
        usage = decoded.get("usage") if isinstance(decoded, Mapping) else None
        token_usage = dict(usage) if isinstance(usage, Mapping) else None
        if response.status_code < 200 or response.status_code >= 300:
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.PROVIDER_ERROR,
                error_type=f"ProviderHTTP{response.status_code}",
                retryable=_is_retryable_status(response.status_code),
                raw_response=raw,
                latency_ms=latency_ms,
                http_status=response.status_code,
                token_usage=token_usage,
            )
        if raw is None:
            raise InferenceProviderFailure(
                code=AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
                error_type="ProviderResponseNotObject",
                retryable=True,
                latency_ms=latency_ms,
                http_status=response.status_code,
            )
        try:
            payload = _extract_structured_payload(raw)
        except InferenceProviderFailure as error:
            raise InferenceProviderFailure(
                code=error.code,
                error_type=error.error_type,
                retryable=error.retryable,
                raw_response=raw,
                latency_ms=latency_ms,
                http_status=response.status_code,
                token_usage=token_usage,
            ) from error
        return InferenceResponse(
            payload=payload,
            provider_adapter=request.profile.provider_adapter,
            model_name=request.profile.model_name,
            raw_response=raw,
            latency_ms=latency_ms,
            http_status=response.status_code,
            token_usage=token_usage,
        )


class UnavailableInferenceProvider:
    """Explicit not-ready adapter used when no model deployment is configured."""

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        raise InferenceProviderFailure(
            code=AnalysisErrorCode.PROVIDER_ERROR,
            error_type="InferenceProviderNotConfigured",
            retryable=True,
        )


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _request_body(request: InferenceRequest) -> dict[str, Any]:
    sampling_parameters = dict(request.profile.sampling_parameters)
    reserved = {"model", "messages", "response_format", "stream"}
    if reserved.intersection(sampling_parameters):
        raise InferenceProviderFailure(
            code=AnalysisErrorCode.PROVIDER_ERROR,
            error_type="ReservedSamplingParameter",
            retryable=False,
        )
    messages: list[dict[str, Any]] = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    messages.append({"role": "user", "content": _user_content(request)})
    return {
        "model": request.profile.model_name,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tgcurator_analysis",
                "strict": True,
                "schema": dict(request.response_schema),
            },
        },
        "stream": False,
        **sampling_parameters,
    }


def _user_content(request: InferenceRequest) -> str | list[dict[str, Any]]:
    image_urls = _image_urls(request.input_manifest.content)
    if not image_urls:
        return request.user_prompt
    return [
        {"type": "text", "text": request.user_prompt},
        *({"type": "image_url", "image_url": {"url": url}} for url in image_urls),
    ]


def _image_urls(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        return ()
    targets = inputs.get("targets")
    if not isinstance(targets, list):
        return ()
    urls: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        media = target.get("media")
        if not isinstance(media, list):
            continue
        for item in media:
            if not isinstance(item, Mapping):
                continue
            value = item.get("provider_url") or item.get("data_url")
            if isinstance(value, str) and value.strip():
                urls.append(value)
    return tuple(urls)


def _extract_structured_payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise InferenceProviderFailure(
            code=AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
            error_type="ProviderChoicesMissing",
            retryable=True,
        )
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise InferenceProviderFailure(
            code=AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
            error_type="ProviderMessageMissing",
            retryable=True,
        )
    content = message.get("content")
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        raise InferenceProviderFailure(
            code=AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
            error_type="ProviderContentInvalid",
            retryable=True,
        )
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InferenceProviderFailure(
            code=AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
            error_type="ProviderContentNotJson",
            retryable=True,
        ) from error
    if not isinstance(payload, Mapping):
        raise InferenceProviderFailure(
            code=AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
            error_type="ProviderContentNotObject",
            retryable=True,
        )
    return dict(payload)
