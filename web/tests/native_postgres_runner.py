# ruff: noqa: E402
# Standalone runner bootstraps this checkout before importing app and shared test helpers.
"""Run frozen frontend against migration-created PostgreSQL; all owned resources are cleaned.
No InMemory backend, DTO replay, external network, workers or file deletion is used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/integration")]
import asyncpg
import httpx
import uvicorn
from fastapi.staticfiles import StaticFiles
from postgres_support import postgres_admin_url, temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.engine import make_url

from apps.api import create_app
from apps.api.dependencies import build_default_admin_service
from tgcurator.application import Settings
from tgcurator.infrastructure.database import AsyncDatabase
from tgcurator.infrastructure.database import models as m
from tgcurator.infrastructure.database.admin_api_repository import SqlAlchemyAdminBackend
from tgcurator.infrastructure.database.secret_vault import SqlAlchemySecretVault
from tgcurator.infrastructure.security import Argon2idPasswordHasher

NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)
ARCHIVED = datetime(2026, 11, 1, tzinfo=UTC)
SENSITIVE: list[str] = []


def sanitized(value: str) -> str:
    for secret in SENSITIVE:
        if secret:
            value = value.replace(secret, "[REDACTED_TEST_SECRET]")
    return value


def dump(path: Path, data) -> None:
    path.write_text(
        sanitized(json.dumps(data, indent=2, default=str)), encoding="utf-8", newline="\n"
    )


def safe_exception(error: BaseException) -> str:
    # Traceback frames only. SQLAlchemy exception strings may include SQL parameters.
    frames = "".join(traceback.format_list(traceback.extract_tb(error.__traceback__)))
    detail = (
        str(error)
        if isinstance(error, (AttributeError, AssertionError))
        else "[exception message omitted to exclude SQL parameters]"
    )
    return sanitized(frames + type(error).__name__ + ": " + detail + "\n")


class RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg = sanitized(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = safe_exception(record.exc_info[1])
            record.exc_info = None
        return True


async def seed_http_and_database(db, vault, base, username, password, evidence):
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        response.raise_for_status()
        csrf = response.json()["csrf_token"]
        client.headers["X-CSRF-Token"] = csrf

        async def post(path, payload=None):
            response = await client.post("/api/v1" + path, json=payload or {})
            if response.status_code not in (200, 201):
                raise RuntimeError(
                    f"Seed HTTP {path}: {response.status_code} {sanitized(response.text)}"
                )
            return response.json()

        # Minimal cleanup HTTP contract: authenticated admin only, no channels/media.
        minimal = await client.post(
            "/api/v1/archive-cleanup/dry-runs", json={"archived_before": "2026-12-01T00:00:00Z"}
        )
        minimal_body = (
            minimal.json()
            if "application/json" in minimal.headers.get("content-type", "")
            else None
        )
        dump(
            evidence / "minimal-empty-cleanup.json",
            {
                "status": minimal.status_code,
                "response": minimal_body,
                "preconditions": (
                    "migrations + one active admin/session; zero source channels/messages/assets"
                ),
            },
        )
        if minimal.status_code != 201:
            raise AssertionError("Minimal zero-asset cleanup HTTP contract failed")
        assert (
            minimal_body["estimated_asset_count"] == 0
            and minimal_body["estimated_message_count"] == 0
            and minimal_body["estimated_bytes"] == 0
        )

        async def config(family, content):
            entity = await post(
                "/config/" + family,
                {"name": "native-diagnostics-" + family, "initial_version": content},
            )
            version = entity["versions"][0]
            await post(f"/config/{family}/{entity['id']}/versions/{version['id']}/publish")
            return version

        credential = secrets.token_urlsafe(32)
        SENSITIVE.append(credential)
        secret = await post(
            "/secrets", {"secret_type": "telegram_session", "plaintext": credential}
        )
        inference_secret = await post(
            "/secrets", {"secret_type": "api_key", "plaintext": credential}
        )
        identity = await post(
            "/telegram-identities",
            {
                "name": "Test reader",
                "identity_type": "mtproto_user",
                "secret_id": secret["secret_id"],
            },
        )
        source = await post(
            "/source-channels",
            {
                "telegram_peer_id": -1001234001,
                "display_name": "Test Source",
                "default_read_identity_id": identity["id"],
            },
        )
        other = await post(
            "/source-channels",
            {
                "telegram_peer_id": -1001234003,
                "display_name": "Other Source",
                "default_read_identity_id": identity["id"],
            },
        )
        dest = await post(
            "/destination-channels",
            {
                "telegram_peer_id": -1001234002,
                "name": "Test Destination",
                "default_publish_identity_id": identity["id"],
            },
        )
        media_label = await config(
            "label-definitions",
            {
                "key": "media_quality",
                "display_name": "Media Quality",
                "scope": "media",
                "negative": False,
            },
        )
        # Use an independent global label; do not alias the browser-created illustration key.
        label_entity = await post(
            "/config/label-definitions",
            {
                "name": "native-diagnostics-global",
                "initial_version": {
                    "key": "diagnostic",
                    "display_name": "Diagnostic",
                    "scope": "global",
                },
            },
        )
        label = label_entity["versions"][0]
        await post(f"/config/label-definitions/{label_entity['id']}/versions/{label['id']}/publish")
        labels = await config(
            "label-sets",
            {
                "bindings": [
                    {"label_definition_version_id": label["id"], "activation_threshold": 0.5}
                ]
            },
        )
        prompt = await config(
            "prompt-templates",
            {
                "system_prompt": "Diagnostic seed",
                "user_prompt_template": "Classify input",
                "declared_variables": [],
            },
        )
        profile = await config(
            "inference-profiles",
            {
                "base_url": "https://example.invalid/v1",
                "model_name": "native-fixture-model",
                "api_secret_reference": inference_secret["secret_id"],
                "capabilities": ["text"],
            },
        )
        stage = await config(
            "analysis-stages",
            {
                "name": "diagnostics",
                "prompt_template_version_id": prompt["id"],
                "label_set_version_id": labels["id"],
                "inference_profile_version_id": profile["id"],
                "input_policy": {"text": True},
            },
        )
        pipeline = await config(
            "analysis-pipelines",
            {
                "nodes": [
                    {
                        "node_key": "diagnostics",
                        "analysis_stage_template_version_id": stage["id"],
                        "depends_on": [],
                    }
                ]
            },
        )
        policy = await config(
            "routing-policies",
            {
                "rules": [
                    {
                        "priority": 1,
                        "stop_on_match": True,
                        "condition": {"fact": "review_status", "op": "eq", "value": "unreviewed"},
                        "actions": [
                            {
                                "destination_channel_id": dest["id"],
                                "publication_mode": "forward_only",
                                "rendering_template_version_id": None,
                            }
                        ],
                    }
                ]
            },
        )
        ids = [UUID(int=i) for i in range(1, 31)]
        async with db.session() as session, session.begin():
            for i, mid in enumerate(ids, 1):
                published = NOW.replace(hour=1, minute=0, second=i)
                session.add(
                    m.MessageRecord(
                        id=mid,
                        source_channel_id=UUID(source["id"]),
                        telegram_message_ids=[i],
                        primary_telegram_message_id=i,
                        original_text=f"Test message {i:02d}",
                        published_at=published,
                        processing_status="processed",
                        terminal_reason="completed",
                        review_status="unreviewed",
                        duplicate_status="unique",
                        media_count=2,
                    )
                )
            await session.flush()
            for i, mid in enumerate(ids, 1):
                session.add(
                    m.MessagePartRecord(
                        id=uuid4(),
                        message_id=mid,
                        source_channel_id=UUID(source["id"]),
                        telegram_message_id=i,
                        published_at=NOW.replace(hour=1, minute=0, second=i),
                        original_text=f"Test message {i:02d}",
                        media=[{"kind": "photo"}, {"kind": "video"}],
                        media_count=2,
                    )
                )
                session.add(
                    m.ImageAssetRecord(
                        id=UUID(int=1000 + i),
                        message_id=mid,
                        source_asset_id=f"native-image-{i}",
                        source_telegram_message_id=i,
                        archive_state="ready",
                        storage_backend="local",
                        storage_key=f"native/{i}.webp",
                        content_type="image/webp",
                        width=1024,
                        height=768,
                        source_sha256="a" * 64,
                        archive_sha256="b" * 64,
                        perceptual_hash="1" * 16,
                        archive_size_bytes=2048,
                        archive_ready_at=ARCHIVED,
                    )
                )
                session.add(
                    m.VideoAssetRecord(
                        id=UUID(int=2000 + i),
                        message_id=mid,
                        source_asset_id=f"native-video-{i}",
                        source_telegram_message_id=i,
                        archive_state="ready",
                        source_cover_phash="2" * 16,
                        source_sha256="c" * 64,
                        duration_seconds=10,
                        source_width=1024,
                        source_height=768,
                        archive_ready_at=ARCHIVED,
                    )
                )
            await session.flush()
            manifest_id, call_id, run_id, sr_id = (uuid4() for _ in range(4))
            session.add(
                m.InputManifestRecord(
                    id=manifest_id,
                    analysis_stage_template_version_id=UUID(stage["id"]),
                    manifest_version=1,
                    content={
                        "message_text": "Test message 01",
                        "targets": [{"target_id": str(ids[0])}],
                    },
                    input_manifest_hash="a" * 64,
                )
            )
            session.add(
                m.AnalysisRunRecord(
                    id=run_id,
                    message_id=ids[0],
                    analysis_pipeline_version_id=UUID(pipeline["id"]),
                    run_mode="formal",
                    status="completed",
                    started_at=NOW,
                    completed_at=NOW,
                )
            )
            await session.flush()
            session.add(
                m.InferenceCallRecord(
                    id=call_id,
                    input_manifest_id=manifest_id,
                    analysis_stage_template_version_id=UUID(stage["id"]),
                    provider_adapter="openai_compatible",
                    model_name="native-fixture-model",
                    request_summary={"fixture": "external-inference-boundary-only"},
                    structured_output_schema={"type": "object"},
                    structured_output_schema_hash="b" * 64,
                    status="succeeded",
                    attempt=1,
                    raw_response={"fixture": "seeded-external-response"},
                    token_usage={"input_tokens": 123, "output_tokens": 7},
                    latency_ms=25,
                    http_status=200,
                    completed_at=NOW,
                )
            )
            await session.flush()
            session.add(
                m.StageRunRecord(
                    id=sr_id,
                    analysis_run_id=run_id,
                    analysis_stage_template_version_id=UUID(stage["id"]),
                    message_id=ids[0],
                    target_scope="global",
                    target_kind="message",
                    target_id=ids[0],
                    status="succeeded",
                    attempt_count=1,
                    input_manifest_id=manifest_id,
                    inference_call_id=call_id,
                    parsed_result={"scores": {"diagnostic": 0.8}},
                    result_origin="inference",
                    completed_at=NOW,
                )
            )
        # Real routing persistence creates the evaluation and intent; no publisher is started.
        routed = await post(
            "/messages/" + str(ids[0]) + "/reroute",
            {
                "routing_policy_version_id": policy["id"],
                "dry_run": False,
                "reason": "Native UI diagnostic seed; no publication worker",
            },
        )
        detail_response = await client.get("/api/v1/messages/" + str(ids[0]))
        detail_response.raise_for_status()
        detail = detail_response.json()
        assert (
            detail["analysis_runs"][0]["stage_runs"][0]["input_manifest"]["input_manifest_hash"]
            == "a" * 64
        )
        assert detail["publication_intents"] and detail["routing_evaluations"]
        fixture = {
            "source_id": source["id"],
            "other_source_id": other["id"],
            "destination_id": dest["id"],
            "identity_id": identity["id"],
            "inference_secret_id": inference_secret["secret_id"],
            "media_label_version_id": media_label["id"],
            "message_ids": [str(i) for i in ids],
            "diagnostic_evaluation_id": routed["evaluation_id"],
        }
        dump(evidence / "fixture.json", fixture)
        dump(evidence / "native-detail-before.json", detail)
        # Verify real SQL vault encryption/decryption without exposing the credential.
        async with db.session() as session:
            encrypted = await session.get(m.EncryptedSecret, UUID(secret["secret_id"]))
            assert (
                encrypted.ciphertext != credential.encode() and encrypted.nonce and encrypted.key_id
            )
        assert await vault.resolve(secret_id=UUID(secret["secret_id"])) == credential.encode()
        await post("/auth/logout")
        return fixture


async def run_native(database_url: str, args) -> int:
    database_name = make_url(database_url).database
    if not database_name or not database_name.startswith("tgcurator_test_web_"):
        raise RuntimeError("refusing non-owned database")
    password = secrets.token_urlsafe(32)
    master = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    SENSITIVE.extend([password, master, database_url, make_url(database_url).password or ""])
    username = "native-ui-admin"
    settings = Settings(
        _env_file=None,
        environment="test",
        log_level="WARNING",
        app_name="native-web-regression",
        database_url=database_url,
        app_master_key=master,
        archive_root=None,
        temp_media_dir=None,
        celery_broker_url=None,
        telegram_api_id=None,
        telegram_api_hash=None,
    )
    db = AsyncDatabase(database_url)
    service = build_default_admin_service(settings=settings, database=db)
    assert isinstance(service._backend, SqlAlchemyAdminBackend)
    assert isinstance(service._secrets, SqlAlchemySecretVault)
    app = create_app(settings, db, admin_service=service)

    # Explicitly reject provider invocation in this isolated fixture. This is an external boundary,
    # while routes, services, repositories, migrations, auth, vault and all responses remain real.
    @app.middleware("http")
    async def prevent_external_invocation(request, call_next):
        if request.url.path == "/api/v1/analysis/test-bench" and request.method == "POST":
            body = await request.json()
            if body.get("invoke_provider", True):
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {
                        "error": {
                            "code": "external_calls_disabled",
                            "message": "Native UI regression requires invoke_provider=false",
                        }
                    },
                    status_code=403,
                )
        try:
            return await call_next(request)
        except Exception as error:
            with (args.evidence / "server-exceptions.log").open(
                "a", encoding="utf-8", newline="\n"
            ) as output:
                output.write(
                    request.method + " " + request.url.path + "\n" + safe_exception(error) + "\n"
                )
            raise

    app.mount("/", StaticFiles(directory=ROOT / "web/dist", html=True), name="native-web-static")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactFilter())
    async with db.session() as session, session.begin():
        session.add(
            m.AdminUser(
                id=uuid4(),
                username=username,
                password_hash=Argon2idPasswordHasher().hash(password),
                is_active=True,
            )
        )
    async with db.session() as session:
        version = (await session.execute(text("SHOW server_version"))).scalar_one()
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
    )
    server_task = asyncio.create_task(server.serve())
    browser = None
    exit_code = 1
    try:
        for _ in range(100):
            if server.started:
                break
            if server_task.done():
                await server_task
                raise RuntimeError("native HTTP server exited before readiness")
            await asyncio.sleep(0.1)
        if not server.started:
            raise TimeoutError("native HTTP readiness timeout")
        base = f"http://127.0.0.1:{args.port}"
        await seed_http_and_database(db, service._secrets, base, username, password, args.evidence)
        dump(
            args.evidence / "native-runtime.json",
            {
                "database": database_name,
                "postgres_version": version,
                "migration_revision": revision,
                "backend": type(service._backend).__name__,
                "vault": type(service._secrets).__name__,
                "base_url": base,
                "mode": "real PostgreSQL HTTP, no DTO replay",
                "process_id": os.getpid(),
            },
        )
        env = os.environ.copy()
        env.update(
            TEST_BASE_URL=base,
            TEST_USERNAME=username,
            TEST_PASSWORD=password,
            TEST_NATIVE_POSTGRES="1",
        )
        browser = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "web/tests/browser-regression.cjs"),
            str(args.evidence),
            cwd=ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(browser.communicate(), timeout=240)
        (args.evidence / "browser.log").write_text(
            sanitized(output.decode("utf-8", errors="replace")), encoding="utf-8", newline="\n"
        )
        exit_code = browser.returncode
        async with db.session() as session:
            counts = {}
            for table in [
                "admin_sessions",
                "encrypted_secrets",
                "source_channels",
                "messages",
                "image_assets",
                "video_assets",
                "analysis_runs",
                "input_manifests",
                "inference_calls",
                "stage_runs",
                "manual_label_assignments",
                "routing_evaluations",
                "publication_intents",
                "archive_cleanup_dry_runs",
                "archive_cleanup_jobs",
                "archive_cleanup_items",
            ]:
                counts[table] = (
                    await session.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one()
            source_profiles = (
                (
                    await session.execute(
                        text(
                            "SELECT id, active_profile_version_id FROM source_channels "
                            "WHERE active_profile_version_id IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            deleted_images = (
                await session.execute(
                    text("SELECT count(*) FROM image_assets WHERE archive_state='deleted'")
                )
            ).scalar_one()
            deleted_videos = (
                await session.execute(
                    text("SELECT count(*) FROM video_assets WHERE archive_state='deleted'")
                )
            ).scalar_one()
        dump(
            args.evidence / "native-database-verification.json",
            {
                "counts": counts,
                "bound_sources": [dict(x) for x in source_profiles],
                "deleted_images": deleted_images,
                "deleted_videos": deleted_videos,
                "browser_exit": exit_code,
            },
        )
        if exit_code:
            raise AssertionError("Native Chrome regression failed; see sanitized browser.log")
        assert counts["archive_cleanup_jobs"] == 1 and counts["archive_cleanup_items"] > 0
        assert counts["manual_label_assignments"] >= 4 and source_profiles
        assert not deleted_images and not deleted_videos
    finally:
        if browser is not None and browser.returncode is None:
            browser.terminate()
            await browser.wait()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=20)
        await db.dispose()
    return exit_code


async def verify_removed(name: str) -> bool:
    parsed = make_url(postgres_admin_url())
    connection = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
        timeout=10,
    )
    try:
        return not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name
        )
    finally:
        await connection.close()


def resolve_git_common_dir(repo_root: Path) -> Path:
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return dot_git.resolve()
    if dot_git.is_file():
        header = dot_git.read_text(encoding="utf-8").strip()
        if header.startswith("gitdir:"):
            raw_gitdir = header.split(":", 1)[1].strip()
            git_dir = Path(raw_gitdir)
            if not git_dir.is_absolute():
                git_dir = (repo_root / git_dir).resolve()
            commondir_file = git_dir / "commondir"
            if commondir_file.is_file():
                raw_common = commondir_file.read_text(encoding="utf-8").strip()
                common_dir = Path(raw_common)
                if not common_dir.is_absolute():
                    common_dir = (git_dir / common_dir).resolve()
                return common_dir
            return git_dir
    return dot_git


def main() -> int:
    git_common_dir = resolve_git_common_dir(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence",
        type=Path,
        default=git_common_dir / "fix-2026-11-12/pr30/native",
    )
    parser.add_argument(
        "--services",
        type=Path,
        default=git_common_dir / "fix-2026-11-12/test-services.json",
    )
    parser.add_argument("--port", type=int, default=18732)
    args = parser.parse_args()
    args.evidence = args.evidence.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    config = json.loads(args.services.read_text(encoding="utf-8"))
    if (
        config.get("host") not in {"127.0.0.1", "localhost", "::1"}
        or config.get("admin_database") != "postgres"
    ):
        raise RuntimeError(
            "native harness only accepts the explicit local postgres maintenance service"
        )
    SENSITIVE.append(config["password"])
    os.environ["TGCURATOR_TEST_SERVICES_FILE"] = str(args.services.resolve())
    os.environ.pop("TGCURATOR_TEST_POSTGRES_ADMIN_URL", None)
    before = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "web/src").rglob("*")
        if p.is_file()
    }
    name = None
    success = False
    try:
        with temporary_postgres_database("web_ui", revision="head") as url:
            name = make_url(url).database
            asyncio.run(run_native(url, args))
        success = True
        print("PASS: real PostgreSQL native web regression; credentials not emitted")
        return 0
    except BaseException as error:
        (args.evidence / "native-error.log").write_text(
            safe_exception(error), encoding="utf-8", newline="\n"
        )
        print("FAIL: native web regression; see sanitized native-error.log")
        return 1
    finally:
        after = {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "web/src").rglob("*")
            if p.is_file()
        }
        removed = asyncio.run(verify_removed(name)) if name else True
        dump(
            args.evidence / "cleanup-verification.json",
            {
                "owned_database": name,
                "database_removed": removed,
                "source_freeze_unchanged": before == after,
                "success": success,
                "server_shutdown_requested": True,
            },
        )
        if before != after:
            print(
                "WARNING: web/src changed outside this harness; "
                "compare the main-line freeze snapshot"
            )
        if not removed:
            print("WARNING: owned database remains; cleanup requires investigation")


if __name__ == "__main__":
    raise SystemExit(main())
