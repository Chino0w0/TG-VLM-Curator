# TG-VLM-Curator

TG-VLM-Curator is a domain-first Telegram ingestion, visual analysis, routing, and
publishing system. The implementation follows `tg-vlm-curator-architecture.md`.

## Current implementation status

**M0 - Domain foundation is complete.** It provides deterministic, framework-independent
rules for:

- message visual identity based on image pHash and video-cover pHash multisets;
- FIXED and stable-window LATEST processing ranges;
- immutable configuration snapshots;
- analysis pipeline DAG validation and Negative Gate decisions;
- a constrained, non-executable Routing DSL;
- publication modes and idempotency keys;
- provider-neutral Telegram, archive, task-dispatch, and inference ports.

The VLM model is not deployed yet. M0 intentionally contains only the
`InferenceProvider` contract: it does not return fabricated inference results and does not
require model credentials to run its test suite.

See `tg-vlm-curator-module-implementation.md` for module boundaries and acceptance criteria.

## Dependency direction

    apps / workers / infrastructure --> application --> domain --> shared

The domain package does not import FastAPI, SQLAlchemy, Celery, Telethon, httpx, or
filesystem adapters. PostgreSQL will become the business source of truth in M1/M2; queue
messages remain reconstructable wake-ups rather than business state.

## Local development

Create a Python 3.12+ virtual environment and install the development tools:

    py -3.12 -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"

Run the M0 acceptance suite (no database, Redis, Telegram credentials, or model required):

    .venv\Scripts\python.exe -m pytest
    .venv\Scripts\python.exe -m unittest discover -s tests/unit -v
    .venv\Scripts\ruff.exe format --check .
    .venv\Scripts\ruff.exe check .
