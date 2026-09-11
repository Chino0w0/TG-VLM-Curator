# Agent Handoff - TG-VLM-Curator

Updated: September 10, 2026 (Asia/Shanghai)
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

- M0, M1, M2, M3, and M4 are merged into origin/main.
- M4 was merged through English pull request #25 at merge commit b27d0b5; its implementation
  commit remains 9bc76d4.
- M5 human review/routing is the current delivery on branch module/m5-human-review-routing, based
  directly on the M4 merge commit.
- M5 implementation, migration, tests, documentation, and final local validation are complete on
  the branch and are delivered as exactly one implementation commit and one English pull request.
- M6 publication/operations remains pending.

## M5 completed scope

M5 implements append-only human review, deterministic routing, and the durable handoff to future
publication execution:

- Append-only manual label events with `set` and `clear` operations for GLOBAL message targets
  and MEDIA image/video targets; model assignments and AnalysisRuns are never mutated.
- Independent model, manual, and effective label namespaces. The latest manual event by
  `(created_at, event_id)` wins; set overrides a matching model value, while clear removes the
  override and falls back to model state.
- Explicit append-only message review transitions across `unreviewed`, `in_review`,
  `reviewed`, and `needs_attention`. `review_status` is workflow/filter state and an
  available fact, not an implicit routing gate.
- Immutable published/archived routing-policy and rendering-template versions with draft-only
  routing rules and actions.
- A constrained non-executable routing DSL supporting ordinary facts, model/manual/effective
  GLOBAL and MEDIA labels, activation and score predicates, `any_media`/`none_media`, stable
  `(-priority, rule_id)` order, `stop_on_match`, and conservative unknown propagation.
- Canonical facts snapshots and SHA-256 hashes derived from one PostgreSQL routing snapshot.
- Write-free dry runs, duplicate formal request-ID reuse, and explicit rerouting through new
  request IDs without deleting earlier evaluations or intents.
- Atomic formal persistence of one immutable RoutingEvaluation and zero-to-many pending
  PublicationIntents with immutable identity fields and stable business idempotency keys.

## M5 execution boundary

Routing loads business state from PostgreSQL and produces deterministic decisions. It calls no LLM,
external inference provider, Telegram client, Celery dispatcher, or publishing worker. Dry runs
write nothing. Formal runs write only their routing evaluation and pending publication intents.

Publication leases, attempts, retries, FloodWait handling, Telegram delivery, partial recovery, and
reconciliation belong to M6 and must not be pulled into M5.

## M5 migration

- Revision: d7b3f9a1e6c2
- Down revision: c4a9e7d2f5b1
- File: migrations/versions/d7b3f9a1e6c2_m5_human_review_routing.py
- Chronology date: September 9, 2026
- Adds `messages.review_status` and ten tables for manual labels, review events, routing policies
  and versions, rules, rendering templates and versions, actions, evaluations, and publication
  intents.
- Adds JSONB snapshots, lifecycle/hash/identity constraints, short explicit foreign-key names,
  published-version and pending-intent partial indexes, and PostgreSQL append-only/immutability/
  draft-only triggers.
- Offline upgrade to M5 and downgrade from M5 to M4 are covered by integration tests, including
  dependency-safe trigger, function, index, and table removal ordering.

## Important implementation locations

- tgcurator/domain/review/
- tgcurator/domain/routing/
- tgcurator/domain/publishing/idempotency.py
- tgcurator/application/review/
- tgcurator/application/routing/
- tgcurator/application/ports/review.py
- tgcurator/application/ports/routing.py
- tgcurator/infrastructure/database/review_repository.py
- tgcurator/infrastructure/database/routing_repository.py
- tgcurator/infrastructure/database/models.py
- migrations/versions/d7b3f9a1e6c2_m5_human_review_routing.py
- tests/unit/test_review.py
- tests/unit/test_review_service.py
- tests/unit/test_review_repository.py
- tests/unit/test_routing.py
- tests/unit/test_routing_service.py
- tests/unit/test_routing_repository.py
- tests/unit/test_publishing.py
- tests/unit/test_database_schema.py
- tests/integration/test_alembic_offline_sql.py

## Final M5 validation

The M5 working tree passes the required local gates without a running PostgreSQL server, Redis
broker, Telegram identity/session, external inference provider, inference model, or publication
worker:

- Focused M5 domain, service, repository, publishing, schema, and migration suite: 65 passed.
- Ruff formatting check: 175 files already formatted.
- Ruff lint: all checks passed.
- Full pytest suite: 286 passed with two dependency deprecation warnings.
- Unit unittest discovery suite: 275 passed.
- Integration unittest discovery suite: 11 passed.
- compileall for apps, tgcurator, and tests.
- pip check: no broken requirements.
- Offline PostgreSQL Alembic upgrade through M5 and M5-to-M4 downgrade rendering.
- git diff --check and final working-tree audit.

The two pytest warnings are existing FastAPI/Starlette compatibility deprecations for the httpx
TestClient import path and the AnyIO BlockingPortal alias; they do not fail the suite.

## Next modules

- M6 remains pending: publication leases and attempts, FloodWait retry, partial recovery,
  reconciliation, metrics, service health, archive cleanup dry-runs, and failure injection.
- Keep future work on separate module branches and separate pull requests.
