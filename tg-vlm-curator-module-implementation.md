# TG-VLM-Curator module implementation plan

Status date: 2026-11-12

Source specification: `tg-vlm-curator-architecture.md`.

This document defines repository-sized implementation modules. Each completed module is
validated locally and delivered as one branch, one commit, and one pull request. A module
may contain multiple cooperating packages when they are required by its acceptance criteria.

## Status

| Module | Scope | Status |
| --- | --- | --- |
| M0 | Domain foundation and framework-neutral ports | Complete |
| M1 | API, environment settings, security, and initial PostgreSQL schema | Complete |
| M2 | Durable ProcessingRange and RangeExecution scheduling | Complete |
| M3 | Telegram ingestion and media archive workflows | Complete |
| M4 | Versioned analysis engine and provider adapters | Complete (2026-09-09) |
| M5 | Human review and routing workflows | Complete (2026-09-09) |
| M6 | Publication, archive cleanup, and operations | Complete (2026-11-12) |
| M7 | End-to-end pipeline orchestration, visual deduplication, and continuous watermarks | Complete (2026-11-12) |
| M8 | Admin HTTP API, authentication, Prompt Test Bench, and Prometheus metrics | Complete (2026-11-12) |
| M9 | React + TypeScript + Vite web administration console | Complete (2026-11-12) |
| M10 | Docker Compose orchestration, daemon lifecycles, backup/consistency tooling, and E2E verification | Complete (2026-11-12) |

## Dependency rule

    apps / workers / infrastructure --> application --> domain --> shared

- `domain` contains deterministic rules, immutable value objects, state transitions, and
  calculations. It must not import FastAPI, SQLAlchemy, Celery, Telethon, httpx, or storage
  clients.
- `application` orchestrates use cases and exposes framework-neutral ports.
- `infrastructure` implements PostgreSQL, Redis/Celery, Telegram, HTTP inference, storage,
  security, and observability adapters.
- `apps` contains composition roots only; endpoint and worker functions do not own domain
  policy.

## M0 - Domain foundation (complete)

M0 establishes:

- repository package and test layout;
- message visual identity independent of text, source, and media order while preserving the
  visual pHash multiset;
- FIXED range boundaries and quiet-period stabilization for LATEST boundaries;
- monotonic execution watermarks and finite frozen windows;
- immutable canonical JSON configuration snapshots;
- stable pipeline DAG validation and Negative Gate decisions;
- a constrained JSON Routing DSL with deterministic priority and `stop_on_match` behavior;
- publication modes and stable publication idempotency keys;
- framework-neutral ports, including an undeployed `InferenceProvider` boundary.

Routing evaluation treats malformed conditions, missing facts, incompatible fact types, and
runtime evaluation errors as non-matches. A `not` node cannot turn an unknown or invalid fact
into a match.

Acceptance commands:

    .venv\Scripts\ruff.exe format --check .
    .venv\Scripts\ruff.exe check .
    .venv\Scripts\python.exe -m pytest
    .venv\Scripts\python.exe -m unittest discover -s tests/unit -v
    .venv\Scripts\python.exe -m compileall -q apps tgcurator tests
    .venv\Scripts\python.exe -m pip check

M0 requires no network, PostgreSQL, Redis, Telegram identity/session, or inference model.

## M1 - API and versioned configuration skeleton (complete)

M1 adds, as one module:

- FastAPI liveness/readiness endpoints and environment settings;
- JSON structured logging with recursive secret redaction;
- PostgreSQL SQLAlchemy metadata and Alembic migrations for initial identities, channels,
  configuration versions, ranges, messages, encrypted secrets, administrators, and audits;
- one-time Argon2id administrator bootstrap;
- AES-256-GCM secret encryption with key identifiers and per-record random nonces;
- database constraints for draft/published/retired configuration versions.

M1 readiness checks PostgreSQL. Missing inference providers are a degraded business
capability, not a reason to make the Web API unavailable.

