"""Add M8 admin HTTP sessions and versioned media processing profiles.

Revision ID: a8b2d4e1f305
Revises: f1a7c3d9e204
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8b2d4e1f305"
down_revision: str | Sequence[str] | None = "f1a7c3d9e204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("session_token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip_address", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(session_token_hash) = 64 AND session_token_hash !~ '[^0-9a-f]'",
            name=op.f("ck_admin_sessions_admin_session_token_hash_format"),
        ),
        sa.CheckConstraint(
            "char_length(csrf_token_hash) = 64 AND csrf_token_hash !~ '[^0-9a-f]'",
            name=op.f("ck_admin_sessions_admin_session_csrf_hash_format"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_admin_sessions_admin_session_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_admin_sessions_admin_session_revoked_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["admin_users.id"],
            name="fk_admin_sessions_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_sessions"),
        sa.UniqueConstraint("session_token_hash", name="uq_admin_sessions_token_hash"),
    )
    op.create_index(
        "ix_admin_sessions_user_expiry",
        "admin_sessions",
        ["admin_user_id", "expires_at"],
    )

    op.create_table(
        "media_processing_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0",
            name=op.f("ck_media_processing_profiles_media_profile_name_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_media_processing_profiles"),
        sa.UniqueConstraint("name", name=op.f("uq_media_processing_profiles_name")),
    )

    op.create_table(
        "media_processing_profile_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("media_processing_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name=op.f("ck_media_processing_profile_versions_media_version_positive"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_media_processing_profile_versions_media_version_state"),
        ),
        sa.CheckConstraint(
            "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) "
            "OR (state = 'published' AND published_at IS NOT NULL AND archived_at IS NULL) "
            "OR (state = 'archived' AND published_at IS NOT NULL "
            "AND archived_at IS NOT NULL AND archived_at >= published_at)",
            name=op.f("ck_media_processing_profile_versions_media_version_lifecycle"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name=op.f("ck_media_processing_profile_versions_media_content_object"),
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64 AND content_hash !~ '[^0-9a-f]'",
            name=op.f("ck_media_processing_profile_versions_media_content_hash_format"),
        ),
        sa.ForeignKeyConstraint(
            ["media_processing_profile_id"],
            ["media_processing_profiles.id"],
            name="fk_media_profile_versions_profile",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_media_processing_profile_versions"),
        sa.UniqueConstraint(
            "media_processing_profile_id",
            "version_number",
            name="uq_media_profile_version_number",
        ),
    )
    op.create_index(
        "uq_media_profile_one_published",
        "media_processing_profile_versions",
        ["media_processing_profile_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )

    delete_error = "published or archived media profile versions cannot be deleted"
    content_error = "published media profile version content is immutable"
    transition_error = "published media profile versions can only be archived"
    op.execute(
        f"""
        CREATE FUNCTION tgcurator_enforce_media_profile_version_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            IF TG_OP = ''DELETE'' THEN
                IF OLD.state IN (''published'', ''archived'') THEN
                    RAISE EXCEPTION ''{delete_error}'';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.state = ''archived'' THEN
                RAISE EXCEPTION ''archived media profile versions are immutable'';
            END IF;

            IF OLD.state = ''draft'' AND NEW.state NOT IN (''draft'', ''published'') THEN
                RAISE EXCEPTION ''draft media profile versions can only be published'';
            END IF;

            IF OLD.state = ''published'' THEN
                IF NEW.state = ''published'' THEN
                    IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
                        RAISE EXCEPTION ''{content_error}'';
                    END IF;
                ELSIF NEW.state = ''archived'' THEN
                    IF (to_jsonb(NEW) - ''state'' - ''archived_at'' - ''updated_at'')
                        IS DISTINCT FROM
                       (to_jsonb(OLD) - ''state'' - ''archived_at'' - ''updated_at'')
                    THEN
                        RAISE EXCEPTION ''{content_error}'';
                    END IF;
                ELSE
                    RAISE EXCEPTION ''{transition_error}'';
                END IF;
            END IF;

            RETURN NEW;
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_media_profile_version_immutable "
        "BEFORE UPDATE OR DELETE ON media_processing_profile_versions "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_enforce_media_profile_version_immutability()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_media_profile_version_immutable ON media_processing_profile_versions"
    )
    op.execute("DROP FUNCTION tgcurator_enforce_media_profile_version_immutability()")
    op.drop_index("uq_media_profile_one_published", table_name="media_processing_profile_versions")
    op.drop_table("media_processing_profile_versions")
    op.drop_table("media_processing_profiles")
    op.drop_index("ix_admin_sessions_user_expiry", table_name="admin_sessions")
    op.drop_table("admin_sessions")
