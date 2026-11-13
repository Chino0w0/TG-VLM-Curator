from __future__ import annotations

import asyncio
import base64
import gc
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from apps.healthcheck import evaluate_service_probe
from apps.runtime_dependencies import (
    VaultInferenceSecretResolver,
    check_heartbeat_file,
    clear_heartbeat_file,
    resolve_database_url,
    upsert_service_heartbeat,
    write_heartbeat_file,
)
from apps.scheduler.__main__ import (
    FallbackLatestBoundarySource,
    build_default_scheduler_runtime,
    run_scheduler_daemon,
)
from apps.scheduler.runtime import SchedulerRuntime
from apps.telegram_ingestor.__main__ import run_ingestor_daemon
from apps.telegram_ingestor.runtime import TelegramIngestorRuntime
from apps.worker.__main__ import main as worker_main
from apps.worker.celery_app import create_worker_celery_app
from apps.worker.runtime import (
    _REGISTERED_EXTRACTORS,
    ConcurrencyBoundedAnalysisOrchestrator,
    ConcurrencyBoundedInferenceProvider,
    WorkerRuntime,
    _WeakRefFFmpegVideoFrameExtractor,
    activate_process_worker_runtime,
    execute_worker_task,
    register_media_extractor,
    shutdown_process_worker_runtime,
    sweep_stale_media_temp_entries,
)
from tgcurator.application import (
    MediaGroupAggregationBuffer,
    MessageIngestService,
    RealtimeTelegramIngestion,
    ReconnectTelegramReconciliation,
    Settings,
    SourceMessageLifecycleService,
    SourceReconciliationService,
    get_settings,
)
from tgcurator.application.ports.analysis import InferenceRequest, InferenceResponse
from tgcurator.application.ports.ingestion import IngestReport
from tgcurator.application.ports.processing import LatestMessageBoundary
from tgcurator.application.ports.secrets import SecretStatus
from tgcurator.application.processing import RangeScheduleReport, WakeupDispatchReport
from tgcurator.domain.analysis import (
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    InferenceProfileVersion,
    InputManifest,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    PromptTemplateVersion,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputPolicy,
    VersionStatus,
    VisualCompositionPolicy,
)
from tgcurator.domain.messages import NormalizedTelegramMessage, TelegramMessage
from tgcurator.infrastructure.database import AsyncDatabase
from tgcurator.infrastructure.database.secret_vault import SecretTypeMismatchError
from tgcurator.infrastructure.observability import OperationsMonitor

NOW = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)


