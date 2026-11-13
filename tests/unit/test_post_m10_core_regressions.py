from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
from PIL import Image
from telethon import functions, types

from tgcurator.application.analysis import AnalysisOrchestrator, AnalysisStageRunWorker
from tgcurator.application.analysis.input_composer import AnalysisTargetInput
from tgcurator.application.cleanup import ArchiveCleanupWorker
from tgcurator.application.media.videos import (
    VideoSamplingProfile as ReexportedVideoSamplingProfile,
)
from tgcurator.application.media.videos import candidate_timestamps
from tgcurator.application.ports.analysis import (
    AnalysisCacheHit,
    AnalysisCommitResult,
    AnalysisStageSnapshot,
    AnalysisTargetSnapshot,
    ClaimedAnalysisStageRun,
    InferenceRequest,
    InferenceResponse,
    StartedInferenceCall,
)
from tgcurator.application.ports.cleanup import (
    ArchiveCleanupFilter,
    ClaimedArchiveCleanupJob,
    ClaimedCleanupItem,
    CleanupAsset,
    CleanupBlob,
    MappingArchiveStorageResolver,
)
from tgcurator.application.ports.media import (
    ContactSheetTile,
    ImageNormalizationProfile,
    VideoSamplingProfile,
)
from tgcurator.application.ports.processing import (
    LatestMessageBoundary,
    PendingRangeExecution,
    ScheduledProcessingRange,
)
from tgcurator.application.ports.publishing import (
    ClaimedPublicationIntent,
    TelegramProtectedContent,
    TelegramPublishRequest,
    TelegramPublishResult,
)
from tgcurator.application.ports.routing import RoutingSnapshot
from tgcurator.application.processing.scheduler import ProcessingRangeScheduler
from tgcurator.application.publishing import PublicationWorker
from tgcurator.application.routing.service import build_routing_facts
from tgcurator.application.settings import Settings
from tgcurator.domain.analysis import (
    AnalysisRunStatus,
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    InferenceProfileVersion,
    InputManifest,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    MediaAssetTarget,
    MessageMediaReadiness,
    PipelineNodeSpec,
    PromptTemplateVersion,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputPolicy,
    VersionStatus,
    VisualCompositionPolicy,
    canonical_hash,
    plan_pipeline_progression,
)
from tgcurator.domain.audit import sanitize_audit_value
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
)
from tgcurator.domain.review import (
    LabelValue,
    ManualLabelEvent,
    ManualLabelOperation,
    ReviewStatus,
    resolve_label_namespaces,
)
from tgcurator.domain.routing import (
    PublicationAction,
    RoutingPolicy,
    RoutingRule,
    evaluate_condition,
)
from tgcurator.infrastructure.archive import (
    ArchivePathEscapeError,
    ArchiveStorageKeyError,
    LocalArchiveStorage,
)
from tgcurator.infrastructure.inference import OpenAICompatibleInferenceProvider
from tgcurator.infrastructure.media import FFmpegVideoFrameExtractor, PillowImageProcessor
from tgcurator.infrastructure.observability.logging import redact_for_log, redact_text
from tgcurator.infrastructure.observability.operations import OperationsMonitor
from tgcurator.infrastructure.telegram import (
    StaticTelegramSourcePeerResolver,
    TelethonMessageMapper,
    TelethonReadGateway,
)
from tgcurator.infrastructure.telegram.configured_runtime import ConfiguredTelegramClientPool
from tgcurator.infrastructure.telegram.subscriptions import (
    _extract_event_peer_id,
    _peer_id_variants,
)
from tgcurator.infrastructure.telegram.telethon_publisher import (
    StaticTelegramPublishClientResolver,
    TelethonPublisher,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)


