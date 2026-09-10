from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from numbers import Real
from typing import Any, NoReturn

from tgcurator.shared import DomainValidationError

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class VersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class LabelScope(StrEnum):
    GLOBAL = "global"
    MEDIA = "media"


class ExecutionMode(StrEnum):
    BATCH_MESSAGES = "batch_messages"
    SINGLE_MESSAGE = "single_message"
    PER_ASSET = "per_asset"
    BATCH_ASSETS = "batch_assets"


class StructuredOutputMode(StrEnum):
    DENSE_SCORES = "dense_scores"
    SPARSE_MATCHES = "sparse_matches"


class CachePolicy(StrEnum):
    NONE = "none"
    MESSAGE = "message"
    MESSAGE_VISUAL = "message_visual"
    ASSET = "asset"


class VisualCompositionPolicy(StrEnum):
    RAW = "raw"
    CONTACT_SHEET = "contact_sheet"
    ADAPTIVE = "adaptive"


class ResultOrigin(StrEnum):
    INFERENCE = "inference"
    CACHE = "cache"


class AnalysisRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED_NEGATIVE_GATE = "blocked_negative_gate"
    FAILED = "failed"


class StageRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED_CONDITION = "skipped_condition"
    SKIPPED_NEGATIVE_GATE = "skipped_negative_gate"


class AnalysisErrorCode(StrEnum):
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_STRUCTURED_OUTPUT = "INVALID_STRUCTURED_OUTPUT"
    MISSING_TARGET_RESULT = "MISSING_TARGET_RESULT"
    INPUT_BUILD_ERROR = "INPUT_BUILD_ERROR"
    MEDIA_UNAVAILABLE = "MEDIA_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def validate_identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must not be blank")
    normalized = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise DomainValidationError(
            f"{field} must start with a letter or underscore and contain only "
            "letters, digits, dots, underscores, or hyphens"
        )
    return normalized


def validate_non_blank(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must not be blank")
    return value.strip()


def validate_score(value: float, *, field: str = "score") -> float:
    if (
        not isinstance(value, Real)
        or isinstance(value, bool)
        or not isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise DomainValidationError(f"{field} must be a finite number in [0, 1]")
    return float(value)


def validate_positive_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DomainValidationError(f"{field} must be a positive integer")
    return value


def canonical_json(value: Any) -> str:
    """Return an immutable, deterministic JSON representation."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise DomainValidationError("value must be finite JSON-compatible data") from error


def canonical_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(value: str) -> Any:
    if not isinstance(value, str):
        raise DomainValidationError("canonical JSON must be a string")
    try:
        return json.loads(value, parse_constant=_reject_non_finite_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise DomainValidationError("canonical JSON is invalid") from error


def require_json_object(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainValidationError(f"{field} must be an object")
    return load_json(canonical_json(value))


def require_json_array(value: Sequence[Any], *, field: str) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise DomainValidationError(f"{field} must be an array")
    normalized = load_json(canonical_json(value))
    if not isinstance(normalized, list):
        raise DomainValidationError(f"{field} must be an array")
    return normalized


def validate_hash(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise DomainValidationError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _reject_non_finite_json(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")