Acceptance uses the same formatting, lint, pytest, unittest, compileall, and pip checks as M0,
plus offline PostgreSQL Alembic upgrade and downgrade rendering. It requires no Redis,
Telegram credentials, running PostgreSQL server, or inference model.

## M2 - Durable processing scheduling (complete)

M2 adds, as one module:

- immutable Telegram message-ID windows with (from_message_id_exclusive,
  to_message_id_inclusive] semantics and an initial lower bound of
  resolved_start_message_id - 1;
- direct FIXED scheduling and LATEST scheduling only after an injected boundary source confirms
  that the newest real message has remained quiet for steady_after_seconds;
- atomic PostgreSQL persistence of each frozen RangeExecution and its reconstructable durable
  wake-up, with row-lock revalidation of range, source-channel, profile-version, and watermark
  state;
- one active execution per ProcessingRange, short compare-by-token leases, bounded retry state,
  safe error codes/types, and monotonic execution/range watermarks;
- at-least-once Celery/Redis wake-up delivery containing only the immutable execution UUID;
- scheduler and worker composition roots designed for M3 to attach Telegram ingestion to a
  claimed execution without fabricating business completion.

PostgreSQL is the source of business truth. durable_wakeups are repairable broker signals,
and Redis loss cannot delete RangeExecution state. FIXED windows never use quiet-period logic;
LATEST windows never freeze from a synthetic boundary.

Acceptance uses the M0/M1 formatting, lint, pytest, unittest, compileall, pip, and offline
Alembic checks. Repository and runtime tests cover atomic freezing, stale-snapshot rejection,
partial unique execution protection, wake-up repair, lease expiry, retries, terminal failures,
and monotonic completion. No running PostgreSQL, Redis, Telegram session, or inference model is
required for the acceptance suite.

## M3 - Telegram ingestion and archive workflows (complete)

M3 adds idempotent history/update ingestion through canonical Telegram DTOs, reconnect
reconciliation with independent monotonic cursors, bounded media-group normalization, and source
message edit/delete lifecycle handling. Image and video assets use protected-content-respecting
Telegram downloads, atomic archive publication, short PostgreSQL leases, bounded retries, and
terminal failure states. Pillow normalizes images to deterministic WebP artifacts; FFprobe and
FFmpeg produce bounded video metadata and representative WebP frames without shell execution.
Durable wake-ups contain only asset UUIDs, and unconfigured media runtimes never fabricate
completion.

Acceptance uses the M0/M1 formatting, lint, pytest, unittest, compileall, pip, and offline
Alembic checks. Repository, worker, adapter, normalization, lifecycle, storage, and sampling tests
require no running PostgreSQL, Redis, Telegram session, FFmpeg installation, or inference model.

## M4 - Analysis engine (complete 2026-09-09)

M4 adds immutable draft/published/retired versions for labels, label sets, prompts, inference
profiles, stages, and condition-checked pipeline DAGs. It persists immutable input manifests,
dynamic structured-output schema snapshots, AnalysisRun and per-target StageRun state,
InferenceCall audits, cache provenance, and formal ModelLabelAssignments. GLOBAL and MEDIA stages
support stable target mapping, partial batch commits, independently retryable invalid or missing
targets, semantic cache reuse, test-run isolation, and first-cause Negative Gate blocking.

The application layer provides a provider-neutral orchestrator and durable leased worker. The
infrastructure layer provides the initial OpenAI-compatible external HTTP adapter, PostgreSQL
repository, and UUID-only Celery `analysis` wake-up. Provider I/O occurs outside database
transactions, retries are bounded, and provider/error audit data is sanitized before persistence.

No inference model or provider is bundled or deployed. A real external provider configuration and
secret are required. The default worker runtime intentionally has no provider; each attempted call
still persists its pending InferenceCall before invocation, then records
`InferenceProviderNotConfigured` and moves affected StageRuns into explicit durable retry or
terminal-failure state. Successful inference is never fabricated.

