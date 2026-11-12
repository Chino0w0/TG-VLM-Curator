from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, exists, func, null, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tgcurator.application.admin.config_validation import (
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
from tgcurator.application.admin.errors import (
    AdminConflictError,
    AdminNotFoundError,
    AdminValidationError,
)
from tgcurator.application.admin.in_memory_backend import (
    _build_cleanup_filter,
    _parse_iso_dt,
    _serialize_ops,
)
from tgcurator.application.analysis import AnalysisTargetInput
from tgcurator.application.cleanup import ArchiveCleanupService
from tgcurator.application.ports.admin_api import (
    AdminAccountRecord,
    AdminSessionState,
    CursorPage,
    MetricsCollectionSnapshot,
)
from tgcurator.application.ports.operations import OperationsSnapshot
from tgcurator.application.ports.routing import RoutingRequest
from tgcurator.application.review import ReviewService
from tgcurator.application.routing import RoutingService
from tgcurator.domain.analysis import AnalysisStageTemplateVersion, LabelScope
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
    ReviewStatus,
    labels_to_routing_snapshot,
    resolve_label_namespaces,
)
from tgcurator.infrastructure.observability import OperationsMonitor
from tgcurator.shared import DomainValidationError, decode_cursor, encode_cursor

from .cleanup_repository import SqlAlchemyArchiveCleanupRepository
from .models import (
    AdminSessionRecord,
    AdminUser,
    AnalysisPipelineRecord,
    AnalysisPipelineVersionRecord,
    AnalysisRunRecord,
    AnalysisStageTemplateRecord,
    AnalysisStageTemplateVersionRecord,
    ArchiveCleanupDryRunRecord,
    ArchiveCleanupJobRecord,
    AuditEvent,
    DestinationChannel,
    DuplicateMatchRecord,
    DurableWakeupRecord,
    ImageAssetRecord,
    InferenceCallRecord,
    InferenceProfileRecord,
    InferenceProfileVersionRecord,
    InputManifestRecord,
    LabelBindingRecord,
    LabelDefinitionRecord,
    LabelDefinitionVersionRecord,
    LabelSetRecord,
    LabelSetVersionRecord,
    ManualLabelAssignmentRecord,
    MediaProcessingProfileRecord,
    MediaProcessingProfileVersionRecord,
    MessagePartRecord,
    MessageRecord,
    MessageReviewEventRecord,
    ModelLabelAssignmentRecord,
    PipelineStageNodeRecord,
    ProcessingRangeRecord,
    PromptTemplateRecord,
    PromptTemplateVersionRecord,
    PublicationIntentRecord,
    PublishAttemptRecord,
    PublishRecord,
    RangeExecutionRecord,
    RenderingTemplateRecord,
    RenderingTemplateVersionRecord,
    RoutingActionRecord,
    RoutingEvaluationRecord,
    RoutingPolicyRecord,
    RoutingPolicyVersionRecord,
    RoutingRuleRecord,
    SourceChannel,
    SourceChannelProfile,
    SourceChannelProfileVersion,
    StageRunRecord,
    TelegramIdentity,
    VideoAssetRecord,
    VideoFrameRecord,
)
from .operations_repository import SqlAlchemyOperationsBacklogRepository
from .pipeline_repository import SqlAlchemyMessagePipelineRepository
from .review_repository import SqlAlchemyReviewRepository
from .routing_repository import SqlAlchemyRoutingRepository
from .session import AsyncDatabase


