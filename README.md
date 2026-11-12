# TG-VLM-Curator

TG-VLM-Curator is a domain-first Telegram ingestion, visual analysis, routing, and
publishing system. The implementation follows tg-vlm-curator-architecture.md.

## Current implementation status

M0–M9 implementations are merged. The remediation merges are M6 / #27
`99696fa2`, M7 / #28 `28c3e6fd`, M8 / #29 `b0e955f4`, and M9 / #30 `0007be1e`.
M10 / #31 delivers the production composition roots, visual-input preparation and
operational verification. Code head `8fc2737` passed all GitHub CI jobs
(python, web and deployment): PR run `43212224146`, push run `43212215720`.
The final documentation head must pass the same checks before exact-SHA merge;
the actual merge commit is recorded on PR #31.

The local suite passed 596 tests with 2 deprecation warnings and no errors,
failures or skips (281.56 seconds). This includes 31 native PostgreSQL/runtime
integration cases and the independent worker OS-process restart; 17 additional
migration tests render offline SQL. GitHub Linux/Python3.12 with SQLAlchemy2.1.8
passed 596 tests with 1 dependency warning (127.86 seconds). Native PostgreSQL
Chrome regression passed all 17 groups, frontend Vitest passed 24 tests, and the
recorded npm audit reported zero vulnerabilities. The full isolated ten-service
Docker exercise passed locally and in GitHub CI.

These are real local PostgreSQL16, Redis/Celery, Docker, HTTP and browser results.
Telegram and inference remain controlled external fixtures, not live credentials,
real channel deliveries, provider compatibility or model-quality acceptance. Full
finding-by-finding evidence and remaining gates are in
[the remediation delivery report](docs/reviews/2026-11-12-remediation-status.md).

M0 provides deterministic, framework-independent rules for message visual identity,
processing boundaries, immutable configuration snapshots, analysis DAG validation, Negative
Gate decisions, safe routing evaluation, and publication idempotency. It also defines
provider-neutral Telegram, storage, task-dispatch, and inference ports.

M1 adds:

- FastAPI liveness and PostgreSQL-only readiness endpoints;
- environment settings with direct or read-only-file master-key injection;
- JSON structured logging with recursive credential redaction and correlation fields;
- PostgreSQL SQLAlchemy metadata plus an initial Alembic migration for administrators,
  encrypted secrets, Telegram identities, source and destination channels, source profile
  versions, processing ranges, messages, and audits;
- one-time administrator bootstrap with Argon2id password hashing;
- AES-256-GCM secret encryption with key identifiers, per-record random nonces, and
  secret-type authenticated data;
- database constraints and triggers that enforce draft, published, and retired source profile
  version rules.

M2 adds:

- immutable message-ID execution windows using (from_message_id_exclusive,
  to_message_id_inclusive] bounds;
- FIXED boundary scheduling and quiet-period stabilization for LATEST ranges through an
  injected, provider-neutral latest-boundary source;
- PostgreSQL-owned execution leases, bounded retries, monotonic watermarks, and terminal
  failure metadata that excludes exception messages;
- reconstructable durable_wakeups plus a JSON-only Celery dispatcher whose broker payload
  contains only a RangeExecution UUID;
- scheduler and worker composition roots that keep PostgreSQL as business truth.

M3 adds:

- canonical, idempotent Telegram history and realtime ingestion with bounded media-group
  aggregation and reconnect reconciliation;
- independent monotonic observation and ingestion cursors, plus source edit/delete lifecycle
  handling;
- protected-content-aware Telegram downloads and immutable local archive publication;
- deterministic Pillow WebP image normalization with durable leased archive work;
- bounded FFprobe/FFmpeg video sampling with atomic frame publication and durable retries;
- PostgreSQL metadata, constraints, and wake-ups for image and video archive state.

Archive task payloads contain only asset UUIDs, and an unconfigured archive runtime returns an
explicit not-processed result rather than fabricating completion.

M4, completed on 2026-09-09, adds:

- immutable, publishable versions for labels, label sets, prompts, inference profiles, stages,
  and analysis pipelines, including condition-checked DAG nodes;
- dynamic structured-output schemas, stable target mapping, partial batch validation, immutable
  input manifests, semantic cache keys, and GLOBAL/MEDIA multi-label results;
- durable AnalysisRun, StageRun, InferenceCall, and ModelLabelAssignment persistence with leases,
  bounded retries, cache provenance, sanitized audit payloads, and first-cause Negative Gate
  blocking;
- a provider-neutral analysis worker and an OpenAI-compatible external HTTP adapter whose
  inference calls occur outside database transactions;
- the dedicated `analysis` queue and UUID-only `tgcurator.analysis` wake-up task.

