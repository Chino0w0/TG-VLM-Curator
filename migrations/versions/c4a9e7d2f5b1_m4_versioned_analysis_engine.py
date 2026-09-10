"""Add M4 versioned analysis engine persistence.

Revision ID: c4a9e7d2f5b1
Revises: b8e6c4f2a137
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4a9e7d2f5b1"
down_revision: str | Sequence[str] | None = "b8e6c4f2a137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_pipelines",
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
            "char_length(btrim(name)) > 0", name=op.f("ck_analysis_pipelines_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_pipelines")),
        sa.UniqueConstraint("name", name=op.f("uq_analysis_pipelines_name")),
    )
    op.create_table(
        "analysis_stage_templates",
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
            name=op.f("ck_analysis_stage_templates_template_name_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_stage_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_analysis_stage_templates_name")),
    )
    op.create_table(
        "inference_profiles",
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
            "char_length(btrim(name)) > 0", name=op.f("ck_inference_profiles_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inference_profiles")),
        sa.UniqueConstraint("name", name=op.f("uq_inference_profiles_name")),
    )
    op.create_table(
        "label_definitions",
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
            "char_length(btrim(name)) > 0", name=op.f("ck_label_definitions_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_definitions")),
        sa.UniqueConstraint("name", name=op.f("uq_label_definitions_name")),
    )
    op.create_table(
        "label_sets",
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
            "char_length(btrim(name)) > 0", name=op.f("ck_label_sets_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_sets")),
        sa.UniqueConstraint("name", name=op.f("uq_label_sets_name")),
    )
    op.create_table(
        "prompt_templates",
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
            "char_length(btrim(name)) > 0", name=op.f("ck_prompt_templates_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_prompt_templates_name")),
    )
    op.create_table(
        "analysis_pipeline_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
                "(state = 'published' AND published_at IS NOT NULL "
                "AND archived_at IS NULL) OR "
                "(state = 'archived' AND published_at IS NOT NULL "
                "AND archived_at IS NOT NULL AND archived_at >= published_at)"
            ),
            name=op.f("ck_analysis_pipeline_versions_version_lifecycle"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_analysis_pipeline_versions_version_state"),
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_analysis_pipeline_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_pipeline_id"],
            ["analysis_pipelines.id"],
            name="fk_analysis_pipeline_versions_pipeline",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_pipeline_versions")),
        sa.UniqueConstraint(
            "analysis_pipeline_id", "version_number", name="uq_analysis_pipeline_version_number"
        ),
    )
    op.create_index(
        "uq_analysis_pipeline_one_published",
        "analysis_pipeline_versions",
        ["analysis_pipeline_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "inference_profile_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("inference_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("provider_adapter", sa.String(length=128), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("api_secret_reference", sa.String(length=512), nullable=False),
        sa.Column(
            "capabilities",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("max_images", sa.Integer(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "structured_output_support",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "sampling_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
                "(state = 'published' AND published_at IS NOT NULL "
                "AND archived_at IS NULL) OR "
                "(state = 'archived' AND published_at IS NOT NULL "
                "AND archived_at IS NOT NULL AND archived_at >= published_at)"
            ),
            name=op.f("ck_inference_profile_versions_version_lifecycle"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sampling_parameters) = 'object'",
            name=op.f("ck_inference_profile_versions_sampling_object"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_inference_profile_versions_version_state"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(api_secret_reference)) > 0",
            name=op.f("ck_inference_profile_versions_secret_ref_not_blank"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(base_url)) > 0",
            name=op.f("ck_inference_profile_versions_base_url_not_blank"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(model_name)) > 0",
            name=op.f("ck_inference_profile_versions_model_not_blank"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(provider_adapter)) > 0",
            name=op.f("ck_inference_profile_versions_provider_not_blank"),
        ),
        sa.CheckConstraint(
            "max_concurrency > 0", name=op.f("ck_inference_profile_versions_concurrency_positive")
        ),
        sa.CheckConstraint(
            "max_images > 0", name=op.f("ck_inference_profile_versions_images_positive")
        ),
        sa.CheckConstraint(
            "max_input_tokens > 0", name=op.f("ck_inference_profile_versions_tokens_positive")
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0", name=op.f("ck_inference_profile_versions_timeout_positive")
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_inference_profile_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["inference_profile_id"],
            ["inference_profiles.id"],
            name="fk_inference_profile_versions_profile",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inference_profile_versions")),
        sa.UniqueConstraint(
            "inference_profile_id", "version_number", name="uq_inference_profile_version_number"
        ),
    )
    op.create_index(
        "uq_inference_profile_one_published",
        "inference_profile_versions",
        ["inference_profile_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "label_definition_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label_definition_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("negative", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
                "(state = 'published' AND published_at IS NOT NULL "
                "AND archived_at IS NULL) OR "
                "(state = 'archived' AND published_at IS NOT NULL "
                "AND archived_at IS NOT NULL AND archived_at >= published_at)"
            ),
            name=op.f("ck_label_definition_versions_version_lifecycle"),
        ),
        sa.CheckConstraint(
            "scope IN ('global', 'media')", name=op.f("ck_label_definition_versions_scope")
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_label_definition_versions_version_state"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(display_name)) > 0",
            name=op.f("ck_label_definition_versions_display_name_not_blank"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(key)) > 0", name=op.f("ck_label_definition_versions_key_not_blank")
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_label_definition_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["label_definition_id"],
            ["label_definitions.id"],
            name="fk_label_definition_versions_definition",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_definition_versions")),
        sa.UniqueConstraint(
            "label_definition_id", "version_number", name="uq_label_definition_version_number"
        ),
    )
    op.create_index(
        "uq_label_definition_one_published",
        "label_definition_versions",
        ["label_definition_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_index(
        "uq_label_definition_published_key",
        "label_definition_versions",
        ["key"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "label_set_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label_set_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
                "(state = 'published' AND published_at IS NOT NULL "
                "AND archived_at IS NULL) OR "
                "(state = 'archived' AND published_at IS NOT NULL "
                "AND archived_at IS NOT NULL AND archived_at >= published_at)"
            ),
            name=op.f("ck_label_set_versions_version_lifecycle"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_label_set_versions_version_state"),
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_label_set_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["label_set_id"],
            ["label_sets.id"],
            name=op.f("fk_label_set_versions_label_set_id_label_sets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_set_versions")),
        sa.UniqueConstraint("label_set_id", "version_number", name="uq_label_set_version_number"),
    )
    op.create_index(
        "uq_label_set_one_published",
        "label_set_versions",
        ["label_set_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "prompt_template_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("prompt_template_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("system_prompt", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("user_prompt_template", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "declared_variables",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
                "(state = 'published' AND published_at IS NOT NULL "
                "AND archived_at IS NULL) OR "
                "(state = 'archived' AND published_at IS NOT NULL "
                "AND archived_at IS NOT NULL AND archived_at >= published_at)"
            ),
            name=op.f("ck_prompt_template_versions_version_lifecycle"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_prompt_template_versions_version_state"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(system_prompt)) > 0 OR char_length(btrim(user_prompt_template)) > 0",
            name=op.f("ck_prompt_template_versions_content_not_blank"),
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_prompt_template_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_prompt_template_versions_prompt_template_id_prompt_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_template_versions")),
        sa.UniqueConstraint(
            "prompt_template_id", "version_number", name="uq_prompt_template_version_number"
        ),
    )
    op.create_index(
        "uq_prompt_template_one_published",
        "prompt_template_versions",
        ["prompt_template_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "analysis_stage_template_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_stage_template_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("prompt_template_version_id", sa.Uuid(), nullable=False),
        sa.Column("label_set_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "structured_output_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("input_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("visual_composition_policy", sa.String(length=32), nullable=False),
        sa.Column("inference_profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("cache_policy", sa.String(length=32), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("retry_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("max_batch_size", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(state = 'draft' AND published_at IS NULL AND archived_at IS NULL) OR "
                "(state = 'published' AND published_at IS NOT NULL "
                "AND archived_at IS NULL) OR "
                "(state = 'archived' AND published_at IS NOT NULL "
                "AND archived_at IS NOT NULL AND archived_at >= published_at)"
            ),
            name=op.f("ck_analysis_stage_template_versions_version_lifecycle"),
        ),
        sa.CheckConstraint(
            (
                "(target_scope = 'global' AND execution_mode IN "
                "('batch_messages', 'single_message')) OR "
                "(target_scope = 'media' AND execution_mode IN "
                "('per_asset', 'batch_assets'))"
            ),
            name=op.f("ck_analysis_stage_template_versions_scope_mode"),
        ),
        sa.CheckConstraint(
            "cache_policy IN ('none', 'message', 'message_visual', 'asset')",
            name=op.f("ck_analysis_stage_template_versions_cache_policy"),
        ),
        sa.CheckConstraint(
            "execution_mode IN ('batch_messages', 'batch_assets') OR max_batch_size = 1",
            name=op.f("ck_analysis_stage_template_versions_non_batch_size"),
        ),
        sa.CheckConstraint(
            "execution_mode IN ('batch_messages', 'single_message', 'per_asset', 'batch_assets')",
            name=op.f("ck_analysis_stage_template_versions_execution_mode"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_policy) = 'object'",
            name=op.f("ck_analysis_stage_template_versions_input_policy_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(retry_policy) = 'object'",
            name=op.f("ck_analysis_stage_template_versions_retry_policy_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(structured_output_policy) = 'object'",
            name=op.f("ck_analysis_stage_template_versions_output_policy_object"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'archived')",
            name=op.f("ck_analysis_stage_template_versions_version_state"),
        ),
        sa.CheckConstraint(
            "target_scope IN ('global', 'media')",
            name=op.f("ck_analysis_stage_template_versions_scope"),
        ),
        sa.CheckConstraint(
            "visual_composition_policy IN ('raw', 'contact_sheet', 'adaptive')",
            name=op.f("ck_analysis_stage_template_versions_visual_policy"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0",
            name=op.f("ck_analysis_stage_template_versions_name_not_blank"),
        ),
        sa.CheckConstraint(
            "max_batch_size > 0", name=op.f("ck_analysis_stage_template_versions_batch_positive")
        ),
        sa.CheckConstraint(
            "max_concurrency > 0",
            name=op.f("ck_analysis_stage_template_versions_concurrency_positive"),
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0", name=op.f("ck_analysis_stage_template_versions_timeout_positive")
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_analysis_stage_template_versions_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_stage_template_id"],
            ["analysis_stage_templates.id"],
            name="fk_analysis_stage_versions_template",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["inference_profile_version_id"],
            ["inference_profile_versions.id"],
            name="fk_analysis_stage_versions_profile",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["label_set_version_id"],
            ["label_set_versions.id"],
            name="fk_analysis_stage_versions_label_set",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_version_id"],
            ["prompt_template_versions.id"],
            name="fk_analysis_stage_versions_prompt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_stage_template_versions")),
        sa.UniqueConstraint(
            "analysis_stage_template_id", "version_number", name="uq_analysis_stage_version_number"
        ),
    )
    op.create_index(
        "uq_analysis_stage_one_published",
        "analysis_stage_template_versions",
        ["analysis_stage_template_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "label_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label_set_version_id", sa.Uuid(), nullable=False),
        sa.Column("label_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("activation_threshold", sa.Float(), nullable=False),
        sa.Column("prompt_hint", sa.Text(), server_default=sa.text("''"), nullable=False),
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
            "activation_threshold >= 0 AND activation_threshold <= 1",
            name=op.f("ck_label_bindings_threshold"),
        ),
        sa.CheckConstraint(
            "output_order >= 0", name=op.f("ck_label_bindings_output_order_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["label_definition_version_id"],
            ["label_definition_versions.id"],
            name="fk_label_definition_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["label_set_version_id"],
            ["label_set_versions.id"],
            name=op.f("fk_label_bindings_label_set_version_id_label_set_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_bindings")),
        sa.UniqueConstraint(
            "label_set_version_id",
            "label_definition_version_id",
            name="uq_label_binding_definition",
        ),
        sa.UniqueConstraint(
            "label_set_version_id", "output_order", name="uq_label_binding_output_order"
        ),
    )
    op.create_table(
        "input_manifests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_stage_template_version_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(input_manifest_hash) = 64 AND input_manifest_hash !~ '[^0-9a-f]'",
            name=op.f("ck_input_manifests_hash_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'", name=op.f("ck_input_manifests_content_object")
        ),
        sa.CheckConstraint(
            "manifest_version > 0", name=op.f("ck_input_manifests_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_stage_template_version_id"],
            ["analysis_stage_template_versions.id"],
            name="fk_analysis_stage_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_input_manifests")),
    )
    op.create_index(
        "ix_input_manifests_hash", "input_manifests", ["input_manifest_hash"], unique=False
    )
    op.create_table(
        "pipeline_stage_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.String(length=128), nullable=False),
        sa.Column("analysis_stage_template_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "depends_on",
            postgresql.ARRAY(sa.String(length=128)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("run_if", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "parameter_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            "jsonb_typeof(parameter_overrides) = 'object'",
            name=op.f("ck_pipeline_stage_nodes_overrides_object"),
        ),
        sa.CheckConstraint(
            "run_if IS NULL OR jsonb_typeof(run_if) = 'object'",
            name=op.f("ck_pipeline_stage_nodes_run_if_object"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(node_key)) > 0",
            name=op.f("ck_pipeline_stage_nodes_node_key_not_blank"),
        ),
        sa.CheckConstraint(
            "output_order >= 0", name=op.f("ck_pipeline_stage_nodes_output_order_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_pipeline_version_id"],
            ["analysis_pipeline_versions.id"],
            name="fk_pipeline_nodes_pipeline_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_stage_template_version_id"],
            ["analysis_stage_template_versions.id"],
            name="fk_analysis_stage_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_stage_nodes")),
        sa.UniqueConstraint(
            "analysis_pipeline_version_id", "node_key", name="uq_pipeline_stage_node_key"
        ),
        sa.UniqueConstraint(
            "analysis_pipeline_version_id", "output_order", name="uq_pipeline_stage_output_order"
        ),
    )
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_pipeline_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "run_mode", sa.String(length=16), server_default=sa.text("'formal'"), nullable=False
        ),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column(
            "facts_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            (
                "(status = 'pending' AND started_at IS NULL "
                "AND completed_at IS NULL) OR "
                "(status = 'running' AND started_at IS NOT NULL "
                "AND completed_at IS NULL) OR "
                "(status IN ('completed', 'blocked_negative_gate', 'failed') "
                "AND started_at IS NOT NULL AND completed_at IS NOT NULL)"
            ),
            name=op.f("ck_analysis_runs_lifecycle"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(facts_snapshot) = 'object'", name=op.f("ck_analysis_runs_facts_object")
        ),
        sa.CheckConstraint(
            "run_mode IN ('formal', 'reanalysis', 'test')", name=op.f("ck_analysis_runs_mode")
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (last_error_code IS NOT NULL AND last_error_type IS NOT NULL)",
            name=op.f("ck_analysis_runs_failure_metadata"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'blocked_negative_gate', 'failed')",
            name=op.f("ck_analysis_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_pipeline_version_id"],
            ["analysis_pipeline_versions.id"],
            name="fk_analysis_runs_pipeline_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_analysis_runs_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_runs")),
    )
    op.create_index(
        "ix_analysis_runs_message_created",
        "analysis_runs",
        ["message_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_analysis_runs_status", "analysis_runs", ["status"], unique=False)
    op.create_table(
        "inference_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("input_manifest_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_stage_template_version_id", sa.Uuid(), nullable=False),
        sa.Column("provider_adapter", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("request_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "structured_output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("structured_output_schema_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("token_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(status = 'pending' AND completed_at IS NULL) OR "
                "(status = 'succeeded' AND completed_at IS NOT NULL "
                "AND raw_response IS NOT NULL AND error_code IS NULL "
                "AND error_type IS NULL) OR "
                "(status = 'failed' AND completed_at IS NOT NULL "
                "AND error_code IS NOT NULL AND error_type IS NOT NULL "
                "AND retryable IS NOT NULL)"
            ),
            name=op.f("ck_inference_calls_lifecycle"),
        ),
        sa.CheckConstraint(
            (
                "char_length(structured_output_schema_hash) = 64 "
                "AND structured_output_schema_hash !~ '[^0-9a-f]'"
            ),
            name=op.f("ck_inference_calls_schema_hash_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request_summary) = 'object'",
            name=op.f("ck_inference_calls_request_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(structured_output_schema) = 'object'",
            name=op.f("ck_inference_calls_schema_object"),
        ),
        sa.CheckConstraint(
            "raw_response IS NULL OR jsonb_typeof(raw_response) = 'object'",
            name=op.f("ck_inference_calls_response_object"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')", name=op.f("ck_inference_calls_status")
        ),
        sa.CheckConstraint(
            "token_usage IS NULL OR jsonb_typeof(token_usage) = 'object'",
            name=op.f("ck_inference_calls_usage_object"),
        ),
        sa.CheckConstraint("attempt > 0", name=op.f("ck_inference_calls_attempt_positive")),
        sa.CheckConstraint(
            "char_length(btrim(model_name)) > 0", name=op.f("ck_inference_calls_model_not_blank")
        ),
        sa.CheckConstraint(
            "char_length(btrim(provider_adapter)) > 0",
            name=op.f("ck_inference_calls_provider_not_blank"),
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name=op.f("ck_inference_calls_http_status"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_inference_calls_latency_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_stage_template_version_id"],
            ["analysis_stage_template_versions.id"],
            name="fk_analysis_stage_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["input_manifest_id"],
            ["input_manifests.id"],
            name=op.f("fk_inference_calls_input_manifest_id_input_manifests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inference_calls")),
    )
    op.create_index(
        "ix_inference_calls_manifest", "inference_calls", ["input_manifest_id"], unique=False
    )
    op.create_index(
        "ix_inference_calls_stage_created",
        "inference_calls",
        ["analysis_stage_template_version_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "stage_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_stage_node_id", sa.Uuid(), nullable=True),
        sa.Column("analysis_stage_template_version_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("image_asset_id", sa.Uuid(), nullable=True),
        sa.Column("video_asset_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("inference_call_id", sa.Uuid(), nullable=True),
        sa.Column("parsed_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "cache_policy", sa.String(length=32), server_default=sa.text("'none'"), nullable=False
        ),
        sa.Column("cache_scope_identity", sa.String(length=512), nullable=True),
        sa.Column("semantic_cache_key", sa.String(length=64), nullable=True),
        sa.Column("result_origin", sa.String(length=16), nullable=True),
        sa.Column("reused_from_stage_run_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            (
                "(result_origin IS NULL AND reused_from_stage_run_id IS NULL) OR "
                "(result_origin = 'inference' "
                "AND reused_from_stage_run_id IS NULL) OR "
                "(result_origin = 'cache' "
                "AND reused_from_stage_run_id IS NOT NULL)"
            ),
            name=op.f("ck_stage_runs_cache_source"),
        ),
        sa.CheckConstraint(
            (
                "(status = 'retry_wait' AND next_retry_at IS NOT NULL) OR "
                "(status <> 'retry_wait' AND next_retry_at IS NULL)"
            ),
            name=op.f("ck_stage_runs_retry_schedule"),
        ),
        sa.CheckConstraint(
            (
                "(status = 'running' AND lease_token IS NOT NULL "
                "AND lease_expires_at IS NOT NULL AND attempt_count > 0) OR "
                "(status <> 'running' AND lease_token IS NULL "
                "AND lease_expires_at IS NULL)"
            ),
            name=op.f("ck_stage_runs_lease"),
        ),
        sa.CheckConstraint(
            (
                "(target_kind = 'message' AND target_scope = 'global' "
                "AND target_id = message_id AND image_asset_id IS NULL "
                "AND video_asset_id IS NULL) OR "
                "(target_kind = 'image_asset' AND target_scope = 'media' "
                "AND target_id = image_asset_id AND image_asset_id IS NOT NULL "
                "AND video_asset_id IS NULL) OR "
                "(target_kind = 'video_asset' AND target_scope = 'media' "
                "AND target_id = video_asset_id AND video_asset_id IS NOT NULL "
                "AND image_asset_id IS NULL)"
            ),
            name=op.f("ck_stage_runs_target_identity"),
        ),
        sa.CheckConstraint(
            "cache_policy IN ('none', 'message', 'message_visual', 'asset')",
            name=op.f("ck_stage_runs_cache_policy"),
        ),
        sa.CheckConstraint(
            "parsed_result IS NULL OR jsonb_typeof(parsed_result) = 'object'",
            name=op.f("ck_stage_runs_result_object"),
        ),
        sa.CheckConstraint(
            "result_origin IS NULL OR result_origin IN ('inference', 'cache')",
            name=op.f("ck_stage_runs_result_origin"),
        ),
        sa.CheckConstraint(
            (
                "semantic_cache_key IS NULL OR "
                "(char_length(semantic_cache_key) = 64 "
                "AND semantic_cache_key !~ '[^0-9a-f]')"
            ),
            name=op.f("ck_stage_runs_cache_key_format"),
        ),
        sa.CheckConstraint(
            (
                "status <> 'succeeded' OR "
                "(parsed_result IS NOT NULL AND result_origin IS NOT NULL "
                "AND input_manifest_id IS NOT NULL)"
            ),
            name=op.f("ck_stage_runs_success_result"),
        ),
        sa.CheckConstraint(
            (
                "status IN ('pending', 'running', 'retry_wait', 'succeeded', "
                "'failed', 'skipped_condition', 'skipped_negative_gate')"
            ),
            name=op.f("ck_stage_runs_status"),
        ),
        sa.CheckConstraint(
            (
                "status NOT IN ('retry_wait', 'failed') OR "
                "(last_error_code IS NOT NULL AND last_error_type IS NOT NULL "
                "AND retryable IS NOT NULL AND last_failure_at IS NOT NULL)"
            ),
            name=op.f("ck_stage_runs_failure_metadata"),
        ),
        sa.CheckConstraint(
            (
                "status NOT IN ('succeeded', 'failed', 'skipped_condition', "
                "'skipped_negative_gate') OR completed_at IS NOT NULL"
            ),
            name=op.f("ck_stage_runs_terminal_timestamp"),
        ),
        sa.CheckConstraint(
            "target_kind IN ('message', 'image_asset', 'video_asset')",
            name=op.f("ck_stage_runs_target_kind"),
        ),
        sa.CheckConstraint(
            "target_scope IN ('global', 'media')", name=op.f("ck_stage_runs_target_scope")
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts AND max_attempts > 0",
            name=op.f("ck_stage_runs_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_stage_runs_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_stage_template_version_id"],
            ["analysis_stage_template_versions.id"],
            name="fk_analysis_stage_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["image_asset_id"],
            ["image_assets.id"],
            name=op.f("fk_stage_runs_image_asset_id_image_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inference_call_id"],
            ["inference_calls.id"],
            name=op.f("fk_stage_runs_inference_call_id_inference_calls"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["input_manifest_id"],
            ["input_manifests.id"],
            name=op.f("fk_stage_runs_input_manifest_id_input_manifests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_stage_runs_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_stage_node_id"],
            ["pipeline_stage_nodes.id"],
            name=op.f("fk_stage_runs_pipeline_stage_node_id_pipeline_stage_nodes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reused_from_stage_run_id"],
            ["stage_runs.id"],
            name=op.f("fk_stage_runs_reused_from_stage_run_id_stage_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_asset_id"],
            ["video_assets.id"],
            name=op.f("fk_stage_runs_video_asset_id_video_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stage_runs")),
        sa.UniqueConstraint(
            "analysis_run_id",
            "pipeline_stage_node_id",
            "analysis_stage_template_version_id",
            "target_id",
            name="uq_stage_run_business_target",
        ),
    )
    op.create_index(
        "ix_stage_runs_analysis_node",
        "stage_runs",
        ["analysis_run_id", "pipeline_stage_node_id"],
        unique=False,
    )
    op.create_index(
        "ix_stage_runs_retry_due", "stage_runs", ["status", "next_retry_at"], unique=False
    )
    op.create_index(
        "ix_stage_runs_semantic_cache",
        "stage_runs",
        ["semantic_cache_key"],
        unique=False,
        postgresql_where=sa.text("status = 'succeeded' AND semantic_cache_key IS NOT NULL"),
    )
    op.create_index(
        "ix_stage_runs_status_lease", "stage_runs", ["status", "lease_expires_at"], unique=False
    )
    op.create_index(
        "uq_stage_run_direct_target",
        "stage_runs",
        ["analysis_run_id", "analysis_stage_template_version_id", "target_id"],
        unique=True,
        postgresql_where=sa.text("pipeline_stage_node_id IS NULL"),
    )
    op.create_table(
        "model_label_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stage_run_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("image_asset_id", sa.Uuid(), nullable=True),
        sa.Column("video_asset_id", sa.Uuid(), nullable=True),
        sa.Column("label_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("label_key", sa.String(length=128), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("activated", sa.Boolean(), nullable=False),
        sa.Column("negative", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(target_kind = 'message' AND target_scope = 'global' "
                "AND target_id = message_id AND image_asset_id IS NULL "
                "AND video_asset_id IS NULL) OR "
                "(target_kind = 'image_asset' AND target_scope = 'media' "
                "AND target_id = image_asset_id AND image_asset_id IS NOT NULL "
                "AND video_asset_id IS NULL) OR "
                "(target_kind = 'video_asset' AND target_scope = 'media' "
                "AND target_id = video_asset_id AND video_asset_id IS NOT NULL "
                "AND image_asset_id IS NULL)"
            ),
            name=op.f("ck_model_label_assignments_target_identity"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'array'",
            name=op.f("ck_model_label_assignments_evidence_array"),
        ),
        sa.CheckConstraint(
            "target_kind IN ('message', 'image_asset', 'video_asset')",
            name=op.f("ck_model_label_assignments_target_kind"),
        ),
        sa.CheckConstraint(
            "target_scope IN ('global', 'media')",
            name=op.f("ck_model_label_assignments_target_scope"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(label_key)) > 0",
            name=op.f("ck_model_label_assignments_label_key_not_blank"),
        ),
        sa.CheckConstraint(
            "score >= 0 AND score <= 1", name=op.f("ck_model_label_assignments_score")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_model_label_assignments_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["image_asset_id"],
            ["image_assets.id"],
            name=op.f("fk_model_label_assignments_image_asset_id_image_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["label_definition_version_id"],
            ["label_definition_versions.id"],
            name="fk_label_definition_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_model_label_assignments_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stage_run_id"],
            ["stage_runs.id"],
            name=op.f("fk_model_label_assignments_stage_run_id_stage_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_asset_id"],
            ["video_assets.id"],
            name=op.f("fk_model_label_assignments_video_asset_id_video_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_label_assignments")),
        sa.UniqueConstraint(
            "stage_run_id", "label_definition_version_id", name="uq_model_assignment_stage_label"
        ),
    )
    op.create_index(
        "ix_model_assignments_message",
        "model_label_assignments",
        ["message_id", "target_scope"],
        unique=False,
    )
    op.create_index(
        "ix_model_assignments_target",
        "model_label_assignments",
        ["target_id", "activated"],
        unique=False,
    )
    op.add_column(
        "messages",
        sa.Column(
            "blocked_from_analysis",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "messages",
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("blocked_by_stage_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("blocked_by_label_assignment_id", sa.Uuid(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_messages_message_analysis_block_state"),
        "messages",
        "(blocked_from_analysis IS FALSE AND blocked_at IS NULL "
        "AND blocked_by_stage_run_id IS NULL "
        "AND blocked_by_label_assignment_id IS NULL) OR "
        "(blocked_from_analysis IS TRUE AND blocked_at IS NOT NULL "
        "AND blocked_by_stage_run_id IS NOT NULL "
        "AND blocked_by_label_assignment_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_messages_blocked_stage_run",
        "messages",
        "stage_runs",
        ["blocked_by_stage_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_messages_blocked_assignment",
        "messages",
        "model_label_assignments",
        ["blocked_by_label_assignment_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_enforce_analysis_version_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            IF TG_OP = ''DELETE'' THEN
                IF OLD.state IN (''published'', ''archived'') THEN
                    RAISE EXCEPTION ''published or archived analysis versions cannot be deleted'';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.state = ''archived'' THEN
                RAISE EXCEPTION ''archived analysis versions are immutable'';
            END IF;

            IF OLD.state = ''draft'' AND NEW.state NOT IN (''draft'', ''published'') THEN
                RAISE EXCEPTION ''draft analysis versions can only be published'';
            END IF;

            IF OLD.state = ''published'' AND (
                NEW.state NOT IN (''published'', ''archived'')
                OR (to_jsonb(NEW) - ''state'' - ''archived_at'')
                    IS DISTINCT FROM
                   (to_jsonb(OLD) - ''state'' - ''archived_at'')
            ) THEN
                RAISE EXCEPTION ''published analysis version content is immutable'';
            END IF;

            RETURN NEW;
        END;'
        """
    )
    version_triggers = (
        ("label_definition_versions", "trg_label_definition_version_immutable"),
        ("label_set_versions", "trg_label_set_version_immutable"),
        ("prompt_template_versions", "trg_prompt_template_version_immutable"),
        ("inference_profile_versions", "trg_inference_profile_version_immutable"),
        ("analysis_stage_template_versions", "trg_analysis_stage_version_immutable"),
        ("analysis_pipeline_versions", "trg_analysis_pipeline_version_immutable"),
    )
    for table_name, trigger_name in version_triggers:
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "tgcurator_enforce_analysis_version_immutability()"
        )

    op.execute(
        """
        CREATE FUNCTION tgcurator_require_draft_label_set_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'DECLARE
            owner_state text;
        BEGIN
            IF TG_OP IN (''UPDATE'', ''DELETE'') THEN
                SELECT state INTO owner_state
                FROM label_set_versions
                WHERE id = OLD.label_set_version_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''label bindings may mutate only for draft label-set versions'';
                END IF;
            END IF;
            IF TG_OP IN (''INSERT'', ''UPDATE'') THEN
                SELECT state INTO owner_state
                FROM label_set_versions
                WHERE id = NEW.label_set_version_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''label bindings may mutate only for draft label-set versions'';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = ''DELETE'' THEN OLD ELSE NEW END;
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_label_bindings_draft_only "
        "BEFORE INSERT OR UPDATE OR DELETE ON label_bindings "
        "FOR EACH ROW EXECUTE FUNCTION tgcurator_require_draft_label_set_version()"
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_require_draft_analysis_pipeline_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'DECLARE
            owner_state text;
        BEGIN
            IF TG_OP IN (''UPDATE'', ''DELETE'') THEN
                SELECT state INTO owner_state
                FROM analysis_pipeline_versions
                WHERE id = OLD.analysis_pipeline_version_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''pipeline nodes may mutate only for draft pipeline versions'';
                END IF;
            END IF;
            IF TG_OP IN (''INSERT'', ''UPDATE'') THEN
                SELECT state INTO owner_state
                FROM analysis_pipeline_versions
                WHERE id = NEW.analysis_pipeline_version_id;
                IF owner_state IS DISTINCT FROM ''draft'' THEN
                    RAISE EXCEPTION ''pipeline nodes may mutate only for draft pipeline versions'';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = ''DELETE'' THEN OLD ELSE NEW END;
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_pipeline_stage_nodes_draft_only "
        "BEFORE INSERT OR UPDATE OR DELETE ON pipeline_stage_nodes "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_require_draft_analysis_pipeline_version()"
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_reject_input_manifest_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            RAISE EXCEPTION ''input manifests are immutable'';
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_input_manifests_immutable "
        "BEFORE UPDATE OR DELETE ON input_manifests "
        "FOR EACH ROW EXECUTE FUNCTION tgcurator_reject_input_manifest_mutation()"
    )


def downgrade() -> None:
    op.drop_constraint("fk_messages_blocked_assignment", "messages", type_="foreignkey")
    op.drop_constraint("fk_messages_blocked_stage_run", "messages", type_="foreignkey")
    op.drop_constraint(
        op.f("ck_messages_message_analysis_block_state"),
        "messages",
        type_="check",
    )
    op.drop_column("messages", "blocked_by_label_assignment_id")
    op.drop_column("messages", "blocked_by_stage_run_id")
    op.drop_column("messages", "blocked_at")
    op.drop_column("messages", "blocked_from_analysis")

    op.execute("DROP TRIGGER trg_input_manifests_immutable ON input_manifests")
    op.execute("DROP TRIGGER trg_pipeline_stage_nodes_draft_only ON pipeline_stage_nodes")
    op.execute("DROP TRIGGER trg_label_bindings_draft_only ON label_bindings")
    version_triggers = (
        ("analysis_pipeline_versions", "trg_analysis_pipeline_version_immutable"),
        ("analysis_stage_template_versions", "trg_analysis_stage_version_immutable"),
        ("inference_profile_versions", "trg_inference_profile_version_immutable"),
        ("prompt_template_versions", "trg_prompt_template_version_immutable"),
        ("label_set_versions", "trg_label_set_version_immutable"),
        ("label_definition_versions", "trg_label_definition_version_immutable"),
    )
    for table_name, trigger_name in version_triggers:
        op.execute(f"DROP TRIGGER {trigger_name} ON {table_name}")
    op.execute("DROP FUNCTION tgcurator_reject_input_manifest_mutation()")
    op.execute("DROP FUNCTION tgcurator_require_draft_analysis_pipeline_version()")
    op.execute("DROP FUNCTION tgcurator_require_draft_label_set_version()")
    op.execute("DROP FUNCTION tgcurator_enforce_analysis_version_immutability()")

    op.drop_index("ix_model_assignments_target", table_name="model_label_assignments")
    op.drop_index("ix_model_assignments_message", table_name="model_label_assignments")
    op.drop_table("model_label_assignments")

    op.drop_index("uq_stage_run_direct_target", table_name="stage_runs")
    op.drop_index("ix_stage_runs_status_lease", table_name="stage_runs")
    op.drop_index("ix_stage_runs_semantic_cache", table_name="stage_runs")
    op.drop_index("ix_stage_runs_retry_due", table_name="stage_runs")
    op.drop_index("ix_stage_runs_analysis_node", table_name="stage_runs")
    op.drop_table("stage_runs")

    op.drop_index("ix_inference_calls_stage_created", table_name="inference_calls")
    op.drop_index("ix_inference_calls_manifest", table_name="inference_calls")
    op.drop_table("inference_calls")

    op.drop_index("ix_analysis_runs_status", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_message_created", table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_table("pipeline_stage_nodes")

    op.drop_index("ix_input_manifests_hash", table_name="input_manifests")
    op.drop_table("input_manifests")
    op.drop_table("label_bindings")

    op.drop_index(
        "uq_analysis_stage_one_published",
        table_name="analysis_stage_template_versions",
    )
    op.drop_table("analysis_stage_template_versions")

    op.drop_index("uq_prompt_template_one_published", table_name="prompt_template_versions")
    op.drop_table("prompt_template_versions")
    op.drop_index("uq_label_set_one_published", table_name="label_set_versions")
    op.drop_table("label_set_versions")
    op.drop_index("uq_label_definition_published_key", table_name="label_definition_versions")
    op.drop_index("uq_label_definition_one_published", table_name="label_definition_versions")
    op.drop_table("label_definition_versions")
    op.drop_index("uq_inference_profile_one_published", table_name="inference_profile_versions")
    op.drop_table("inference_profile_versions")
    op.drop_index("uq_analysis_pipeline_one_published", table_name="analysis_pipeline_versions")
    op.drop_table("analysis_pipeline_versions")

    op.drop_table("prompt_templates")
    op.drop_table("label_sets")
    op.drop_table("label_definitions")
    op.drop_table("inference_profiles")
    op.drop_table("analysis_stage_templates")
    op.drop_table("analysis_pipelines")
