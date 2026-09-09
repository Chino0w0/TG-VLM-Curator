from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from tgcurator.shared import DomainValidationError

from .common import (
    VersionStatus,
    require_json_object,
    validate_identifier,
    validate_non_blank,
    validate_positive_int,
)

_SENSITIVE_PARAMETER_FRAGMENTS = ("secret", "password", "api_key", "apikey", "authorization")


@dataclass(frozen=True, slots=True)
class InferenceProfileVersion:
    version_id: str
    inference_profile_id: str
    version_number: int
    provider_adapter: str
    base_url: str
    model_name: str
    api_secret_reference: str
    capabilities: tuple[str, ...]
    timeout_seconds: float
    max_concurrency: int
    max_images: int
    max_input_tokens: int
    structured_output_support: bool
    sampling_parameters: Mapping[str, Any]
    status: VersionStatus = VersionStatus.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "version_id",
            validate_non_blank(self.version_id, field="inference profile version_id"),
        )
        object.__setattr__(
            self,
            "inference_profile_id",
            validate_non_blank(self.inference_profile_id, field="inference profile id"),
        )
        validate_positive_int(self.version_number, field="inference profile version number")
        object.__setattr__(
            self,
            "provider_adapter",
            validate_identifier(self.provider_adapter, field="provider adapter"),
        )
        base_url = validate_non_blank(self.base_url, field="inference base_url").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DomainValidationError("inference base_url must be an absolute HTTP(S) URL")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(
            self, "model_name", validate_non_blank(self.model_name, field="model name")
        )
        object.__setattr__(
            self,
            "api_secret_reference",
            validate_non_blank(self.api_secret_reference, field="API secret reference"),
        )
        if not isinstance(self.capabilities, tuple) or any(
            not isinstance(capability, str) for capability in self.capabilities
        ):
            raise DomainValidationError("capabilities must be a tuple of strings")
        normalized_capabilities = tuple(
            validate_identifier(capability, field="inference capability")
            for capability in self.capabilities
        )
        if len(set(normalized_capabilities)) != len(normalized_capabilities):
            raise DomainValidationError("inference capabilities cannot contain duplicates")
        object.__setattr__(self, "capabilities", normalized_capabilities)
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise DomainValidationError("inference timeout_seconds must be positive")
        validate_positive_int(self.max_concurrency, field="max_concurrency")
        validate_positive_int(self.max_images, field="max_images")
        validate_positive_int(self.max_input_tokens, field="max_input_tokens")
        if not isinstance(self.structured_output_support, bool):
            raise DomainValidationError("structured_output_support must be a boolean")
        normalized_parameters = require_json_object(
            self.sampling_parameters, field="sampling parameters"
        )
        if any(
            fragment in str(key).lower()
            for key in normalized_parameters
            for fragment in _SENSITIVE_PARAMETER_FRAGMENTS
        ):
            raise DomainValidationError("sampling parameters cannot contain secrets")
        object.__setattr__(self, "sampling_parameters", normalized_parameters)
        if not isinstance(self.status, VersionStatus):
            raise DomainValidationError("inference profile status must be a VersionStatus")
