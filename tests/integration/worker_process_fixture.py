"""Isolated OS worker fixture: only Telegram is substituted; tasks/runtime/DB stay real."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime_fixtures import ExternalTelegram, ExternalTelegramClient  # noqa: E402


def append_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"pid": os.getpid(), **event}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    from celery import signals

    from apps.worker import runtime as worker_runtime

    audit = Path(os.environ["TGCURATOR_TEST_PROCESS_AUDIT"])
    remote_path = Path(os.environ["TGCURATOR_TEST_REMOTE_STATE"])
    pause = os.environ.get("TGCURATOR_TEST_PAUSE_AFTER_REMOTE") == "1"
    peer = ExternalTelegram()
    peer.add_message(1)
    peer.add_message(2, text="second runtime message")
    applied = 0

    class Client(ExternalTelegramClient):
        async def __call__(self, request):
            nonlocal applied
            if remote_path.exists():
                peer.remote_ids = {
                    int(k): v
                    for k, v in json.loads(remote_path.read_text(encoding="utf-8")).items()
                }
            random_ids = request.random_id
            if not isinstance(random_ids, list):
                random_ids = [random_ids]
            already_applied = all(rid in peer.remote_ids for rid in random_ids)
            response = await super().__call__(request)
            temporary = remote_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(peer.remote_ids), encoding="utf-8", newline="\n")
            temporary.replace(remote_path)
            applied += 1
            append_event(
                audit,
                {
                    "event": "remote_applied",
                    "random_ids": random_ids,
                    "remote_ids": [peer.remote_ids[rid] for rid in random_ids],
                    "replayed": already_applied,
                    "operation": type(request).__name__,
                },
            )
            if pause and applied == 2:
                # Durable fake external side effect exists, but production publisher has
                # not received the response or committed its PublishRecord.
                append_event(audit, {"event": "paused_after_remote", "random_ids": random_ids})
                await asyncio.Event().wait()
            return response

    def external_client_factory(*, credentials, receive_updates):
        assert credentials.api_id == 12345
        client = Client(peer, receive_updates)
        peer.clients.append(client)
        return client

    original_factory = worker_runtime.create_worker_runtime

    def factory(**kwargs):
        runtime = original_factory(**kwargs, telegram_client_factory=external_client_factory)
        # Timing-only override: expire the real persisted lease without modifying DB rows,
        # sleeping five minutes, substituting the worker, or faking clocks.
        runtime.publication_worker.lease_duration = timedelta(seconds=4)
        return runtime

    worker_runtime.create_worker_runtime = factory
    from apps.worker.celery_app import celery_app

    celery_app.conf.update(
        broker_transport_options={"global_keyprefix": os.environ["TGCURATOR_TEST_KEYPREFIX"]},
        broker_pool_limit=0,
        worker_prefetch_multiplier=1,
        worker_enable_remote_control=False,
        task_always_eager=False,
    )

    @signals.worker_ready.connect(weak=False)
    def ready(**kwargs):
        append_event(audit, {"event": "worker_ready"})

    @signals.task_failure.connect(weak=False)
    def failed(exception=None, **kwargs):
        append_event(audit, {"event": "task_failed", "error_type": type(exception).__name__})

    @signals.task_postrun.connect(weak=False)
    def completed(sender=None, task_id=None, state=None, **kwargs):
        append_event(
            audit,
            {"event": "task_finished", "name": sender.name, "task_id": task_id, "state": state},
        )

    append_event(audit, {"event": "process_started", "publication_lease_seconds": 4})
    celery_app.worker_main(
        argv=[
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--loglevel=WARNING",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--queues=range_execution,image_archive,video_archive,analysis,publication,archive_cleanup",
        ]
    )


if __name__ == "__main__":
    main()
