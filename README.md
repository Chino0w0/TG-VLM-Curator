# TG VLM Curator

A modular system for ingesting Telegram content, preserving media evidence, applying versioned VLM pipelines, evaluating deterministic routing policies, and publishing auditable results.

Architecture specification: tg-vlm-curator-architecture.md.
Implementation plan and module boundaries: tg-vlm-curator-module-implementation.md.

## Current stage

M0 is complete. M1 establishes the API, versioned-configuration, security, and PostgreSQL migration foundation; M2 provides durable range scheduling. M3 Parts 1-10 now provide typed, idempotent Telegram message normalization/persistence, a local atomic archive adapter, deterministic image normalization/fingerprinting, generic finite-window history-execution orchestration, a short media-group aggregation path for real-time Updates, a typed bounded Telethon history reader, durable source reconciliation-cursor storage, durable source edit/deletion lifecycle services, durable image-asset metadata with post-publication READY transitions, and a protected-content-respecting Telethon media-download adapter. The current implementation passed deterministic unit and offline Alembic SQL verification on September 4, 2026.

The repository now includes:

- Side-effect-free domain models for message fingerprints, processing windows, pipeline DAG validation, negative gates, deterministic routing, and publication idempotency keys.
- Immutable configuration version rules with canonical JSON content hashes. PostgreSQL checks and a trigger enforce required publication timestamps and prevent changes to published or retired configuration content.
- FastAPI liveness and database-readiness endpoints, environment settings, and JSON structured logging with recursive redaction for passwords, secrets, tokens, authorization data, encrypted payloads, nonces, and master keys.
- SQLAlchemy PostgreSQL schema metadata and an initial async Alembic migration for identities, channels, profile versions, ranges, messages, encrypted secrets, administrators, and audit events.
- One-time administrator bootstrap using Argon2id password hashes. A PostgreSQL partial unique index permits at most one active administrator.
- AES-256-GCM encrypted-secret storage. Encrypted-secret status metadata does not expose plaintext, ciphertext, or nonces.
- PostgreSQL-only migration configuration. Migration URLs are read from TGCURATOR_DATABASE_URL or DATABASE_URL and are never hard-coded in alembic.ini.
- PostgreSQL-owned finite RangeExecution records with watermark, status, lease, and completion constraints.
- A scheduler application service that freezes FIXED and stable LATEST time windows, persists each execution atomically with a durable wake-up, and performs Telegram observation before the database transaction.
- Durable wake-up leases that use PostgreSQL SELECT FOR UPDATE SKIP LOCKED. Successful broker dispatches remain repairable, so broker loss can result in duplicate wakes but cannot erase unfinished database work.
- Worker-facing range-execution lease, monotonic progress, and completion services. Completion atomically advances a parent range watermark only for a contiguous finished execution and completes its durable wake-up.
- A Celery producer adapter that sends JSON-only `tgcurator.range_execution` wake-ups containing a normalized RangeExecution UUID, plus scheduler and worker composition roots that wire PostgreSQL repositories through the application services.
- M3 Part 1 typed Telegram ingestion DTOs and one `MessageIngestService` path for both history reads and live updates. Telegram-native albums normalize by source peer and `grouped_id`; their actual constituent Telegram message IDs are retained.
- A PostgreSQL `message_parts` membership table and idempotent message/part upsert repository. Regular messages use source + Telegram message-ID identity; albums use a source + `grouped_id` partial unique index and retain their deterministic lowest-ID anchor.
- A local `LocalArchiveStorage` adapter with relative-key validation, temporary-file write/fsync, atomic no-replace publication, immutable-key conflict detection, idempotent same-byte writes, and safe deletion. The caller persists only backend/key rather than host paths.
- A Pillow-backed `ImageProcessor` adapter that applies EXIF orientation, bounds dimensions, converts image evidence to metadata-free WebP, computes source/archive SHA-256 values, and produces a deterministic 64-bit DCT perceptual hash from normalized display pixels. Typed ingestion creates pending `image_assets` records for image media; `ImageArchiveService` archives first, verifies the immutable object size, then performs the short PostgreSQL READY-metadata transition. Exact immutable archive replays are safe, while missing, deleted, or conflicting assets cannot become READY.
- A RangeExecutionHistoryIngestion application service that fetches a claimed immutable Telegram history window, validates source and time boundaries, uses the idempotent ingestion path, then advances the watermark and completes only after persistence succeeds. The worker runtime invokes this service only when deployment injects a configured Telegram gateway; otherwise it intentionally leaves the lease open rather than marking unprocessed work complete.
- A reconstructable in-memory MediaGroupAggregationBuffer plus RealtimeTelegramIngestion service. Regular Updates persist immediately; native album parts are held for a short bounded window, released in deterministic component-ID order, and then use the same normalization/upsert path as history. Repeated Update delivery is safe, and controlled shutdown can flush pending groups.
- A read-only Telethon adapter with a platform-neutral mapper for bounded newest-to-oldest history scans. It resolves a configured source peer, filters to the immutable time window, stops after the lower boundary, and maps Telegram photos/videos/audio/documents to typed media DTOs. A companion `TelethonMediaDownloader` retrieves bytes for one validated source message reference, rejects missing media, and translates Telegram protected-content signals without attempting to bypass them. Production identity/session composition, live Updates, and durable archive-worker composition remain separate pending work.
- A durable PostgreSQL `source_channels.last_seen_message_id` reconciliation cursor with a positive-ID constraint and atomic compare-and-set advancement. `SourceReconciliationService` validates identifiers before the persistence port is called, while the database prevents concurrent, duplicate, or out-of-order delivery from moving the cursor backwards.
- A durable source-message lifecycle boundary: newer Telegram edits atomically replace retained message text and mark the message changed, while deletion events retain message/archive/analysis history and only set a deletion marker. The PostgreSQL adapter resolves normalized albums through `message_parts`, so any component ID reaches the parent logical message. A deployable Telethon Update adapter remains pending.

M3 remains in progress: deployable Telethon identity/session plus live-Update adapter that drives aggregation flushing, cursor reconciliation, and the completed edit/deletion services; durable archive-worker composition around the media downloader; video cover/representative-frame extraction; and protected-content handling beyond the download boundary. No inference model has been deployed or simulated; that integration remains isolated behind `InferenceProvider`. Live PostgreSQL migration/adapter verification, including concurrent replay behavior, remains pending because a local Docker daemon was not available during this validation.

## Development

Create a Python 3.12+ virtual environment and install the runtime and development extras:

    py -3.12 -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[runtime,dev]"

Run the deterministic unit suite:

    .venv\Scripts\python.exe -m unittest discover -s tests/unit -v

For offline migration SQL rendering, set TGCURATOR_DATABASE_URL to a PostgreSQL asyncpg URL, then run:

    .venv\Scripts\alembic.exe upgrade head --sql

Run the offline migration verification suite:

    .venv\Scripts\python.exe -m unittest discover -s tests/integration -v

## Dependency boundary

    apps / workers / infrastructure --> application --> domain --> shared

The domain layer does not depend on FastAPI, SQLAlchemy, Celery, Telethon, or HTTP clients. PostgreSQL is the business source of truth; Redis/Celery provide only reconstructable task wake-ups.
