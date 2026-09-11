from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from typing import Any

from tgcurator.shared import DomainValidationError


class PublicationMode(StrEnum):
    NATIVE_FORWARD_WITH_SUPPLEMENT = "native_forward_with_supplement"
    COPY_WITH_CAPTION = "copy_with_caption"
    FORWARD_ONLY = "forward_only"
    METADATA_ONLY = "metadata_only"


def publication_idempotency_key(
    *,
    source_message_id: str,
    destination_channel_id: str,
    routing_policy_version_id: str,
    routing_rule_id: str,
    action_id: str,
    publication_mode: PublicationMode,
    routing_request_id: str | None = None,
    routing_evaluation_id: str | None = None,
) -> str:
    """Build the database-unique identity for one intended publication effect.

    Legacy callers without a routing request or evaluation retain the exact v1
    identity. Durable routing uses v2 so an explicit reroute request creates new
    intents while retries of the same request remain stable.
    """
    identifiers: dict[str, Any] = {
        "source_message_id": source_message_id,
        "destination_channel_id": destination_channel_id,
        "routing_policy_version_id": routing_policy_version_id,
        "routing_rule_id": routing_rule_id,
        "action_id": action_id,
    }
    if any(not isinstance(value, str) or not value.strip() for value in identifiers.values()):
        raise DomainValidationError("publication idempotency key inputs must not be blank")
    if not isinstance(publication_mode, PublicationMode):
        raise DomainValidationError("publication_mode must be a PublicationMode")
    for field, value in (
        ("routing_request_id", routing_request_id),
        ("routing_evaluation_id", routing_evaluation_id),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise DomainValidationError(f"{field} must not be blank")

    parts = {**identifiers, "publication_mode": publication_mode.value}
    version = "v1"
    if routing_request_id is not None or routing_evaluation_id is not None:
        parts["routing_request_id"] = routing_request_id
        parts["routing_evaluation_id"] = routing_evaluation_id
        version = "v2"
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"publication:{version}:" + sha256(canonical.encode("utf-8")).hexdigest()
