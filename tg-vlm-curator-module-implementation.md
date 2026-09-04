# TG VLM Curator Modular Implementation Plan

Status: M0 completed; M1 implemented; M2 durable range-scheduling foundation implemented; M3 Parts 1-6 typed idempotent Telegram normalization, local archive storage, image normalization/fingerprinting, generic range-history execution orchestration, a real-time media-group aggregation buffer, and a typed bounded Telethon history reader implemented. The repository is verified with unit tests plus offline Alembic PostgreSQL SQL rendering on 2026-09-04. Live PostgreSQL adapter verification remains pending.

Source specification: tg-vlm-curator-architecture.md.

Purpose: turn the architecture specification into a testable modular monorepo. Start with side-effect-free domain rules, then add PostgreSQL, a queue, Telegram, inference providers, and the web console in separate iterations.

## 1. Implementation principles

### 1.1 Dependency direction

The repository is a modular monolith using ports and adapters.

    apps / workers / infrastructure --> application --> domain --> shared

- domain contains rules, value objects, state machines, and deterministic calculations. It must not import FastAPI, SQLAlchemy, Celery, Telethon, httpx, or a filesystem client.
- application orchestrates use cases, transactions, authorization, and task wake-ups. It only depends on domain objects and abstract ports.
- infrastructure provides PostgreSQL, Redis/Celery, Telethon, HTTP inference, archive storage, and security adapters.
- apps is the composition root for the API, ingestor, scheduler, and workers.

Dependencies are one-way. Routing never invokes inference or Telegram. Analysis never creates a publish record. A worker never owns domain policy logic.

### 1.2 Source of truth and effects

PostgreSQL is the only business source of truth. Redis and Celery are disposable wake-up infrastructure.

    persist intent or lease -> commit -> call external system -> persist outcome

Tasks carry stable database IDs only. A worker reloads current database state and runs idempotently. Network I/O must not run inside a long database transaction.

### 1.3 Versioning

Published Prompt, LabelSet, Stage, Pipeline, InferenceProfile, RoutingPolicy, RenderingTemplate, SourceChannelProfile, and MediaProcessingProfile versions are immutable. Each run records the actual version and snapshot used to explain historical outcomes.

## 2. Repository structure and ownership

    apps/
      api/                    Future FastAPI composition root
      telegram_ingestor/      Future Telethon updates and history scan process
      scheduler/              Future PostgreSQL scan and task wake-up process
      worker/                 Future Celery composition root

    tgcurator/
      shared/                 Domain errors and time validation
      domain/
        messages/             Logical messages, assets, visual identity
        processing/           Ranges, executions, watermarks
        media/                Future normalization and archive policies
        analysis/             DAG, labels, schemas, cache, negative gate
        routing/              Facts, constrained DSL, rule evaluation
        publishing/           Modes, states, business idempotency
        telegram/             Future identity/capability domain model
        archive/              Future archive metadata and cleanup policy
        audit/                Future audit event model
      application/
        ports/                External system abstractions
        commands/             Future state-changing use cases
        queries/              Future query use cases
        services/             Future cross-aggregate orchestration
      infrastructure/         Future adapter implementations
      workers/                Future task declarations and routing

    tests/
      unit/                   Deterministic tests with no external dependency
      integration/            Future PostgreSQL/Redis tests with fakes
      contract/               Future opt-in live Telegram/provider tests

Do not create a repository-wide models.py, worker.py, or service.py. A bounded module may own local models.py, policies.py, services.py, and ports.py.

## 3. Current module contracts

### 3.1 Messages

MessageContent is the immutable normalized logical message. Albums may become one logical message but the system does not aggregate across messages.

message_visual_fingerprint() hashes only the sorted multiset of image original pHashes and video cover pHashes. It intentionally ignores text, source channel, media order, and video representative frames. Near-duplicate thresholds and index lookups belong to a later media/repository iteration.

