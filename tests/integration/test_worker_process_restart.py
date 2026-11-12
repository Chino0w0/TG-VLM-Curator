"""Real OS worker restart after a remote send but before its DB acknowledgement.

Requires explicit TEST PG16/Redis DB15 opt-in. Uses production Celery task functions,
factory, SQLAlchemy repositories, scheduler and HTTP adapter. Only external peers are fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import redis
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
from apps.worker.runtime import create_worker_runtime
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


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = _resolve_evidence_dir(ROOT)


def read_events(path):
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # A concurrent append can expose only its last incomplete line.
            continue
    return events


def launch_worker(*, settings, prefix, directory, pause, inference_secret):
    env = os.environ.copy()
    for key in (
        "TGCURATOR_APP_MASTER_KEY_FILE",
        "APP_MASTER_KEY_FILE",
        "APP_MASTER_KEY",
        "DATABASE_URL",
        "TEST_BROKER",
    ):
        env.pop(key, None)
    broker = settings.celery_broker_url.get_secret_value()
    master = settings.app_master_key.get_secret_value()
    env.update(
        TGCURATOR_ENVIRONMENT="test",
        TGCURATOR_DATABASE_URL=settings.database_url,
        TGCURATOR_CELERY_BROKER_URL=broker,
        TGCURATOR_APP_MASTER_KEY=master,
        TGCURATOR_ARCHIVE_ROOT=str(settings.archive_root),
        TGCURATOR_TEMP_MEDIA_DIR=str(directory / "tmp"),
        TGCURATOR_TEST_KEYPREFIX=prefix,
        TGCURATOR_TEST_PROCESS_AUDIT=str(directory / "events.jsonl"),
        TGCURATOR_TEST_REMOTE_STATE=str(directory / "remote-state.json"),
        TGCURATOR_TEST_PAUSE_AFTER_REMOTE="1" if pause else "0",
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("worker_process_fixture.py"))],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    secrets = [
        settings.database_url,
        broker,
        master,
        inference_secret,
        make_url(settings.database_url).password,
        make_url(broker).password,
    ]
    log_path = directory / f"worker-{process.pid}.log"

    def drain():
        with log_path.open("w", encoding="utf-8", newline="\n") as output:
            for line in process.stdout:
                for secret in secrets:
                    if secret:
                        line = line.replace(secret, "[REDACTED]")
                output.write(line)
                output.flush()
        process.stdout.close()

    thread = threading.Thread(target=drain, daemon=True, name=f"worker-log-{process.pid}")
    thread.start()
    return process, thread


def stop_process(process, thread):
    if process.poll() is None:
        # Windows venv python.exe may launch the base interpreter as a child.
        # Terminate this exact owned process tree, never an unrelated Python process.
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process.kill()
    process.wait(timeout=20)
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_worker_os_restart_preserves_remote_idempotency_and_watermark(tmp_path):
    broker = redis_test_url()
    prefix = "tgcurator_test_runtime_" + uuid4().hex + ":"
    directory = EVIDENCE / ("restart-" + uuid4().hex[:12])
    directory.mkdir(parents=True)
    client = redis.Redis.from_url(broker)
    assert client.ping()
    peer = ExternalTelegram()
    peer.add_message(1)
    peer.add_message(2, text="second runtime message")
    processes = []
    proof = {
        "keyprefix": prefix,
        "redis_db": 15,
        "processes": [],
        "failure_point": "remote side effect committed; response/PublishRecord not committed",
    }
    try:
        with (
            temporary_postgres_database("runtime_restart") as url,
            inference_http_server(peer) as endpoint,
            asyncio.Runner() as runner,
        ):
            settings = runtime_settings(url, broker, tmp_path / "archive")
            runtime = create_worker_runtime(settings=settings, telegram_client_factory=peer.factory)
            scheduler = create_scheduler_runtime(
                settings=settings,
                latest_boundary_source=runtime.range_execution_history_ingestion._telegram_gateway,
            )
            producer = scheduler.wakeup_dispatcher.task_dispatcher._celery
            producer.conf.update(
                broker_transport_options={"global_keyprefix": prefix}, broker_pool_limit=0
            )
            scheduler.wakeup_dispatcher.repair_interval = timedelta(seconds=1)

            async def database_snapshot():
                async with runtime.database.session() as s:
                    execution = await s.scalar(select(m.RangeExecutionRecord))
                    intents = list(await s.scalars(select(m.PublicationIntentRecord)))
                    records = list(await s.scalars(select(m.PublishRecord)))
                    wakeups = list(await s.scalars(select(m.DurableWakeupRecord)))
                    return execution, intents, records, wakeups

            async def wait_until(predicate, process, *, timeout=150):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    assert process.poll() is None, f"worker {process.pid} exited unexpectedly"
                    events = read_events(directory / "events.jsonl")
                    assert not [x for x in events if x["event"] == "task_failed"], events
                    report = await scheduler.run_once(now=datetime.now(UTC))
                    assert report.wakeup_dispatch.failed_dispatches == 0
                    state = await database_snapshot()
                    if predicate(events, state):
                        return state
                    await asyncio.sleep(0.15)
                state = await database_snapshot()
                raise AssertionError(
                    f"process restart gate timed out: execution={state[0].status}; "
                    f"intents={[(x.status, x.last_error_type) for x in state[1]]}"
                )

            try:

                async def setup():
                    async with runtime.database.session() as s:
                        version = int(await s.scalar(text("SHOW server_version_num")))
                        assert 160000 <= version < 170000
                        proof["postgres_version_num"] = version
                    await seed_configuration(
                        runtime.database, settings, peer, endpoint, end_mode="fixed"
                    )
                    return await scheduler.run_once(now=datetime.now(UTC))

                assert runner.run(setup()).wakeup_dispatch.dispatched_wakeups == 1
                proof["database"] = make_url(url).database
                assert any(client.type(k) == b"list" for k in client.scan_iter(match=prefix + "*"))
                old, old_log = launch_worker(
                    settings=settings,
                    prefix=prefix,
                    directory=directory,
                    pause=True,
                    inference_secret=peer.inference_key,
                )
                processes.append((old, old_log))
                before = runner.run(
                    wait_until(
                        lambda events, state: (
                            any(e["event"] == "paused_after_remote" for e in events)
                            and len(state[2]) == 1
                        ),
                        old,
                    )
                )
                execution, intents, records, wakeups = before
                assert (
                    execution.status == "awaiting_pipeline" and execution.watermark_message_id == 0
                )
                in_flight = [x for x in intents if x.in_flight_part is not None]
                assert len(in_flight) == 1 and in_flight[0].lease_token is not None
                assert len(json.loads((directory / "remote-state.json").read_text())) == 2
                original_record_ids = {r.id for r in records}
                old_lease = in_flight[0].lease_token
                proof["before_kill"] = {
                    "execution_status": execution.status,
                    "watermark": execution.watermark_message_id,
                    "records": [str(r.id) for r in records],
                    "intent": str(in_flight[0].id),
                    "lease_token": str(old_lease),
                    "lease_expires_at": in_flight[0].lease_expires_at.isoformat(),
                    "wakeup_count": len(wakeups),
                    "remote_effects": 2,
                }
                old_worker_pid = next(
                    e["pid"]
                    for e in read_events(directory / "events.jsonl")
                    if e["event"] == "paused_after_remote"
                )
                proof["old_worker_pid"] = old_worker_pid
                stop_process(old, old_log)
                proof["processes"].append(
                    {"pid": old.pid, "exit_code": old.returncode, "hard_killed": True}
                )
                new, new_log = launch_worker(
                    settings=settings,
                    prefix=prefix,
                    directory=directory,
                    pause=False,
                    inference_secret=peer.inference_key,
                )
                processes.append((new, new_log))
                assert old.pid != new.pid and old.poll() is not None
                final = runner.run(
                    wait_until(
                        lambda events, state: (
                            state[0].status == "completed"
                            and any(
                                e["event"] == "worker_ready" and e["pid"] != old_worker_pid
                                for e in events
                            )
                        ),
                        new,
                    )
                )
                execution, intents, records, wakeups = final
                assert execution.watermark_message_id == 2
                assert len(intents) == 4 and {x.publication_mode for x in intents} == set(MODES)
                assert all(x.status == "sent" and x.lease_token is None for x in intents)
                assert len(records) == 5 and original_record_ids <= {r.id for r in records}
                assert len({(r.publication_intent_id, r.part) for r in records}) == 5
                assert any(r.reconciled for r in records)
                events = read_events(directory / "events.jsonl")
                new_worker_pid = next(
                    e["pid"]
                    for e in events
                    if e["event"] == "worker_ready" and e["pid"] != old_worker_pid
                )
                assert new_worker_pid != old_worker_pid
                proof["new_worker_pid"] = new_worker_pid
                replayed = [e for e in events if e["event"] == "remote_applied" and e["replayed"]]
                assert len(replayed) == 1 and replayed[0]["pid"] == new_worker_pid
                remote = json.loads((directory / "remote-state.json").read_text(encoding="utf-8"))
                assert len(remote) == 5
                for record in records:
                    assert all(
                        remote[str(rid)] == mid
                        for rid, mid in zip(
                            record.random_ids, record.remote_message_ids, strict=True
                        )
                    )
                completed_random_ids = {rid for r in before[2] for rid in r.random_ids}
                assert not any(
                    completed_random_ids.intersection(e["random_ids"])
                    for e in events
                    if e["event"] == "remote_applied" and e["pid"] == new_worker_pid
                )
                proof["after_restart"] = {
                    "pid": new.pid,
                    "watermark": execution.watermark_message_id,
                    "publish_records": len(records),
                    "remote_effects": len(remote),
                    "replayed_unknown_response": len(replayed),
                    "prior_records_unchanged": True,
                    "modes": sorted({x.publication_mode for x in intents}),
                    "http_requests": len(peer.http_requests),
                }
                stop_process(new, new_log)
                proof["processes"].append(
                    {"pid": new.pid, "exit_code": new.returncode, "stopped_after_completion": True}
                )
            finally:
                for process, thread in processes:
                    stop_process(process, thread)
                runner.run(scheduler.close())
                producer.close()
                runner.run(runtime.close())
        proof["database_dropped"] = True
    finally:
        for process, thread in processes:
            stop_process(process, thread)
        keys = list(client.scan_iter(match=prefix + "*"))
        if keys:
            client.delete(*keys)
        proof["redis_keys_after_cleanup"] = len(list(client.scan_iter(match=prefix + "*")))
        assert proof["redis_keys_after_cleanup"] == 0
        client.close()
        proof["all_child_processes_exited"] = all(p.poll() is not None for p, _ in processes)
        (directory / "result.json").write_text(
            json.dumps(proof, indent=2), encoding="utf-8", newline="\n"
        )
        (EVIDENCE / "restart-latest.json").write_text(
            json.dumps(proof, indent=2), encoding="utf-8", newline="\n"
        )
