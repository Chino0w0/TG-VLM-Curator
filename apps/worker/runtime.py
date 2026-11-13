from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import threading
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from apps.runtime_dependencies import (
    resolve_database_url,
    upsert_service_heartbeat,
    write_heartbeat_file,
)
from tgcurator.application import MessagePipelineCoordinator, Settings, get_settings
from tgcurator.application.analysis import AnalysisOrchestrator, AnalysisStageRunWorker
from tgcurator.application.analysis.orchestrator import (
    AnalysisInvocationResult,
    PreparedAnalysisCall,
)
from tgcurator.application.cleanup import ArchiveCleanupWorker
from tgcurator.application.media import ImageArchiveWorker, VideoArchiveWorker
from tgcurator.application.ports.analysis import (
    InferenceImage,
    InferenceRequest,
    InferenceResponse,
)
from tgcurator.application.ports.operations import OperationsTelemetry
from tgcurator.application.processing import RangeExecutionHistoryIngestion, RangeExecutionWorker
from tgcurator.application.publishing import PublicationWorker
from tgcurator.application.routing import RoutingService
from tgcurator.domain.analysis import AnalysisStageTemplateVersion
from tgcurator.infrastructure.archive import LocalArchiveStorage
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyAnalysisStageRunRepository,
    SqlAlchemyArchiveCleanupRepository,
    SqlAlchemyMessagePipelineRepository,
    SqlAlchemyRangeExecutionWorkerRepository,
    SqlAlchemyRoutingRepository,
)
from tgcurator.infrastructure.inference import OpenAICompatibleInferenceProvider
from tgcurator.infrastructure.media import FFmpegVideoFrameExtractor
from tgcurator.infrastructure.observability import OperationsMonitor
from tgcurator.shared import DomainValidationError

logger = logging.getLogger(__name__)

_EXTRACTOR_LOCK = threading.Lock()
_REGISTERED_EXTRACTORS: weakref.WeakValueDictionary[int, FFmpegVideoFrameExtractor] = (
    weakref.WeakValueDictionary()
)
_FALLBACK_EXTRACTORS: dict[int, FFmpegVideoFrameExtractor] = {}


class _WeakRefFFmpegVideoFrameExtractor(FFmpegVideoFrameExtractor):
    """Weakref-capable subclass so unclosed extractors never leak in _REGISTERED_EXTRACTORS."""


def register_media_extractor(extractor: FFmpegVideoFrameExtractor) -> FFmpegVideoFrameExtractor:
    """Register an FFmpeg extractor so worker SIGTERM hooks can stop active child processes."""

    key = id(extractor)
    with _EXTRACTOR_LOCK:
        try:
            _REGISTERED_EXTRACTORS[key] = extractor
        except TypeError:
            _FALLBACK_EXTRACTORS[key] = extractor
    return extractor


def terminate_registered_media_extractors() -> int:
    """Terminate all active ffprobe/ffmpeg child processes across registered extractors."""

    with _EXTRACTOR_LOCK:
        extractors_by_id = {
            **{key: item for key, item in _FALLBACK_EXTRACTORS.items()},
            **{key: item for key, item in _REGISTERED_EXTRACTORS.items()},
        }
        extractors = list(extractors_by_id.values())
    return sum(item.terminate_active_processes() for item in extractors)


def sweep_stale_media_temp_entries(temp_dir: Path | None) -> int:
    """Remove leftover `tgcurator-ff*` temporary directories/files on media worker startup."""

    if temp_dir is None:
        return 0
    target_dir = Path(temp_dir).expanduser()
    if not target_dir.is_dir():
        return 0
    swept = 0
    try:
        entries = list(target_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith("tgcurator-ff"):
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=False)
            else:
                entry.unlink(missing_ok=True)
            swept += 1
        except OSError:
            logger.warning("Could not remove stale temp media path %s", entry, exc_info=True)
    return swept


