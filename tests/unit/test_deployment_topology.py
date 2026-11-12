from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from apps.healthcheck import evaluate_service_probe
from apps.scheduler.__main__ import run_scheduler_daemon
from apps.telegram_ingestor.__main__ import run_ingestor_daemon
from apps.worker.__main__ import QUEUES_BY_ROLE, resolve_worker_argv
from apps.worker.runtime import (
    register_media_extractor,
    terminate_registered_media_extractors,
)
from tgcurator.application import Settings
from tgcurator.application.ports.ingestion import IngestReport
from tgcurator.infrastructure.media import FFmpegMediaError, FFmpegVideoFrameExtractor

ROOT = Path(__file__).resolve().parents[2]

EXPECTED_SERVICES = {
    "postgres",
    "redis",
    "api",
    "web",
    "telegram-ingestor",
    "scheduler",
    "worker-media",
    "worker-analysis",
    "worker-publish",
    "worker-maintenance",
    "archive-tools",
}

EXPECTED_VOLUMES = {
    "postgres-data",
    "redis-data",
    "archive-data",
    "temp-media",
}

FORBIDDEN_INFERENCE_TOKENS = (
    "vllm/vllm",
    "ollama/ollama",
    "lmstudio",
    "tensorrt-llm",
    "text-generation-inference",
)


def _extract_top_level_yaml_keys(content: str, section_name: str) -> set[str]:
    in_section = False
    keys: set[str] = set()
    for raw_line in content.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if not raw_line.startswith(" "):
            in_section = raw_line.strip() == f"{section_name}:"
            continue
        if in_section:
            match = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", raw_line)
            if match:
                keys.add(match.group(1))
    return keys


class _FakeSchedulerRuntime:
    def __init__(self) -> None:
        self.cycles = 0
        self.closed = False

    async def run_once(self, *, now: datetime) -> None:
        _ = now
        self.cycles += 1

    async def close(self) -> None:
        self.closed = True


class _FakeIngestorRuntime:
    def __init__(self) -> None:
        self.flushes = 0
        self.shutdown_called = False

    async def flush_due_groups(self, *, now: datetime) -> IngestReport:
        _ = now
        self.flushes += 1
        return IngestReport(
            logical_messages=0,
            component_messages=0,
        )

    async def shutdown(self) -> IngestReport:
        self.shutdown_called = True
        return IngestReport(
            logical_messages=1,
            component_messages=2,
        )