class PostM10OpsRegressionsTests(unittest.IsolatedAsyncioTestCase):
    def test_worktree_git_file_evidence_resolution_succeeds(self) -> None:
        integration_dir = str(Path(__file__).resolve().parents[1] / "integration")
        with patch.object(sys, "path", [integration_dir, *sys.path]):
            from tests.integration.test_runtime_closed_loop import (
                _resolve_evidence_dir as resolve_closed_loop_evidence,
            )
            from tests.integration.test_worker_process_restart import (
                _resolve_evidence_dir as resolve_restart_evidence,
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree_git_dir = root / "common-git" / "worktrees" / "worker-a"
            worktree_git_dir.mkdir(parents=True)
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / ".git").write_text(
                f"gitdir: {worktree_git_dir.as_posix()}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TGCURATOR_EVIDENCE_DIR", None)
                resolved_a = resolve_closed_loop_evidence(checkout)
                resolved_b = resolve_restart_evidence(checkout)
            self.assertEqual(resolved_a, resolved_b)
            resolved_a.mkdir(parents=True, exist_ok=True)
            self.assertTrue(resolved_a.is_dir())
            self.assertEqual(resolved_a, (worktree_git_dir / "fix-2026-11-12/runtime").resolve())

            override = root / "custom-evidence"
            with patch.dict(os.environ, {"TGCURATOR_EVIDENCE_DIR": str(override)}, clear=False):
                self.assertEqual(resolve_closed_loop_evidence(checkout), override.resolve())

    def test_resolve_database_url_encodes_special_password_characters(self) -> None:
        raw_password = "p@ss:w/rd?query=1#frag%25 space"
        settings = Settings(_env_file=None, environment="test", database_url=None)
        with patch.dict(
            os.environ,
            {
                "POSTGRES_HOST": "postgres",
                "POSTGRES_PORT": "5432",
                "POSTGRES_USER": "cur@tor",
                "POSTGRES_PASSWORD": raw_password,
                "POSTGRES_DB": "tgcurator_prod",
            },
            clear=False,
        ):
            resolved = resolve_database_url(settings)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        db = AsyncDatabase(resolved)
        self.assertTrue(db.configured)
        parsed = make_url(resolved)
        self.assertEqual(parsed.username, "cur@tor")
        self.assertEqual(parsed.password, raw_password)
        self.assertEqual(parsed.host, "postgres")
        self.assertEqual(parsed.database, "tgcurator_prod")

    async def test_vault_inference_secret_resolver_enforces_expected_secret_type(self) -> None:
        secret_id = uuid4()
        calls: list[tuple[object, str | None]] = []

        class _FakeVault:
            def __init__(self, secret_type: str) -> None:
                self.secret_type = secret_type

            async def status(self, *, secret_id):
                return SecretStatus(
                    secret_id=secret_id,
                    secret_type=self.secret_type,
                    key_id="primary",
                    created_at=NOW,
                )

            async def resolve(self, *, secret_id, expected_secret_type: str | None = None) -> bytes:
                calls.append((secret_id, expected_secret_type))
                if expected_secret_type != self.secret_type:
                    raise SecretTypeMismatchError("secret reference has an unexpected type")
                return b"sk-live-secret"

        for valid_type in ("api_key", "inference_api_key"):
            resolver = VaultInferenceSecretResolver(_FakeVault(valid_type))  # type: ignore[arg-type]
            resolved = await resolver.resolve(secret_reference=str(secret_id))
            self.assertEqual(resolved, "sk-live-secret")
            self.assertEqual(calls[-1], (secret_id, valid_type))

        for forbidden_type in ("telegram_session", "bot_token"):
            resolver = VaultInferenceSecretResolver(_FakeVault(forbidden_type))  # type: ignore[arg-type]
            with self.assertRaises(SecretTypeMismatchError):
                await resolver.resolve(secret_reference=str(secret_id))
            self.assertEqual(calls[-1], (secret_id, "api_key"))

    async def test_healthcheck_enforces_timeout_and_heartbeat_freshness(self) -> None:
        settings = Settings(
            _env_file=None,
            environment="test",
            database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/db",
            celery_broker_url=SecretStr("redis://127.0.0.1:6379/0"),
        )

        async def _hung_db(_url: str) -> bool:
            await asyncio.sleep(10.0)
            return True

        code, payload = await evaluate_service_probe(
            role="api",
            probe="ready",
            settings=settings,
            database_probe=_hung_db,
            probe_timeout_seconds=0.05,
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["blockers"], ["database_unavailable"])

        with tempfile.TemporaryDirectory() as tmp:
            hb_path = Path(tmp) / "scheduler.json"
            missing_code, missing_payload = await evaluate_service_probe(
                role="scheduler",
                probe="live",
                settings=settings,
                heartbeat_file=hb_path,
                now=NOW,
            )
            self.assertEqual(missing_code, 1)
            self.assertEqual(missing_payload["blockers"], ["heartbeat_missing"])

            write_heartbeat_file("scheduler", status="running", now=NOW, explicit_path=hb_path)
            ok_flag, ok_reason = check_heartbeat_file(hb_path, now=NOW + timedelta(seconds=30))
            self.assertTrue(ok_flag)
            self.assertIsNone(ok_reason)

            live_code, _ = await evaluate_service_probe(
                role="scheduler",
                probe="live",
                settings=settings,
                heartbeat_file=hb_path,
                now=NOW + timedelta(seconds=30),
            )
            self.assertEqual(live_code, 0)

            stale_code, stale_payload = await evaluate_service_probe(
                role="scheduler",
                probe="ready",
                settings=settings,
                database_probe=lambda _u: asyncio.sleep(0, result=True),
                broker_probe=lambda _u: asyncio.sleep(0, result=True),
                heartbeat_file=hb_path,
                max_heartbeat_age_seconds=60.0,
                now=NOW + timedelta(seconds=61),
            )
            self.assertEqual(stale_code, 1)
            self.assertIn("heartbeat_stale", stale_payload["blockers"])
            clear_heartbeat_file("scheduler", explicit_path=hb_path)
            self.assertFalse(hb_path.exists())

    async def test_upsert_service_heartbeat_ignores_unmigrated_table_errors(self) -> None:
        self.assertFalse(
            await upsert_service_heartbeat(
                object(),
                service_name="scheduler",
                database_ready=True,
                broker_ready=True,
                now=NOW,
            )
        )
        idle_db = AsyncDatabase("postgresql+asyncpg://u:p@127.0.0.1:5432/db")
        self.assertFalse(
            await upsert_service_heartbeat(
                idle_db,
                service_name="scheduler",
                database_ready=True,
                broker_ready=True,
                now=NOW,
                connect_if_idle=False,
            )
        )

    def test_celery_app_bare_import_without_broker_env_is_safe(self) -> None:
        saved = os.environ.get("TGCURATOR_CELERY_BROKER_URL")
        try:
            os.environ.pop("TGCURATOR_CELERY_BROKER_URL", None)
            get_settings.cache_clear()
            app = create_worker_celery_app(require_broker=False)
            self.assertEqual(app.conf.broker_url, "memory://")
            with self.assertRaisesRegex(RuntimeError, "TGCURATOR_CELERY_BROKER_URL is required"):
                create_worker_celery_app(require_broker=True)
            with self.assertRaisesRegex(RuntimeError, "TGCURATOR_CELERY_BROKER_URL is required"):
                worker_main(["--role", "media"])
        finally:
            if saved is not None:
                os.environ["TGCURATOR_CELERY_BROKER_URL"] = saved
            get_settings.cache_clear()

    async def test_scheduler_close_and_daemon_resilience_and_boundary_wiring(self) -> None:
        celery_mock = MagicMock()
        pool_mock = AsyncMock()
        db_mock = MagicMock()
        db_mock.dispose = AsyncMock()

        runtime = SchedulerRuntime(
            database=db_mock,
            range_scheduler=MagicMock(),
            wakeup_dispatcher=SimpleNamespace(  # type: ignore[arg-type]
                task_dispatcher=SimpleNamespace(_celery=celery_mock)
            ),
            telegram_client_pool=pool_mock,
        )
        await runtime.close()
        pool_mock.close.assert_awaited_once()
        celery_mock.close.assert_called_once()
        db_mock.dispose.assert_awaited_once()

        key = SecretStr(base64.b64encode(b"k" * 32).decode("ascii"))
        settings = Settings(
            _env_file=None,
            environment="test",
            database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/db",
            celery_broker_url=SecretStr("redis://127.0.0.1:6379/0"),
            app_master_key=key,
        )
        composed = build_default_scheduler_runtime(settings)
        try:
            self.assertIsInstance(
                composed.range_scheduler.latest_boundary_source,
                FallbackLatestBoundarySource,
            )
            self.assertIsNotNone(composed.telegram_client_pool)
        finally:
            await composed.close()

        fallback_boundary = LatestMessageBoundary(message_id=42, published_at=NOW)
        primary_fail = SimpleNamespace(
            newest_message=AsyncMock(side_effect=RuntimeError("telegram rpc down"))
        )
        secondary_ok = SimpleNamespace(newest_message=AsyncMock(return_value=fallback_boundary))
        fb = FallbackLatestBoundarySource(primary=primary_fail, fallback=secondary_ok)
        self.assertEqual(await fb.newest_message(source_channel_id=str(uuid4())), fallback_boundary)

        # Daemon survives transient cycle failure and cancels hung run_once on SIGTERM stop_event.
        attempts = 0
        stop_event = asyncio.Event()
        closed = False

        class _FlakyThenHungScheduler:
            async def run_once(self, *, now: datetime) -> SchedulerRuntime:
                nonlocal attempts
                _ = now
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("transient failure")
                if attempts == 2:
                    return SimpleNamespace(  # type: ignore[return-value]
                        range_schedule=RangeScheduleReport(0, 0),
                        wakeup_dispatch=WakeupDispatchReport(0, 0, 0),
                    )
                await asyncio.sleep(30.0)
                raise AssertionError("unreachable")

            async def close(self) -> None:
                nonlocal closed
                closed = True

        async def _stop_soon() -> None:
            while attempts < 3:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            stop_event.set()

        stopper = asyncio.create_task(_stop_soon())
        cycles = await run_scheduler_daemon(
            runtime=_FlakyThenHungScheduler(),  # type: ignore[arg-type]
            interval_seconds=0.01,
            stop_event=stop_event,
        )
        await stopper
        self.assertEqual(cycles, 1)
        self.assertTrue(closed)

    async def test_ingestor_per_channel_isolation_and_sigterm_cancels_hung_poll_with_flush(
        self,
    ) -> None:
        good_channel = str(uuid4())
        bad_channel = str(uuid4())
        upserted: list[NormalizedTelegramMessage] = []

        class _IngestRepo:
            async def upsert_message(self, *, message: NormalizedTelegramMessage) -> None:
                upserted.append(message)

        class _CursorRepo:
            async def advance_latest_seen_message_id(self, **_: object) -> bool:
                return True

            async def advance_last_seen_message_id(self, **_: object) -> bool:
                return True

            async def get_latest_seen_message_id(self, **_: object) -> int | None:
                return None

            async def get_last_seen_message_id(self, **_: object) -> int | None:
                return None

        class _Gateway:
            async def newest_message(self, *, source_channel_id: str):
                if source_channel_id == bad_channel:
                    raise RuntimeError("FloodWaitError")
                return LatestMessageBoundary(message_id=5, published_at=NOW)

            async def fetch_history(
                self,
                *,
                source_channel_id: str,
                from_message_id_exclusive: int,
                to_message_id_inclusive: int,
            ):
                _ = (from_message_id_exclusive, to_message_id_inclusive)
                return (
                    TelegramMessage(
                        source_channel_id=source_channel_id,
                        telegram_message_id=5,
                        published_at=NOW,
                        original_text="healthy message",
                    ),
                )

        class _LifecycleRepo:
            async def record_edit(self, **_: object) -> bool:
                return False

            async def record_deletion(self, **_: object) -> bool:
                return False

        ingest_svc = MessageIngestService(repository=_IngestRepo())
        cursor_svc = SourceReconciliationService(repository=_CursorRepo())
        ingestor = TelegramIngestorRuntime(
            database=SimpleNamespace(dispose=AsyncMock()),  # type: ignore[arg-type]
            realtime_ingestion=RealtimeTelegramIngestion(
                message_ingest_service=ingest_svc,
                media_group_buffer=MediaGroupAggregationBuffer(
                    aggregation_window=timedelta(seconds=3)
                ),
                cursor_service=cursor_svc,
            ),
            source_lifecycle=SourceMessageLifecycleService(repository=_LifecycleRepo()),
            reconciliation=ReconnectTelegramReconciliation(
                telegram_gateway=_Gateway(),  # type: ignore[arg-type]
                message_ingest_service=ingest_svc,
                cursor_service=cursor_svc,
            ),
        )
        reports = await ingestor.reconcile_sources(
            source_channel_ids=(bad_channel, good_channel),
            now=NOW,
        )
        self.assertNotIn(bad_channel, reports)
        self.assertEqual(reports[good_channel].logical_messages, 1)
        self.assertTrue(ingestor.database_ready)

        # Hung poll_updates is cancelled when stop_event fires, and shutdown() still flushes.
        stop_event = asyncio.Event()
        shutdown_flushed = False
        poll_entered = asyncio.Event()

        class _HungPollIngestor:
            async def poll_updates(self, *, now: datetime) -> None:
                _ = now
                poll_entered.set()
                await asyncio.sleep(30.0)

            async def flush_due_groups(self, *, now: datetime) -> IngestReport:
                _ = now
                return IngestReport(logical_messages=0, component_messages=0)

            async def shutdown(self) -> IngestReport:
                nonlocal shutdown_flushed
                shutdown_flushed = True
                return IngestReport(logical_messages=1, component_messages=2)

        async def _trigger_stop() -> None:
            await poll_entered.wait()
            stop_event.set()

        stopper = asyncio.create_task(_trigger_stop())
        ticks = await run_ingestor_daemon(
            runtime=_HungPollIngestor(),  # type: ignore[arg-type]
            flush_interval_seconds=1.0,
            stop_event=stop_event,
        )
        await stopper
        self.assertEqual(ticks, 0)
        self.assertTrue(shutdown_flushed)

    def test_weakref_extractor_cleanup_temp_sweep_and_process_session_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            stale_dir = temp_root / "tgcurator-ffmpeg-stale123"
            stale_dir.mkdir()
            (stale_dir / "source.video").write_bytes(b"abc")
            keep_file = temp_root / "unrelated.txt"
            keep_file.write_text("keep", encoding="utf-8")

            swept = sweep_stale_media_temp_entries(temp_root)
            self.assertEqual(swept, 1)
            self.assertFalse(stale_dir.exists())
            self.assertTrue(keep_file.exists())

            extractor = register_media_extractor(
                _WeakRefFFmpegVideoFrameExtractor(temp_dir=temp_root)
            )
            ext_id = id(extractor)
            self.assertIn(ext_id, _REGISTERED_EXTRACTORS)
            del extractor
            gc.collect()
            self.assertNotIn(ext_id, _REGISTERED_EXTRACTORS)

        # Out-of-order worker heartbeat timestamps never raise DomainValidationError.
        monitor = OperationsMonitor()
        rt = WorkerRuntime(
            database=SimpleNamespace(dispose=AsyncMock()),  # type: ignore[arg-type]
            range_execution_worker=SimpleNamespace(claim=AsyncMock(return_value=None)),  # type: ignore[arg-type]
            telemetry=monitor,
        )
        later = NOW + timedelta(seconds=10)
        asyncio.run(rt.handle_range_execution(execution_id=str(uuid4()), now=later))
        asyncio.run(rt.handle_range_execution(execution_id=str(uuid4()), now=NOW))
        self.assertEqual(monitor.worker_health(now=later).last_heartbeat_at, later)

        # Process-scoped WorkerRuntime reuses one runtime and one event loop across tasks.
        created_runtimes: list[object] = []
        observed_loops: list[int] = []
        closed_count = 0

        class _SessionRuntime:
            async def close(self) -> None:
                nonlocal closed_count
                closed_count += 1

        def _fake_factory(**_kwargs: object) -> _SessionRuntime:
            instance = _SessionRuntime()
            created_runtimes.append(instance)
            return instance

        with (
            patch("apps.worker.runtime.create_worker_runtime", side_effect=_fake_factory),
            patch("apps.worker.runtime.get_settings", return_value=Settings(_env_file=None)),
        ):
            activate_process_worker_runtime(install_sigterm=False)
            try:
                for _ in range(3):
                    result = execute_worker_task(
                        coro_on_runtime=lambda _rt: self._record_loop(observed_loops),
                        standalone_coro=lambda: asyncio.sleep(0, result=False),
                    )
                    self.assertTrue(result)
                self.assertEqual(len(created_runtimes), 1)
                self.assertEqual(len(set(observed_loops)), 1)
            finally:
                from apps.worker.celery_app import _WorkerRuntimeLifecycleStep

                step = _WorkerRuntimeLifecycleStep(parent=None)
                step.terminate(None)
                shutdown_process_worker_runtime(deactivate=True)
        self.assertEqual(closed_count, 1)

    @staticmethod
    async def _record_loop(observed_loops: list[int]) -> bool:
        observed_loops.append(id(asyncio.get_running_loop()))
        return True

    async def test_max_concurrency_semaphores_bound_parallel_inference_calls(self) -> None:
        active = 0
        peak = 0

        async def _handler(_request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"results":[{"keep":true}]}'}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                },
            )

        class _StaticSecretResolver:
            async def resolve(self, *, secret_reference: str) -> str:
                _ = secret_reference
                return "sk-test"

        provider = ConcurrencyBoundedInferenceProvider(
            secret_resolver=_StaticSecretResolver(),
            transport=httpx.MockTransport(_handler),
        )
        profile = InferenceProfileVersion(
            version_id=str(uuid4()),
            inference_profile_id=str(uuid4()),
            version_number=1,
            provider_adapter="openai_compatible",
            base_url="https://inference.example/v1",
            model_name="qwen-vl",
            api_secret_reference=str(uuid4()),
            capabilities=("text",),
            timeout_seconds=10.0,
            max_concurrency=2,
            max_images=4,
            max_input_tokens=4096,
            structured_output_support=True,
            sampling_parameters={},
            status=VersionStatus.PUBLISHED,
        )
        manifest = InputManifest.create(
            manifest_version=1,
            target_scope=LabelScope.GLOBAL,
            execution_mode=ExecutionMode.SINGLE_MESSAGE,
            target_ids=("m-1",),
            prompt_version_id=str(uuid4()),
            structured_output_schema_hash="a" * 64,
            inference_profile_version_id=profile.version_id,
            inputs={"text": "hello"},
        )
        req = InferenceRequest(
            profile=profile,
            system_prompt="sys",
            user_prompt="usr",
            input_manifest=manifest,
            response_schema={"type": "object"},
            timeout_seconds=10.0,
        )
        await asyncio.gather(*(provider.infer(req) for _ in range(6)))
        self.assertEqual(peak, 2)
        await provider.aclose()

        # Also verify stage-level max_concurrency = 1 on ConcurrencyBoundedAnalysisOrchestrator.
        stage_active = 0
        stage_peak = 0

        class _CountingProvider:
            async def infer(self, request: InferenceRequest) -> InferenceResponse:
                nonlocal stage_active, stage_peak
                stage_active += 1
                stage_peak = max(stage_peak, stage_active)
                await asyncio.sleep(0.02)
                stage_active -= 1
                return InferenceResponse(
                    payload={"scores": {"keep": 0.9}},
                    provider_adapter=request.profile.provider_adapter,
                    model_name=request.profile.model_name,
                    raw_response={"ok": True},
                )

        label = LabelDefinitionVersion(
            version_id=str(uuid4()),
            definition_id=str(uuid4()),
            version_number=1,
            key="keep",
            display_name="Keep",
            description="keep flag",
            scope=LabelScope.GLOBAL,
            status=VersionStatus.PUBLISHED,
        )
        stage = AnalysisStageTemplateVersion(
            version_id=str(uuid4()),
            stage_template_id=str(uuid4()),
            version_number=1,
            name="text_gate",
            target_scope=LabelScope.GLOBAL,
            execution_mode=ExecutionMode.SINGLE_MESSAGE,
            prompt=PromptTemplateVersion(
                version_id=str(uuid4()),
                prompt_template_id=str(uuid4()),
                version_number=1,
                system_prompt="sys",
                user_prompt_template="{{ message_text }}",
                declared_variables=("message_text",),
                status=VersionStatus.PUBLISHED,
            ),
            label_set=LabelSetVersion(
                version_id=str(uuid4()),
                label_set_id=str(uuid4()),
                version_number=1,
                bindings=(
                    LabelBinding(
                        label=label,
                        activation_threshold=0.5,
                        output_order=0,
                    ),
                ),
                status=VersionStatus.PUBLISHED,
            ),
            structured_output_policy=StructuredOutputPolicy(),
            input_policy=StageInputPolicy(text=True, media=False),
            visual_composition_policy=VisualCompositionPolicy.RAW,
            inference_profile=profile,
            cache_policy=CachePolicy.NONE,
            timeout_seconds=10.0,
            retry_policy=StageRetryPolicy(),
            max_concurrency=1,
            status=VersionStatus.PUBLISHED,
        )
        orchestrator = ConcurrencyBoundedAnalysisOrchestrator(provider=_CountingProvider())
        from tgcurator.application.analysis.input_composer import AnalysisTargetInput

        calls = [
            orchestrator.prepare(
                stage=stage,
                targets=(
                    AnalysisTargetInput(
                        target_id=f"msg-{idx}",
                        message_id=f"msg-{idx}",
                        text="hi",
                    ),
                ),
            )
            for idx in range(4)
        ]
        await asyncio.gather(*(orchestrator.invoke(c) for c in calls))
        self.assertEqual(stage_peak, 1)
        self.assertFalse(hasattr(orchestrator, "_stage_limits_by_request"))

        # prepare() without invoke() (semantic cache hit + GC) carries metadata on prepared call
        # and never leaks or throttles subsequent wider-concurrency stage calls via reused id().
        for idx in range(32):
            cached_call = orchestrator.prepare(
                stage=stage,
                targets=(
                    AnalysisTargetInput(
                        target_id=f"cached-{idx}",
                        message_id=f"cached-{idx}",
                        text="cached",
                    ),
                ),
            )
            del cached_call
        gc.collect()

        wide_stage = AnalysisStageTemplateVersion(
            version_id=str(uuid4()),
            stage_template_id=str(uuid4()),
            version_number=1,
            name="wide_gate",
            target_scope=LabelScope.GLOBAL,
            execution_mode=ExecutionMode.SINGLE_MESSAGE,
            prompt=stage.prompt,
            label_set=stage.label_set,
            structured_output_policy=StructuredOutputPolicy(),
            input_policy=StageInputPolicy(text=True, media=False),
            visual_composition_policy=VisualCompositionPolicy.RAW,
            inference_profile=profile,
            cache_policy=CachePolicy.NONE,
            timeout_seconds=10.0,
            retry_policy=StageRetryPolicy(),
            max_concurrency=4,
            status=VersionStatus.PUBLISHED,
        )
        stage_peak = 0
        wide_calls = [
            orchestrator.prepare(
                stage=wide_stage,
                targets=(
                    AnalysisTargetInput(
                        target_id=f"wide-{idx}",
                        message_id=f"wide-{idx}",
                        text="hi",
                    ),
                ),
            )
            for idx in range(4)
        ]
        await asyncio.gather(*(orchestrator.invoke(c) for c in wide_calls))
        self.assertEqual(stage_peak, 4)
        await orchestrator.aclose()


if __name__ == "__main__":
    unittest.main()