class ConcurrencyBoundedInferenceProvider(OpenAICompatibleInferenceProvider):
    """Enforces per-InferenceProfile `max_concurrency` on the worker's event loop."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._semaphores: dict[tuple[int, str, int], asyncio.Semaphore] = {}

    def _semaphore_for(self, request: InferenceRequest) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        raw_limit = getattr(request.profile, "max_concurrency", 1)
        limit = max(1, int(raw_limit) if isinstance(raw_limit, int) else 1)
        profile_key = str(
            getattr(request.profile, "inference_profile_id", None)
            or getattr(request.profile, "profile_id", None)
            or getattr(request.profile, "version_id", None)
            or f"{request.profile.base_url}|{request.profile.model_name}"
        )
        key = (loop_id, profile_key, limit)
        semaphore = self._semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            self._semaphores[key] = semaphore
        return semaphore

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        semaphore = self._semaphore_for(request)
        async with semaphore:
            return await super().infer(request)

    async def aclose(self) -> None:
        self._semaphores.clear()
        parent_close = getattr(super(), "aclose", None)
        if callable(parent_close):
            await parent_close()


@dataclass(frozen=True, slots=True)
class _BoundedPreparedAnalysisCall(PreparedAnalysisCall):
    stage_key: str | None = None
    stage_limit: int | None = None


class ConcurrencyBoundedAnalysisOrchestrator(AnalysisOrchestrator):
    """Enforces stage-level `max_concurrency` around live provider invocations."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stage_semaphores: dict[tuple[int, str, int], asyncio.Semaphore] = {}

    def prepare(
        self,
        *,
        stage: AnalysisStageTemplateVersion,
        targets: tuple[Any, ...],
        source_channel_context: Mapping[str, Any] | None = None,
        attempt: int = 1,
        visual_inputs: tuple[InferenceImage, ...] = (),
    ) -> PreparedAnalysisCall:
        prepared = super().prepare(
            stage=stage,
            targets=targets,
            source_channel_context=source_channel_context,
            attempt=attempt,
            visual_inputs=visual_inputs,
        )
        stage_limit = getattr(stage, "max_concurrency", None)
        if isinstance(stage_limit, int) and stage_limit > 0:
            stage_key = str(
                getattr(stage, "stage_template_id", None)
                or getattr(stage, "template_id", None)
                or getattr(stage, "version_id", None)
                or id(stage)
            )
            return _BoundedPreparedAnalysisCall(
                schema=prepared.schema,
                request=prepared.request,
                cache_key=prepared.cache_key,
                stage_key=stage_key,
                stage_limit=stage_limit,
            )
        return prepared

    async def invoke(self, prepared: PreparedAnalysisCall) -> AnalysisInvocationResult:
        stage_key = getattr(prepared, "stage_key", None)
        stage_limit = getattr(prepared, "stage_limit", None)
        if not isinstance(stage_key, str) or not isinstance(stage_limit, int) or stage_limit <= 0:
            return await super().invoke(prepared)
        loop_id = id(asyncio.get_running_loop())
        sem_key = (loop_id, stage_key, stage_limit)
        semaphore = self._stage_semaphores.get(sem_key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(stage_limit)
            self._stage_semaphores[sem_key] = semaphore
        async with semaphore:
            return await super().invoke(prepared)

    async def aclose(self) -> None:
        self._stage_semaphores.clear()
        provider_close = getattr(self._provider, "aclose", None)
        if callable(provider_close):
            await provider_close()


@dataclass(frozen=True, slots=True)
class _LocalStorageResolver:
    storage: LocalArchiveStorage

    def resolve(self, *, storage_backend: str) -> LocalArchiveStorage:
        if storage_backend != "local":
            raise DomainValidationError(f"unsupported storage_backend: {storage_backend!r}")
        return self.storage


@dataclass(slots=True)
class WorkerRuntime:
    """Worker composition root for PostgreSQL-owned range and image-archive work."""

    database: AsyncDatabase
    range_execution_worker: RangeExecutionWorker
    range_execution_history_ingestion: RangeExecutionHistoryIngestion | None = None
    image_archive_worker: ImageArchiveWorker | None = None
    video_archive_worker: VideoArchiveWorker | None = None
    analysis_stage_run_worker: AnalysisStageRunWorker | None = None
    publication_worker: PublicationWorker | None = None
    cleanup_worker: ArchiveCleanupWorker | None = None
    pipeline_coordinator: MessagePipelineCoordinator | None = None
    telemetry: OperationsTelemetry | None = None

    telegram_client_pool: Any | None = None
    media_extractors: tuple[FFmpegVideoFrameExtractor, ...] = ()

    async def handle_range_execution(self, *, execution_id: str, now: datetime) -> bool:
        normalized_execution_id = _normalize_execution_id(execution_id)

        async def _execute() -> bool:
            claim = await self.range_execution_worker.claim(
                execution_id=normalized_execution_id,
                now=now,
            )
            if claim is None:
                return False
            if self.range_execution_history_ingestion is None:
                # Deployment injects a Telegram gateway after its identity/session adapter exists.
                # Leaving this lease open is safer than completing an unprocessed window.
                return True
            return await self.range_execution_history_ingestion.process(claim=claim, now=now)

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_image_archive(self, *, image_asset_id: str, now: datetime) -> bool:
        normalized_image_asset_id = _normalize_image_asset_id(image_asset_id)

        async def _execute() -> bool:
            if self.image_archive_worker is None:
                # The wake-up remains durable and will be repaired once Telegram identity/session
                # and archive storage composition are injected. Never fake archive completion.
                return False
            processed = await self.image_archive_worker.process(
                image_asset_id=normalized_image_asset_id,
                now=now,
            )
            if processed and self.pipeline_coordinator is not None:
                await self.pipeline_coordinator.on_image_asset_event(
                    image_asset_id=normalized_image_asset_id,
                    now=now,
                )
            return processed

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_video_archive(self, *, video_asset_id: str, now: datetime) -> bool:
        normalized_video_asset_id = _normalize_uuid(video_asset_id, field="video_asset_id")

        async def _execute() -> bool:
            if self.video_archive_worker is None:
                return False
            processed = await self.video_archive_worker.process(
                video_asset_id=normalized_video_asset_id,
                now=now,
            )
            if processed and self.pipeline_coordinator is not None:
                await self.pipeline_coordinator.on_video_asset_event(
                    video_asset_id=normalized_video_asset_id,
                    now=now,
                )
            return processed

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_analysis_stage_run(self, *, stage_run_id: str, now: datetime) -> bool:
        normalized_stage_run_id = _normalize_uuid(stage_run_id, field="stage_run_id")

        async def _execute() -> bool:
            if self.analysis_stage_run_worker is None:
                return False
            processed = await self.analysis_stage_run_worker.process(
                stage_run_id=normalized_stage_run_id,
                now=now,
            )
            if processed and self.pipeline_coordinator is not None:
                await self.pipeline_coordinator.on_stage_run_event(
                    stage_run_id=normalized_stage_run_id,
                    now=now,
                )
            return processed

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_publication(self, *, intent_id: str, now: datetime) -> bool:
        normalized_intent_id = _normalize_uuid(intent_id, field="intent_id")

        async def _execute() -> bool:
            if self.publication_worker is None:
                # Publication remains database-owned and scheduler repair will wake it again after a
                # Telegram identity/session composition is injected. Never fake remote completion.
                return False
            processed = await self.publication_worker.process(
                intent_id=normalized_intent_id,
                now=now,
            )
            if processed and self.pipeline_coordinator is not None:
                await self.pipeline_coordinator.on_publication_intent_event(
                    intent_id=normalized_intent_id,
                    now=now,
                )
            return processed

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def handle_archive_cleanup(self, *, job_id: str, now: datetime) -> bool:
        normalized_job_id = _normalize_uuid(job_id, field="job_id")

        async def _execute() -> bool:
            if self.cleanup_worker is None:
                # Cleanup storage is deployment-specific. Keep the PostgreSQL job and wake-up
                # durable until a resolver is explicitly composed; never infer a backend from keys.
                return False
            return await self.cleanup_worker.process(job_id=normalized_job_id, now=now)

        return await self._run_with_heartbeat(now=now, action=_execute)

    async def _run_with_heartbeat(
        self,
        *,
        now: datetime,
        action: Callable[[], Awaitable[bool]],
        refresh_completion_clock: bool = False,
    ) -> bool:
        try:
            result = await action()
        except Exception:
            stamped = max(now, datetime.now(UTC)) if refresh_completion_clock else now
            if self.telemetry is not None:
                self.telemetry.heartbeat_worker(
                    at=_monotonic_heartbeat_time(self.telemetry, stamped),
                    database_ready=False,
                    broker_ready=True,
                )
            await _record_worker_external_heartbeats(
                self.database,
                database_ready=False,
                broker_ready=True,
                now=stamped,
            )
            raise
        stamped = max(now, datetime.now(UTC)) if refresh_completion_clock else now
        if self.telemetry is not None:
            self.telemetry.heartbeat_worker(
                at=_monotonic_heartbeat_time(self.telemetry, stamped),
                database_ready=True,
                broker_ready=True,
            )
        write_heartbeat_file("worker", status="running", now=stamped)
        await _record_worker_external_heartbeats(
            self.database,
            database_ready=True,
            broker_ready=True,
            now=stamped,
        )
        return result

    async def close(self) -> None:
        try:
            for extractor in self.media_extractors:
                extractor.terminate_active_processes()
                with _EXTRACTOR_LOCK:
                    _REGISTERED_EXTRACTORS.pop(id(extractor), None)
                    _FALLBACK_EXTRACTORS.pop(id(extractor), None)
            orchestrator = getattr(self.analysis_stage_run_worker, "orchestrator", None)
            orchestrator_close = getattr(orchestrator, "aclose", None)
            if callable(orchestrator_close):
                await orchestrator_close()
            if self.telegram_client_pool is not None:
                await self.telegram_client_pool.close()
        finally:
            await self.database.dispose()


def _monotonic_heartbeat_time(telemetry: OperationsTelemetry, candidate: datetime) -> datetime:
    worker_state = getattr(telemetry, "_worker", None)
    prior = getattr(worker_state, "last_heartbeat_at", None)
    if isinstance(prior, datetime) and candidate < prior:
        return prior
    return candidate


_WORKER_SERVICE_ROLE: str | None = None


def set_worker_service_role(role: str | None) -> None:
    global _WORKER_SERVICE_ROLE
    _WORKER_SERVICE_ROLE = role.strip() if isinstance(role, str) and role.strip() else None


async def _record_worker_external_heartbeats(
    database: Any,
    *,
    database_ready: bool,
    broker_ready: bool,
    now: datetime,
    connect_if_idle: bool = False,
) -> None:
    await upsert_service_heartbeat(
        database,
        service_name="worker",
        database_ready=database_ready,
        broker_ready=broker_ready,
        now=now,
        connect_if_idle=connect_if_idle,
    )
    if _WORKER_SERVICE_ROLE:
        await upsert_service_heartbeat(
            database,
            service_name=f"worker-{_WORKER_SERVICE_ROLE}",
            database_ready=database_ready,
            broker_ready=broker_ready,
            now=now,
            connect_if_idle=connect_if_idle,
        )


def create_worker_runtime(
    *,
    settings: Settings,
    telemetry: OperationsTelemetry | None = None,
    telegram_client_factory: Callable[..., object] | None = None,
) -> WorkerRuntime:
    from apps.runtime_dependencies import (
        VaultInferenceSecretResolver,
        configured_archive_storage,
        configured_secret_vault,
    )
    from tgcurator.application import MessageIngestService
    from tgcurator.application.media import (
        ImageArchiveService,
        VideoFrameSamplingService,
        VideoSamplingProfile,
    )
    from tgcurator.application.ports.media import ImageNormalizationProfile
    from tgcurator.infrastructure.database.image_archive_repository import (
        SqlAlchemyImageArchiveWorkRepository,
    )
    from tgcurator.infrastructure.database.media_profile_repository import (
        SqlAlchemyMediaProfileResolver,
    )
    from tgcurator.infrastructure.database.message_ingest_repository import (
        SqlAlchemyTelegramMessageIngestRepository,
    )
    from tgcurator.infrastructure.database.publication_repository import (
        SqlAlchemyPublicationRepository,
    )
    from tgcurator.infrastructure.database.video_archive_repository import (
        SqlAlchemyVideoArchiveWorkRepository,
    )
    from tgcurator.infrastructure.media import PillowImageProcessor
    from tgcurator.infrastructure.telegram.configured_runtime import (
        ConfiguredTelegramClientPool,
        ConfiguredTelegramGateway,
        ConfiguredTelegramMediaDownloader,
    )
    from tgcurator.infrastructure.telegram.telethon_publisher import TelethonPublisher

    database_url = resolve_database_url(settings)
    if database_url is None or not database_url.strip():
        raise RuntimeError("TGCURATOR_DATABASE_URL is required")
    database = AsyncDatabase(database_url)
    resolved_telemetry = telemetry if telemetry is not None else OperationsMonitor()
    vault = configured_secret_vault(settings=settings, database=database)
    storages = configured_archive_storage(settings)
    pool = (
        ConfiguredTelegramClientPool(
            database=database,
            vault=vault,
            settings=settings,
            client_factory=telegram_client_factory,
        )
        if vault is not None
        else None
    )
    gateway = ConfiguredTelegramGateway(database=database, client_pool=pool) if pool else None
    range_worker = RangeExecutionWorker(
        repository=SqlAlchemyRangeExecutionWorkerRepository(database)
    )
    pipeline = MessagePipelineCoordinator(
        repository=SqlAlchemyMessagePipelineRepository(database),
        routing_service=RoutingService(repository=SqlAlchemyRoutingRepository(database)),
    )
    history = (
        RangeExecutionHistoryIngestion(
            range_execution_worker=range_worker,
            telegram_gateway=gateway,
            message_ingest_service=MessageIngestService(
                repository=SqlAlchemyTelegramMessageIngestRepository(
                    database, defer_media_wakeups=True
                )
            ),
            pipeline_coordinator=pipeline,
        )
        if gateway is not None
        else None
    )
    image_worker = None
    video_worker = None
    extractors: tuple[FFmpegVideoFrameExtractor, ...] = ()
    if gateway is not None and storages is not None:
        storage = storages.resolve(storage_backend="local")
        downloader = ConfiguredTelegramMediaDownloader(gateway)
        processor = PillowImageProcessor()
        image_profile = ImageNormalizationProfile(max_side_pixels=1536, quality=85)
        media_profiles = SqlAlchemyMediaProfileResolver(database)
        image_worker = ImageArchiveWorker(
            repository=SqlAlchemyImageArchiveWorkRepository(database),
            media_downloader=downloader,
            archive_service=ImageArchiveService(image_processor=processor, archive_storage=storage),
            normalization_profile=image_profile,
            storage_backend="local",
            profile_resolver=media_profiles,
        )
        extractor = register_media_extractor(
            _WeakRefFFmpegVideoFrameExtractor(temp_dir=settings.temp_media_dir)
        )
        extractors = (extractor,)
        video_worker = VideoArchiveWorker(
            repository=SqlAlchemyVideoArchiveWorkRepository(database),
            media_downloader=downloader,
            metadata_probe=extractor,
            sampling_service=VideoFrameSamplingService(
                frame_extractor=extractor, image_processor=processor
            ),
            archive_storage=storage,
            sampling_profile=VideoSamplingProfile(),
            image_profile=image_profile,
            storage_backend="local",
            profile_resolver=media_profiles,
        )
    provider = (
        ConcurrencyBoundedInferenceProvider(secret_resolver=VaultInferenceSecretResolver(vault))
        if vault is not None
        else None
    )
    return WorkerRuntime(
        database=database,
        range_execution_worker=range_worker,
        range_execution_history_ingestion=history,
        image_archive_worker=image_worker,
        video_archive_worker=video_worker,
        analysis_stage_run_worker=AnalysisStageRunWorker(
            repository=SqlAlchemyAnalysisStageRunRepository(database),
            orchestrator=ConcurrencyBoundedAnalysisOrchestrator(
                provider=provider,
                storage_resolver=storages,
            ),
        ),
        publication_worker=(
            PublicationWorker(
                repository=SqlAlchemyPublicationRepository(database),
                publisher=TelethonPublisher(client_resolver=pool),
                telemetry=resolved_telemetry,
            )
            if pool is not None
            else None
        ),
        cleanup_worker=(
            ArchiveCleanupWorker(
                repository=SqlAlchemyArchiveCleanupRepository(database), storage_resolver=storages
            )
            if storages is not None
            else None
        ),
        pipeline_coordinator=pipeline,
        telemetry=resolved_telemetry,
        telegram_client_pool=pool,
        media_extractors=extractors,
    )


class _ProcessWorkerLoopSession:
    """Owns one persistent asyncio event loop and singleton WorkerRuntime per OS PID."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(
            target=self._serve_loop,
            name=f"tgcurator-worker-loop-{self.pid}",
            daemon=True,
        )
        self.runtime: WorkerRuntime | None = None
        self._runtime_signature: tuple[object, ...] | None = None
        self._init_lock: asyncio.Lock | None = None
        self.thread.start()
        self._ready.wait(timeout=10.0)

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._init_lock = asyncio.Lock()
        self._ready.set()
        self.loop.run_forever()

    @staticmethod
    def _compute_signature(settings: Settings) -> tuple[object, ...]:
        media_dir = getattr(settings, "media_archive_dir", None)
        return (
            id(settings),
            resolve_database_url(settings),
            str(media_dir) if media_dir is not None else None,
            id(create_worker_runtime),
        )

    async def get_runtime(self) -> WorkerRuntime:
        assert self._init_lock is not None
        settings = get_settings()
        signature = self._compute_signature(settings)
        if self.runtime is not None and self._runtime_signature == signature:
            return self.runtime
        async with self._init_lock:
            settings = get_settings()
            signature = self._compute_signature(settings)
            if self.runtime is not None and self._runtime_signature != signature:
                stale_runtime, self.runtime = self.runtime, None
                self._runtime_signature = None
                await stale_runtime.close()
            if self.runtime is None:
                self.runtime = create_worker_runtime(settings=settings)
                self._runtime_signature = signature
            return self.runtime

    def run_coro(self, coro_factory: Callable[[WorkerRuntime], Awaitable[bool]]) -> bool:
        async def _runner() -> bool:
            runtime = await self.get_runtime()
            return await coro_factory(runtime)

        future = asyncio.run_coroutine_threadsafe(_runner(), self.loop)
        return future.result()

    def close(self) -> None:
        async def _close_runtime() -> None:
            runtime, self.runtime = self.runtime, None
            if runtime is not None:
                await runtime.close()

        try:
            if self.thread.is_alive() and self.loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_close_runtime(), self.loop)
                future.result(timeout=25.0)
        finally:
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            if self.thread.is_alive() and threading.current_thread() is not self.thread:
                self.thread.join(timeout=10.0)
            if not self.loop.is_closed() and not self.loop.is_running():
                self.loop.close()


_SESSION_LOCK = threading.Lock()
_PROCESS_WORKER_ACTIVE = False
_PROCESS_SESSIONS_BY_PID: dict[int, _ProcessWorkerLoopSession] = {}
_INSTALLED_CHILD_SIGTERM_PID: int | None = None


def install_worker_child_sigterm_handler() -> None:
    """Ensure SIGTERM inside a worker child immediately stops active FFmpeg subprocesses."""

    global _INSTALLED_CHILD_SIGTERM_PID
    if threading.current_thread() is not threading.main_thread():
        return
    current_pid = os.getpid()
    if _INSTALLED_CHILD_SIGTERM_PID == current_pid:
        return
    previous = signal.getsignal(signal.SIGTERM)

    def _handle_sigterm(signum: int, frame: Any) -> None:
        terminate_registered_media_extractors()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
        _INSTALLED_CHILD_SIGTERM_PID = current_pid
    except (OSError, ValueError):
        pass


def activate_process_worker_runtime(*, install_sigterm: bool = False) -> None:
    """Enable persistent WorkerRuntime + event-loop reuse for tasks in this worker process."""

    global _PROCESS_WORKER_ACTIVE
    with _SESSION_LOCK:
        _PROCESS_WORKER_ACTIVE = True
    if install_sigterm:
        install_worker_child_sigterm_handler()


def shutdown_process_worker_runtime(*, deactivate: bool = True) -> None:
    """Dispose any cached WorkerRuntime and persistent event loop in the current OS PID."""

    global _PROCESS_WORKER_ACTIVE
    terminate_registered_media_extractors()
    current_pid = os.getpid()
    with _SESSION_LOCK:
        if deactivate:
            _PROCESS_WORKER_ACTIVE = False
        session = _PROCESS_SESSIONS_BY_PID.pop(current_pid, None)
    if session is not None:
        session.close()


def _active_process_session() -> _ProcessWorkerLoopSession | None:
    current_pid = os.getpid()
    with _SESSION_LOCK:
        if not _PROCESS_WORKER_ACTIVE:
            return None
        session = _PROCESS_SESSIONS_BY_PID.get(current_pid)
        if session is None or not session.thread.is_alive():
            session = _ProcessWorkerLoopSession()
            _PROCESS_SESSIONS_BY_PID[current_pid] = session
        return session


def execute_worker_task(
    *,
    coro_on_runtime: Callable[[WorkerRuntime], Awaitable[bool]],
    standalone_coro: Callable[[], Awaitable[bool]],
) -> bool:
    """Execute a Celery task on the persistent worker session when active, else one-shot."""

    session = _active_process_session()
    if session is not None:
        return session.run_coro(coro_on_runtime)
    return asyncio.run(standalone_coro())


async def _handle_range_on_runtime(runtime: WorkerRuntime, execution_id: str) -> bool:
    normalized = _normalize_execution_id(execution_id)

    async def _execute() -> bool:
        now = datetime.now(UTC)
        claim = await runtime.range_execution_worker.claim(execution_id=normalized, now=now)
        if claim is None:
            return False
        if runtime.range_execution_history_ingestion is None:
            return True
        return await runtime.range_execution_history_ingestion.process(claim=claim, now=now)

    return await runtime._run_with_heartbeat(
        now=datetime.now(UTC),
        action=_execute,
        refresh_completion_clock=True,
    )


async def _handle_image_on_runtime(runtime: WorkerRuntime, image_asset_id: str) -> bool:
    normalized = _normalize_image_asset_id(image_asset_id)

    async def _execute() -> bool:
        if runtime.image_archive_worker is None:
            return False
        processed = await runtime.image_archive_worker.process(
            image_asset_id=normalized,
            now=datetime.now(UTC),
        )
        if processed and runtime.pipeline_coordinator is not None:
            await runtime.pipeline_coordinator.on_image_asset_event(
                image_asset_id=normalized,
                now=datetime.now(UTC),
            )
        return processed

    return await runtime._run_with_heartbeat(
        now=datetime.now(UTC),
        action=_execute,
        refresh_completion_clock=True,
    )


async def _handle_video_on_runtime(runtime: WorkerRuntime, video_asset_id: str) -> bool:
    normalized = _normalize_uuid(video_asset_id, field="video_asset_id")

    async def _execute() -> bool:
        if runtime.video_archive_worker is None:
            return False
        processed = await runtime.video_archive_worker.process(
            video_asset_id=normalized,
            now=datetime.now(UTC),
        )
        if processed and runtime.pipeline_coordinator is not None:
            await runtime.pipeline_coordinator.on_video_asset_event(
                video_asset_id=normalized,
                now=datetime.now(UTC),
            )
        return processed

    return await runtime._run_with_heartbeat(
        now=datetime.now(UTC),
        action=_execute,
        refresh_completion_clock=True,
    )


async def _handle_analysis_on_runtime(runtime: WorkerRuntime, stage_run_id: str) -> bool:
    normalized = _normalize_uuid(stage_run_id, field="stage_run_id")

    async def _execute() -> bool:
        if runtime.analysis_stage_run_worker is None:
            return False
        processed = await runtime.analysis_stage_run_worker.process(
            stage_run_id=normalized,
            now=datetime.now(UTC),
        )
        if processed and runtime.pipeline_coordinator is not None:
            await runtime.pipeline_coordinator.on_stage_run_event(
                stage_run_id=normalized,
                now=datetime.now(UTC),
            )
        return processed

    return await runtime._run_with_heartbeat(
        now=datetime.now(UTC),
        action=_execute,
        refresh_completion_clock=True,
    )


async def _handle_publication_on_runtime(runtime: WorkerRuntime, intent_id: str) -> bool:
    normalized = _normalize_uuid(intent_id, field="intent_id")

    async def _execute() -> bool:
        if runtime.publication_worker is None:
            return False
        processed = await runtime.publication_worker.process(
            intent_id=normalized,
            now=datetime.now(UTC),
        )
        if processed and runtime.pipeline_coordinator is not None:
            await runtime.pipeline_coordinator.on_publication_intent_event(
                intent_id=normalized,
                now=datetime.now(UTC),
            )
        return processed

    return await runtime._run_with_heartbeat(
        now=datetime.now(UTC),
        action=_execute,
        refresh_completion_clock=True,
    )


async def _handle_cleanup_on_runtime(runtime: WorkerRuntime, job_id: str) -> bool:
    normalized = _normalize_uuid(job_id, field="job_id")

    async def _execute() -> bool:
        if runtime.cleanup_worker is None:
            return False
        return await runtime.cleanup_worker.process(job_id=normalized, now=datetime.now(UTC))

    return await runtime._run_with_heartbeat(
        now=datetime.now(UTC),
        action=_execute,
        refresh_completion_clock=True,
    )


async def run_range_execution_task(execution_id: str, *, settings: Settings | None = None) -> bool:
    """Run the M2 worker entry point for exactly one stable RangeExecution UUID."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await _handle_range_on_runtime(runtime, execution_id)
    finally:
        await runtime.close()


async def run_image_archive_task(image_asset_id: str, *, settings: Settings | None = None) -> bool:
    """Run one durable image-archive wake-up without inventing a missing Telegram composition."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await _handle_image_on_runtime(runtime, image_asset_id)
    finally:
        await runtime.close()


async def run_video_archive_task(video_asset_id: str, *, settings: Settings | None = None) -> bool:
    """Run one durable video-archive wake-up without inventing missing media composition."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await _handle_video_on_runtime(runtime, video_asset_id)
    finally:
        await runtime.close()


async def run_analysis_stage_run_task(
    stage_run_id: str, *, settings: Settings | None = None
) -> bool:
    """Run one durable analysis StageRun using the configured external provider, if any."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await _handle_analysis_on_runtime(runtime, stage_run_id)
    finally:
        await runtime.close()


async def run_publication_task(intent_id: str, *, settings: Settings | None = None) -> bool:
    """Run one UUID-only publication wake-up without fabricating a Telegram session."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await _handle_publication_on_runtime(runtime, intent_id)
    finally:
        await runtime.close()


async def run_archive_cleanup_task(job_id: str, *, settings: Settings | None = None) -> bool:
    """Run one UUID-only cleanup wake-up without fabricating archive storage composition."""

    runtime = create_worker_runtime(settings=settings or get_settings())
    try:
        return await _handle_cleanup_on_runtime(runtime, job_id)
    finally:
        await runtime.close()


def _normalize_execution_id(execution_id: str) -> str:
    return _normalize_uuid(execution_id, field="execution_id")


def _normalize_image_asset_id(image_asset_id: str) -> str:
    return _normalize_uuid(image_asset_id, field="image_asset_id")


def _normalize_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
