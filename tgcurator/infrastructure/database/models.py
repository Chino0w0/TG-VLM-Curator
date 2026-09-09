from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKey


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint("char_length(btrim(username)) > 0", name="admin_username_not_blank"),
        Index(
            "uq_admin_users_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class EncryptedSecret(CreatedAtMixin, Base):
    __tablename__ = "encrypted_secrets"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(secret_type)) > 0",
            name="encrypted_secret_type_not_blank",
        ),
        CheckConstraint(
            "char_length(btrim(key_id)) > 0",
            name="encrypted_secret_key_id_not_blank",
        ),
        CheckConstraint("octet_length(nonce) = 12", name="encrypted_secret_nonce_size"),
        CheckConstraint(
            "octet_length(ciphertext) >= 16",
            name="encrypted_secret_ciphertext_tag",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    secret_type: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)


class AuditEvent(CreatedAtMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("char_length(btrim(event_type)) > 0", name="audit_event_type_not_blank"),
        CheckConstraint("char_length(btrim(entity_type)) > 0", name="audit_entity_type_not_blank"),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    id: Mapped[UUIDPrimaryKey]
    actor_admin_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class TelegramIdentity(TimestampMixin, Base):
    __tablename__ = "telegram_identities"
    __table_args__ = (
        CheckConstraint(
            "identity_type IN ('mtproto_user', 'bot')",
            name="telegram_identity_type",
        ),
        CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'failed')",
            name="telegram_identity_health",
        ),
        CheckConstraint(
            "char_length(btrim(name)) > 0",
            name="telegram_identity_name_not_blank",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    identity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    health_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default=text("'unknown'"),
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_id: Mapped[UUID] = mapped_column(
        ForeignKey("encrypted_secrets.id", ondelete="RESTRICT"),
        nullable=False,
    )


class SourceChannel(TimestampMixin, Base):
    __tablename__ = "source_channels"
    __table_args__ = (
        CheckConstraint("telegram_peer_id <> 0", name="source_channel_peer_nonzero"),
        CheckConstraint(
            "latest_seen_message_id IS NULL OR latest_seen_message_id > 0",
            name="source_channel_latest_message_positive",
        ),
        CheckConstraint(
            "last_seen_message_id IS NULL OR last_seen_message_id > 0",
            name="source_channel_last_seen_positive",
        ),
        UniqueConstraint("telegram_peer_id", name="uq_source_channel_peer"),
    )

    id: Mapped[UUIDPrimaryKey]
    telegram_peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    default_read_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    active_profile_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "source_channel_profile_versions.id",
            name="fk_source_channels_active_profile_version",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    latest_seen_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_seen_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DestinationChannel(TimestampMixin, Base):
    __tablename__ = "destination_channels"
    __table_args__ = (
        CheckConstraint("telegram_peer_id <> 0", name="destination_channel_peer_nonzero"),
        CheckConstraint(
            "char_length(btrim(name)) > 0",
            name="destination_channel_name_not_blank",
        ),
        UniqueConstraint("telegram_peer_id", name="uq_destination_channel_peer"),
    )

    id: Mapped[UUIDPrimaryKey]
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    telegram_peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    default_publish_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "telegram_identities.id",
            name="fk_destination_publish_identity",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class SourceChannelProfile(TimestampMixin, Base):
    __tablename__ = "source_channel_profiles"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(name)) > 0",
            name="profile_name_not_blank",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    source_channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channels.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class SourceChannelProfileVersion(CreatedAtMixin, Base):
    __tablename__ = "source_channel_profile_versions"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "version_number",
            name="uq_source_profile_version_number",
        ),
        UniqueConstraint(
            "profile_id",
            "content_hash",
            name="uq_source_profile_content_hash",
        ),
        CheckConstraint("version_number > 0", name="version_positive"),
        CheckConstraint(
            "state IN ('draft', 'published', 'retired')",
            name="state_valid",
        ),
        CheckConstraint(
            "(state = 'draft' AND published_at IS NULL) "
            "OR (state IN ('published', 'retired') AND published_at IS NOT NULL)",
            name="published_at_state",
        ),
        CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="content_object",
        ),
        CheckConstraint(
            "char_length(content_hash) = 64 AND content_hash !~ '[^0-9a-f]'",
            name="hash_format",
        ),
        Index(
            "uq_source_profile_one_published",
            "profile_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "source_channel_profiles.id",
            name="fk_profile_versions_profile",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ProcessingRangeRecord(TimestampMixin, Base):
    __tablename__ = "processing_ranges"
    __table_args__ = (
        CheckConstraint(
            "end_mode IN ('fixed', 'latest')",
            name="processing_range_end_mode",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'paused', 'completed', 'failed')",
            name="processing_range_status",
        ),
        CheckConstraint(
            "(end_mode = 'fixed' AND end_at IS NOT NULL AND steady_after_seconds IS NULL) "
            "OR (end_mode = 'latest' AND end_at IS NULL AND steady_after_seconds > 0)",
            name="processing_range_boundary_fields",
        ),
        CheckConstraint(
            "end_at IS NULL OR end_at > start_at",
            name="processing_range_fixed_order",
        ),
        CheckConstraint(
            "resolved_start_message_id IS NULL OR resolved_start_message_id > 0",
            name="processing_range_start_message_positive",
        ),
        CheckConstraint(
            "resolved_fixed_end_message_id IS NULL OR resolved_fixed_end_message_id > 0",
            name="processing_range_end_message_positive",
        ),
        CheckConstraint(
            "processing_watermark_message_id IS NULL OR processing_watermark_message_id >= 0",
            name="processing_range_watermark_nonnegative",
        ),
        CheckConstraint(
            "active_high_watermark_message_id IS NULL OR active_high_watermark_message_id > 0",
            name="processing_range_active_high_positive",
        ),
        CheckConstraint(
            "processing_watermark_message_id IS NULL "
            "OR active_high_watermark_message_id IS NULL "
            "OR processing_watermark_message_id <= active_high_watermark_message_id",
            name="processing_range_watermark_high_ceiling",
        ),
        CheckConstraint(
            "processing_watermark_message_id IS NULL "
            "OR resolved_start_message_id IS NULL "
            "OR processing_watermark_message_id >= resolved_start_message_id - 1",
            name="processing_range_watermark_start_floor",
        ),
        CheckConstraint(
            "end_mode <> 'fixed' "
            "OR processing_watermark_message_id IS NULL "
            "OR resolved_fixed_end_message_id IS NULL "
            "OR processing_watermark_message_id <= resolved_fixed_end_message_id",
            name="processing_range_watermark_fixed_ceiling",
        ),
        Index("ix_processing_ranges_source_status", "source_channel_id", "status"),
    )

    id: Mapped[UUIDPrimaryKey]
    source_channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_start_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolved_fixed_end_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    processing_watermark_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active_high_watermark_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    steady_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class RangeExecutionRecord(TimestampMixin, Base):
    __tablename__ = "range_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'completed', 'failed')",
            name="range_execution_status",
        ),
        CheckConstraint(
            "from_message_id_exclusive >= 0 "
            "AND to_message_id_inclusive > from_message_id_exclusive",
            name="range_execution_bounds",
        ),
        CheckConstraint(
            "watermark_message_id >= from_message_id_exclusive "
            "AND watermark_message_id <= to_message_id_inclusive",
            name="range_execution_watermark_bounds",
        ),
        CheckConstraint(
            "max_attempts > 0 AND attempt_count >= 0 AND attempt_count <= max_attempts",
            name="range_execution_attempts",
        ),
        CheckConstraint(
            "status <> 'completed' "
            "OR (watermark_message_id = to_message_id_inclusive AND completed_at IS NOT NULL)",
            name="range_execution_completion",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="range_execution_lease",
        ),
        CheckConstraint(
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) "
            "OR (status <> 'retry_wait' AND next_retry_at IS NULL)",
            name="range_execution_retry_schedule",
        ),
        CheckConstraint(
            "status NOT IN ('retry_wait', 'failed') "
            "OR (last_error_code IS NOT NULL "
            "AND last_error_type IS NOT NULL AND last_failure_at IS NOT NULL)",
            name="range_execution_failure_metadata",
        ),
        UniqueConstraint(
            "processing_range_id",
            "from_message_id_exclusive",
            "to_message_id_inclusive",
            name="uq_range_execution_bounds",
        ),
        Index("ix_range_executions_status_lease", "status", "lease_expires_at"),
        Index("ix_range_executions_retry_due", "status", "next_retry_at"),
        Index(
            "uq_range_execution_one_active",
            "processing_range_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running', 'retry_wait')"),
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    processing_range_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_ranges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_profile_version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "source_channel_profile_versions.id",
            name="fk_range_executions_profile_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    from_message_id_exclusive: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_message_id_inclusive: Mapped[int] = mapped_column(BigInteger, nullable=False)
    watermark_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default=text("5"),
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DurableWakeupRecord(TimestampMixin, Base):
    __tablename__ = "durable_wakeups"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'cancelled')",
            name="durable_wakeup_status",
        ),
        CheckConstraint("dispatch_attempts >= 0", name="durable_wakeup_attempts"),
        CheckConstraint(
            "(status = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="durable_wakeup_lease",
        ),
        UniqueConstraint("queue", "entity_id", name="uq_durable_wakeup_queue_entity"),
        Index("ix_durable_wakeups_due", "status", "next_attempt_at"),
    )

    id: Mapped[UUIDPrimaryKey]
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dispatch_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageRecord(TimestampMixin, Base):
    """One logical Telegram message; native media groups retain all component IDs."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "cardinality(telegram_message_ids) > 0",
            name="message_parts_not_empty",
        ),
        CheckConstraint(
            "primary_telegram_message_id = ANY(telegram_message_ids)",
            name="message_primary_in_parts",
        ),
        CheckConstraint(
            "primary_telegram_message_id > 0",
            name="message_primary_positive",
        ),
        CheckConstraint(
            "telegram_grouped_id IS NULL OR telegram_grouped_id > 0",
            name="message_grouped_positive",
        ),
        CheckConstraint(
            "edited_at IS NULL OR edited_at >= published_at",
            name="message_edited_after_published",
        ),
        CheckConstraint("media_count >= 0", name="message_media_count_nonnegative"),
        CheckConstraint(
            "processing_status IN ('ingested', 'processing', 'processed', 'failed')",
            name="message_processing_status",
        ),
        CheckConstraint(
            "visual_fingerprint IS NULL "
            "OR (char_length(visual_fingerprint) = 64 "
            "AND visual_fingerprint !~ '[^0-9a-f]')",
            name="message_visual_fingerprint_format",
        ),
        CheckConstraint(
            "(source_deleted IS FALSE AND source_deleted_at IS NULL) OR "
            "(source_deleted IS TRUE AND source_deleted_at IS NOT NULL)",
            name="message_source_deleted_state",
        ),
        CheckConstraint(
            "source_deleted_at IS NULL OR source_deleted_at >= published_at",
            name="message_source_deleted_after_published",
        ),
        Index("ix_messages_source_published_at", "source_channel_id", "published_at"),
        Index(
            "uq_messages_source_regular",
            "source_channel_id",
            "primary_telegram_message_id",
            unique=True,
            postgresql_where=text("telegram_grouped_id IS NULL"),
        ),
        Index(
            "uq_messages_source_grouped",
            "source_channel_id",
            "telegram_grouped_id",
            unique=True,
            postgresql_where=text("telegram_grouped_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    source_channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    telegram_message_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    telegram_grouped_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    primary_telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    processing_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ingested",
        server_default=text("'ingested'"),
    )
    media_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    visual_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_changed_after_processing: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    source_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    source_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MessagePartRecord(TimestampMixin, Base):
    """A rich, independently editable Telegram component snapshot."""

    __tablename__ = "message_parts"
    __table_args__ = (
        UniqueConstraint(
            "source_channel_id",
            "telegram_message_id",
            name="uq_message_part_source_telegram",
        ),
        CheckConstraint(
            "telegram_message_id > 0",
            name="message_part_telegram_message_positive",
        ),
        CheckConstraint(
            "edited_at IS NULL OR edited_at >= published_at",
            name="message_part_edited_after_published",
        ),
        CheckConstraint("media_count >= 0", name="message_part_media_count_nonnegative"),
        CheckConstraint(
            "jsonb_typeof(media) = 'array' AND jsonb_array_length(media) = media_count",
            name="message_part_media_snapshot_count",
        ),
        Index("ix_message_parts_message", "message_id"),
    )

    id: Mapped[UUIDPrimaryKey]
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    source_channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channels.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    media: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    media_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )


class ImageAssetRecord(TimestampMixin, Base):
    """One source image with bounded durable archive attempts and immutable READY facts."""

    __tablename__ = "image_assets"
    __table_args__ = (
        UniqueConstraint("message_id", "source_asset_id", name="uq_image_asset_message_source"),
        CheckConstraint(
            "archive_state IN ('pending', 'processing', 'retry_wait', 'ready', "
            "'deleted', 'failed')",
            name="image_asset_archive_state",
        ),
        CheckConstraint(
            "source_telegram_message_id IS NULL OR source_telegram_message_id > 0",
            name="image_asset_source_message_positive",
        ),
        CheckConstraint(
            "archive_attempt_count >= 0 AND archive_attempt_count <= archive_max_attempts "
            "AND archive_max_attempts > 0",
            name="image_asset_archive_attempts",
        ),
        CheckConstraint(
            "(archive_state = 'processing' AND archive_lease_token IS NOT NULL "
            "AND archive_lease_expires_at IS NOT NULL AND archive_attempt_count > 0) OR "
            "(archive_state <> 'processing' AND archive_lease_token IS NULL "
            "AND archive_lease_expires_at IS NULL)",
            name="image_asset_archive_lease",
        ),
        CheckConstraint(
            "(archive_state = 'retry_wait' AND archive_next_retry_at IS NOT NULL) OR "
            "(archive_state <> 'retry_wait' AND archive_next_retry_at IS NULL)",
            name="image_asset_archive_retry_schedule",
        ),
        CheckConstraint(
            "archive_state NOT IN ('retry_wait', 'failed') OR "
            "(archive_last_error_code IS NOT NULL AND archive_last_error_type IS NOT NULL "
            "AND archive_last_failure_at IS NOT NULL)",
            name="image_asset_archive_failure_metadata",
        ),
        CheckConstraint(
            "archive_state <> 'ready' OR (storage_backend IS NOT NULL AND storage_key IS NOT NULL "
            "AND content_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL "
            "AND source_sha256 IS NOT NULL AND archive_sha256 IS NOT NULL "
            "AND perceptual_hash IS NOT NULL AND archive_size_bytes IS NOT NULL "
            "AND archive_ready_at IS NOT NULL)",
            name="image_asset_ready_metadata",
        ),
        CheckConstraint(
            "archive_state <> 'deleted' OR archive_deleted_at IS NOT NULL",
            name="image_asset_deleted_timestamp",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="image_asset_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="image_asset_height_positive"),
        CheckConstraint(
            "archive_size_bytes IS NULL OR archive_size_bytes > 0",
            name="image_asset_archive_size_positive",
        ),
        Index("ix_image_assets_archive_state", "archive_state"),
        Index("ix_image_assets_archive_due", "archive_state", "archive_next_retry_at"),
        Index("ix_image_assets_archive_lease", "archive_state", "archive_lease_expires_at"),
    )

    id: Mapped[UUIDPrimaryKey]
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    source_asset_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_phash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    archive_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    archive_max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=text("5")
    )
    archive_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archive_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_last_error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    storage_backend: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    archive_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class VideoAssetRecord(TimestampMixin, Base):
    """One source video whose durable visual evidence is stored in video_frames."""

    __tablename__ = "video_assets"
    __table_args__ = (
        UniqueConstraint("message_id", "source_asset_id", name="uq_video_asset_message_source"),
        CheckConstraint(
            "archive_state IN ('pending', 'processing', 'retry_wait', 'ready', "
            "'deleted', 'failed')",
            name="video_asset_archive_state",
        ),
        CheckConstraint(
            "source_telegram_message_id IS NULL OR source_telegram_message_id > 0",
            name="video_asset_source_message_positive",
        ),
        CheckConstraint(
            "archive_attempt_count >= 0 AND archive_attempt_count <= archive_max_attempts "
            "AND archive_max_attempts > 0",
            name="video_asset_archive_attempts",
        ),
        CheckConstraint(
            "(archive_state = 'processing' AND archive_lease_token IS NOT NULL "
            "AND archive_lease_expires_at IS NOT NULL AND archive_attempt_count > 0) OR "
            "(archive_state <> 'processing' AND archive_lease_token IS NULL "
            "AND archive_lease_expires_at IS NULL)",
            name="video_asset_archive_lease",
        ),
        CheckConstraint(
            "(archive_state = 'retry_wait' AND archive_next_retry_at IS NOT NULL) OR "
            "(archive_state <> 'retry_wait' AND archive_next_retry_at IS NULL)",
            name="video_asset_archive_retry_schedule",
        ),
        CheckConstraint(
            "archive_state NOT IN ('retry_wait', 'failed') OR "
            "(archive_last_error_code IS NOT NULL AND archive_last_error_type IS NOT NULL "
            "AND archive_last_failure_at IS NOT NULL)",
            name="video_asset_archive_failure_metadata",
        ),
        CheckConstraint(
            "archive_state <> 'ready' OR (duration_seconds IS NOT NULL "
            "AND source_sha256 IS NOT NULL AND archive_ready_at IS NOT NULL)",
            name="video_asset_ready_metadata",
        ),
        CheckConstraint(
            "archive_state <> 'deleted' OR archive_deleted_at IS NOT NULL",
            name="video_asset_deleted_timestamp",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds > 0",
            name="video_asset_duration_positive",
        ),
        CheckConstraint(
            "source_size_bytes IS NULL OR source_size_bytes > 0",
            name="video_asset_source_size_positive",
        ),
        Index("ix_video_assets_archive_state", "archive_state"),
        Index("ix_video_assets_archive_due", "archive_state", "archive_next_retry_at"),
        Index("ix_video_assets_archive_lease", "archive_state", "archive_lease_expires_at"),
    )

    id: Mapped[UUIDPrimaryKey]
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    source_asset_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_cover_phash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    archive_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    archive_max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=text("5")
    )
    archive_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archive_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_last_error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archive_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class VideoFrameRecord(TimestampMixin, Base):
    """A long-lived normalized cover or representative frame for one video."""

    __tablename__ = "video_frames"
    __table_args__ = (
        UniqueConstraint(
            "video_asset_id", "frame_role", "frame_index", name="uq_video_frame_role_index"
        ),
        UniqueConstraint("storage_backend", "storage_key", name="uq_video_frame_storage_key"),
        CheckConstraint(
            "frame_role IN ('cover', 'representative')",
            name="video_frame_role",
        ),
        CheckConstraint(
            "frame_index >= 0 AND (frame_role <> 'cover' OR frame_index = 0)",
            name="video_frame_index",
        ),
        CheckConstraint("timestamp_seconds >= 0", name="video_frame_timestamp_nonnegative"),
        CheckConstraint("content_type = 'image/webp'", name="video_frame_content_type"),
        CheckConstraint("width > 0", name="video_frame_width_positive"),
        CheckConstraint("height > 0", name="video_frame_height_positive"),
        CheckConstraint("archive_size_bytes > 0", name="video_frame_archive_size_positive"),
        Index("ix_video_frames_video", "video_asset_id", "frame_role", "frame_index"),
    )

    id: Mapped[UUIDPrimaryKey]
    video_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("video_assets.id", ondelete="CASCADE"), nullable=False
    )
    frame_role: Mapped[str] = mapped_column(String(24), nullable=False)
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    perceptual_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    archive_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
