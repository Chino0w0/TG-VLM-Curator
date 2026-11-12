# PR30 frontend regression suite

This suite tests the current `web/` against the existing FastAPI routes, authentication, CSRF and application services. The browser server uses `InMemoryAdminBackend` only; it never starts Telegram, a cleanup worker, an inference provider, PostgreSQL or Redis. It is a frontend/API-contract regression, not production or database acceptance.

## Fast checks

Reuse the existing `web/node_modules`; no dependency installation or lockfile change is required. From the repository root on this Windows host:

```powershell
& 'D:/Program Files/Git/bin/bash.exe' -lc 'cd web && npm run typecheck && npm test && npm run build'
```

`src/__tests__/regressions.test.tsx` adds 17 focused regressions to the existing 7 tests. The JSON fixtures originate from the actual backend HTTP responses and database serializers, not nested frontend-only aliases. The enum fixture reads `MessageRecord`'s database check constraint and `DuplicateStatus`.

## Real Chrome regression

Start a fresh local fixture server for each full browser run because the test intentionally creates, publishes, archives and edits isolated in-memory objects. Build the frontend first. In one terminal:

```powershell
& 'D:/Program Files/Git/bin/bash.exe' -lc '.venv/Scripts/python.exe web/tests/local_api_server.py --evidence .git/fix-2026-11-12/pr30'
```

In another terminal:

```powershell
& 'D:/Program Files/Git/bin/bash.exe' -lc 'node web/tests/browser-regression.cjs .git/fix-2026-11-12/pr30'
```

The defaults reuse this host's `C:/Program Files/Google/Chrome/Application/chrome.exe` and `E:/AgentProject/SCAU-QAassist-CIE/node_modules/playwright-core`. `CHROME_PATH` and `PLAYWRIGHT_CORE` can point to other already-installed copies. `TEST_BASE_URL` defaults to `http://127.0.0.1:18731`; external hosts are rejected. The fixture server accepts `--port` when another loopback port is needed.

Stop the local server after the run. The script closes its own Chrome context and process in `finally`. `browser-results.json`, `browser.log`, `fixture.json`, `serializer-detail.json`, `reroute-dto.json` and screenshots are evidence; credentials are synthetic test-only values from the existing backend unit-test fixture. No real credentials are needed or written to evidence.

The 16 browser checks cover all ten configuration families, actual default rendering and routing evaluation, legacy empty entity recovery in Analysis and Routing, unsaved-draft publishing locks, published-profile source scoping, image/video set and clear identity, Review 25+5 cursor pages, delayed Messages and Audit responses, delayed cleanup preview plus single-shot confirmation, serializer-driven causal chain, real reroute response fields, Test Bench diagnostics, identity/channel editing, failed logout, expired authentication and successful logout. The mobile Audit capture asserts document width does not exceed 390 pixels.

Only delayed response delivery, an injected logout 503, and the explicitly identified database-serializer detail replay are intercepted. Other calls use the real local HTTP implementation. The serializer replay invokes `_serialize_stage_run`, `_serialize_analysis_run`, `_serialize_evaluation` and `_serialize_intent` without a database connection. The default renderer check invokes the real routing service through HTTP in dry-run mode and verifies the resulting rendered text. Cleanup confirmation creates an in-memory job; no worker or deletion is executed.

## Updating contract fixtures

After a reviewed backend contract change, start the fixture server once with `--export-fixtures` to regenerate `config-entity.json`, `message-detail.json` and `message-states.json` inside `web/src/__tests__/fixtures/`. After a successful browser run, `reroute-dto.json` is the real HTTP response used for `reroute.json`. Inspect changes before adopting them; do not add compatibility aliases just to make an incorrect mock pass.

Inference profile URL and model defaults are authoring examples, not verified live endpoints. Actual inference credentials and endpoint/model availability require a separately authorized provider test. Cross-database persistence, migrations, PostgreSQL concurrency, Redis and external Telegram behavior belong to the main integration gate.
## Native PostgreSQL browser gate

The same browser checks can run against a real migration-created PostgreSQL database, real `SqlAlchemyAdminBackend`, `SqlAlchemySecretVault`, session repository and HTTP routes. Use the centralized isolated service configuration; do not pass credentials in command arguments:

```powershell
& 'D:/Program Files/Git/bin/bash.exe' -lc '.venv/Scripts/python.exe -B web/tests/native_postgres_runner.py --services .git/fix-2026-11-12/test-services.json --evidence .git/fix-2026-11-12/pr30/native/final'
```

Build `web/dist` before this command when testing a new source revision. The runner does not modify or rebuild `web/src`; it hashes all source files before/after. It imports the main-line-owned `tests/integration/postgres_support.py`, creates only a UUID-suffixed `tgcurator_test_web_ui_*` database, runs migrations to `head`, listens only on `127.0.0.1:18732`, launches the existing Chrome/Playwright installation, and shuts down HTTP plus drops that exact database in `finally`. It rejects nonlocal service configuration or a non-maintenance admin database. It does not modify containers or any other database. An explicit `--port` changes only the loopback HTTP listener.

Test password/master key/credential values are random in memory. The browser receives the test password through its private environment, not command arguments or evidence. The vault ciphertext and authenticated decryption are asserted. Exception evidence includes stack frames and exception types; SQLAlchemy exception messages/SQL parameters are omitted. The centralized service password is never printed.

Native mode runs 17 checks: the original functional groups plus effective/manual MEDIA SQL filtering before and after CLEAR. Logout503 fault injection and database-detail response replay are disabled in native mode. The only response interception is controlled delay of actual HTTP responses for race tests; bodies are never replaced. The diagnostic Message/Part/Image/Video/AnalysisRun/Manifest/InferenceCall/StageRun fixtures are real SQL rows inserted under the production constraints. Evaluation/Intent creation uses the real HTTP routing service. The only synthetic results represent the external inference boundary; no provider or Telegram request is made. No cleanup worker is started, and a final database assertion requires zero deleted image/video rows.

A separate initial HTTP probe verifies empty-asset cleanup with only a real administrator/session. The browser later confirms a nonempty cleanup job and the runner verifies its persisted item count, label events, source Profile binding, and real diagnostic rows. Native output includes `native-runtime.json`, `minimal-empty-cleanup.json`, `native-detail-http.json`, `native-database-verification.json`, `browser-results.json`, screenshots and `cleanup-verification.json`.

If a backend exception occurs, retain that run's directory and report it to the backend owner; do not modify frontend DTOs or inject substitute responses to pass. The runner can be rerun with a new evidence directory after the authorized fix. The PostgreSQL gate does not exercise Redis queues, real media files, actual deletion, Telegram delivery or a live inference endpoint.