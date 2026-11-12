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
- M7 (`module/m7-pipeline-orchestration`) is stacked on `module/m6-publication-lifecycle` and
  delivers end-to-end pipeline orchestration, visual deduplication, continuous gap-free watermarks,
  full-queue wake-up repair, and the `apps/telegram_ingestor` composition root.
- M8 through M10 remain in tg-vlm-curator-module-implementation.md.

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

## Next modules

- M8 (`module/m8-admin-api`): FastAPI administration endpoints, Argon2id session auth + CSRF,
  sanitized audit events, isolated Prompt Test Bench, cursor-paginated message/causal inspection,
  and Prometheus `/metrics`.
- M9 (`module/m9-web-admin`): React + TypeScript + Vite single-administrator web console covering
  all 11 architectural workspaces.
- M10 (`module/m10-deployment-and-operations`): Docker Compose 10-service topology, graceful
  daemon lifecycles, backup/restore scripts, storage consistency scanner, and closed-loop E2E
  verification.
