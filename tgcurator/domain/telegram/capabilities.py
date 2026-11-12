from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from tgcurator.shared import DomainValidationError


class TelegramIdentityKind(StrEnum):
    MTPROTO_USER = "mtproto_user"
    BOT = "bot"


class TelegramIdentityHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


CAPABILITY_KEYS: frozenset[str] = frozenset(
    {
        "can_read_source",
        "can_download_source_media",
        "can_native_forward",
        "can_upload_media",
        "can_send_message",
        "can_access_destination",
    }
)


@dataclass(frozen=True, slots=True)
class TelegramCapabilitySet:
    """Explicit capability evaluation for a TelegramIdentity in context."""

    can_read_source: bool
    can_download_source_media: bool
    can_native_forward: bool
    can_upload_media: bool
    can_send_message: bool
    can_access_destination: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "can_read_source": self.can_read_source,
            "can_download_source_media": self.can_download_source_media,
            "can_native_forward": self.can_native_forward,
            "can_upload_media": self.can_upload_media,
            "can_send_message": self.can_send_message,
            "can_access_destination": self.can_access_destination,
            "reasons": list(self.reasons),
        }


class TelegramCapabilityResolver:
    """Resolve explicit Telegram operational capabilities without hardcoding callers to raw kind."""

    def resolve(
        self,
        *,
        identity_type: TelegramIdentityKind | str,
        enabled: bool,
        health_status: TelegramIdentityHealth | str = TelegramIdentityHealth.UNKNOWN,
        source_protected_content: bool = False,
        has_source_access: bool = True,
        has_destination_post_permission: bool = True,
        capability_overrides: Mapping[str, bool] | None = None,
    ) -> TelegramCapabilitySet:
        kind = _parse_kind(identity_type)
        health = _parse_health(health_status)
        for field_name, flag in (
            ("enabled", enabled),
            ("source_protected_content", source_protected_content),
            ("has_source_access", has_source_access),
            ("has_destination_post_permission", has_destination_post_permission),
        ):
            if not isinstance(flag, bool):
                raise DomainValidationError(f"{field_name} must be a boolean")

        overrides = _validate_overrides(capability_overrides)
        reasons: list[str] = []

        if not enabled:
            reasons.append("identity_disabled")
            return TelegramCapabilitySet(
                can_read_source=False,
                can_download_source_media=False,
                can_native_forward=False,
                can_upload_media=False,
                can_send_message=False,
                can_access_destination=False,
                reasons=tuple(reasons),
            )

        if health is TelegramIdentityHealth.FAILED:
            reasons.append("identity_failed")
            return TelegramCapabilitySet(
                can_read_source=False,
                can_download_source_media=False,
                can_native_forward=False,
                can_upload_media=False,
                can_send_message=False,
                can_access_destination=False,
                reasons=tuple(reasons),
            )

        if health is TelegramIdentityHealth.DEGRADED:
            reasons.append("identity_degraded")

        if kind is TelegramIdentityKind.MTPROTO_USER:
            defaults = {
                "can_read_source": has_source_access,
                "can_download_source_media": has_source_access and not source_protected_content,
                "can_native_forward": (
                    has_source_access
                    and has_destination_post_permission
                    and not source_protected_content
                ),
                "can_upload_media": has_destination_post_permission,
                "can_send_message": has_destination_post_permission,
                "can_access_destination": has_destination_post_permission,
            }
        else:
            reasons.append("bot_history_read_restricted")
            defaults = {
                "can_read_source": False,
                "can_download_source_media": False,
                "can_native_forward": (
                    has_source_access
                    and has_destination_post_permission
                    and not source_protected_content
                ),
                "can_upload_media": has_destination_post_permission,
                "can_send_message": has_destination_post_permission,
                "can_access_destination": has_destination_post_permission,
            }

        resolved = dict(defaults)
        for key, value in overrides.items():
            resolved[key] = value

        # Platform invariants cannot be overridden by configuration flags.
        if not has_source_access:
            resolved["can_read_source"] = False
            resolved["can_download_source_media"] = False
            resolved["can_native_forward"] = False
            reasons.append("source_access_missing")
        if source_protected_content:
            resolved["can_download_source_media"] = False
            resolved["can_native_forward"] = False
            reasons.append("source_protected_content")
        if not has_destination_post_permission:
            resolved["can_native_forward"] = False
            resolved["can_upload_media"] = False
            resolved["can_send_message"] = False
            resolved["can_access_destination"] = False
            reasons.append("destination_permission_missing")

        return TelegramCapabilitySet(
            can_read_source=resolved["can_read_source"],
            can_download_source_media=resolved["can_download_source_media"],
            can_native_forward=resolved["can_native_forward"],
            can_upload_media=resolved["can_upload_media"],
            can_send_message=resolved["can_send_message"],
            can_access_destination=resolved["can_access_destination"],
            reasons=tuple(reasons),
        )


def _parse_kind(value: TelegramIdentityKind | str) -> TelegramIdentityKind:
    if isinstance(value, TelegramIdentityKind):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        try:
            return TelegramIdentityKind(normalized)
        except ValueError as exc:
            raise DomainValidationError(f"unsupported telegram identity_type: {value}") from exc
    raise DomainValidationError("identity_type must be a string or TelegramIdentityKind")


def _parse_health(value: TelegramIdentityHealth | str) -> TelegramIdentityHealth:
    if isinstance(value, TelegramIdentityHealth):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        try:
            return TelegramIdentityHealth(normalized)
        except ValueError as exc:
            raise DomainValidationError(f"unsupported telegram health_status: {value}") from exc
    raise DomainValidationError("health_status must be a string or TelegramIdentityHealth")


def _validate_overrides(raw: Mapping[str, bool] | None) -> dict[str, bool]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DomainValidationError("capability_overrides must be a mapping")
    cleaned: dict[str, bool] = {}
    for key, value in raw.items():
        if key not in CAPABILITY_KEYS:
            raise DomainValidationError(f"unknown capability override: {key}")
        if not isinstance(value, bool):
            raise DomainValidationError(f"capability override {key} must be a boolean")
        cleaned[key] = value
    return cleaned
