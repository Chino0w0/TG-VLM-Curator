# Agent Handoff - TG-VLM-Curator

Updated: November 12, 2026 (Asia/Shanghai)
Workspace: E:\Project\TG-VLM-Curator
Remote: https://github.com/Chino0w0/TG-VLM-Curator

## User delivery rules

- Follow tg-vlm-curator-module-implementation.md and tg-vlm-curator-architecture.md.
- A delivery unit is one complete documented module, not one file or one small change.
- Use one branch, one commit, and one English pull request for each completed module.
- Leave stacked pull requests open without merging until the user reviews them.
- Push completed module branches to the GitHub remote only after all local checks pass.
- PostgreSQL is business truth; Redis/Celery messages are reconstructable UUID-only wake-ups.
- Do not bundle, deploy, or fabricate an inference model/provider.

## Current delivery

- M0, M1, M2, M3, M4, and M5 are merged into origin/main (`78d3d3e`).
- M6 (`module/m6-publication-lifecycle`) is open as PR #27 (`2163124`).
- M7 (`module/m7-pipeline-orchestration`) is open as PR #28 (`0f82278`), stacked on
  `module/m6-publication-lifecycle`.
- M8 (`module/m8-admin-api`) is open as PR #29 (`6b086aa`), stacked on
  `module/m7-pipeline-orchestration`.
- M9 (`module/m9-web-admin`) is open as PR #30 (`eb08a7b`), stacked on `module/m8-admin-api`.
- M10 (`module/m10-deployment-and-operations`) is stacked on `module/m9-web-admin` and completes the
  entire first-edition TG-VLM-Curator architecture (M0-M10): production 10-service Docker Compose
  topology, graceful `SIGTERM` daemons and FFmpeg child-process cleanup, backup/restore scripts,
  `StorageConsistencyScanner`, and closed-loop E2E verification.

## M10 completed scope

M10 delivers the production container topology, operational tooling, and closed-loop E2E suite:

- Container & Compose topology (`docker/backend.Dockerfile`, `docker/web.Dockerfile`,
  `docker/nginx.conf`, `.dockerignore`, `.env.example`, `docker-compose.yml`): exact 10 core
  services (`postgres`, `redis`, `api`, `web`, `telegram-ingestor`, `scheduler`, `worker-media`,
  `worker-analysis`, `worker-publish`, `worker-maintenance`), 4 named volumes (`postgres-data`,
  `redis-data`, `archive-data`, `temp-media`), read-only file secret `app_master_key`, and strictly
  zero bundled LLM/VLM services.
- Daemon lifecycles, subprocess cleanup, and health probes (`apps/scheduler/__main__.py`,
  `apps/telegram_ingestor/__main__.py`, `apps/worker/__main__.py`, `apps/worker/runtime.py`,
  `tgcurator/infrastructure/media/ffmpeg.py`, `apps/healthcheck.py`): graceful `SIGTERM`/`SIGINT`
  shutdown loops, media-group flush before ingestor exit, active `ffprobe`/`ffmpeg` child-process
  termination on worker shutdown, `_LocalStorageResolver` archive cleanup wiring, and PostgreSQL vs
  PostgreSQL+Redis service readiness probes (`tgcurator-healthcheck`).
- Backup/restore & consistency scanner (`scripts/backup-postgres.sh`, `scripts/backup-media.sh`,
  `scripts/restore-postgres.sh`, `tgcurator/domain/archive/consistency.py`,
  `tgcurator/application/maintenance/consistency.py`,
  `tgcurator/infrastructure/database/consistency_repository.py`, `apps/worker/consistency_cli.py`):
  UTC timestamp + SHA-256 backup manifests, independent `APP_MASTER_KEY` vault reminders, and
  `tgcurator-check-consistency` verifying `READY` archive blobs against observed files.
- Closed-loop E2E & topology tests (`tests/e2e/test_closed_loop_pipeline.py`,
  `tests/unit/test_consistency_scanner.py`, `tests/unit/test_deployment_topology.py`).

## M9 completed scope

M9 implements the browser administration console (`web/`) over `/api/v1/*`:

- Typed HTTP client & session security (`web/src/api/types.ts`, `web/src/api/client.ts`,
  `web/src/components/LoginView.tsx`): `credentials: "include"` cookie propagation, automatic
  `X-CSRF-Token` header attachment on mutating requests, autofill-compliant login controls, and
  explicit session logout.
- Reusable UI primitives (`web/src/components/ResponsiveTable.tsx`, `ModalDialog.tsx`, `Tabs.tsx`,
  `web/src/styles/app.css`): keyboard-focusable container-query responsive tables, native modal
  `<dialog>` with safe destructive focus defaults, accessible tablists, and `:user-invalid` forms.
- All workspaces (`web/src/workspaces/`): `DashboardWorkspace`, `SourceChannelsWorkspace`,
  `ProcessingRangesWorkspace`, `TelegramIdentitiesWorkspace` (write-only secrets that immediately
  scrub plaintext), `MessagesWorkspace` (16 filters + cursor pagination),
  `MessageDetailWorkspace` (6-stage causal chain inspector + audited skip), `ReviewWorkspace`
  (append-only manual overrides + dry-run/formal re-route diffs), `AnalysisConfigWorkspace`
  (immutable published versions + clone-to-draft + `Prompt Test Bench`), `RoutingWorkspace`,
  `ArchiveManagementWorkspace` (two-step `Dry-Run -> Confirm` lock invalidated on any filter edit),
  `OperationsWorkspace` (targeted step retry), and `AuditLogWorkspace`.
- Automated Vitest + Testing Library suite (`web/src/__tests__/app.test.tsx`).

## M8 completed scope

M8 exposes the complete single-administrator FastAPI control plane over the domain and application
layers without breaking dependency boundaries:

- Domain additions: `tgcurator/shared/pagination.py` (opaque `(timestamp, entity_id)` cursor codec),
  `tgcurator/domain/telegram/capabilities.py` (`TelegramCapabilityResolver` across `mtproto_user`
  and `bot` identities plus protected-content constraints), and `tgcurator/domain/audit/models.py`
  (recursive credential/token/cookie redaction for `AuditEvent` snapshots).
- Application administration layer (`tgcurator/application/admin/`): `AdminSessionService`
  (Argon2id verification, opaque SHA-256-hashed session tokens, sliding idle expiry, HMAC-SHA256
  CSRF tokens), `PromptTestBenchService` (`run_mode="test"` side-effect-free prompt/manifest/schema
  and external provider execution), `config_validation.py` (domain validation across all 10
  versioned configuration families), `metrics.py` (Prometheus 0.0.4 text exposition),
  `AdminApplicationService`, and `InMemoryAdminBackend`.
- PostgreSQL persistence & migration (`a8b2d4e1f305`): adds `admin_sessions`,
  `media_processing_profiles`, and `media_processing_profile_versions` (with partial unique index on
  published versions and immutability trigger), extends `SecretVault` with `rotate()` and
  `list_statuses()`, and implements `SqlAlchemyAdminBackend` across Section 37 message filters,
  causal message detail, review/reroute/reanalyze/skip/retry, archive cleanup, audit queries, and
  metrics snapshots.
- FastAPI routers & security (`apps/api/`): `/api/v1/*` endpoints with `HttpOnly`, `Secure` (outside
  dev/test), `SameSite=Lax` session cookies, mandatory `X-CSRF-Token` checks on mutating routes,
  structured error envelopes, `/metrics`, and unchanged PostgreSQL-only `/health/ready`.

## M8 migration

- Revision: a8b2d4e1f305
- Down revision: f1a7c3d9e204
- File: migrations/versions/a8b2d4e1f305_m8_admin_sessions_and_media_profiles.py
- Chronology date: November 12, 2026
- Adds `admin_sessions`, `media_processing_profiles`, and `media_processing_profile_versions` with
  check constraints, partial unique published index, and published/archived immutability trigger.
- Offline upgrade to M8 and downgrade from M8 to M7 are covered by integration tests.

## M7 completed scope

M7 connects ingestion, media archiving, versioned analysis, visual deduplication, formal routing,
and publication settlement into one continuous PostgreSQL-backed lifecycle:

