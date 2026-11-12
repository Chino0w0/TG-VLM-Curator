from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from .errors import DomainValidationError
from .time import ensure_aware


@dataclass(frozen=True, slots=True)
class CursorToken:
    """Opaque sort-key cursor ordered by `(timestamp DESC, entity_id DESC)`."""

    timestamp: datetime
    entity_id: str

    def __post_init__(self) -> None:
        ensure_aware(self.timestamp, field="timestamp")
        if not isinstance(self.entity_id, str) or not self.entity_id.strip():
            raise DomainValidationError("cursor entity_id must not be blank")
        try:
            UUID(self.entity_id)
        except ValueError as exc:
            raise DomainValidationError("cursor entity_id must be a valid UUID") from exc


def encode_cursor(*, timestamp: datetime, entity_id: str | UUID) -> str:
    token = CursorToken(timestamp=timestamp, entity_id=str(entity_id))
    payload = json.dumps(
        {"ts": token.timestamp.isoformat(), "id": token.entity_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(raw_cursor: str) -> CursorToken:
    if not isinstance(raw_cursor, str) or not raw_cursor.strip():
        raise DomainValidationError("cursor must be a non-empty string")
    normalized = raw_cursor.strip()
    padding = "=" * (-len(normalized) % 4)
    try:
        decoded = base64.urlsafe_b64decode((normalized + padding).encode("ascii"))
        payload: Any = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainValidationError("cursor is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"id", "ts"}:
        raise DomainValidationError("cursor payload has an unexpected shape")
    raw_ts = payload.get("ts")
    raw_id = payload.get("id")
    if not isinstance(raw_ts, str) or not isinstance(raw_id, str):
        raise DomainValidationError("cursor fields must be strings")
    try:
        parsed_ts = datetime.fromisoformat(raw_ts)
    except ValueError as exc:
        raise DomainValidationError("cursor timestamp is malformed") from exc
    return CursorToken(timestamp=parsed_ts, entity_id=raw_id)
