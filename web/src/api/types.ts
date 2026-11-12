export type WorkspaceKey =
  | "dashboard"
  | "source-channels"
  | "processing-ranges"
  | "telegram-identities"
  | "messages"
  | "message-detail"
  | "review"
  | "analysis-config"
  | "routing"
  | "archive-management"
  | "operations"
  | "audit-log";

export type ConfigFamily =
  | "label-definitions"
  | "label-sets"
  | "prompt-templates"
  | "inference-profiles"
  | "analysis-stages"
  | "analysis-pipelines"
  | "media-processing-profiles"
  | "source-channel-profiles"
  | "rendering-templates"
  | "routing-policies";

export interface AdminUser {
  id: string;
  username: string;
}

export interface AdminSessionResponse {
  user: AdminUser;
  session_id: string;
  csrf_token: string;
  expires_at: string;
}

export interface CursorPageResponse<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface ServiceProbe {
  alive: boolean;
  ready: boolean;
  database_ready: boolean;
  broker_ready: boolean;
  last_heartbeat_at: string | null;
  blockers: string[];
}

export interface OperationsMetrics {
  publication: {
    claimed: number;
    sent: number;
    retry_scheduled: number;
    failed: number;
    cancelled: number;
    reconciled_parts: number;
  };
  flood_wait: {
    events: number;
    wait_seconds_total: number;
    wait_seconds_max: number;
    last_observed_at: string | null;
    last_retry_at: string | null;
  };
  backlog: {
    publication_pending: number;
    publication_partial: number;
    publication_retry_due: number;
    cleanup_pending: number;
    cleanup_retry_due: number;
    durable_wakeups_due: number;
    observed_at: string | null;
  };
  worker: ServiceProbe;
  scheduler: ServiceProbe;
}

export interface DashboardSummary {
  source_channels: {
    total: number;
    enabled: number;
    active_profiles: number;
  };
  destination_channels: {
    total: number;
    enabled: number;
  };
  processing_ranges: {
    total: number;
    active: number;
    paused: number;
    completed: number;
    failed: number;
  };
  messages: {
    total: number;
    processed: number;
    processing: number;
    failed: number;
    blocked_from_analysis: number;
    unreviewed: number;
  };
  identities: {
    total: number;
    healthy: number;
    degraded: number;
    failed: number;
  };
  archive: {
    ready_images: number;
    ready_videos: number;
    deleted_assets: number;
    total_archive_bytes: number;
  };
  operations: OperationsMetrics;
}

export interface SecretStatusRecord {
  secret_id: string;
  secret_type: string;
  key_id: string;
  rotation_version?: number;
  configured?: boolean;
  created_at: string;
  updated_at?: string;
}

export interface TelegramCapabilities {
  identity_kind: string;
  health: string;
  enabled: boolean;
  can_read_source: boolean;
  can_download_media: boolean;
  can_native_forward: boolean;
  can_copy_publish: boolean;
  can_metadata_publish: boolean;
  can_supplement_reply: boolean;
  restrictions: string[];
}

export interface TelegramIdentity {
  id: string;
  name: string;
  identity_type: "mtproto_user" | "bot";
  secret_id: string;
  enabled: boolean;
  health_status: string;
  capabilities: TelegramCapabilities;
  capability_overrides?: Record<string, boolean>;
  created_at?: string;
  updated_at?: string;
}

export interface SourceChannel {
  id: string;
  telegram_peer_id: number;
  display_name: string;
  username: string | null;
  enabled: boolean;
  protected_content: boolean;
  default_read_identity_id: string | null;
  active_profile_version_id: string | null;
  last_observed_message_id?: number | null;
  last_observed_at?: string | null;
  last_ingested_message_id?: number | null;
  created_at?: string;
  updated_at?: string;
}