No inference model or provider is bundled or deployed. A real external provider and its secret
configuration are required for inference. The runtime resolves published inference profiles and encrypted vault references;
missing provider configuration records an explicit failure and durable retry or terminal-failure
state instead of fabricating a successful result. API readiness checks
PostgreSQL only, so provider unavailability remains a degraded business capability rather than
making the administration API unavailable.

M5, completed on 2026-09-09, adds:

- append-only manual `set`/`clear` label events and append-only review-status transition
  events, with `review_status` retained only as explicit workflow/filter state rather than an
  implicit routing gate;
- separate model, manual, and effective label namespaces for GLOBAL and MEDIA targets: the latest
  manual event by `(created_at, event_id)` wins, a set overrides the matching model value, and a
  clear removes the override and falls back to the model value when one exists;
- publishable routing-policy and rendering-template versions with draft-only rules/actions, plus a
  non-executable routing DSL for activation and score predicates, `any_media`/`none_media`,
  deterministic `(-priority, rule_id)` ordering, `stop_on_match`, and conservative unknown
  propagation;
- canonical PostgreSQL routing-fact snapshots and SHA-256 hashes, write-free dry runs, duplicate
  request-ID reuse, and explicit reroutes that use new request IDs while retaining prior history;
- atomic persistence of each formal routing evaluation with zero-to-many durable
  `PublicationIntent(status="pending")` records and stable publication identity fields.

Routing performs no LLM/provider inference or Telegram calls. Dry runs perform zero database writes;
formal runs persist the immutable routing evaluation, rendered template snapshots, pending
publication intents, and reconstructable UUID-only `publication` wake-ups in a single PostgreSQL
transaction.

M6, completed on 2026-11-12, adds:

- restricted dictionary-path template interpolation (no loops, attribute access, or arbitrary code
  execution) with canonical rendered-context snapshots and SHA-256 hashes frozen during routing;
- leased PostgreSQL `PublicationIntent` execution (`SKIP LOCKED`), auditable `PublishAttempt`
  history, deterministic 63-bit MTProto `random_id` persistence before external sends, incremental
  `PublishRecord` commits (`partial` recovery for `native_forward_with_supplement`), and remote
  reconciliation of unresolved in-flight steps before retries;
- all four publication modes (`native_forward_with_supplement`, `copy_with_caption`,
  `forward_only`, `metadata_only`), Telethon error translation, non-blocking `FloodWait`
  `next_retry_at` scheduling, and explicit `fail` / `metadata_only` / `skip` protected-content
  fallbacks;
- two-step manual archive cleanup (`Dry-Run` -> hash-verified `Confirm` -> leased `maintenance`
  worker) across 15 composable filter criteria, atomic `AuditEvent` logging, row-locked active
  Analysis/Publication usage revalidation, and blob-only deletion that preserves messages, hashes,
  visual fingerprints, labels, routing, and publication history;
- a framework-neutral `OperationsTelemetry` port, PostgreSQL backlog observation, thread-safe
  in-process publication/FloodWait/backlog metrics, and scheduler/worker liveness and readiness
  snapshots, plus UUID-only `tgcurator.publication` and `tgcurator.archive_cleanup` wake-up tasks
  and Scheduler wake-up repair.

Default worker factories resolve encrypted Telegram identities, published configuration and local
archive storage. Missing or invalid external configuration remains an explicit error or durable
retry/failed state, never a fabricated remote delivery. See
[runtime configuration and acceptance boundaries](docs/operations/runtime.md).

M7, completed on 2026-11-12, adds:

- end-to-end `MessagePipelineCoordinator` orchestration across `RangeExecution` handoff
  (`awaiting_pipeline`), pre-download `batch_messages` PreScreen stages, first-cause Negative Gate
  media-download blocking (while preserving formal Routing), post-PreScreen media archive wake-ups,
  visual deduplication, conditional DAG progression, formal `RoutingService.evaluate`, and
  publication settlement;
- order-independent message visual fingerprints and exact unordered pHash multiset pairing across
  image and video-cover pHashes (`duplicate_matches` and `canonical_message_id`), supporting both
  `skip` and `reclassify_with_text` (with complete prepared-input semantic cache validation rather than unsafe donor copying);
- gap-free monotonic continuous `processing_watermark` advancement on `RangeExecution` and
  `ProcessingRange`, plus full-queue Scheduler wake-up repair (`range_execution`, `image_archive`,
  `video_archive`, `analysis`, `publication`, and `maintenance`) and the single-instance
  `apps/telegram_ingestor` composition root.

M8, completed on 2026-11-12, adds:

