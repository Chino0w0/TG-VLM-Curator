from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from tgcurator.application.analysis import AnalysisTargetInput
from tgcurator.application.ports.admin_api import (
    AdminAccountRecord,
    AdminSessionState,
    CursorPage,
    MetricsCollectionSnapshot,
)
from tgcurator.application.ports.cleanup import ArchiveCleanupFilter
from tgcurator.application.ports.operations import (
    BacklogMetricsSnapshot,
    FloodWaitMetricsSnapshot,
    OperationsSnapshot,
    PublicationMetricsSnapshot,
    ServiceHealthSnapshot,
)
from tgcurator.application.ports.routing import RoutingRequest, RoutingSnapshot
from tgcurator.application.ports.secrets import SecretStatus
from tgcurator.application.routing import RoutingService
from tgcurator.domain.analysis import (
    AnalysisStageTemplateVersion,
    LabelScope,
)
from tgcurator.domain.audit import sanitize_audit_payload
from tgcurator.domain.processing import (
    SourceChannelProfileConfig,
    WindowMessageProgress,
    compute_continuous_watermark,
)
from tgcurator.domain.review import (
    LabelValue,
    ManualLabelEvent,
    ManualLabelOperation,
    MessageReviewEvent,
    ReviewStatus,
    labels_to_routing_snapshot,
    resolve_label_namespaces,
)
from tgcurator.domain.routing import PublicationAction, RoutingPolicy, RoutingRule
from tgcurator.shared import DomainValidationError, decode_cursor, encode_cursor

from .config_validation import (
    build_inference_profile_domain,
    build_label_definition_domain,
    build_label_set_domain,
    build_pipeline_domain,
    build_prompt_template_domain,
    build_stage_template_domain,
    canonical_content_hash,
    validate_media_profile_content,
    validate_rendering_template_syntax,
    validate_routing_policy_content,
    validate_source_profile_content,
)
from .errors import AdminConflictError, AdminNotFoundError, AdminValidationError


