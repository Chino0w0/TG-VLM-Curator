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


class MessageRecord(TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "source_channel_id",
            "primary_telegram_message_id",
            name="uq_message_source_primary",
        ),
        CheckConstraint(
            "cardinality(telegram_message_ids) > 0",
            name="message_parts_not_empty",
        ),
        CheckConstraint(
            "primary_telegram_message_id = ANY(telegram_message_ids)",
            name="message_primary_in_parts",
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
        Index("ix_messages_source_published_at", "source_channel_id", "published_at"),
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