export interface DestinationChannel {
  id: string;
  telegram_peer_id: number;
  name: string;
  username?: string | null;
  enabled: boolean;
  default_publish_identity_id: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ProcessingRange {
  id: string;
  source_channel_id: string;
  start_at: string;
  end_mode: "fixed" | "latest";
  end_at: string | null;
  steady_after_seconds: number | null;
  status: string;
  active_high_watermark_message_id: number | null;
  processing_watermark_message_id: number | null;
  last_error_code?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface RangeExecution {
  id: string;
  processing_range_id: string;
  from_message_id_exclusive: number;
  to_message_id_inclusive: number;
  watermark_message_id: number;
  history_ingested: boolean;
  status: string;
  attempt_count: number;
  last_error_code?: string | null;
  created_at: string;
}

export interface ConfigVersion {
  id: string;
  entity_id: string;
  family?: string;
  version_number: number;
  state: "draft" | "published" | "archived" | "retired";
  content: Record<string, unknown>;
  content_hash?: string;
  created_at: string;
  published_at?: string | null;
  archived_at?: string | null;
}

export interface ConfigEntity {
  id: string;
  family: ConfigFamily;
  name: string;
  description?: string;
  source_channel_id?: string;
  published_version: ConfigVersion | null;
  versions: ConfigVersion[];
  created_at?: string;
}

export interface PromptTestBenchLabel {
  label_definition_version_id: string;
  label_key: string;
  scope: string;
  score: number;
  activated: boolean;
  negative: boolean;
}

export interface PromptTestBenchTargetResult {
  target_id: string;
  reason: string | null;
  evidence: string[];
  labels: PromptTestBenchLabel[];
}

export interface PromptTestBenchResponse {
  run_mode: "test";
  status: string;
  system_prompt: string | null;
  user_prompt: string;
  input_manifest: Record<string, unknown>;
  input_manifest_hash: string;
  structured_output_schema: Record<string, unknown>;
  structured_output_schema_hash: string;
  raw_response: Record<string, unknown> | null;
  parsed_results: PromptTestBenchTargetResult[];
  invalid_targets: Record<string, string>;
  missing_target_ids: string[];
  negative_gate_would_trigger: boolean;
  token_usage: Record<string, number> | null;
  latency_ms: number | null;
  error_code: string | null;
  error_type: string | null;
}

export interface MessageSummary {
  id: string;
  source_channel_id: string;
  primary_telegram_message_id: number;
  media_group_id?: string | null;
  published_at: string;
  original_text: string | null;
  media_count: number;
  processing_status: string;
  terminal_reason: string | null;
  failure_stage?: string | null;
  last_error_code?: string | null;
  duplicate_status: string;
  duplicate_distance?: number | null;
  canonical_message_id?: string | null;
  review_status: string;
  blocked_from_analysis: boolean;
  blocked_by_label_key?: string | null;
  source_deleted?: boolean;
}

export interface LabelFactItem {
  message_id: string;
  target_scope: "global" | "media";
  target_kind: string;
  target_id: string;
  label_definition_version_id: string;
  label_key: string;
  score: number;
  activated: boolean;
}

export interface ManualLabelEventItem {
  id: string;
  message_id: string;
  operation: "set" | "clear";
  target_scope: "global" | "media";
  target_kind: string;
  target_id: string;
  label_definition_version_id: string;
  label_key: string;
  score: number | null;
  activated: boolean | null;
  actor_admin_user_id: string;
  reason: string | null;
  created_at: string;
}

export interface NamespaceScopeSnapshot {
  global: Record<string, { score: number; activated: boolean; source?: string }>;
  media: Record<string, Record<string, { score: number; activated: boolean; source?: string }>>;
}

export interface StageRunCausalItem {
  id: string;
  analysis_run_id: string;
  node_key?: string;
  target_id?: string;
  status: string;
  result_origin?: string | null;
  attempt_count: number;
  last_error_code?: string | null;
  last_error_type?: string | null;
  input_manifest: {
    id: string;
    manifest_hash: string;
    content: Record<string, unknown>;
  } | null;
  inference_call: {
    id: string;
    provider_adapter: string;
    model_name: string;
    status: string;
    http_status: number | null;
    latency_ms: number | null;
    structured_output_schema_hash: string | null;
    token_usage: Record<string, unknown> | null;
    raw_response?: Record<string, unknown> | null;
    error_code?: string | null;
    error_type?: string | null;
  } | null;
}

export interface AnalysisRunCausalItem {
  id: string;
  analysis_pipeline_version_id: string;
  run_mode: string;
  status: string;
  blocked_by_negative_gate?: boolean;
  created_at: string;
  stage_runs: StageRunCausalItem[];
}

export interface PublicationIntentCausalItem {
  id: string;
  destination_channel_id: string;
  publish_identity_id: string | null;
  publication_mode: string;
  status: string;
  attempt_count: number;
  last_error_code?: string | null;
  next_retry_at?: string | null;
  attempts: Array<{
    id: string;
    attempt_number: number;
    status: string;
    error_code?: string | null;
    error_type?: string | null;
    flood_wait_seconds?: number | null;
    created_at?: string;
  }>;
  records: Array<{
    id: string;
    part_kind: string;
    remote_message_id: number;
    created_at?: string;
  }>;
}

export interface RoutingEvaluationCausalItem {
  id: string;
  routing_request_id: string;
  routing_policy_version_id: string;
  facts_hash: string;
  facts_snapshot: Record<string, unknown>;
  rule_outcomes: Array<Record<string, unknown>>;
  stopped_at_rule_id: string | null;
  created_at: string;
  publication_intents: PublicationIntentCausalItem[];
}

export interface AuditEventItem {
  id: string;
  event_type: string;
  entity_type: string;
  entity_id: string | null;
  actor_admin_user_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface MessageDetail {
  message: MessageSummary & {
    telegram_metadata?: Record<string, unknown>;
    visual_fingerprint?: string | null;
  };
  parts: Array<{
    id: string;
    telegram_message_id: number;
    part_index?: number;
    media_kind?: string | null;
  }>;
  media: {
    images: Array<{
      id: string;
      archive_state: string;
      storage_key?: string | null;
      archive_sha256?: string | null;
      perceptual_hash?: string | null;
      archive_size_bytes?: number | null;
      width?: number | null;
      height?: number | null;
    }>;
    videos: Array<{
      id: string;
      archive_state: string;
      cover_perceptual_hash?: string | null;
      duration_seconds?: number | null;
      frames: Array<{
        id: string;
        frame_index: number;
        timestamp_seconds?: number;
        archive_state: string;
        archive_size_bytes?: number | null;
      }>;
    }>;
  };
  duplicate_match: {
    canonical_message_id: string;
    duplicate_policy: string;
    max_hamming_distance: number;
  } | null;
  analysis_runs: AnalysisRunCausalItem[];
  labels: {
    model: LabelFactItem[];
    manual_events: ManualLabelEventItem[];
    namespaces: {
      model: NamespaceScopeSnapshot;
      manual: NamespaceScopeSnapshot;
      effective: NamespaceScopeSnapshot;
    };
  };
  review_events: Array<{
    id: string;
    previous_status: string;
    new_status: string;
    reason: string | null;
    created_at: string;
  }>;
  routing_evaluations: RoutingEvaluationCausalItem[];
  audit_events: AuditEventItem[];
}

export interface RerouteResult {
  evaluation_id: string;
  routing_request_id: string;
  routing_policy_version_id: string;
  dry_run: boolean;
  persisted: boolean;
  facts_hash: string;
  matched_rule_ids: string[];
  stopped_at_rule_id: string | null;
  planned_intents: Array<{
    destination_channel_id: string;
    publication_mode: string;
    rendering_template_version_id: string | null;
  }>;
  comparison: {
    existing_destinations: string[];
    planned_destinations: string[];
    added_destinations: string[];
    unchanged_destinations?: string[];
  };
}

export interface CleanupFilterPayload {
  archived_from?: string | null;
  archived_before?: string | null;
  source_channel_ids?: string[];
  message_published_from?: string | null;
  message_published_before?: string | null;
  model_label_keys?: string[];
  manual_label_keys?: string[];
  effective_label_keys?: string[];
  label_scopes?: string[];
  negative_label?: boolean | null;
  review_statuses?: string[];
  processing_statuses?: string[];
  duplicate_statuses?: string[];
  media_types?: string[];
  blocked_from_analysis?: boolean | null;
}

export interface CleanupDryRun {
  id: string;
  actor_admin_user_id: string;
  filter_snapshot: Record<string, unknown>;
  filter_hash: string;
  estimated_asset_count: number;
  estimated_message_count: number;
  estimated_bytes: number;
  created_at: string;
}

export interface CleanupJob {
  id: string;
  dry_run_id: string;
  filter_hash: string;
  estimated_asset_count: number;
  estimated_message_count: number;
  estimated_bytes: number;
  item_count?: number;
  deleted_count?: number;
  skipped_active_count?: number;
  status: string;
  created_at: string;
}

export interface FailedStepItem {
  step_kind: string;
  target_id: string;
  message_id?: string | null;
  status: string;
  error_code: string | null;
  error_type?: string | null;
  attempt_count: number;
}

export interface OperationsStatusResponse {
  operations: OperationsMetrics;
  queues: Record<string, number>;
  failed_count: number;
}

export interface MessageSearchFilters {
  source_channel_id?: string;
  published_from?: string;
  published_before?: string;
  processing_status?: string;
  review_status?: string;
  duplicate_status?: string;
  blocked_from_analysis?: string;
  negative_label?: string;
  label_key?: string;
  label_namespace?: string;
  label_scope?: string;
  pipeline_version_id?: string;
  routing_policy_version_id?: string;
  destination_channel_id?: string;
  archive_state?: string;
  query?: string;
}