class PostM10CoreRegressionsTests(unittest.TestCase):
    def test_settings_builds_rfc3986_encoded_database_url_for_special_char_credentials(
        self,
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_url=None,
            postgres_user="curator@admin:1",
            postgres_password="p@ss:w/rd?#%!",
            postgres_host="db.internal",
            postgres_port=5433,
            postgres_db="tg/curator",
        )
        self.assertEqual(
            settings.database_url,
            "postgresql+asyncpg://curator%40admin%3A1:p%40ss%3Aw%2Frd%3F%23%25%21"
            "@db.internal:5433/tg%2Fcurator",
        )

    def test_inference_profile_rejects_url_userinfo_and_secret_query_params(self) -> None:
        for unsafe_url in (
            "https://user:secret@api.example.com/v1",
            "https://token@api.example.com/v1",
            "https://api.example.com/v1?api_key=sk-secret",
            "https://api.example.com/v1?access_token=tok-123",
        ):
            with self.subTest(unsafe_url=unsafe_url):
                with self.assertRaises(DomainValidationError):
                    InferenceProfileVersion(
                        version_id="prof-v1",
                        inference_profile_id="prof-1",
                        version_number=1,
                        provider_adapter="openai_compatible",
                        base_url=unsafe_url,
                        model_name="qwen-vl",
                        api_secret_reference="sec-1",
                        capabilities=("text", "vision"),
                        timeout_seconds=30,
                        max_concurrency=4,
                        max_images=8,
                        max_input_tokens=4096,
                        structured_output_support=True,
                        sampling_parameters={},
                        status=VersionStatus.PUBLISHED,
                    )

    def test_logging_and_audit_redaction_preserve_safe_token_counts_and_scrub_secrets(
        self,
    ) -> None:
        payload = {
            "token_usage": {
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "total_tokens": 165,
                "input_tokens": 120,
                "output_tokens": 45,
            },
            "plaintext": "raw-secret-material",
            "client_secret": "very-secret",
            "endpoint": "https://admin:superpass@vlm.internal:8443/v1/chat",
            "bot_log": "bot error on 123456789:AAHfiqksKZ8WmR2z9pL0xQvN4bT6yU8iO1aB",
        }
        redacted_log = redact_for_log(payload)
        self.assertEqual(
            redacted_log["token_usage"],
            {
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "total_tokens": 165,
                "input_tokens": 120,
                "output_tokens": 45,
            },
        )
        self.assertEqual(redacted_log["plaintext"], "[REDACTED]")
        self.assertEqual(redacted_log["client_secret"], "[REDACTED]")
        self.assertNotIn("superpass", redacted_log["endpoint"])
        self.assertNotIn("AAHfiqksKZ8WmR2z9pL0xQvN4bT6yU8iO1aB", redacted_log["bot_log"])
        self.assertIn("[REDACTED]", redact_text(payload["bot_log"]))

        sanitized_audit = sanitize_audit_value(payload)
        self.assertEqual(sanitized_audit["token_usage"]["total_tokens"], 165)
        self.assertEqual(sanitized_audit["plaintext"], "[REDACTED]")
        self.assertEqual(sanitized_audit["client_secret"], "[REDACTED]")
        self.assertNotIn("superpass", str(sanitized_audit["endpoint"]))
        self.assertNotIn("AAHfiqksKZ8WmR2z9pL0xQvN4bT6yU8iO1aB", str(sanitized_audit["bot_log"]))

    def test_local_archive_storage_rejects_nul_bytes_and_intra_root_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            storage = LocalArchiveStorage(root=root)
            with self.assertRaises(ArchiveStorageKeyError):
                asyncio.run(storage.exists(key="safe\x00evil.webp"))

            asyncio.run(
                storage.put(
                    key="real/target.webp",
                    content=b"keep-me",
                    content_type="image/webp",
                )
            )
            link_path = root / "real" / "alias.webp"
            target_path = root / "real" / "target.webp"
            try:
                os.symlink(target_path, link_path)
            except OSError:
                self.skipTest("OS symlink privilege unavailable in current environment")

            with self.assertRaises(ArchivePathEscapeError):
                asyncio.run(storage.open(key="real/alias.webp"))
            with self.assertRaises(ArchivePathEscapeError):
                asyncio.run(storage.delete(key="real/alias.webp"))
            self.assertEqual(target_path.read_bytes(), b"keep-me")

    def test_configured_telegram_pool_enforces_expected_secret_type(self) -> None:
        secret_id = uuid4()
        identity_id = uuid4()
        resolved_calls: list[tuple[object, str | None]] = []

        class _FakeTx:
            async def __aenter__(self) -> _FakeTx:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        current_identity_type = "bot"

        class _FakeSession:
            async def __aenter__(self) -> _FakeSession:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            def begin(self) -> _FakeTx:
                return _FakeTx()

            async def get(self, *_: object, **__: object) -> None:
                return None

            async def execute(self, statement: object) -> Any:
                sql = str(statement)
                if "SELECT" in sql:
                    identity = SimpleNamespace(
                        id=identity_id,
                        enabled=True,
                        secret_id=secret_id,
                        identity_type=current_identity_type,
                    )
                    return SimpleNamespace(one_or_none=lambda: (identity, b"nonce-1"))
                return SimpleNamespace(one_or_none=lambda: None)

            async def commit(self) -> None:
                return None

        class _FakeDB:
            def session(self) -> _FakeSession:
                return _FakeSession()

        class _FakeVault:
            async def resolve(
                self,
                *,
                secret_id: object,
                expected_secret_type: str | None = None,
            ) -> bytes:
                resolved_calls.append((secret_id, expected_secret_type))
                if expected_secret_type == "bot_token":
                    return b"123456:BOT_TOKEN"
                return b"1BVtsOKtestsessionstring=="

        class _FakeTelethonClient:
            def __init__(self) -> None:
                self.connected = False

            async def connect(self) -> None:
                self.connected = True

            async def disconnect(self) -> None:
                self.connected = False

            def is_connected(self) -> bool:
                return self.connected

            async def is_user_authorized(self) -> bool:
                return True

            async def get_dialogs(self, *, limit: object = None) -> list[object]:
                return []

            async def start(self, *, bot_token: str) -> None:
                self.connected = True

        pool = ConfiguredTelegramClientPool(
            database=_FakeDB(),  # type: ignore[arg-type]
            vault=_FakeVault(),  # type: ignore[arg-type]
            settings=Settings(
                _env_file=None,
                telegram_api_id=12345,
                telegram_api_hash="hash-value",
            ),
            client_factory=lambda *_, **__: _FakeTelethonClient(),  # type: ignore[arg-type]
        )
        asyncio.run(pool.resolve_client(identity_id=str(identity_id)))
        self.assertEqual(resolved_calls, [(secret_id, "bot_token")])

        current_identity_type = "mtproto_user"
        user_identity_id = uuid4()
        identity_id = user_identity_id
        asyncio.run(pool.resolve_client(identity_id=str(user_identity_id)))
        self.assertEqual(
            resolved_calls,
            [(secret_id, "bot_token"), (secret_id, "telegram_session")],
        )

    def test_video_sampling_eof_clamp_mkv_stream_duration_sseof_fallback_and_cancellation(
        self,
    ) -> None:
        self.assertIs(ReexportedVideoSamplingProfile, VideoSamplingProfile)
        stamps = candidate_timestamps(
            duration_seconds=10.0,
            profile=VideoSamplingProfile(
                min_candidate_frames=3,
                max_candidate_frames=3,
                max_representative_frames=2,
                min_temporal_gap_seconds=5.0,
            ),
        )
        self.assertEqual(stamps, (0.0, 5.0, 10.0))

        extractor = FFmpegVideoFrameExtractor(
            ffprobe_path="safe-ffprobe",
            ffmpeg_path="safe-ffmpeg",
            timeout_seconds=1.0,
            terminate_grace_seconds=0.2,
        )
        seen_commands: list[list[str]] = []
        input_files_seen: set[Path] = set()

        def fake_popen(arguments: list[str], **kwargs: object) -> Any:
            seen_commands.append(list(arguments))
            if arguments[0] == "safe-ffprobe":
                stdout = kwargs["stdout"]
                stdout.write(  # type: ignore[union-attr]
                    b'{"streams":[{"width":1280,"height":720,"duration":"12.500"}],'
                    b'"format":{"format_name":"matroska,webm"}}'
                )
                stdout.flush()  # type: ignore[union-attr]
                return _SimpleProc()
            input_idx = arguments.index("-i") + 1
            input_files_seen.add(Path(arguments[input_idx]))
            output_path = Path(arguments[-1])
            if "-sseof" in arguments or arguments[arguments.index("-ss") + 1] == "0.000000":
                output_path.write_bytes(b"png-bytes")
            return _SimpleProc()

        with patch("tgcurator.infrastructure.media.ffmpeg.subprocess.Popen", fake_popen):
            probe = asyncio.run(extractor.probe(content=b"mkv-bytes"))
            frames = asyncio.run(
                extractor.extract_frames(
                    content=b"mkv-bytes",
                    timestamps=(0.0, 12.5),
                    duration_seconds=12.5,
                )
            )

        self.assertEqual(probe.duration_seconds, 12.5)
        self.assertEqual(probe.content_type, "video/matroska")
        self.assertEqual(frames, (b"png-bytes", b"png-bytes"))
        self.assertEqual(len(input_files_seen), 1)
        ss_seeks = [float(cmd[cmd.index("-ss") + 1]) for cmd in seen_commands if "-ss" in cmd]
        self.assertTrue(any(0.0 < seek < 12.5 for seek in ss_seeks))
        self.assertTrue(any("-sseof" in cmd for cmd in seen_commands))

        started = asyncio.Event()
        blocking_proc = _BlockingProc()

        def blocking_popen(arguments: list[str], **kwargs: object) -> Any:
            return blocking_proc

        async def cancel_in_flight() -> None:
            loop = asyncio.get_running_loop()
            blocking_proc.on_wait = lambda: loop.call_soon_threadsafe(started.set)
            task = asyncio.create_task(extractor.probe(content=b"slow-video"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch("tgcurator.infrastructure.media.ffmpeg.subprocess.Popen", blocking_popen):
            asyncio.run(cancel_in_flight())
        self.assertTrue(blocking_proc.terminated)

    def test_pillow_image_processor_separable_phash_and_contact_sheet_rendering(self) -> None:
        processor = PillowImageProcessor()
        image = Image.new("RGB", (64, 64))
        for y in range(64):
            for x in range(64):
                image.putpixel((x, y), ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256))
        buf = BytesIO()
        image.save(buf, format="PNG")
        raw_png = buf.getvalue()

        normalized = asyncio.run(
            processor.normalize(
                content=raw_png,
                profile=ImageNormalizationProfile(max_side_pixels=512, quality=82),
            )
        )
        self.assertEqual(len(normalized.perceptual_hash), 16)
        int(normalized.perceptual_hash, 16)

        inspected = processor.inspect_visual_image_sync(
            content=normalized.content, max_pixels=1_000_000
        )
        self.assertEqual((inspected.width, inspected.height), (64, 64))
        sheet = processor.render_contact_sheet_sync(
            tiles=(
                ContactSheetTile(content=normalized.content, source={"source_index": 0}),
                ContactSheetTile(content=normalized.content, source={"source_index": 1}),
            ),
            cell_size=64,
        )
        sheet_inspected = processor.inspect_visual_image_sync(
            content=sheet.content, max_pixels=1_000_000
        )
        self.assertGreater(sheet_inspected.width, 0)
        self.assertGreater(sheet_inspected.height, 0)

    def test_operations_monitor_clamps_out_of_order_heartbeat_timestamps(self) -> None:
        monitor = OperationsMonitor()
        later = NOW + timedelta(seconds=30)
        earlier = NOW + timedelta(seconds=5)
        monitor.heartbeat_scheduler(at=later, database_ready=True, broker_ready=True)
        monitor.heartbeat_scheduler(at=earlier, database_ready=True, broker_ready=True)
        snap = monitor.snapshot(now=later + timedelta(seconds=1))
        self.assertEqual(snap.scheduler.last_heartbeat_at, later)

    def test_scheduler_deduplicates_channel_boundary_and_isolates_range_errors(self) -> None:
        chan_good = str(uuid4())
        chan_bad = str(uuid4())
        profile_v = str(uuid4())
        r1 = ScheduledProcessingRange(
            range_id=str(uuid4()),
            source_channel_id=chan_good,
            source_profile_version_id=profile_v,
            end_mode="latest",
            resolved_start_message_id=1,
            resolved_fixed_end_message_id=None,
            processing_watermark_message_id=10,
            steady_after_seconds=60,
        )
        r2 = ScheduledProcessingRange(
            range_id=str(uuid4()),
            source_channel_id=chan_good,
            source_profile_version_id=profile_v,
            end_mode="latest",
            resolved_start_message_id=5,
            resolved_fixed_end_message_id=None,
            processing_watermark_message_id=10,
            steady_after_seconds=60,
        )
        r_bad = ScheduledProcessingRange(
            range_id=str(uuid4()),
            source_channel_id=chan_bad,
            source_profile_version_id=profile_v,
            end_mode="latest",
            resolved_start_message_id=1,
            resolved_fixed_end_message_id=None,
            processing_watermark_message_id=1,
            steady_after_seconds=60,
        )
        boundary_calls: list[str] = []
        created_ranges: list[str] = []

        class _Boundary:
            async def newest_message(
                self, *, source_channel_id: str
            ) -> LatestMessageBoundary | None:
                boundary_calls.append(source_channel_id)
                if source_channel_id == chan_bad:
                    raise RuntimeError("telegram RPC timeout")
                return LatestMessageBoundary(message_id=25, published_at=NOW - timedelta(minutes=5))

        class _ScheduleRepo:
            async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
                return (r_bad, r1, r2)

            async def create_execution_and_wakeup(
                self, *, execution: PendingRangeExecution, queue: str, available_at: datetime
            ) -> bool:
                created_ranges.append(execution.processing_range_id)
                return True

        scheduler = ProcessingRangeScheduler(
            _ScheduleRepo(),
            _Boundary(),
        )
        summary = asyncio.run(scheduler.schedule(now=NOW))
        self.assertEqual(boundary_calls, [chan_bad, chan_good])
        self.assertEqual(created_ranges, [r1.range_id, r2.range_id])
        self.assertEqual(summary.failed_ranges, 1)

    def test_telegram_peer_variants_and_service_message_skip(self) -> None:
        variants = _peer_id_variants(-1001234567890)
        self.assertIn(-1001234567890, variants)
        self.assertIn(1234567890, variants)
        self.assertIn(-1234567890, variants)
        event = SimpleNamespace(
            chat_id=None,
            message=SimpleNamespace(peer_id=SimpleNamespace(channel_id=1234567890)),
        )
        self.assertEqual(_extract_event_peer_id(event), -1001234567890)

        source_channel_id = str(uuid4())
        service_msg = SimpleNamespace(
            id=10,
            date=NOW,
            action=SimpleNamespace(kind="pin"),
            raw_text=None,
            grouped_id=None,
        )
        normal_msg = SimpleNamespace(
            id=11,
            date=NOW,
            action=None,
            raw_text="hello",
            grouped_id=None,
            edit_date=None,
            photo=None,
            video=None,
            audio=None,
            document=None,
            post=None,
            silent=None,
            legacy=None,
            edit_hide=None,
            pinned=None,
            noforwards=False,
            views=None,
            forwards=None,
            via_bot_id=None,
            post_author=None,
        )

        class _FakeReadClient:
            async def iter_messages(self, *_: object, **__: object) -> Any:
                for item in (service_msg, normal_msg):
                    yield item

        gateway = TelethonReadGateway(
            client=_FakeReadClient(),  # type: ignore[arg-type]
            source_peer_resolver=StaticTelegramSourcePeerResolver(
                {source_channel_id: -1001234567890}
            ),
            message_mapper=TelethonMessageMapper(),
        )
        collected = asyncio.run(
            gateway.fetch_history(
                source_channel_id=source_channel_id,
                from_message_id_exclusive=0,
                to_message_id_inclusive=20,
            )
        )
        self.assertEqual([m.telegram_message_id for m in collected], [11])

    def test_progression_fails_closed_when_media_is_deleting_or_deleted(self) -> None:
        node = PipelineNodeSpec(
            node_id="node-1",
            node_key="vision_stage",
            stage_version_id="stv-1",
            target_scope=LabelScope.MEDIA,
            input_requires_media=True,
            is_text_only_prescreen=False,
            cache_policy=CachePolicy.ASSET,
            max_attempts=3,
        )
        for unavailable_state in ("deleting", "deleted"):
            plan = plan_pipeline_progression(
                message_id="msg-1",
                nodes=(node,),
                existing_runs_by_node={},
                media=MessageMediaReadiness(
                    assets=(
                        MediaAssetTarget(
                            target_kind="image_asset",
                            asset_id="img-1",
                            archive_state=unavailable_state,
                        ),
                    )
                ),
                facts={},
                blocked_from_analysis=False,
            )
            self.assertEqual(plan.run_status, AnalysisRunStatus.FAILED)

    def test_asset_cache_key_uses_content_sha256_and_skips_deleted_blob_open_on_hit(self) -> None:
        sha = hashlib.sha256(b"identical-webp").hexdigest()
        stage = _media_stage(
            execution_mode=ExecutionMode.PER_ASSET,
            cache_policy=CachePolicy.ASSET,
        )

        class _NeverOpenStorage:
            async def open(self, *, key: str) -> bytes:
                raise AssertionError(f"storage.open should not be called on cache hit: {key}")

        orchestrator = AnalysisOrchestrator(
            provider=_StaticProvider(),
            storage_resolver=MappingArchiveStorageResolver({"archive": _NeverOpenStorage()}),  # type: ignore[dict-item]
            visual_backend=PillowImageProcessor(),
        )
        target_a = AnalysisTargetInput(
            target_id="img-a",
            message_id="msg-a",
            asset_id="img-a",
            media=(
                {
                    "asset_id": "img-a",
                    "media_type": "image",
                    "archive_state": "deleted",
                    "storage_backend": "archive",
                    "storage_key": "a.webp",
                    "content_type": "image/webp",
                    "width": 64,
                    "height": 64,
                    "size_bytes": 128,
                    "archive_sha256": sha,
                    "perceptual_hash": "0123456789abcdef",
                },
            ),
            visual_identity=f"sha256:{sha}",
        )
        target_b = AnalysisTargetInput(
            target_id="img-b",
            message_id="msg-b",
            asset_id="img-b",
            media=(
                {
                    "asset_id": "img-b",
                    "media_type": "image",
                    "archive_state": "ready",
                    "storage_backend": "archive",
                    "storage_key": "b.webp",
                    "content_type": "image/webp",
                    "width": 64,
                    "height": 64,
                    "size_bytes": 128,
                    "archive_sha256": sha,
                    "perceptual_hash": "fedcba9876543210",
                },
            ),
            visual_identity=f"sha256:{sha}",
        )
        probe_a = orchestrator.prepare_cache_probe(stage=stage, targets=(target_a,))
        probe_b = orchestrator.prepare_cache_probe(stage=stage, targets=(target_b,))
        self.assertIsNotNone(probe_a)
        self.assertIsNotNone(probe_b)
        assert probe_a is not None and probe_b is not None
        assert probe_a.cache_key is not None and probe_b.cache_key is not None
        self.assertEqual(probe_a.cache_key.value, probe_b.cache_key.value)

        claim = ClaimedAnalysisStageRun(
            stage_run_id="sr-b",
            analysis_run_id="run-b",
            message_id="msg-b",
            target_id="img-b",
            analysis_stage_template_version_id=stage.version_id,
            lease_token="lease-b",
            attempt_count=1,
            max_attempts=3,
            run_mode="formal",
        )
        snap = AnalysisStageSnapshot(
            claim=claim,
            stage=stage,
            targets=(
                AnalysisTargetSnapshot(
                    stage_run_id="sr-b",
                    lease_token="lease-b",
                    target_id="img-b",
                    message_id="msg-b",
                    target_kind="image_asset",
                    asset_id="img-b",
                    analysis_run_id="run-b",
                    media=target_b.media,
                    visual_identity=sha,
                ),
            ),
            source_channel_context={},
        )
        cache_commits: list[str] = []

        class _HitRepo:
            async def claim(self, **_: object) -> ClaimedAnalysisStageRun:
                return claim

            async def load_stage_snapshot(self, **_: object) -> AnalysisStageSnapshot:
                return snap

            async def find_cache_hit(self, **_: object) -> AnalysisCacheHit:
                return AnalysisCacheHit("donor-stage", {"scores": {"quality": 0.9}})

            async def commit_cache_hit(self, **_: object) -> AnalysisCommitResult:
                cache_commits.append("img-b")
                return AnalysisCommitResult(("img-b",))

            async def complete_analysis_run(self, **_: object) -> bool:
                return True

        hit_worker = AnalysisStageRunWorker(
            repository=_HitRepo(),  # type: ignore[arg-type]
            orchestrator=orchestrator,
        )
        self.assertTrue(asyncio.run(hit_worker.process(stage_run_id="sr-b", now=NOW)))
        self.assertEqual(cache_commits, ["img-b"])

        target_phash_only = AnalysisTargetInput(
            target_id="img-c",
            message_id="msg-c",
            asset_id="img-c",
            media=(
                {
                    "asset_id": "img-c",
                    "media_type": "image",
                    "archive_state": "ready",
                    "storage_backend": "archive",
                    "storage_key": "c.webp",
                    "content_type": "image/webp",
                    "width": 64,
                    "height": 64,
                    "size_bytes": 128,
                    "perceptual_hash": "0123456789abcdef",
                },
            ),
            visual_identity=None,
        )
        self.assertIsNone(
            orchestrator.prepare_cache_probe(stage=stage, targets=(target_phash_only,))
        )

    def test_batch_prepare_isolates_corrupt_target_without_failing_healthy_sibling(self) -> None:
        good_png = BytesIO()
        Image.new("RGB", (16, 16), (10, 20, 30)).save(good_png, format="PNG")
        good_bytes = good_png.getvalue()
        sha_good = hashlib.sha256(good_bytes).hexdigest()
        sha_bad = hashlib.sha256(b"corrupt").hexdigest()

        stage = _media_stage(
            execution_mode=ExecutionMode.BATCH_ASSETS,
            cache_policy=CachePolicy.NONE,
        )
        claim = ClaimedAnalysisStageRun(
            stage_run_id="sr-good",
            analysis_run_id="run-good",
            message_id="msg-good",
            target_id="img-good",
            analysis_stage_template_version_id=stage.version_id,
            lease_token="lease-good",
            attempt_count=1,
            max_attempts=3,
            run_mode="formal",
        )
        batch_snap = AnalysisStageSnapshot(
            claim=claim,
            stage=stage,
            targets=(
                AnalysisTargetSnapshot(
                    stage_run_id="sr-good",
                    lease_token="lease-good",
                    target_id="img-good",
                    message_id="msg-good",
                    target_kind="image_asset",
                    asset_id="img-good",
                    analysis_run_id="run-good",
                    media=(
                        {
                            "asset_id": "img-good",
                            "media_type": "image",
                            "archive_state": "ready",
                            "storage_backend": "archive",
                            "storage_key": "good.png",
                            "content_type": "image/png",
                            "width": 16,
                            "height": 16,
                            "size_bytes": len(good_bytes),
                            "archive_sha256": sha_good,
                        },
                    ),
                ),
                AnalysisTargetSnapshot(
                    stage_run_id="sr-bad",
                    lease_token="lease-bad",
                    target_id="img-bad",
                    message_id="msg-bad",
                    target_kind="image_asset",
                    asset_id="img-bad",
                    analysis_run_id="run-bad",
                    media=(
                        {
                            "asset_id": "img-bad",
                            "media_type": "image",
                            "archive_state": "ready",
                            "storage_backend": "archive",
                            "storage_key": "corrupt.png",
                            "content_type": "image/png",
                            "width": 16,
                            "height": 16,
                            "size_bytes": 7,
                            "archive_sha256": sha_bad,
                        },
                    ),
                ),
            ),
            source_channel_context={},
        )

        class _MemStorage:
            async def size(self, *, key: str) -> int:
                return len(good_bytes) if key == "good.png" else 12

            async def open(self, *, key: str) -> bytes:
                if key == "good.png":
                    return good_bytes
                return b"not-an-image"

        retried_targets: list[tuple[str, ...]] = []
        committed_targets: list[tuple[str, ...]] = []

        class _BatchRepo:
            async def claim(self, **_: object) -> ClaimedAnalysisStageRun:
                return claim

            async def load_stage_snapshot(
                self, *, claim: ClaimedAnalysisStageRun
            ) -> AnalysisStageSnapshot:
                return batch_snap

            async def find_cache_hit(self, **_: object) -> AnalysisCacheHit | None:
                return None

            async def retry_or_fail_targets(self, **kwargs: Any) -> dict[str, str]:
                target_ids = tuple(kwargs["target_ids"])
                retried_targets.append(target_ids)
                return {tid: "retry_wait" for tid in target_ids}

            async def start_inference(self, **_: object) -> StartedInferenceCall:
                return StartedInferenceCall(
                    inference_call_id="call-1",
                    input_manifest_id="man-1",
                )

            async def record_inference_success(self, **_: object) -> None:
                return None

            async def commit_validated_targets(self, **kwargs: Any) -> AnalysisCommitResult:
                tids = tuple(r.target_id for r in kwargs["results"])
                committed_targets.append(tids)
                return AnalysisCommitResult(
                    committed_target_ids=tids,
                    negative_gate_assignments=(),
                )

            async def apply_negative_gate(self, **_: object) -> bool:
                return False

            async def complete_analysis_run(self, **_: object) -> bool:
                return False

        provider = _StaticProvider(
            payload={"results": [{"asset_id": "img-good", "scores": {"quality": 0.9}}]}
        )
        worker = AnalysisStageRunWorker(
            repository=_BatchRepo(),  # type: ignore[arg-type]
            orchestrator=AnalysisOrchestrator(
                provider=provider,
                storage_resolver=MappingArchiveStorageResolver({"archive": _MemStorage()}),  # type: ignore[dict-item]
                visual_backend=PillowImageProcessor(),
            ),
        )
        handled = asyncio.run(worker.process(stage_run_id="sr-good", now=NOW))
        self.assertTrue(handled)
        self.assertEqual(retried_targets, [("img-bad",)])
        self.assertEqual(committed_targets, [("img-good",)])

    def test_openai_provider_reuses_pooled_async_client_and_closes_cleanly(self) -> None:
        seen_requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "model": "qwen-vl",
                    "choices": [{"message": {"content": '{"scores": {"quality": 0.8}}'}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                },
            )

        provider = OpenAICompatibleInferenceProvider(
            secret_resolver=SimpleNamespace(resolve=lambda **_: _async_return("sk-test")),
            transport=httpx.MockTransport(handler),
        )
        req = InferenceRequest(
            profile=InferenceProfileVersion(
                version_id="pv-1",
                inference_profile_id="p-1",
                version_number=1,
                provider_adapter="openai_compatible",
                base_url="https://api.example.com/v1",
                model_name="qwen-vl",
                api_secret_reference="sec-1",
                capabilities=("text",),
                timeout_seconds=10,
                max_concurrency=2,
                max_images=4,
                max_input_tokens=4096,
                structured_output_support=True,
                sampling_parameters={},
                status=VersionStatus.PUBLISHED,
            ),
            system_prompt="sys",
            user_prompt="usr",
            input_manifest=InputManifest(
                manifest_version=2,
                content_json='{"manifest_version":2,"targets":[]}',
                input_manifest_hash=canonical_hash({"manifest_version": 2, "targets": []}),
            ),
            response_schema={"type": "object", "properties": {"scores": {"type": "object"}}},
            timeout_seconds=10.0,
        )

        async def run_twice() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
            await provider.infer(req)
            first_client = provider._client
            assert first_client is not None
            await provider.infer(req)
            second_client = provider._client
            assert second_client is not None
            await provider.aclose()
            return first_client, second_client

        c1, c2 = asyncio.run(run_twice())
        self.assertIs(c1, c2)
        self.assertTrue(c1.is_closed)
        self.assertEqual(len(seen_requests), 2)

    def test_review_cross_version_label_key_override_and_routing_numeric_facts(self) -> None:
        msg_id = str(uuid4())
        ldv_v1 = str(uuid4())
        ldv_v2 = str(uuid4())
        ldv_v3 = str(uuid4())
        model_v1 = LabelValue(
            message_id=msg_id,
            target_scope="global",
            target_kind="message",
            target_id=msg_id,
            label_definition_version_id=ldv_v1,
            label_key="nsfw",
            score=0.95,
            activated=True,
        )
        manual_set_v2 = ManualLabelEvent(
            event_id=str(uuid4()),
            message_id=msg_id,
            target_scope="global",
            target_kind="message",
            target_id=msg_id,
            label_definition_version_id=ldv_v2,
            label_key="nsfw",
            operation=ManualLabelOperation.SET,
            score=0.1,
            activated=False,
            actor_admin_user_id=str(uuid4()),
            reason="false positive after v2 publish",
            created_at=NOW,
        )
        resolved = resolve_label_namespaces((model_v1,), (manual_set_v2,))
        self.assertEqual(len(resolved.effective), 1)
        self.assertFalse(resolved.effective[0].activated)
        self.assertEqual(resolved.effective[0].score, 0.1)

        manual_clear_v3 = ManualLabelEvent(
            event_id=str(uuid4()),
            message_id=msg_id,
            target_scope="global",
            target_kind="message",
            target_id=msg_id,
            label_definition_version_id=ldv_v3,
            label_key="nsfw",
            operation=ManualLabelOperation.CLEAR,
            actor_admin_user_id=str(uuid4()),
            reason="revert override",
            created_at=NOW + timedelta(seconds=10),
        )
        reverted = resolve_label_namespaces((model_v1,), (manual_set_v2, manual_clear_v3))
        self.assertEqual(len(reverted.effective), 1)
        self.assertTrue(reverted.effective[0].activated)
        self.assertEqual(reverted.effective[0].label_definition_version_id, ldv_v1)

        dst_id = str(uuid4())
        snapshot = RoutingSnapshot(
            message_id=msg_id,
            message={
                "id": msg_id,
                "blocked_from_analysis": False,
                "original_text": "hello",
                "primary_telegram_message_id": 42,
                "media_count": 0,
            },
            source_channel={"id": "source-1", "username": "source"},
            duplicate_state={"status": "unique"},
            media=(),
            review_status=ReviewStatus.REVIEWED,
            model_assignments=(model_v1,),
            manual_events=(manual_set_v2,),
            policy=RoutingPolicy(
                policy_version_id=str(uuid4()),
                rules=(
                    RoutingRule(
                        rule_id=str(uuid4()),
                        priority=1,
                        condition={"fact": "manual.global.nsfw", "op": "eq", "value": 0.1},
                        actions=(
                            PublicationAction(
                                action_id=str(uuid4()),
                                destination_channel_id=dst_id,
                                publication_mode="forward_only",
                            ),
                        ),
                    ),
                ),
            ),
            destination_publish_identity_ids={dst_id: str(uuid4())},
            rendering_template_texts={},
        )
        facts = build_routing_facts(snapshot)
        self.assertEqual(facts["model"]["global"]["nsfw"], 0.95)
        self.assertEqual(facts["manual"]["global"]["nsfw"], 0.1)
        self.assertEqual(facts["effective"]["global"]["nsfw"], 0.1)
        self.assertTrue(
            evaluate_condition({"fact": "score", "op": "eq", "value": 1}, {"score": 1.0})
        )
        self.assertFalse(
            evaluate_condition({"fact": "flag", "op": "eq", "value": 1}, {"flag": True})
        )

    def test_telethon_publisher_chat_noforwards_text_copy_and_cross_identity_reupload(
        self,
    ) -> None:
        pub_id = str(uuid4())
        pub_bot_id = str(uuid4())
        read_user_id = str(uuid4())
        protected_chat_msg = SimpleNamespace(
            id=11,
            media=None,
            noforwards=False,
            chat=SimpleNamespace(noforwards=True),
        )
        pub_client = _PubClient(
            responses=[SimpleNamespace(id=501, random_id=77, updates=None)],
            messages=(protected_chat_msg,),
        )
        publisher = TelethonPublisher(
            client_resolver=StaticTelegramPublishClientResolver({pub_id: pub_client}),
            clock=lambda: NOW,
        )
        with self.assertRaises(TelegramProtectedContent):
            asyncio.run(
                publisher.publish(
                    request=TelegramPublishRequest(
                        intent_id=str(uuid4()),
                        part=PublicationPart.MAIN,
                        operation=PublicationOperation.COPY_WITH_CAPTION,
                        identity_id=pub_id,
                        source_peer_id=-1001,
                        source_message_ids=(11,),
                        destination_peer_id=-1002,
                        random_ids=(77,),
                        text="caption",
                    )
                )
            )

        text_only_msg = SimpleNamespace(id=11, media=None, noforwards=False, chat=None)
        text_client = _PubClient(
            responses=[SimpleNamespace(id=502, random_id=78, updates=None)],
            messages=(text_only_msg,),
        )
        text_pub = TelethonPublisher(
            client_resolver=StaticTelegramPublishClientResolver({pub_id: text_client}),
            clock=lambda: NOW,
        )
        res_text = asyncio.run(
            text_pub.publish(
                request=TelegramPublishRequest(
                    intent_id=str(uuid4()),
                    part=PublicationPart.MAIN,
                    operation=PublicationOperation.COPY_WITH_CAPTION,
                    identity_id=pub_id,
                    source_peer_id=-1001,
                    source_message_ids=(11,),
                    destination_peer_id=-1002,
                    random_ids=(78,),
                    text="text fallback",
                )
            )
        )
        self.assertEqual(res_text.remote_message_ids, (502,))
        self.assertIsInstance(text_client.requests[0], functions.messages.SendMessageRequest)

        photo_media = types.InputMediaPhoto(
            id=types.InputPhoto(id=11, access_hash=999, file_reference=b"ref")
        )
        read_msg = SimpleNamespace(
            id=11,
            media=photo_media,
            noforwards=False,
            chat=None,
            file=SimpleNamespace(name="photo.jpg", mime_type="image/jpeg"),
        )
        read_client = _PubClient(responses=[], messages=(read_msg,), downloaded=b"jpeg-bytes")
        bot_pub_client = _PubClient(
            responses=[SimpleNamespace(updates=[SimpleNamespace(random_id=79, id=601)])],
            messages=(),
        )
        cross_pub = TelethonPublisher(
            client_resolver=StaticTelegramPublishClientResolver(
                {pub_bot_id: bot_pub_client, read_user_id: read_client}
            ),
            clock=lambda: NOW,
        )
        res_cross = asyncio.run(
            cross_pub.publish(
                request=TelegramPublishRequest(
                    intent_id=str(uuid4()),
                    part=PublicationPart.MAIN,
                    operation=PublicationOperation.COPY_WITH_CAPTION,
                    identity_id=pub_bot_id,
                    source_read_identity_id=read_user_id,
                    source_peer_id=-1001,
                    source_message_ids=(11,),
                    destination_peer_id=-1002,
                    random_ids=(79,),
                    text="re-uploaded caption",
                )
            )
        )
        self.assertEqual(res_cross.remote_message_ids, (601,))
        self.assertEqual(len(bot_pub_client.uploads), 1)
        self.assertIsInstance(bot_pub_client.requests[0], functions.messages.SendMediaRequest)

    def test_publication_worker_metadata_only_degradation_completes_in_single_part(self) -> None:
        intent_id = str(uuid4())
        claimed = ClaimedPublicationIntent(
            intent_id=intent_id,
            attempt_id=str(uuid4()),
            lease_token=str(uuid4()),
            attempt_number=1,
            max_attempts=3,
            message_id=str(uuid4()),
            source_channel_id=str(uuid4()),
            source_peer_id=-1001,
            source_message_ids=(10,),
            destination_channel_id=str(uuid4()),
            destination_peer_id=-1002,
            publish_identity_id=str(uuid4()),
            mode=PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT,
            protected_content_fallback=ProtectedContentFallback.METADATA_ONLY,
            rendered_text="summary metadata",
            main_random_ids=(1001,),
            supplement_random_id=1002,
            completed_parts=(),
            main_remote_message_ids=(),
            in_flight_part=None,
            in_flight_operation=None,
            source_read_identity_id=str(uuid4()),
            main_degraded_to_text=False,
        )
        recorded_parts: list[tuple[PublicationPart, PublicationOperation]] = []
        completed_calls: list[str] = []

        class _PubRepo:
            async def claim(self, **_: object) -> ClaimedPublicationIntent:
                return claimed

            async def begin_part(self, **_: object) -> bool:
                return True

            async def record_part(self, **kwargs: Any) -> bool:
                recorded_parts.append((kwargs["part"], kwargs["operation"]))
                return True

            async def complete(self, **kwargs: Any) -> bool:
                completed_calls.append(kwargs["claim"].intent_id)
                return True

            async def complete_wakeup_if_terminal(self, **_: object) -> bool:
                return True

        class _ProtectedThenTextPublisher:
            async def publish(self, *, request: TelegramPublishRequest) -> TelegramPublishResult:
                if request.operation is PublicationOperation.NATIVE_FORWARD:
                    raise TelegramProtectedContent()
                return TelegramPublishResult((9001,))

            async def reconcile(
                self, *, request: TelegramPublishRequest
            ) -> TelegramPublishResult | None:
                return None

        worker = PublicationWorker(
            repository=_PubRepo(),  # type: ignore[arg-type]
            publisher=_ProtectedThenTextPublisher(),
        )
        handled = asyncio.run(worker.process(intent_id=intent_id, now=NOW))
        self.assertTrue(handled)
        self.assertEqual(
            recorded_parts,
            [(PublicationPart.MAIN, PublicationOperation.SEND_TEXT)],
        )
        self.assertEqual(completed_calls, [intent_id])

    def test_cleanup_filter_duplicate_aliases_and_worker_wall_clock_progression(self) -> None:
        flt = ArchiveCleanupFilter(
            duplicate_statuses=("duplicate_skipped", "duplicate_reclassify", "duplicate_analyzed")
        )
        self.assertEqual(flt.duplicate_statuses, ("duplicate",))

        seen_timestamps: list[datetime] = []
        job_id = str(uuid4())
        claim = ClaimedArchiveCleanupJob(
            job_id=job_id,
            lease_token=str(uuid4()),
            attempt_number=1,
            max_attempts=3,
        )
        items = [
            ClaimedCleanupItem(
                item_id=str(uuid4()),
                deletion_token=str(uuid4()),
                asset=CleanupAsset(
                    asset_kind="image",
                    asset_id=str(uuid4()),
                    message_id=str(uuid4()),
                    blobs=(
                        CleanupBlob(
                            storage_backend="archive", storage_key="k1.webp", size_bytes=10
                        ),
                    ),
                    previous_archive_size=10,
                ),
            ),
            ClaimedCleanupItem(
                item_id=str(uuid4()),
                deletion_token=str(uuid4()),
                asset=CleanupAsset(
                    asset_kind="image",
                    asset_id=str(uuid4()),
                    message_id=str(uuid4()),
                    blobs=(
                        CleanupBlob(
                            storage_backend="archive", storage_key="k2.webp", size_bytes=20
                        ),
                    ),
                    previous_archive_size=20,
                ),
            ),
        ]

        class _CleanupRepo:
            async def claim(self, **kwargs: Any) -> ClaimedArchiveCleanupJob:
                seen_timestamps.append(kwargs["now"])
                return claim

            async def claim_next_item(self, **kwargs: Any) -> ClaimedCleanupItem | None:
                seen_timestamps.append(kwargs["now"])
                return items.pop(0) if items else None

            async def finalize_item_deleted(self, **kwargs: Any) -> bool:
                seen_timestamps.append(kwargs["now"])
                return True

            async def complete(self, **kwargs: Any) -> bool:
                seen_timestamps.append(kwargs["now"])
                return True

            async def complete_wakeup_if_terminal(self, **_: Any) -> bool:
                return True

        class _NoopStorage:
            async def delete(self, *, key: str) -> None:
                return None

        tick = 0

        def advancing_clock() -> datetime:
            nonlocal tick
            tick += 5
            return NOW + timedelta(seconds=tick)

        worker = ArchiveCleanupWorker(
            repository=_CleanupRepo(),  # type: ignore[arg-type]
            storage_resolver=MappingArchiveStorageResolver({"archive": _NoopStorage()}),  # type: ignore[dict-item]
            clock=advancing_clock,
        )
        asyncio.run(worker.process(job_id=job_id, now=NOW))
        self.assertGreater(seen_timestamps[-1], seen_timestamps[0])


class _SimpleProc:
    def poll(self) -> int | None:
        return 0

    def wait(self, timeout: float) -> int:
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


class _BlockingProc:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.on_wait: Any = None

    def poll(self) -> int | None:
        return -15 if (self.terminated or self.killed) else None

    def wait(self, timeout: float) -> int:
        if self.on_wait is not None:
            cb, self.on_wait = self.on_wait, None
            cb()
        import time

        deadline = time.monotonic() + timeout
        while not self.terminated and time.monotonic() < deadline:
            time.sleep(0.005)
        if not self.terminated:
            raise subprocess.TimeoutExpired("ffprobe", timeout)
        return -15

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _StaticProvider:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {"scores": {"quality": 0.9}}

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(
            payload=self.payload,
            provider_adapter="openai_compatible",
            model_name="test-vlm",
            raw_response={"ok": True},
            latency_ms=1.0,
            http_status=200,
            token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class _PubClient:
    def __init__(
        self,
        *,
        responses: list[Any],
        messages: tuple[Any, ...],
        downloaded: bytes = b"",
    ) -> None:
        self.responses = list(responses)
        self.messages = messages
        self.downloaded = downloaded
        self.requests: list[Any] = []
        self.uploads: list[tuple[bytes, str | None]] = []

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return self.responses.pop(0)

    async def get_messages(self, entity: object, *, ids: object) -> Any:
        return self.messages

    async def download_media(self, message: object, *, file: type[bytes]) -> bytes:
        return self.downloaded

    async def upload_file(self, data: bytes, *, file_name: str | None = None) -> Any:
        self.uploads.append((data, file_name))
        return types.InputFile(id=1, parts=1, name=file_name or "media.bin", md5_checksum="")


async def _async_return(value: Any) -> Any:
    return value


async def _collect_async(async_iter: Any) -> list[Any]:
    return [item async for item in async_iter]


def _media_stage(
    *,
    execution_mode: ExecutionMode,
    cache_policy: CachePolicy,
) -> AnalysisStageTemplateVersion:
    label_def = LabelDefinitionVersion(
        version_id="quality-v1",
        definition_id="quality",
        version_number=1,
        key="quality",
        display_name="Quality",
        description="Image quality",
        scope=LabelScope.MEDIA,
        status=VersionStatus.PUBLISHED,
    )
    label_set = LabelSetVersion(
        version_id="lsv-1",
        label_set_id="ls-1",
        version_number=1,
        bindings=(LabelBinding(label_def, 0.5, 0),),
        status=VersionStatus.PUBLISHED,
    )
    prompt = PromptTemplateVersion(
        version_id="ptv-1",
        prompt_template_id="pt-1",
        version_number=1,
        system_prompt="Evaluate {{ label_definitions }}",
        user_prompt_template="Inspect media",
        declared_variables=("label_definitions",),
        status=VersionStatus.PUBLISHED,
    )
    profile = InferenceProfileVersion(
        version_id="ipv-1",
        inference_profile_id="ip-1",
        version_number=1,
        provider_adapter="openai_compatible",
        base_url="https://api.example.com/v1",
        model_name="qwen-vl",
        api_secret_reference="sec-1",
        capabilities=("vision",),
        timeout_seconds=30,
        max_concurrency=2,
        max_images=8,
        max_input_tokens=4096,
        structured_output_support=True,
        sampling_parameters={},
        status=VersionStatus.PUBLISHED,
    )
    return AnalysisStageTemplateVersion(
        version_id="stv-1",
        stage_template_id="st-1",
        version_number=1,
        name="media_quality",
        target_scope=LabelScope.MEDIA,
        execution_mode=execution_mode,
        prompt=prompt,
        label_set=label_set,
        structured_output_policy=StructuredOutputPolicy(),
        input_policy=StageInputPolicy(text=False, media=True),
        visual_composition_policy=VisualCompositionPolicy.ADAPTIVE,
        inference_profile=profile,
        cache_policy=cache_policy,
        timeout_seconds=20,
        retry_policy=StageRetryPolicy(max_attempts=3),
        max_batch_size=8 if execution_mode is ExecutionMode.BATCH_ASSETS else 1,
        max_concurrency=2,
        status=VersionStatus.PUBLISHED,
    )


if __name__ == "__main__":
    unittest.main()
