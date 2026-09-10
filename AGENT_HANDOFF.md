# Agent Handoff - TG-VLM-Curator

Updated: September 9, 2026 (Asia/Shanghai)
Workspace: E:\Project\TG-VLM-Curator
Remote: https://github.com/Chino0w0/TG-VLM-Curator

## User delivery rules

- Follow tg-vlm-curator-module-implementation.md and tg-vlm-curator-architecture.md.
- A delivery unit is one complete documented module, not one file or one small change.
- Use one branch, one commit, and one English pull request for each completed module.
- Push completed module branches to the GitHub remote only after all local checks pass.
- PostgreSQL is business truth; Redis/Celery messages are reconstructable UUID-only wake-ups.
- Do not bundle, deploy, or fabricate an inference model/provider.

## Current delivery

- M0, M1, M2, and M3 are merged into origin/main.
- M4 is completed on branch module/m4-analysis-engine, based on origin/main at 787dab9.
- M4 is delivered as one implementation commit and one English pull request against main.
- M5 human review/routing and M6 publication/operations remain pending.

## M4 completed scope

M4 implements the versioned analysis engine and external-provider adapter boundary:

- Immutable draft/published/retired versions for label definitions, label sets, prompts,
  inference profiles, analysis stages, and analysis pipelines.
- Draft-only label bindings and pipeline nodes, published-version immutability, validated DAG
  dependencies, and deterministic run_if fact conditions.
- Dynamic dense/sparse JSON Schemas keyed to stable message or asset target IDs.
- Strict structured-output validation with partial batch commits and per-target retry for missing
  or invalid results.
- Immutable InputManifest snapshots and semantic cache keys isolated by stage, prompt, label set,
  response schema, inference profile, provider parameters, and actual inputs.
- GLOBAL and MEDIA multi-label results, formal ModelLabelAssignments, test-run isolation, and
  first-cause Negative Gate blocking of later analysis.
- Durable AnalysisRun, StageRun, InferenceCall, and assignment persistence with short leases,
  expired-lease recovery, bounded attempts, exact cache provenance, and sanitized error/audit
  payloads.
- Provider-neutral orchestration that persists a pending InferenceCall before provider I/O and
  keeps network calls outside database transactions.
- An initial OpenAI-compatible external HTTP adapter with secret resolution, structured-output
  requests, bounded response handling, stable retry classification, and credential/URL redaction.
- Celery/runtime registration for the dedicated analysis queue and UUID-only
  tgcurator.analysis task.

## Provider availability and failure behavior

No inference model or provider is bundled or deployed. An operator must configure a real external
provider endpoint and secret before inference can succeed. The default runtime intentionally uses
AnalysisOrchestrator(provider=None).

Missing provider configuration is not treated as success and does not fabricate labels. The worker
first persists the pending InferenceCall, records InferenceProviderNotConfigured, and moves each
affected StageRun into explicit durable retry_wait state or terminal failed state when its bounded
attempt limit is reached. API readiness continues to depend on PostgreSQL rather than the optional
inference provider.

## M4 migration

- Revision: c4a9e7d2f5b1
- Down revision: b8e6c4f2a137
- File: migrations/versions/c4a9e7d2f5b1_m4_versioned_analysis_engine.py
- Chronology date: September 9, 2026
- Adds 19 M4 tables, message Negative Gate cause columns/foreign keys, lifecycle constraints,
  partial unique indexes, and PostgreSQL immutability/draft-only triggers.
- Offline upgrade to M4 and downgrade from M4 to M3 are covered by integration tests.

## Important implementation locations

- tgcurator/domain/analysis/
- tgcurator/application/analysis/
- tgcurator/application/ports/analysis.py
- tgcurator/infrastructure/database/analysis_repository.py
- tgcurator/infrastructure/inference/openai_compatible.py
- tgcurator/infrastructure/queue/celery_dispatcher.py
- apps/worker/runtime.py
- apps/worker/celery_app.py
- migrations/versions/c4a9e7d2f5b1_m4_versioned_analysis_engine.py
- tests/unit/test_analysis_engine_domain.py
- tests/unit/test_analysis_orchestrator.py
- tests/unit/test_analysis_worker.py
- tests/unit/test_analysis_repository.py
- tests/unit/test_openai_compatible_inference.py
- tests/integration/test_alembic_offline_sql.py

## Final M4 validation

The M4 branch passes the required local gates without a running PostgreSQL server, Redis broker,
Telegram identity/session, external inference provider, or inference model:

- Ruff formatting check and lint.
- Full pytest suite: 252 passed with two dependency deprecation warnings.
- Unit unittest discovery suite: 243 passed.
- Integration unittest discovery suite: 9 passed.
- compileall for apps, tgcurator, and tests.
- pip check.
- Offline PostgreSQL Alembic upgrade to M4 and M4-to-M3 downgrade rendering.
- git diff --check and final working-tree audit.

The two pytest warnings are existing FastAPI/Starlette compatibility deprecations for the httpx
TestClient import path and the AnyIO BlockingPortal alias; they do not fail the suite.

## Next modules

- M5 remains pending: preserve model/manual/effective labels separately, add human review, persist
  routing evaluations, and create PublicationIntent records.
- M6 remains pending: publication leases and attempts, FloodWait retry, partial recovery,
  reconciliation, metrics, service health, archive cleanup dry-runs, and failure injection.
- Keep future work on separate module branches and separate pull requests.