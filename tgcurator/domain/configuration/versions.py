from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any

from tgcurator.shared import DomainValidationError, ensure_aware


class ConfigurationState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class ConfigurationVersion:
    """An immutable JSON snapshot for a versioned business configuration."""

    version_id: str
    configuration_kind: str
    version_number: int
    content_json: str
    content_hash: str
    state: ConfigurationState = ConfigurationState.DRAFT
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.version_id.strip() or not self.configuration_kind.strip():
            raise DomainValidationError("version_id and configuration_kind must not be blank")
        if self.version_number < 1:
            raise DomainValidationError("version_number must be at least one")
        try:
            json.loads(self.content_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DomainValidationError("content_json must contain valid JSON") from exc
        expected_hash = sha256(self.content_json.encode("utf-8")).hexdigest()
        if self.content_hash != expected_hash:
            raise DomainValidationError("content_hash does not match content_json")

        if self.state is ConfigurationState.DRAFT:
            if self.published_at is not None:
                raise DomainValidationError("a draft configuration cannot have published_at")
        else:
            if self.published_at is None:
                raise DomainValidationError(
                    "published and retired configurations require published_at"
                )
            ensure_aware(self.published_at, field="published_at")

    @classmethod
    def draft(
        cls,
        *,
        version_id: str,
        configuration_kind: str,
        version_number: int,
        definition: Mapping[str, Any],
    ) -> ConfigurationVersion:
        content_json = json.dumps(
            definition, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return cls(
            version_id=version_id,
            configuration_kind=configuration_kind,
            version_number=version_number,
            content_json=content_json,
            content_hash=sha256(content_json.encode("utf-8")).hexdigest(),
        )

    @property
    def definition(self) -> dict[str, Any]:
        return json.loads(self.content_json)

    def publish(self, published_at: datetime) -> ConfigurationVersion:
        if self.state is not ConfigurationState.DRAFT:
            raise DomainValidationError("only a draft configuration can be published")
        ensure_aware(published_at, field="published_at")
        return replace(self, state=ConfigurationState.PUBLISHED, published_at=published_at)

    def revise(self, definition: Mapping[str, Any]) -> ConfigurationVersion:
        if self.state is not ConfigurationState.DRAFT:
            raise DomainValidationError("published or retired configuration versions are immutable")
        return self.draft(
            version_id=self.version_id,
            configuration_kind=self.configuration_kind,
            version_number=self.version_number,
            definition=definition,
        )

    def retire(self) -> ConfigurationVersion:
        if self.state is not ConfigurationState.PUBLISHED:
            raise DomainValidationError("only a published configuration can be retired")
        return replace(self, state=ConfigurationState.RETIRED)