### 3.2 Processing

A FIXED range validates start_at < fixed_end_at and always uses the fixed right boundary. A LATEST range only freezes an execution when the observed newest message has remained quiet for the configured stable window. A watermark stays within execution bounds, moves monotonically, and must reach the right boundary before completion.

### 3.3 Analysis

A pipeline is an explicit DAG. Undefined dependencies, duplicate stage IDs, self-dependencies, and cycles are rejected before use. The Negative Gate evaluates frozen label facts against a frozen policy and returns an auditable decision without changing model labels.

### 3.4 Routing

Routing is deterministic and has no effects. The DSL accepts JSON/YAML-derived constrained AST data only. It cannot execute Python, SQL, templates, arbitrary object traversal, files, environment variables, or network calls.

Supported boolean nodes are all, any, and not. Normal predicates follow the architecture form:

    fact: effective.global.real_ugc
    op: gte
    value: 0.70

Supported comparison operations are exists, eq, neq, gt, gte, lt, lte, in, contains, and starts_with. Explicit label_score and label_present helper nodes are also supported. Unknown fields, invalid AST, and incompatible types never become matches.

Rules are ordered by priority DESC and rule_id ASC. A matching rule may emit zero or more publication actions. stop_on_match stops subsequent rules only.

### 3.5 Publishing

The domain exposes all four required modes:

1. native_forward_with_supplement
2. copy_with_caption
3. forward_only
4. metadata_only

publication_idempotency_key() derives a stable SHA-256 business identity from the source message, target, policy version, rule, action, and mode. The later PostgreSQL schema must enforce it with a unique constraint.

## 4. Application ports

The application ports currently include:

| Port | Responsibility | First adapter milestone |
| --- | --- | --- |
| TaskDispatcher | Best-effort wake-up by stable entity ID | M2 |
| ArchiveStorage | put, open, exists, delete, size | M3 |
| TelegramGateway | History, updates, download, forward/send, reconcile | M3/M6 |
| InferenceProvider | Versioned structured inference request | M4 |

Repositories, unit of work, audit, and publication lease ports are added with the first PostgreSQL schema in M1/M2. Ports do not leak ORM sessions, Telethon objects, or raw HTTP responses.

## 5. Transaction and task boundaries

| Use case | Atomic database result | Effect only after commit |
| --- | --- | --- |
| Ingest | Normalized message, Telegram unique key, initial state | Wake pre-screen |
| Complete stage | Validated result, labels, completed StageRun | Wake dependent stages/routing |
| Route | Facts snapshot, outcomes, PublicationIntents | Wake publish |
| Publish claim | Lease, sending attempt, stable random ID | Telegram send/forward |
| Archive write | READY metadata after atomic blob write | Wake analysis |

The scheduler scans records that were never enqueued, expired leases, due retry waits, and recovery states. A Redis wipe cannot change business completion state.

## 6. Delivery milestones

### M0 - Domain foundation (completed)

- Package, test layout, quality commands, and dependency direction.
- Message visual identity.
- FIXED/LATEST range boundaries and watermark invariants.
- Pipeline DAG validation and Negative Gate.
- Constrained Routing DSL and deterministic priority/stop behavior.
- Publication modes and idempotency key.
- Framework-neutral adapter ports.

Acceptance command:

    py -3.12 -m unittest discover -s tests/unit -v

The M0 suite requires no network, database, Redis, Telegram credentials, or model provider.

### M1 - API and versioned configuration skeleton (implemented; live PostgreSQL verification pending)

- FastAPI liveness/readiness endpoints and environment settings with a production master-key requirement.
- JSON structured logging with recursive sensitive-data redaction and no exception tracebacks in emitted JSON.
- PostgreSQL SQLAlchemy metadata plus asynchronous Alembic migrations for identities, channels, source profiles/versions, ranges, messages, encrypted secrets, administrators, and audit events.
- One-time admin bootstrap using Argon2id; a PostgreSQL partial unique index allows no more than one active administrator.
- AES-256-GCM secret encryption with per-record random nonces, key identifiers, and safe status metadata.
- Draft/published/retired source-profile version constraints. Database checks require the correct publication timestamp; a PostgreSQL trigger prevents mutation of published/retired content.

