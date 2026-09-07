# TG-VLM-Curator module implementation plan

Status date: 2026-09-07

Source specification: `tg-vlm-curator-architecture.md`.

This document defines repository-sized implementation modules. Each completed module is
validated locally and delivered as one branch, one commit, and one pull request. A module
may contain multiple cooperating packages when they are required by its acceptance criteria.

## Status

| Module | Scope | Status |
| --- | --- | --- |
| M0 | Domain foundation and framework-neutral ports | Complete |
| M1 | API, environment settings, security, and initial PostgreSQL schema | Complete |
| M2 | Durable ProcessingRange and RangeExecution scheduling | Pending |
| M3 | Telegram ingestion and media archive workflows | Pending |
| M4 | Versioned analysis engine and provider adapters | Pending; model not deployed |
| M5 | Human review and routing workflows | Pending |
| M6 | Publication and operations | Pending |

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

## M2 - Durable processing scheduling

M2 will persist ProcessingRange and RangeExecution state, leases, retries, watermarks, and
reconstructable queue wake-ups. A LATEST range will always freeze a stable finite right
boundary before workers process it.

## M3 - Telegram ingestion and archive workflows

M3 will add idempotent history/update ingestion, reconnect reconciliation, media-group
normalization, protected-content-respecting downloads, atomic archive publication, image
normalization, and durable image/video archive workers.

## M4 - Analysis engine

M4 will add versioned labels, prompts, stages, pipelines, inference profiles, input manifests,
dynamic response schemas, response validation, stage caching, text pre-screen, Negative Gate,
and GLOBAL/MEDIA multi-label runs. Until a real model or provider is supplied, only
provider-neutral interfaces and explicit not-ready behavior may exist; fake successful
inference results are prohibited.

## M5 - Human review and routing

M5 will preserve model, manual, and effective labels separately; add review workflows; and
persist routing evaluations plus PublicationIntent records.

## M6 - Publication and operations

M6 will add publication leases/attempts, FloodWait retry, partial recovery, reconciliation,
metrics, service health, archive cleanup dry-runs, and failure-injection coverage.