Acceptance uses the M0/M1 formatting, lint, pytest, unittest, compileall, pip, and offline Alembic
checks. Domain, orchestrator, worker, repository, provider-adapter, schema, migration, Celery, and
runtime tests require no running PostgreSQL, Redis, Telegram session, external provider, or
inference model.

## M5 - Human review and routing (complete 2026-09-09)

M5 adds append-only manual label assignment events with explicit `set` and `clear` operations
for GLOBAL message targets and MEDIA image/video targets. Model assignments remain immutable,
manual edits never mutate an AnalysisRun, and current routing facts expose independent `model`,
`manual`, and `effective` namespaces. Events are resolved by `(created_at, event_id)`: the
latest manual set overrides the corresponding model assignment, while the latest clear removes the
manual override and falls back to the model value when one exists.

Message review uses explicit `unreviewed`, `in_review`, `reviewed`, and `needs_attention`
workflow state plus append-only transition history. `review_status` is available for filtering
and as an explicit routing fact, but routing does not treat it as an implicit approval gate.

Versioned routing policies, rules, actions, rendering templates, and template versions are stored
in PostgreSQL with draft-only editing and published-version immutability. The constrained routing
DSL evaluates GLOBAL/MEDIA labels across model/manual/effective namespaces, activation state,
scores, `any_media` and `none_media` predicates, ordinary message/source/media facts, and
unknown propagation that cannot be inverted into a match. Enabled rules execute in stable
`(-priority, rule_id)` order, accumulate zero-to-many actions, and stop only after a matched
`stop_on_match` rule.

Each routing request loads one PostgreSQL snapshot, resolves labels, canonicalizes and hashes the
facts, and evaluates without external side effects. Dry runs return the complete decision while
writing nothing. A duplicate formal request ID reuses its original evaluation and intents; an
explicit reroute uses a new request ID and preserves the prior history. Formal persistence is one
atomic `RoutingEvaluation + PublicationIntents[]` transaction, and each produced intent starts
as `pending` with immutable routing/publication identity fields and a stable business
idempotency key.

M5 calls no LLM, inference provider, Telegram client, Celery dispatcher, or publishing worker. It
creates durable pending PublicationIntent records only. Publication leases and attempts, retries,
FloodWait handling, Telegram calls, partial recovery, and reconciliation remain M6.

Acceptance uses the M0/M1 formatting, lint, pytest, unittest, compileall, pip, and offline Alembic
checks. Domain, service, repository, schema, and migration tests require no running PostgreSQL,
Redis, Telegram session, external inference provider, or inference model.

## M6 - Publication and operations (complete 2026-11-12)

M6 closes the backend publication, archive-cleanup, and runtime-operations lifecycle while
preserving strict domain/application boundaries. A publish worker claims one PostgreSQL
PublicationIntent through a short compare-by-token lease, freezes the source/destination/template
facts needed for the attempt, and persists stable 63-bit MTProto `random_id` values plus in-flight
step state before any Telegram request. Every claim creates an auditable PublishAttempt;
successful remote message IDs are recorded incrementally in durable PublishRecords so
`native_forward_with_supplement` retries only a missing forward or supplement, and an explicit
lease release transitions unfinished multi-part intents cleanly to `partial` or `pending`.

All four publication modes are implemented through a framework-neutral Telegram publisher port:
`native_forward_with_supplement`, `copy_with_caption`, `forward_only`, and `metadata_only`. The
Telethon adapter translates FloodWait, permission, expired-session, protected-content, and
unavailable-media failures into typed application exceptions. FloodWait and ordinary transient
failures persist an exact or bounded `next_retry_at`; workers never sleep through platform waits.
Protected content uses the routing action's explicit `fail`, `metadata_only`, or `skip` fallback
rather than bypassing Telegram restrictions.