Acceptance commands:

    .venv\Scripts\python.exe -m unittest discover -s tests/unit -v
    .venv\Scripts\python.exe -m unittest discover -s tests/integration -v
    .venv\Scripts\alembic.exe upgrade head --sql

The integration suite verifies generated PostgreSQL DDL without connecting to a server. A live PostgreSQL test should apply the migration and validate the partial unique index, immutable-version trigger, and encrypted-secret persistence adapter before M1 is declared fully complete.

### M2 - PostgreSQL processing state machine (durable scheduling foundation implemented)

Implemented foundation:

- RangeExecution persistence with frozen finite boundaries, snapshot profile version, watermark, status, lease, and completion constraints.
- ProcessingRange durable watermark column and ceiling/floor checks.
- Scheduler application service that creates FIXED or stable LATEST executions. Telegram newest-message observation occurs before the transaction that inserts the execution and wake-up.
- Atomic RangeExecution plus durable-wakeup insertion, protected by unique range-boundary and queue/entity keys.
- Durable wake-up lease repository with PostgreSQL SELECT FOR UPDATE SKIP LOCKED and an at-least-once repair loop. A successful dispatcher handoff schedules a later repair attempt until a future worker records entity completion.
- Range-execution worker state-machine services: lease claim, monotonic execution-watermark persistence, and completion. Completion advances the parent durable watermark only when the finished execution is contiguous with that watermark, and completes the matching durable wake-up in the same transaction.
- Celery producer adapter and scheduler/worker composition roots. The broker task is JSON-only and contains one normalized RangeExecution UUID; it never carries configuration, credentials, cursors, or message payloads.
- Unit tests for stable-boundary scheduling, duplicate execution suppression, dispatch retry behavior, worker claim/progress/completion wiring, and compiled PostgreSQL SKIP LOCKED SQL.

Still required to complete M2:

- Live PostgreSQL concurrency verification for wake-up/execution claims, lease reclamation, stale-lease rejection, and contiguous range-watermark advancement.
- A Redis-loss runtime recovery test proving that an unfinished durable-wakeup row causes re-enqueue after broker loss.

### M3 - Telegram ingestion and media archive (Parts 1-7: typed ingestion, local atomic storage, image normalization, range-history orchestration, real-time media-group aggregation, bounded Telethon history reads, and durable reconciliation-cursor storage implemented)

Implemented M3 checkpoints:

