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
) -> str:
    """Build the database-unique identity for one intended publication effect."""
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
    parts = {**identifiers, "publication_mode": publication_mode.value}
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "publication:v1:" + sha256(canonical.encode("utf-8")).hexdigest()
