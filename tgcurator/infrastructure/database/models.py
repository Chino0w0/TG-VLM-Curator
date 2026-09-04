from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKey


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        Index(
            "uq_admin_users_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EncryptedSecret(CreatedAtMixin, Base):
    __tablename__ = "encrypted_secrets"

    id: Mapped[UUIDPrimaryKey]
    secret_type: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)


class TelegramIdentity(TimestampMixin, Base):
    __tablename__ = "telegram_identities"
    __table_args__ = (
        CheckConstraint(
            "identity_type IN ('mtproto_user', 'bot')", name="ck_telegram_identity_type"
        ),
        CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'failed')",
            name="ck_telegram_identity_health",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    identity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_id: Mapped[UUID] = mapped_column(
        ForeignKey("encrypted_secrets.id", ondelete="RESTRICT"), nullable=False
    )


class SourceChannel(TimestampMixin, Base):
    __tablename__ = "source_channels"
    __table_args__ = (
        UniqueConstraint(
            "telegram_identity_id", "telegram_channel_id", name="uq_source_channel_identity_remote"
        ),
        CheckConstraint(
            "last_seen_message_id IS NULL OR last_seen_message_id > 0",
            name="ck_source_channel_last_seen_positive",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    telegram_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_identities.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DestinationChannel(TimestampMixin, Base):
    __tablename__ = "destination_channels"
    __table_args__ = (
        UniqueConstraint(
            "telegram_identity_id",
            "telegram_channel_id",
            name="uq_destination_channel_identity_remote",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    telegram_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_identities.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SourceChannelProfile(TimestampMixin, Base):
    __tablename__ = "source_channel_profiles"

    id: Mapped[UUIDPrimaryKey]
    source_channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channels.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class SourceChannelProfileVersion(CreatedAtMixin, Base):
    __tablename__ = "source_channel_profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version_number", name="uq_source_profile_version_number"),
        UniqueConstraint("profile_id", "content_hash", name="uq_source_profile_content_hash"),
        CheckConstraint(
            "state IN ('draft', 'published', 'retired')", name="ck_source_profile_version_state"
        ),
        CheckConstraint(
            "(state = 'draft' AND published_at IS NULL) "
            "OR (state IN ('published', 'retired') AND published_at IS NOT NULL)",
            name="ck_source_profile_version_published_at",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channel_profiles.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessingRange(TimestampMixin, Base):
    __tablename__ = "processing_ranges"
    __table_args__ = (
        CheckConstraint(
            "boundary_kind IN ('fixed', 'latest')", name="ck_processing_range_boundary_kind"
        ),
        CheckConstraint(
            "(boundary_kind = 'fixed' "
            "AND fixed_end_at IS NOT NULL "
            "AND latest_quiet_seconds IS NULL) "
            "OR (boundary_kind = 'latest' "
            "AND fixed_end_at IS NULL "
            "AND latest_quiet_seconds IS NOT NULL)",
            name="ck_processing_range_boundary_fields",
        ),
        CheckConstraint(
            "fixed_end_at IS NULL OR fixed_end_at > start_at",
            name="ck_processing_range_fixed_order",
        ),
        CheckConstraint(
            "latest_quiet_seconds IS NULL OR latest_quiet_seconds > 0",
            name="ck_processing_range_quiet_positive",
        ),
        CheckConstraint(
            "processing_watermark_at IS NULL OR processing_watermark_at >= start_at",
            name="ck_processing_range_watermark_floor",
        ),
        CheckConstraint(
            "fixed_end_at IS NULL OR processing_watermark_at IS NULL "
            "OR processing_watermark_at <= fixed_end_at",
            name="ck_processing_range_watermark_fixed_ceiling",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    source_channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channels.id", ondelete="RESTRICT"), nullable=False
    )
    source_profile_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channel_profile_versions.id", ondelete="RESTRICT"), nullable=False
    )
    boundary_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fixed_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_quiet_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    processing_watermark_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MessageRecord(TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "source_channel_id", "telegram_anchor_message_id", name="uq_message_source_anchor"
        ),
        CheckConstraint("telegram_anchor_message_id > 0", name="ck_message_anchor_positive"),
        CheckConstraint(
            "telegram_group_id IS NULL OR telegram_group_id > 0",
            name="ck_message_group_positive",
        ),
        CheckConstraint("media_count >= 0", name="ck_message_media_count_nonnegative"),
        Index("ix_messages_source_sent_at", "source_channel_id", "sent_at"),
        Index(
            "uq_messages_source_group",
            "source_channel_id",
            "telegram_group_id",
            unique=True,
            postgresql_where=text("telegram_group_id IS NOT NULL"),
        ),
        CheckConstraint(
            "source_edited_at IS NULL OR source_edited_at >= sent_at",
            name="ck_message_source_edited_after_sent",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    source_channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channels.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_anchor_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    media_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visual_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_changed_after_processing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class MessagePartRecord(CreatedAtMixin, Base):
    """One actual Telegram component Message belonging to a logical normalized Message."""

    __tablename__ = "message_parts"
    __table_args__ = (
        UniqueConstraint(
            "source_channel_id",
            "telegram_message_id",
            name="uq_message_part_source_telegram",
        ),
        CheckConstraint("telegram_message_id > 0", name="ck_message_part_telegram_positive"),
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


class ImageAssetRecord(TimestampMixin, Base):
    """One image asset awaiting or retaining normalized archive evidence."""

    __tablename__ = "image_assets"
    __table_args__ = (
        UniqueConstraint("message_id", "source_asset_id", name="uq_image_asset_message_source"),
        CheckConstraint(
            "archive_state IN ('pending', 'ready', 'deleted', 'failed')",
            name="ck_image_asset_archive_state",
        ),
        CheckConstraint(
            "archive_state <> 'ready' OR (storage_backend IS NOT NULL AND storage_key IS NOT NULL "
            "AND content_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL "
            "AND source_sha256 IS NOT NULL AND archive_sha256 IS NOT NULL AND "
            "perceptual_hash IS NOT NULL "
            "AND archive_size_bytes IS NOT NULL AND archive_ready_at IS NOT NULL)",
            name="ck_image_asset_ready_metadata",
        ),
        CheckConstraint(
            "archive_state <> 'deleted' OR archive_deleted_at IS NOT NULL",
            name="ck_image_asset_deleted_timestamp",
        ),
        CheckConstraint(
            "source_telegram_message_id IS NULL OR source_telegram_message_id > 0",
            name="ck_image_asset_source_message_positive",
        ),
        CheckConstraint(
            "(archive_lease_token IS NULL AND archive_lease_expires_at IS NULL) OR "
            "(archive_state = 'pending' AND archive_lease_token IS NOT NULL "
            "AND archive_lease_expires_at IS NOT NULL)",
            name="ck_image_asset_archive_lease",
        ),
        CheckConstraint(
            "archive_state <> 'failed' OR archive_failure_reason IS NOT NULL",
            name="ck_image_asset_failure_reason",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="ck_image_asset_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_image_asset_height_positive"),
        CheckConstraint(
            "archive_size_bytes IS NULL OR archive_size_bytes > 0",
            name="ck_image_asset_archive_size_positive",
        ),
        Index("ix_image_assets_archive_state", "archive_state"),
        Index(
            "ix_image_assets_pending_archive_lease",
            "archive_state",
            "archive_lease_expires_at",
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    source_asset_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_phash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    archive_lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archive_lease_expires_at: Mapped[datetime | None] = mapped_column(
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
    archive_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class RangeExecutionRecord(TimestampMixin, Base):
    __tablename__ = "range_executions"
    __table_args__ = (
        UniqueConstraint(
            "processing_range_id", "from_at", "to_at", name="uq_range_execution_bounds"
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_range_execution_status",
        ),
        CheckConstraint("from_at < to_at", name="ck_range_execution_bounds"),
        CheckConstraint(
            "watermark_at IS NULL OR (watermark_at >= from_at AND watermark_at <= to_at)",
            name="ck_range_execution_watermark_bounds",
        ),
        CheckConstraint(
            "status <> 'completed' OR (watermark_at = to_at AND completed_at IS NOT NULL)",
            name="ck_range_execution_completion",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_range_execution_lease",
        ),
        Index("ix_range_executions_status_lease", "status", "lease_expires_at"),
        Index(
            "uq_range_execution_one_active",
            "processing_range_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    processing_range_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_ranges.id", ondelete="RESTRICT"), nullable=False
    )
    source_profile_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_channel_profile_versions.id", ondelete="RESTRICT"), nullable=False
    )
    from_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    to_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DurableWakeup(TimestampMixin, Base):
    __tablename__ = "durable_wakeups"
    __table_args__ = (
        UniqueConstraint("queue", "entity_id", name="uq_durable_wakeup_queue_entity"),
        CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'cancelled')",
            name="ck_durable_wakeup_status",
        ),
        CheckConstraint("dispatch_attempts >= 0", name="ck_durable_wakeup_attempts"),
        CheckConstraint(
            "(status = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_durable_wakeup_lease",
        ),
        Index("ix_durable_wakeups_due", "status", "next_attempt_at"),
    )

    id: Mapped[UUIDPrimaryKey]
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(CreatedAtMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_entity", "entity_type", "entity_id"),)

    id: Mapped[UUIDPrimaryKey]
    actor_admin_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