class InMemorySecretVault:
    """Deterministic encrypted-at-rest secret store for unit and integration harnesses."""

    def __init__(
        self,
        *,
        key_id: str = "primary",
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._key_id = key_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4
        self._records: dict[UUID, dict[str, Any]] = {}

    async def store(self, *, secret_type: str, plaintext: bytes) -> UUID:
        normalized = _normalize_secret_type(secret_type)
        if not isinstance(plaintext, bytes) or not plaintext:
            raise ValueError("secret plaintext must not be empty")
        secret_id = self._id_factory()
        self._records[secret_id] = {
            "secret_id": secret_id,
            "secret_type": normalized,
            "ciphertext": bytes(b ^ 0xA5 for b in plaintext),
            "key_id": self._key_id,
            "created_at": self._clock(),
        }
        return secret_id

    async def rotate(self, *, secret_id: UUID, plaintext: bytes) -> SecretStatus:
        if secret_id not in self._records:
            raise LookupError("secret reference does not exist")
        if not isinstance(plaintext, bytes) or not plaintext:
            raise ValueError("secret plaintext must not be empty")
        rec = self._records[secret_id]
        rec["ciphertext"] = bytes(b ^ 0xA5 for b in plaintext)
        return SecretStatus(
            secret_id=rec["secret_id"],
            secret_type=rec["secret_type"],
            key_id=rec["key_id"],
            created_at=rec["created_at"],
        )

    async def resolve(
        self,
        *,
        secret_id: UUID,
        expected_secret_type: str | None = None,
    ) -> bytes:
        rec = self._records.get(secret_id)
        if rec is None:
            raise LookupError("secret reference does not exist")
        if expected_secret_type is not None and rec["secret_type"] != expected_secret_type.strip():
            raise ValueError("secret reference has an unexpected type")
        return bytes(b ^ 0xA5 for b in rec["ciphertext"])

    async def status(self, *, secret_id: UUID) -> SecretStatus | None:
        rec = self._records.get(secret_id)
        if rec is None:
            return None
        return SecretStatus(
            secret_id=rec["secret_id"],
            secret_type=rec["secret_type"],
            key_id=rec["key_id"],
            created_at=rec["created_at"],
        )

    async def list_statuses(self) -> tuple[SecretStatus, ...]:
        ordered = sorted(
            self._records.values(),
            key=lambda r: (r["created_at"], str(r["secret_id"])),
            reverse=True,
        )
        return tuple(
            SecretStatus(
                secret_id=r["secret_id"],
                secret_type=r["secret_type"],
                key_id=r["key_id"],
                created_at=r["created_at"],
            )
            for r in ordered
        )


def _normalize_secret_type(secret_type: str) -> str:
    if not isinstance(secret_type, str):
        raise TypeError("secret_type must be a string")
    normalized = secret_type.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("secret_type must be 1..64 characters")
    return normalized


class InMemoryAdminBackend:
    """Stateful reference backend implementing AdminSessionRepository and AdminWorkspaceBackend."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
        operations_snapshot_provider: Callable[[datetime], OperationsSnapshot] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4
        self._ops_provider = operations_snapshot_provider

        self.admins: dict[str, AdminAccountRecord] = {}
        self.sessions: dict[str, AdminSessionState] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.identities: dict[str, dict[str, Any]] = {}
        self.source_channels: dict[str, dict[str, Any]] = {}
        self.destination_channels: dict[str, dict[str, Any]] = {}
        self.processing_ranges: dict[str, dict[str, Any]] = {}
        self.range_executions: dict[str, dict[str, Any]] = {}
        self.config_entities: dict[str, dict[str, dict[str, Any]]] = {
            family: {}
            for family in (
                "media-processing-profiles",
                "source-channel-profiles",
                "label-definitions",
                "label-sets",
                "prompt-templates",
                "inference-profiles",
                "analysis-stages",
                "analysis-pipelines",
                "rendering-templates",
                "routing-policies",
            )
        }
        self.config_versions: dict[str, dict[str, Any]] = {}

        self.messages: dict[str, dict[str, Any]] = {}
        self.message_parts: dict[str, list[dict[str, Any]]] = {}
        self.image_assets: dict[str, dict[str, Any]] = {}
        self.video_assets: dict[str, dict[str, Any]] = {}
        self.video_frames: dict[str, list[dict[str, Any]]] = {}
        self.duplicate_matches: dict[str, dict[str, Any]] = {}
        self.analysis_runs: dict[str, dict[str, Any]] = {}
        self.stage_runs: dict[str, dict[str, Any]] = {}
        self.input_manifests: dict[str, dict[str, Any]] = {}
        self.inference_calls: dict[str, dict[str, Any]] = {}
        self.model_labels: dict[str, list[LabelValue]] = {}
        self.manual_events: dict[str, list[ManualLabelEvent]] = {}
        self.review_events: dict[str, list[MessageReviewEvent]] = {}
        self.routing_evaluations: dict[str, dict[str, Any]] = {}
        self.publication_intents: dict[str, dict[str, Any]] = {}
        self.publish_attempts: dict[str, list[dict[str, Any]]] = {}
        self.publish_records: dict[str, list[dict[str, Any]]] = {}
        self.cleanup_dry_runs: dict[str, dict[str, Any]] = {}
        self.cleanup_jobs: dict[str, dict[str, Any]] = {}
        self.wakeups: list[tuple[str, str]] = []

    def seed_admin(self, *, username: str, password_hash: str, user_id: str | None = None) -> str:
        uid = user_id or str(self._id_factory())
        self.admins[username] = AdminAccountRecord(
            user_id=uid,
            username=username,
            password_hash=password_hash,
            is_active=True,
        )
        return uid

    # --- AdminSessionRepository ---

    async def find_active_admin_by_username(self, *, username: str) -> AdminAccountRecord | None:
        account = self.admins.get(username)
        if account is None or not account.is_active:
            return None
        return account

    async def create_session(
        self,
        *,
        session_id: str,
        admin_user_id: str,
        session_token_hash: str,
        csrf_token_hash: str,
        expires_at: datetime,
        now: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AdminSessionState:
        username = next(
            (acc.username for acc in self.admins.values() if acc.user_id == admin_user_id),
            "admin",
        )
        state = AdminSessionState(
            session_id=session_id,
            admin_user_id=admin_user_id,
            username=username,
            session_token_hash=session_token_hash,
            csrf_token_hash=csrf_token_hash,
            expires_at=expires_at,
            last_seen_at=now,
            created_at=now,
            revoked_at=None,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.sessions[session_id] = state
        return state

    async def get_session_by_token_hash(
        self,
        *,
        session_token_hash: str,
    ) -> AdminSessionState | None:
        for state in self.sessions.values():
            if state.session_token_hash == session_token_hash:
                return state
        return None

    async def touch_session(self, *, session_id: str, now: datetime) -> None:
        state = self.sessions.get(session_id)
        if state is not None:
            self.sessions[session_id] = AdminSessionState(
                session_id=state.session_id,
                admin_user_id=state.admin_user_id,
                username=state.username,
                session_token_hash=state.session_token_hash,
                csrf_token_hash=state.csrf_token_hash,
                expires_at=state.expires_at,
                last_seen_at=now,
                created_at=state.created_at,
                revoked_at=state.revoked_at,
                user_agent=state.user_agent,
                ip_address=state.ip_address,
            )

    async def revoke_session(self, *, session_id: str, now: datetime) -> None:
        state = self.sessions.get(session_id)
        if state is not None:
            self.sessions[session_id] = AdminSessionState(
                session_id=state.session_id,
                admin_user_id=state.admin_user_id,
                username=state.username,
                session_token_hash=state.session_token_hash,
                csrf_token_hash=state.csrf_token_hash,
                expires_at=state.expires_at,
                last_seen_at=state.last_seen_at,
                created_at=state.created_at,
                revoked_at=now,
                user_agent=state.user_agent,
                ip_address=state.ip_address,
            )

    async def record_audit(
        self,
        *,
        actor_admin_user_id: str | None,
        event_type: str,
        entity_type: str,
        entity_id: str | None,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> str:
        event_id = str(self._id_factory())
        self.audit_events.append(
            {
                "id": event_id,
                "actor_admin_user_id": actor_admin_user_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": sanitize_audit_payload(payload),
                "created_at": now.isoformat(),
                "_ts": now,
            }
        )
        return event_id

    # --- Identities ---

    async def list_identities(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(v) for v in self.identities.values()]

    async def get_identity(self, *, identity_id: str) -> dict[str, Any] | None:
        item = self.identities.get(identity_id)
        return copy.deepcopy(item) if item is not None else None

    async def create_identity(
        self,
        *,
        name: str,
        identity_type: str,
        secret_id: str,
        enabled: bool,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_name = name.strip() if isinstance(name, str) else ""
        if not normalized_name:
            raise AdminValidationError("identity name must not be blank")
        if identity_type not in {"mtproto_user", "bot"}:
            raise AdminValidationError("identity_type must be mtproto_user or bot")
        if any(item["name"] == normalized_name for item in self.identities.values()):
            raise AdminConflictError(f"telegram identity {normalized_name!r} already exists")
        identity_id = str(self._id_factory())
        record = {
            "id": identity_id,
            "name": normalized_name,
            "identity_type": identity_type,
            "enabled": bool(enabled),
            "health_status": "unknown",
            "last_connected_at": None,
            "last_error": None,
            "secret_id": secret_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self.identities[identity_id] = record
        return copy.deepcopy(record)

    async def update_identity(
        self,
        *,
        identity_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.identities.get(identity_id)
        if record is None:
            raise AdminNotFoundError("telegram identity does not exist")
        before = copy.deepcopy(record)
        if "name" in changes and changes["name"] is not None:
            new_name = str(changes["name"]).strip()
            if not new_name:
                raise AdminValidationError("identity name must not be blank")
            if any(
                k != identity_id and item["name"] == new_name for k, item in self.identities.items()
            ):
                raise AdminConflictError(f"telegram identity {new_name!r} already exists")
            record["name"] = new_name
        if "enabled" in changes and changes["enabled"] is not None:
            record["enabled"] = bool(changes["enabled"])
        if "health_status" in changes and changes["health_status"] is not None:
            hs = str(changes["health_status"])
            if hs not in {"unknown", "healthy", "degraded", "failed"}:
                raise AdminValidationError("invalid health_status")
            record["health_status"] = hs
        if "last_error" in changes:
            record["last_error"] = (
                str(changes["last_error"]) if changes["last_error"] is not None else None
            )
        if "secret_id" in changes and changes["secret_id"] is not None:
            record["secret_id"] = str(changes["secret_id"])
        record["updated_at"] = now.isoformat()
        return before, copy.deepcopy(record)

    # --- Source & Destination Channels ---

    async def list_source_channels(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(v) for v in self.source_channels.values()]

    async def get_source_channel(self, *, channel_id: str) -> dict[str, Any] | None:
        item = self.source_channels.get(channel_id)
        return copy.deepcopy(item) if item is not None else None

    async def create_source_channel(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        peer_id = payload.get("telegram_peer_id")
        if not isinstance(peer_id, int) or isinstance(peer_id, bool) or peer_id == 0:
            raise AdminValidationError("telegram_peer_id must be a non-zero integer")
        if any(item["telegram_peer_id"] == peer_id for item in self.source_channels.values()):
            raise AdminConflictError(f"source channel peer {peer_id} already exists")
        display_name = str(payload.get("display_name", "")).strip()
        if not display_name:
            raise AdminValidationError("display_name must not be blank")
        read_identity_id = str(payload.get("default_read_identity_id", "")).strip()
        if read_identity_id not in self.identities:
            raise AdminValidationError("default_read_identity_id does not exist")
        active_profile_vid = payload.get("active_profile_version_id")
        if active_profile_vid is not None:
            ver = self.config_versions.get(str(active_profile_vid))
            if (
                ver is None
                or ver["family"] != "source-channel-profiles"
                or ver["state"] != "published"
            ):
                raise AdminValidationError("active_profile_version_id must be a published profile")
        channel_id = str(self._id_factory())
        record = {
            "id": channel_id,
            "telegram_peer_id": peer_id,
            "username": payload.get("username"),
            "display_name": display_name,
            "default_read_identity_id": read_identity_id,
            "active_profile_version_id": (
                str(active_profile_vid) if active_profile_vid is not None else None
            ),
            "enabled": bool(payload.get("enabled", True)),
            "last_activity_at": None,
            "latest_seen_message_id": None,
            "last_seen_message_id": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self.source_channels[channel_id] = record
        return copy.deepcopy(record)

    async def update_source_channel(
        self,
        *,
        channel_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.source_channels.get(channel_id)
        if record is None:
            raise AdminNotFoundError("source channel does not exist")
        before = copy.deepcopy(record)
        if "display_name" in changes and changes["display_name"] is not None:
            dn = str(changes["display_name"]).strip()
            if not dn:
                raise AdminValidationError("display_name must not be blank")
            record["display_name"] = dn
        if "username" in changes:
            record["username"] = changes["username"]
        if (
            "default_read_identity_id" in changes
            and changes["default_read_identity_id"] is not None
        ):
            rid = str(changes["default_read_identity_id"])
            if rid not in self.identities:
                raise AdminValidationError("default_read_identity_id does not exist")
            record["default_read_identity_id"] = rid
        if "active_profile_version_id" in changes:
            pvid = changes["active_profile_version_id"]
            if pvid is not None:
                ver = self.config_versions.get(str(pvid))
                if (
                    ver is None
                    or ver["family"] != "source-channel-profiles"
                    or ver["state"] != "published"
                ):
                    raise AdminValidationError(
                        "active_profile_version_id must be a published profile"
                    )
                record["active_profile_version_id"] = str(pvid)
            else:
                record["active_profile_version_id"] = None
        if "enabled" in changes and changes["enabled"] is not None:
            record["enabled"] = bool(changes["enabled"])
        record["updated_at"] = now.isoformat()
        return before, copy.deepcopy(record)

    async def list_destination_channels(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(v) for v in self.destination_channels.values()]

    async def get_destination_channel(self, *, channel_id: str) -> dict[str, Any] | None:
        item = self.destination_channels.get(channel_id)
        return copy.deepcopy(item) if item is not None else None

    async def create_destination_channel(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        peer_id = payload.get("telegram_peer_id")
        if not isinstance(peer_id, int) or isinstance(peer_id, bool) or peer_id == 0:
            raise AdminValidationError("telegram_peer_id must be a non-zero integer")
        if any(item["telegram_peer_id"] == peer_id for item in self.destination_channels.values()):
            raise AdminConflictError(f"destination channel peer {peer_id} already exists")
        name = str(payload.get("name", "")).strip()
        if not name:
            raise AdminValidationError("destination channel name must not be blank")
        pub_identity_id = str(payload.get("default_publish_identity_id", "")).strip()
        if pub_identity_id not in self.identities:
            raise AdminValidationError("default_publish_identity_id does not exist")
        channel_id = str(self._id_factory())
        record = {
            "id": channel_id,
            "name": name,
            "telegram_peer_id": peer_id,
            "username": payload.get("username"),
            "default_publish_identity_id": pub_identity_id,
            "enabled": bool(payload.get("enabled", True)),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self.destination_channels[channel_id] = record
        return copy.deepcopy(record)

    async def update_destination_channel(
        self,
        *,
        channel_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.destination_channels.get(channel_id)
        if record is None:
            raise AdminNotFoundError("destination channel does not exist")
        before = copy.deepcopy(record)
        if "name" in changes and changes["name"] is not None:
            name = str(changes["name"]).strip()
            if not name:
                raise AdminValidationError("destination channel name must not be blank")
            record["name"] = name
        if "username" in changes:
            record["username"] = changes["username"]
        if (
            "default_publish_identity_id" in changes
            and changes["default_publish_identity_id"] is not None
        ):
            pid = str(changes["default_publish_identity_id"])
            if pid not in self.identities:
                raise AdminValidationError("default_publish_identity_id does not exist")
            record["default_publish_identity_id"] = pid
        if "enabled" in changes and changes["enabled"] is not None:
            record["enabled"] = bool(changes["enabled"])
        record["updated_at"] = now.isoformat()
        return before, copy.deepcopy(record)

    # --- Processing Ranges ---

    async def list_processing_ranges(
        self,
        *,
        source_channel_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        results = []
        for item in self.processing_ranges.values():
            if source_channel_id is not None and item["source_channel_id"] != source_channel_id:
                continue
            if status is not None and item["status"] != status:
                continue
            results.append(copy.deepcopy(item))
        return results

    async def get_processing_range(self, *, range_id: str) -> dict[str, Any] | None:
        item = self.processing_ranges.get(range_id)
        return copy.deepcopy(item) if item is not None else None

    async def create_processing_range(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        source_id = str(payload.get("source_channel_id", "")).strip()
        if source_id not in self.source_channels:
            raise AdminValidationError("source_channel_id does not exist")
        start_at = _parse_iso_dt(payload.get("start_at"), field="start_at")
        end_mode = str(payload.get("end_mode", "")).strip().lower()
        if end_mode not in {"fixed", "latest"}:
            raise AdminValidationError("end_mode must be fixed or latest")
        end_at: datetime | None = None
        steady_after: int | None = None
        if end_mode == "fixed":
            end_at = _parse_iso_dt(payload.get("end_at"), field="end_at")
            if end_at <= start_at:
                raise AdminValidationError("end_at must be greater than start_at")
            if payload.get("steady_after_seconds") is not None:
                raise AdminValidationError("fixed range cannot set steady_after_seconds")
        else:
            if payload.get("end_at") is not None:
                raise AdminValidationError("latest range cannot set end_at")
            raw_steady = payload.get("steady_after_seconds", 60)
            if not isinstance(raw_steady, int) or isinstance(raw_steady, bool) or raw_steady <= 0:
                raise AdminValidationError("steady_after_seconds must be a positive integer")
            steady_after = raw_steady

        resolved_start = payload.get("resolved_start_message_id")
        resolved_end = payload.get("resolved_fixed_end_message_id")
        watermark = (
            resolved_start - 1 if isinstance(resolved_start, int) and resolved_start > 0 else None
        )
        enabled = bool(payload.get("enabled", True))
        range_id = str(self._id_factory())
        record = {
            "id": range_id,
            "source_channel_id": source_id,
            "start_at": start_at.isoformat(),
            "end_mode": end_mode,
            "end_at": end_at.isoformat() if end_at is not None else None,
            "steady_after_seconds": steady_after,
            "resolved_start_message_id": resolved_start,
            "resolved_fixed_end_message_id": resolved_end,
            "processing_watermark_message_id": watermark,
            "active_high_watermark_message_id": None,
            "status": "active" if enabled else "paused",
            "enabled": enabled,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self.processing_ranges[range_id] = record
        return copy.deepcopy(record)

    async def update_processing_range(
        self,
        *,
        range_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.processing_ranges.get(range_id)
        if record is None:
            raise AdminNotFoundError("processing range does not exist")
        before = copy.deepcopy(record)
        if "enabled" in changes and changes["enabled"] is not None:
            enabled = bool(changes["enabled"])
            record["enabled"] = enabled
            if not enabled and record["status"] in {"pending", "active"}:
                record["status"] = "paused"
            elif enabled and record["status"] == "paused":
                record["status"] = "active"
        if "status" in changes and changes["status"] is not None:
            st = str(changes["status"])
            if st not in {"pending", "active", "paused", "completed", "failed"}:
                raise AdminValidationError("invalid processing range status")
            record["status"] = st
            record["enabled"] = st in {"pending", "active"}
        record["updated_at"] = now.isoformat()
        return before, copy.deepcopy(record)

    async def list_range_executions(self, *, range_id: str) -> list[dict[str, Any]]:
        items = [
            copy.deepcopy(ex)
            for ex in self.range_executions.values()
            if ex["processing_range_id"] == range_id
        ]
        items.sort(key=lambda item: item["from_message_id_exclusive"], reverse=True)
        return items

    # --- Versioned Configurations (all 10 families) ---

    async def list_config_entities(self, *, family: str) -> list[dict[str, Any]]:
        bucket = self.config_entities[family]
        return [self._serialize_config_entity(family, ent["id"]) for ent in bucket.values()]

    async def get_config_entity(
        self,
        *,
        family: str,
        entity_id: str,
    ) -> dict[str, Any] | None:
        if entity_id not in self.config_entities[family]:
            return None
        return self._serialize_config_entity(family, entity_id)

    async def create_config_entity(
        self,
        *,
        family: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise AdminValidationError("configuration entity name must not be blank")
        bucket = self.config_entities[family]
        if family == "source-channel-profiles":
            sc_id = str(payload.get("source_channel_id", "")).strip()
            if sc_id not in self.source_channels:
                raise AdminValidationError("source_channel_id does not exist")
            if any(ent.get("source_channel_id") == sc_id for ent in bucket.values()):
                raise AdminConflictError("source channel already has a profile")
        else:
            if any(ent["name"] == name for ent in bucket.values()):
                raise AdminConflictError(f"{family} {name!r} already exists")

        entity_id = str(self._id_factory())
        ent: dict[str, Any] = {
            "id": entity_id,
            "family": family,
            "name": name,
            "source_channel_id": payload.get("source_channel_id"),
            "version_ids": [],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        bucket[entity_id] = ent

        initial_content = payload.get("initial_version")
        if isinstance(initial_content, Mapping):
            await self.create_config_version(
                family=family,
                entity_id=entity_id,
                content=initial_content,
                now=now,
            )
        return self._serialize_config_entity(family, entity_id)

    async def create_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        content: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        ent = self.config_entities[family].get(entity_id)
        if ent is None:
            raise AdminNotFoundError(f"{family} entity does not exist")
        if not isinstance(content, Mapping):
            raise AdminValidationError("version content must be a JSON object")

        version_id = str(self._id_factory())
        version_number = len(ent["version_ids"]) + 1
        normalized_content = copy.deepcopy(dict(content))
        self._validate_family_version(
            family=family,
            version_id=version_id,
            entity_id=entity_id,
            version_number=version_number,
            state="draft",
            content=normalized_content,
            require_published_refs=False,
        )
        version_record = {
            "id": version_id,
            "family": family,
            "entity_id": entity_id,
            "version_number": version_number,
            "state": "draft",
            "content": normalized_content,
            "content_hash": canonical_content_hash(normalized_content),
            "published_at": None,
            "archived_at": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self.config_versions[version_id] = version_record
        ent["version_ids"].append(version_id)
        ent["updated_at"] = now.isoformat()
        return copy.deepcopy(version_record)

    async def update_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        content: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        ver = self._require_version(family=family, entity_id=entity_id, version_id=version_id)
        if ver["state"] != "draft":
            raise AdminConflictError("published or archived configuration versions are immutable")
        normalized_content = copy.deepcopy(dict(content))
        self._validate_family_version(
            family=family,
            version_id=version_id,
            entity_id=entity_id,
            version_number=ver["version_number"],
            state="draft",
            content=normalized_content,
            require_published_refs=False,
        )
        ver["content"] = normalized_content
        ver["content_hash"] = canonical_content_hash(normalized_content)
        ver["updated_at"] = now.isoformat()
        return copy.deepcopy(ver)

    async def publish_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        supersede_published: bool,
        now: datetime,
    ) -> dict[str, Any]:
        ver = self._require_version(family=family, entity_id=entity_id, version_id=version_id)
        if ver["state"] != "draft":
            raise AdminConflictError("only a draft configuration version can be published")
        self._validate_family_version(
            family=family,
            version_id=version_id,
            entity_id=entity_id,
            version_number=ver["version_number"],
            state="published",
            content=ver["content"],
            require_published_refs=True,
        )
        ent = self.config_entities[family][entity_id]
        archive_state = "retired" if family == "source-channel-profiles" else "archived"
        for sibling_id in ent["version_ids"]:
            sibling = self.config_versions[sibling_id]
            if sibling["id"] != version_id and sibling["state"] == "published":
                if not supersede_published:
                    raise AdminConflictError("another version is already published")
                sibling["state"] = archive_state
                sibling["archived_at"] = now.isoformat()
                sibling["updated_at"] = now.isoformat()

        ver["state"] = "published"
        ver["published_at"] = now.isoformat()
        ver["updated_at"] = now.isoformat()
        return copy.deepcopy(ver)

    async def archive_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        ver = self._require_version(family=family, entity_id=entity_id, version_id=version_id)
        if ver["state"] != "published":
            raise AdminConflictError("only a published configuration version can be archived")
        ver["state"] = "retired" if family == "source-channel-profiles" else "archived"
        ver["archived_at"] = now.isoformat()
        ver["updated_at"] = now.isoformat()
        return copy.deepcopy(ver)

    # --- Prompt Test Bench Preparation ---

    async def prepare_test_bench_inputs(
        self,
        *,
        payload: Mapping[str, Any],
    ) -> tuple[
        AnalysisStageTemplateVersion,
        tuple[AnalysisTargetInput, ...],
        Mapping[str, Any] | None,
    ]:
        stage_vid = payload.get("stage_template_version_id") or payload.get("stage_version_id")
        if isinstance(stage_vid, str) and stage_vid.strip():
            stage = self._resolve_stage_domain(stage_vid.strip())
        else:
            inline_stage = payload.get("stage")
            if not isinstance(inline_stage, Mapping):
                raise AdminValidationError(
                    "provide stage_template_version_id or inline stage specification"
                )
            stage = build_stage_template_domain(
                version_id=str(self._id_factory()),
                entity_id=str(self._id_factory()),
                version_number=1,
                state="draft",
                content=inline_stage,
                resolve_prompt=self._resolve_prompt_domain,
                resolve_label_set=self._resolve_label_set_domain,
                resolve_inference_profile=self._resolve_inference_profile_domain,
                require_published_refs=False,
            )

        message_id = payload.get("message_id")
        targets: list[AnalysisTargetInput] = []
        source_ctx: Mapping[str, Any] | None = None
        if isinstance(message_id, str) and message_id.strip():
            msg = self.messages.get(message_id.strip())
            if msg is None:
                raise AdminNotFoundError("message does not exist")
            sc = self.source_channels.get(msg["source_channel_id"])
            if sc is not None:
                source_ctx = {
                    "source_channel_id": sc["id"],
                    "display_name": sc["display_name"],
                    "username": sc.get("username"),
                }
            if stage.target_scope is LabelScope.GLOBAL:
                media_items = self._collect_message_media_descriptors(msg["id"])
                targets.append(
                    AnalysisTargetInput(
                        target_id=msg["id"],
                        message_id=msg["id"],
                        text=msg.get("original_text"),
                        telegram_metadata=msg.get("telegram_metadata") or {},
                        media=tuple(media_items),
                    )
                )
            else:
                asset_id = payload.get("asset_id")
                media_items = self._collect_message_media_descriptors(msg["id"])
                if asset_id is not None:
                    media_items = [m for m in media_items if m["asset_id"] == str(asset_id)]
                if not media_items:
                    raise AdminValidationError("no matching media assets available on message")
                for m in media_items:
                    targets.append(
                        AnalysisTargetInput(
                            target_id=str(m["asset_id"]),
                            message_id=msg["id"],
                            asset_id=str(m["asset_id"]),
                            text=msg.get("original_text"),
                            telegram_metadata=msg.get("telegram_metadata") or {},
                            media=(m,),
                        )
                    )
        else:
            sample_text = str(payload.get("sample_text", ""))
            sample_id = str(payload.get("sample_target_id") or self._id_factory())
            sample_media = payload.get("sample_media") or []
            targets.append(
                AnalysisTargetInput(
                    target_id=sample_id,
                    message_id=sample_id,
                    asset_id=sample_id if stage.target_scope is LabelScope.MEDIA else None,
                    text=sample_text,
                    telegram_metadata={},
                    media=tuple(sample_media) if isinstance(sample_media, list) else (),
                )
            )
        return stage, tuple(targets), source_ctx

    # --- Messages, Review, Reanalysis, Reroute & Skip ---

    async def list_messages(
        self,
        *,
        filters: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]:
        token = decode_cursor(cursor) if cursor else None
        matched: list[dict[str, Any]] = []
        for msg in self.messages.values():
            if not self._matches_message_filters(msg, filters):
                continue
            matched.append(msg)

        matched.sort(
            key=lambda m: (_parse_iso_dt(m["published_at"], field="published_at"), m["id"]),
            reverse=True,
        )
        if token is not None:
            matched = [
                m
                for m in matched
                if (_parse_iso_dt(m["published_at"], field="published_at"), m["id"])
                < (token.timestamp, token.entity_id)
            ]

        window = matched[: limit + 1]
        has_more = len(window) > limit
        items = [self._serialize_message_summary(m) for m in window[:limit]]
        next_cursor = None
        if has_more and items:
            last = window[limit - 1]
            next_cursor = encode_cursor(
                timestamp=_parse_iso_dt(last["published_at"], field="published_at"),
                entity_id=last["id"],
            )
        return CursorPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    async def get_message_detail(self, *, message_id: str) -> dict[str, Any] | None:
        msg = self.messages.get(message_id)
        if msg is None:
            return None
        resolved = resolve_label_namespaces(
            tuple(self.model_labels.get(message_id, ())),
            tuple(self.manual_events.get(message_id, ())),
        )
        label_snapshot = labels_to_routing_snapshot(resolved)

        runs = [
            copy.deepcopy(run)
            for run in self.analysis_runs.values()
            if run["message_id"] == message_id
        ]
        for run in runs:
            sruns = [
                copy.deepcopy(sr)
                for sr in self.stage_runs.values()
                if sr["analysis_run_id"] == run["id"]
            ]
            for sr in sruns:
                sr["input_manifest"] = copy.deepcopy(
                    self.input_manifests.get(sr.get("input_manifest_id") or "")
                )
                sr["inference_call"] = copy.deepcopy(
                    self.inference_calls.get(sr.get("inference_call_id") or "")
                )
            run["stage_runs"] = sruns

        intents = [
            copy.deepcopy(pi)
            for pi in self.publication_intents.values()
            if pi["message_id"] == message_id
        ]
        for pi in intents:
            pi["attempts"] = copy.deepcopy(self.publish_attempts.get(pi["id"], []))
            pi["records"] = copy.deepcopy(self.publish_records.get(pi["id"], []))

        images = [
            copy.deepcopy(img)
            for img in self.image_assets.values()
            if img["message_id"] == message_id
        ]
        videos = []
        for vid in self.video_assets.values():
            if vid["message_id"] == message_id:
                vc = copy.deepcopy(vid)
                vc["frames"] = copy.deepcopy(self.video_frames.get(vid["id"], []))
                videos.append(vc)

        audits = [
            {k: v for k, v in ev.items() if k != "_ts"}
            for ev in self.audit_events
            if ev.get("entity_id") == message_id
        ]

        return {
            "message": copy.deepcopy(msg),
            "parts": copy.deepcopy(self.message_parts.get(message_id, [])),
            "media": {"images": images, "videos": videos},
            "duplicate_match": copy.deepcopy(self.duplicate_matches.get(message_id)),
            "analysis_runs": runs,
            "labels": {
                "model": [
                    {
                        "target_scope": item.target_scope,
                        "target_kind": item.target_kind,
                        "target_id": item.target_id,
                        "image_asset_id": item.image_asset_id,
                        "video_asset_id": item.video_asset_id,
                        "label_definition_version_id": item.label_definition_version_id,
                        "label_key": item.label_key,
                        "score": item.score,
                        "activated": item.activated,
                    }
                    for item in self.model_labels.get(message_id, ())
                ],
                "manual_events": [
                    self._serialize_manual_event(ev)
                    for ev in self.manual_events.get(message_id, ())
                ],
                "namespaces": label_snapshot,
            },
            "review_events": [
                {
                    "event_id": ev.event_id,
                    "message_id": ev.message_id,
                    "old_status": ev.old_status.value,
                    "new_status": ev.new_status.value,
                    "actor_admin_user_id": ev.actor_admin_user_id,
                    "reason": ev.reason,
                    "created_at": ev.created_at.isoformat(),
                }
                for ev in self.review_events.get(message_id, ())
            ],
            "routing_evaluations": [
                copy.deepcopy(ev)
                for ev in self.routing_evaluations.values()
                if ev["message_id"] == message_id
            ],
            "publication_intents": intents,
            "audit_events": audits,
        }

    async def apply_manual_label(
        self,
        *,
        message_id: str,
        payload: Mapping[str, Any],
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        if message_id not in self.messages:
            raise AdminNotFoundError("message does not exist")
        op_raw = str(payload.get("operation", "set")).strip().lower()
        try:
            operation = ManualLabelOperation(op_raw)
            event = ManualLabelEvent(
                event_id=str(self._id_factory()),
                message_id=message_id,
                target_scope=str(payload.get("target_scope", "global")),
                target_kind=str(payload.get("target_kind", "message")),
                target_id=str(payload.get("target_id", message_id)),
                image_asset_id=(
                    str(payload["image_asset_id"])
                    if payload.get("image_asset_id") is not None
                    else None
                ),
                video_asset_id=(
                    str(payload["video_asset_id"])
                    if payload.get("video_asset_id") is not None
                    else None
                ),
                label_definition_version_id=str(payload.get("label_definition_version_id", "")),
                label_key=str(payload.get("label_key", "")),
                operation=operation,
                score=(
                    float(payload["score"])
                    if operation is ManualLabelOperation.SET and payload.get("score") is not None
                    else None
                ),
                activated=(
                    bool(payload["activated"])
                    if operation is ManualLabelOperation.SET
                    and payload.get("activated") is not None
                    else None
                ),
                actor_admin_user_id=actor_admin_user_id,
                created_at=now,
                reason=str(payload["reason"]) if payload.get("reason") is not None else None,
            )
        except (DomainValidationError, ValueError, TypeError) as exc:
            raise AdminValidationError(str(exc)) from exc

        self.manual_events.setdefault(message_id, []).append(event)
        return self._serialize_manual_event(event)

    async def transition_review_status(
        self,
        *,
        message_id: str,
        new_status: str,
        reason: str | None,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        msg = self.messages.get(message_id)
        if msg is None:
            raise AdminNotFoundError("message does not exist")
        try:
            target_status = ReviewStatus(new_status.strip().lower())
            old_status = ReviewStatus(msg["review_status"])
            event = MessageReviewEvent(
                event_id=str(self._id_factory()),
                message_id=message_id,
                old_status=old_status,
                new_status=target_status,
                actor_admin_user_id=actor_admin_user_id,
                created_at=now,
                reason=reason,
            )
        except (DomainValidationError, ValueError) as exc:
            raise AdminValidationError(str(exc)) from exc

        msg["review_status"] = target_status.value
        msg["updated_at"] = now.isoformat()
        self.review_events.setdefault(message_id, []).append(event)
        return {
            "event_id": event.event_id,
            "message_id": message_id,
            "old_status": event.old_status.value,
            "new_status": event.new_status.value,
            "actor_admin_user_id": actor_admin_user_id,
            "reason": reason,
            "created_at": now.isoformat(),
        }

    async def reanalyze_message(
        self,
        *,
        message_id: str,
        pipeline_version_id: str | None,
        reason: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        msg = self.messages.get(message_id)
        if msg is None:
            raise AdminNotFoundError("message does not exist")
        resolved_pipe_vid = pipeline_version_id
        if resolved_pipe_vid is None:
            sc = self.source_channels.get(msg["source_channel_id"], {})
            prof_vid = sc.get("active_profile_version_id")
            if prof_vid and prof_vid in self.config_versions:
                cfg = SourceChannelProfileConfig.from_content(
                    self.config_versions[prof_vid]["content"]
                )
                resolved_pipe_vid = cfg.analysis_pipeline_version_id
        if not resolved_pipe_vid or resolved_pipe_vid not in self.config_versions:
            raise AdminValidationError("published analysis_pipeline_version_id is required")
        pipe_ver = self.config_versions[resolved_pipe_vid]
        if pipe_ver["family"] != "analysis-pipelines" or pipe_ver["state"] != "published":
            raise AdminValidationError("analysis_pipeline_version_id must be published")

        run_id = str(self._id_factory())
        self.analysis_runs[run_id] = {
            "id": run_id,
            "message_id": message_id,
            "analysis_pipeline_version_id": resolved_pipe_vid,
            "run_mode": "reanalysis",
            "status": "running",
            "facts_snapshot": {},
            "started_at": now.isoformat(),
            "completed_at": None,
            "created_at": now.isoformat(),
        }
        created_stage_runs: list[str] = []
        for node in pipe_ver["content"].get("nodes", []):
            if not node.get("depends_on"):
                sr_id = str(self._id_factory())
                self.stage_runs[sr_id] = {
                    "id": sr_id,
                    "analysis_run_id": run_id,
                    "analysis_stage_template_version_id": node[
                        "analysis_stage_template_version_id"
                    ],
                    "message_id": message_id,
                    "target_scope": "global",
                    "target_kind": "message",
                    "target_id": message_id,
                    "status": "pending",
                    "attempt_count": 0,
                    "max_attempts": 3,
                    "created_at": now.isoformat(),
                }
                created_stage_runs.append(sr_id)
                self.wakeups.append(("analysis", sr_id))

        return {
            "analysis_run_id": run_id,
            "message_id": message_id,
            "analysis_pipeline_version_id": resolved_pipe_vid,
            "run_mode": "reanalysis",
            "stage_run_ids": created_stage_runs,
            "reason": reason,
        }

    async def reroute_message(
        self,
        *,
        message_id: str,
        routing_policy_version_id: str | None,
        dry_run: bool,
        reason: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        msg = self.messages.get(message_id)
        if msg is None:
            raise AdminNotFoundError("message does not exist")
        policy_vid = routing_policy_version_id
        if policy_vid is None:
            sc = self.source_channels.get(msg["source_channel_id"], {})
            prof_vid = sc.get("active_profile_version_id")
            if prof_vid and prof_vid in self.config_versions:
                cfg = SourceChannelProfileConfig.from_content(
                    self.config_versions[prof_vid]["content"]
                )
                policy_vid = cfg.routing_policy_version_id
        if not policy_vid or policy_vid not in self.config_versions:
            raise AdminValidationError("published routing_policy_version_id is required")
        policy_ver = self.config_versions[policy_vid]
        if policy_ver["family"] != "routing-policies" or policy_ver["state"] != "published":
            raise AdminValidationError("routing_policy_version_id must be published")

        adapter = _InMemoryRoutingRepositoryAdapter(self)
        routing_service = RoutingService(adapter, id_factory=self._id_factory)
        request = RoutingRequest(
            request_id=f"reroute:{self._id_factory()}",
            message_id=message_id,
            routing_policy_version_id=policy_vid,
            dry_run=dry_run,
        )
        try:
            exec_result = await routing_service.evaluate(request)
        except DomainValidationError as exc:
            raise AdminValidationError(str(exc)) from exc

        prev_evals = [
            ev for ev in self.routing_evaluations.values() if ev["message_id"] == message_id
        ]
        # Exclude the newly persisted evaluation when dry_run=False
        prior_evals = [ev for ev in prev_evals if ev["id"] != exec_result.evaluation_id]
        previous_eval = prior_evals[-1] if prior_evals else None
        prev_intents = (
            [
                pi
                for pi in self.publication_intents.values()
                if pi["routing_evaluation_id"] == previous_eval["id"]
            ]
            if previous_eval is not None
            else []
        )
        prev_dests = {pi["destination_channel_id"]: pi["publication_mode"] for pi in prev_intents}
        new_dests = {
            intent.destination_channel_id: intent.publication_mode for intent in exec_result.intents
        }
        comparison = {
            "previous_evaluation_id": previous_eval["id"] if previous_eval else None,
            "added_destinations": sorted(set(new_dests) - set(prev_dests)),
            "removed_destinations": sorted(set(prev_dests) - set(new_dests)),
            "changed_modes": {
                dest: {"before": prev_dests[dest], "after": new_dests[dest]}
                for dest in sorted(set(prev_dests) & set(new_dests))
                if prev_dests[dest] != new_dests[dest]
            },
        }
        return {
            "evaluation_id": exec_result.evaluation_id,
            "routing_request_id": exec_result.routing_request_id,
            "message_id": message_id,
            "routing_policy_version_id": policy_vid,
            "dry_run": dry_run,
            "persisted": exec_result.persisted,
            "facts_hash": exec_result.facts_hash,
            "stopped_at_rule_id": exec_result.decision.stopped_at_rule_id,
            "matched_rule_ids": [o.rule_id for o in exec_result.decision.outcomes if o.matched],
            "intents": [
                {
                    "intent_id": i.intent_id,
                    "destination_channel_id": i.destination_channel_id,
                    "publish_identity_id": i.publish_identity_id,
                    "publication_mode": i.publication_mode,
                    "rendered_text": i.rendered_text,
                }
                for i in exec_result.intents
            ],
            "comparison": comparison,
        }

    async def skip_message(
        self,
        *,
        message_id: str,
        reason: str,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        msg = self.messages.get(message_id)
        if msg is None:
            raise AdminNotFoundError("message does not exist")
        msg["processing_status"] = "processed"
        msg["terminal_reason"] = "admin_skipped"
        msg["failure_stage"] = None
        msg["last_error_code"] = None
        msg["updated_at"] = now.isoformat()

        settled_executions: list[dict[str, Any]] = []
        source_id = msg["source_channel_id"]
        primary_id = int(msg["primary_telegram_message_id"])
        for ex in self.range_executions.values():
            rng = self.processing_ranges.get(ex["processing_range_id"])
            if rng is None or rng["source_channel_id"] != source_id:
                continue
            if not (ex["from_message_id_exclusive"] < primary_id <= ex["to_message_id_inclusive"]):
                continue
            window_msgs = [
                WindowMessageProgress(
                    primary_telegram_message_id=int(m["primary_telegram_message_id"]),
                    processing_status=m["processing_status"],
                    terminal_reason=m.get("terminal_reason"),
                )
                for m in self.messages.values()
                if m["source_channel_id"] == source_id
                and ex["from_message_id_exclusive"]
                < int(m["primary_telegram_message_id"])
                <= ex["to_message_id_inclusive"]
            ]
            adv = compute_continuous_watermark(
                from_message_id_exclusive=ex["from_message_id_exclusive"],
                to_message_id_inclusive=ex["to_message_id_inclusive"],
                current_watermark_message_id=ex["watermark_message_id"],
                history_ingested=bool(ex.get("history_ingested", True)),
                messages=window_msgs,
            )
            ex["watermark_message_id"] = adv.next_watermark_message_id
            if adv.window_completed and ex["status"] in {"awaiting_pipeline", "running", "failed"}:
                ex["status"] = "completed"
                ex["completed_at"] = now.isoformat()
            current_rw = rng.get("processing_watermark_message_id")
            if current_rw is None or adv.next_watermark_message_id > current_rw:
                rng["processing_watermark_message_id"] = adv.next_watermark_message_id
            settled_executions.append(
                {
                    "execution_id": ex["id"],
                    "new_watermark_message_id": adv.next_watermark_message_id,
                    "window_completed": adv.window_completed,
                }
            )

        return {
            "message_id": message_id,
            "processing_status": "processed",
            "terminal_reason": "admin_skipped",
            "reason": reason,
            "actor_admin_user_id": actor_admin_user_id,
            "settled_executions": settled_executions,
        }

    async def retry_step(
        self,
        *,
        step_kind: str,
        target_id: str,
        reason: str | None,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        store_by_kind: dict[str, tuple[dict[str, dict[str, Any]], str, str]] = {
            "range_execution": (self.range_executions, "range_execution", "status"),
            "image_archive": (self.image_assets, "image_archive", "archive_state"),
            "video_archive": (self.video_assets, "video_archive", "archive_state"),
            "stage_run": (self.stage_runs, "analysis", "status"),
            "publication_intent": (self.publication_intents, "publication", "status"),
            "cleanup_job": (self.cleanup_jobs, "maintenance", "status"),
        }
        if step_kind not in store_by_kind:
            raise AdminValidationError(f"unsupported step_kind: {step_kind}")
        bucket, queue_name, status_key = store_by_kind[step_kind]
        item = bucket.get(target_id)
        if item is None:
            raise AdminNotFoundError(f"{step_kind} {target_id} does not exist")
        old_state = item.get(status_key)
        item[status_key] = "pending"
        if "attempt_count" in item:
            item["attempt_count"] = 0
        if "archive_attempt_count" in item:
            item["archive_attempt_count"] = 0
        item["lease_token"] = None
        item["lease_expires_at"] = None
        item["next_retry_at"] = None
        item["updated_at"] = now.isoformat()
        msg_id = item.get("message_id")
        if isinstance(msg_id, str) and msg_id in self.messages:
            parent_msg = self.messages[msg_id]
            if parent_msg.get("processing_status") == "failed":
                parent_msg["processing_status"] = "processing"
                parent_msg["failure_stage"] = None
                parent_msg["last_error_code"] = None
        self.wakeups.append((queue_name, target_id))
        return {
            "step_kind": step_kind,
            "target_id": target_id,
            "previous_status": old_state,
            "status": "pending",
            "enqueued_queue": queue_name,
        }

    # --- Archive Cleanup ---

    async def create_cleanup_dry_run(
        self,
        *,
        filter_payload: Mapping[str, Any],
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        cleanup_filter = _build_cleanup_filter(filter_payload)
        if not cleanup_filter.has_meaningful_filter:
            raise AdminValidationError("archive cleanup filter must specify at least one criterion")

        matched_assets = 0
        matched_messages: set[str] = set()
        estimated_bytes = 0
        for img in self.image_assets.values():
            if img.get("archive_state") == "ready" and self._matches_cleanup_asset(
                img, "image", cleanup_filter
            ):
                matched_assets += 1
                matched_messages.add(img["message_id"])
                estimated_bytes += int(img.get("archive_size_bytes") or 0)
        for vid in self.video_assets.values():
            if vid.get("archive_state") == "ready" and self._matches_cleanup_asset(
                vid, "video", cleanup_filter
            ):
                matched_assets += 1
                matched_messages.add(vid["message_id"])
                for frame in self.video_frames.get(vid["id"], []):
                    estimated_bytes += int(frame.get("archive_size_bytes") or 0)

        dry_run_id = str(self._id_factory())
        snapshot = cleanup_filter.snapshot()
        fhash = cleanup_filter.sha256
        record = {
            "id": dry_run_id,
            "actor_admin_user_id": actor_admin_user_id,
            "filter_snapshot": snapshot,
            "filter_hash": fhash,
            "estimated_asset_count": matched_assets,
            "estimated_message_count": len(matched_messages),
            "estimated_bytes": estimated_bytes,
            "created_at": now.isoformat(),
        }
        self.cleanup_dry_runs[dry_run_id] = record
        await self.record_audit(
            actor_admin_user_id=actor_admin_user_id,
            event_type="archive_cleanup.dry_run_created",
            entity_type="archive_cleanup_dry_run",
            entity_id=dry_run_id,
            payload={
                "filter_hash": fhash,
                "estimated_asset_count": matched_assets,
                "estimated_message_count": len(matched_messages),
                "estimated_bytes": estimated_bytes,
            },
            now=now,
        )
        return copy.deepcopy(record)

    async def list_cleanup_dry_runs(self, *, limit: int) -> list[dict[str, Any]]:
        items = list(self.cleanup_dry_runs.values())
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return [copy.deepcopy(r) for r in items[:limit]]

    async def get_cleanup_dry_run(self, *, dry_run_id: str) -> dict[str, Any] | None:
        item = self.cleanup_dry_runs.get(dry_run_id)
        return copy.deepcopy(item) if item is not None else None

    async def confirm_cleanup_job(
        self,
        *,
        dry_run_id: str,
        expected_filter_hash: str,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        dry_run = self.cleanup_dry_runs.get(dry_run_id)
        if dry_run is None:
            raise AdminNotFoundError("archive cleanup dry run does not exist")
        if dry_run["filter_hash"] != expected_filter_hash:
            raise AdminConflictError("expected_filter_hash does not match dry run filter_hash")
        if any(j["dry_run_id"] == dry_run_id for j in self.cleanup_jobs.values()):
            raise AdminConflictError("archive cleanup dry run has already been confirmed")

        job_id = str(self._id_factory())
        job = {
            "id": job_id,
            "dry_run_id": dry_run_id,
            "confirmed_by_admin_user_id": actor_admin_user_id,
            "filter_snapshot": copy.deepcopy(dry_run["filter_snapshot"]),
            "filter_hash": dry_run["filter_hash"],
            "estimated_asset_count": dry_run["estimated_asset_count"],
            "estimated_message_count": dry_run["estimated_message_count"],
            "estimated_bytes": dry_run["estimated_bytes"],
            "item_count": dry_run["estimated_asset_count"],
            "status": "pending",
            "attempt_count": 0,
            "max_attempts": 5,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "_ts": now,
        }
        self.cleanup_jobs[job_id] = job
        self.wakeups.append(("maintenance", job_id))
        await self.record_audit(
            actor_admin_user_id=actor_admin_user_id,
            event_type="archive_cleanup.job_confirmed",
            entity_type="archive_cleanup_job",
            entity_id=job_id,
            payload={"dry_run_id": dry_run_id, "filter_hash": dry_run["filter_hash"]},
            now=now,
        )
        return {k: v for k, v in copy.deepcopy(job).items() if k != "_ts"}

    async def list_cleanup_jobs(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]:
        token = decode_cursor(cursor) if cursor else None
        jobs = list(self.cleanup_jobs.values())
        jobs.sort(key=lambda j: (j["_ts"], j["id"]), reverse=True)
        if token is not None:
            jobs = [j for j in jobs if (j["_ts"], j["id"]) < (token.timestamp, token.entity_id)]
        window = jobs[: limit + 1]
        has_more = len(window) > limit
        items = [{k: v for k, v in copy.deepcopy(j).items() if k != "_ts"} for j in window[:limit]]
        next_cursor = (
            encode_cursor(timestamp=window[limit - 1]["_ts"], entity_id=window[limit - 1]["id"])
            if has_more and items
            else None
        )
        return CursorPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    async def get_cleanup_job(self, *, job_id: str) -> dict[str, Any] | None:
        item = self.cleanup_jobs.get(job_id)
        if item is None:
            return None
        return {k: v for k, v in copy.deepcopy(item).items() if k != "_ts"}

    # --- Dashboard, Operations, Audit & Metrics ---

    async def dashboard_summary(self, *, now: datetime) -> dict[str, Any]:
        ops = self._current_ops(now)
        archive_bytes = sum(
            int(img.get("archive_size_bytes") or 0)
            for img in self.image_assets.values()
            if img.get("archive_state") == "ready"
        ) + sum(
            int(fr.get("archive_size_bytes") or 0)
            for frames in self.video_frames.values()
            for fr in frames
            if fr.get("archive_state", "ready") == "ready"
        )
        return {
            "source_channels": {
                "total": len(self.source_channels),
                "enabled": sum(1 for c in self.source_channels.values() if c["enabled"]),
                "active_profiles": sum(
                    1
                    for c in self.source_channels.values()
                    if c.get("active_profile_version_id") is not None
                ),
            },
            "destination_channels": {
                "total": len(self.destination_channels),
                "enabled": sum(1 for c in self.destination_channels.values() if c["enabled"]),
            },
            "processing_ranges": {
                "total": len(self.processing_ranges),
                "active": sum(
                    1 for r in self.processing_ranges.values() if r["status"] == "active"
                ),
                "paused": sum(
                    1 for r in self.processing_ranges.values() if r["status"] == "paused"
                ),
                "completed": sum(
                    1 for r in self.processing_ranges.values() if r["status"] == "completed"
                ),
                "failed": sum(
                    1 for r in self.processing_ranges.values() if r["status"] == "failed"
                ),
            },
            "messages": {
                "total": len(self.messages),
                "processed": sum(
                    1 for m in self.messages.values() if m["processing_status"] == "processed"
                ),
                "processing": sum(
                    1 for m in self.messages.values() if m["processing_status"] == "processing"
                ),
                "failed": sum(
                    1 for m in self.messages.values() if m["processing_status"] == "failed"
                ),
                "blocked_from_analysis": sum(
                    1 for m in self.messages.values() if m.get("blocked_from_analysis")
                ),
                "unreviewed": sum(
                    1 for m in self.messages.values() if m.get("review_status") == "unreviewed"
                ),
            },
            "identities": {
                "total": len(self.identities),
                "healthy": sum(
                    1 for i in self.identities.values() if i["health_status"] == "healthy"
                ),
                "degraded": sum(
                    1 for i in self.identities.values() if i["health_status"] == "degraded"
                ),
                "failed": sum(
                    1 for i in self.identities.values() if i["health_status"] == "failed"
                ),
            },
            "archive": {
                "ready_images": sum(
                    1 for a in self.image_assets.values() if a.get("archive_state") == "ready"
                ),
                "ready_videos": sum(
                    1 for a in self.video_assets.values() if a.get("archive_state") == "ready"
                ),
                "deleted_assets": sum(
                    1 for a in self.image_assets.values() if a.get("archive_state") == "deleted"
                )
                + sum(1 for a in self.video_assets.values() if a.get("archive_state") == "deleted"),
                "total_archive_bytes": archive_bytes,
            },
            "operations": _serialize_ops(ops),
        }

    async def operations_status(self, *, now: datetime) -> dict[str, Any]:
        ops = self._current_ops(now)
        failed_items = await self.list_failed_items(step_kind=None, limit=25)
        return {
            "operations": _serialize_ops(ops),
            "queues": {
                "range_execution": sum(
                    1
                    for e in self.range_executions.values()
                    if e["status"] in {"pending", "retry_wait"}
                ),
                "image_archive": sum(
                    1
                    for a in self.image_assets.values()
                    if a.get("archive_state") in {"pending", "retry_wait"}
                ),
                "video_archive": sum(
                    1
                    for a in self.video_assets.values()
                    if a.get("archive_state") in {"pending", "retry_wait"}
                ),
                "analysis": sum(
                    1 for s in self.stage_runs.values() if s["status"] in {"pending", "retry_wait"}
                ),
                "publication": sum(
                    1
                    for p in self.publication_intents.values()
                    if p["status"] in {"pending", "partial", "retry_wait"}
                ),
                "maintenance": sum(
                    1
                    for j in self.cleanup_jobs.values()
                    if j["status"] in {"pending", "retry_wait"}
                ),
            },
            "failed_count": len(failed_items),
        }

    async def list_failed_items(
        self,
        *,
        step_kind: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        sources: list[tuple[str, dict[str, dict[str, Any]], str]] = [
            ("range_execution", self.range_executions, "status"),
            ("image_archive", self.image_assets, "archive_state"),
            ("video_archive", self.video_assets, "archive_state"),
            ("stage_run", self.stage_runs, "status"),
            ("publication_intent", self.publication_intents, "status"),
            ("cleanup_job", self.cleanup_jobs, "status"),
        ]
        for kind, bucket, status_key in sources:
            if step_kind is not None and kind != step_kind:
                continue
            for item in bucket.values():
                if item.get(status_key) == "failed":
                    results.append(
                        {
                            "step_kind": kind,
                            "target_id": item["id"],
                            "message_id": item.get("message_id"),
                            "status": "failed",
                            "error_code": item.get("last_error_code")
                            or item.get("archive_last_error_code"),
                            "error_type": item.get("last_error_type")
                            or item.get("archive_last_error_type"),
                            "attempt_count": item.get("attempt_count")
                            or item.get("archive_attempt_count", 0),
                        }
                    )
        return results[:limit]

    async def list_audit_events(
        self,
        *,
        filters: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]:
        token = decode_cursor(cursor) if cursor else None
        matched = []
        for ev in self.audit_events:
            if filters.get("entity_type") and ev["entity_type"] != filters["entity_type"]:
                continue
            if filters.get("entity_id") and ev["entity_id"] != filters["entity_id"]:
                continue
            if filters.get("event_type") and ev["event_type"] != filters["event_type"]:
                continue
            matched.append(ev)
        matched.sort(key=lambda e: (e["_ts"], e["id"]), reverse=True)
        if token is not None:
            matched = [
                e for e in matched if (e["_ts"], e["id"]) < (token.timestamp, token.entity_id)
            ]
        window = matched[: limit + 1]
        has_more = len(window) > limit
        items = [{k: v for k, v in copy.deepcopy(e).items() if k != "_ts"} for e in window[:limit]]
        next_cursor = (
            encode_cursor(timestamp=window[limit - 1]["_ts"], entity_id=window[limit - 1]["id"])
            if has_more and items
            else None
        )
        return CursorPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    async def collect_metrics_snapshot(self, *, now: datetime) -> MetricsCollectionSnapshot:
        ops = self._current_ops(now)
        msg_by_status: dict[str, int] = {}
        blocked_count = 0
        for m in self.messages.values():
            st = m["processing_status"]
            msg_by_status[st] = msg_by_status.get(st, 0) + 1
            if m.get("blocked_from_analysis"):
                blocked_count += 1

        ranges_by_status: dict[str, int] = {}
        range_lags: dict[str, int] = {}
        for r in self.processing_ranges.values():
            st = r["status"]
            ranges_by_status[st] = ranges_by_status.get(st, 0) + 1
            ceiling = r.get("active_high_watermark_message_id") or r.get(
                "resolved_fixed_end_message_id"
            )
            wm = r.get("processing_watermark_message_id")
            if isinstance(ceiling, int) and isinstance(wm, int):
                range_lags[r["id"]] = max(0, ceiling - wm)

        media_by_kind_state: dict[tuple[str, str], int] = {}
        archive_bytes = 0
        for img in self.image_assets.values():
            key = ("image", str(img.get("archive_state", "pending")))
            media_by_kind_state[key] = media_by_kind_state.get(key, 0) + 1
            if img.get("archive_state") == "ready":
                archive_bytes += int(img.get("archive_size_bytes") or 0)
        for vid in self.video_assets.values():
            key = ("video", str(vid.get("archive_state", "pending")))
            media_by_kind_state[key] = media_by_kind_state.get(key, 0) + 1
        for frames in self.video_frames.values():
            for fr in frames:
                if fr.get("archive_state", "ready") == "ready":
                    archive_bytes += int(fr.get("archive_size_bytes") or 0)

        stage_by_so: dict[tuple[str, str], int] = {}
        cache_hits = 0
        for sr in self.stage_runs.values():
            origin = str(sr.get("result_origin") or "none")
            key = (str(sr["status"]), origin)
            stage_by_so[key] = stage_by_so.get(key, 0) + 1
            if origin == "cache":
                cache_hits += 1

        calls_by_se: dict[tuple[str, str], int] = {}
        latency_sum = 0.0
        latency_count = 0
        tokens_by_kind: dict[str, int] = {}
        for call in self.inference_calls.values():
            key = (str(call["status"]), str(call.get("error_code") or "none"))
            calls_by_se[key] = calls_by_se.get(key, 0) + 1
            if call.get("latency_ms") is not None:
                latency_sum += float(call["latency_ms"])
                latency_count += 1
            usage = call.get("token_usage") or {}
            if isinstance(usage, Mapping):
                for tk, tv in usage.items():
                    if isinstance(tv, int):
                        tokens_by_kind[str(tk)] = tokens_by_kind.get(str(tk), 0) + tv

        intents_by_status: dict[str, int] = {}
        for pi in self.publication_intents.values():
            st = str(pi["status"])
            intents_by_status[st] = intents_by_status.get(st, 0) + 1

        return MetricsCollectionSnapshot(
            operations=ops,
            messages_by_status=msg_by_status,
            messages_blocked_total=blocked_count,
            ranges_by_status=ranges_by_status,
            range_lags=range_lags,
            media_assets_by_kind_state=media_by_kind_state,
            archive_bytes_total=archive_bytes,
            stage_runs_by_status_origin=stage_by_so,
            analysis_cache_hits_total=cache_hits,
            inference_calls_by_status_error=calls_by_se,
            inference_latency_ms_sum=latency_sum,
            inference_latency_ms_count=latency_count,
            inference_tokens_by_kind=tokens_by_kind,
            routing_evaluations_total=len(self.routing_evaluations),
            routing_matched_actions_total=len(self.publication_intents),
            publication_intents_by_status=intents_by_status,
            api_database_ready=True,
        )

    # --- Internal Helpers ---

    def _current_ops(self, now: datetime) -> OperationsSnapshot:
        if self._ops_provider is not None:
            return self._ops_provider(now)
        return OperationsSnapshot(
            publication=PublicationMetricsSnapshot(),
            flood_wait=FloodWaitMetricsSnapshot(),
            backlog=BacklogMetricsSnapshot(observed_at=now),
            worker=ServiceHealthSnapshot(
                service="worker",
                checked_at=now,
                last_heartbeat_at=now,
                alive=True,
                ready=True,
                database_ready=True,
                broker_ready=True,
                blockers=(),
            ),
            scheduler=ServiceHealthSnapshot(
                service="scheduler",
                checked_at=now,
                last_heartbeat_at=now,
                alive=True,
                ready=True,
                database_ready=True,
                broker_ready=True,
                blockers=(),
            ),
        )

    def _serialize_config_entity(self, family: str, entity_id: str) -> dict[str, Any]:
        ent = self.config_entities[family][entity_id]
        versions = [copy.deepcopy(self.config_versions[vid]) for vid in ent["version_ids"]]
        published = next((v for v in versions if v["state"] == "published"), None)
        return {
            "id": ent["id"],
            "family": family,
            "name": ent["name"],
            "source_channel_id": ent.get("source_channel_id"),
            "published_version_id": published["id"] if published else None,
            "versions": versions,
            "created_at": ent["created_at"],
            "updated_at": ent["updated_at"],
        }

    def _require_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        ver = self.config_versions.get(version_id)
        if ver is None or ver["family"] != family or ver["entity_id"] != entity_id:
            raise AdminNotFoundError(f"{family} version does not exist")
        return ver

    def _validate_family_version(
        self,
        *,
        family: str,
        version_id: str,
        entity_id: str,
        version_number: int,
        state: str,
        content: Mapping[str, Any],
        require_published_refs: bool,
    ) -> None:
        if family == "media-processing-profiles":
            validate_media_profile_content(content)
        elif family == "source-channel-profiles":
            cfg = validate_source_profile_content(content)
            if require_published_refs:
                for ref_vid, expected_fam in (
                    (cfg.analysis_pipeline_version_id, "analysis-pipelines"),
                    (cfg.routing_policy_version_id, "routing-policies"),
                    (cfg.media_processing_profile_version_id, "media-processing-profiles"),
                ):
                    if ref_vid is not None:
                        ref = self.config_versions.get(ref_vid)
                        if (
                            ref is None
                            or ref["family"] != expected_fam
                            or ref["state"] != "published"
                        ):
                            raise AdminValidationError(
                                f"referenced {expected_fam} version {ref_vid} must be published"
                            )
        elif family == "label-definitions":
            build_label_definition_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
            )
        elif family == "prompt-templates":
            build_prompt_template_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
            )
        elif family == "inference-profiles":
            build_inference_profile_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
            )
        elif family == "label-sets":
            build_label_set_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
                resolve_label_def=self._resolve_label_def_domain,
                require_published_refs=require_published_refs,
            )
        elif family == "analysis-stages":
            build_stage_template_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
                resolve_prompt=self._resolve_prompt_domain,
                resolve_label_set=self._resolve_label_set_domain,
                resolve_inference_profile=self._resolve_inference_profile_domain,
                require_published_refs=require_published_refs,
            )
        elif family == "analysis-pipelines":
            build_pipeline_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
                resolve_stage=self._resolve_stage_domain,
                require_published_refs=require_published_refs,
            )
        elif family == "rendering-templates":
            validate_rendering_template_syntax(str(content.get("template_text", "")))
        elif family == "routing-policies":
            validate_routing_policy_content(
                content,
                destination_exists=lambda did: did in self.destination_channels,
                template_is_published=lambda tid: (
                    tid in self.config_versions
                    and self.config_versions[tid]["family"] == "rendering-templates"
                    and self.config_versions[tid]["state"] == "published"
                ),
                require_published_refs=require_published_refs,
            )

    def _resolve_label_def_domain(self, version_id: str) -> Any:
        ver = self.config_versions.get(version_id)
        if ver is None or ver["family"] != "label-definitions":
            raise AdminValidationError(f"label definition version {version_id} does not exist")
        return build_label_definition_domain(
            version_id=ver["id"],
            entity_id=ver["entity_id"],
            version_number=ver["version_number"],
            state=ver["state"],
            content=ver["content"],
        )

    def _resolve_prompt_domain(self, version_id: str) -> Any:
        ver = self.config_versions.get(version_id)
        if ver is None or ver["family"] != "prompt-templates":
            raise AdminValidationError(f"prompt template version {version_id} does not exist")
        return build_prompt_template_domain(
            version_id=ver["id"],
            entity_id=ver["entity_id"],
            version_number=ver["version_number"],
            state=ver["state"],
            content=ver["content"],
        )

    def _resolve_inference_profile_domain(self, version_id: str) -> Any:
        ver = self.config_versions.get(version_id)
        if ver is None or ver["family"] != "inference-profiles":
            raise AdminValidationError(f"inference profile version {version_id} does not exist")
        return build_inference_profile_domain(
            version_id=ver["id"],
            entity_id=ver["entity_id"],
            version_number=ver["version_number"],
            state=ver["state"],
            content=ver["content"],
        )

    def _resolve_label_set_domain(self, version_id: str) -> Any:
        ver = self.config_versions.get(version_id)
        if ver is None or ver["family"] != "label-sets":
            raise AdminValidationError(f"label set version {version_id} does not exist")
        return build_label_set_domain(
            version_id=ver["id"],
            entity_id=ver["entity_id"],
            version_number=ver["version_number"],
            state=ver["state"],
            content=ver["content"],
            resolve_label_def=self._resolve_label_def_domain,
        )

    def _resolve_stage_domain(self, version_id: str) -> AnalysisStageTemplateVersion:
        ver = self.config_versions.get(version_id)
        if ver is None or ver["family"] != "analysis-stages":
            raise AdminValidationError(f"analysis stage version {version_id} does not exist")
        return build_stage_template_domain(
            version_id=ver["id"],
            entity_id=ver["entity_id"],
            version_number=ver["version_number"],
            state=ver["state"],
            content=ver["content"],
            resolve_prompt=self._resolve_prompt_domain,
            resolve_label_set=self._resolve_label_set_domain,
            resolve_inference_profile=self._resolve_inference_profile_domain,
        )

    def _collect_message_media_descriptors(self, message_id: str) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        for img in self.image_assets.values():
            if img["message_id"] == message_id and img.get("archive_state") == "ready":
                descriptors.append(
                    {
                        "asset_id": img["id"],
                        "kind": "image",
                        "storage_key": img.get("storage_key", f"images/{img['id']}.webp"),
                        "content_type": img.get("content_type", "image/webp"),
                        "sha256": img.get("archive_sha256", "a" * 64),
                        "width": img.get("width", 512),
                        "height": img.get("height", 512),
                    }
                )
        for vid in self.video_assets.values():
            if vid["message_id"] == message_id and vid.get("archive_state") == "ready":
                frames = self.video_frames.get(vid["id"], [])
                descriptors.append(
                    {
                        "asset_id": vid["id"],
                        "kind": "video",
                        "frames": [
                            {
                                "frame_index": fr.get("frame_index", idx),
                                "storage_key": fr.get(
                                    "storage_key", f"frames/{vid['id']}_{idx}.webp"
                                ),
                                "content_type": "image/webp",
                                "sha256": fr.get("archive_sha256", "b" * 64),
                                "width": fr.get("width", 512),
                                "height": fr.get("height", 512),
                            }
                            for idx, fr in enumerate(frames)
                        ],
                    }
                )
        return descriptors

    def _matches_message_filters(
        self,
        msg: Mapping[str, Any],
        filters: Mapping[str, Any],
    ) -> bool:
        if (
            filters.get("source_channel_id")
            and msg["source_channel_id"] != filters["source_channel_id"]
        ):
            return False
        if filters.get("published_from"):
            pub_ts = _parse_iso_dt(msg["published_at"], field="published_at")
            if pub_ts < _parse_iso_dt(filters["published_from"], field="published_from"):
                return False
        if filters.get("published_before"):
            pub_ts = _parse_iso_dt(msg["published_at"], field="published_at")
            if pub_ts >= _parse_iso_dt(filters["published_before"], field="published_before"):
                return False
        if (
            filters.get("processing_status")
            and msg["processing_status"] != filters["processing_status"]
        ):
            return False
        if filters.get("review_status") and msg.get("review_status") != filters["review_status"]:
            return False
        if (
            filters.get("duplicate_status")
            and msg.get("duplicate_status") != filters["duplicate_status"]
        ):
            return False
        if filters.get("blocked_from_analysis") is not None and bool(
            msg.get("blocked_from_analysis")
        ) != bool(filters["blocked_from_analysis"]):
            return False
        if filters.get("negative_label") is not None:
            has_neg = bool(msg.get("blocked_by_label_key")) or any(
                item.activated
                and bool(
                    self.config_versions.get(item.label_definition_version_id, {})
                    .get("content", {})
                    .get("negative")
                )
                for item in self.model_labels.get(msg["id"], ())
            )
            if has_neg != bool(filters["negative_label"]):
                return False
        if filters.get("pipeline_version_id"):
            target_pv = filters["pipeline_version_id"]
            if not any(
                r["message_id"] == msg["id"] and r.get("pipeline_version_id") == target_pv
                for r in self.analysis_runs.values()
            ):
                return False
        if filters.get("routing_policy_version_id"):
            target_rpv = filters["routing_policy_version_id"]
            if not any(
                ev["message_id"] == msg["id"] and ev.get("routing_policy_version_id") == target_rpv
                for ev in self.routing_evaluations.values()
            ):
                return False
        if filters.get("destination_channel_id"):
            target_dest = filters["destination_channel_id"]
            if not any(
                pi["message_id"] == msg["id"] and pi.get("destination_channel_id") == target_dest
                for pi in self.publication_intents.values()
            ):
                return False
        if filters.get("archive_state"):
            target_as = filters["archive_state"]
            has_img = any(
                a["message_id"] == msg["id"] and a.get("archive_state") == target_as
                for a in self.image_assets.values()
            )
            has_vid = any(
                v["message_id"] == msg["id"] and v.get("archive_state") == target_as
                for v in self.video_assets.values()
            )
            if not (has_img or has_vid):
                return False
        q = filters.get("query")
        if isinstance(q, str) and q.strip():
            text = (msg.get("original_text") or "").lower()
            if q.strip().lower() not in text:
                return False
        label_key = filters.get("label_key")
        if isinstance(label_key, str) and label_key.strip():
            ns = str(filters.get("label_namespace") or "effective")
            scope_filter = filters.get("label_scope")
            resolved = labels_to_routing_snapshot(
                resolve_label_namespaces(
                    tuple(self.model_labels.get(msg["id"], ())),
                    tuple(self.manual_events.get(msg["id"], ())),
                )
            )
            ns_data = resolved.get(ns, {})
            global_labels = ns_data.get("global", {})
            media_map = ns_data.get("media", {})
            found = False
            if scope_filter in (None, "", "global"):
                if label_key in global_labels and global_labels[label_key].get("activated"):
                    found = True
            if scope_filter in (None, "", "media"):
                for mdata in media_map.values():
                    mlinks = mdata.get("labels", {})
                    if label_key in mlinks and mlinks[label_key].get("activated"):
                        found = True
            if not found:
                return False
        return True

    def _serialize_message_summary(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        resolved = labels_to_routing_snapshot(
            resolve_label_namespaces(
                tuple(self.model_labels.get(msg["id"], ())),
                tuple(self.manual_events.get(msg["id"], ())),
            )
        )
        effective_global = [
            key
            for key, info in resolved.get("effective", {}).get("global", {}).items()
            if info.get("activated")
        ]
        return {
            **copy.deepcopy(dict(msg)),
            "effective_global_labels": sorted(effective_global),
        }

    @staticmethod
    def _serialize_manual_event(ev: ManualLabelEvent) -> dict[str, Any]:
        return {
            "event_id": ev.event_id,
            "message_id": ev.message_id,
            "target_scope": ev.target_scope,
            "target_kind": ev.target_kind,
            "target_id": ev.target_id,
            "image_asset_id": ev.image_asset_id,
            "video_asset_id": ev.video_asset_id,
            "label_definition_version_id": ev.label_definition_version_id,
            "label_key": ev.label_key,
            "operation": ev.operation.value,
            "score": ev.score,
            "activated": ev.activated,
            "actor_admin_user_id": ev.actor_admin_user_id,
            "reason": ev.reason,
            "created_at": ev.created_at.isoformat(),
        }

    def _matches_cleanup_asset(
        self,
        asset: Mapping[str, Any],
        media_kind: str,
        cleanup_filter: ArchiveCleanupFilter,
    ) -> bool:
        msg = self.messages.get(asset["message_id"])
        if msg is None:
            return False
        if cleanup_filter.media_types and media_kind not in cleanup_filter.media_types:
            return False
        if (
            cleanup_filter.source_channel_ids
            and msg["source_channel_id"] not in cleanup_filter.source_channel_ids
        ):
            return False
        if (
            cleanup_filter.processing_statuses
            and msg["processing_status"] not in cleanup_filter.processing_statuses
        ):
            return False
        if (
            cleanup_filter.review_statuses
            and msg.get("review_status") not in cleanup_filter.review_statuses
        ):
            return False
        if (
            cleanup_filter.blocked_from_analysis is not None
            and bool(msg.get("blocked_from_analysis")) != cleanup_filter.blocked_from_analysis
        ):
            return False
        return True


class _InMemoryRoutingRepositoryAdapter:
    """Adapt InMemoryAdminBackend to RoutingRepository for dry-run and formal rerouting."""

    def __init__(self, backend: InMemoryAdminBackend) -> None:
        self._backend = backend

    async def find_by_request_id(self, *, routing_request_id: str) -> Any:
        return None

    async def load_snapshot(
        self,
        *,
        message_id: str,
        routing_policy_version_id: str,
    ) -> RoutingSnapshot:
        msg = self._backend.messages[message_id]
        sc = self._backend.source_channels[msg["source_channel_id"]]
        policy_ver = self._backend.config_versions[routing_policy_version_id]
        rules: list[RoutingRule] = []
        template_texts: dict[str, str] = {}
        for idx, raw_rule in enumerate(policy_ver["content"].get("rules", [])):
            rule_id = str(raw_rule.get("id") or f"rule-{idx + 1}")
            actions: list[PublicationAction] = []
            for a_idx, raw_action in enumerate(raw_rule.get("actions", [])):
                tvid = raw_action.get("rendering_template_version_id")
                if isinstance(tvid, str) and tvid in self._backend.config_versions:
                    template_texts[tvid] = str(
                        self._backend.config_versions[tvid]["content"].get("template_text", "")
                    )
                actions.append(
                    PublicationAction(
                        action_id=str(raw_action.get("id") or f"{rule_id}-action-{a_idx + 1}"),
                        destination_channel_id=str(raw_action["destination_channel_id"]),
                        publication_mode=str(raw_action["publication_mode"]),
                        rendering_template_version_id=str(tvid) if tvid is not None else None,
                        publish_identity_id=raw_action.get("publish_identity_id"),
                        protected_content_fallback=str(
                            raw_action.get("protected_content_fallback", "fail")
                        ),
                        supplement_as_reply=bool(raw_action.get("supplement_as_reply", True)),
                        routing_rule_id=rule_id,
                    )
                )
            rules.append(
                RoutingRule(
                    rule_id=rule_id,
                    priority=int(raw_rule["priority"]),
                    enabled=bool(raw_rule.get("enabled", True)),
                    condition=raw_rule["condition"],
                    stop_on_match=bool(raw_rule.get("stop_on_match", False)),
                    actions=tuple(actions),
                )
            )

        policy = RoutingPolicy(
            policy_version_id=routing_policy_version_id,
            rules=tuple(rules),
        )
        dest_identities = {
            cid: d["default_publish_identity_id"]
            for cid, d in self._backend.destination_channels.items()
        }
        dup = self._backend.duplicate_matches.get(message_id)
        dup_state = (
            {
                "status": msg.get("duplicate_status", "unique"),
                "is_duplicate": True,
                "canonical_message_id": dup["canonical_message_id"],
                "hamming_distance": dup["hamming_distance"],
            }
            if dup is not None
            else {"status": msg.get("duplicate_status", "unchecked"), "is_duplicate": False}
        )
        return RoutingSnapshot(
            message_id=message_id,
            policy=policy,
            message={
                "id": message_id,
                "text": msg.get("original_text") or "",
                "original_text": msg.get("original_text") or "",
                "primary_telegram_message_id": msg["primary_telegram_message_id"],
                "media_count": msg.get("media_count", 0),
                "blocked_from_analysis": bool(msg.get("blocked_from_analysis", False)),
            },
            source_channel={
                "id": sc["id"],
                "telegram_peer_id": sc["telegram_peer_id"],
                "username": sc.get("username"),
                "display_name": sc["display_name"],
            },
            duplicate_state=dup_state,
            media=tuple(self._backend._collect_message_media_descriptors(message_id)),
            review_status=ReviewStatus(msg.get("review_status", "unreviewed")),
            model_assignments=tuple(self._backend.model_labels.get(message_id, ())),
            manual_events=tuple(self._backend.manual_events.get(message_id, ())),
            destination_publish_identity_ids=dest_identities,
            rendering_template_texts=template_texts,
        )

    async def persist_evaluation(
        self,
        *,
        evaluation: Any,
        decision: Any,
        intents: tuple[Any, ...],
    ) -> Any:
        from tgcurator.application.ports.routing import RoutingExecutionResult

        now = self._backend._clock()
        self._backend.routing_evaluations[evaluation.evaluation_id] = {
            "id": evaluation.evaluation_id,
            "routing_request_id": evaluation.routing_request_id,
            "message_id": evaluation.message_id,
            "routing_policy_version_id": evaluation.routing_policy_version_id,
            "facts_snapshot": copy.deepcopy(dict(evaluation.facts_snapshot)),
            "facts_hash": evaluation.facts_hash,
            "rule_outcomes": list(evaluation.rule_outcomes),
            "stopped_at_rule_id": evaluation.stopped_at_rule_id,
            "created_at": now.isoformat(),
        }
        for draft in intents:
            self._backend.publication_intents[draft.intent_id] = {
                "id": draft.intent_id,
                "routing_evaluation_id": draft.routing_evaluation_id,
                "routing_request_id": draft.routing_request_id,
                "message_id": draft.message_id,
                "routing_policy_version_id": draft.routing_policy_version_id,
                "routing_rule_id": draft.routing_rule_id,
                "routing_action_id": draft.routing_action_id,
                "destination_channel_id": draft.destination_channel_id,
                "publish_identity_id": draft.publish_identity_id,
                "publication_mode": draft.publication_mode,
                "protected_content_fallback": draft.protected_content_fallback,
                "supplement_as_reply": draft.supplement_as_reply,
                "rendering_template_version_id": draft.rendering_template_version_id,
                "rendered_text": draft.rendered_text,
                "rendered_context": dict(draft.rendered_context),
                "rendered_snapshot_hash": draft.rendered_snapshot_hash,
                "business_idempotency_key": draft.business_idempotency_key,
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 5,
                "created_at": now.isoformat(),
            }
            self._backend.wakeups.append(("publication", draft.intent_id))

        return RoutingExecutionResult(
            evaluation_id=evaluation.evaluation_id,
            routing_request_id=evaluation.routing_request_id,
            message_id=evaluation.message_id,
            routing_policy_version_id=evaluation.routing_policy_version_id,
            facts_snapshot=evaluation.facts_snapshot,
            facts_hash=evaluation.facts_hash,
            decision=decision,
            intents=intents,
            persisted=True,
            dry_run=False,
        )


def _parse_iso_dt(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise AdminValidationError(f"{field} must include a timezone offset")
        return value
    if not isinstance(value, str) or not value.strip():
        raise AdminValidationError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdminValidationError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AdminValidationError(f"{field} must include a timezone offset")
    return parsed


def _build_cleanup_filter(raw: Mapping[str, Any]) -> ArchiveCleanupFilter:
    def opt_dt(key: str) -> datetime | None:
        val = raw.get(key)
        return _parse_iso_dt(val, field=key) if val is not None else None

    def tup(key: str) -> tuple[str, ...]:
        val = raw.get(key) or ()
        if not isinstance(val, (list, tuple)):
            raise AdminValidationError(f"{key} must be a list of strings")
        return tuple(str(x) for x in val)

    try:
        return ArchiveCleanupFilter(
            archived_from=opt_dt("archived_from"),
            archived_before=opt_dt("archived_before"),
            source_channel_ids=tup("source_channel_ids"),
            message_published_from=opt_dt("message_published_from"),
            message_published_before=opt_dt("message_published_before"),
            model_label_keys=tup("model_label_keys"),
            manual_label_keys=tup("manual_label_keys"),
            effective_label_keys=tup("effective_label_keys"),
            label_scopes=tup("label_scopes"),
            negative_label=raw.get("negative_label"),
            review_statuses=tup("review_statuses"),
            processing_statuses=tup("processing_statuses"),
            duplicate_statuses=tup("duplicate_statuses"),
            media_types=tup("media_types"),
            blocked_from_analysis=raw.get("blocked_from_analysis"),
        )
    except DomainValidationError as exc:
        raise AdminValidationError(str(exc)) from exc


def _serialize_ops(ops: OperationsSnapshot) -> dict[str, Any]:
    return {
        "publication": {
            "claimed": ops.publication.claimed,
            "sent": ops.publication.sent,
            "retry_scheduled": ops.publication.retry_scheduled,
            "failed": ops.publication.failed,
            "cancelled": ops.publication.cancelled,
            "reconciled_parts": ops.publication.reconciled_parts,
        },
        "flood_wait": {
            "events": ops.flood_wait.events,
            "wait_seconds_total": ops.flood_wait.wait_seconds_total,
            "wait_seconds_max": ops.flood_wait.wait_seconds_max,
            "last_observed_at": (
                ops.flood_wait.last_observed_at.isoformat()
                if ops.flood_wait.last_observed_at
                else None
            ),
            "last_retry_at": (
                ops.flood_wait.last_retry_at.isoformat() if ops.flood_wait.last_retry_at else None
            ),
        },
        "backlog": {
            "publication_pending": ops.backlog.publication_pending,
            "publication_partial": ops.backlog.publication_partial,
            "publication_retry_due": ops.backlog.publication_retry_due,
            "cleanup_pending": ops.backlog.cleanup_pending,
            "cleanup_retry_due": ops.backlog.cleanup_retry_due,
            "durable_wakeups_due": ops.backlog.durable_wakeups_due,
            "observed_at": ops.backlog.observed_at.isoformat() if ops.backlog.observed_at else None,
        },
        "worker": {
            "alive": ops.worker.alive,
            "ready": ops.worker.ready,
            "database_ready": ops.worker.database_ready,
            "broker_ready": ops.worker.broker_ready,
            "last_heartbeat_at": (
                ops.worker.last_heartbeat_at.isoformat() if ops.worker.last_heartbeat_at else None
            ),
            "blockers": list(ops.worker.blockers),
        },
        "scheduler": {
            "alive": ops.scheduler.alive,
            "ready": ops.scheduler.ready,
            "database_ready": ops.scheduler.database_ready,
            "broker_ready": ops.scheduler.broker_ready,
            "last_heartbeat_at": (
                ops.scheduler.last_heartbeat_at.isoformat()
                if ops.scheduler.last_heartbeat_at
                else None
            ),
            "blockers": list(ops.scheduler.blockers),
        },
    }


__all__ = [
    "InMemoryAdminBackend",
    "InMemorySecretVault",
]
