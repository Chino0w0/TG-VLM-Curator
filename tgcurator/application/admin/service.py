from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from tgcurator.application.analysis import AnalysisTargetInput
from tgcurator.application.ports.admin_api import (
    AuthenticatedAdmin,
    CursorPage,
    MetricsCollectionSnapshot,
)
from tgcurator.application.ports.secrets import SecretStatus, SecretVault
from tgcurator.domain.audit import sanitize_audit_payload
from tgcurator.domain.telegram import TelegramCapabilityResolver

from .auth import AdminSessionService
from .config_validation import SUPPORTED_CONFIG_FAMILIES
from .errors import (
    AdminConflictError,
    AdminNotFoundError,
    AdminValidationError,
)
from .metrics import render_prometheus_metrics
from .test_bench import PromptTestBenchResult, PromptTestBenchService


class AdminWorkspaceBackend(Protocol):
    """Persistence and orchestration port for the administrative HTTP surface."""

    async def record_audit(
        self,
        *,
        actor_admin_user_id: str | None,
        event_type: str,
        entity_type: str,
        entity_id: str | None,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> str: ...

    async def dashboard_summary(self, *, now: datetime) -> dict[str, Any]: ...

    async def list_identities(self) -> list[dict[str, Any]]: ...

    async def get_identity(self, *, identity_id: str) -> dict[str, Any] | None: ...

    async def create_identity(
        self,
        *,
        name: str,
        identity_type: str,
        secret_id: str,
        enabled: bool,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def update_identity(
        self,
        *,
        identity_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    async def list_source_channels(self) -> list[dict[str, Any]]: ...

    async def get_source_channel(self, *, channel_id: str) -> dict[str, Any] | None: ...

    async def create_source_channel(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    async def update_source_channel(
        self,
        *,
        channel_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    async def list_destination_channels(self) -> list[dict[str, Any]]: ...

    async def get_destination_channel(self, *, channel_id: str) -> dict[str, Any] | None: ...

    async def create_destination_channel(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    async def update_destination_channel(
        self,
        *,
        channel_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    async def list_processing_ranges(
        self,
        *,
        source_channel_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get_processing_range(self, *, range_id: str) -> dict[str, Any] | None: ...

    async def create_processing_range(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    async def update_processing_range(
        self,
        *,
        range_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    async def list_range_executions(self, *, range_id: str) -> list[dict[str, Any]]: ...

    async def list_config_entities(self, *, family: str) -> list[dict[str, Any]]: ...

    async def get_config_entity(
        self,
        *,
        family: str,
        entity_id: str,
    ) -> dict[str, Any] | None: ...

    async def create_config_entity(
        self,
        *,
        family: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    async def create_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        content: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    async def update_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        content: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    async def publish_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        supersede_published: bool,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def archive_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def prepare_test_bench_inputs(
        self,
        *,
        payload: Mapping[str, Any],
    ) -> tuple[Any, tuple[AnalysisTargetInput, ...], Mapping[str, Any] | None]: ...

    async def list_messages(
        self,
        *,
        filters: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]: ...

    async def get_message_detail(self, *, message_id: str) -> dict[str, Any] | None: ...

    async def apply_manual_label(
        self,
        *,
        message_id: str,
        payload: Mapping[str, Any],
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def transition_review_status(
        self,
        *,
        message_id: str,
        new_status: str,
        reason: str | None,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def reanalyze_message(
        self,
        *,
        message_id: str,
        pipeline_version_id: str | None,
        reason: str | None,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def reroute_message(
        self,
        *,
        message_id: str,
        routing_policy_version_id: str | None,
        dry_run: bool,
        reason: str | None,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def skip_message(
        self,
        *,
        message_id: str,
        reason: str,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def retry_step(
        self,
        *,
        step_kind: str,
        target_id: str,
        reason: str | None,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def create_cleanup_dry_run(
        self,
        *,
        filter_payload: Mapping[str, Any],
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def list_cleanup_dry_runs(self, *, limit: int) -> list[dict[str, Any]]: ...

    async def get_cleanup_dry_run(self, *, dry_run_id: str) -> dict[str, Any] | None: ...

    async def confirm_cleanup_job(
        self,
        *,
        dry_run_id: str,
        expected_filter_hash: str,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]: ...

    async def list_cleanup_jobs(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]: ...

    async def get_cleanup_job(self, *, job_id: str) -> dict[str, Any] | None: ...

    async def operations_status(self, *, now: datetime) -> dict[str, Any]: ...

    async def list_failed_items(
        self,
        *,
        step_kind: str | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def list_audit_events(
        self,
        *,
        filters: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]: ...

    async def collect_metrics_snapshot(self, *, now: datetime) -> MetricsCollectionSnapshot: ...


class AdminApplicationService:
    """Coordinate administrative use cases, secret redaction, capabilities, and audit logging."""

    def __init__(
        self,
        *,
        sessions: AdminSessionService,
        secrets: SecretVault,
        backend: AdminWorkspaceBackend,
        test_bench: PromptTestBenchService | None = None,
        capability_resolver: TelegramCapabilityResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self.sessions = sessions
        self._secrets = secrets
        self._backend = backend
        self._test_bench = test_bench or PromptTestBenchService(provider=None)
        self._capabilities = capability_resolver or TelegramCapabilityResolver()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4

    # --- Secrets (strictly write-only) ---

    async def list_secrets(self) -> list[dict[str, Any]]:
        statuses = await self._secrets.list_statuses()
        return [_format_secret_status(item) for item in statuses]

    async def get_secret(self, *, secret_id: str) -> dict[str, Any]:
        uid = _parse_uuid(secret_id, field="secret_id")
        status = await self._secrets.status(secret_id=uid)
        if status is None:
            raise AdminNotFoundError("secret reference does not exist")
        return _format_secret_status(status)

    async def create_secret(
        self,
        *,
        admin: AuthenticatedAdmin,
        secret_type: str,
        plaintext: str,
    ) -> dict[str, Any]:
        if not isinstance(plaintext, str) or not plaintext:
            raise AdminValidationError("secret plaintext must not be empty")
        try:
            secret_uid = await self._secrets.store(
                secret_type=secret_type,
                plaintext=plaintext.encode("utf-8"),
            )
        except (TypeError, ValueError) as exc:
            raise AdminValidationError(str(exc)) from exc

        status = await self._secrets.status(secret_id=secret_uid)
        assert status is not None
        now = self._clock()
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="secret.created",
            entity_type="encrypted_secret",
            entity_id=str(secret_uid),
            payload=sanitize_audit_payload(
                {
                    "secret_id": str(status.secret_id),
                    "secret_type": status.secret_type,
                    "key_id": status.key_id,
                    "secret_configured": True,
                }
            ),
            now=now,
        )
        return _format_secret_status(status)

    async def rotate_secret(
        self,
        *,
        admin: AuthenticatedAdmin,
        secret_id: str,
        plaintext: str,
    ) -> dict[str, Any]:
        if not isinstance(plaintext, str) or not plaintext:
            raise AdminValidationError("secret plaintext must not be empty")
        uid = _parse_uuid(secret_id, field="secret_id")
        try:
            status = await self._secrets.rotate(
                secret_id=uid,
                plaintext=plaintext.encode("utf-8"),
            )
        except LookupError as exc:
            raise AdminNotFoundError("secret reference does not exist") from exc
        except (TypeError, ValueError) as exc:
            raise AdminValidationError(str(exc)) from exc

        now = self._clock()
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="secret.rotated",
            entity_type="encrypted_secret",
            entity_id=str(status.secret_id),
            payload=sanitize_audit_payload(
                {
                    "secret_id": str(status.secret_id),
                    "secret_type": status.secret_type,
                    "key_id": status.key_id,
                    "secret_configured": True,
                }
            ),
            now=now,
        )
        return _format_secret_status(status)

    # --- Telegram Identities & Capability Resolver ---

    async def list_identities(self) -> list[dict[str, Any]]:
        items = await self._backend.list_identities()
        return [self._enrich_identity(item) for item in items]

    async def get_identity(self, *, identity_id: str) -> dict[str, Any]:
        _parse_uuid(identity_id, field="identity_id")
        item = await self._backend.get_identity(identity_id=identity_id)
        if item is None:
            raise AdminNotFoundError("telegram identity does not exist")
        return self._enrich_identity(item)

    async def create_identity(
        self,
        *,
        admin: AuthenticatedAdmin,
        name: str,
        identity_type: str,
        secret_id: str,
        enabled: bool = True,
    ) -> dict[str, Any]:
        secret_uid = _parse_uuid(secret_id, field="secret_id")
        if await self._secrets.status(secret_id=secret_uid) is None:
            raise AdminValidationError("referenced secret_id does not exist")
        now = self._clock()
        created = await self._backend.create_identity(
            name=name,
            identity_type=identity_type,
            secret_id=str(secret_uid),
            enabled=enabled,
            now=now,
        )
        enriched = self._enrich_identity(created)
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="telegram_identity.created",
            entity_type="telegram_identity",
            entity_id=enriched["id"],
            payload=sanitize_audit_payload({"after": enriched}),
            now=now,
        )
        return enriched

    async def update_identity(
        self,
        *,
        admin: AuthenticatedAdmin,
        identity_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        _parse_uuid(identity_id, field="identity_id")
        if "secret_id" in changes and changes["secret_id"] is not None:
            secret_uid = _parse_uuid(str(changes["secret_id"]), field="secret_id")
            if await self._secrets.status(secret_id=secret_uid) is None:
                raise AdminValidationError("referenced secret_id does not exist")
        now = self._clock()
        before, after = await self._backend.update_identity(
            identity_id=identity_id,
            changes=changes,
            now=now,
        )
        enriched = self._enrich_identity(after)
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="telegram_identity.updated",
            entity_type="telegram_identity",
            entity_id=identity_id,
            payload=sanitize_audit_payload(
                {"before": self._enrich_identity(before), "after": enriched}
            ),
            now=now,
        )
        return enriched

    async def evaluate_identity_capabilities(
        self,
        *,
        identity_id: str,
        source_protected_content: bool = False,
        has_source_access: bool = True,
        has_destination_post_permission: bool = True,
        capability_overrides: Mapping[str, bool] | None = None,
    ) -> dict[str, Any]:
        identity = await self.get_identity(identity_id=identity_id)
        cap_set = self._capabilities.resolve(
            identity_type=identity["identity_type"],
            enabled=bool(identity["enabled"]),
            health_status=identity["health_status"],
            source_protected_content=source_protected_content,
            has_source_access=has_source_access,
            has_destination_post_permission=has_destination_post_permission,
            capability_overrides=capability_overrides,
        )
        return {
            "identity_id": identity["id"],
            "identity_type": identity["identity_type"],
            "enabled": identity["enabled"],
            "health_status": identity["health_status"],
            "capabilities": cap_set.to_dict(),
        }

    # --- Channels & Processing Ranges ---

    async def list_source_channels(self) -> list[dict[str, Any]]:
        return await self._backend.list_source_channels()

    async def get_source_channel(self, *, channel_id: str) -> dict[str, Any]:
        _parse_uuid(channel_id, field="channel_id")
        item = await self._backend.get_source_channel(channel_id=channel_id)
        if item is None:
            raise AdminNotFoundError("source channel does not exist")
        return item

    async def create_source_channel(
        self,
        *,
        admin: AuthenticatedAdmin,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._clock()
        created = await self._backend.create_source_channel(payload=payload, now=now)
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="source_channel.created",
            entity_type="source_channel",
            entity_id=created["id"],
            payload=sanitize_audit_payload({"after": created}),
            now=now,
        )
        return created

    async def update_source_channel(
        self,
        *,
        admin: AuthenticatedAdmin,
        channel_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        _parse_uuid(channel_id, field="channel_id")
        now = self._clock()
        before, after = await self._backend.update_source_channel(
            channel_id=channel_id,
            changes=changes,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="source_channel.updated",
            entity_type="source_channel",
            entity_id=channel_id,
            payload=sanitize_audit_payload({"before": before, "after": after}),
            now=now,
        )
        return after

    async def list_destination_channels(self) -> list[dict[str, Any]]:
        return await self._backend.list_destination_channels()

    async def get_destination_channel(self, *, channel_id: str) -> dict[str, Any]:
        _parse_uuid(channel_id, field="channel_id")
        item = await self._backend.get_destination_channel(channel_id=channel_id)
        if item is None:
            raise AdminNotFoundError("destination channel does not exist")
        return item

    async def create_destination_channel(
        self,
        *,
        admin: AuthenticatedAdmin,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._clock()
        created = await self._backend.create_destination_channel(payload=payload, now=now)
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="destination_channel.created",
            entity_type="destination_channel",
            entity_id=created["id"],
            payload=sanitize_audit_payload({"after": created}),
            now=now,
        )
        return created

    async def update_destination_channel(
        self,
        *,
        admin: AuthenticatedAdmin,
        channel_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        _parse_uuid(channel_id, field="channel_id")
        now = self._clock()
        before, after = await self._backend.update_destination_channel(
            channel_id=channel_id,
            changes=changes,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="destination_channel.updated",
            entity_type="destination_channel",
            entity_id=channel_id,
            payload=sanitize_audit_payload({"before": before, "after": after}),
            now=now,
        )
        return after

    async def list_processing_ranges(
        self,
        *,
        source_channel_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._backend.list_processing_ranges(
            source_channel_id=source_channel_id,
            status=status,
        )

    async def get_processing_range(self, *, range_id: str) -> dict[str, Any]:
        _parse_uuid(range_id, field="range_id")
        item = await self._backend.get_processing_range(range_id=range_id)
        if item is None:
            raise AdminNotFoundError("processing range does not exist")
        executions = await self._backend.list_range_executions(range_id=range_id)
        return {**item, "executions": executions}

    async def create_processing_range(
        self,
        *,
        admin: AuthenticatedAdmin,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._clock()
        created = await self._backend.create_processing_range(payload=payload, now=now)
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="processing_range.created",
            entity_type="processing_range",
            entity_id=created["id"],
            payload=sanitize_audit_payload({"after": created}),
            now=now,
        )
        return created

    async def update_processing_range(
        self,
        *,
        admin: AuthenticatedAdmin,
        range_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        _parse_uuid(range_id, field="range_id")
        now = self._clock()
        before, after = await self._backend.update_processing_range(
            range_id=range_id,
            changes=changes,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="processing_range.updated",
            entity_type="processing_range",
            entity_id=range_id,
            payload=sanitize_audit_payload({"before": before, "after": after}),
            now=now,
        )
        return after

    async def list_range_executions(self, *, range_id: str) -> list[dict[str, Any]]:
        _parse_uuid(range_id, field="range_id")
        if await self._backend.get_processing_range(range_id=range_id) is None:
            raise AdminNotFoundError("processing range does not exist")
        return await self._backend.list_range_executions(range_id=range_id)

    # --- Versioned Configuration Families (DRAFT -> PUBLISHED -> ARCHIVED) ---

    async def list_config_entities(self, *, family: str) -> list[dict[str, Any]]:
        _validate_family(family)
        return await self._backend.list_config_entities(family=family)

    async def get_config_entity(self, *, family: str, entity_id: str) -> dict[str, Any]:
        _validate_family(family)
        _parse_uuid(entity_id, field="entity_id")
        entity = await self._backend.get_config_entity(family=family, entity_id=entity_id)
        if entity is None:
            raise AdminNotFoundError(f"{family} entity does not exist")
        return entity

    async def create_config_entity(
        self,
        *,
        admin: AuthenticatedAdmin,
        family: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_family(family)
        now = self._clock()
        created = await self._backend.create_config_entity(
            family=family,
            payload=payload,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type=f"{family}.created",
            entity_type=family,
            entity_id=created["id"],
            payload=sanitize_audit_payload({"after": created}),
            now=now,
        )
        return created

    async def create_config_version(
        self,
        *,
        admin: AuthenticatedAdmin,
        family: str,
        entity_id: str,
        content: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_family(family)
        _parse_uuid(entity_id, field="entity_id")
        now = self._clock()
        version = await self._backend.create_config_version(
            family=family,
            entity_id=entity_id,
            content=content,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type=f"{family}.version_created",
            entity_type=family,
            entity_id=version["id"],
            payload=sanitize_audit_payload({"entity_id": entity_id, "version": version}),
            now=now,
        )
        return version

    async def update_config_version(
        self,
        *,
        admin: AuthenticatedAdmin,
        family: str,
        entity_id: str,
        version_id: str,
        content: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_family(family)
        _parse_uuid(entity_id, field="entity_id")
        _parse_uuid(version_id, field="version_id")
        now = self._clock()
        updated = await self._backend.update_config_version(
            family=family,
            entity_id=entity_id,
            version_id=version_id,
            content=content,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type=f"{family}.version_updated",
            entity_type=family,
            entity_id=version_id,
            payload=sanitize_audit_payload({"entity_id": entity_id, "version": updated}),
            now=now,
        )
        return updated

    async def publish_config_version(
        self,
        *,
        admin: AuthenticatedAdmin,
        family: str,
        entity_id: str,
        version_id: str,
        supersede_published: bool = True,
    ) -> dict[str, Any]:
        _validate_family(family)
        _parse_uuid(entity_id, field="entity_id")
        _parse_uuid(version_id, field="version_id")
        now = self._clock()
        published = await self._backend.publish_config_version(
            family=family,
            entity_id=entity_id,
            version_id=version_id,
            supersede_published=supersede_published,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type=f"{family}.version_published",
            entity_type=family,
            entity_id=version_id,
            payload=sanitize_audit_payload(
                {
                    "entity_id": entity_id,
                    "version_id": version_id,
                    "version_number": published["version_number"],
                    "state": published["state"],
                }
            ),
            now=now,
        )
        return published

    async def archive_config_version(
        self,
        *,
        admin: AuthenticatedAdmin,
        family: str,
        entity_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        _validate_family(family)
        _parse_uuid(entity_id, field="entity_id")
        _parse_uuid(version_id, field="version_id")
        now = self._clock()
        archived = await self._backend.archive_config_version(
            family=family,
            entity_id=entity_id,
            version_id=version_id,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type=f"{family}.version_archived",
            entity_type=family,
            entity_id=version_id,
            payload=sanitize_audit_payload(
                {
                    "entity_id": entity_id,
                    "version_id": version_id,
                    "state": archived["state"],
                }
            ),
            now=now,
        )
        return archived

    # --- Prompt Test Bench (run_mode="test", strictly side-effect-free) ---

    async def run_prompt_test_bench(
        self,
        *,
        admin: AuthenticatedAdmin,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        stage, targets, source_context = await self._backend.prepare_test_bench_inputs(
            payload=payload
        )
        invoke_provider = bool(payload.get("invoke_provider", True))
        result: PromptTestBenchResult = await self._test_bench.run(
            stage=stage,
            targets=targets,
            source_channel_context=source_context,
            invoke_provider=invoke_provider,
        )
        now = self._clock()
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="analysis.test_bench_executed",
            entity_type="analysis_test_bench",
            entity_id=None,
            payload=sanitize_audit_payload(
                {
                    "run_mode": "test",
                    "status": result.status,
                    "stage_version_id": stage.version_id,
                    "target_ids": [t.target_id for t in targets],
                    "invoke_provider": invoke_provider,
                }
            ),
            now=now,
        )
        return {
            "run_mode": result.run_mode,
            "status": result.status,
            "system_prompt": result.system_prompt,
            "user_prompt": result.user_prompt,
            "input_manifest": dict(result.input_manifest),
            "input_manifest_hash": result.input_manifest_hash,
            "structured_output_schema": dict(result.structured_output_schema),
            "structured_output_schema_hash": result.structured_output_schema_hash,
            "raw_response": dict(result.raw_response) if result.raw_response is not None else None,
            "parsed_results": result.parsed_results,
            "invalid_targets": result.invalid_targets,
            "missing_target_ids": result.missing_target_ids,
            "negative_gate_would_trigger": result.negative_gate_would_trigger,
            "token_usage": dict(result.token_usage) if result.token_usage is not None else None,
            "latency_ms": result.latency_ms,
            "error_code": result.error_code,
            "error_type": result.error_type,
        }

    # --- Messages Browser, Causal Detail, Review, Reanalysis & Reroute ---

    async def list_messages(
        self,
        *,
        filters: Mapping[str, Any],
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        bounded_limit = _validate_limit(limit)
        page = await self._backend.list_messages(
            filters=filters,
            cursor=cursor,
            limit=bounded_limit,
        )
        return {
            "items": list(page.items),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    async def get_message_detail(self, *, message_id: str) -> dict[str, Any]:
        _parse_uuid(message_id, field="message_id")
        detail = await self._backend.get_message_detail(message_id=message_id)
        if detail is None:
            raise AdminNotFoundError("message does not exist")
        return detail

    async def apply_manual_label(
        self,
        *,
        admin: AuthenticatedAdmin,
        message_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        _parse_uuid(message_id, field="message_id")
        now = self._clock()
        result = await self._backend.apply_manual_label(
            message_id=message_id,
            payload=payload,
            actor_admin_user_id=admin.user_id,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type=f"message.manual_label_{payload.get('operation', 'set')}",
            entity_type="message",
            entity_id=message_id,
            payload=sanitize_audit_payload({"event": result}),
            now=now,
        )
        return result

    async def transition_review_status(
        self,
        *,
        admin: AuthenticatedAdmin,
        message_id: str,
        new_status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        _parse_uuid(message_id, field="message_id")
        now = self._clock()
        result = await self._backend.transition_review_status(
            message_id=message_id,
            new_status=new_status,
            reason=reason,
            actor_admin_user_id=admin.user_id,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="message.review_status_changed",
            entity_type="message",
            entity_id=message_id,
            payload=sanitize_audit_payload({"event": result}),
            now=now,
        )
        return result

    async def reanalyze_message(
        self,
        *,
        admin: AuthenticatedAdmin,
        message_id: str,
        pipeline_version_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        _parse_uuid(message_id, field="message_id")
        now = self._clock()
        result = await self._backend.reanalyze_message(
            message_id=message_id,
            pipeline_version_id=pipeline_version_id,
            reason=reason,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="message.reanalysis_requested",
            entity_type="message",
            entity_id=message_id,
            payload=sanitize_audit_payload(result),
            now=now,
        )
        return result

    async def reroute_message(
        self,
        *,
        admin: AuthenticatedAdmin,
        message_id: str,
        routing_policy_version_id: str | None = None,
        dry_run: bool = True,
        reason: str | None = None,
    ) -> dict[str, Any]:
        _parse_uuid(message_id, field="message_id")
        now = self._clock()
        result = await self._backend.reroute_message(
            message_id=message_id,
            routing_policy_version_id=routing_policy_version_id,
            dry_run=dry_run,
            reason=reason,
            now=now,
        )
        if not dry_run:
            await self._backend.record_audit(
                actor_admin_user_id=admin.user_id,
                event_type="message.rerouted",
                entity_type="message",
                entity_id=message_id,
                payload=sanitize_audit_payload(
                    {
                        "evaluation_id": result["evaluation_id"],
                        "routing_policy_version_id": result["routing_policy_version_id"],
                        "intent_count": len(result["intents"]),
                        "reason": reason,
                    }
                ),
                now=now,
            )
        return result

    async def skip_message(
        self,
        *,
        admin: AuthenticatedAdmin,
        message_id: str,
        reason: str,
    ) -> dict[str, Any]:
        _parse_uuid(message_id, field="message_id")
        if not isinstance(reason, str) or not reason.strip():
            raise AdminValidationError("administrator skip requires a non-blank reason")
        now = self._clock()
        result = await self._backend.skip_message(
            message_id=message_id,
            reason=reason.strip(),
            actor_admin_user_id=admin.user_id,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="message.admin_skipped",
            entity_type="message",
            entity_id=message_id,
            payload=sanitize_audit_payload(result),
            now=now,
        )
        return result

    # --- Archive Cleanup (enforced Dry-Run -> Confirm) ---

    async def create_cleanup_dry_run(
        self,
        *,
        admin: AuthenticatedAdmin,
        filter_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._clock()
        return await self._backend.create_cleanup_dry_run(
            filter_payload=filter_payload,
            actor_admin_user_id=admin.user_id,
            now=now,
        )

    async def list_cleanup_dry_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._backend.list_cleanup_dry_runs(limit=_validate_limit(limit))

    async def get_cleanup_dry_run(self, *, dry_run_id: str) -> dict[str, Any]:
        _parse_uuid(dry_run_id, field="dry_run_id")
        item = await self._backend.get_cleanup_dry_run(dry_run_id=dry_run_id)
        if item is None:
            raise AdminNotFoundError("archive cleanup dry run does not exist")
        return item

    async def confirm_cleanup_job(
        self,
        *,
        admin: AuthenticatedAdmin,
        dry_run_id: str,
        expected_filter_hash: str,
    ) -> dict[str, Any]:
        _parse_uuid(dry_run_id, field="dry_run_id")
        if not isinstance(expected_filter_hash, str) or len(expected_filter_hash.strip()) != 64:
            raise AdminValidationError("expected_filter_hash must be a 64-character SHA-256 hex")
        now = self._clock()
        return await self._backend.confirm_cleanup_job(
            dry_run_id=dry_run_id,
            expected_filter_hash=expected_filter_hash.strip().lower(),
            actor_admin_user_id=admin.user_id,
            now=now,
        )

    async def list_cleanup_jobs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        page = await self._backend.list_cleanup_jobs(
            cursor=cursor,
            limit=_validate_limit(limit),
        )
        return {
            "items": list(page.items),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    async def get_cleanup_job(self, *, job_id: str) -> dict[str, Any]:
        _parse_uuid(job_id, field="job_id")
        item = await self._backend.get_cleanup_job(job_id=job_id)
        if item is None:
            raise AdminNotFoundError("archive cleanup job does not exist")
        return item

    # --- Dashboard, Operations, Retry, Audit Log & Metrics ---

    async def dashboard_summary(self) -> dict[str, Any]:
        return await self._backend.dashboard_summary(now=self._clock())

    async def operations_status(self) -> dict[str, Any]:
        return await self._backend.operations_status(now=self._clock())

    async def list_failed_items(
        self,
        *,
        step_kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._backend.list_failed_items(
            step_kind=step_kind,
            limit=_validate_limit(limit),
        )

    async def retry_step(
        self,
        *,
        admin: AuthenticatedAdmin,
        step_kind: str,
        target_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        _parse_uuid(target_id, field="target_id")
        now = self._clock()
        result = await self._backend.retry_step(
            step_kind=step_kind,
            target_id=target_id,
            reason=reason,
            actor_admin_user_id=admin.user_id,
            now=now,
        )
        await self._backend.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="operations.step_retried",
            entity_type=step_kind,
            entity_id=target_id,
            payload=sanitize_audit_payload({**result, "reason": reason}),
            now=now,
        )
        return result

    async def list_audit_events(
        self,
        *,
        filters: Mapping[str, Any],
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        page = await self._backend.list_audit_events(
            filters=filters,
            cursor=cursor,
            limit=_validate_limit(limit),
        )
        return {
            "items": list(page.items),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    async def render_metrics(self) -> str:
        snapshot = await self._backend.collect_metrics_snapshot(now=self._clock())
        return render_prometheus_metrics(snapshot)

    def _enrich_identity(self, item: Mapping[str, Any]) -> dict[str, Any]:
        cap_set = self._capabilities.resolve(
            identity_type=str(item["identity_type"]),
            enabled=bool(item["enabled"]),
            health_status=str(item["health_status"]),
        )
        return {
            "id": str(item["id"]),
            "name": str(item["name"]),
            "identity_type": str(item["identity_type"]),
            "enabled": bool(item["enabled"]),
            "health_status": str(item["health_status"]),
            "last_connected_at": item.get("last_connected_at"),
            "last_error": item.get("last_error"),
            "secret_id": str(item["secret_id"]),
            "secret_configured": True,
            "capabilities": cap_set.to_dict(),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }


def _format_secret_status(status: SecretStatus) -> dict[str, Any]:
    return {
        "secret_id": str(status.secret_id),
        "secret_type": status.secret_type,
        "key_id": status.key_id,
        "configured": True,
        "created_at": status.created_at.isoformat(),
    }


def _parse_uuid(raw: str, *, field: str) -> UUID:
    if not isinstance(raw, str) or not raw.strip():
        raise AdminValidationError(f"{field} must be a valid UUID")
    try:
        return UUID(raw.strip())
    except ValueError as exc:
        raise AdminValidationError(f"{field} must be a valid UUID") from exc


def _validate_family(family: str) -> None:
    if family not in SUPPORTED_CONFIG_FAMILIES:
        raise AdminNotFoundError(f"unsupported configuration family: {family}")


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise AdminValidationError("limit must be an integer between 1 and 200")
    return limit


__all__ = [
    "AdminApplicationService",
    "AdminConflictError",
    "AdminNotFoundError",
    "AdminValidationError",
    "AdminWorkspaceBackend",
]