class SqlAlchemyAdminBackend:
    """PostgreSQL implementation of AdminSessionRepository and AdminWorkspaceBackend."""

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        operations_monitor: OperationsMonitor | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._database = database
        self._monitor = operations_monitor or OperationsMonitor()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4
        self._review_service = ReviewService(
            SqlAlchemyReviewRepository(database),
            clock=self._clock,
            id_factory=self._id_factory,
        )
        self._routing_service = RoutingService(
            SqlAlchemyRoutingRepository(database),
            id_factory=self._id_factory,
        )
        self._cleanup_service = ArchiveCleanupService(
            repository=SqlAlchemyArchiveCleanupRepository(database)
        )
        self._backlog_repo = SqlAlchemyOperationsBacklogRepository(database)
        self._pipeline_repo = SqlAlchemyMessagePipelineRepository(database)

    # --- AdminSessionRepository ---

    async def find_active_admin_by_username(self, *, username: str) -> AdminAccountRecord | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(AdminUser).where(
                        AdminUser.username == username,
                        AdminUser.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return AdminAccountRecord(
                user_id=str(row.id),
                username=row.username,
                password_hash=row.password_hash,
                is_active=row.is_active,
            )

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
        async with self._database.session() as session:
            async with session.begin():
                user = await session.get(AdminUser, UUID(admin_user_id))
                if user is None:
                    raise AdminNotFoundError("admin user does not exist")
                record = AdminSessionRecord(
                    id=UUID(session_id),
                    admin_user_id=user.id,
                    session_token_hash=session_token_hash,
                    csrf_token_hash=csrf_token_hash,
                    expires_at=expires_at,
                    last_seen_at=now,
                    revoked_at=None,
                    user_agent=user_agent,
                    ip_address=ip_address,
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
                await session.flush()
                return AdminSessionState(
                    session_id=str(record.id),
                    admin_user_id=str(record.admin_user_id),
                    username=user.username,
                    session_token_hash=record.session_token_hash,
                    csrf_token_hash=record.csrf_token_hash,
                    expires_at=record.expires_at,
                    last_seen_at=record.last_seen_at,
                    created_at=record.created_at,
                    revoked_at=record.revoked_at,
                    user_agent=record.user_agent,
                    ip_address=record.ip_address,
                )

    async def get_session_by_token_hash(
        self,
        *,
        session_token_hash: str,
    ) -> AdminSessionState | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(AdminSessionRecord, AdminUser.username)
                    .join(AdminUser, AdminUser.id == AdminSessionRecord.admin_user_id)
                    .where(
                        AdminSessionRecord.session_token_hash == session_token_hash,
                        AdminUser.is_active.is_(True),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            rec, username = row
            return AdminSessionState(
                session_id=str(rec.id),
                admin_user_id=str(rec.admin_user_id),
                username=username,
                session_token_hash=rec.session_token_hash,
                csrf_token_hash=rec.csrf_token_hash,
                expires_at=rec.expires_at,
                last_seen_at=rec.last_seen_at,
                created_at=rec.created_at,
                revoked_at=rec.revoked_at,
                user_agent=rec.user_agent,
                ip_address=rec.ip_address,
            )

    async def touch_session(self, *, session_id: str, now: datetime) -> None:
        async with self._database.session() as session:
            async with session.begin():
                rec = await session.get(AdminSessionRecord, UUID(session_id))
                if rec is not None:
                    rec.last_seen_at = now
                    rec.updated_at = now

    async def revoke_session(self, *, session_id: str, now: datetime) -> None:
        async with self._database.session() as session:
            async with session.begin():
                rec = await session.get(AdminSessionRecord, UUID(session_id))
                if rec is not None and rec.revoked_at is None:
                    rec.revoked_at = now
                    rec.updated_at = now

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
        event_uid = self._id_factory()
        async with self._database.session() as session:
            async with session.begin():
                session.add(
                    AuditEvent(
                        id=event_uid,
                        actor_admin_user_id=(
                            UUID(actor_admin_user_id) if actor_admin_user_id else None
                        ),
                        event_type=event_type,
                        entity_type=entity_type,
                        entity_id=_try_uuid(entity_id),
                        payload=sanitize_audit_payload(payload),
                        created_at=now,
                    )
                )
        return str(event_uid)

    # --- Identities & Channels ---

    async def list_identities(self) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(TelegramIdentity).order_by(TelegramIdentity.created_at.desc())
                )
            ).scalars()
            return [_serialize_identity(r) for r in rows]

    async def get_identity(self, *, identity_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = await session.get(TelegramIdentity, UUID(identity_id))
            return _serialize_identity(row) if row is not None else None

    async def create_identity(
        self,
        *,
        name: str,
        identity_type: str,
        secret_id: str,
        enabled: bool,
        now: datetime,
    ) -> dict[str, Any]:
        normalized = name.strip() if isinstance(name, str) else ""
        if not normalized:
            raise AdminValidationError("identity name must not be blank")
        if identity_type not in {"mtproto_user", "bot"}:
            raise AdminValidationError("identity_type must be mtproto_user or bot")
        async with self._database.session() as session:
            try:
                async with session.begin():
                    rec = TelegramIdentity(
                        id=self._id_factory(),
                        name=normalized,
                        identity_type=identity_type,
                        enabled=bool(enabled),
                        health_status="unknown",
                        secret_id=UUID(secret_id),
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(rec)
                    await session.flush()
                    return _serialize_identity(rec)
            except IntegrityError as exc:
                raise AdminConflictError("telegram identity already exists") from exc

    async def update_identity(
        self,
        *,
        identity_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._database.session() as session:
            try:
                async with session.begin():
                    rec = await session.get(
                        TelegramIdentity, UUID(identity_id), with_for_update=True
                    )
                    if rec is None:
                        raise AdminNotFoundError("telegram identity does not exist")
                    before = _serialize_identity(rec)
                    if "name" in changes and changes["name"] is not None:
                        nm = str(changes["name"]).strip()
                        if not nm:
                            raise AdminValidationError("identity name must not be blank")
                        rec.name = nm
                    if "enabled" in changes and changes["enabled"] is not None:
                        rec.enabled = bool(changes["enabled"])
                    if "health_status" in changes and changes["health_status"] is not None:
                        hs = str(changes["health_status"])
                        if hs not in {"unknown", "healthy", "degraded", "failed"}:
                            raise AdminValidationError("invalid health_status")
                        rec.health_status = hs
                    if "last_error" in changes:
                        rec.last_error = (
                            str(changes["last_error"])
                            if changes["last_error"] is not None
                            else None
                        )
                    if "secret_id" in changes and changes["secret_id"] is not None:
                        rec.secret_id = UUID(str(changes["secret_id"]))
                    rec.updated_at = now
                    await session.flush()
                    return before, _serialize_identity(rec)
            except IntegrityError as exc:
                raise AdminConflictError("telegram identity update conflicted") from exc

    async def list_source_channels(self) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(SourceChannel).order_by(SourceChannel.created_at.desc())
                )
            ).scalars()
            return [_serialize_source_channel(r) for r in rows]

    async def get_source_channel(self, *, channel_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = await session.get(SourceChannel, UUID(channel_id))
            return _serialize_source_channel(row) if row is not None else None

    async def create_source_channel(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        peer_id = payload.get("telegram_peer_id")
        if not isinstance(peer_id, int) or isinstance(peer_id, bool) or peer_id == 0:
            raise AdminValidationError("telegram_peer_id must be a non-zero integer")
        display_name = str(payload.get("display_name", "")).strip()
        if not display_name:
            raise AdminValidationError("display_name must not be blank")
        read_id = _require_uuid(payload.get("default_read_identity_id"), "default_read_identity_id")
        active_vid = _try_uuid(payload.get("active_profile_version_id"))
        async with self._database.session() as session:
            try:
                async with session.begin():
                    if active_vid is not None:
                        ver = await session.get(SourceChannelProfileVersion, active_vid)
                        if ver is None or ver.state != "published":
                            raise AdminValidationError(
                                "active_profile_version_id must reference a published version"
                            )
                    rec = SourceChannel(
                        id=self._id_factory(),
                        telegram_peer_id=peer_id,
                        username=payload.get("username"),
                        display_name=display_name,
                        default_read_identity_id=read_id,
                        active_profile_version_id=active_vid,
                        enabled=bool(payload.get("enabled", True)),
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(rec)
                    await session.flush()
                    return _serialize_source_channel(rec)
            except IntegrityError as exc:
                raise AdminConflictError("source channel conflict") from exc

    async def update_source_channel(
        self,
        *,
        channel_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._database.session() as session:
            try:
                async with session.begin():
                    rec = await session.get(SourceChannel, UUID(channel_id), with_for_update=True)
                    if rec is None:
                        raise AdminNotFoundError("source channel does not exist")
                    before = _serialize_source_channel(rec)
                    if "display_name" in changes and changes["display_name"] is not None:
                        dn = str(changes["display_name"]).strip()
                        if not dn:
                            raise AdminValidationError("display_name must not be blank")
                        rec.display_name = dn
                    if "username" in changes:
                        rec.username = changes["username"]
                    if (
                        "default_read_identity_id" in changes
                        and changes["default_read_identity_id"] is not None
                    ):
                        rec.default_read_identity_id = _require_uuid(
                            changes["default_read_identity_id"], "default_read_identity_id"
                        )
                    if "active_profile_version_id" in changes:
                        avid = _try_uuid(changes["active_profile_version_id"])
                        if avid is not None:
                            ver = await session.get(SourceChannelProfileVersion, avid)
                            if ver is None or ver.state != "published":
                                raise AdminValidationError(
                                    "active_profile_version_id must be published"
                                )
                        rec.active_profile_version_id = avid
                    if "enabled" in changes and changes["enabled"] is not None:
                        rec.enabled = bool(changes["enabled"])
                    rec.updated_at = now
                    await session.flush()
                    return before, _serialize_source_channel(rec)
            except IntegrityError as exc:
                raise AdminConflictError("source channel update conflicted") from exc

    async def list_destination_channels(self) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(DestinationChannel).order_by(DestinationChannel.created_at.desc())
                )
            ).scalars()
            return [_serialize_destination_channel(r) for r in rows]

    async def get_destination_channel(self, *, channel_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = await session.get(DestinationChannel, UUID(channel_id))
            return _serialize_destination_channel(row) if row is not None else None

    async def create_destination_channel(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        peer_id = payload.get("telegram_peer_id")
        if not isinstance(peer_id, int) or isinstance(peer_id, bool) or peer_id == 0:
            raise AdminValidationError("telegram_peer_id must be a non-zero integer")
        name = str(payload.get("name", "")).strip()
        if not name:
            raise AdminValidationError("destination channel name must not be blank")
        pub_id = _require_uuid(
            payload.get("default_publish_identity_id"), "default_publish_identity_id"
        )
        async with self._database.session() as session:
            try:
                async with session.begin():
                    rec = DestinationChannel(
                        id=self._id_factory(),
                        name=name,
                        telegram_peer_id=peer_id,
                        username=payload.get("username"),
                        default_publish_identity_id=pub_id,
                        enabled=bool(payload.get("enabled", True)),
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(rec)
                    await session.flush()
                    return _serialize_destination_channel(rec)
            except IntegrityError as exc:
                raise AdminConflictError("destination channel conflict") from exc

    async def update_destination_channel(
        self,
        *,
        channel_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._database.session() as session:
            try:
                async with session.begin():
                    rec = await session.get(
                        DestinationChannel, UUID(channel_id), with_for_update=True
                    )
                    if rec is None:
                        raise AdminNotFoundError("destination channel does not exist")
                    before = _serialize_destination_channel(rec)
                    if "name" in changes and changes["name"] is not None:
                        nm = str(changes["name"]).strip()
                        if not nm:
                            raise AdminValidationError("name must not be blank")
                        rec.name = nm
                    if "username" in changes:
                        rec.username = changes["username"]
                    if (
                        "default_publish_identity_id" in changes
                        and changes["default_publish_identity_id"] is not None
                    ):
                        rec.default_publish_identity_id = _require_uuid(
                            changes["default_publish_identity_id"], "default_publish_identity_id"
                        )
                    if "enabled" in changes and changes["enabled"] is not None:
                        rec.enabled = bool(changes["enabled"])
                    rec.updated_at = now
                    await session.flush()
                    return before, _serialize_destination_channel(rec)
            except IntegrityError as exc:
                raise AdminConflictError("destination channel update conflicted") from exc

    # --- Processing Ranges ---

    async def list_processing_ranges(
        self,
        *,
        source_channel_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            stmt = select(ProcessingRangeRecord).order_by(ProcessingRangeRecord.created_at.desc())
            if source_channel_id is not None:
                stmt = stmt.where(
                    ProcessingRangeRecord.source_channel_id
                    == _require_uuid(source_channel_id, "source_channel_id")
                )
            if status is not None:
                stmt = stmt.where(ProcessingRangeRecord.status == status)
            rows = (await session.execute(stmt)).scalars()
            return [_serialize_range(r) for r in rows]

    async def get_processing_range(self, *, range_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = await session.get(ProcessingRangeRecord, UUID(range_id))
            return _serialize_range(row) if row is not None else None

    async def create_processing_range(
        self,
        *,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        sc_id = _require_uuid(payload.get("source_channel_id"), "source_channel_id")
        start_at = _parse_iso_dt(payload.get("start_at"), field="start_at")
        end_mode = str(payload.get("end_mode", "")).strip().lower()
        if end_mode not in {"fixed", "latest"}:
            raise AdminValidationError("end_mode must be fixed or latest")
        end_at = (
            _parse_iso_dt(payload.get("end_at"), field="end_at") if end_mode == "fixed" else None
        )
        steady_after = (
            int(payload.get("steady_after_seconds", 60)) if end_mode == "latest" else None
        )
        resolved_start = payload.get("resolved_start_message_id")
        resolved_end = payload.get("resolved_fixed_end_message_id")
        watermark = (
            resolved_start - 1 if isinstance(resolved_start, int) and resolved_start > 0 else None
        )
        enabled = bool(payload.get("enabled", True))
        async with self._database.session() as session:
            try:
                async with session.begin():
                    rec = ProcessingRangeRecord(
                        id=self._id_factory(),
                        source_channel_id=sc_id,
                        start_at=start_at,
                        end_mode=end_mode,
                        end_at=end_at,
                        steady_after_seconds=steady_after,
                        resolved_start_message_id=resolved_start,
                        resolved_fixed_end_message_id=resolved_end,
                        processing_watermark_message_id=watermark,
                        active_high_watermark_message_id=None,
                        status="active" if enabled else "paused",
                        enabled=enabled,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(rec)
                    await session.flush()
                    return _serialize_range(rec)
            except IntegrityError as exc:
                raise AdminValidationError("invalid processing range parameters") from exc

    async def update_processing_range(
        self,
        *,
        range_id: str,
        changes: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._database.session() as session:
            async with session.begin():
                rec = await session.get(ProcessingRangeRecord, UUID(range_id), with_for_update=True)
                if rec is None:
                    raise AdminNotFoundError("processing range does not exist")
                before = _serialize_range(rec)
                if "enabled" in changes and changes["enabled"] is not None:
                    enabled = bool(changes["enabled"])
                    rec.enabled = enabled
                    if not enabled and rec.status in {"pending", "active"}:
                        rec.status = "paused"
                    elif enabled and rec.status == "paused":
                        rec.status = "active"
                if "status" in changes and changes["status"] is not None:
                    rec.status = str(changes["status"])
                    rec.enabled = rec.status in {"pending", "active"}
                rec.updated_at = now
                await session.flush()
                return before, _serialize_range(rec)

    async def list_range_executions(self, *, range_id: str) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(RangeExecutionRecord)
                    .where(RangeExecutionRecord.processing_range_id == UUID(range_id))
                    .order_by(RangeExecutionRecord.from_message_id_exclusive.desc())
                )
            ).scalars()
            return [_serialize_execution(r) for r in rows]

    # --- Versioned Configurations ---

    async def list_config_entities(self, *, family: str) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            parent_cls = _FAMILY_PARENTS[family]
            parents = (
                await session.execute(select(parent_cls).order_by(parent_cls.created_at.desc()))
            ).scalars()
            return [await self._load_serialized_entity(session, family, p) for p in parents]

    async def get_config_entity(self, *, family: str, entity_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            parent_cls = _FAMILY_PARENTS[family]
            parent = await session.get(parent_cls, UUID(entity_id))
            if parent is None:
                return None
            return await self._load_serialized_entity(session, family, parent)

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
        parent_cls = _FAMILY_PARENTS[family]
        ent_id = self._id_factory()
        async with self._database.session() as session:
            try:
                async with session.begin():
                    kwargs: dict[str, Any] = {
                        "id": ent_id,
                        "name": name,
                        "created_at": now,
                        "updated_at": now,
                    }
                    if family == "source-channel-profiles":
                        kwargs["source_channel_id"] = _require_uuid(
                            payload.get("source_channel_id"), "source_channel_id"
                        )
                    parent = parent_cls(**kwargs)
                    session.add(parent)
                    await session.flush()
            except IntegrityError as exc:
                raise AdminConflictError(f"{family} {name!r} already exists") from exc

        initial = payload.get("initial_version")
        if isinstance(initial, Mapping):
            await self.create_config_version(
                family=family,
                entity_id=str(ent_id),
                content=initial,
                now=now,
            )
        ent = await self.get_config_entity(family=family, entity_id=str(ent_id))
        assert ent is not None
        return ent

    async def create_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        content: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            async with session.begin():
                parent = await session.get(_FAMILY_PARENTS[family], UUID(entity_id))
                if parent is None:
                    raise AdminNotFoundError(f"{family} entity does not exist")
                ver_cls, fk_col = _FAMILY_VERSIONS[family]
                max_ver = (
                    await session.execute(
                        select(func.coalesce(func.max(ver_cls.version_number), 0)).where(
                            getattr(ver_cls, fk_col) == parent.id
                        )
                    )
                ).scalar_one()
                version_id = self._id_factory()
                version_number = int(max_ver) + 1
                await self._validate_db_version(
                    session=session,
                    family=family,
                    version_id=str(version_id),
                    entity_id=entity_id,
                    version_number=version_number,
                    state="draft",
                    content=content,
                    require_published_refs=False,
                )
                ver = await self._insert_db_version(
                    session=session,
                    family=family,
                    version_id=version_id,
                    entity_id=UUID(entity_id),
                    version_number=version_number,
                    content=content,
                    now=now,
                )
                return await self._serialize_db_version(session, family, ver)

    async def update_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        content: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        ver_cls, fk_col = _FAMILY_VERSIONS[family]
        async with self._database.session() as session:
            async with session.begin():
                ver = await session.get(ver_cls, UUID(version_id), with_for_update=True)
                if ver is None or str(getattr(ver, fk_col)) != entity_id:
                    raise AdminNotFoundError(f"{family} version does not exist")
                if ver.state != "draft":
                    raise AdminConflictError(
                        "published or archived configuration versions are immutable"
                    )
                await self._validate_db_version(
                    session=session,
                    family=family,
                    version_id=version_id,
                    entity_id=entity_id,
                    version_number=ver.version_number,
                    state="draft",
                    content=content,
                    require_published_refs=False,
                )
                await self._apply_db_version_update(session, family, ver, content, now)
                return await self._serialize_db_version(session, family, ver)

    async def publish_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        supersede_published: bool,
        now: datetime,
    ) -> dict[str, Any]:
        ver_cls, fk_col = _FAMILY_VERSIONS[family]
        async with self._database.session() as session:
            async with session.begin():
                ver = await session.get(ver_cls, UUID(version_id), with_for_update=True)
                if ver is None or str(getattr(ver, fk_col)) != entity_id:
                    raise AdminNotFoundError(f"{family} version does not exist")
                if ver.state != "draft":
                    raise AdminConflictError("only a draft configuration version can be published")
                serialized = await self._serialize_db_version(session, family, ver)
                await self._validate_db_version(
                    session=session,
                    family=family,
                    version_id=version_id,
                    entity_id=entity_id,
                    version_number=ver.version_number,
                    state="published",
                    content=serialized["content"],
                    require_published_refs=True,
                )
                existing_pub = (
                    await session.execute(
                        select(ver_cls)
                        .where(
                            getattr(ver_cls, fk_col) == UUID(entity_id),
                            ver_cls.state == "published",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing_pub is not None:
                    if not supersede_published:
                        raise AdminConflictError("another version is already published")
                    if family == "source-channel-profiles":
                        existing_pub.state = "retired"
                    else:
                        existing_pub.state = "archived"
                        existing_pub.archived_at = now
                        if hasattr(type(existing_pub), "updated_at"):
                            existing_pub.updated_at = now
                    await session.flush()

                ver.state = "published"
                ver.published_at = now
                if hasattr(type(ver), "updated_at"):
                    ver.updated_at = now
                await session.flush()
                return await self._serialize_db_version(session, family, ver)

    async def archive_config_version(
        self,
        *,
        family: str,
        entity_id: str,
        version_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        ver_cls, fk_col = _FAMILY_VERSIONS[family]
        async with self._database.session() as session:
            async with session.begin():
                ver = await session.get(ver_cls, UUID(version_id), with_for_update=True)
                if ver is None or str(getattr(ver, fk_col)) != entity_id:
                    raise AdminNotFoundError(f"{family} version does not exist")
                if ver.state != "published":
                    raise AdminConflictError(
                        "only a published configuration version can be archived"
                    )
                if family == "source-channel-profiles":
                    ver.state = "retired"
                else:
                    ver.state = "archived"
                    ver.archived_at = now
                    if hasattr(type(ver), "updated_at"):
                        ver.updated_at = now
                await session.flush()
                return await self._serialize_db_version(session, family, ver)

    # --- Prompt Test Bench ---

    async def prepare_test_bench_inputs(
        self,
        *,
        payload: Mapping[str, Any],
    ) -> tuple[
        AnalysisStageTemplateVersion,
        tuple[AnalysisTargetInput, ...],
        Mapping[str, Any] | None,
    ]:
        async with self._database.session() as session:
            stage_vid = payload.get("stage_template_version_id") or payload.get("stage_version_id")
            if isinstance(stage_vid, str) and stage_vid.strip():
                stage = await self._load_stage_domain(session, stage_vid.strip())
            else:
                inline = payload.get("stage")
                if not isinstance(inline, Mapping):
                    raise AdminValidationError(
                        "provide stage_template_version_id or inline stage specification"
                    )
                prompt = await self._load_prompt_domain(
                    session, str(inline.get("prompt_template_version_id", ""))
                )
                lset = await self._load_label_set_domain(
                    session, str(inline.get("label_set_version_id", ""))
                )
                prof = await self._load_inference_profile_domain(
                    session, str(inline.get("inference_profile_version_id", ""))
                )
                stage = build_stage_template_domain(
                    version_id=str(self._id_factory()),
                    entity_id=str(self._id_factory()),
                    version_number=1,
                    state="draft",
                    content=inline,
                    resolve_prompt=lambda _: prompt,
                    resolve_label_set=lambda _: lset,
                    resolve_inference_profile=lambda _: prof,
                    require_published_refs=False,
                )

            message_id = payload.get("message_id")
            if isinstance(message_id, str) and message_id.strip():
                msg = await session.get(MessageRecord, _require_uuid(message_id, "message_id"))
                if msg is None:
                    raise AdminNotFoundError("message does not exist")
                sc = await session.get(SourceChannel, msg.source_channel_id)
                source_ctx = (
                    {
                        "source_channel_id": str(sc.id),
                        "display_name": sc.display_name,
                        "username": sc.username,
                    }
                    if sc is not None
                    else None
                )
                media_desc = await self._load_media_descriptors(session, msg.id)
                if stage.target_scope is LabelScope.GLOBAL:
                    targets = (
                        AnalysisTargetInput(
                            target_id=str(msg.id),
                            message_id=str(msg.id),
                            text=msg.original_text,
                            telegram_metadata=msg.telegram_metadata,
                            media=tuple(media_desc),
                        ),
                    )
                else:
                    asset_id = payload.get("asset_id")
                    if asset_id is not None:
                        media_desc = [m for m in media_desc if m["asset_id"] == str(asset_id)]
                    if not media_desc:
                        raise AdminValidationError("no matching media assets on message")
                    targets = tuple(
                        AnalysisTargetInput(
                            target_id=str(m["asset_id"]),
                            message_id=str(msg.id),
                            asset_id=str(m["asset_id"]),
                            text=msg.original_text,
                            telegram_metadata=msg.telegram_metadata,
                            media=(m,),
                        )
                        for m in media_desc
                    )
                return stage, targets, source_ctx

            sample_id = str(payload.get("sample_target_id") or self._id_factory())
            sample_media = payload.get("sample_media") or []
            return (
                stage,
                (
                    AnalysisTargetInput(
                        target_id=sample_id,
                        message_id=sample_id,
                        asset_id=sample_id if stage.target_scope is LabelScope.MEDIA else None,
                        text=str(payload.get("sample_text", "")),
                        telegram_metadata={},
                        media=tuple(sample_media) if isinstance(sample_media, list) else (),
                    ),
                ),
                None,
            )

    # --- Messages, Review, Reanalysis, Reroute & Skip ---

    async def list_messages(
        self,
        *,
        filters: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]:
        token = decode_cursor(cursor) if cursor else None
        async with self._database.session() as session:
            stmt = select(MessageRecord)
            if filters.get("source_channel_id"):
                stmt = stmt.where(
                    MessageRecord.source_channel_id
                    == _require_uuid(filters["source_channel_id"], "source_channel_id")
                )
            if filters.get("published_from"):
                stmt = stmt.where(
                    MessageRecord.published_at
                    >= _parse_iso_dt(filters["published_from"], field="published_from")
                )
            if filters.get("published_before"):
                stmt = stmt.where(
                    MessageRecord.published_at
                    < _parse_iso_dt(filters["published_before"], field="published_before")
                )
            if filters.get("processing_status"):
                stmt = stmt.where(MessageRecord.processing_status == filters["processing_status"])
            if filters.get("review_status"):
                stmt = stmt.where(MessageRecord.review_status == filters["review_status"])
            if filters.get("duplicate_status"):
                stmt = stmt.where(MessageRecord.duplicate_status == filters["duplicate_status"])
            if filters.get("blocked_from_analysis") is not None:
                stmt = stmt.where(
                    MessageRecord.blocked_from_analysis.is_(bool(filters["blocked_from_analysis"]))
                )
            if filters.get("negative_label") is not None:
                neg_clause = exists(
                    select(ModelLabelAssignmentRecord.id).where(
                        ModelLabelAssignmentRecord.message_id == MessageRecord.id,
                        ModelLabelAssignmentRecord.activated.is_(True),
                        ModelLabelAssignmentRecord.negative.is_(True),
                    )
                )
                stmt = stmt.where(
                    or_(MessageRecord.blocked_by_stage_run_id.is_not(None), neg_clause)
                    if bool(filters["negative_label"])
                    else and_(MessageRecord.blocked_by_stage_run_id.is_(None), ~neg_clause)
                )
            if filters.get("pipeline_version_id"):
                pv_uid = _require_uuid(filters["pipeline_version_id"], "pipeline_version_id")
                stmt = stmt.where(
                    exists(
                        select(AnalysisRunRecord.id).where(
                            AnalysisRunRecord.message_id == MessageRecord.id,
                            AnalysisRunRecord.analysis_pipeline_version_id == pv_uid,
                        )
                    )
                )
            if filters.get("routing_policy_version_id"):
                rpv_uid = _require_uuid(
                    filters["routing_policy_version_id"], "routing_policy_version_id"
                )
                stmt = stmt.where(
                    exists(
                        select(RoutingEvaluationRecord.id).where(
                            RoutingEvaluationRecord.message_id == MessageRecord.id,
                            RoutingEvaluationRecord.routing_policy_version_id == rpv_uid,
                        )
                    )
                )
            if filters.get("destination_channel_id"):
                dest_uid = _require_uuid(
                    filters["destination_channel_id"], "destination_channel_id"
                )
                stmt = stmt.where(
                    exists(
                        select(PublicationIntentRecord.id).where(
                            PublicationIntentRecord.message_id == MessageRecord.id,
                            PublicationIntentRecord.destination_channel_id == dest_uid,
                        )
                    )
                )
            if filters.get("archive_state"):
                target_as = str(filters["archive_state"])
                stmt = stmt.where(
                    or_(
                        exists(
                            select(ImageAssetRecord.id).where(
                                ImageAssetRecord.message_id == MessageRecord.id,
                                ImageAssetRecord.archive_state == target_as,
                            )
                        ),
                        exists(
                            select(VideoAssetRecord.id).where(
                                VideoAssetRecord.message_id == MessageRecord.id,
                                VideoAssetRecord.archive_state == target_as,
                            )
                        ),
                    )
                )
            if filters.get("label_key"):
                stmt = stmt.where(
                    _current_label_filter(
                        label_key=str(filters["label_key"]),
                        namespace=str(filters.get("label_namespace") or "effective"),
                        scope=filters.get("label_scope"),
                    )
                )
            if filters.get("query"):
                stmt = stmt.where(MessageRecord.original_text.ilike(f"%{filters['query']}%"))
            if token is not None:
                stmt = stmt.where(
                    or_(
                        MessageRecord.published_at < token.timestamp,
                        and_(
                            MessageRecord.published_at == token.timestamp,
                            MessageRecord.id < UUID(token.entity_id),
                        ),
                    )
                )
            stmt = stmt.order_by(MessageRecord.published_at.desc(), MessageRecord.id.desc()).limit(
                limit + 1
            )
            rows = list((await session.execute(stmt)).scalars())
            has_more = len(rows) > limit
            items = [_serialize_message(r) for r in rows[:limit]]
            next_cursor = (
                encode_cursor(timestamp=rows[limit - 1].published_at, entity_id=rows[limit - 1].id)
                if has_more and items
                else None
            )
            return CursorPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    async def get_message_detail(self, *, message_id: str) -> dict[str, Any] | None:
        msg_uid = UUID(message_id)
        async with self._database.session() as session:
            msg = await session.get(MessageRecord, msg_uid)
            if msg is None:
                return None
            parts = list(
                (
                    await session.execute(
                        select(MessagePartRecord)
                        .where(MessagePartRecord.message_id == msg_uid)
                        .order_by(MessagePartRecord.telegram_message_id.asc())
                    )
                ).scalars()
            )
            images = list(
                (
                    await session.execute(
                        select(ImageAssetRecord).where(ImageAssetRecord.message_id == msg_uid)
                    )
                ).scalars()
            )
            videos = list(
                (
                    await session.execute(
                        select(VideoAssetRecord).where(VideoAssetRecord.message_id == msg_uid)
                    )
                ).scalars()
            )
            serialized_videos = []
            for vid in videos:
                frames = list(
                    (
                        await session.execute(
                            select(VideoFrameRecord)
                            .where(VideoFrameRecord.video_asset_id == vid.id)
                            .order_by(VideoFrameRecord.frame_index.asc())
                        )
                    ).scalars()
                )
                serialized_videos.append(_serialize_video(vid, frames))

            dup = (
                await session.execute(
                    select(DuplicateMatchRecord).where(DuplicateMatchRecord.message_id == msg_uid)
                )
            ).scalar_one_or_none()

            runs = list(
                (
                    await session.execute(
                        select(AnalysisRunRecord)
                        .where(AnalysisRunRecord.message_id == msg_uid)
                        .order_by(AnalysisRunRecord.created_at.asc())
                    )
                ).scalars()
            )
            serialized_runs = []
            for run in runs:
                sruns = list(
                    (
                        await session.execute(
                            select(StageRunRecord)
                            .where(StageRunRecord.analysis_run_id == run.id)
                            .order_by(StageRunRecord.created_at.asc())
                        )
                    ).scalars()
                )
                srun_list = []
                for sr in sruns:
                    man = (
                        await session.get(InputManifestRecord, sr.input_manifest_id)
                        if sr.input_manifest_id
                        else None
                    )
                    call = (
                        await session.get(InferenceCallRecord, sr.inference_call_id)
                        if sr.inference_call_id
                        else None
                    )
                    srun_list.append(_serialize_stage_run(sr, man, call))
                serialized_runs.append(_serialize_analysis_run(run, srun_list))

            model_rows = list(
                (
                    await session.execute(
                        select(ModelLabelAssignmentRecord)
                        .where(ModelLabelAssignmentRecord.message_id == msg_uid)
                        .order_by(
                            ModelLabelAssignmentRecord.created_at.asc(),
                            ModelLabelAssignmentRecord.id.asc(),
                        )
                    )
                ).scalars()
            )
            manual_rows = list(
                (
                    await session.execute(
                        select(ManualLabelAssignmentRecord)
                        .where(ManualLabelAssignmentRecord.message_id == msg_uid)
                        .order_by(
                            ManualLabelAssignmentRecord.created_at.asc(),
                            ManualLabelAssignmentRecord.id.asc(),
                        )
                    )
                ).scalars()
            )
            # Match routing's active facts while retaining model_rows as audit history.
            current_model_run = max(
                (
                    r
                    for r in runs
                    if r.run_mode in {"formal", "reanalysis"}
                    and r.status in {"completed", "blocked_negative_gate"}
                ),
                key=lambda r: (r.created_at, r.id),
                default=None,
            )
            model_facts = tuple(
                LabelValue(
                    message_id=str(r.message_id),
                    target_scope=r.target_scope,
                    target_kind=r.target_kind,
                    target_id=str(r.target_id),
                    image_asset_id=str(r.image_asset_id) if r.image_asset_id else None,
                    video_asset_id=str(r.video_asset_id) if r.video_asset_id else None,
                    label_definition_version_id=str(r.label_definition_version_id),
                    label_key=r.label_key,
                    score=r.score,
                    activated=r.activated,
                )
                for r in model_rows
                if current_model_run is not None and r.analysis_run_id == current_model_run.id
            )
            manual_evts = tuple(
                ManualLabelEvent(
                    event_id=str(r.id),
                    message_id=str(r.message_id),
                    target_scope=r.target_scope,
                    target_kind=r.target_kind,
                    target_id=str(r.target_id),
                    image_asset_id=str(r.image_asset_id) if r.image_asset_id else None,
                    video_asset_id=str(r.video_asset_id) if r.video_asset_id else None,
                    label_definition_version_id=str(r.label_definition_version_id),
                    label_key=r.label_key,
                    operation=ManualLabelOperation(r.operation),
                    score=r.score,
                    activated=r.activated,
                    actor_admin_user_id=str(r.actor_admin_user_id)
                    if r.actor_admin_user_id
                    else None,
                    created_at=r.created_at,
                    reason=r.reason,
                )
                for r in manual_rows
            )
            resolved = labels_to_routing_snapshot(
                resolve_label_namespaces(model_facts, manual_evts)
            )
            rev_rows = list(
                (
                    await session.execute(
                        select(MessageReviewEventRecord)
                        .where(MessageReviewEventRecord.message_id == msg_uid)
                        .order_by(MessageReviewEventRecord.created_at.asc())
                    )
                ).scalars()
            )
            eval_rows = list(
                (
                    await session.execute(
                        select(RoutingEvaluationRecord)
                        .where(RoutingEvaluationRecord.message_id == msg_uid)
                        .order_by(RoutingEvaluationRecord.created_at.asc())
                    )
                ).scalars()
            )
            intent_rows = list(
                (
                    await session.execute(
                        select(PublicationIntentRecord)
                        .where(PublicationIntentRecord.message_id == msg_uid)
                        .order_by(PublicationIntentRecord.created_at.asc())
                    )
                ).scalars()
            )
            serialized_intents = []
            for pi in intent_rows:
                atts = list(
                    (
                        await session.execute(
                            select(PublishAttemptRecord)
                            .where(PublishAttemptRecord.publication_intent_id == pi.id)
                            .order_by(PublishAttemptRecord.attempt_number.asc())
                        )
                    ).scalars()
                )
                recs = list(
                    (
                        await session.execute(
                            select(PublishRecord)
                            .where(PublishRecord.publication_intent_id == pi.id)
                            .order_by(PublishRecord.sent_at.asc())
                        )
                    ).scalars()
                )
                serialized_intents.append(_serialize_intent(pi, atts, recs))

            audits = list(
                (
                    await session.execute(
                        select(AuditEvent)
                        .where(AuditEvent.entity_id == msg_uid)
                        .order_by(AuditEvent.created_at.desc())
                    )
                ).scalars()
            )

            return {
                "message": _serialize_message(msg),
                "parts": [_serialize_part(p) for p in parts],
                "media": {
                    "images": [_serialize_image(i) for i in images],
                    "videos": serialized_videos,
                },
                "duplicate_match": (
                    {
                        "id": str(dup.id),
                        "canonical_message_id": str(dup.canonical_message_id),
                        "match_kind": dup.match_kind,
                        "hamming_distance": dup.hamming_distance,
                        "duplicate_policy": dup.duplicate_policy,
                        "created_at": dup.created_at.isoformat(),
                    }
                    if dup
                    else None
                ),
                "analysis_runs": serialized_runs,
                "labels": {
                    "model": [
                        {
                            "assignment_id": str(r.id),
                            "stage_run_id": str(r.stage_run_id),
                            "analysis_run_id": str(r.analysis_run_id),
                            "target_scope": r.target_scope,
                            "target_kind": r.target_kind,
                            "target_id": str(r.target_id),
                            "label_definition_version_id": str(r.label_definition_version_id),
                            "label_key": r.label_key,
                            "score": r.score,
                            "activated": r.activated,
                            "negative": r.negative,
                            "reason": r.reason,
                            "evidence": r.evidence,
                        }
                        for r in model_rows
                    ],
                    "manual_events": [
                        {
                            "event_id": e.event_id,
                            "message_id": e.message_id,
                            "target_scope": e.target_scope,
                            "target_kind": e.target_kind,
                            "target_id": e.target_id,
                            "label_definition_version_id": e.label_definition_version_id,
                            "label_key": e.label_key,
                            "operation": e.operation.value,
                            "score": e.score,
                            "activated": e.activated,
                            "actor_admin_user_id": e.actor_admin_user_id,
                            "reason": e.reason,
                            "created_at": e.created_at.isoformat(),
                        }
                        for e in manual_evts
                    ],
                    "namespaces": resolved,
                },
                "review_events": [
                    {
                        "event_id": str(r.id),
                        "message_id": str(r.message_id),
                        "old_status": r.old_status,
                        "new_status": r.new_status,
                        "actor_admin_user_id": (
                            str(r.actor_admin_user_id) if r.actor_admin_user_id else None
                        ),
                        "reason": r.reason,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in rev_rows
                ],
                "routing_evaluations": [_serialize_evaluation(ev) for ev in eval_rows],
                "publication_intents": serialized_intents,
                "audit_events": [_serialize_audit(a) for a in audits],
            }

    async def apply_manual_label(
        self,
        *,
        message_id: str,
        payload: Mapping[str, Any],
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        op_raw = str(payload.get("operation", "set")).strip().lower()
        try:
            if op_raw == "set":
                ev = await self._review_service.set_label(
                    message_id=message_id,
                    target_scope=str(payload.get("target_scope", "global")),
                    target_kind=str(payload.get("target_kind", "message")),
                    target_id=str(payload.get("target_id", message_id)),
                    image_asset_id=(
                        str(payload["image_asset_id"]) if payload.get("image_asset_id") else None
                    ),
                    video_asset_id=(
                        str(payload["video_asset_id"]) if payload.get("video_asset_id") else None
                    ),
                    label_definition_version_id=str(payload.get("label_definition_version_id", "")),
                    label_key=str(payload.get("label_key", "")),
                    score=float(payload.get("score", 1.0)),
                    activated=bool(payload.get("activated", True)),
                    actor_admin_user_id=actor_admin_user_id,
                    reason=payload.get("reason"),
                    created_at=now,
                )
            elif op_raw == "clear":
                ev = await self._review_service.clear_label(
                    message_id=message_id,
                    target_scope=str(payload.get("target_scope", "global")),
                    target_kind=str(payload.get("target_kind", "message")),
                    target_id=str(payload.get("target_id", message_id)),
                    image_asset_id=(
                        str(payload["image_asset_id"]) if payload.get("image_asset_id") else None
                    ),
                    video_asset_id=(
                        str(payload["video_asset_id"]) if payload.get("video_asset_id") else None
                    ),
                    label_definition_version_id=str(payload.get("label_definition_version_id", "")),
                    label_key=str(payload.get("label_key", "")),
                    actor_admin_user_id=actor_admin_user_id,
                    reason=payload.get("reason"),
                    created_at=now,
                )
            else:
                raise AdminValidationError("operation must be set or clear")
        except DomainValidationError as exc:
            raise AdminValidationError(str(exc)) from exc
        except LookupError as exc:
            raise AdminNotFoundError(str(exc)) from exc

        return {
            "event_id": ev.event_id,
            "message_id": ev.message_id,
            "target_scope": ev.target_scope,
            "target_kind": ev.target_kind,
            "target_id": ev.target_id,
            "label_definition_version_id": ev.label_definition_version_id,
            "label_key": ev.label_key,
            "operation": ev.operation.value,
            "score": ev.score,
            "activated": ev.activated,
            "actor_admin_user_id": ev.actor_admin_user_id,
            "reason": ev.reason,
            "created_at": ev.created_at.isoformat(),
        }

    async def transition_review_status(
        self,
        *,
        message_id: str,
        new_status: str,
        reason: str | None,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        try:
            ev = await self._review_service.transition_status(
                message_id=message_id,
                new_status=ReviewStatus(new_status.strip().lower()),
                actor_admin_user_id=actor_admin_user_id,
                reason=reason,
                created_at=now,
            )
        except (DomainValidationError, ValueError) as exc:
            raise AdminValidationError(str(exc)) from exc
        except LookupError as exc:
            raise AdminNotFoundError(str(exc)) from exc
        return {
            "event_id": ev.event_id,
            "message_id": ev.message_id,
            "old_status": ev.old_status.value,
            "new_status": ev.new_status.value,
            "actor_admin_user_id": ev.actor_admin_user_id,
            "reason": ev.reason,
            "created_at": ev.created_at.isoformat(),
        }

    async def reanalyze_message(
        self,
        *,
        message_id: str,
        pipeline_version_id: str | None,
        reason: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            async with session.begin():
                msg = await session.get(MessageRecord, UUID(message_id), with_for_update=True)
                if msg is None:
                    raise AdminNotFoundError("message does not exist")
                pipe_vid = _try_uuid(pipeline_version_id)
                if pipe_vid is None:
                    sc = await session.get(SourceChannel, msg.source_channel_id)
                    if sc and sc.active_profile_version_id:
                        prof = await session.get(
                            SourceChannelProfileVersion, sc.active_profile_version_id
                        )
                        if prof:
                            cfg = SourceChannelProfileConfig.from_content(prof.content)
                            pipe_vid = _try_uuid(cfg.analysis_pipeline_version_id)
                if pipe_vid is None:
                    raise AdminValidationError("published analysis_pipeline_version_id is required")
                pver = await session.get(AnalysisPipelineVersionRecord, pipe_vid)
                if pver is None or pver.state != "published":
                    raise AdminValidationError("analysis_pipeline_version_id must be published")
                run_id = self._id_factory()
                run = AnalysisRunRecord(
                    id=run_id,
                    message_id=msg.id,
                    analysis_pipeline_version_id=pipe_vid,
                    run_mode="reanalysis",
                    status="running",
                    facts_snapshot={},
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                # No ORM relationships order these writes: persist the parent first.
                await session.flush()
                from .reanalysis_repository import advance_reanalysis_in_session

                stage_run_ids = await advance_reanalysis_in_session(
                    session=session, run=run, now=now
                )
                return {
                    "analysis_run_id": str(run_id),
                    "message_id": message_id,
                    "analysis_pipeline_version_id": str(pipe_vid),
                    "run_mode": "reanalysis",
                    "stage_run_ids": list(stage_run_ids),
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
        async with self._database.session() as session:
            msg = await session.get(MessageRecord, UUID(message_id))
            if msg is None:
                raise AdminNotFoundError("message does not exist")
            policy_vid = routing_policy_version_id
            if policy_vid is None:
                sc = await session.get(SourceChannel, msg.source_channel_id)
                if sc and sc.active_profile_version_id:
                    prof = await session.get(
                        SourceChannelProfileVersion, sc.active_profile_version_id
                    )
                    if prof:
                        cfg = SourceChannelProfileConfig.from_content(prof.content)
                        policy_vid = cfg.routing_policy_version_id
            if not policy_vid:
                raise AdminValidationError("published routing_policy_version_id is required")
            prior_eval = (
                await session.execute(
                    select(RoutingEvaluationRecord)
                    .where(RoutingEvaluationRecord.message_id == msg.id)
                    .order_by(RoutingEvaluationRecord.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            prev_intents = (
                list(
                    (
                        await session.execute(
                            select(PublicationIntentRecord).where(
                                PublicationIntentRecord.routing_evaluation_id == prior_eval.id
                            )
                        )
                    ).scalars()
                )
                if prior_eval is not None
                else []
            )

        try:
            res = await self._routing_service.evaluate(
                RoutingRequest(
                    request_id=f"reroute:{self._id_factory()}",
                    message_id=message_id,
                    routing_policy_version_id=policy_vid,
                    dry_run=dry_run,
                )
            )
        except DomainValidationError as exc:
            raise AdminValidationError(str(exc)) from exc
        except LookupError as exc:
            raise AdminNotFoundError(str(exc)) from exc

        prev_dests = {str(pi.destination_channel_id): pi.publication_mode for pi in prev_intents}
        new_dests = {i.destination_channel_id: i.publication_mode for i in res.intents}
        return {
            "evaluation_id": res.evaluation_id,
            "routing_request_id": res.routing_request_id,
            "message_id": message_id,
            "routing_policy_version_id": policy_vid,
            "dry_run": dry_run,
            "persisted": res.persisted,
            "facts_hash": res.facts_hash,
            "stopped_at_rule_id": res.decision.stopped_at_rule_id,
            "matched_rule_ids": [o.rule_id for o in res.decision.outcomes if o.matched],
            "intents": [
                {
                    "intent_id": i.intent_id,
                    "destination_channel_id": i.destination_channel_id,
                    "publish_identity_id": i.publish_identity_id,
                    "publication_mode": i.publication_mode,
                    "rendered_text": i.rendered_text,
                }
                for i in res.intents
            ],
            "comparison": {
                "previous_evaluation_id": str(prior_eval.id) if prior_eval else None,
                "added_destinations": sorted(set(new_dests) - set(prev_dests)),
                "removed_destinations": sorted(set(prev_dests) - set(new_dests)),
                "changed_modes": {
                    d: {"before": prev_dests[d], "after": new_dests[d]}
                    for d in sorted(set(prev_dests) & set(new_dests))
                    if prev_dests[d] != new_dests[d]
                },
            },
        }

    async def skip_message(
        self,
        *,
        message_id: str,
        reason: str,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            async with session.begin():
                msg = await session.get(MessageRecord, UUID(message_id), with_for_update=True)
                if msg is None:
                    raise AdminNotFoundError("message does not exist")
                msg.processing_status = "processed"
                msg.terminal_reason = "admin_skipped"
                msg.failure_stage = None
                msg.last_error_code = None
                msg.updated_at = now

        settled_executions: list[dict[str, Any]] = []
        exec_ids = await self._pipeline_repo.find_awaiting_execution_ids_for_message(
            message_id=message_id
        )
        for exec_id in exec_ids:
            execution = await self._pipeline_repo.load_execution(execution_id=exec_id)
            if execution is None or execution.status != "awaiting_pipeline":
                continue
            window_msgs = await self._pipeline_repo.load_window_messages(execution=execution)
            progress = tuple(
                WindowMessageProgress(
                    primary_telegram_message_id=item.primary_telegram_message_id,
                    processing_status=item.processing_status,
                    terminal_reason=item.terminal_reason,
                )
                for item in window_msgs
            )
            advancement = compute_continuous_watermark(
                from_message_id_exclusive=execution.from_message_id_exclusive,
                current_watermark_message_id=execution.watermark_message_id,
                to_message_id_inclusive=execution.to_message_id_inclusive,
                history_ingested=True,
                messages=progress,
            )
            _, completed = await self._pipeline_repo.persist_watermark_advancement(
                execution_id=exec_id,
                next_watermark_message_id=advancement.next_watermark_message_id,
                window_completed=advancement.window_completed,
                progress_snapshot=progress,
                now=now,
            )
            settled_executions.append(
                {
                    "execution_id": exec_id,
                    "new_watermark_message_id": advancement.next_watermark_message_id,
                    "window_completed": completed,
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
        uid = UUID(target_id)
        async with self._database.session() as session:
            async with session.begin():
                prev, queue = await _reset_db_step(
                    session=session, step_kind=step_kind, uid=uid, now=now
                )
                await _upsert_wakeup(session, queue=queue, entity_id=uid, now=now)
                return {
                    "step_kind": step_kind,
                    "target_id": target_id,
                    "previous_status": prev,
                    "status": "pending",
                    "enqueued_queue": queue,
                }

    # --- Cleanup, Operations, Audit & Metrics ---

    async def create_cleanup_dry_run(
        self,
        *,
        filter_payload: Mapping[str, Any],
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        cleanup_filter = _build_cleanup_filter(filter_payload)
        try:
            res = await self._cleanup_service.dry_run(
                cleanup_filter=cleanup_filter,
                actor_admin_user_id=actor_admin_user_id,
                now=now,
            )
        except DomainValidationError as exc:
            raise AdminValidationError(str(exc)) from exc
        return {
            "id": res.dry_run_id,
            "actor_admin_user_id": res.actor_admin_user_id,
            "filter_snapshot": dict(res.filter_snapshot),
            "filter_hash": res.filter_hash,
            "estimated_asset_count": res.estimate.estimated_asset_count,
            "estimated_message_count": res.estimate.estimated_message_count,
            "estimated_bytes": res.estimate.estimated_bytes,
            "created_at": res.created_at.isoformat(),
        }

    async def list_cleanup_dry_runs(self, *, limit: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(ArchiveCleanupDryRunRecord)
                    .order_by(ArchiveCleanupDryRunRecord.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
            return [_serialize_dry_run(r) for r in rows]

    async def get_cleanup_dry_run(self, *, dry_run_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = await session.get(ArchiveCleanupDryRunRecord, UUID(dry_run_id))
            return _serialize_dry_run(row) if row is not None else None

    async def confirm_cleanup_job(
        self,
        *,
        dry_run_id: str,
        expected_filter_hash: str,
        actor_admin_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        try:
            job = await self._cleanup_service.confirm(
                dry_run_id=dry_run_id,
                expected_filter_hash=expected_filter_hash,
                confirmed_by_admin_user_id=actor_admin_user_id,
                now=now,
            )
        except LookupError as exc:
            raise AdminNotFoundError(str(exc)) from exc
        except DomainValidationError as exc:
            raise AdminConflictError(str(exc)) from exc
        return {
            "id": job.job_id,
            "dry_run_id": job.dry_run_id,
            "confirmed_by_admin_user_id": job.confirmed_by_admin_user_id,
            "filter_hash": job.filter_hash,
            "estimated_asset_count": job.estimate.estimated_asset_count,
            "estimated_message_count": job.estimate.estimated_message_count,
            "estimated_bytes": job.estimate.estimated_bytes,
            "item_count": job.item_count,
            "status": job.status,
            "created_at": job.created_at.isoformat(),
        }

    async def list_cleanup_jobs(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> CursorPage[dict[str, Any]]:
        token = decode_cursor(cursor) if cursor else None
        async with self._database.session() as session:
            stmt = select(ArchiveCleanupJobRecord)
            if token is not None:
                stmt = stmt.where(
                    or_(
                        ArchiveCleanupJobRecord.created_at < token.timestamp,
                        and_(
                            ArchiveCleanupJobRecord.created_at == token.timestamp,
                            ArchiveCleanupJobRecord.id < UUID(token.entity_id),
                        ),
                    )
                )
            stmt = stmt.order_by(
                ArchiveCleanupJobRecord.created_at.desc(), ArchiveCleanupJobRecord.id.desc()
            ).limit(limit + 1)
            rows = list((await session.execute(stmt)).scalars())
            has_more = len(rows) > limit
            items = [_serialize_cleanup_job(r) for r in rows[:limit]]
            next_cursor = (
                encode_cursor(timestamp=rows[limit - 1].created_at, entity_id=rows[limit - 1].id)
                if has_more and items
                else None
            )
            return CursorPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    async def get_cleanup_job(self, *, job_id: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = await session.get(ArchiveCleanupJobRecord, UUID(job_id))
            return _serialize_cleanup_job(row) if row is not None else None

    async def dashboard_summary(self, *, now: datetime) -> dict[str, Any]:
        ops = await self._refresh_ops(now)
        async with self._database.session() as session:
            total_sc = (await session.execute(select(func.count(SourceChannel.id)))).scalar_one()
            en_sc = (
                await session.execute(
                    select(func.count(SourceChannel.id)).where(SourceChannel.enabled.is_(True))
                )
            ).scalar_one()
            prof_sc = (
                await session.execute(
                    select(func.count(SourceChannel.id)).where(
                        SourceChannel.active_profile_version_id.is_not(None)
                    )
                )
            ).scalar_one()
            total_dc = (
                await session.execute(select(func.count(DestinationChannel.id)))
            ).scalar_one()
            en_dc = (
                await session.execute(
                    select(func.count(DestinationChannel.id)).where(
                        DestinationChannel.enabled.is_(True)
                    )
                )
            ).scalar_one()
            msg_counts = dict(
                (
                    await session.execute(
                        select(
                            MessageRecord.processing_status, func.count(MessageRecord.id)
                        ).group_by(MessageRecord.processing_status)
                    )
                ).all()
            )
            blocked_msgs = (
                await session.execute(
                    select(func.count(MessageRecord.id)).where(
                        MessageRecord.blocked_from_analysis.is_(True)
                    )
                )
            ).scalar_one()
            unreviewed = (
                await session.execute(
                    select(func.count(MessageRecord.id)).where(
                        MessageRecord.review_status == "unreviewed"
                    )
                )
            ).scalar_one()
            range_counts = dict(
                (
                    await session.execute(
                        select(
                            ProcessingRangeRecord.status, func.count(ProcessingRangeRecord.id)
                        ).group_by(ProcessingRangeRecord.status)
                    )
                ).all()
            )
            id_counts = dict(
                (
                    await session.execute(
                        select(
                            TelegramIdentity.health_status, func.count(TelegramIdentity.id)
                        ).group_by(TelegramIdentity.health_status)
                    )
                ).all()
            )
            img_bytes = (
                await session.execute(
                    select(func.coalesce(func.sum(ImageAssetRecord.archive_size_bytes), 0)).where(
                        ImageAssetRecord.archive_state == "ready"
                    )
                )
            ).scalar_one()
            vid_bytes = (
                await session.execute(
                    select(func.coalesce(func.sum(VideoFrameRecord.archive_size_bytes), 0)).where(
                        VideoFrameRecord.archive_state == "ready"
                    )
                )
            ).scalar_one()
            ready_imgs = (
                await session.execute(
                    select(func.count(ImageAssetRecord.id)).where(
                        ImageAssetRecord.archive_state == "ready"
                    )
                )
            ).scalar_one()
            ready_vids = (
                await session.execute(
                    select(func.count(VideoAssetRecord.id)).where(
                        VideoAssetRecord.archive_state == "ready"
                    )
                )
            ).scalar_one()
            del_imgs = (
                await session.execute(
                    select(func.count(ImageAssetRecord.id)).where(
                        ImageAssetRecord.archive_state == "deleted"
                    )
                )
            ).scalar_one()
            del_vids = (
                await session.execute(
                    select(func.count(VideoAssetRecord.id)).where(
                        VideoAssetRecord.archive_state == "deleted"
                    )
                )
            ).scalar_one()

        return {
            "source_channels": {
                "total": int(total_sc),
                "enabled": int(en_sc),
                "active_profiles": int(prof_sc),
            },
            "destination_channels": {"total": int(total_dc), "enabled": int(en_dc)},
            "processing_ranges": {
                "total": sum(int(v) for v in range_counts.values()),
                "active": int(range_counts.get("active", 0)),
                "paused": int(range_counts.get("paused", 0)),
                "completed": int(range_counts.get("completed", 0)),
                "failed": int(range_counts.get("failed", 0)),
            },
            "messages": {
                "total": sum(int(v) for v in msg_counts.values()),
                "processed": int(msg_counts.get("processed", 0)),
                "processing": int(msg_counts.get("processing", 0)),
                "failed": int(msg_counts.get("failed", 0)),
                "blocked_from_analysis": int(blocked_msgs),
                "unreviewed": int(unreviewed),
            },
            "identities": {
                "total": sum(int(v) for v in id_counts.values()),
                "healthy": int(id_counts.get("healthy", 0)),
                "degraded": int(id_counts.get("degraded", 0)),
                "failed": int(id_counts.get("failed", 0)),
            },
            "archive": {
                "ready_images": int(ready_imgs),
                "ready_videos": int(ready_vids),
                "deleted_assets": int(del_imgs) + int(del_vids),
                "total_archive_bytes": int(img_bytes) + int(vid_bytes),
            },
            "operations": _serialize_ops(ops),
        }

    async def operations_status(self, *, now: datetime) -> dict[str, Any]:
        ops = await self._refresh_ops(now)
        failed = await self.list_failed_items(step_kind=None, limit=50)
        return {
            "operations": _serialize_ops(ops),
            "queues": {
                "publication_pending": ops.backlog.publication_pending,
                "publication_partial": ops.backlog.publication_partial,
                "publication_retry_due": ops.backlog.publication_retry_due,
                "cleanup_pending": ops.backlog.cleanup_pending,
                "cleanup_retry_due": ops.backlog.cleanup_retry_due,
                "durable_wakeups_due": ops.backlog.durable_wakeups_due,
            },
            "failed_count": len(failed),
        }

    async def list_failed_items(
        self,
        *,
        step_kind: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with self._database.session() as session:
            if step_kind in (None, "stage_run"):
                rows = (
                    await session.execute(
                        select(StageRunRecord).where(StageRunRecord.status == "failed").limit(limit)
                    )
                ).scalars()
                for r in rows:
                    results.append(
                        {
                            "step_kind": "stage_run",
                            "target_id": str(r.id),
                            "message_id": str(r.message_id),
                            "status": "failed",
                            "error_code": r.last_error_code,
                            "error_type": r.last_error_type,
                            "attempt_count": r.attempt_count,
                        }
                    )
            if step_kind in (None, "publication_intent") and len(results) < limit:
                rows = (
                    await session.execute(
                        select(PublicationIntentRecord)
                        .where(PublicationIntentRecord.status == "failed")
                        .limit(limit - len(results))
                    )
                ).scalars()
                for r in rows:
                    results.append(
                        {
                            "step_kind": "publication_intent",
                            "target_id": str(r.id),
                            "message_id": str(r.message_id),
                            "status": "failed",
                            "error_code": r.last_error_code,
                            "error_type": r.last_error_type,
                            "attempt_count": r.attempt_count,
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
        async with self._database.session() as session:
            stmt = select(AuditEvent)
            if filters.get("entity_type"):
                stmt = stmt.where(AuditEvent.entity_type == filters["entity_type"])
            if filters.get("entity_id"):
                stmt = stmt.where(
                    AuditEvent.entity_id == _require_uuid(filters["entity_id"], "entity_id")
                )
            if filters.get("event_type"):
                stmt = stmt.where(AuditEvent.event_type == filters["event_type"])
            if token is not None:
                stmt = stmt.where(
                    or_(
                        AuditEvent.created_at < token.timestamp,
                        and_(
                            AuditEvent.created_at == token.timestamp,
                            AuditEvent.id < UUID(token.entity_id),
                        ),
                    )
                )
            stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(
                limit + 1
            )
            rows = list((await session.execute(stmt)).scalars())
            has_more = len(rows) > limit
            items = [_serialize_audit(r) for r in rows[:limit]]
            next_cursor = (
                encode_cursor(timestamp=rows[limit - 1].created_at, entity_id=rows[limit - 1].id)
                if has_more and items
                else None
            )
            return CursorPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    async def collect_metrics_snapshot(self, *, now: datetime) -> MetricsCollectionSnapshot:
        ops = await self._refresh_ops(now)
        db_ok = await self._database.ping()
        async with self._database.session() as session:
            msg_by_status = {
                str(k): int(v)
                for k, v in (
                    await session.execute(
                        select(
                            MessageRecord.processing_status, func.count(MessageRecord.id)
                        ).group_by(MessageRecord.processing_status)
                    )
                ).all()
            }
            blocked = int(
                (
                    await session.execute(
                        select(func.count(MessageRecord.id)).where(
                            MessageRecord.blocked_from_analysis.is_(True)
                        )
                    )
                ).scalar_one()
            )
            eval_total = int(
                (await session.execute(select(func.count(RoutingEvaluationRecord.id)))).scalar_one()
            )
            intents_by_status = {
                str(k): int(v)
                for k, v in (
                    await session.execute(
                        select(
                            PublicationIntentRecord.status, func.count(PublicationIntentRecord.id)
                        ).group_by(PublicationIntentRecord.status)
                    )
                ).all()
            }
        return MetricsCollectionSnapshot(
            operations=ops,
            messages_by_status=msg_by_status,
            messages_blocked_total=blocked,
            routing_evaluations_total=eval_total,
            routing_matched_actions_total=sum(intents_by_status.values()),
            publication_intents_by_status=intents_by_status,
            api_database_ready=db_ok,
        )

    async def _refresh_ops(self, now: datetime) -> OperationsSnapshot:
        try:
            backlog = await self._backlog_repo.observe_backlog(now=now)
            self._monitor.observe_backlog(
                publication_pending=backlog.publication_pending,
                publication_partial=backlog.publication_partial,
                publication_retry_due=backlog.publication_retry_due,
                cleanup_pending=backlog.cleanup_pending,
                cleanup_retry_due=backlog.cleanup_retry_due,
                durable_wakeups_due=backlog.durable_wakeups_due,
                observed_at=now,
            )
        except Exception:
            pass
        return self._monitor.snapshot(now=now)

    # --- Private SQLAlchemy Version Helpers ---

    async def _load_serialized_entity(
        self,
        session: AsyncSession,
        family: str,
        parent: Any,
    ) -> dict[str, Any]:
        ver_cls, fk_col = _FAMILY_VERSIONS[family]
        vers = list(
            (
                await session.execute(
                    select(ver_cls)
                    .where(getattr(ver_cls, fk_col) == parent.id)
                    .order_by(ver_cls.version_number.asc())
                )
            ).scalars()
        )
        serialized_vers = [await self._serialize_db_version(session, family, v) for v in vers]
        pub = next((v for v in serialized_vers if v["state"] == "published"), None)
        return {
            "id": str(parent.id),
            "family": family,
            "name": parent.name,
            "source_channel_id": (
                str(parent.source_channel_id)
                if hasattr(parent, "source_channel_id") and parent.source_channel_id
                else None
            ),
            "published_version_id": pub["id"] if pub else None,
            "versions": serialized_vers,
            "created_at": parent.created_at.isoformat(),
            "updated_at": parent.updated_at.isoformat(),
        }

    async def _serialize_db_version(
        self,
        session: AsyncSession,
        family: str,
        ver: Any,
    ) -> dict[str, Any]:
        content: dict[str, Any]
        if family in {"media-processing-profiles", "source-channel-profiles"}:
            content = dict(ver.content)
        elif family == "label-definitions":
            content = {
                "key": ver.key,
                "display_name": ver.display_name,
                "description": ver.description,
                "scope": ver.scope,
                "negative": ver.negative,
                "enabled": ver.enabled,
            }
        elif family == "label-sets":
            binds = list(
                (
                    await session.execute(
                        select(LabelBindingRecord)
                        .where(LabelBindingRecord.label_set_version_id == ver.id)
                        .order_by(LabelBindingRecord.output_order.asc())
                    )
                ).scalars()
            )
            content = {
                "description": ver.description,
                "bindings": [
                    {
                        "label_definition_version_id": str(b.label_definition_version_id),
                        "activation_threshold": b.activation_threshold,
                        "prompt_hint": b.prompt_hint,
                        "output_order": b.output_order,
                    }
                    for b in binds
                ],
            }
        elif family == "prompt-templates":
            content = {
                "system_prompt": ver.system_prompt,
                "user_prompt_template": ver.user_prompt_template,
                "declared_variables": list(ver.declared_variables),
            }
        elif family == "inference-profiles":
            content = {
                "provider_adapter": ver.provider_adapter,
                "base_url": ver.base_url,
                "model_name": ver.model_name,
                "api_secret_reference": str(ver.api_secret_reference),
                "capabilities": list(ver.capabilities),
                "timeout_seconds": ver.timeout_seconds,
                "max_concurrency": ver.max_concurrency,
                "max_images": ver.max_images,
                "max_input_tokens": ver.max_input_tokens,
                "structured_output_support": ver.structured_output_support,
                "sampling_parameters": dict(ver.sampling_parameters),
            }
        elif family == "analysis-stages":
            content = {
                "name": ver.name,
                "target_scope": ver.target_scope,
                "execution_mode": ver.execution_mode,
                "prompt_template_version_id": str(ver.prompt_template_version_id),
                "label_set_version_id": str(ver.label_set_version_id),
                "structured_output_policy": dict(ver.structured_output_policy),
                "input_policy": dict(ver.input_policy),
                "visual_composition_policy": ver.visual_composition_policy,
                "inference_profile_version_id": str(ver.inference_profile_version_id),
                "cache_policy": ver.cache_policy,
                "timeout_seconds": ver.timeout_seconds,
                "retry_policy": dict(ver.retry_policy),
                "max_batch_size": ver.max_batch_size,
                "max_concurrency": ver.max_concurrency,
            }
        elif family == "analysis-pipelines":
            nodes = list(
                (
                    await session.execute(
                        select(PipelineStageNodeRecord)
                        .where(PipelineStageNodeRecord.analysis_pipeline_version_id == ver.id)
                        .order_by(PipelineStageNodeRecord.output_order.asc())
                    )
                ).scalars()
            )
            content = {
                "description": ver.description,
                "nodes": [
                    {
                        "node_key": n.node_key,
                        "analysis_stage_template_version_id": str(
                            n.analysis_stage_template_version_id
                        ),
                        "depends_on": list(n.depends_on),
                        "run_if": dict(n.run_if) if n.run_if else None,
                        "parameter_overrides": dict(n.parameter_overrides),
                    }
                    for n in nodes
                ],
            }
        elif family == "rendering-templates":
            content = {"template_text": ver.template_text}
        else:
            rules = list(
                (
                    await session.execute(
                        select(RoutingRuleRecord)
                        .where(RoutingRuleRecord.routing_policy_version_id == ver.id)
                        .order_by(RoutingRuleRecord.priority.desc())
                    )
                ).scalars()
            )
            rule_items = []
            for r in rules:
                acts = list(
                    (
                        await session.execute(
                            select(RoutingActionRecord)
                            .where(RoutingActionRecord.routing_rule_id == r.id)
                            .order_by(RoutingActionRecord.output_order.asc())
                        )
                    ).scalars()
                )
                rule_items.append(
                    {
                        "id": str(r.id),
                        "priority": r.priority,
                        "enabled": r.enabled,
                        "condition": dict(r.condition),
                        "stop_on_match": r.stop_on_match,
                        "actions": [
                            {
                                "id": str(a.id),
                                "destination_channel_id": str(a.destination_channel_id),
                                "publication_mode": a.publication_mode,
                                "rendering_template_version_id": (
                                    str(a.rendering_template_version_id)
                                    if a.rendering_template_version_id
                                    else None
                                ),
                                "publish_identity_id": (
                                    str(a.publish_identity_id) if a.publish_identity_id else None
                                ),
                                "protected_content_fallback": a.protected_content_fallback,
                                "supplement_as_reply": a.supplement_as_reply,
                            }
                            for a in acts
                        ],
                    }
                )
            content = {"description": ver.description, "rules": rule_items}

        _, fk_col = _FAMILY_VERSIONS[family]
        return {
            "id": str(ver.id),
            "family": family,
            "entity_id": str(getattr(ver, fk_col)),
            "version_number": ver.version_number,
            "state": ver.state,
            "content": content,
            "content_hash": canonical_content_hash(content),
            "published_at": ver.published_at.isoformat() if ver.published_at else None,
            "archived_at": (
                ver.archived_at.isoformat()
                if hasattr(ver, "archived_at") and ver.archived_at
                else None
            ),
            "created_at": ver.created_at.isoformat(),
        }

    async def _validate_db_version(
        self,
        *,
        session: AsyncSession,
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
            validate_source_profile_content(content)
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
        elif family == "rendering-templates":
            validate_rendering_template_syntax(str(content.get("template_text", "")))
        elif family == "routing-policies":
            validate_routing_policy_content(content)
        elif family == "label-sets":
            defs = {}
            for b in content.get("bindings", []):
                vid = str(b.get("label_definition_version_id", ""))
                defs[vid] = await self._load_label_def_domain(session, vid)
            build_label_set_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
                resolve_label_def=lambda k: defs[k],
                require_published_refs=require_published_refs,
            )
        elif family == "analysis-stages":
            p = await self._load_prompt_domain(
                session, str(content.get("prompt_template_version_id", ""))
            )
            ls = await self._load_label_set_domain(
                session, str(content.get("label_set_version_id", ""))
            )
            ip = await self._load_inference_profile_domain(
                session, str(content.get("inference_profile_version_id", ""))
            )
            build_stage_template_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
                resolve_prompt=lambda _: p,
                resolve_label_set=lambda _: ls,
                resolve_inference_profile=lambda _: ip,
                require_published_refs=require_published_refs,
            )
        elif family == "analysis-pipelines":
            stages = {}
            for n in content.get("nodes", []):
                svid = str(n.get("analysis_stage_template_version_id", ""))
                stages[svid] = await self._load_stage_domain(session, svid)
            build_pipeline_domain(
                version_id=version_id,
                entity_id=entity_id,
                version_number=version_number,
                state=state,
                content=content,
                resolve_stage=lambda k: stages[k],
                require_published_refs=require_published_refs,
            )

    async def _insert_db_version(
        self,
        *,
        session: AsyncSession,
        family: str,
        version_id: UUID,
        entity_id: UUID,
        version_number: int,
        content: Mapping[str, Any],
        now: datetime,
    ) -> Any:
        chash = canonical_content_hash(content)
        if family == "media-processing-profiles":
            rec = MediaProcessingProfileVersionRecord(
                id=version_id,
                media_processing_profile_id=entity_id,
                version_number=version_number,
                state="draft",
                content=dict(content),
                content_hash=chash,
                created_at=now,
                updated_at=now,
            )
            session.add(rec)
            await session.flush()
            return rec
        if family == "source-channel-profiles":
            rec_sp = SourceChannelProfileVersion(
                id=version_id,
                profile_id=entity_id,
                version_number=version_number,
                state="draft",
                content=dict(content),
                content_hash=chash,
                created_at=now,
            )
            session.add(rec_sp)
            await session.flush()
            return rec_sp
        if family == "label-definitions":
            rec_ld = LabelDefinitionVersionRecord(
                id=version_id,
                label_definition_id=entity_id,
                version_number=version_number,
                state="draft",
                key=str(content["key"]),
                display_name=str(content["display_name"]),
                description=str(content.get("description", "")),
                scope=str(content["scope"]),
                negative=bool(content.get("negative", False)),
                enabled=bool(content.get("enabled", True)),
                created_at=now,
            )
            session.add(rec_ld)
            await session.flush()
            return rec_ld
        if family == "prompt-templates":
            rec_pt = PromptTemplateVersionRecord(
                id=version_id,
                prompt_template_id=entity_id,
                version_number=version_number,
                state="draft",
                system_prompt=str(content.get("system_prompt", "")),
                user_prompt_template=str(content.get("user_prompt_template", "")),
                declared_variables=list(content.get("declared_variables", [])),
                created_at=now,
            )
            session.add(rec_pt)
            await session.flush()
            return rec_pt
        if family == "inference-profiles":
            rec_ip = InferenceProfileVersionRecord(
                id=version_id,
                inference_profile_id=entity_id,
                version_number=version_number,
                state="draft",
                provider_adapter=str(content.get("provider_adapter", "openai_compatible")),
                base_url=str(content["base_url"]),
                model_name=str(content["model_name"]),
                api_secret_reference=str(content["api_secret_reference"]),
                capabilities=list(content.get("capabilities", ["text"])),
                timeout_seconds=float(content.get("timeout_seconds", 30.0)),
                max_concurrency=int(content.get("max_concurrency", 2)),
                max_images=int(content.get("max_images", 8)),
                max_input_tokens=int(content.get("max_input_tokens", 8192)),
                structured_output_support=bool(content.get("structured_output_support", True)),
                sampling_parameters=dict(content.get("sampling_parameters", {})),
                created_at=now,
            )
            session.add(rec_ip)
            await session.flush()
            return rec_ip
        if family == "label-sets":
            rec_ls = LabelSetVersionRecord(
                id=version_id,
                label_set_id=entity_id,
                version_number=version_number,
                state="draft",
                description=str(content.get("description", "")),
                created_at=now,
            )
            session.add(rec_ls)
            await session.flush()
            for idx, b in enumerate(content.get("bindings", [])):
                session.add(
                    LabelBindingRecord(
                        id=self._id_factory(),
                        label_set_version_id=version_id,
                        label_definition_version_id=UUID(str(b["label_definition_version_id"])),
                        activation_threshold=float(b.get("activation_threshold", 0.5)),
                        prompt_hint=str(b.get("prompt_hint", "")),
                        output_order=int(b.get("output_order", idx)),
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.flush()
            return rec_ls
        if family == "analysis-stages":
            vis = content.get("visual_composition_policy", "adaptive")
            if isinstance(vis, Mapping):
                vis = vis.get("mode", "adaptive")
            rec_st = AnalysisStageTemplateVersionRecord(
                id=version_id,
                analysis_stage_template_id=entity_id,
                version_number=version_number,
                state="draft",
                name=str(content.get("name", "stage")),
                target_scope=str(content.get("target_scope", "global")),
                execution_mode=str(content.get("execution_mode", "single_message")),
                prompt_template_version_id=UUID(str(content["prompt_template_version_id"])),
                label_set_version_id=UUID(str(content["label_set_version_id"])),
                structured_output_policy=dict(content.get("structured_output_policy", {})),
                input_policy=dict(content.get("input_policy", {"text": True})),
                visual_composition_policy=str(vis),
                inference_profile_version_id=UUID(str(content["inference_profile_version_id"])),
                cache_policy=str(content.get("cache_policy", "none")),
                timeout_seconds=float(content.get("timeout_seconds", 30.0)),
                retry_policy=dict(content.get("retry_policy", {})),
                max_batch_size=int(content.get("max_batch_size", 1)),
                max_concurrency=int(content.get("max_concurrency", 1)),
                created_at=now,
            )
            session.add(rec_st)
            await session.flush()
            return rec_st
        if family == "analysis-pipelines":
            rec_ap = AnalysisPipelineVersionRecord(
                id=version_id,
                analysis_pipeline_id=entity_id,
                version_number=version_number,
                state="draft",
                description=str(content.get("description", "")),
                created_at=now,
            )
            session.add(rec_ap)
            await session.flush()
            for idx, n in enumerate(content.get("nodes", [])):
                session.add(
                    PipelineStageNodeRecord(
                        id=self._id_factory(),
                        analysis_pipeline_version_id=version_id,
                        node_key=str(n.get("node_key") or n.get("node_id")),
                        analysis_stage_template_version_id=UUID(
                            str(n["analysis_stage_template_version_id"])
                        ),
                        depends_on=list(n.get("depends_on", [])),
                        run_if=dict(n["run_if"]) if n.get("run_if") else null(),
                        parameter_overrides=dict(n.get("parameter_overrides") or {}),
                        output_order=idx,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.flush()
            return rec_ap
        if family == "rendering-templates":
            tmpl_text = str(content["template_text"])
            rec_rt = RenderingTemplateVersionRecord(
                id=version_id,
                rendering_template_id=entity_id,
                version_number=version_number,
                state="draft",
                template_text=tmpl_text,
                content_hash=canonical_content_hash({"template_text": tmpl_text}),
                created_at=now,
                updated_at=now,
            )
            session.add(rec_rt)
            await session.flush()
            return rec_rt

        rec_rp = RoutingPolicyVersionRecord(
            id=version_id,
            routing_policy_id=entity_id,
            version_number=version_number,
            state="draft",
            description=str(content.get("description", "")),
            created_at=now,
            updated_at=now,
        )
        session.add(rec_rp)
        await session.flush()
        for r in content.get("rules", []):
            rule_id = self._id_factory()
            session.add(
                RoutingRuleRecord(
                    id=rule_id,
                    routing_policy_version_id=version_id,
                    priority=int(r["priority"]),
                    enabled=bool(r.get("enabled", True)),
                    condition=dict(r["condition"]),
                    stop_on_match=bool(r.get("stop_on_match", False)),
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            for idx, a in enumerate(r.get("actions", [])):
                session.add(
                    RoutingActionRecord(
                        id=self._id_factory(),
                        routing_rule_id=rule_id,
                        destination_channel_id=UUID(str(a["destination_channel_id"])),
                        publication_mode=str(a["publication_mode"]),
                        rendering_template_version_id=_try_uuid(
                            a.get("rendering_template_version_id")
                        ),
                        publish_identity_id=_try_uuid(a.get("publish_identity_id")),
                        protected_content_fallback=str(a.get("protected_content_fallback", "fail")),
                        supplement_as_reply=bool(a.get("supplement_as_reply", True)),
                        output_order=idx,
                        created_at=now,
                        updated_at=now,
                    )
                )
        await session.flush()
        return rec_rp

    async def _apply_db_version_update(
        self,
        session: AsyncSession,
        family: str,
        ver: Any,
        content: Mapping[str, Any],
        now: datetime,
    ) -> None:
        if family in {"media-processing-profiles", "source-channel-profiles"}:
            ver.content = dict(content)
            ver.content_hash = canonical_content_hash(content)
        elif family == "rendering-templates":
            ver.template_text = str(content["template_text"])
            ver.content_hash = canonical_content_hash(
                {"template_text": str(content["template_text"])}
            )
        elif family == "label-definitions":
            ver.key = str(content["key"])
            ver.display_name = str(content["display_name"])
            ver.description = str(content.get("description", ""))
            ver.scope = str(content["scope"])
            ver.negative = bool(content.get("negative", False))
            ver.enabled = bool(content.get("enabled", True))
        elif family == "prompt-templates":
            ver.system_prompt = str(content.get("system_prompt", ""))
            ver.user_prompt_template = str(content.get("user_prompt_template", ""))
            ver.declared_variables = list(content.get("declared_variables", []))
        elif family == "inference-profiles":
            ver.provider_adapter = str(content.get("provider_adapter", "openai_compatible"))
            ver.base_url = str(content["base_url"])
            ver.model_name = str(content["model_name"])
            ver.api_secret_reference = str(content["api_secret_reference"])
            ver.capabilities = list(content.get("capabilities", ["text"]))
            ver.timeout_seconds = float(content.get("timeout_seconds", 30.0))
            ver.max_concurrency = int(content.get("max_concurrency", 2))
            ver.max_images = int(content.get("max_images", 8))
            ver.max_input_tokens = int(content.get("max_input_tokens", 8192))
            ver.structured_output_support = bool(content.get("structured_output_support", True))
            ver.sampling_parameters = dict(content.get("sampling_parameters", {}))
        elif family == "analysis-stages":
            vis = content.get("visual_composition_policy", "adaptive")
            if isinstance(vis, Mapping):
                vis = vis.get("mode", "adaptive")
            ver.name = str(content.get("name", "stage"))
            ver.target_scope = str(content.get("target_scope", "global"))
            ver.execution_mode = str(content.get("execution_mode", "single_message"))
            ver.prompt_template_version_id = UUID(str(content["prompt_template_version_id"]))
            ver.label_set_version_id = UUID(str(content["label_set_version_id"]))
            ver.inference_profile_version_id = UUID(str(content["inference_profile_version_id"]))
            ver.structured_output_policy = dict(content.get("structured_output_policy", {}))
            ver.input_policy = dict(content.get("input_policy", {"text": True}))
            ver.visual_composition_policy = str(vis)
            ver.cache_policy = str(content.get("cache_policy", "none"))
            ver.timeout_seconds = float(content.get("timeout_seconds", 30.0))
            ver.retry_policy = dict(content.get("retry_policy", {}))
            ver.max_batch_size = int(content.get("max_batch_size", 1))
            ver.max_concurrency = int(content.get("max_concurrency", 1))
        elif family == "label-sets":
            ver.description = str(content.get("description", ""))
            await session.execute(
                delete(LabelBindingRecord).where(LabelBindingRecord.label_set_version_id == ver.id)
            )
            for idx, binding in enumerate(content.get("bindings", [])):
                session.add(
                    LabelBindingRecord(
                        id=self._id_factory(),
                        label_set_version_id=ver.id,
                        label_definition_version_id=UUID(
                            str(binding["label_definition_version_id"])
                        ),
                        activation_threshold=float(binding.get("activation_threshold", 0.5)),
                        prompt_hint=str(binding.get("prompt_hint", "")),
                        output_order=int(binding.get("output_order", idx)),
                        created_at=now,
                        updated_at=now,
                    )
                )
        elif family == "analysis-pipelines":
            ver.description = str(content.get("description", ""))
            await session.execute(
                delete(PipelineStageNodeRecord).where(
                    PipelineStageNodeRecord.analysis_pipeline_version_id == ver.id
                )
            )
            for idx, node in enumerate(content.get("nodes", [])):
                session.add(
                    PipelineStageNodeRecord(
                        id=self._id_factory(),
                        analysis_pipeline_version_id=ver.id,
                        node_key=str(node.get("node_key") or node.get("node_id")),
                        analysis_stage_template_version_id=UUID(
                            str(node["analysis_stage_template_version_id"])
                        ),
                        depends_on=list(node.get("depends_on", [])),
                        run_if=dict(node["run_if"]) if node.get("run_if") else null(),
                        parameter_overrides=dict(node.get("parameter_overrides") or {}),
                        output_order=idx,
                        created_at=now,
                        updated_at=now,
                    )
                )
        elif family == "routing-policies":
            ver.description = str(content.get("description", ""))
            # Delete children first; do not rely on implicit mapper ordering/cascades.
            rule_ids = select(RoutingRuleRecord.id).where(
                RoutingRuleRecord.routing_policy_version_id == ver.id
            )
            await session.execute(
                delete(RoutingActionRecord).where(RoutingActionRecord.routing_rule_id.in_(rule_ids))
            )
            await session.execute(
                delete(RoutingRuleRecord).where(
                    RoutingRuleRecord.routing_policy_version_id == ver.id
                )
            )
            for rule in content.get("rules", []):
                rule_id = self._id_factory()
                session.add(
                    RoutingRuleRecord(
                        id=rule_id,
                        routing_policy_version_id=ver.id,
                        priority=int(rule["priority"]),
                        enabled=bool(rule.get("enabled", True)),
                        condition=dict(rule["condition"]),
                        stop_on_match=bool(rule.get("stop_on_match", False)),
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                for idx, action in enumerate(rule.get("actions", [])):
                    session.add(
                        RoutingActionRecord(
                            id=self._id_factory(),
                            routing_rule_id=rule_id,
                            destination_channel_id=UUID(str(action["destination_channel_id"])),
                            publication_mode=str(action["publication_mode"]),
                            rendering_template_version_id=_try_uuid(
                                action.get("rendering_template_version_id")
                            ),
                            publish_identity_id=_try_uuid(action.get("publish_identity_id")),
                            protected_content_fallback=str(
                                action.get("protected_content_fallback", "fail")
                            ),
                            supplement_as_reply=bool(action.get("supplement_as_reply", True)),
                            output_order=idx,
                            created_at=now,
                            updated_at=now,
                        )
                    )
        else:
            raise AdminValidationError(f"unsupported configuration family: {family}")
        if hasattr(type(ver), "updated_at"):
            ver.updated_at = now
        await session.flush()

    async def _load_label_def_domain(self, session: AsyncSession, vid: str) -> Any:
        rec = await session.get(LabelDefinitionVersionRecord, _require_uuid(vid, "version_id"))
        if rec is None:
            raise AdminValidationError(f"label definition version {vid} does not exist")
        ser = await self._serialize_db_version(session, "label-definitions", rec)
        return build_label_definition_domain(
            version_id=ser["id"],
            entity_id=ser["entity_id"],
            version_number=ser["version_number"],
            state=ser["state"],
            content=ser["content"],
        )

    async def _load_prompt_domain(self, session: AsyncSession, vid: str) -> Any:
        rec = await session.get(PromptTemplateVersionRecord, _require_uuid(vid, "version_id"))
        if rec is None:
            raise AdminValidationError(f"prompt template version {vid} does not exist")
        ser = await self._serialize_db_version(session, "prompt-templates", rec)
        return build_prompt_template_domain(
            version_id=ser["id"],
            entity_id=ser["entity_id"],
            version_number=ser["version_number"],
            state=ser["state"],
            content=ser["content"],
        )

    async def _load_inference_profile_domain(self, session: AsyncSession, vid: str) -> Any:
        rec = await session.get(InferenceProfileVersionRecord, _require_uuid(vid, "version_id"))
        if rec is None:
            raise AdminValidationError(f"inference profile version {vid} does not exist")
        ser = await self._serialize_db_version(session, "inference-profiles", rec)
        return build_inference_profile_domain(
            version_id=ser["id"],
            entity_id=ser["entity_id"],
            version_number=ser["version_number"],
            state=ser["state"],
            content=ser["content"],
        )

    async def _load_label_set_domain(self, session: AsyncSession, vid: str) -> Any:
        rec = await session.get(LabelSetVersionRecord, _require_uuid(vid, "version_id"))
        if rec is None:
            raise AdminValidationError(f"label set version {vid} does not exist")
        ser = await self._serialize_db_version(session, "label-sets", rec)
        defs = {}
        for b in ser["content"]["bindings"]:
            dvid = b["label_definition_version_id"]
            defs[dvid] = await self._load_label_def_domain(session, dvid)
        return build_label_set_domain(
            version_id=ser["id"],
            entity_id=ser["entity_id"],
            version_number=ser["version_number"],
            state=ser["state"],
            content=ser["content"],
            resolve_label_def=lambda k: defs[k],
        )

    async def _load_stage_domain(
        self,
        session: AsyncSession,
        vid: str,
    ) -> AnalysisStageTemplateVersion:
        rec = await session.get(
            AnalysisStageTemplateVersionRecord, _require_uuid(vid, "version_id")
        )
        if rec is None:
            raise AdminValidationError(f"analysis stage version {vid} does not exist")
        ser = await self._serialize_db_version(session, "analysis-stages", rec)
        c = ser["content"]
        p = await self._load_prompt_domain(session, c["prompt_template_version_id"])
        ls = await self._load_label_set_domain(session, c["label_set_version_id"])
        ip = await self._load_inference_profile_domain(session, c["inference_profile_version_id"])
        return build_stage_template_domain(
            version_id=ser["id"],
            entity_id=ser["entity_id"],
            version_number=ser["version_number"],
            state=ser["state"],
            content=c,
            resolve_prompt=lambda _: p,
            resolve_label_set=lambda _: ls,
            resolve_inference_profile=lambda _: ip,
        )

    async def _load_media_descriptors(
        self,
        session: AsyncSession,
        message_id: UUID,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        imgs = (
            await session.execute(
                select(ImageAssetRecord).where(
                    ImageAssetRecord.message_id == message_id,
                    ImageAssetRecord.archive_state == "ready",
                )
            )
        ).scalars()
        for img in imgs:
            out.append(
                {
                    "asset_id": str(img.id),
                    "kind": "image",
                    "storage_key": img.storage_key,
                    "content_type": img.content_type or "image/webp",
                    "sha256": img.archive_sha256 or ("0" * 64),
                    "width": img.width or 512,
                    "height": img.height or 512,
                }
            )
        vids = (
            await session.execute(
                select(VideoAssetRecord).where(
                    VideoAssetRecord.message_id == message_id,
                    VideoAssetRecord.archive_state == "ready",
                )
            )
        ).scalars()
        for vid in vids:
            frames = list(
                (
                    await session.execute(
                        select(VideoFrameRecord)
                        .where(
                            VideoFrameRecord.video_asset_id == vid.id,
                            VideoFrameRecord.archive_state == "ready",
                        )
                        .order_by(VideoFrameRecord.frame_index.asc())
                    )
                ).scalars()
            )
            out.append(
                {
                    "asset_id": str(vid.id),
                    "kind": "video",
                    "frames": [
                        {
                            "frame_index": fr.frame_index,
                            "storage_key": fr.storage_key,
                            "content_type": fr.content_type,
                            "sha256": fr.archive_sha256,
                            "width": fr.width,
                            "height": fr.height,
                        }
                        for fr in frames
                    ],
                }
            )
        return out


_FAMILY_PARENTS: dict[str, Any] = {
    "media-processing-profiles": MediaProcessingProfileRecord,
    "source-channel-profiles": SourceChannelProfile,
    "label-definitions": LabelDefinitionRecord,
    "label-sets": LabelSetRecord,
    "prompt-templates": PromptTemplateRecord,
    "inference-profiles": InferenceProfileRecord,
    "analysis-stages": AnalysisStageTemplateRecord,
    "analysis-pipelines": AnalysisPipelineRecord,
    "rendering-templates": RenderingTemplateRecord,
    "routing-policies": RoutingPolicyRecord,
}

_FAMILY_VERSIONS: dict[str, tuple[Any, str]] = {
    "media-processing-profiles": (
        MediaProcessingProfileVersionRecord,
        "media_processing_profile_id",
    ),
    "source-channel-profiles": (SourceChannelProfileVersion, "profile_id"),
    "label-definitions": (LabelDefinitionVersionRecord, "label_definition_id"),
    "label-sets": (LabelSetVersionRecord, "label_set_id"),
    "prompt-templates": (PromptTemplateVersionRecord, "prompt_template_id"),
    "inference-profiles": (InferenceProfileVersionRecord, "inference_profile_id"),
    "analysis-stages": (
        AnalysisStageTemplateVersionRecord,
        "analysis_stage_template_id",
    ),
    "analysis-pipelines": (AnalysisPipelineVersionRecord, "analysis_pipeline_id"),
    "rendering-templates": (RenderingTemplateVersionRecord, "rendering_template_id"),
    "routing-policies": (RoutingPolicyVersionRecord, "routing_policy_id"),
}


def _current_label_filter(*, label_key: str, namespace: str, scope: Any) -> Any:
    def latest(model: Any, name: str) -> Any:
        stmt = select(
            model.id,
            model.message_id,
            model.target_scope,
            model.target_id,
            model.label_definition_version_id,
            model.label_key,
            model.activated,
            *([model.operation] if model is ManualLabelAssignmentRecord else []),
            func.row_number()
            .over(
                partition_by=(
                    model.message_id,
                    model.target_scope,
                    model.target_id,
                    model.label_definition_version_id,
                ),
                order_by=(model.created_at.desc(), model.id.desc()),
            )
            .label("position"),
        )
        if model is ModelLabelAssignmentRecord:
            active_run = (
                select(AnalysisRunRecord.id)
                .where(
                    AnalysisRunRecord.message_id == model.message_id,
                    AnalysisRunRecord.run_mode.in_(("formal", "reanalysis")),
                    AnalysisRunRecord.status.in_(("completed", "blocked_negative_gate")),
                )
                .order_by(AnalysisRunRecord.created_at.desc(), AnalysisRunRecord.id.desc())
                .limit(1)
                .correlate(model)
                .scalar_subquery()
            )
            stmt = stmt.where(model.analysis_run_id == active_run)
        return stmt.subquery(name)

    model = latest(ModelLabelAssignmentRecord, "current_model_labels")
    manual = latest(ManualLabelAssignmentRecord, "current_manual_labels")

    def matches(row: Any) -> list[Any]:
        predicates = [
            row.c.position == 1,
            row.c.message_id == MessageRecord.id,
            row.c.label_key == label_key,
            row.c.activated.is_(True),
        ]
        if scope in ("global", "media"):
            predicates.append(row.c.target_scope == scope)
        return predicates

    manual_active = exists(select(manual.c.id).where(*matches(manual), manual.c.operation == "set"))
    if namespace == "manual":
        return manual_active
    model_predicates = matches(model)
    if namespace != "model":
        # A latest SET replaces the model even when false. CLEAR restores it.
        override = exists(
            select(manual.c.id).where(
                manual.c.position == 1,
                manual.c.operation == "set",
                manual.c.message_id == model.c.message_id,
                manual.c.target_scope == model.c.target_scope,
                manual.c.target_id == model.c.target_id,
                manual.c.label_definition_version_id == model.c.label_definition_version_id,
            )
        ).correlate(model)
        model_predicates.append(~override)
    model_active = exists(select(model.c.id).where(*model_predicates))
    return model_active if namespace == "model" else or_(model_active, manual_active)


async def _upsert_wakeup(
    session: AsyncSession,
    *,
    queue: str,
    entity_id: UUID,
    now: datetime,
) -> None:
    stmt = (
        postgresql_insert(DurableWakeupRecord)
        .values(
            id=uuid4(),
            queue=queue,
            entity_id=entity_id,
            status="pending",
            next_attempt_at=now,
            dispatch_attempts=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_durable_wakeup_queue_entity",
            set_={
                "status": "pending",
                "next_attempt_at": now,
                "lease_token": None,
                "lease_expires_at": None,
                "completed_at": None,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)


async def _reset_db_step(
    *,
    session: AsyncSession,
    step_kind: str,
    uid: UUID,
    now: datetime,
) -> tuple[str, str]:
    if step_kind == "stage_run":
        sr = await session.get(StageRunRecord, uid, with_for_update=True)
        if sr is None:
            raise AdminNotFoundError("stage_run does not exist")
        if sr.status != "failed":
            raise AdminConflictError("only a failed stage run can be retried")
        run = await session.get(AnalysisRunRecord, sr.analysis_run_id, with_for_update=True)
        if run is None or run.status not in {"failed", "running", "pending"}:
            raise AdminConflictError("analysis run cannot be retried")
        msg = await session.get(MessageRecord, sr.message_id, with_for_update=True)
        if msg is None:
            raise AdminNotFoundError("message does not exist")
        prev = sr.status
        sr.status = "pending"
        sr.attempt_count = 0
        sr.lease_token = None
        sr.lease_expires_at = None
        sr.next_retry_at = None
        sr.completed_at = None
        sr.updated_at = now
        run.status = "running"
        run.completed_at = None
        run.started_at = run.started_at or now
        run.last_error_code = None
        run.last_error_type = None
        run.updated_at = now
        if run.run_mode == "formal" and msg.processing_status == "failed":
            msg.processing_status = "processing"
            msg.terminal_reason = None
            msg.updated_at = now
        return prev, "analysis"
    if step_kind == "publication_intent":
        pi = await session.get(PublicationIntentRecord, uid, with_for_update=True)
        if pi is None:
            raise AdminNotFoundError("publication_intent does not exist")
        if pi.status != "failed":
            raise AdminConflictError("only a failed publication intent can be retried")
        prev = pi.status
        pi.status = "pending" if not pi.completed_parts else "partial"
        # Reset only this retry cycle's budget. Publication claim allocates a
        # separate monotonic audit attempt_number from the retained history.
        pi.attempt_count = 0
        pi.lease_token = None
        pi.lease_expires_at = None
        pi.next_retry_at = None
        pi.completed_at = None
        pi.updated_at = now
        return prev, "publication"
    if step_kind == "image_archive":
        img = await session.get(ImageAssetRecord, uid, with_for_update=True)
        if img is None:
            raise AdminNotFoundError("image_asset does not exist")
        prev = img.archive_state
        img.archive_state = "pending"
        img.archive_attempt_count = 0
        img.archive_lease_token = None
        img.archive_lease_expires_at = None
        img.archive_next_retry_at = None
        img.updated_at = now
        return prev, "image_archive"
    if step_kind == "video_archive":
        vid = await session.get(VideoAssetRecord, uid, with_for_update=True)
        if vid is None:
            raise AdminNotFoundError("video_asset does not exist")
        prev = vid.archive_state
        vid.archive_state = "pending"
        vid.archive_attempt_count = 0
        vid.archive_lease_token = None
        vid.archive_lease_expires_at = None
        vid.archive_next_retry_at = None
        vid.updated_at = now
        return prev, "video_archive"
    if step_kind == "range_execution":
        rex = await session.get(RangeExecutionRecord, uid, with_for_update=True)
        if rex is None:
            raise AdminNotFoundError("range_execution does not exist")
        prev = rex.status
        rex.status = "pending"
        rex.attempt_count = 0
        rex.lease_token = None
        rex.lease_expires_at = None
        rex.next_retry_at = None
        rex.completed_at = None
        rex.updated_at = now
        return prev, "range_execution"
    if step_kind == "cleanup_job":
        cj = await session.get(ArchiveCleanupJobRecord, uid, with_for_update=True)
        if cj is None:
            raise AdminNotFoundError("cleanup_job does not exist")
        prev = cj.status
        cj.status = "pending"
        cj.attempt_count = 0
        cj.lease_token = None
        cj.lease_expires_at = None
        cj.next_retry_at = None
        cj.completed_at = None
        cj.updated_at = now
        return prev, "maintenance"
    raise AdminValidationError(f"unsupported step_kind: {step_kind}")


def _require_uuid(value: Any, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        raise AdminValidationError(f"{field} must be a valid UUID")
    try:
        return UUID(value.strip())
    except ValueError as exc:
        raise AdminValidationError(f"{field} must be a valid UUID") from exc


def _try_uuid(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value.strip())
        except ValueError:
            return None
    return None


def _serialize_identity(r: TelegramIdentity) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "name": r.name,
        "identity_type": r.identity_type,
        "enabled": r.enabled,
        "health_status": r.health_status,
        "last_connected_at": r.last_connected_at.isoformat() if r.last_connected_at else None,
        "last_error": r.last_error,
        "secret_id": str(r.secret_id),
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


def _serialize_source_channel(r: SourceChannel) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "telegram_peer_id": r.telegram_peer_id,
        "username": r.username,
        "display_name": r.display_name,
        "default_read_identity_id": str(r.default_read_identity_id),
        "active_profile_version_id": (
            str(r.active_profile_version_id) if r.active_profile_version_id else None
        ),
        "enabled": r.enabled,
        "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
        "latest_seen_message_id": r.latest_seen_message_id,
        "last_seen_message_id": r.last_seen_message_id,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


def _serialize_destination_channel(r: DestinationChannel) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "name": r.name,
        "telegram_peer_id": r.telegram_peer_id,
        "username": r.username,
        "default_publish_identity_id": str(r.default_publish_identity_id),
        "enabled": r.enabled,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


def _serialize_range(r: ProcessingRangeRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "source_channel_id": str(r.source_channel_id),
        "start_at": r.start_at.isoformat(),
        "end_mode": r.end_mode,
        "end_at": r.end_at.isoformat() if r.end_at else None,
        "steady_after_seconds": r.steady_after_seconds,
        "resolved_start_message_id": r.resolved_start_message_id,
        "resolved_fixed_end_message_id": r.resolved_fixed_end_message_id,
        "processing_watermark_message_id": r.processing_watermark_message_id,
        "active_high_watermark_message_id": r.active_high_watermark_message_id,
        "status": r.status,
        "enabled": r.enabled,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


def _serialize_execution(r: RangeExecutionRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "processing_range_id": str(r.processing_range_id),
        "source_profile_version_id": str(r.source_profile_version_id),
        "from_message_id_exclusive": r.from_message_id_exclusive,
        "to_message_id_inclusive": r.to_message_id_inclusive,
        "watermark_message_id": r.watermark_message_id,
        "status": r.status,
        "attempt_count": r.attempt_count,
        "max_attempts": r.max_attempts,
        "last_error_code": r.last_error_code,
        "last_error_type": r.last_error_type,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "created_at": r.created_at.isoformat(),
    }


def _serialize_message(r: MessageRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "source_channel_id": str(r.source_channel_id),
        "telegram_message_ids": list(r.telegram_message_ids),
        "telegram_grouped_id": r.telegram_grouped_id,
        "primary_telegram_message_id": r.primary_telegram_message_id,
        "published_at": r.published_at.isoformat(),
        "edited_at": r.edited_at.isoformat() if r.edited_at else None,
        "original_text": r.original_text,
        "telegram_metadata": dict(r.telegram_metadata),
        "processing_status": r.processing_status,
        "terminal_reason": r.terminal_reason,
        "failure_stage": r.failure_stage,
        "last_error_code": r.last_error_code,
        "review_status": r.review_status,
        "media_count": r.media_count,
        "visual_fingerprint": r.visual_fingerprint,
        "canonical_message_id": str(r.canonical_message_id) if r.canonical_message_id else None,
        "duplicate_status": r.duplicate_status,
        "duplicate_distance": r.duplicate_distance,
        "blocked_from_analysis": r.blocked_from_analysis,
        "blocked_at": r.blocked_at.isoformat() if r.blocked_at else None,
        "blocked_by_stage_run_id": (
            str(r.blocked_by_stage_run_id) if r.blocked_by_stage_run_id else None
        ),
        "blocked_by_label_assignment_id": (
            str(r.blocked_by_label_assignment_id) if r.blocked_by_label_assignment_id else None
        ),
        "source_changed_after_processing": r.source_changed_after_processing,
        "source_deleted": r.source_deleted,
        "source_deleted_at": r.source_deleted_at.isoformat() if r.source_deleted_at else None,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


def _serialize_part(r: MessagePartRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "telegram_message_id": r.telegram_message_id,
        "published_at": r.published_at.isoformat(),
        "edited_at": r.edited_at.isoformat() if r.edited_at else None,
        "original_text": r.original_text,
        "media_count": r.media_count,
        "media": list(r.media),
    }


def _serialize_image(r: ImageAssetRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "source_asset_id": r.source_asset_id,
        "archive_state": r.archive_state,
        "storage_key": r.storage_key,
        "content_type": r.content_type,
        "width": r.width,
        "height": r.height,
        "perceptual_hash": r.perceptual_hash,
        "archive_sha256": r.archive_sha256,
        "archive_size_bytes": r.archive_size_bytes,
    }


def _serialize_video(r: VideoAssetRecord, frames: list[VideoFrameRecord]) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "source_asset_id": r.source_asset_id,
        "archive_state": r.archive_state,
        "duration_seconds": r.duration_seconds,
        "source_width": r.source_width,
        "source_height": r.source_height,
        "source_cover_phash": r.source_cover_phash,
        "frames": [
            {
                "id": str(f.id),
                "frame_role": f.frame_role,
                "frame_index": f.frame_index,
                "timestamp_seconds": f.timestamp_seconds,
                "storage_key": f.storage_key,
                "archive_state": f.archive_state,
                "archive_size_bytes": f.archive_size_bytes,
            }
            for f in frames
        ],
    }


def _serialize_stage_run(
    sr: StageRunRecord,
    man: InputManifestRecord | None,
    call: InferenceCallRecord | None,
) -> dict[str, Any]:
    return {
        "id": str(sr.id),
        "analysis_stage_template_version_id": str(sr.analysis_stage_template_version_id),
        "target_scope": sr.target_scope,
        "target_kind": sr.target_kind,
        "target_id": str(sr.target_id),
        "status": sr.status,
        "attempt_count": sr.attempt_count,
        "result_origin": sr.result_origin,
        "reused_from_stage_run_id": (
            str(sr.reused_from_stage_run_id) if sr.reused_from_stage_run_id else None
        ),
        "last_error_code": sr.last_error_code,
        "last_error_type": sr.last_error_type,
        "input_manifest": (
            {
                "id": str(man.id),
                "input_manifest_hash": man.input_manifest_hash,
                "content": dict(man.content),
            }
            if man
            else None
        ),
        "inference_call": (
            {
                "id": str(call.id),
                "provider_adapter": call.provider_adapter,
                "model_name": call.model_name,
                "status": call.status,
                "structured_output_schema_hash": call.structured_output_schema_hash,
                "token_usage": dict(call.token_usage) if call.token_usage else None,
                "latency_ms": call.latency_ms,
                "error_code": call.error_code,
            }
            if call
            else None
        ),
    }


def _serialize_analysis_run(
    run: AnalysisRunRecord,
    stage_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "analysis_pipeline_version_id": str(run.analysis_pipeline_version_id),
        "run_mode": run.run_mode,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "stage_runs": stage_runs,
    }


def _serialize_evaluation(ev: RoutingEvaluationRecord) -> dict[str, Any]:
    return {
        "id": str(ev.id),
        "routing_request_id": ev.routing_request_id,
        "routing_policy_version_id": str(ev.routing_policy_version_id),
        "facts_hash": ev.facts_hash,
        "facts_snapshot": dict(ev.facts_snapshot),
        "rule_outcomes": list(ev.rule_outcomes),
        "stopped_at_rule_id": str(ev.stopped_at_rule_id) if ev.stopped_at_rule_id else None,
        "created_at": ev.created_at.isoformat(),
    }


def _serialize_intent(
    pi: PublicationIntentRecord,
    attempts: list[PublishAttemptRecord],
    records: list[PublishRecord],
) -> dict[str, Any]:
    return {
        "id": str(pi.id),
        "routing_evaluation_id": str(pi.routing_evaluation_id),
        "destination_channel_id": str(pi.destination_channel_id),
        "publish_identity_id": str(pi.publish_identity_id),
        "publication_mode": pi.publication_mode,
        "protected_content_fallback": pi.protected_content_fallback,
        "rendered_text": pi.rendered_text,
        "status": pi.status,
        "attempt_count": pi.attempt_count,
        "last_error_code": pi.last_error_code,
        "attempts": [
            {
                "id": str(a.id),
                "attempt_number": a.attempt_number,
                "status": a.status,
                "error_code": a.error_code,
                "reconciled": a.reconciled,
                "started_at": a.started_at.isoformat(),
            }
            for a in attempts
        ],
        "records": [
            {
                "id": str(r.id),
                "part": r.part,
                "operation": r.operation,
                "remote_message_ids": list(r.remote_message_ids),
                "sent_at": r.sent_at.isoformat(),
            }
            for r in records
        ],
    }


def _serialize_dry_run(r: ArchiveCleanupDryRunRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "actor_admin_user_id": str(r.actor_admin_user_id) if r.actor_admin_user_id else None,
        "filter_snapshot": dict(r.filter_snapshot),
        "filter_hash": r.filter_hash,
        "estimated_asset_count": r.estimated_asset_count,
        "estimated_message_count": r.estimated_message_count,
        "estimated_bytes": r.estimated_bytes,
        "created_at": r.created_at.isoformat(),
    }


def _serialize_cleanup_job(r: ArchiveCleanupJobRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "dry_run_id": str(r.dry_run_id),
        "confirmed_by_admin_user_id": (
            str(r.confirmed_by_admin_user_id) if r.confirmed_by_admin_user_id else None
        ),
        "filter_hash": r.filter_hash,
        "estimated_asset_count": r.estimated_asset_count,
        "estimated_message_count": r.estimated_message_count,
        "estimated_bytes": r.estimated_bytes,
        "item_count": r.item_count,
        "status": r.status,
        "attempt_count": r.attempt_count,
        "last_error_code": r.last_error_code,
        "created_at": r.created_at.isoformat(),
    }


def _serialize_audit(r: AuditEvent) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "actor_admin_user_id": str(r.actor_admin_user_id) if r.actor_admin_user_id else None,
        "event_type": r.event_type,
        "entity_type": r.entity_type,
        "entity_id": str(r.entity_id) if r.entity_id else None,
        "payload": dict(r.payload),
        "created_at": r.created_at.isoformat(),
    }