- Platform-neutral `TelegramMessage` DTOs now cross the Telegram application boundary; `TelegramGateway.fetch_history` returns this typed model rather than `Any`.
- `MessageIngestService.ingest_history(...)` and `.ingest_update(...)` delegate to one common normalization and repository-upsert path. Telegram I/O remains outside the persistence transaction.
- Normal messages retain source-channel + Telegram message-ID identity. Media groups normalize only by source-channel + Telegram `grouped_id`; their deterministic anchor is the smallest observed component ID and every actual component ID is retained.
- `message_parts` stores source-channel/component-message membership. PostgreSQL constraints prevent duplicate component ownership, invalid IDs, and duplicate non-null source/group identities.
- PostgreSQL upserts preserve known payload fields and add newly observed album component IDs on repeat history/update ingestion. Focused unit tests cover history/update idempotency, cross-source group isolation, album ordering, duplicate component rejection, schema constraints, and compiled PostgreSQL conflict SQL.
- `LocalArchiveStorage` implements the `ArchiveStorage` port for a local Docker-volume-compatible root. It accepts only safe relative POSIX keys, writes temporary files with fsync, atomically publishes without replacement, makes same-byte replays idempotent, rejects different bytes at an immutable key, and never persists a host path as a business key.
- `PillowImageProcessor` implements the `ImageProcessor` port: it applies EXIF orientation, preserves alpha when present, bounds dimensions, emits metadata-free WebP, computes SHA-256 values for both source and archive bytes, and calculates a deterministic 64-bit DCT pHash from normalized display pixels. `ImageArchiveService` keeps archive I/O outside any database transaction; asset metadata/READY persistence is deliberately still pending.
- RangeExecutionHistoryIngestion performs claimed finite-window processing without a long transaction: it fetches the immutable Telegram interval, rejects foreign or out-of-window DTOs, invokes the idempotent MessageIngestService, then advances the execution watermark to its immutable right boundary and completes only after that update succeeds. WorkerRuntime calls it only when deployment composition injects a configured Telegram gateway; its no-adapter fallback leaves the lease open rather than falsely completing work.
- MediaGroupAggregationBuffer provides a short, positive-duration, in-memory wait for native Telegram grouped-message parts. RealtimeTelegramIngestion sends regular messages immediately and flushes complete/expired or shutdown-drained groups through the existing common normalization/upsert path. The buffer is intentionally reconstructable: replay/history reconciliation restores anything a process restart had not yet emitted.
- TelethonReadGateway and TelethonMessageMapper define the read-side adapter without leaking Telethon objects across the application boundary. Given an injected source-peer resolver, it obtains newest timestamps and bounded newest-to-oldest history, maps caption/group/media DTOs, and stops after the left boundary. It is deliberately not a deployed identity/session or live-Update composition.
- `source_channels.last_seen_message_id` persists a positive, nullable reconciliation point. `SourceReconciliationService` validates source UUIDs and positive Telegram IDs before delegating to a narrow cursor port; the PostgreSQL adapter atomically advances only from null or a lower value, so concurrent, duplicate, and out-of-order deliveries cannot regress it. This is storage foundation only: no Update adapter or reconnect scan is composed yet.

Still required for the remainder of M3:

- Deployable Telethon identity/session plus live Update adapter that drives the aggregation flush loop, and reconnect reconciliation that reads from the persisted `last_seen_message_id` cursor. Bounded history reads and cursor storage are implemented, but no Telethon gateway is composed into a deployed process yet.
- Source edits/deletions; archive-asset metadata and DB READY-state transitions; Telegram media download wiring; video cover/representative-frame extraction; and protected-content error handling.
- Live PostgreSQL adapter verification for concurrent replay/upsert behavior. SQLite is not a substitute because the schema relies on PostgreSQL partial indexes and conflict semantics.

### M4 - Analysis engine

Add versioned labels/prompts/stages/pipelines/profiles, InputManifest, dynamic JSON schema, provider adapters, response validation, cache, text pre-screen, Negative Gate, and GLOBAL/MEDIA multi-label runs.

### M5 - Review and routing workflow

Add message review, manual/effective labels, reanalysis, routing dry-run, facts snapshots, and transactional PublicationIntent creation.

### M6 - Publishing and operations

Add all publication modes, leases, attempts, FloodWait retry, partial publish recovery, reconciliation, dashboard, audit, metrics, health, archive cleanup dry-run, and failure-injection coverage for restarts, Redis loss, and worker crashes.

## 7. Explicit prohibitions

- Do not place analysis or routing policy in a FastAPI endpoint or Celery task.
- Do not store secrets in normal configuration, logs, exceptions, or audit diffs.
- Do not keep a PublicationIntent only in memory or Redis before publishing.
- Do not process an unstable LATEST right boundary.
- Do not reuse analysis cache solely because media appears visually similar.
- Do not overwrite model labels with manual labels or silently rewrite historical outcomes.
- Do not treat unknown DSL facts, exceptions, or type mismatches as a routing match.