- Domain visual deduplication (`tgcurator/domain/messages/deduplication.py`): order-independent
  SHA-256 visual fingerprints and exact Held-Karp bitmask DP pairing across image and video-cover
  perceptual hashes (never message text, source channel, media order, or representative video
  frames), supporting `skip` (`duplicate_skipped`) and `reclassify_with_text`
  (`duplicate_reclassify`).
- Domain continuous watermarks (`tgcurator/domain/processing/watermark.py`) and conditional DAG
  progression (`tgcurator/domain/analysis/progression.py`): monotonic gap-free watermark
  advancement across contiguous terminal messages and sparse Telegram IDs, plus topological DAG
  planning across `depends_on`, `run_if`, pre-download PreScreen roots, media readiness gates, and
  first-cause Negative Gate halting.
- Pre-download `batch_messages` and `batch_assets` co-leasing in
  `SqlAlchemyAnalysisStageRunRepository.load_stage_snapshot` (`FOR UPDATE SKIP LOCKED`) with
  multi-run Negative Gate grouping and completion in `AnalysisStageRunWorker`.
- `MessagePipelineCoordinator` and `SqlAlchemyMessagePipelineRepository`: `RangeExecution` handoff
  (`awaiting_pipeline`), pre-download PreScreen scheduling, Negative Gate media-wakeup cancellation
  while preserving formal Routing, post-PreScreen media archive wake-ups, `duplicate_matches`
  persistence, version-matched canonical visual stage cache reuse under `reclassify_with_text`,
  conditional DAG progression, formal `RoutingService.evaluate`, publication settlement gating,
  continuous watermark persistence, and wake-up repair across `range_execution`, `image_archive`,
  `video_archive`, and `analysis` queues.
- `SchedulerRuntime`, `WorkerRuntime`, and `apps/telegram_ingestor` (`TelegramIngestorRuntime`)
  wiring with `defer_media_wakeups=True` for realtime updates, bounded media-group flushing,
  reconnect reconciliation, source edit/delete lifecycle preservation, and graceful shutdown.

## M7 migration

- Revision: f1a7c3d9e204
- Down revision: e6d4a8b2c951
- File: migrations/versions/f1a7c3d9e204_m7_pipeline_dedup_watermarks.py
- Chronology date: September 20, 2026
- Extends `range_executions` with `awaiting_pipeline`; extends `messages` with
  `canonical_message_id`, `duplicate_status`, `duplicate_distance`, `terminal_reason`,
  `failure_stage`, `last_error_code`, check constraints, and indexes; adds `duplicate_matches`.
- Offline upgrade to M7 and downgrade from M7 to M6 are covered by integration tests.

## Important implementation locations

- tgcurator/domain/messages/deduplication.py
- tgcurator/domain/processing/watermark.py
- tgcurator/domain/analysis/progression.py
- tgcurator/application/ports/pipeline.py
- tgcurator/application/pipeline/coordinator.py
- tgcurator/application/analysis/worker.py
- tgcurator/application/processing/history_ingestion.py
- tgcurator/infrastructure/database/pipeline_repository.py
- tgcurator/infrastructure/database/analysis_repository.py
- tgcurator/infrastructure/database/message_ingest_repository.py
- tgcurator/infrastructure/database/routing_repository.py
- tgcurator/infrastructure/database/models.py
- migrations/versions/f1a7c3d9e204_m7_pipeline_dedup_watermarks.py
- apps/scheduler/runtime.py
- apps/worker/runtime.py
- apps/telegram_ingestor/runtime.py
- tests/unit/test_deduplication.py
- tests/unit/test_continuous_watermark.py
- tests/unit/test_pipeline_progression.py
- tests/unit/test_pipeline_coordinator.py
- tests/unit/test_telegram_ingestor_runtime.py

## Stacked PR chain (all left open for user review)

- `main` <- PR #27 `module/m6-publication-lifecycle`
- `module/m6-publication-lifecycle` <- PR #28 `module/m7-pipeline-orchestration`
- `module/m7-pipeline-orchestration` <- PR #29 `module/m8-admin-api`
- `module/m8-admin-api` <- PR #30 `module/m9-web-admin`
- `module/m9-web-admin` <- PR #31 `module/m10-deployment-and-operations`
