# Agent Handoff - TG-VLM-Curator

Updated: November 12, 2026 (Asia/Shanghai)
Workspace: E:\Project\TG-VLM-Curator
Remote: https://github.com/Chino0w0/TG-VLM-Curator

## User delivery rules

- Follow tg-vlm-curator-module-implementation.md and tg-vlm-curator-architecture.md.
- A delivery unit is one complete documented module, not one file or one small change.
- Use one branch, one commit, and one English pull request for each completed module.
- Push completed module branches to the GitHub remote only after all local checks pass.
- PostgreSQL is business truth; Redis/Celery messages are reconstructable UUID-only wake-ups.
- Do not bundle, deploy, or fabricate an inference model/provider.

## Current delivery

- M0, M1, M2, M3, M4, and M5 are merged into origin/main.
- M5 was merged through English pull request #26 at merge commit 78d3d3e; its implementation
  commit remains f215e8f.
- M6 publication, archive cleanup, and operations is the current delivery on branch
  module/m6-publication-lifecycle, based directly on the M5 merge commit (78d3d3e).
- M6 implementation, migration, stateful failure-injection suite, documentation, and final local
  validation are complete on the branch and are delivered as exactly one implementation commit and
  one English pull request.
- M7 through M10 are documented as the post-M6 roadmap in tg-vlm-curator-module-implementation.md.

## M6 completed scope

M6 implements leased Telegram publication execution, manual two-step archive cleanup, and
runtime/backlog observability while preserving PostgreSQL as the sole source of business truth:

- Restricted dictionary-path template interpolation (`RestrictedTemplateRenderer`) with no loops,
  attribute traversal, or arbitrary code execution, producing canonical rendered-context snapshots
  and SHA-256 hashes frozen atomically alongside `PublicationIntent` rows and UUID-only
  `publication` wake-ups during formal `RoutingService.evaluate` runs.
- Leased `PublicationIntent` execution (`SKIP LOCKED`), auditable `PublishAttempt` records,
  deterministic 63-bit MTProto `random_id` persistence plus in-flight step tracking before external
  Telegram calls, incremental `PublishRecord` commits (`partial` recovery for
  `native_forward_with_supplement`), clean `release()` transitions to `partial` or `pending`, and
  remote reconciliation of unresolved in-flight steps prior to retries.
- All four publication modes (`native_forward_with_supplement`, `copy_with_caption`,
  `forward_only`, `metadata_only`) via `TelegramPublisher` and `TelethonPublisher`, translating
  FloodWait, permission, expired-session, protected-content, and unavailable-media failures into
  typed application errors, persisting exact non-blocking `next_retry_at` timestamps on FloodWait,
  and honoring explicit `fail`, `metadata_only`, or `skip` protected-content fallbacks.
- Manual two-step archive cleanup (`Dry-Run` -> SHA-256 hash-verified `Confirm` -> leased
  `maintenance` worker) across 15 composable filter criteria, recording sanitized `AuditEvent`
  snapshots in the same transaction as dry-run creation and job confirmation, row-locking `READY`
  media assets (also mirrored in `SqlAlchemyAnalysisRepository` when staging media inputs),
  revalidating active `StageRun` and `PublicationIntent` usage, deleting only archive blobs outside
  transactions, and marking asset metadata `deleted` while preserving messages, hashes, visual
  fingerprints, labels, routing, and publication history.
- Framework-neutral `OperationsTelemetry` and `OperationsBacklogRepository` ports implemented by
  `OperationsMonitor` and `SqlAlchemyOperationsBacklogRepository`, wired into `PublicationWorker`,
  `SchedulerRuntime`, and `WorkerRuntime` for publication counters, FloodWait metrics, PostgreSQL
  backlog snapshots, and scheduler/worker liveness and readiness reporting.
- UUID-only Celery tasks (`tgcurator.publication` on `publication` and
  `tgcurator.archive_cleanup` on `maintenance`) plus `SchedulerRuntime` wake-up repair for
  unfinished or lease-expired publication intents and cleanup jobs.
- Nine-scenario stateful failure-injection suite (`tests/unit/test_m6_failure_injection.py`)
  verifying post-send pre-commit reconciliation, pre-enqueue crash recovery, Redis + wake-up loss,
  duplicate broker delivery, expired lease takeover, worker/scheduler restarts, partial supplement
  retry, FloodWait scheduling, and concurrent cleanup usage races plus post-delete crash replay.

## M6 execution boundary

Default worker runtimes never fabricate Telegram deliveries or archive blob deletions when no live
Telethon session store or archive storage backend is configured; unconfigured publication/cleanup
attempts transition through explicit error codes and durable retry/terminal states. API readiness
continues to check PostgreSQL only.

