"""Add M5 human review and deterministic routing persistence.

Revision ID: d7b3f9a1e6c2
Revises: c4a9e7d2f5b1
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7b3f9a1e6c2"
down_revision: str | Sequence[str] | None = "c4a9e7d2f5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "review_status",
            sa.String(length=24),
            server_default=sa.text("'unreviewed'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_messages_message_review_status"),
        "messages",
        "review_status IN ('unreviewed', 'in_review', 'reviewed', 'needs_attention')",
    )

    op.create_table(
        "manual_label_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("image_asset_id", sa.Uuid(), nullable=True),
        sa.Column("video_asset_id", sa.Uuid(), nullable=True),
        sa.Column("label_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("label_key", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("activated", sa.Boolean(), nullable=True),
        sa.Column("actor_admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(label_key)) > 0",
            name=op.f("ck_manual_label_assignments_manual_label_key_not_blank"),
        ),
        sa.CheckConstraint(
            "operation IN ('set', 'clear')",
            name=op.f("ck_manual_label_assignments_manual_label_operation"),
        ),
        sa.CheckConstraint(
            "(operation = 'set' AND score IS NOT NULL AND activated IS NOT NULL) OR "
            "(operation = 'clear' AND score IS NULL AND activated IS NULL)",
            name=op.f("ck_manual_label_assignments_manual_label_payload"),
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name=op.f("ck_manual_label_assignments_manual_label_score"),
        ),
        sa.CheckConstraint(
            "(target_kind = 'message' AND target_scope = 'global' "
            "AND target_id = message_id AND image_asset_id IS NULL "
            "AND video_asset_id IS NULL) OR "
            "(target_kind = 'image_asset' AND target_scope = 'media' "
            "AND target_id = image_asset_id AND image_asset_id IS NOT NULL "
            "AND video_asset_id IS NULL) OR "
            "(target_kind = 'video_asset' AND target_scope = 'media' "
            "AND target_id = video_asset_id AND video_asset_id IS NOT NULL "
            "AND image_asset_id IS NULL)",
            name=op.f("ck_manual_label_assignments_manual_label_target_identity"),
        ),
        sa.CheckConstraint(
            "target_kind IN ('message', 'image_asset', 'video_asset')",
            name=op.f("ck_manual_label_assignments_manual_label_target_kind"),
        ),
        sa.CheckConstraint(
            "target_scope IN ('global', 'media')",
            name=op.f("ck_manual_label_assignments_manual_label_target_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_admin_user_id"],
            ["admin_users.id"],
            name=op.f("fk_manual_label_assignments_actor_admin_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["image_asset_id"],
            ["image_assets.id"],
            name=op.f("fk_manual_label_assignments_image_asset_id_image_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["label_definition_version_id"],
            ["label_definition_versions.id"],
            name="fk_manual_labels_definition_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_manual_label_assignments_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_asset_id"],
            ["video_assets.id"],
            name=op.f("fk_manual_label_assignments_video_asset_id_video_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_label_assignments")),
    )
    op.create_index(
        "ix_manual_labels_message_created",
        "manual_label_assignments",
        ["message_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_manual_labels_target_label",
        "manual_label_assignments",
        ["target_id", "label_definition_version_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "message_review_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("old_status", sa.String(length=24), nullable=False),
        sa.Column("new_status", sa.String(length=24), nullable=False),
        sa.Column("actor_admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "new_status IN ('unreviewed', 'in_review', 'reviewed', 'needs_attention')",
            name=op.f("ck_message_review_events_review_event_new_status"),
        ),
        sa.CheckConstraint(
            "old_status IN ('unreviewed', 'in_review', 'reviewed', 'needs_attention')",
            name=op.f("ck_message_review_events_review_event_old_status"),
        ),
        sa.CheckConstraint(
            "old_status <> new_status",
            name=op.f("ck_message_review_events_review_event_status_changed"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_admin_user_id"],
            ["admin_users.id"],
            name=op.f("fk_message_review_events_actor_admin_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_review_events_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_review_events")),
    )
    op.create_index(
        "ix_review_events_message_created",
        "message_review_events",
        ["message_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "routing_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0",
            name=op.f("ck_routing_policies_routing_policy_name_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_policies")),
        sa.UniqueConstraint("name", name=op.f("uq_routing_policies_name")),
    )

    op.create_table(
        "routing_policy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_policy_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "description",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name=op.f("ck_routing_policy_versions_routing_version_positive"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_routing_policy_versions_routing_version_state"),
        ),
        sa.CheckConstraint(
            "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
            "(state = 'published' AND published_at IS NOT NULL AND archived_at IS NULL) OR "
            "(state = 'archived' AND published_at IS NOT NULL "
            "AND archived_at IS NOT NULL AND archived_at >= published_at)",
            name=op.f("ck_routing_policy_versions_routing_version_lifecycle"),
        ),
        sa.ForeignKeyConstraint(
            ["routing_policy_id"],
            ["routing_policies.id"],
            name=op.f("fk_routing_policy_versions_routing_policy_id_routing_policies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_policy_versions")),
        sa.UniqueConstraint(
            "routing_policy_id",
            "version_number",
            name="uq_routing_policy_version_number",
        ),
    )
    op.create_index(
        "uq_routing_policy_one_published",
        "routing_policy_versions",
        ["routing_policy_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )

    op.create_table(
        "routing_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_policy_version_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("condition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "stop_on_match",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(condition) = 'object'",
            name=op.f("ck_routing_rules_routing_rule_condition_object"),
        ),
        sa.ForeignKeyConstraint(
            ["routing_policy_version_id"],
            ["routing_policy_versions.id"],
            name="fk_routing_rules_policy_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_rules")),
    )
    op.create_index(
        "ix_routing_rules_policy_order",
        "routing_rules",
        ["routing_policy_version_id", "priority", "id"],
        unique=False,
    )

    op.create_table(
        "rendering_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0",
            name=op.f("ck_rendering_templates_rendering_template_name_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rendering_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_rendering_templates_name")),
    )

    op.create_table(
        "rendering_template_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rendering_template_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name=op.f("ck_rendering_template_versions_rendering_version_positive"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_rendering_template_versions_rendering_version_state"),
        ),
        sa.CheckConstraint(
            "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
            "(state = 'published' AND published_at IS NOT NULL AND archived_at IS NULL) OR "
            "(state = 'archived' AND published_at IS NOT NULL "
            "AND archived_at IS NOT NULL AND archived_at >= published_at)",
            name=op.f("ck_rendering_template_versions_rendering_version_lifecycle"),
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64 AND content_hash !~ '[^0-9a-f]'",
            name=op.f("ck_rendering_template_versions_rendering_content_hash_format"),
        ),
        sa.ForeignKeyConstraint(
            ["rendering_template_id"],
            ["rendering_templates.id"],
            name="fk_rendering_versions_template",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rendering_template_versions")),
        sa.UniqueConstraint(
            "rendering_template_id",
            "content_hash",
            name="uq_rendering_template_content_hash",
        ),
        sa.UniqueConstraint(
            "rendering_template_id",
            "version_number",
            name="uq_rendering_template_version_number",
        ),
    )
    op.create_index(
        "uq_rendering_template_one_published",
        "rendering_template_versions",
        ["rendering_template_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )

    op.create_table(
        "routing_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_rule_id", sa.Uuid(), nullable=False),
        sa.Column("destination_channel_id", sa.Uuid(), nullable=False),
        sa.Column("publication_mode", sa.String(length=48), nullable=False),
        sa.Column("rendering_template_version_id", sa.Uuid(), nullable=True),
        sa.Column("publish_identity_id", sa.Uuid(), nullable=True),
        sa.Column("output_order", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "publication_mode IN ('native_forward_with_supplement', 'copy_with_caption', "
            "'forward_only', 'metadata_only')",
            name=op.f("ck_routing_actions_routing_action_publication_mode"),
        ),
        sa.CheckConstraint(
            "(publication_mode = 'forward_only' AND rendering_template_version_id IS NULL) OR "
            "(publication_mode <> 'forward_only' AND rendering_template_version_id IS NOT NULL)",
            name=op.f("ck_routing_actions_routing_action_rendering_template"),
        ),
        sa.CheckConstraint(
            "output_order >= 0",
            name=op.f("ck_routing_actions_routing_action_output_order_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_channel_id"],
            ["destination_channels.id"],
            name=op.f("fk_routing_actions_destination_channel_id_destination_channels"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publish_identity_id"],
            ["telegram_identities.id"],
            name=op.f("fk_routing_actions_publish_identity_id_telegram_identities"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rendering_template_version_id"],
            ["rendering_template_versions.id"],
            name="fk_routing_actions_rendering_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_rule_id"],
            ["routing_rules.id"],
            name=op.f("fk_routing_actions_routing_rule_id_routing_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_actions")),
        sa.UniqueConstraint(
            "routing_rule_id",
            "output_order",
            name="uq_routing_action_output_order",
        ),
    )
    op.create_index(
        "ix_routing_actions_rule",
        "routing_actions",
        ["routing_rule_id", "output_order"],
        unique=False,
    )

    op.create_table(
        "routing_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_request_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("routing_policy_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "facts_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "rule_outcomes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("stopped_at_rule_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(routing_request_id)) > 0",
            name=op.f("ck_routing_evaluations_routing_evaluation_request_not_blank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(facts_snapshot) = 'object'",
            name=op.f("ck_routing_evaluations_routing_evaluation_facts_object"),
        ),
        sa.CheckConstraint(
            "char_length(facts_hash) = 64 AND facts_hash !~ '[^0-9a-f]'",
            name=op.f("ck_routing_evaluations_routing_evaluation_hash_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rule_outcomes) = 'array'",
            name=op.f("ck_routing_evaluations_routing_evaluation_outcomes_array"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_routing_evaluations_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_policy_version_id"],
            ["routing_policy_versions.id"],
            name="fk_routing_evaluations_policy_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stopped_at_rule_id"],
            ["routing_rules.id"],
            name=op.f("fk_routing_evaluations_stopped_at_rule_id_routing_rules"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_evaluations")),
        sa.UniqueConstraint(
            "routing_request_id",
            name=op.f("uq_routing_evaluations_routing_request_id"),
        ),
    )
    op.create_index(
        "ix_routing_evaluations_message",
        "routing_evaluations",
        ["message_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "publication_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("routing_request_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("routing_policy_version_id", sa.Uuid(), nullable=False),
        sa.Column("routing_rule_id", sa.Uuid(), nullable=False),
        sa.Column("routing_action_id", sa.Uuid(), nullable=False),
        sa.Column("destination_channel_id", sa.Uuid(), nullable=False),
        sa.Column("publish_identity_id", sa.Uuid(), nullable=False),
        sa.Column("publication_mode", sa.String(length=48), nullable=False),
        sa.Column("rendering_template_version_id", sa.Uuid(), nullable=True),
        sa.Column("business_idempotency_key", sa.String(length=96), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'partial', 'retry_wait', "
            "'failed', 'cancelled')",
            name=op.f("ck_publication_intents_publication_intent_status"),
        ),
        sa.CheckConstraint(
            "publication_mode IN ('native_forward_with_supplement', 'copy_with_caption', "
            "'forward_only', 'metadata_only')",
            name=op.f("ck_publication_intents_publication_intent_mode"),
        ),
        sa.CheckConstraint(
            "(publication_mode = 'forward_only' AND rendering_template_version_id IS NULL) OR "
            "(publication_mode <> 'forward_only' AND rendering_template_version_id IS NOT NULL)",
            name=op.f("ck_publication_intents_publication_intent_rendering_template"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(routing_request_id)) > 0",
            name=op.f("ck_publication_intents_publication_intent_request_not_blank"),
        ),
        sa.CheckConstraint(
            "char_length(business_idempotency_key) > 0",
            name=op.f("ck_publication_intents_publication_intent_key_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_channel_id"],
            ["destination_channels.id"],
            name="fk_publication_intents_destination",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_publication_intents_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publish_identity_id"],
            ["telegram_identities.id"],
            name=op.f("fk_publication_intents_publish_identity_id_telegram_identities"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rendering_template_version_id"],
            ["rendering_template_versions.id"],
            name="fk_publication_intents_rendering_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_action_id"],
            ["routing_actions.id"],
            name=op.f("fk_publication_intents_routing_action_id_routing_actions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_evaluation_id"],
            ["routing_evaluations.id"],
            name="fk_publication_intents_evaluation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_policy_version_id"],
            ["routing_policy_versions.id"],
            name="fk_publication_intents_policy_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_rule_id"],
            ["routing_rules.id"],
            name=op.f("fk_publication_intents_routing_rule_id_routing_rules"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_intents")),
        sa.UniqueConstraint(
            "business_idempotency_key",
            name=op.f("uq_publication_intents_business_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "routing_evaluation_id",
            "routing_action_id",
            name="uq_publication_intent_evaluation_action",
        ),
    )
    op.create_index(
        "ix_publication_intents_message",
        "publication_intents",
        ["message_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_publication_intents_pending",
        "publication_intents",
        ["status", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_reject_manual_label_assignment_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            RAISE EXCEPTION ''manual label assignments are append-only'';
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_manual_label_assignments_append_only "
        "BEFORE UPDATE OR DELETE ON manual_label_assignments "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_reject_manual_label_assignment_mutation()"
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_reject_message_review_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            RAISE EXCEPTION ''message review events are append-only'';
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_message_review_events_append_only "
        "BEFORE UPDATE OR DELETE ON message_review_events "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_reject_message_review_event_mutation()"
    )

    routing_versions_delete_error = (
        "published or archived routing configuration versions cannot be deleted"
    )
    routing_version_content_error = "published routing configuration version content is immutable"
    routing_version_transition_error = (
        "published routing configuration versions can only be archived"
    )
    routing_rule_draft_error = "routing rules may mutate only for draft routing policy versions"
    routing_action_draft_error = "routing actions may mutate only for draft routing policy versions"

    op.execute(
        f"""
        CREATE FUNCTION tgcurator_enforce_routing_version_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            IF TG_OP = ''DELETE'' THEN
                IF OLD.state IN (''published'', ''archived'') THEN
                    RAISE EXCEPTION ''{routing_versions_delete_error}'';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.state = ''archived'' THEN
                RAISE EXCEPTION ''archived routing configuration versions are immutable'';
            END IF;

            IF OLD.state = ''draft'' AND NEW.state NOT IN (''draft'', ''published'') THEN
                RAISE EXCEPTION ''draft routing configuration versions can only be published'';
            END IF;

            IF OLD.state = ''published'' THEN
                IF NEW.state = ''published'' THEN
                    IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
                        RAISE EXCEPTION ''{routing_version_content_error}'';
                    END IF;
                ELSIF NEW.state = ''archived'' THEN
                    IF (to_jsonb(NEW) - ''state'' - ''archived_at'' - ''updated_at'')
                        IS DISTINCT FROM
                       (to_jsonb(OLD) - ''state'' - ''archived_at'' - ''updated_at'')
                    THEN
                        RAISE EXCEPTION ''{routing_version_content_error}'';
                    END IF;
                ELSE
                    RAISE EXCEPTION ''{routing_version_transition_error}'';
                END IF;
            END IF;

            RETURN NEW;
        END;'
        """
    )
    version_triggers = (
        ("routing_policy_versions", "trg_routing_policy_version_immutable"),
        ("rendering_template_versions", "trg_rendering_template_version_immutable"),
    )
    for table_name, trigger_name in version_triggers:
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "tgcurator_enforce_routing_version_immutability()"
        )

    op.execute(
        f"""
        CREATE FUNCTION tgcurator_require_draft_routing_policy_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'DECLARE
            owner_state text;
        BEGIN
            IF TG_OP IN (''UPDATE'', ''DELETE'') THEN
                SELECT state INTO owner_state
                FROM routing_policy_versions
                WHERE id = OLD.routing_policy_version_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''{routing_rule_draft_error}'';
                END IF;
            END IF;
            IF TG_OP IN (''INSERT'', ''UPDATE'') THEN
                SELECT state INTO owner_state
                FROM routing_policy_versions
                WHERE id = NEW.routing_policy_version_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''{routing_rule_draft_error}'';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = ''DELETE'' THEN OLD ELSE NEW END;
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_routing_rules_draft_only "
        "BEFORE INSERT OR UPDATE OR DELETE ON routing_rules "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_require_draft_routing_policy_version()"
    )

    op.execute(
        f"""
        CREATE FUNCTION tgcurator_require_draft_routing_action_owner()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'DECLARE
            owner_state text;
        BEGIN
            IF TG_OP IN (''UPDATE'', ''DELETE'') THEN
                SELECT version.state INTO owner_state
                FROM routing_rules AS rule
                JOIN routing_policy_versions AS version
                  ON version.id = rule.routing_policy_version_id
                WHERE rule.id = OLD.routing_rule_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''{routing_action_draft_error}'';
                END IF;
            END IF;
            IF TG_OP IN (''INSERT'', ''UPDATE'') THEN
                SELECT version.state INTO owner_state
                FROM routing_rules AS rule
                JOIN routing_policy_versions AS version
                  ON version.id = rule.routing_policy_version_id
                WHERE rule.id = NEW.routing_rule_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''{routing_action_draft_error}'';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = ''DELETE'' THEN OLD ELSE NEW END;
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_routing_actions_draft_only "
        "BEFORE INSERT OR UPDATE OR DELETE ON routing_actions "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_require_draft_routing_action_owner()"
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_reject_routing_evaluation_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            RAISE EXCEPTION ''routing evaluations are immutable'';
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_routing_evaluations_immutable "
        "BEFORE UPDATE OR DELETE ON routing_evaluations "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_reject_routing_evaluation_mutation()"
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_enforce_publication_intent_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            IF TG_OP = ''DELETE'' THEN
                RAISE EXCEPTION ''publication intents cannot be deleted'';
            END IF;
            IF (to_jsonb(NEW) - ''status'' - ''updated_at'')
                IS DISTINCT FROM
               (to_jsonb(OLD) - ''status'' - ''updated_at'')
            THEN
                RAISE EXCEPTION ''publication intent identity is immutable'';
            END IF;
            RETURN NEW;
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_publication_intents_identity_immutable "
        "BEFORE UPDATE OR DELETE ON publication_intents "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_enforce_publication_intent_identity()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_publication_intents_identity_immutable ON publication_intents")
    op.execute("DROP TRIGGER trg_routing_evaluations_immutable ON routing_evaluations")
    op.execute("DROP TRIGGER trg_routing_actions_draft_only ON routing_actions")
    op.execute("DROP TRIGGER trg_routing_rules_draft_only ON routing_rules")
    op.execute(
        "DROP TRIGGER trg_rendering_template_version_immutable ON rendering_template_versions"
    )
    op.execute("DROP TRIGGER trg_routing_policy_version_immutable ON routing_policy_versions")
    op.execute("DROP TRIGGER trg_message_review_events_append_only ON message_review_events")
    op.execute("DROP TRIGGER trg_manual_label_assignments_append_only ON manual_label_assignments")

    op.execute("DROP FUNCTION tgcurator_enforce_publication_intent_identity()")
    op.execute("DROP FUNCTION tgcurator_reject_routing_evaluation_mutation()")
    op.execute("DROP FUNCTION tgcurator_require_draft_routing_action_owner()")
    op.execute("DROP FUNCTION tgcurator_require_draft_routing_policy_version()")
    op.execute("DROP FUNCTION tgcurator_enforce_routing_version_immutability()")
    op.execute("DROP FUNCTION tgcurator_reject_message_review_event_mutation()")
    op.execute("DROP FUNCTION tgcurator_reject_manual_label_assignment_mutation()")

    op.drop_index("ix_publication_intents_pending", table_name="publication_intents")
    op.drop_index("ix_publication_intents_message", table_name="publication_intents")
    op.drop_table("publication_intents")

    op.drop_index("ix_routing_evaluations_message", table_name="routing_evaluations")
    op.drop_table("routing_evaluations")

    op.drop_index("ix_routing_actions_rule", table_name="routing_actions")
    op.drop_table("routing_actions")
    op.drop_index("ix_routing_rules_policy_order", table_name="routing_rules")
    op.drop_table("routing_rules")

    op.drop_index(
        "uq_rendering_template_one_published",
        table_name="rendering_template_versions",
    )
    op.drop_table("rendering_template_versions")
    op.drop_index(
        "uq_routing_policy_one_published",
        table_name="routing_policy_versions",
    )
    op.drop_table("routing_policy_versions")
    op.drop_table("rendering_templates")
    op.drop_table("routing_policies")

    op.drop_index(
        "ix_review_events_message_created",
        table_name="message_review_events",
    )
    op.drop_table("message_review_events")
    op.drop_index(
        "ix_manual_labels_target_label",
        table_name="manual_label_assignments",
    )
    op.drop_index(
        "ix_manual_labels_message_created",
        table_name="manual_label_assignments",
    )
    op.drop_table("manual_label_assignments")

    op.drop_constraint(
        op.f("ck_messages_message_review_status"),
        "messages",
        type_="check",
    )
    op.drop_column("messages", "review_status")
