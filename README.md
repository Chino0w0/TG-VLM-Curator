# TG-VLM-Curator

TG-VLM-Curator is a domain-first Telegram ingestion, visual analysis, routing, and
publishing system. The implementation follows tg-vlm-curator-architecture.md.

## Current implementation status

**M0 - Domain foundation** and **M1 - API, security, and PostgreSQL foundation** are
complete.

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

The VLM model is not deployed yet. The repository intentionally keeps only the
InferenceProvider boundary and never fabricates successful inference. API readiness checks
PostgreSQL only; a missing inference provider remains a degraded business capability rather
than making the administration API unavailable.

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
    .venv\Scripts\python.exe -m compileall -q apps tgcurator tests
    .venv\Scripts\python.exe -m pip check
    git diff --check

The Alembic integration tests render PostgreSQL upgrade and downgrade SQL offline, so the test
suite itself does not require a running PostgreSQL server.
