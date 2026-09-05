# TG VLM Curator

A modular system for ingesting Telegram content, preserving media evidence, applying versioned VLM pipelines, evaluating deterministic routing policies, and publishing auditable results.

Architecture specification: tg-vlm-curator-architecture.md.
Implementation plan and module boundaries: tg-vlm-curator-module-implementation.md.

## Current stage

M0 is complete. M1 establishes the API, versioned-configuration, security, and PostgreSQL migration foundation; M2 provides durable range scheduling. M3 Part 1 now provides typed, idempotent Telegram message normalization and persistence. The current implementation passed deterministic unit and offline Alembic SQL verification on September 4, 2026.

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

M3 remains in progress: Telethon adapters, an aggregation-window buffer, reconciliation cursors, range-execution history processing, source edits/deletions, archive storage, image/video extraction, pHash generation, and protected-content handling are still pending. The M2 worker therefore still only claims a stable execution UUID; it does not yet invoke Telegram history processing or advance the execution watermark. A live PostgreSQL migration/adapter verification remains pending because a local Docker daemon was not available during this validation.

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
