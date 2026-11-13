"""Production composition through PG16 + Redis + actual Celery task entry points.

Opt in through explicit TGCURATOR_TEST_* variables. Never consumes business queues.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import io
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import redis
from celery import signals
from celery.contrib.testing.worker import start_worker
from PIL import Image
from postgres_support import temporary_postgres_database
from runtime_fixtures import (
    MODES,
    ExternalTelegram,
    inference_http_server,
    redis_test_url,
    runtime_settings,
    seed_configuration,
)
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from apps.scheduler.runtime import create_scheduler_runtime
from apps.worker import runtime as worker_module
from tgcurator.domain.analysis.common import canonical_hash
from tgcurator.infrastructure.database import models as m


def _resolve_evidence_dir(root: Path) -> Path:
    configured = os.environ.get("TGCURATOR_EVIDENCE_DIR")
    if configured and configured.strip():
        return Path(configured.strip()).resolve()
    dot_git = root / ".git"
    if dot_git.is_file():
        header = dot_git.read_text(encoding="utf-8").strip()
        if header.startswith("gitdir:"):
            git_dir = (root / header.partition(":")[2].strip()).resolve()
            return git_dir / "fix-2026-11-12/runtime"
    return dot_git / "fix-2026-11-12/runtime"


EVIDENCE = _resolve_evidence_dir(Path(__file__).resolve().parents[2])


@pytest.mark.parametrize("end_mode", ["fixed", "latest"])
def test_production_runtime_closed_loop(end_mode, tmp_path, monkeypatch):
    broker = redis_test_url()
    prefix = "tgcurator_test_runtime_" + uuid4().hex + ":"
    redis_client = redis.Redis.from_url(broker)
    assert redis_client.ping()
    peer = ExternalTelegram()
    peer.add_message(1)
    # Negative PreScreen must terminalize the second message without downloading its photo.
    peer.add_message(2, text="second runtime message")
    evidence = {
        "mode": end_mode,
        "pid": os.getpid(),
        "redis_db": 15,
        "redis_keyprefix": prefix,
        "tasks": [],
        "task_errors": [],
    }

    def task_done(sender=None, task_id=None, state=None, **kwargs):
        if sender and sender.name.startswith("tgcurator."):
            evidence["tasks"].append(
                {
                    "id": task_id,
                    "name": sender.name,
                    "state": state,
                    "thread": threading.current_thread().name,
                }
            )

    def task_error(exception=None, **kwargs):
        evidence["task_errors"].append(type(exception).__name__)

    signals.task_postrun.connect(task_done, weak=False)
    signals.task_failure.connect(task_error, weak=False)
    try:
        with temporary_postgres_database("runtime_" + end_mode) as url:
            evidence["database"] = make_url(url).database
            settings = runtime_settings(url, broker, tmp_path / "archive")
            original_factory = worker_module.create_worker_runtime
            created = []

            def configured_factory(**kwargs):
                # Inject ONLY the external Telethon client constructor. The real production
                # factory, repositories, provider, composer and worker handlers are unchanged.
                runtime = original_factory(**kwargs, telegram_client_factory=peer.factory)
                created.append(runtime)
                return runtime

            monkeypatch.setenv("TGCURATOR_CELERY_BROKER_URL", broker)
            with (
                patch.object(worker_module, "create_worker_runtime", configured_factory),
                patch.object(worker_module, "get_settings", return_value=settings),
                inference_http_server(peer) as base_url,
                asyncio.Runner() as runner,
            ):
                runtime = original_factory(settings=settings, telegram_client_factory=peer.factory)
                scheduler = None
                app = None
                app_config = None
                worker_connection = None
                try:

                    async def setup():
                        async with runtime.database.session() as session:
                            version = await session.scalar(text("SHOW server_version_num"))
                            assert 160000 <= int(version) < 170000
                            evidence["postgres_version_num"] = version
                            evidence["alembic_revision"] = await session.scalar(
                                text("SELECT version_num FROM alembic_version")
                            )
                        return await seed_configuration(
                            runtime.database, settings, peer, base_url, end_mode=end_mode
                        )

                    runner.run(setup())
                    gateway = runtime.range_execution_history_ingestion._telegram_gateway
                    scheduler = create_scheduler_runtime(
                        settings=settings, latest_boundary_source=gateway
                    )
                    producer = scheduler.wakeup_dispatcher.task_dispatcher._celery
                    producer.conf.broker_transport_options = {"global_keyprefix": prefix}
                    producer.conf.broker_pool_limit = 0
                    # Short repair interval is timing only; the durable production repair is real.
                    scheduler.wakeup_dispatcher.repair_interval = timedelta(seconds=1)
                    module = importlib.import_module("apps.worker.celery_app")
                    app = module.celery_app
                    app_config = {
                        key: app.conf[key]
                        for key in (
                            "broker_url",
                            "broker_transport_options",
                            "worker_enable_remote_control",
                            "worker_send_task_events",
                            "task_always_eager",
                            "worker_prefetch_multiplier",
                            "broker_pool_limit",
                        )
                    }
                    app.conf.update(
                        broker_url=broker,
                        broker_transport_options={"global_keyprefix": prefix},
                        worker_enable_remote_control=False,
                        worker_send_task_events=False,
                        task_always_eager=False,
                        worker_prefetch_multiplier=1,
                        broker_pool_limit=0,
                    )
                    first = runner.run(scheduler.run_once(now=datetime.now(UTC)))
                    assert first.range_schedule.created_executions == 1
                    queue_keys = [
                        key
                        for key in redis_client.scan_iter(match=prefix + "*")
                        if redis_client.type(key) == b"list"
                    ]
                    assert queue_keys, "real scheduler must have enqueued a Redis message"
                    evidence["lost_queue_messages"] = sum(redis_client.llen(k) for k in queue_keys)
                    # Simulate Redis transport signal loss ONLY in this test namespace.
                    redis_client.delete(*queue_keys)
                    evidence["cleared_keys"] = [k.decode() for k in queue_keys]
                    with start_worker(
                        app,
                        pool="solo",
                        concurrency=1,
                        perform_ping_check=False,
                        shutdown_timeout=20,
                        loglevel="WARNING",
                    ) as worker:
                        evidence["worker_alive"] = worker is not None
                        worker_connection = worker.consumer.connection

                        async def drive():
                            deadline = time.monotonic() + 120
                            snapshots = evidence.setdefault("snapshots", [])
                            while time.monotonic() < deadline:
                                assert not evidence["task_errors"], (
                                    "production Celery task failed: "
                                    + repr(evidence["task_errors"])
                                )
                                report = await scheduler.run_once(now=datetime.now(UTC))
                                assert report.wakeup_dispatch.failed_dispatches == 0
                                async with runtime.database.session() as s:
                                    execution = await s.scalar(select(m.RangeExecutionRecord))
                                    stages = list(await s.scalars(select(m.StageRunRecord)))
                                    messages = list(await s.scalars(select(m.MessageRecord)))
                                    intents = list(
                                        await s.scalars(select(m.PublicationIntentRecord))
                                    )
                                    snapshots.append(
                                        {
                                            "watermark": execution.watermark_message_id,
                                            "status": execution.status,
                                            "stage_statuses": [x.status for x in stages],
                                            "intent_statuses": [x.status for x in intents],
                                            "message_states": {
                                                str(x.primary_telegram_message_id): [
                                                    x.processing_status,
                                                    x.terminal_reason,
                                                ]
                                                for x in messages
                                            },
                                        }
                                    )
                                    if execution.status == "completed":
                                        return execution, stages, messages, intents, snapshots
                                    failed_intents = [x for x in intents if x.status == "failed"]
                                    assert not failed_intents, repr(
                                        [
                                            (
                                                x.publication_mode,
                                                x.last_error_code,
                                                x.last_error_type,
                                            )
                                            for x in failed_intents
                                        ]
                                    )
                                    failed = [x for x in stages if x.status == "failed"]
                                    if failed:
                                        raise AssertionError(
                                            "production StageRun failed: "
                                            + repr(
                                                [
                                                    (
                                                        str(x.id),
                                                        x.last_error_code,
                                                        x.last_error_type,
                                                    )
                                                    for x in failed
                                                ]
                                            )
                                        )
                                await asyncio.sleep(0.12)
                            raise AssertionError(
                                "runtime closed loop timed out: " + repr(snapshots[-5:])
                            )

                        execution, stages, messages, intents, snapshots = runner.run(drive())
                        evidence["snapshots"] = snapshots
                        assert execution.watermark_message_id == 2
                        assert any(
                            x["message_states"].get("2") == ["processed", "blocked_negative_gate"]
                            and x["watermark"] == 0
                            for x in snapshots
                        ), "later terminal message must not skip the unfinished first message"
                        assert [x["watermark"] for x in snapshots] == sorted(
                            x["watermark"] for x in snapshots
                        )
                        assert any(x.status == "skipped_condition" for x in stages)
                        assert execution.from_message_id_exclusive == 0
                        assert execution.to_message_id_inclusive == 2
                        assert all(x.processing_status == "processed" for x in messages)
                        assert set(x.publication_mode for x in intents) == set(MODES)
                        assert all(x.status == "sent" for x in intents)
                        # Deliver the same stable IDs again through the actual broker/worker.
                        before = len(peer.requests)
                        duplicate_ids = {
                            producer.send_task(
                                "tgcurator.range_execution",
                                args=[str(execution.id)],
                                queue="range_execution",
                            ).id
                        }
                        for intent in intents:
                            duplicate_ids.add(
                                producer.send_task(
                                    "tgcurator.publication",
                                    args=[str(intent.id)],
                                    queue="publication",
                                ).id
                            )
                        deadline = time.monotonic() + 30
                        while not duplicate_ids.issubset(
                            {item["id"] for item in evidence["tasks"]}
                        ):
                            assert time.monotonic() < deadline, "duplicate deliveries not consumed"
                            time.sleep(0.05)
                        evidence["explicit_duplicate_task_ids"] = sorted(duplicate_ids)
                        assert len(peer.requests) == before, "duplicate tasks must not republish"
                        evidence["duplicate_delivery_no_remote_send"] = True
                        # postrun precedes broker acknowledgement; let the consumer drain ACKs
                        # before stopping, otherwise Kombu may restore reserved signals at exit.
                        time.sleep(0.5)

                    async def verify_db():
                        async with runtime.database.session() as s:
                            assets = list(await s.scalars(select(m.ImageAssetRecord)))
                            attempts = list(await s.scalars(select(m.InferenceCallRecord)))
                            manifests = list(await s.scalars(select(m.InputManifestRecord)))
                            records = list(await s.scalars(select(m.PublishRecord)))
                            return assets, attempts, manifests, records

                    assets, attempts, manifests, records = runner.run(verify_db())
                    assert len(records) == 5
                    assert {r.operation for r in records} == {
                        "native_forward",
                        "copy_with_caption",
                        "send_text",
                    }
                    by_intent = {str(x.id): x for x in intents}
                    for record in records:
                        intent = by_intent[str(record.publication_intent_id)]
                        assert record.random_ids and record.remote_message_ids
                        assert all(
                            peer.remote_ids[rid] == remote
                            for rid, remote in zip(
                                record.random_ids, record.remote_message_ids, strict=True
                            )
                        )
                        if record.part == "supplement":
                            assert intent.publication_mode == "native_forward_with_supplement"
                    assert len(attempts) == len(peer.http_requests) == 3
                    assert all(x.status == "succeeded" and x.http_status == 200 for x in attempts)
                    assert all(
                        canonical_hash(x.content) == x.input_manifest_hash for x in manifests
                    )
                    assert all(x.input_manifest_id in {v.id for v in manifests} for x in attempts)
                    assert len(assets) == 2
                    ready = [asset for asset in assets if asset.archive_state == "ready"]
                    assert len(ready) == 1
                    assert [event for event in peer.events if event[0] == "download"] == [
                        ("download", 1)
                    ]
                    assert any(x.terminal_reason == "blocked_negative_gate" for x in messages)
                    archive = tmp_path / "archive" / ready[0].storage_key
                    with Image.open(archive) as image:
                        image.load()
                        assert image.format in {"WEBP", "JPEG", "PNG"}
                    event_names = [x[0] for x in peer.events]
                    assert event_names.index("prescreen") < event_names.index("download")
                    visual = [
                        part
                        for req in peer.http_requests
                        for message in req["messages"]
                        if isinstance(message["content"], list)
                        for part in message["content"]
                        if part.get("type") == "image_url"
                    ]
                    assert visual, "actual HTTP request must carry archived image bytes"
                    image_hashes = []
                    for part in visual:
                        encoded = part["image_url"]["url"].split(",", 1)[1]
                        decoded = base64.b64decode(encoded)
                        Image.open(io.BytesIO(decoded)).verify()
                        image_hashes.append(hashlib.sha256(decoded).hexdigest())
                    manifest_visuals = [
                        v
                        for manifest in manifests
                        for v in manifest.content["inputs"]["visual_inputs"]
                    ]
                    assert [v["sha256"] for v in manifest_visuals] == image_hashes
                    assert all(
                        v["sources"][0]["asset_id"] == str(ready[0].id) for v in manifest_visuals
                    )
                    manifest_json = json.dumps([x.content for x in manifests])
                    assert (
                        peer.inference_key not in manifest_json
                        and "data:image" not in manifest_json
                    )
                    assert "storage_key" not in manifest_json
                    assert not peer.http_errors
                    assert not evidence["task_errors"]
                    evidence.update(
                        http_requests=len(peer.http_requests),
                        image_parts=len(visual),
                        archive_sha256=ready[0].archive_sha256,
                        publish_records=len(records),
                        manifest_hashes=[x.input_manifest_hash for x in manifests],
                        image_sha256=image_hashes,
                        events=peer.events,
                        publication_intents=len(intents),
                        publication_modes=sorted({x.publication_mode for x in intents}),
                        remote_operations=[type(r).__name__ for r in peer.requests],
                        stage_statuses=[x.status for x in stages],
                        attempts=len(attempts),
                    )
                finally:
                    # start_worker joins its thread but Kombu can retain its consumer
                    # connection/finalizer; close it before namespace cleanup, not at exit.
                    if worker_connection is not None:
                        worker_connection.close()
                    if scheduler is not None:
                        runner.run(scheduler.close())
                        scheduler.wakeup_dispatcher.task_dispatcher._celery.close()
                    runner.run(runtime.close())
                    if app is not None and app_config is not None:
                        app.close()
                        app.conf.update(app_config)
                assert all(not client.connected for client in peer.clients)
                assert all(not x.database._engine for x in created)
                evidence["clients_closed"] = len(peer.clients)
                evidence["runtime_instances_closed"] = len(created)
    finally:
        signals.task_postrun.disconnect(task_done)
        signals.task_failure.disconnect(task_error)
        keys = list(redis_client.scan_iter(match=prefix + "*"))
        if keys:
            redis_client.delete(*keys)
        assert not list(redis_client.scan_iter(match=prefix + "*"))
        redis_client.close()
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / (end_mode + ".json")).write_text(
            json.dumps(evidence, indent=2), encoding="utf-8", newline="\n"
        )


def test_real_ingestor_subscription_reconnect_edit_delete(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from apps.telegram_ingestor.runtime import create_telegram_ingestor_runtime
    from tgcurator.infrastructure.telegram import configured_runtime

    broker = redis_test_url()
    peer = ExternalTelegram()
    first = peer.add_message(1)
    first.photo = first.media = None
    monkeypatch.setattr(configured_runtime, "_default_client_factory", peer.factory)
    with temporary_postgres_database("runtime_ingestor") as url, asyncio.Runner() as runner:
        settings = runtime_settings(url, broker, tmp_path)
        runtime = create_telegram_ingestor_runtime(settings=settings)

        async def scenario():
            ids = await seed_configuration(
                runtime.database, settings, peer, "http://127.0.0.1:1/v1", end_mode="latest"
            )
            now = datetime.now(UTC)
            try:
                await runtime.poll_updates(now=now)
                assert runtime.health_snapshot().connected
                assert len(peer.clients) == 1
                old = peer.clients[0]
                assert old.receive_updates and len(old.handlers) == 3
                async with runtime.database.session() as s:
                    assert (
                        await s.scalar(select(m.MessageRecord))
                    ).original_text == "runtime image"
                    source = await s.get(m.SourceChannel, ids.source)
                    assert source.last_seen_message_id == 1
                # Drop the external connection, add a missed message, then let production
                # subscription manager rebuild client/handlers and reconcile the durable gap.
                await old.disconnect()
                second = peer.add_message(2, text="missed while disconnected")
                second.photo = second.media = None
                await runtime.poll_updates(now=now + timedelta(seconds=2))
                assert not old.handlers
                assert len(peer.clients) == 2
                active = peer.clients[-1]
                assert len(active.handlers) == 3
                third = peer.add_message(3, text="live event")
                third.photo = third.media = None
                handlers = {type(builder).__name__: handler for handler, builder in active.handlers}
                await handlers["NewMessage"](SimpleNamespace(chat_id=-100111, message=third))
                first.raw_text = "edited through real lifecycle repository"
                first.edit_date = datetime.now(UTC)
                await handlers["MessageEdited"](SimpleNamespace(chat_id=-100111, message=first))
                await handlers["MessageDeleted"](SimpleNamespace(chat_id=-100111, deleted_ids=[2]))
                # Unknown/unsubscribed peers must not enter the DB.
                unknown = peer.add_message(99, peer=-100999)
                await handlers["NewMessage"](SimpleNamespace(chat_id=-100999, message=unknown))
                async with runtime.database.session() as s:
                    messages = {
                        x.primary_telegram_message_id: x
                        for x in await s.scalars(select(m.MessageRecord))
                    }
                    assert set(messages) == {1, 2, 3}
                    assert messages[1].original_text == first.raw_text
                    assert messages[1].edited_at is not None
                    assert messages[2].source_deleted
                    assert messages[3].original_text == "live event"
                    assert (await s.get(m.SourceChannel, ids.source)).last_seen_message_id == 3
                    assert not list(await s.scalars(select(m.DurableWakeupRecord)))
                return {
                    "messages": 3,
                    "reconnected_clients": 2,
                    "handlers_per_client": 3,
                    "edited": 1,
                    "deleted": 1,
                    "unknown_peer_ignored": True,
                }
            finally:
                await runtime.shutdown()

        evidence = runner.run(scenario())
        assert all(not c.connected and not c.handlers for c in peer.clients)
        evidence["clients_and_handlers_closed"] = True
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "ingestor.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8", newline="\n"
        )
