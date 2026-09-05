from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

SENSITIVE_FIELD_MARKERS = (
    "password",
    "secret",
    "token",
    "authorization",
    "ciphertext",
    "nonce",
    "master_key",
)
REDACTED_VALUE = "[REDACTED]"


def redact_for_log(value: Any, *, field_name: str | None = None) -> Any:
    """Return JSON-compatible log data without credential material."""
    if field_name is not None and _is_sensitive_field(field_name):
        return REDACTED_VALUE
    if isinstance(value, Mapping):
        return {str(key): redact_for_log(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_for_log(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _is_sensitive_field(field_name: str) -> bool:
    normalized_name = field_name.lower().replace("-", "_")
    return any(marker in normalized_name for marker in SENSITIVE_FIELD_MARKERS)


class JsonFormatter(logging.Formatter):
    """Minimal structured formatter that intentionally excludes exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = str(event)
        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload["context"] = redact_for_log(context)
        if record.exc_info:
            payload["exception"] = "exception details withheld from structured logs"
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_structured_logging(level: str) -> None:
    """Configure one process-wide JSON stderr handler without duplicating handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())
    for handler in root_logger.handlers:
        if getattr(handler, "_tgcurator_structured", False):
            handler.setLevel(level.upper())
            return

    handler = logging.StreamHandler()
    handler.setLevel(level.upper())
    handler.setFormatter(JsonFormatter())
    handler._tgcurator_structured = True  # type: ignore[attr-defined]
    root_logger.addHandler(handler)