- single-administrator Argon2id session authentication (`HttpOnly`, `Secure`, `SameSite=Lax`
  cookies), HMAC-SHA256 CSRF protection on state-mutating endpoints, durable `admin_sessions`, and
  recursively sanitized `AuditEvent` logging;
- write-only AES-256-GCM secret creation and rotation endpoints that never echo plaintext, Telegram
  identity management with `TelegramCapabilityResolver`, source and destination channel endpoints,
  and `ProcessingRange` lifecycle (`FIXED` / `LATEST`, pause, resume), audited message skip, and
  exact step-level retry controls;
- full `DRAFT -> PUBLISHED -> ARCHIVED` (and `RETIRED` for source channel profiles) endpoints across
  all ten versioned configuration families, including `media_processing_profiles` and
  `media_processing_profile_versions` (`a8b2d4e1f305`), plus isolated `run_mode="test"` Prompt Test
  Bench execution (`POST /api/v1/analysis/test-bench`);
- cursor-paginated `Messages` search across Section 37 filters, full causal-chain `Message Detail`
  inspection, append-only manual review/labeling, explicit reanalysis, dry-run/formal reroute
  comparison, two-step archive cleanup (`Dry-Run` -> hash-verified `Confirm`), operations/audit
  queries, and Prometheus text exposition (`GET /metrics`).

M9, completed on 2026-11-12, adds:

- the single-administrator React + TypeScript + Vite SPA under `web/` with cookie-backed session
  authentication, automatic `X-CSRF-Token` attachment on mutating requests, and responsive
  accessibility-hardened tables, forms, tabs, and native `<dialog>` confirmations;
- `Dashboard`, `Source Channels`, `Processing Ranges` (`FIXED`/`LATEST` steady-state watermarks),
  and `Telegram Identities` (write-only `EncryptedSecret` storage and rotation that immediately
  scrub plaintext and display metadata only);
- cursor-paginated `Messages` across all 16 Section 37 filters, the 6-stage `Message Detail` causal
  chain inspector, and `Review` workflows (append-only manual label overrides preserving raw
  `ModelLabelAssignment`s, explicit reanalysis, and incremental dry-run/formal re-route diffs);
- `Analysis Configuration` enforcing `DRAFT -> PUBLISHED -> ARCHIVED` immutability plus interactive
  `Prompt Test Bench` (`run_mode="test"`), `Routing` rule/template authoring and historical dry-run
  evaluation, mandatory two-step `Archive Management` (`Dry-Run` before `Confirm`), `Operations`
  step-level retries, and sanitized `Audit Log` browsing.

M10, implemented with native local and GitHub CI acceptance, adds:

- production `docker/backend.Dockerfile`, `docker/web.Dockerfile`, `docker/nginx.conf`,
  `.dockerignore`, `.env.example`, and `docker-compose.yml` orchestrating the ten core services
  (`postgres`, `redis`, `api`, `web`, `telegram-ingestor`, `scheduler`, `worker-media`,
  `worker-analysis`, `worker-publish`, `worker-maintenance`), four persistent volumes
  (`postgres-data`, `redis-data`, `archive-data`, `temp-media`), and read-only file secret
  `app_master_key`, with strictly zero bundled LLM/VLM services;
- graceful `SIGTERM` daemon entrypoints (`python -m apps.scheduler`,
  `python -m apps.telegram_ingestor`, `python -m apps.worker --role ...`), active `ffprobe`/`ffmpeg`
  child-process termination on worker shutdown, and `tgcurator-healthcheck` (`apps/healthcheck.py`)
  enforcing PostgreSQL-only readiness on `api`/`ingestor` and PostgreSQL + Redis readiness on
  `scheduler`/`worker`;
- operator backup and restore scripts (`scripts/backup-postgres.sh`, `scripts/backup-media.sh`,
  `scripts/restore-postgres.sh`) plus the `StorageConsistencyScanner` and
  `tgcurator-check-consistency` CLI (`apps/worker/consistency_cli.py`) verifying `READY` archive
  blobs and reporting corrupted or orphan files;
- end-to-end closed-loop pipeline, recovery, cleanup, consistency, and Admin API security
  verification (`tests/e2e/test_closed_loop_pipeline.py`) driven by deterministic
  `FakeTelegramAdapter` and `FakeInferenceProvider` harnesses.

See tg-vlm-curator-module-implementation.md for module boundaries and acceptance criteria.

## Dependency direction

    apps / workers / infrastructure --> application --> domain --> shared

The domain package does not import FastAPI, SQLAlchemy, Celery, Telethon, httpx, or storage
clients. PostgreSQL is the business source of truth; queue messages remain
reconstructable wake-ups rather than business state.

## Local development