End-to-end automatic progression across ingestion -> PreScreen -> media archive -> visual
deduplication -> analysis DAG -> routing -> continuous watermark advancement belongs to M7.

## M6 migration

- Revision: e6d4a8b2c951
- Down revision: d7b3f9a1e6c2
- File: migrations/versions/e6d4a8b2c951_m6_publication_cleanup_operations.py
- Chronology date: September 16, 2026
- Extends `publication_intents` with rendered snapshots, deterministic `random_id` arrays,
  incremental `completed_parts`, in-flight step columns, leases, and retry metadata; extends
  `image_assets`, `video_assets`, and `video_frames` with `deleting`/`deleted` archive lifecycle
  and cleanup audit columns; adds `publish_attempts`, `publish_records`,
  `archive_cleanup_dry_runs`, `archive_cleanup_jobs`, and `archive_cleanup_items`.
- Updates immutability triggers so `publication_intents` identity fields remain immutable while
  execution lease/status columns progress safely, enforces append-only `publish_records` and
  `archive_cleanup_dry_runs`, and adds partial indexes for claimable/repairable publication and
  cleanup work.
- Offline upgrade to M6 and downgrade from M6 to M5 are covered by integration tests.

## Important implementation locations

- tgcurator/domain/publishing/lifecycle.py
- tgcurator/application/routing/rendering.py
- tgcurator/application/routing/service.py
- tgcurator/application/ports/publishing.py
- tgcurator/application/ports/cleanup.py
- tgcurator/application/ports/operations.py
- tgcurator/application/publishing/worker.py
- tgcurator/application/cleanup/service.py
- tgcurator/application/cleanup/worker.py
- tgcurator/infrastructure/database/publication_repository.py
- tgcurator/infrastructure/database/cleanup_repository.py
- tgcurator/infrastructure/database/operations_repository.py
- tgcurator/infrastructure/database/routing_repository.py
- tgcurator/infrastructure/database/analysis_repository.py
- tgcurator/infrastructure/database/models.py
- tgcurator/infrastructure/telegram/telethon_publisher.py
- tgcurator/infrastructure/observability/operations.py
- tgcurator/infrastructure/queue/celery_dispatcher.py
- apps/scheduler/runtime.py
- apps/worker/runtime.py
- apps/worker/celery_app.py
- migrations/versions/e6d4a8b2c951_m6_publication_cleanup_operations.py
- tests/unit/test_publishing.py
- tests/unit/test_routing_rendering.py
- tests/unit/test_publication_worker.py
- tests/unit/test_publication_repository.py
- tests/unit/test_telethon_publisher.py
- tests/unit/test_cleanup_contracts.py
- tests/unit/test_cleanup_worker.py
- tests/unit/test_cleanup_repository.py
- tests/unit/test_operations_observability.py
- tests/unit/test_m6_failure_injection.py
- tests/integration/test_alembic_offline_sql.py

## Final M6 validation

The M6 working tree passes all required local gates without a running PostgreSQL server, Redis
broker, Telegram identity/session, external inference provider, inference model, or external
archive object store:

- Ruff formatting check: 200 files already formatted.
- Ruff lint: all checks passed.
- Full pytest suite: 383 passed with two dependency deprecation warnings.
- Unit unittest discovery suite: 370 passed.
- Integration unittest discovery suite: 13 passed.
- compileall for apps, tgcurator, and tests.
- pip check: no broken requirements.
- Offline PostgreSQL Alembic upgrade through M6 (`e6d4a8b2c951`) and M6-to-M5 downgrade rendering.
- git diff --check and LF line-ending verification across all modified and untracked files.

The two pytest warnings are existing FastAPI/Starlette compatibility deprecations for the httpx
TestClient import path and the AnyIO BlockingPortal alias; they do not fail the suite.

## Next modules

- M7 (`module/m7-pipeline-orchestration`): end-to-end message pipeline orchestration, visual
  deduplication, continuous gap-free watermarks, full-queue wake-up repair, and
  `apps/telegram_ingestor` daemon runtime.
- M8 (`module/m8-admin-api`): FastAPI administration endpoints, Argon2id session auth + CSRF,
  sanitized audit events, isolated Prompt Test Bench, cursor-paginated message/causal inspection,
  and Prometheus `/metrics`.
- M9 (`module/m9-web-admin`): React + TypeScript + Vite single-administrator web console covering
  all 11 architectural workspaces.
- M10 (`module/m10-deployment-and-operations`): Docker Compose 10-service topology, graceful
  daemon lifecycles, backup/restore scripts, storage consistency scanner, and closed-loop E2E
  verification.
- Keep future work on separate module branches, single module commits, and separate English pull
  requests.