The publisher treats PostgreSQL as business truth and Celery as a reconstructable wake-up only.
Formal routing inserts rendered template snapshots and a UUID-only `publication` wake-up in the
same transaction as each intent. Scheduler repair rediscovers pending intents, due retries,
`partial` work, and expired leases after broker loss or process restart. Reconciliation examines
unresolved in-flight sends at the remote boundary before a retry, allowing a Telegram success
followed by a failed database commit to be recovered without resending the completed part.

Operations add a framework-neutral `OperationsTelemetry` port, PostgreSQL backlog observation,
thread-safe in-process publication/FloodWait/backlog metrics, and explicit worker/scheduler
liveness and readiness snapshots wired into `PublicationWorker`, `SchedulerRuntime`, and
`WorkerRuntime`. Archive cleanup is a durable maintenance workflow: a dry run stores a canonical
15-dimension filter snapshot, SHA-256 filter hash, estimated asset/message/byte totals, and an
atomic `AuditEvent`; confirmation revalidates the filter hash, records a confirmation
`AuditEvent`, and creates a leased cleanup job with a UUID-only `maintenance` wake-up; execution
locks ready assets and revalidates active Analysis/Publication usage before deleting only archive
blobs outside transactions and marking asset metadata `deleted` with deletion audit fields.
Messages, hashes, visual fingerprints, Analysis, labels, routing, and publication history remain
intact.

Acceptance extends the earlier formatting, lint, pytest, unittest, compileall, pip, schema, and
offline Alembic gates. Stateful failure-injection tests cover a send succeeding before the result
commit, an intent committed before broker enqueue, Redis and wake-up loss, duplicate consumption,
expired publication and cleanup leases, worker/scheduler restart, partial publication recovery,
FloodWait scheduling, and cleanup usage races plus post-delete crash replay. No running
PostgreSQL, Redis, Telegram session, external inference provider, or inference model is required
by the local acceptance suite.

## M7 - End-to-end pipeline orchestration, visual deduplication, and continuous watermarks (complete 2026-11-12)

M7 connects ingestion, archive normalization, versioned analysis, visual deduplication, formal
routing, and publication settlement into one continuous, crash-recoverable message lifecycle:

- pre-download `batch_messages` text PreScreen stages (`load_stage_snapshot` co-leasing eligible
  sibling targets up to `max_batch_size` with `FOR UPDATE SKIP LOCKED`) and first-cause Negative
  Gate blocking before media downloads are scheduled, while still allowing formal Routing to
  evaluate blocked messages;
- automatic image/video archive wake-up generation for unblocked messages, followed by
  order-independent visual fingerprint and unordered pHash multiset pairing (`duplicate_matches`
  and `canonical_message_id`) supporting `skip` and `reclassify_with_text` duplicate policies,
  with version-matched canonical visual stage cache reuse when `reclassify_with_text` runs;
- topological conditional DAG progression across `depends_on` and `run_if` predicates, creating
  downstream `StageRun` rows and UUID-only `analysis` wake-ups until the `AnalysisRun` reaches a
  terminal state, then triggering formal `RoutingService.evaluate` and waiting for produced
  `PublicationIntent`s to settle;
- gap-free monotonic `processing_watermark` advancement on `RangeExecution` (`awaiting_pipeline` ->
  `completed`) and `ProcessingRange` only across contiguous prefix messages that have reached valid
  terminal lifecycle states (completed, Negative Gate blocked, skipped by duplicate policy, no
  eligible stages, unsupported, or audited administrator skip);
- scheduler wake-up repair coverage across `range_execution`, `image_archive`, `video_archive`,
  `analysis`, `publication`, and `maintenance` queues, plus the single-instance
  `apps/telegram_ingestor` composition root for realtime updates, bounded media-group flushing,
  reconnect reconciliation, source edit/delete lifecycle tracking, and graceful cursor persistence.

