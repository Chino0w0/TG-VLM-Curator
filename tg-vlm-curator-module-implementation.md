# TG-VLM-Curator module implementation plan

Status date: 2026-09-09

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

## M6 - Publication and operations

M6 will add publication leases/attempts, FloodWait retry, partial recovery, reconciliation,
metrics, service health, archive cleanup dry-runs, and failure-injection coverage.
