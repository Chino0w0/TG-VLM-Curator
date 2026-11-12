from __future__ import annotations

from celery import Celery, bootsteps
from celery.signals import (
    worker_process_init,
    worker_process_shutdown,
    worker_ready,
    worker_shutdown,
    worker_shutting_down,
)

from apps.worker.runtime import (
    _handle_analysis_on_runtime,
    _handle_cleanup_on_runtime,
    _handle_image_on_runtime,
    _handle_publication_on_runtime,
    _handle_range_on_runtime,
    _handle_video_on_runtime,
    activate_process_worker_runtime,
    execute_worker_task,
    run_analysis_stage_run_task,
    run_archive_cleanup_task,
    run_image_archive_task,
    run_publication_task,
    run_range_execution_task,
    run_video_archive_task,
    shutdown_process_worker_runtime,
)
from tgcurator.application import get_settings
from tgcurator.infrastructure.queue import (
    ANALYSIS_TASK_NAME,
    CLEANUP_TASK_NAME,
    IMAGE_ARCHIVE_TASK_NAME,
    PUBLICATION_TASK_NAME,
    RANGE_EXECUTION_TASK_NAME,
    VIDEO_ARCHIVE_TASK_NAME,
    create_celery_client,
)

_UNCONFIGURED_BROKER_PLACEHOLDER = "memory://"


class _WorkerRuntimeLifecycleStep(bootsteps.StartStopStep):
    """Guarantees singleton WorkerRuntime disposal on both warm stop and cold terminate."""

    requires = {"celery.worker.components:Pool"}

    def start(self, _parent: object) -> None:
        activate_process_worker_runtime(install_sigterm=False)

    def stop(self, _parent: object) -> None:
        shutdown_process_worker_runtime(deactivate=True)

    def terminate(self, _parent: object) -> None:
        shutdown_process_worker_runtime(deactivate=True)

    def close(self, _parent: object) -> None:
        shutdown_process_worker_runtime(deactivate=True)


def create_worker_celery_app(*, require_broker: bool = True) -> Celery:
    """Create the worker Celery application without placing business state in Redis."""

    broker = get_settings().celery_broker_url
    broker_url = broker.get_secret_value().strip() if broker is not None else ""
    if not broker_url:
        if require_broker:
            raise RuntimeError("TGCURATOR_CELERY_BROKER_URL is required")
        broker_url = _UNCONFIGURED_BROKER_PLACEHOLDER
    app = create_celery_client(
        broker_url=broker_url,
        application_name="tgcurator.worker",
    )
    app.steps["worker"].add(_WorkerRuntimeLifecycleStep)
    return app


celery_app = create_worker_celery_app(require_broker=False)


@worker_process_init.connect(weak=False)
def _on_worker_process_init(**_kwargs: object) -> None:
    activate_process_worker_runtime(install_sigterm=True)


@worker_ready.connect(weak=False)
def _on_worker_ready(**_kwargs: object) -> None:
    activate_process_worker_runtime(install_sigterm=False)


@worker_process_shutdown.connect(weak=False)
def _on_worker_process_shutdown(**_kwargs: object) -> None:
    shutdown_process_worker_runtime(deactivate=False)


@worker_shutting_down.connect(weak=False)
def _on_worker_shutting_down(**_kwargs: object) -> None:
    shutdown_process_worker_runtime(deactivate=True)


@worker_shutdown.connect(weak=False)
def _on_worker_shutdown(**_kwargs: object) -> None:
    shutdown_process_worker_runtime(deactivate=True)


@celery_app.task(name=ANALYSIS_TASK_NAME, ignore_result=True)
def process_analysis_stage_run(stage_run_id: str) -> None:
    """Celery transport entry point; payload is only the durable StageRun UUID."""

    execute_worker_task(
        coro_on_runtime=lambda rt: _handle_analysis_on_runtime(rt, stage_run_id),
        standalone_coro=lambda: run_analysis_stage_run_task(stage_run_id),
    )


@celery_app.task(name=RANGE_EXECUTION_TASK_NAME, ignore_result=True)
def process_range_execution(execution_id: str) -> None:
    """Celery transport entry point; payload is only the RangeExecution UUID."""

    execute_worker_task(
        coro_on_runtime=lambda rt: _handle_range_on_runtime(rt, execution_id),
        standalone_coro=lambda: run_range_execution_task(execution_id),
    )


@celery_app.task(name=IMAGE_ARCHIVE_TASK_NAME, ignore_result=True)
def process_image_archive(image_asset_id: str) -> None:
    """Celery transport entry point; payload is only the durable ImageAsset UUID."""

    execute_worker_task(
        coro_on_runtime=lambda rt: _handle_image_on_runtime(rt, image_asset_id),
        standalone_coro=lambda: run_image_archive_task(image_asset_id),
    )


@celery_app.task(name=VIDEO_ARCHIVE_TASK_NAME, ignore_result=True)
def process_video_archive(video_asset_id: str) -> None:
    """Celery transport entry point; payload is only the durable VideoAsset UUID."""

    execute_worker_task(
        coro_on_runtime=lambda rt: _handle_video_on_runtime(rt, video_asset_id),
        standalone_coro=lambda: run_video_archive_task(video_asset_id),
    )


@celery_app.task(name=PUBLICATION_TASK_NAME, ignore_result=True)
def process_publication(intent_id: str) -> None:
    """Celery transport entry point; payload is only the PublicationIntent UUID."""

    execute_worker_task(
        coro_on_runtime=lambda rt: _handle_publication_on_runtime(rt, intent_id),
        standalone_coro=lambda: run_publication_task(intent_id),
    )


@celery_app.task(name=CLEANUP_TASK_NAME, ignore_result=True)
def process_archive_cleanup(job_id: str) -> None:
    """Celery transport entry point; payload is only the cleanup job UUID."""

    execute_worker_task(
        coro_on_runtime=lambda rt: _handle_cleanup_on_runtime(rt, job_id),
        standalone_coro=lambda: run_archive_cleanup_task(job_id),
    )