class DeploymentTopologyAndLifecycleTests(unittest.TestCase):
    def test_docker_compose_declares_services_four_volumes_and_no_bundled_llm(
        self,
    ) -> None:
        compose_path = ROOT / "docker-compose.yml"
        self.assertTrue(compose_path.is_file())
        content = compose_path.read_text(encoding="utf-8")

        services = _extract_top_level_yaml_keys(content, "services")
        volumes = _extract_top_level_yaml_keys(content, "volumes")
        secrets = _extract_top_level_yaml_keys(content, "secrets")

        self.assertEqual(services, EXPECTED_SERVICES)
        self.assertEqual(volumes, EXPECTED_VOLUMES)
        self.assertEqual(secrets, {"app_master_key"})
        self.assertIn(
            "TGCURATOR_APP_MASTER_KEY_FILE: /run/secrets/app_master_key",
            content,
        )
        self.assertIn("stop_signal: SIGTERM", content)

        non_comment_lowered = "\n".join(
            line.lower() for line in content.splitlines() if not line.lstrip().startswith("#")
        )
        for token in FORBIDDEN_INFERENCE_TOKENS:
            self.assertNotIn(token, non_comment_lowered)

    def test_dockerfiles_nginx_and_backup_scripts_exist_and_follow_invariants(
        self,
    ) -> None:
        for rel in (
            ".dockerignore",
            ".env.example",
            "docker/backend.Dockerfile",
            "docker/web.Dockerfile",
            "docker/nginx.conf",
            "scripts/backup-postgres.sh",
            "scripts/backup-media.sh",
            "scripts/restore-postgres.sh",
        ):
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"Missing {rel}")
            raw = path.read_bytes()
            self.assertNotIn(b"\r\n", raw, f"Expected LF line endings in {rel}")

        backup_pg = (ROOT / "scripts/backup-postgres.sh").read_text(encoding="utf-8")
        self.assertIn("APP_MASTER_KEY", backup_pg)
        self.assertIn("started_at_utc", backup_pg)

        restore_pg = (ROOT / "scripts/restore-postgres.sh").read_text(encoding="utf-8")
        self.assertIn("--confirm-restore", restore_pg)
        self.assertIn("apps.worker.consistency_cli", restore_pg)

    def test_tls_and_proxy_boundary(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        nginx = (ROOT / "docker/nginx.conf").read_text(encoding="utf-8")
        backend = (ROOT / "docker/backend.Dockerfile").read_text(encoding="utf-8")
        self.assertIn("listen 443 ssl;", nginx)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", nginx)
        self.assertIn("return 308 https://", nginx)
        self.assertIn("read_only: true", compose)
        self.assertEqual(compose.count("create_host_path: false"), 2)
        self.assertIn("--no-proxy-headers", compose)
        self.assertNotIn("postgresql-client", backend)
        self.assertNotIn("proxy_add_x_forwarded_for", nginx)
        self.assertIn("location = /metrics { return 404; }", nginx)

    def test_service_healthcheck_respects_section_41_probe_boundaries(self) -> None:
        async def _run() -> None:
            settings = Settings(
                environment="test",
                database_url="postgresql+asyncpg://u:p@localhost:5432/db",
                celery_broker_url="redis://localhost:6379/0",  # type: ignore[arg-type]
            )

            # API checks PostgreSQL only — broker outage does NOT make API unready!
            api_code, api_payload = await evaluate_service_probe(
                role="api",
                probe="ready",
                settings=settings,
                database_probe=lambda _url: asyncio.sleep(0, result=True),
                broker_probe=lambda _url: asyncio.sleep(0, result=False),
            )
            self.assertEqual(api_code, 0)
            self.assertEqual(api_payload["status"], "ready")
            self.assertNotIn("broker_ready", api_payload)

            # Scheduler & Worker require BOTH PostgreSQL and Redis broker!
            sched_code, sched_payload = await evaluate_service_probe(
                role="scheduler",
                probe="ready",
                settings=settings,
                database_probe=lambda _url: asyncio.sleep(0, result=True),
                broker_probe=lambda _url: asyncio.sleep(0, result=False),
            )
            self.assertEqual(sched_code, 1)
            self.assertEqual(sched_payload["blockers"], ["broker_unavailable"])
            self.assertFalse(sched_payload["broker_ready"])

            worker_ok_code, worker_ok_payload = await evaluate_service_probe(
                role="worker",
                probe="ready",
                settings=settings,
                database_probe=lambda _url: asyncio.sleep(0, result=True),
                broker_probe=lambda _url: asyncio.sleep(0, result=True),
            )
            self.assertEqual(worker_ok_code, 0)
            self.assertTrue(worker_ok_payload["broker_ready"])

        asyncio.run(_run())

    def test_worker_role_queue_mapping(self) -> None:
        self.assertEqual(
            set(QUEUES_BY_ROLE.keys()),
            {"media", "analysis", "publish", "maintenance", "all"},
        )
        media_argv = resolve_worker_argv(role="media", concurrency=3)
        self.assertIn(
            "--queues=image_archive,video_archive,range_execution",
            media_argv,
        )
        self.assertIn("--concurrency=3", media_argv)

        publish_argv = resolve_worker_argv(role="publish")
        self.assertIn("--queues=publication", publish_argv)
        self.assertIn("--concurrency=1", publish_argv)

    def test_ffmpeg_active_subprocess_terminated_cleanly_on_shutdown(self) -> None:
        extractor = register_media_extractor(
            FFmpegVideoFrameExtractor(
                ffprobe_path=sys.executable,
                ffmpeg_path=sys.executable,
                timeout_seconds=15.0,
                terminate_grace_seconds=1.0,
            )
        )
        caught: list[BaseException] = []

        def _worker_call() -> None:
            try:
                extractor._run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdout_limit=1024,
                    error_type=FFmpegMediaError,
                )
            except BaseException as exc:  # noqa: BLE001
                caught.append(exc)

        thread = threading.Thread(target=_worker_call, daemon=True)
        thread.start()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(extractor._active_processes) > 0:
                break
            time.sleep(0.05)

        terminated = terminate_registered_media_extractors()
        self.assertGreaterEqual(terminated, 1)
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(extractor._active_processes), 0)
        self.assertEqual(len(caught), 1)

    def test_scheduler_and_ingestor_daemons_stop_gracefully_and_flush(self) -> None:
        async def _run() -> None:
            sched = _FakeSchedulerRuntime()
            sched_stop = asyncio.Event()

            async def _trigger_sched_stop() -> None:
                await asyncio.sleep(0.05)
                sched_stop.set()

            asyncio.create_task(_trigger_sched_stop())
            cycles = await run_scheduler_daemon(
                runtime=sched,  # type: ignore[arg-type]
                interval_seconds=10.0,
                stop_event=sched_stop,
            )
            self.assertGreaterEqual(cycles, 1)
            self.assertTrue(sched.closed)

            ingestor = _FakeIngestorRuntime()
            ing_stop = asyncio.Event()

            async def _trigger_ing_stop() -> None:
                await asyncio.sleep(0.05)
                ing_stop.set()

            asyncio.create_task(_trigger_ing_stop())
            ticks = await run_ingestor_daemon(
                runtime=ingestor,  # type: ignore[arg-type]
                flush_interval_seconds=10.0,
                stop_event=ing_stop,
            )
            self.assertGreaterEqual(ticks, 1)
            self.assertTrue(ingestor.shutdown_called)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