Acceptance extends the formatting, lint, pytest, unittest, compileall, pip, schema, and offline
Alembic gates across visual deduplication, continuous watermarks, conditional DAG progression,
batch co-leasing, pipeline coordination, and `telegram_ingestor` runtime tests without requiring a
running PostgreSQL server, Redis broker, Telegram session, external provider, or inference model.

## M8 - Admin HTTP API, authentication, Prompt Test Bench, and Prometheus metrics (complete 2026-11-12)

M8 exposes the complete FastAPI administration surface over the domain and application services:

- single-administrator Argon2id session authentication (`HttpOnly`, `Secure`, `SameSite=Lax`
  cookies), HMAC-SHA256 CSRF validation on state-mutating endpoints, durable `admin_sessions`
  storage, and recursively sanitized `AuditEvent` persistence across administrative mutations;
- write-only encrypted secret endpoints (`POST /api/v1/secrets`, `PUT /api/v1/secrets/{id}`) that
  never echo plaintext, Telegram identity management with `TelegramCapabilityResolver`,
  source/destination channel endpoints, `media_processing_profiles` + `source_channel_profiles`
  endpoints, and `ProcessingRange` lifecycle (`FIXED` / `LATEST`, pause, resume) plus audited
  message skip (with continuous `processing_watermark` settlement) and step-level retry controls;
- full `DRAFT -> PUBLISHED -> ARCHIVED` (and `RETIRED` for source channel profiles) endpoints across
  all ten configuration families (`label-definitions`, `label-sets`, `prompt-templates`,
  `inference-profiles`, `analysis-stages`, `analysis-pipelines`, `media-processing-profiles`,
  `source-channel-profiles`, `rendering-templates`, and `routing-policies`), plus isolated
  `run_mode="test"` Prompt Test Bench execution (`POST /api/v1/analysis/test-bench`) that never
  mutates formal labels, Negative Gate blocks, watermarks, or routing;
- cursor-paginated `Messages` search across all Section 37 filters, full causal-chain
  `Message Detail` inspection, append-only manual review/labeling, explicit reanalysis, dry-run
  comparison and formal rerouting, two-step archive cleanup (`Dry-Run` -> hash-verified `Confirm`),
  Prometheus text exposition (`GET /metrics`), and dashboard/operations/audit query APIs.

Acceptance extends the formatting, lint, pytest, unittest, compileall, pip, dependency-boundary,
schema, and offline Alembic (`a8b2d4e1f305`) gates across session/CSRF security, write-only secret
isolation, configuration immutability, DAG/DSL validation, Prompt Test Bench side-effect isolation,
cursor pagination, review/reroute/skip/cleanup workflows, and `/metrics` exposition.

## M9 - React + TypeScript + Vite web administration console (complete 2026-11-12)

M9 implements the single-administrator React + TypeScript + Vite SPA under `web/` covering all
architectural workspaces and administration workflows:

- `LoginView` and `AdminApiClient` (`web/src/api/client.ts`) with `credentials: "include"` session
  cookie propagation, automatic `X-CSRF-Token` injection on mutating requests, autofill-compliant
  username/password controls, accessible password reveal toggle, and explicit logout;
- `Dashboard`, `Source Channels`, `Processing Ranges` (`FIXED` / `LATEST` steady-state quiet window
  watermarks, pause/resume, `RangeExecution` drill-down), and `Telegram Identities` (write-only
  `EncryptedSecret` creation and rotation that immediately scrub plaintext and display `"Configured"`
  metadata only, plus `TelegramCapabilityResolver` simulation);
- cursor-paginated `Messages` browser across all 16 Section 37 filters, full 6-stage causal-chain
  `Message Detail` inspector (Telegram parts -> normalized SHA-256/pHash assets & `DuplicateMatch`
  -> `AnalysisRun`/`StageRun`/`InputManifest`/`InferenceCall` -> side-by-side `Model`, `Manual`, and
  `Effective` label namespaces -> `RoutingEvaluation`/`PublicationIntent`/`PublishRecord` ->
  `AuditEvent` & audited watermark skip), and `Review` workspace (append-only manual label `set` /
  `clear`, review status transitions, explicit reanalysis, and incremental re-route destination
  diffs);
