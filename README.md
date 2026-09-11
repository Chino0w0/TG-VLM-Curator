# TG-VLM-Curator

TG-VLM-Curator is a domain-first Telegram ingestion, visual analysis, routing, and
publishing system. The implementation follows tg-vlm-curator-architecture.md.

## Current implementation status

**M0 - Domain foundation**, **M1 - API, security, and PostgreSQL foundation**,
**M2 - durable processing scheduling**, **M3 - Telegram ingestion and media archive
workflows**, **M4 - versioned analysis engine and provider adapters**, and **M5 - human
review and routing workflows** are complete.

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
configuration are required for inference. The default runtime remains deliberately unconfigured;
missing provider configuration records an explicit failed InferenceCall and durable StageRun
retry or terminal-failure state instead of fabricating a successful result. API readiness checks
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

Routing performs no LLM/provider inference, Telegram calls, Celery dispatch, or publication worker
work. M5 stops at durable pending intents; publication leases, attempts, retries, FloodWait
handling, Telegram delivery, and reconciliation remain M6.

See tg-vlm-curator-module-implementation.md for module boundaries and acceptance criteria.

## Dependency direction

    apps / workers / infrastructure --> application --> domain --> shared

The domain package does not import FastAPI, SQLAlchemy, Celery, Telethon, httpx, or storage
clients. PostgreSQL is the business source of truth; future queue messages remain
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

Run the module acceptance suite without requiring Redis, Telegram credentials, or an inference
model:

    .venv\Scripts\python.exe -m ruff format --check .
    .venv\Scripts\python.exe -m ruff check .
    .venv\Scripts\python.exe -m pytest
    .venv\Scripts\python.exe -m unittest discover -s tests/unit -v
    .venv\Scripts\python.exe -m unittest discover -s tests/integration -v
    .venv\Scripts\python.exe -m compileall -q apps tgcurator tests
    .venv\Scripts\python.exe -m pip check
    git diff --check

The Alembic integration tests render PostgreSQL upgrade and downgrade SQL offline, so the test
suite itself does not require a running PostgreSQL server.