Create a Python 3.12+ virtual environment and install runtime plus development dependencies:

    py -3.12 -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[runtime,dev]"

Configure PostgreSQL through an asyncpg URL:

    $env:TGCURATOR_DATABASE_URL = "postgresql+asyncpg://curator:secret@localhost:5432/tgcurator"

Configure the Celery broker used only for reconstructable wake-up delivery:

    $env:TGCURATOR_CELERY_BROKER_URL = "redis://localhost:6379/0"

Redis/Celery is not business state. Clearing the broker does not remove pending work because
durable_wakeups and range_executions remain in PostgreSQL.

For environments that use encrypted secrets, inject one base64-encoded 32-byte master key
either directly or through a host-mounted read-only file:

    $env:TGCURATOR_APP_MASTER_KEY = "<base64-encoded-32-byte-key>"

or:

    $env:TGCURATOR_APP_MASTER_KEY_FILE = "C:\run\secrets\tgcurator-master-key"

Do not store the master key in PostgreSQL, source control, command output, or application logs.
Production settings require one of these two master-key sources.

Apply the schema, initialize the sole administrator, and run the API:

    .venv\Scripts\python.exe -m alembic upgrade head
    .venv\Scripts\python.exe -m apps.bootstrap_admin --username curator
    .venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000

Health endpoints:

- GET /health/live checks the API process;
- GET /health/ready checks PostgreSQL and returns HTTP 503 when unavailable.

## Docker Compose deployment & operations

Production serves HTTPS through nginx on port 443. Provide an operator-managed trusted
certificate/full chain and matching private key in `.env`; both are mounted read-only.
HTTP port 80 redirects to HTTPS. Production Secure cookies remain enabled. The database,
Redis and API are internal-only; the API does not trust caller-supplied proxy headers.

Follow [HTTPS deployment and recovery](docs/operations/deployment.md) for file permissions,
certificate renewal, staged migrations/bootstrap, explicit project selection, maintenance
windows, backup verification and isolated recovery. Preserve the application master key
independently in an offline secret vault. Never include it in database/media backup sets.

With an explicit `COMPOSE_PROJECT_NAME` and `COMPOSE_ENV_FILE`, and all application writers
stopped for the complete backup window:

```bash
bash scripts/backup-postgres.sh
bash scripts/backup-media.sh
# Restore only into an explicitly prepared, stopped recovery project with an empty archive volume:
bash scripts/restore-postgres.sh --confirm-restore <backup.dump> --media <archive.tar.gz>
```

The scripts use the Compose PostgreSQL16 service and the named archive volume, not a host
PostgreSQL client or a host `/var/lib` directory. Restore verifies both SHA256 checksums,
fails on SQL errors, and requires a post-restore media consistency report. Resolve all report
discrepancies and verify encrypted secrets with the independently recovered key before cutover.

Run the isolated real-container TLS login and backup/restore exercise:

```bash
python scripts/check-deployment.py
```

It uses disposable loopback ports, temporary secrets and a local-only self-signed certificate
trusted only by its test client. It never establishes production certificate trust or contacts
Telegram/inference services, and cleans up only the two projects it creates. The completed
local exercise additionally verifies all ten services starting/restarting, application SIGTERM,
real FFprobe/frame extraction and active FFmpeg cancellation, independent-key recovery,
wrong-key rejection, and failing/clean archive consistency scans.

## Validation and CI

```bash
python -m pip install -e '.[runtime,dev]'
python -m ruff format --check .
python -m ruff check .
python -m pytest -ra
python -m compileall -q apps tgcurator migrations tests
python -m pip check
cd web && npm ci && npm run typecheck && npm test && npm run build
```

The CI workflow provisions PostgreSQL16 and Redis7. Real PostgreSQL regression tests opt in
through `TGCURATOR_TEST_POSTGRES_ADMIN_URL` (maintenance database `postgres`) or
`TGCURATOR_TEST_SERVICES_FILE`; without either they skip. Existing offline SQL and fake-service
tests remain useful but are not evidence of real persistence/queue correctness. Redis runtime
tests use `TGCURATOR_TEST_REDIS_URL`. Branches without `web/package.json` skip frontend checks.
The deployment job exercises real nginx TLS, login cookies and isolated backup/recovery.

The final local result is recorded in `.git/fix-2026-11-12/main/final-full.txt` and
`final-full.xml`; deployment evidence is `main/final-deployment.txt`. These local
artifacts are not distributed by ordinary Git commits. Supply service credentials
through the approved local/CI mechanism without printing or committing their contents.
The workflow definition is not proof of a successful workflow run: verify all required
jobs against the final PR head before merging. Live Telegram/provider and production
TLS/restore acceptance require separately authorized environments.