- `Analysis Configuration` across all eight analysis/profile families enforcing `DRAFT -> PUBLISHED
  -> ARCHIVED` immutability (read-only published versions + clone-to-draft) plus interactive
  side-effect-free `Prompt Test Bench` (`run_mode="test"`), `Routing` workspace (`DestinationChannel`
  management, `RenderingTemplate` & `RoutingPolicy` versioning, and historical dry-run evaluator),
  two-step `Archive Management` (`Dry-Run` preview required before native `<dialog>` confirmation,
  automatically invalidated on any filter mutation), `Operations` targeted step-level retry, and
  redacted `Audit Log` browser.

Acceptance extends the backend gates with strict TypeScript typechecking (`npm run typecheck`),
Vitest + Testing Library component/workflow verification (`npm test`), and production Vite bundle
compilation (`npm run build`).

## M10 - Docker Compose orchestration, daemon lifecycles, backup/consistency tooling, and E2E verification (complete 2026-11-12)

M10 delivers the production container topology, operational maintenance utilities, and closed-loop
system verification without bundling any inference model service:

- multi-stage `docker/backend.Dockerfile` (non-root Python 3.12 + FFmpeg runtime), `docker/web.Dockerfile`
  (Node 22 Vite build + Nginx static SPA serving with `/api/`, `/health/`, and `/metrics` reverse
  proxying via `docker/nginx.conf`), `.dockerignore`, `.env.example`, and `docker-compose.yml`
  orchestrating the exact ten core services (`postgres`, `redis`, `api`, `web`, `telegram-ingestor`,
  `scheduler`, `worker-media`, `worker-analysis`, `worker-publish`, `worker-maintenance`), four
  persistent volumes (`postgres-data`, `redis-data`, `archive-data`, `temp-media`), and read-only
  file secret `app_master_key`;
- graceful `SIGTERM` handling across `apps/scheduler/__main__.py`,
  `apps/telegram_ingestor/__main__.py` (bounded media-group flush + `shutdown()`),
  `apps/worker/__main__.py` (`--role media|analysis|publish|maintenance|all` plus Celery
  `worker_shutting_down` hook terminating active `ffprobe`/`ffmpeg` child processes in
  `FFmpegVideoFrameExtractor`), and service-appropriate readiness probes in `apps/healthcheck.py`
  (`tgcurator-healthcheck`: PostgreSQL-only for `api`/`ingestor`, PostgreSQL + Redis for
  `scheduler`/`worker`);
- operator backup and restore scripts (`scripts/backup-postgres.sh`, `scripts/backup-media.sh`,
  `scripts/restore-postgres.sh`) with UTC timestamp + SHA-256 manifests and independent
  `APP_MASTER_KEY` reminders, plus the framework-independent `StorageConsistencyScanner`
  (`tgcurator/domain/archive/consistency.py`, `tgcurator/application/maintenance/consistency.py`,
  `tgcurator/infrastructure/database/consistency_repository.py`, and `apps/worker/consistency_cli.py`
  / `tgcurator-check-consistency`) auditing `READY` database blobs against local archive files;
- end-to-end closed-loop integration, resilience, and API security invariant verification in
  `tests/e2e/test_closed_loop_pipeline.py` driven by deterministic `FakeTelegramAdapter` and
  `FakeInferenceProvider` harnesses alongside unit tests in `tests/unit/test_consistency_scanner.py`
  and `tests/unit/test_deployment_topology.py`.

Acceptance passes all formatting, lint, pytest, unit/integration/e2e unittest discovery, compileall,
pip, dependency-boundary, frontend (`typecheck`, `test`, `build`), and git whitespace gates.
