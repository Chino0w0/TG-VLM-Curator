"""Create the M1 API, security, and PostgreSQL foundation.

Revision ID: 94c2d3062de4
Revises:
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "94c2d3062de4"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps(*, include_updated_at: bool) -> list[sa.Column]:
    columns: list[sa.Column] = []
    if include_updated_at:
        columns.append(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            )
        )
    columns.append(
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
    )
    return columns


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(include_updated_at=True),
        sa.CheckConstraint(
            "char_length(btrim(username)) > 0",
            name="ck_admin_users_admin_username_not_blank",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_users"),
        sa.UniqueConstraint("username", name="uq_admin_users_username"),
    )
    op.create_index(
        "uq_admin_users_single_active",
        "admin_users",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
    )

    op.create_table(
        "encrypted_secrets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("secret_type", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=128), nullable=False),
        *_timestamps(include_updated_at=False),
        sa.CheckConstraint(
            "char_length(btrim(secret_type)) > 0",
            name="ck_encrypted_secrets_encrypted_secret_type_not_blank",
        ),
        sa.CheckConstraint(
            "char_length(btrim(key_id)) > 0",
            name="ck_encrypted_secrets_encrypted_secret_key_id_not_blank",
        ),
        sa.CheckConstraint(
            "octet_length(nonce) = 12",
            name="ck_encrypted_secrets_encrypted_secret_nonce_size",
        ),
        sa.CheckConstraint(
            "octet_length(ciphertext) >= 16",
            name="ck_encrypted_secrets_encrypted_secret_ciphertext_tag",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_encrypted_secrets"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=128), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(include_updated_at=False),
        sa.CheckConstraint(
            "char_length(btrim(event_type)) > 0",
            name="ck_audit_events_audit_event_type_not_blank",
        ),
        sa.CheckConstraint(
            "char_length(btrim(entity_type)) > 0",
            name="ck_audit_events_audit_entity_type_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["actor_admin_user_id"],
            ["admin_users.id"],
            name="fk_audit_events_actor_admin_user_id_admin_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_id"],
    )

    op.create_table(
        "telegram_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("identity_type", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "health_status",
            sa.String(length=32),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("secret_id", sa.Uuid(), nullable=False),
        *_timestamps(include_updated_at=True),
        sa.CheckConstraint(
            "identity_type IN ('mtproto_user', 'bot')",
            name="ck_telegram_identities_telegram_identity_type",
        ),
        sa.CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'failed')",
            name="ck_telegram_identities_telegram_identity_health",
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0",
            name="ck_telegram_identities_telegram_identity_name_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["secret_id"],
            ["encrypted_secrets.id"],
            name="fk_telegram_identities_secret_id_encrypted_secrets",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_identities"),
        sa.UniqueConstraint("name", name="uq_telegram_identities_name"),
    )

    op.create_table(
        "source_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("telegram_peer_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("default_read_identity_id", sa.Uuid(), nullable=False),
        sa.Column("active_profile_version_id", sa.Uuid(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_seen_message_id", sa.BigInteger(), nullable=True),
        *_timestamps(include_updated_at=True),
        sa.CheckConstraint(
            "telegram_peer_id <> 0",
            name="ck_source_channels_source_channel_peer_nonzero",
        ),
        sa.CheckConstraint(
            "latest_seen_message_id IS NULL OR latest_seen_message_id > 0",
            name="ck_source_channels_source_channel_latest_message_positive",
        ),
        sa.ForeignKeyConstraint(
            ["default_read_identity_id"],
            ["telegram_identities.id"],
            name="fk_source_channels_default_read_identity_id_telegram_identities",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_channels"),
        sa.UniqueConstraint("telegram_peer_id", name="uq_source_channel_peer"),
    )

    op.create_table(
        "destination_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("telegram_peer_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("default_publish_identity_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(include_updated_at=True),
        sa.CheckConstraint(
            "telegram_peer_id <> 0",
            name="ck_destination_channels_destination_channel_peer_nonzero",
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0",
            name="ck_destination_channels_destination_channel_name_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["default_publish_identity_id"],
            ["telegram_identities.id"],
            name="fk_destination_publish_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_destination_channels"),
        sa.UniqueConstraint("telegram_peer_id", name="uq_destination_channel_peer"),
    )

    op.create_table(
        "source_channel_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        *_timestamps(include_updated_at=True),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0",
            name="ck_source_channel_profiles_profile_name_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["source_channel_id"],
            ["source_channels.id"],
            name="fk_source_channel_profiles_source_channel_id_source_channels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_channel_profiles"),
        sa.UniqueConstraint(
            "source_channel_id",
            name="uq_source_channel_profiles_source_channel_id",
        ),
    )

    op.create_table(
        "source_channel_profile_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(include_updated_at=False),
        sa.CheckConstraint(
            "version_number > 0",
            name="ck_source_channel_profile_versions_version_positive",
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'retired')",
            name="ck_source_channel_profile_versions_state_valid",
        ),
        sa.CheckConstraint(
            "(state = 'draft' AND published_at IS NULL) "
            "OR (state IN ('published', 'retired') AND published_at IS NOT NULL)",
            name="ck_source_channel_profile_versions_published_at_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_source_channel_profile_versions_content_object",
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64 AND content_hash !~ '[^0-9a-f]'",
            name="ck_source_channel_profile_versions_hash_format",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["source_channel_profiles.id"],
            name="fk_profile_versions_profile",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_channel_profile_versions"),
        sa.UniqueConstraint(
            "profile_id",
            "content_hash",
            name="uq_source_profile_content_hash",
        ),
        sa.UniqueConstraint(
            "profile_id",
            "version_number",
            name="uq_source_profile_version_number",
        ),
    )
    op.create_index(
        "uq_source_profile_one_published",
        "source_channel_profile_versions",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_foreign_key(
        "fk_source_channels_active_profile_version",
        "source_channels",
        "source_channel_profile_versions",
        ["active_profile_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_enforce_source_profile_version_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            IF TG_OP = ''DELETE'' THEN
                IF OLD.state IN (''published'', ''retired'') THEN
                    RAISE EXCEPTION ''published or retired profile versions cannot be deleted'';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.state = ''retired'' THEN
                RAISE EXCEPTION ''retired profile versions are immutable'';
            END IF;

            IF OLD.state = ''draft'' AND NEW.state NOT IN (''draft'', ''published'') THEN
                RAISE EXCEPTION ''draft profile versions can only be published'';
            END IF;

            IF OLD.state = ''published'' AND (
                NEW.state NOT IN (''published'', ''retired'')
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
                OR NEW.version_number IS DISTINCT FROM OLD.version_number
                OR NEW.content IS DISTINCT FROM OLD.content
                OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                OR NEW.published_at IS DISTINCT FROM OLD.published_at
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            ) THEN
                RAISE EXCEPTION ''published profile version content is immutable'';
            END IF;

            RETURN NEW;
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_source_profile_version_immutable "
        "BEFORE UPDATE OR DELETE ON source_channel_profile_versions "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_enforce_source_profile_version_immutability()"
    )

    op.create_table(
        "processing_ranges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_mode", sa.String(length=16), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_start_message_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_fixed_end_message_id", sa.BigInteger(), nullable=True),
        sa.Column("processing_watermark_message_id", sa.BigInteger(), nullable=True),
        sa.Column("active_high_watermark_message_id", sa.BigInteger(), nullable=True),
        sa.Column("steady_after_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(include_updated_at=True),
        sa.CheckConstraint(
            "end_mode IN ('fixed', 'latest')",
            name="ck_processing_ranges_processing_range_end_mode",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'paused', 'completed', 'failed')",
            name="ck_processing_ranges_processing_range_status",
        ),
        sa.CheckConstraint(
            "(end_mode = 'fixed' AND end_at IS NOT NULL AND steady_after_seconds IS NULL) "
            "OR (end_mode = 'latest' AND end_at IS NULL AND steady_after_seconds > 0)",
            name="ck_processing_ranges_processing_range_boundary_fields",
        ),
        sa.CheckConstraint(
            "end_at IS NULL OR end_at > start_at",
            name="ck_processing_ranges_processing_range_fixed_order",
        ),
        sa.CheckConstraint(
            "resolved_start_message_id IS NULL OR resolved_start_message_id > 0",
            name="ck_processing_ranges_processing_range_start_message_positive",
        ),
        sa.CheckConstraint(
            "resolved_fixed_end_message_id IS NULL OR resolved_fixed_end_message_id > 0",
            name="ck_processing_ranges_processing_range_end_message_positive",
        ),
        sa.CheckConstraint(
            "processing_watermark_message_id IS NULL OR processing_watermark_message_id >= 0",
            name="ck_processing_ranges_processing_range_watermark_nonnegative",
        ),
        sa.CheckConstraint(
            "active_high_watermark_message_id IS NULL OR active_high_watermark_message_id > 0",
            name="ck_processing_ranges_processing_range_active_high_positive",
        ),
        sa.CheckConstraint(
            "processing_watermark_message_id IS NULL "
            "OR active_high_watermark_message_id IS NULL "
            "OR processing_watermark_message_id <= active_high_watermark_message_id",
            name="ck_processing_ranges_processing_range_watermark_high_ceiling",
        ),
        sa.ForeignKeyConstraint(
            ["source_channel_id"],
            ["source_channels.id"],
            name="fk_processing_ranges_source_channel_id_source_channels",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processing_ranges"),
    )
    op.create_index(
        "ix_processing_ranges_source_status",
        "processing_ranges",
        ["source_channel_id", "status"],
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_message_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("telegram_grouped_id", sa.BigInteger(), nullable=True),
        sa.Column("primary_telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column(
            "telegram_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "processing_status",
            sa.String(length=32),
            server_default=sa.text("'ingested'"),
            nullable=False,
        ),
        sa.Column("media_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("visual_fingerprint", sa.String(length=64), nullable=True),
        *_timestamps(include_updated_at=True),
        sa.CheckConstraint(
            "cardinality(telegram_message_ids) > 0",
            name="ck_messages_message_parts_not_empty",
        ),
        sa.CheckConstraint(
            "primary_telegram_message_id = ANY(telegram_message_ids)",
            name="ck_messages_message_primary_in_parts",
        ),
        sa.CheckConstraint(
            "media_count >= 0",
            name="ck_messages_message_media_count_nonnegative",
        ),
        sa.CheckConstraint(
            "processing_status IN ('ingested', 'processing', 'processed', 'failed')",
            name="ck_messages_message_processing_status",
        ),
        sa.CheckConstraint(
            "visual_fingerprint IS NULL "
            "OR (char_length(visual_fingerprint) = 64 "
            "AND visual_fingerprint !~ '[^0-9a-f]')",
            name="ck_messages_message_visual_fingerprint_format",
        ),
        sa.ForeignKeyConstraint(
            ["source_channel_id"],
            ["source_channels.id"],
            name="fk_messages_source_channel_id_source_channels",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint(
            "source_channel_id",
            "primary_telegram_message_id",
            name="uq_message_source_primary",
        ),
    )
    op.create_index(
        "ix_messages_source_published_at",
        "messages",
        ["source_channel_id", "published_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_source_published_at", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_processing_ranges_source_status", table_name="processing_ranges")
    op.drop_table("processing_ranges")
    op.execute(
        "DROP TRIGGER trg_source_profile_version_immutable ON source_channel_profile_versions"
    )
    op.execute("DROP FUNCTION tgcurator_enforce_source_profile_version_immutability()")
    op.drop_constraint(
        "fk_source_channels_active_profile_version",
        "source_channels",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_source_profile_one_published",
        table_name="source_channel_profile_versions",
    )
    op.drop_table("source_channel_profile_versions")
    op.drop_table("source_channel_profiles")
    op.drop_table("destination_channels")
    op.drop_table("source_channels")
    op.drop_table("telegram_identities")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("encrypted_secrets")
    op.drop_index("uq_admin_users_single_active", table_name="admin_users")
    op.drop_table("admin_users")
