from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256

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
    parts = {
        "source_message_id": source_message_id,
        "destination_channel_id": destination_channel_id,
        "routing_policy_version_id": routing_policy_version_id,
        "routing_rule_id": routing_rule_id,
        "action_id": action_id,
        "publication_mode": publication_mode.value,
    }
    if any(not value.strip() for key, value in parts.items() if key != "publication_mode"):
        raise DomainValidationError("publication idempotency key inputs must not be blank")
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "publication:v1:" + sha256(canonical.encode("utf-8")).hexdigest()
