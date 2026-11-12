from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID

from tgcurator.shared import DomainValidationError, ensure_aware

AUDIT_REDACTED = "[REDACTED]"
_SAFE_AUDIT_KEYS: frozenset[str] = frozenset(
    {
        "secret_id",
        "secret_type",
        "key_id",
        "secret_configured",
        "api_secret_reference",
        "max_input_tokens",
        "token_usage",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "lease_token_cleared",
    }
)
_SENSITIVE_AUDIT_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "plaintext",
    "secret_value",
    "secret_plaintext",
    "api_key",
    "api_hash",
    "bot_token",
    "session_token",
    "csrf_token",
    "csrf",
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
    "ciphertext",
    "nonce",
    "master_key",
)
_BEARER_VALUE_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|api[_-]?hash|bot[_-]?token|authorization|cookie|session_string)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


class AuditActorType(StrEnum):
    ADMIN = "admin"
    SYSTEM = "system"


def _normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def is_sensitive_audit_key(field_name: str) -> bool:
    normalized = _normalize_key(field_name)
    if normalized in _SAFE_AUDIT_KEYS:
        return False
    if normalized in {"secret", "token"}:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_AUDIT_SUBSTRINGS)


def sanitize_audit_value(value: Any, *, field_name: str | None = None) -> Any:
    """Sanitize arbitrary audit snapshot data so plaintext credentials can never persist."""

    if field_name is not None and is_sensitive_audit_key(field_name):
        return AUDIT_REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return AUDIT_REDACTED
    if isinstance(value, (UUID, date, datetime, Enum)):
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)
    if isinstance(value, str):
        scrubbed = _BEARER_VALUE_RE.sub(lambda m: m.group(1) + AUDIT_REDACTED, value)
        return _CREDENTIAL_ASSIGNMENT_RE.sub(
            lambda m: m.group(1) + m.group(2) + AUDIT_REDACTED,
            scrubbed,
        )
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_audit_value(item, field_name=str(key)) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_audit_value(item) for item in value]
    return str(value)


def sanitize_audit_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise DomainValidationError("audit payload must be a mapping")
    sanitized = sanitize_audit_value(payload)
    assert isinstance(sanitized, dict)
    return sanitized


@dataclass(frozen=True, slots=True)
class AuditRecordDraft:
    """Immutable sanitized administrative or system audit record."""

    event_id: str
    actor_type: AuditActorType
    actor_admin_user_id: str | None
    event_type: str
    entity_type: str
    entity_id: str | None
    payload: dict[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise DomainValidationError("event_id must not be blank")
        if not isinstance(self.actor_type, AuditActorType):
            raise DomainValidationError("actor_type must be an AuditActorType")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise DomainValidationError("event_type must not be blank")
        if not isinstance(self.entity_type, str) or not self.entity_type.strip():
            raise DomainValidationError("entity_type must not be blank")
        ensure_aware(self.created_at, field="created_at")
        object.__setattr__(self, "payload", sanitize_audit_payload(self.payload))
