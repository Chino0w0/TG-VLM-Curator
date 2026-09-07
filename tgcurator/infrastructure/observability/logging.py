from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import SecretBytes, SecretStr

REDACTED_VALUE = "[REDACTED]"
RECURSIVE_VALUE = "[RECURSIVE]"
CORRELATION_FIELDS = (
    "message_id",
    "source_channel_id",
    "processing_range_id",
    "range_execution_id",
    "analysis_run_id",
    "stage_run_id",
    "inference_call_id",
    "routing_evaluation_id",
    "publication_intent_id",
)
_SAFE_SECRET_METADATA_FIELDS = {"secret_id", "secret_type", "key_id", "secret_configured"}
_SENSITIVE_PHRASES = (
    "password",
    "passwd",
    "api_key",
    "api_hash",
    "bot_token",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "set_cookie",
    "telegram_session",
    "session_string",
    "login_code",
    "verification_code",
    "phone_code",
    "two_factor",
    "2fa",
    "otp",
    "csrf",
    "ciphertext",
    "nonce",
    "master_key",
    "secret_value",
    "secret_plaintext",
)
_BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|api[_-]?hash|token|authorization|cookie|session)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def _normalize_field_name(field_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", field_name.lower()).strip("_")


def _is_sensitive_field(field_name: str) -> bool:
    normalized = _normalize_field_name(field_name)
    if normalized in _SAFE_SECRET_METADATA_FIELDS:
        return False
    return any(phrase in normalized for phrase in _SENSITIVE_PHRASES)


def redact_text(value: str) -> str:
    value = _BEARER_PATTERN.sub(lambda match: match.group(1) + REDACTED_VALUE, value)
    return _KEY_VALUE_PATTERN.sub(
        lambda match: match.group(1) + match.group(2) + REDACTED_VALUE,
        value,
    )


def redact_for_log(
    value: Any,
    *,
    field_name: str | None = None,
    _seen: set[int] | None = None,
) -> Any:
    """Return bounded JSON-compatible data without credential material."""
    if field_name is not None and _is_sensitive_field(field_name):
        return REDACTED_VALUE
    if isinstance(value, (SecretStr, SecretBytes)):
        return REDACTED_VALUE
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (UUID, date, datetime, Path, Enum)):
        return str(value)

    seen = _seen if _seen is not None else set()
    object_id = id(value)
    if object_id in seen:
        return RECURSIVE_VALUE

    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            return {
                str(key): redact_for_log(item, field_name=str(key), _seen=seen)
                for key, item in value.items()
            }
        finally:
            seen.remove(object_id)
    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(object_id)
        try:
            return [redact_for_log(item, _seen=seen) for item in value]
        finally:
            seen.remove(object_id)
    return f"<{type(value).__name__}>"


class JsonFormatter(logging.Formatter):
    """Structured formatter that never serializes traceback text or secret wrappers."""

    def __init__(self, *, service: str = "tg-vlm-curator") -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "service": self._service,
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = redact_for_log(event)
        for field in CORRELATION_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = redact_for_log(value, field_name=field)
        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload["context"] = redact_for_log(context)
        if record.exc_info:
            exception_type = record.exc_info[0]
            payload["exception_type"] = getattr(exception_type, "__name__", "Exception")
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _resolve_level(level: str) -> int:
    normalized = level.strip().upper()
    resolved = getattr(logging, normalized, None)
    if not isinstance(resolved, int):
        raise ValueError(f"unsupported log level: {level}")
    return resolved


def configure_structured_logging(level: str, *, service: str = "tg-vlm-curator") -> None:
    """Replace root handlers so a service emits one consistent JSON stream."""
    resolved_level = _resolve_level(level)
    root_logger = logging.getLogger()
    root_logger.setLevel(resolved_level)
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
        existing.close()

    handler = logging.StreamHandler()
    handler.setLevel(resolved_level)
    handler.setFormatter(JsonFormatter(service=service))
    handler._tgcurator_structured = True
    root_logger.addHandler(handler)
