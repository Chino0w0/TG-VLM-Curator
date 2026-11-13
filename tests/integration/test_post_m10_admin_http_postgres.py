"""Post-M10 HTTP + PostgreSQL 16 integration suite for Admin API hardening.

Opt in with TGCURATOR_TEST_POSTGRES_ADMIN_URL or TGCURATOR_TEST_SERVICES_FILE.
Exercises FastAPI routers + AdminApplicationService + SqlAlchemyAdminBackend +
SqlAlchemySecretVault against migration-created isolated PostgreSQL 16 databases.
"""

from __future__ import annotations

import base64
import json
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from postgres_support import temporary_postgres_database
from sqlalchemy import event, select, text

from apps.api import create_app
from apps.api.dependencies import _VaultInferenceSecretResolver, build_default_admin_service
from tgcurator.application import Settings
from tgcurator.application.admin import (
    SESSION_COOKIE_NAME,
    AdminApplicationService,
    AdminSessionService,
    PromptTestBenchService,
)
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SecretTypeMismatchError,
    SqlAlchemyAdminBackend,
    SqlAlchemyArchiveCleanupRepository,
    SqlAlchemySecretVault,
)
from tgcurator.infrastructure.database import (
    models as m,
)
from tgcurator.infrastructure.security import AesGcmSecretCipher, Argon2idPasswordHasher

MASTER_KEY_B64 = base64.b64encode(b"post-m10-pg-master-key-32-bytes!").decode("ascii")


class PostM10AdminHttpPostgresTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database_context = temporary_postgres_database("post_m10_admin")
        self.database_url = self.database_context.__enter__()
        self.addCleanup(self.database_context.__exit__, None, None, None)
        self.db = AsyncDatabase(self.database_url)

    async def asyncSetUp(self) -> None:
        self.addAsyncCleanup(self.db.dispose)
        self.clock = [datetime(2026, 11, 12, 12, 0, tzinfo=UTC)]
        self.hasher = Argon2idPasswordHasher()
        self.admin_id = uuid4()
        self.admin_password = "correct-horse-battery-staple"
        async with self.db.session() as session, session.begin():
            session.add(
                m.AdminUser(
                    id=self.admin_id,
                    username="curator-admin",
                    password_hash=self.hasher.hash(self.admin_password),
                    is_active=True,
                )
            )

        self.cipher = AesGcmSecretCipher(master_key=MASTER_KEY_B64, key_id="pg-key-1")
        self.vault = SqlAlchemySecretVault(self.db, self.cipher)
        self.backend = SqlAlchemyAdminBackend(self.db, allow_local_inference=False)
        self.sessions = AdminSessionService(
            repository=self.backend,
            password_hasher=self.hasher,
            csrf_secret=b"post-m10-pg-csrf-secret-32-bytes",
            session_ttl=timedelta(hours=12),
            clock=lambda: self.clock[0],
        )
        self.admin_service = AdminApplicationService(
            sessions=self.sessions,
            secrets=self.vault,
            backend=self.backend,
            test_bench=PromptTestBenchService(provider=None),
            clock=lambda: self.clock[0],
            allow_local_inference=False,
        )
        self.settings = Settings(
            environment="test",
            database_url=self.database_url,
            app_master_key=MASTER_KEY_B64,
            app_master_key_id="pg-key-1",
            cookie_secure=False,
        )
        self.app = create_app(
            settings=self.settings,
            database=self.db,
            admin_service=self.admin_service,
        )

    async def _login(self, client: httpx.AsyncClient) -> dict[str, str]:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "curator-admin", "password": self.admin_password},
        )
        self.assertEqual(resp.status_code, 200)
        return {"X-CSRF-Token": resp.json()["csrf_token"]}

    async def test_http_auth_422_redaction_secret_isolation_ssrf_and_touch_throttling(self) -> None:
        # 1. Fail-closed master-key check when AsyncDatabase is configured
        no_key_settings = Settings(
            environment="test",
            database_url=self.database_url,
            app_master_key=None,
        )
        with self.assertRaisesRegex(RuntimeError, "APP_MASTER_KEY"):
            build_default_admin_service(settings=no_key_settings, database=self.db)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        ) as client:
            # 2. RequestValidationError 422 strips plaintext passwords & secrets
            leaked_pw = "super-sensitive-login-passphrase-999"
            r_422_login = await client.post(
                "/api/v1/auth/login",
                json={"username": 12345, "password": leaked_pw},
            )
            self.assertEqual(r_422_login.status_code, 422)
            self.assertNotIn(leaked_pw, r_422_login.text)
            for item in r_422_login.json()["detail"]:
                self.assertNotIn("input", item)
                self.assertNotIn("ctx", item)

            # 3. Unknown username + wrong password audit events in PostgreSQL
            unknown_pw = "unknown-user-attempt-secret"
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/auth/login",
                        json={"username": "intruder", "password": unknown_pw},
                    )
                ).status_code,
                401,
            )
            wrong_pw = "wrong-known-admin-secret"
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/auth/login",
                        json={"username": "curator-admin", "password": wrong_pw},
                    )
                ).status_code,
                401,
            )

            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "curator-admin", "password": self.admin_password},
            )
            self.assertEqual(login.status_code, 200)
            set_cookie = login.headers.get("set-cookie", "").lower()
            self.assertIn(SESSION_COOKIE_NAME, set_cookie)
            self.assertIn("max-age=43200", set_cookie)
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}

            audits = (
                await client.get(
                    "/api/v1/audit-events",
                    params={"event_type": "admin.login_failed"},
                )
            ).json()["items"]
            self.assertEqual(len(audits), 2)
            self.assertEqual(
                {a["payload"]["reason"] for a in audits},
                {"unknown_or_inactive_user", "invalid_password"},
            )
            dumped_audits = json.dumps(audits)
            self.assertNotIn(unknown_pw, dumped_audits)
            self.assertNotIn(wrong_pw, dumped_audits)

            # 4. 60-second touch_session write throttling verified directly on PostgreSQL row
            async with self.db.session() as session:
                sess_row = await session.scalar(select(m.AdminSessionRecord))
                assert sess_row is not None
                initial_seen = sess_row.last_seen_at
            self.assertEqual(initial_seen, self.clock[0])

            self.clock[0] = self.clock[0] + timedelta(seconds=45)
            self.assertEqual((await client.get("/api/v1/auth/me")).status_code, 200)
            async with self.db.session() as session:
                sess_row = await session.scalar(select(m.AdminSessionRecord))
                assert sess_row is not None
                self.assertEqual(sess_row.last_seen_at, initial_seen)

            self.clock[0] = self.clock[0] + timedelta(seconds=15)
            self.assertEqual((await client.get("/api/v1/auth/me")).status_code, 200)
            async with self.db.session() as session:
                sess_row = await session.scalar(select(m.AdminSessionRecord))
                assert sess_row is not None
                self.assertEqual(sess_row.last_seen_at, self.clock[0])

            # 5. 422 secret redaction on /api/v1/secrets
            leaked_secret = "sk-live-ultra-secret-never-echo-422"
            r_422_sec = await client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": 12345, "plaintext": leaked_secret},
            )
            self.assertEqual(r_422_sec.status_code, 422)
            self.assertNotIn(leaked_secret, r_422_sec.text)

            # 6. Cross-purpose secret_type isolation + SSRF base_url guard
            api_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "api_key", "plaintext": "sk-pg-openai-key"},
                )
            ).json()["secret_id"]
            bot_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "bot_token", "plaintext": "123456:ABCDEF-bot"},
                )
            ).json()["secret_id"]
            tg_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "telegram_session", "plaintext": "1BVtsOHwBu-tg"},
                )
            ).json()["secret_id"]

            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/telegram-identities",
                        headers=headers,
                        json={
                            "name": "bad-mt",
                            "identity_type": "mtproto_user",
                            "secret_id": bot_sec,
                        },
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/telegram-identities",
                        headers=headers,
                        json={"name": "bad-bot", "identity_type": "bot", "secret_id": tg_sec},
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/inference-profiles",
                        headers=headers,
                        json={
                            "name": "bad-inf-secret",
                            "initial_version": {
                                "base_url": "https://api.openai.com/v1",
                                "model_name": "gpt-4o-mini",
                                "api_secret_reference": tg_sec,
                            },
                        },
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/inference-profiles",
                        headers=headers,
                        json={
                            "name": "ssrf-metadata",
                            "initial_version": {
                                "base_url": "http://169.254.169.254/latest/meta-data/",
                                "model_name": "gpt-4o-mini",
                                "api_secret_reference": api_sec,
                            },
                        },
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/inference-profiles",
                        headers=headers,
                        json={
                            "name": "good-inf",
                            "initial_version": {
                                "base_url": "https://api.openai.com/v1",
                                "model_name": "gpt-4o-mini",
                                "api_secret_reference": api_sec,
                            },
                        },
                    )
                ).status_code,
                201,
            )

            resolver = _VaultInferenceSecretResolver(self.vault)
            self.assertEqual(await resolver.resolve(secret_reference=api_sec), "sk-pg-openai-key")
            with self.assertRaises(SecretTypeMismatchError):
                await resolver.resolve(secret_reference=bot_sec)

    async def test_config_sql_cross_refs_n_plus_one_batching_channel_and_range_guards(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        ) as client:
            headers = await self._login(client)

            tg_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "telegram_session", "plaintext": "1BVtsOHwBu-sess"},
                )
            ).json()["secret_id"]
            bot_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "bot_token", "plaintext": "777777:ABCDEF-bot"},
                )
            ).json()["secret_id"]
            mt_id = (
                await client.post(
                    "/api/v1/telegram-identities",
                    headers=headers,
                    json={
                        "name": "mt-reader",
                        "identity_type": "mtproto_user",
                        "secret_id": tg_sec,
                    },
                )
            ).json()["id"]
            bot_id = (
                await client.post(
                    "/api/v1/telegram-identities",
                    headers=headers,
                    json={"name": "bot-pub", "identity_type": "bot", "secret_id": bot_sec},
                )
            ).json()["id"]

            # 1. Malformed non-object items & non-string template_text -> 422 (never 500)
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/label-sets",
                        headers=headers,
                        json={"name": "bad-ls", "initial_version": {"bindings": ["not-a-dict"]}},
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/analysis-pipelines",
                        headers=headers,
                        json={"name": "bad-pipe", "initial_version": {"nodes": [123]}},
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/routing-policies",
                        headers=headers,
                        json={"name": "bad-rp", "initial_version": {"rules": ["not-a-dict"]}},
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/rendering-templates",
                        headers=headers,
                        json={"name": "bad-tpl", "initial_version": {"template_text": 12345}},
                    )
                ).status_code,
                422,
            )

            # 2. Duplicate content_hash version on same entity -> 409 Conflict
            tpl = (
                await client.post(
                    "/api/v1/config/rendering-templates",
                    headers=headers,
                    json={"name": "main-tpl", "initial_version": {"template_text": "{{ text }}"}},
                )
            ).json()
            tpl_id = tpl["id"]
            tpl_vid = tpl["versions"][0]["id"]
            self.assertEqual(
                (
                    await client.post(
                        f"/api/v1/config/rendering-templates/{tpl_id}/versions",
                        headers=headers,
                        json={"content": {"template_text": "{{ text }}"}},
                    )
                ).status_code,
                409,
            )

            # 3. Routing policy cross-ref & published template requirement
            dest_id = (
                await client.post(
                    "/api/v1/destination-channels",
                    headers=headers,
                    json={
                        "name": "curated-dest",
                        "telegram_peer_id": -1005501,
                        "default_publish_identity_id": bot_id,
                    },
                )
            ).json()["id"]

            # Nonexistent destination rejected on draft creation
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/routing-policies",
                        headers=headers,
                        json={
                            "name": "missing-dest-rp",
                            "initial_version": {
                                "rules": [
                                    {
                                        "priority": 1,
                                        "condition": {
                                            "label": {
                                                "namespace": "effective",
                                                "scope": "global",
                                                "key": "sample",
                                            }
                                        },
                                        "actions": [
                                            {
                                                "destination_channel_id": str(uuid4()),
                                                "publish_identity_id": bot_id,
                                                "publication_mode": "forward_only",
                                            }
                                        ],
                                    }
                                ]
                            },
                        },
                    )
                ).status_code,
                422,
            )

            # Draft rendering template allowed in draft policy, rejected on publish until published
            rp = (
                await client.post(
                    "/api/v1/config/routing-policies",
                    headers=headers,
                    json={
                        "name": "rp-1",
                        "initial_version": {
                            "rules": [
                                {
                                    "priority": 1,
                                    "condition": {
                                        "label": {
                                            "namespace": "effective",
                                            "scope": "global",
                                            "key": "sample",
                                        }
                                    },
                                    "actions": [
                                        {
                                            "destination_channel_id": dest_id,
                                            "publish_identity_id": bot_id,
                                            "publication_mode": "copy_with_caption",
                                            "rendering_template_version_id": tpl_vid,
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                )
            ).json()
            rp_id = rp["id"]
            rp_vid = rp["versions"][0]["id"]
            self.assertEqual(
                (
                    await client.post(
                        f"/api/v1/config/routing-policies/{rp_id}/versions/{rp_vid}/publish",
                        headers=headers,
                        json={},
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        f"/api/v1/config/rendering-templates/{tpl_id}/versions/{tpl_vid}/publish",
                        headers=headers,
                        json={},
                    )
                ).status_code,
                200,
            )
            self.assertEqual(
                (
                    await client.post(
                        f"/api/v1/config/routing-policies/{rp_id}/versions/{rp_vid}/publish",
                        headers=headers,
                        json={},
                    )
                ).status_code,
                200,
            )

            # Seed 6 additional routing policies and assert batched SQL query count (no N+1)
            for idx in range(6):
                await client.post(
                    "/api/v1/config/routing-policies",
                    headers=headers,
                    json={
                        "name": f"rp-batch-{idx}",
                        "initial_version": {
                            "rules": [
                                {
                                    "priority": 1,
                                    "condition": {
                                        "label": {
                                            "namespace": "effective",
                                            "scope": "global",
                                            "key": "sample",
                                        }
                                    },
                                    "actions": [
                                        {
                                            "destination_channel_id": dest_id,
                                            "publish_identity_id": bot_id,
                                            "publication_mode": "forward_only",
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                )

            select_statements: list[str] = []

            def _count_sql(_conn, _cursor, statement, _params, _ctx, _many) -> None:
                if statement.lstrip().upper().startswith("SELECT"):
                    select_statements.append(statement)

            sync_engine = self.db._engine.sync_engine
            event.listen(sync_engine, "before_cursor_execute", _count_sql)
            try:
                rp_list = await client.get("/api/v1/config/routing-policies")
            finally:
                event.remove(sync_engine, "before_cursor_execute", _count_sql)
            self.assertEqual(rp_list.status_code, 200)
            self.assertEqual(len(rp_list.json()["items"]), 7)
            # Auth session SELECT + 4 batched config tables (entities, versions, rules, actions)
            self.assertLessEqual(len(select_statements), 7)

            # 4. SourceChannel reader identity + SourceChannelProfile cross-ref & channel-ownership
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/source-channels",
                        headers=headers,
                        json={
                            "telegram_peer_id": -1006600,
                            "display_name": "Bot Reader Rejected",
                            "default_read_identity_id": bot_id,
                        },
                    )
                ).status_code,
                422,
            )
            src_a = (
                await client.post(
                    "/api/v1/source-channels",
                    headers=headers,
                    json={
                        "telegram_peer_id": -1006601,
                        "display_name": "Source A",
                        "default_read_identity_id": mt_id,
                    },
                )
            ).json()["id"]
            src_b = (
                await client.post(
                    "/api/v1/source-channels",
                    headers=headers,
                    json={
                        "telegram_peer_id": -1006602,
                        "display_name": "Source B",
                        "default_read_identity_id": mt_id,
                    },
                )
            ).json()["id"]

            # Nonexistent media_processing_profile_version_id rejected on source-channel-profiles
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/config/source-channel-profiles",
                        headers=headers,
                        json={
                            "name": "bad-ref-scp",
                            "source_channel_id": src_a,
                            "initial_version": {
                                "media_processing_profile_version_id": str(uuid4()),
                            },
                        },
                    )
                ).status_code,
                422,
            )

            scp_b = (
                await client.post(
                    "/api/v1/config/source-channel-profiles",
                    headers=headers,
                    json={
                        "name": "scp-b",
                        "source_channel_id": src_b,
                        "initial_version": {"routing_policy_version_id": rp_vid},
                    },
                )
            ).json()
            scp_b_vid = scp_b["versions"][0]["id"]
            # Draft profile rejected on SourceChannel
            self.assertEqual(
                (
                    await client.patch(
                        f"/api/v1/source-channels/{src_b}",
                        headers=headers,
                        json={"active_profile_version_id": scp_b_vid},
                    )
                ).status_code,
                422,
            )
            await client.post(
                f"/api/v1/config/source-channel-profiles/{scp_b['id']}/versions/{scp_b_vid}/publish",
                headers=headers,
                json={},
            )
            # Published profile owned by src_b rejected on src_a, accepted on src_b
            self.assertEqual(
                (
                    await client.patch(
                        f"/api/v1/source-channels/{src_a}",
                        headers=headers,
                        json={"active_profile_version_id": scp_b_vid},
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.patch(
                        f"/api/v1/source-channels/{src_b}",
                        headers=headers,
                        json={"active_profile_version_id": scp_b_vid},
                    )
                ).status_code,
                200,
            )

            # 5. ProcessingRange validation + pause/resume state guards
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/processing-ranges",
                        headers=headers,
                        json={
                            "source_channel_id": src_b,
                            "start_at": "2026-11-10T00:00:00Z",
                            "end_mode": "fixed",
                            "end_at": "2026-11-09T00:00:00Z",
                            "steady_after_seconds": 60,
                        },
                    )
                ).status_code,
                422,
            )
            rng = (
                await client.post(
                    "/api/v1/processing-ranges",
                    headers=headers,
                    json={
                        "source_channel_id": src_b,
                        "start_at": "2026-11-01T00:00:00Z",
                        "end_mode": "latest",
                        "steady_after_seconds": 60,
                    },
                )
            ).json()
            rng_id = rng["id"]
            # Active range rejects no-op resume -> 409
            self.assertEqual(
                (
                    await client.patch(
                        f"/api/v1/processing-ranges/{rng_id}",
                        headers=headers,
                        json={"status": "active"},
                    )
                ).status_code,
                409,
            )
            # Pause succeeds -> resume succeeds
            self.assertEqual(
                (
                    await client.patch(
                        f"/api/v1/processing-ranges/{rng_id}",
                        headers=headers,
                        json={"status": "paused"},
                    )
                ).status_code,
                200,
            )
            self.assertEqual(
                (
                    await client.patch(
                        f"/api/v1/processing-ranges/{rng_id}",
                        headers=headers,
                        json={"status": "active"},
                    )
                ).status_code,
                200,
            )

    async def test_messages_dto_contracts_escaped_ilike_operations_cleanup_replay_and_metrics(
        self,
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        ) as client:
            headers = await self._login(client)

            api_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "api_key", "plaintext": "sk-pg-inf"},
                )
            ).json()["secret_id"]
            tg_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "telegram_session", "plaintext": "1BVtsOHwBu-s"},
                )
            ).json()["secret_id"]
            bot_sec = (
                await client.post(
                    "/api/v1/secrets",
                    headers=headers,
                    json={"secret_type": "bot_token", "plaintext": "888888:ABCDEF-b"},
                )
            ).json()["secret_id"]
            mt_id = (
                await client.post(
                    "/api/v1/telegram-identities",
                    headers=headers,
                    json={"name": "mt-ops", "identity_type": "mtproto_user", "secret_id": tg_sec},
                )
            ).json()["id"]
            bot_id = (
                await client.post(
                    "/api/v1/telegram-identities",
                    headers=headers,
                    json={"name": "bot-ops", "identity_type": "bot", "secret_id": bot_sec},
                )
            ).json()["id"]
            src_id = (
                await client.post(
                    "/api/v1/source-channels",
                    headers=headers,
                    json={
                        "telegram_peer_id": -1007701,
                        "display_name": "Ops Source",
                        "default_read_identity_id": mt_id,
                    },
                )
            ).json()["id"]
            dest_id = (
                await client.post(
                    "/api/v1/destination-channels",
                    headers=headers,
                    json={
                        "name": "Ops Dest",
                        "telegram_peer_id": -1007702,
                        "default_publish_identity_id": bot_id,
                    },
                )
            ).json()["id"]

            # Build published analysis + routing + source profile stack
            lbl = (
                await client.post(
                    "/api/v1/config/label-definitions",
                    headers=headers,
                    json={
                        "name": "nsfw-lbl",
                        "initial_version": {
                            "key": "nsfw_block",
                            "display_name": "NSFW Block",
                            "scope": "global",
                            "negative": True,
                        },
                    },
                )
            ).json()
            lbl_vid = lbl["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/label-definitions/{lbl['id']}/versions/{lbl_vid}/publish",
                headers=headers,
                json={},
            )
            prompt = (
                await client.post(
                    "/api/v1/config/prompt-templates",
                    headers=headers,
                    json={
                        "name": "ops-prompt",
                        "initial_version": {
                            "system_prompt": "Check",
                            "user_prompt_template": "Check",
                            "declared_variables": [],
                        },
                    },
                )
            ).json()
            prompt_vid = prompt["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/prompt-templates/{prompt['id']}/versions/{prompt_vid}/publish",
                headers=headers,
                json={},
            )
            ip = (
                await client.post(
                    "/api/v1/config/inference-profiles",
                    headers=headers,
                    json={
                        "name": "ops-ip",
                        "initial_version": {
                            "base_url": "https://api.openai.com/v1",
                            "model_name": "gpt-4o-mini",
                            "api_secret_reference": api_sec,
                        },
                    },
                )
            ).json()
            ip_vid = ip["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/inference-profiles/{ip['id']}/versions/{ip_vid}/publish",
                headers=headers,
                json={},
            )
            lset = (
                await client.post(
                    "/api/v1/config/label-sets",
                    headers=headers,
                    json={
                        "name": "ops-lset",
                        "initial_version": {
                            "bindings": [
                                {
                                    "label_definition_version_id": lbl_vid,
                                    "activation_threshold": 0.5,
                                }
                            ]
                        },
                    },
                )
            ).json()
            lset_vid = lset["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/label-sets/{lset['id']}/versions/{lset_vid}/publish",
                headers=headers,
                json={},
            )
            stage = (
                await client.post(
                    "/api/v1/config/analysis-stages",
                    headers=headers,
                    json={
                        "name": "ops-stage",
                        "initial_version": {
                            "name": "gate",
                            "prompt_template_version_id": prompt_vid,
                            "label_set_version_id": lset_vid,
                            "inference_profile_version_id": ip_vid,
                            "input_policy": {"text": True},
                        },
                    },
                )
            ).json()
            stage_vid = stage["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/analysis-stages/{stage['id']}/versions/{stage_vid}/publish",
                headers=headers,
                json={},
            )
            pipe = (
                await client.post(
                    "/api/v1/config/analysis-pipelines",
                    headers=headers,
                    json={
                        "name": "ops-pipe",
                        "initial_version": {
                            "nodes": [
                                {
                                    "node_key": "root_gate",
                                    "analysis_stage_template_version_id": stage_vid,
                                    "depends_on": [],
                                }
                            ]
                        },
                    },
                )
            ).json()
            pipe_vid = pipe["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/analysis-pipelines/{pipe['id']}/versions/{pipe_vid}/publish",
                headers=headers,
                json={},
            )
            rp = (
                await client.post(
                    "/api/v1/config/routing-policies",
                    headers=headers,
                    json={
                        "name": "ops-rp",
                        "initial_version": {
                            "rules": [
                                {
                                    "priority": 1,
                                    "condition": {
                                        "label": {
                                            "namespace": "effective",
                                            "scope": "global",
                                            "key": "sample",
                                        }
                                    },
                                    "actions": [
                                        {
                                            "destination_channel_id": dest_id,
                                            "publish_identity_id": bot_id,
                                            "publication_mode": "forward_only",
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                )
            ).json()
            rp_vid = rp["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/routing-policies/{rp['id']}/versions/{rp_vid}/publish",
                headers=headers,
                json={},
            )
            scp = (
                await client.post(
                    "/api/v1/config/source-channel-profiles",
                    headers=headers,
                    json={
                        "name": "ops-scp",
                        "source_channel_id": src_id,
                        "initial_version": {
                            "analysis_pipeline_version_id": pipe_vid,
                            "routing_policy_version_id": rp_vid,
                        },
                    },
                )
            ).json()
            scp_vid = scp["versions"][0]["id"]
            await client.post(
                f"/api/v1/config/source-channel-profiles/{scp['id']}/versions/{scp_vid}/publish",
                headers=headers,
                json={},
            )
            rng = (
                await client.post(
                    "/api/v1/processing-ranges",
                    headers=headers,
                    json={
                        "source_channel_id": src_id,
                        "start_at": "2026-11-01T00:00:00Z",
                        "end_mode": "latest",
                        "steady_after_seconds": 60,
                    },
                )
            ).json()
            rng_id = UUID(rng["id"])

            # Seed canonical message, duplicate failed message, failed steps & flood_wait
            now = self.clock[0]
            msg_canon_id = uuid4()
            msg_failed_id = uuid4()
            rex_failed_id = uuid4()
            img_failed_id = uuid4()
            vid_failed_id = uuid4()
            img_cleanup_id = uuid4()
            vid_cleanup_id = uuid4()
            frame_cleanup_id = uuid4()
            img_in_use_id = uuid4()
            run_id = uuid4()
            sr_gate_id = uuid4()
            sr_failed_id = uuid4()
            manifest_id = uuid4()
            call_id = uuid4()
            assign_id = uuid4()
            eval_id = uuid4()
            pi_failed_id = uuid4()
            pi_sent_id = uuid4()
            att_failed_id = uuid4()
            att_sent_id = uuid4()
            pub_rec_id = uuid4()

            async with self.db.session() as session, session.begin():
                node_row = await session.scalar(
                    select(m.PipelineStageNodeRecord).where(
                        m.PipelineStageNodeRecord.analysis_pipeline_version_id == UUID(pipe_vid)
                    )
                )
                assert node_row is not None
                rule_row = await session.scalar(
                    select(m.RoutingRuleRecord).where(
                        m.RoutingRuleRecord.routing_policy_version_id == UUID(rp_vid)
                    )
                )
                assert rule_row is not None
                action_row = await session.scalar(
                    select(m.RoutingActionRecord).where(
                        m.RoutingActionRecord.routing_rule_id == rule_row.id
                    )
                )
                assert action_row is not None

                rng_row = await session.get(m.ProcessingRangeRecord, rng_id)
                assert rng_row is not None
                rng_row.status = "failed"
                rng_row.enabled = False
                rng_row.active_high_watermark_message_id = 200
                rng_row.processing_watermark_message_id = 100

                session.add(
                    m.RangeExecutionRecord(
                        id=rex_failed_id,
                        processing_range_id=rng_id,
                        source_profile_version_id=UUID(scp_vid),
                        from_message_id_exclusive=0,
                        to_message_id_inclusive=50,
                        watermark_message_id=10,
                        status="failed",
                        attempt_count=5,
                        max_attempts=5,
                        last_error_code="TG_READ_ERR",
                        last_error_type="RpcError",
                        last_failure_at=now + timedelta(minutes=1),
                        updated_at=now + timedelta(minutes=1),
                    )
                )
                session.add(
                    m.MessageRecord(
                        id=msg_canon_id,
                        source_channel_id=UUID(src_id),
                        telegram_message_ids=[100],
                        primary_telegram_message_id=100,
                        published_at=now - timedelta(hours=2),
                        original_text="100X literalZmatchcheck",
                        processing_status="processed",
                        terminal_reason="completed",
                        duplicate_status="unique",
                        visual_fingerprint="e" * 64,
                    )
                )
                session.add(
                    m.MessageRecord(
                        id=msg_failed_id,
                        source_channel_id=UUID(src_id),
                        telegram_message_ids=[101],
                        primary_telegram_message_id=101,
                        published_at=now - timedelta(hours=1),
                        original_text="100% literal_match\\check",
                        processing_status="failed",
                        failure_stage="media_failed",
                        last_error_code="IMG_DL_ERR",
                        canonical_message_id=msg_canon_id,
                        duplicate_status="duplicate_skipped",
                        duplicate_distance=0,
                        visual_fingerprint="e" * 64,
                    )
                )
                await session.flush()

                # Failed image & video archive steps
                session.add(
                    m.ImageAssetRecord(
                        id=img_failed_id,
                        message_id=msg_failed_id,
                        source_asset_id="tg-img-failed",
                        archive_state="failed",
                        archive_attempt_count=5,
                        archive_max_attempts=5,
                        archive_last_error_code="IMG_DL_ERR",
                        archive_last_error_type="NetworkError",
                        archive_last_failure_at=now + timedelta(minutes=2),
                        updated_at=now + timedelta(minutes=2),
                    )
                )
                session.add(
                    m.VideoAssetRecord(
                        id=vid_failed_id,
                        message_id=msg_failed_id,
                        source_asset_id="tg-vid-failed",
                        archive_state="failed",
                        archive_attempt_count=5,
                        archive_max_attempts=5,
                        archive_last_error_code="VID_FFMPEG_ERR",
                        archive_last_error_type="DecodeError",
                        archive_last_failure_at=now + timedelta(minutes=3),
                        updated_at=now + timedelta(minutes=3),
                    )
                )
                # Ready duplicate image & video+frame for cleanup replay + one in-use image
                session.add(
                    m.ImageAssetRecord(
                        id=img_cleanup_id,
                        message_id=msg_failed_id,
                        source_asset_id="tg-img-cleanup",
                        perceptual_hash="0" * 16,
                        archive_state="ready",
                        storage_backend="filesystem",
                        storage_key="images/cleanup.webp",
                        content_type="image/webp",
                        width=640,
                        height=480,
                        source_sha256="1" * 64,
                        archive_sha256="2" * 64,
                        archive_size_bytes=2048,
                        archive_ready_at=now,
                    )
                )
                session.add(
                    m.VideoAssetRecord(
                        id=vid_cleanup_id,
                        message_id=msg_failed_id,
                        source_asset_id="tg-vid-cleanup",
                        archive_state="ready",
                        source_sha256="3" * 64,
                        duration_seconds=4.5,
                        archive_ready_at=now,
                    )
                )
                session.add(
                    m.ImageAssetRecord(
                        id=img_in_use_id,
                        message_id=msg_canon_id,
                        source_asset_id="tg-img-in-use",
                        perceptual_hash="f" * 16,
                        archive_state="ready",
                        storage_backend="filesystem",
                        storage_key="images/in_use.webp",
                        content_type="image/webp",
                        width=640,
                        height=480,
                        source_sha256="4" * 64,
                        archive_sha256="5" * 64,
                        archive_size_bytes=1024,
                        archive_ready_at=now,
                    )
                )
                await session.flush()
                session.add(
                    m.VideoFrameRecord(
                        id=frame_cleanup_id,
                        video_asset_id=vid_cleanup_id,
                        frame_role="cover",
                        frame_index=0,
                        timestamp_seconds=0.0,
                        storage_backend="filesystem",
                        storage_key="frames/cleanup_cover.webp",
                        content_type="image/webp",
                        width=640,
                        height=360,
                        source_sha256="6" * 64,
                        archive_sha256="7" * 64,
                        perceptual_hash="a" * 16,
                        archive_size_bytes=1536,
                        archive_state="ready",
                    )
                )

                # Analysis run + negative-gate stage_run + failed stage_run with inference_call
                session.add(
                    m.InputManifestRecord(
                        id=manifest_id,
                        analysis_stage_template_version_id=UUID(stage_vid),
                        manifest_version=1,
                        content={"text": "100% literal_match\\check"},
                        input_manifest_hash="8" * 64,
                    )
                )
                await session.flush()
                session.add(
                    m.InferenceCallRecord(
                        id=call_id,
                        input_manifest_id=manifest_id,
                        analysis_stage_template_version_id=UUID(stage_vid),
                        provider_adapter="openai_compatible",
                        model_name="gpt-4o-mini",
                        request_summary={"model": "gpt-4o-mini"},
                        structured_output_schema={"type": "object"},
                        structured_output_schema_hash="9" * 64,
                        status="failed",
                        attempt=1,
                        raw_response={"error": "upstream gateway timeout"},
                        token_usage={
                            "prompt_tokens": 64,
                            "completion_tokens": 16,
                            "total_tokens": 80,
                        },
                        latency_ms=245.5,
                        http_status=504,
                        error_code="PROVIDER_ERROR",
                        error_type="GatewayTimeoutError",
                        retryable=True,
                        completed_at=now + timedelta(minutes=4),
                    )
                )
                session.add(
                    m.AnalysisRunRecord(
                        id=run_id,
                        message_id=msg_failed_id,
                        analysis_pipeline_version_id=UUID(pipe_vid),
                        run_mode="formal",
                        status="failed",
                        facts_snapshot={},
                        last_error_code="PROVIDER_ERROR",
                        last_error_type="GatewayTimeoutError",
                        started_at=now,
                        completed_at=now + timedelta(minutes=4),
                        created_at=now,
                    )
                )
                await session.flush()
                session.add(
                    m.StageRunRecord(
                        id=sr_gate_id,
                        analysis_run_id=run_id,
                        pipeline_stage_node_id=node_row.id,
                        analysis_stage_template_version_id=UUID(stage_vid),
                        message_id=msg_failed_id,
                        target_scope="global",
                        target_kind="message",
                        target_id=msg_failed_id,
                        status="succeeded",
                        attempt_count=1,
                        max_attempts=3,
                        input_manifest_id=manifest_id,
                        parsed_result={"scores": {"nsfw_block": 0.98}},
                        result_origin="inference",
                        started_at=now,
                        completed_at=now + timedelta(minutes=3),
                    )
                )
                session.add(
                    m.StageRunRecord(
                        id=sr_failed_id,
                        analysis_run_id=run_id,
                        pipeline_stage_node_id=None,
                        analysis_stage_template_version_id=UUID(stage_vid),
                        message_id=msg_failed_id,
                        target_scope="global",
                        target_kind="message",
                        target_id=msg_failed_id,
                        status="failed",
                        attempt_count=3,
                        max_attempts=3,
                        last_error_code="PROVIDER_ERROR",
                        last_error_type="GatewayTimeoutError",
                        retryable=True,
                        last_failure_at=now + timedelta(minutes=4),
                        inference_call_id=call_id,
                        completed_at=now + timedelta(minutes=4),
                        updated_at=now + timedelta(minutes=4),
                    )
                )
                await session.flush()
                session.add(
                    m.ModelLabelAssignmentRecord(
                        id=assign_id,
                        stage_run_id=sr_gate_id,
                        analysis_run_id=run_id,
                        message_id=msg_failed_id,
                        target_scope="global",
                        target_kind="message",
                        target_id=msg_failed_id,
                        label_definition_version_id=UUID(lbl_vid),
                        label_key="nsfw_block",
                        score=0.98,
                        activated=True,
                        negative=True,
                        evidence=[],
                    )
                )
                await session.flush()

                msg_f_row = await session.get(m.MessageRecord, msg_failed_id)
                assert msg_f_row is not None
                msg_f_row.blocked_from_analysis = True
                msg_f_row.blocked_at = now + timedelta(minutes=3)
                msg_f_row.blocked_by_stage_run_id = sr_gate_id
                msg_f_row.blocked_by_label_assignment_id = assign_id

                # Routing evaluations + failed & active publication intents
                session.add(
                    m.RoutingEvaluationRecord(
                        id=eval_id,
                        routing_request_id="req-ops-1",
                        message_id=msg_failed_id,
                        routing_policy_version_id=UUID(rp_vid),
                        facts_snapshot={},
                        facts_hash="c" * 64,
                        rule_outcomes=[],
                    )
                )
                eval_canon_id = uuid4()
                session.add(
                    m.RoutingEvaluationRecord(
                        id=eval_canon_id,
                        routing_request_id="req-ops-canon",
                        message_id=msg_canon_id,
                        routing_policy_version_id=UUID(rp_vid),
                        facts_snapshot={},
                        facts_hash="d" * 64,
                        rule_outcomes=[],
                    )
                )
                await session.flush()
                session.add(
                    m.PublicationIntentRecord(
                        id=pi_failed_id,
                        routing_evaluation_id=eval_id,
                        routing_request_id="req-ops-1",
                        message_id=msg_failed_id,
                        routing_policy_version_id=UUID(rp_vid),
                        routing_rule_id=rule_row.id,
                        routing_action_id=action_row.id,
                        destination_channel_id=UUID(dest_id),
                        publish_identity_id=UUID(bot_id),
                        publication_mode="forward_only",
                        protected_content_fallback="fail",
                        business_idempotency_key="idem-failed-1",
                        status="failed",
                        main_random_ids=[9001],
                        attempt_count=5,
                        max_attempts=5,
                        last_error_code="FLOOD_WAIT_30",
                        last_error_type="FloodWaitError",
                        last_failure_at=now + timedelta(minutes=5),
                        completed_at=now + timedelta(minutes=5),
                        updated_at=now + timedelta(minutes=5),
                    )
                )
                # Keep an active pending intent on msg_canon_id so img_in_use_id is skipped_in_use!
                session.add(
                    m.PublicationIntentRecord(
                        id=pi_sent_id,
                        routing_evaluation_id=eval_canon_id,
                        routing_request_id="req-ops-canon",
                        message_id=msg_canon_id,
                        routing_policy_version_id=UUID(rp_vid),
                        routing_rule_id=rule_row.id,
                        routing_action_id=action_row.id,
                        destination_channel_id=UUID(dest_id),
                        publish_identity_id=UUID(bot_id),
                        publication_mode="forward_only",
                        protected_content_fallback="fail",
                        business_idempotency_key="idem-canon-active",
                        status="partial",
                        main_random_ids=[9002],
                        completed_parts=["main"],
                        main_remote_message_ids=[7001],
                        attempt_count=1,
                        max_attempts=5,
                    )
                )
                await session.flush()
                session.add(
                    m.PublishAttemptRecord(
                        id=att_failed_id,
                        publication_intent_id=pi_failed_id,
                        attempt_number=1,
                        lease_token=uuid4(),
                        status="failed",
                        retryable=True,
                        error_code="FLOOD_WAIT_30",
                        error_type="FloodWaitError",
                        reconciled=False,
                        started_at=now + timedelta(minutes=5),
                        completed_at=now + timedelta(minutes=5),
                    )
                )
                session.add(
                    m.PublishAttemptRecord(
                        id=att_sent_id,
                        publication_intent_id=pi_sent_id,
                        attempt_number=1,
                        lease_token=uuid4(),
                        status="sent",
                        retryable=False,
                        reconciled=True,
                        started_at=now + timedelta(minutes=2),
                        completed_at=now + timedelta(minutes=2),
                    )
                )
                await session.flush()
                session.add(
                    m.PublishRecord(
                        id=pub_rec_id,
                        publication_intent_id=pi_sent_id,
                        publish_attempt_id=att_sent_id,
                        part="main",
                        operation="native_forward",
                        remote_message_ids=[7001],
                        random_ids=[9002],
                        reconciled=True,
                        sent_at=now + timedelta(minutes=2),
                    )
                )
                session.add(
                    m.AuditEvent(
                        actor_admin_user_id=self.admin_id,
                        event_type="message.manual_note",
                        entity_type="message",
                        entity_id=msg_failed_id,
                        payload={"note": "checked"},
                    )
                )

            # 1. Create cleanup dry-run using duplicate_skipped alias + unique -> 3 assets matched
            dry_alias = await client.post(
                "/api/v1/archive-cleanup/dry-runs",
                headers=headers,
                json={"duplicate_statuses": ["duplicate_skipped"]},
            )
            self.assertEqual(dry_alias.status_code, 201)
            self.assertEqual(dry_alias.json()["estimated_asset_count"], 2)

            dry_all = await client.post(
                "/api/v1/archive-cleanup/dry-runs",
                headers=headers,
                json={"duplicate_statuses": ["duplicate_skipped", "canonical"]},
            )
            self.assertEqual(dry_all.status_code, 201)
            self.assertEqual(dry_all.json()["estimated_asset_count"], 3)
            cj_created = await client.post(
                "/api/v1/archive-cleanup/jobs",
                headers=headers,
                json={
                    "dry_run_id": dry_all.json()["id"],
                    "expected_filter_hash": dry_all.json()["filter_hash"],
                },
            )
            self.assertEqual(cj_created.status_code, 201)
            cj_id = cj_created.json()["id"]

            # Drive cleanup job via real SqlAlchemyArchiveCleanupRepository into terminal failure
            # after skipping in-use canonical image and locking duplicate image as "deleting"!
            cleanup_repo = SqlAlchemyArchiveCleanupRepository(self.db)
            claim1 = await cleanup_repo.claim(
                job_id=cj_id,
                now=now + timedelta(minutes=6),
                lease_duration=timedelta(minutes=5),
            )
            assert claim1 is not None
            first_item = await cleanup_repo.claim_next_item(
                claim=claim1,
                now=now + timedelta(minutes=6),
            )
            assert first_item is not None
            await cleanup_repo.retry_or_fail(
                claim=claim1,
                item=first_item,
                error_code="DISK_IO",
                error_type="OSError",
                retryable=False,
                retry_at=now + timedelta(minutes=7),
                now=now + timedelta(minutes=6),
            )

            # 2. Escaped ILIKE wildcard search + blocked_by_label_key + message detail DTOs
            msgs = (
                await client.get(
                    "/api/v1/messages",
                    params={"query": "100% literal_match\\check"},
                )
            ).json()["items"]
            self.assertEqual([item["id"] for item in msgs], [str(msg_failed_id)])
            self.assertEqual(msgs[0]["blocked_by_label_key"], "nsfw_block")

            detail = (await client.get(f"/api/v1/messages/{msg_failed_id}")).json()
            self.assertEqual(detail["message"]["blocked_by_label_key"], "nsfw_block")
            self.assertEqual(len(detail["audit_events"]), 1)
            self.assertEqual(detail["audit_events"][0]["entity_type"], "message")
            arun = detail["analysis_runs"][0]
            self.assertIsNotNone(arun["created_at"])
            by_id = {sr["id"]: sr for sr in arun["stage_runs"]}
            self.assertEqual(by_id[str(sr_gate_id)]["node_key"], "root_gate")
            icall = by_id[str(sr_failed_id)]["inference_call"]
            self.assertEqual(icall["http_status"], 504)
            self.assertEqual(icall["error_type"], "GatewayTimeoutError")
            self.assertEqual(icall["raw_response"], {"error": "upstream gateway timeout"})
            pintent = detail["publication_intents"][0]
            self.assertIn("next_retry_at", pintent)
            self.assertEqual(pintent["attempts"][0]["error_type"], "FloodWaitError")

            # 404 on nonexistent message; 409 on skipping already-processed canonical message
            missing_id = str(uuid4())
            self.assertEqual(
                (
                    await client.post(
                        f"/api/v1/messages/{missing_id}/review-status",
                        headers=headers,
                        json={"new_status": "reviewed"},
                    )
                ).status_code,
                404,
            )
            self.assertEqual(
                (
                    await client.post(
                        f"/api/v1/messages/{missing_id}/labels",
                        headers=headers,
                        json={
                            "operation": "set",
                            "label_definition_version_id": lbl_vid,
                            "label_key": "nsfw_block",
                        },
                    )
                ).status_code,
                404,
            )
            self.assertEqual(
                (
                    await client.post(
                        f"/api/v1/messages/{msg_canon_id}/skip",
                        headers=headers,
                        json={"reason": "already processed"},
                    )
                ).status_code,
                409,
            )

            # 3. Operations: 6 step_kinds newest-first + fallback before service_heartbeats exists
            failed_items = (await client.get("/api/v1/operations/failed-items")).json()["items"]
            self.assertEqual(
                [it["step_kind"] for it in failed_items],
                [
                    "cleanup_job",
                    "publication_intent",
                    "stage_run",
                    "video_archive",
                    "image_archive",
                    "range_execution",
                ],
            )
            ops_before_hb = (await client.get("/api/v1/operations/status")).json()
            self.assertEqual(ops_before_hb["failed_count"], 6)
            for q_name in (
                "range_execution",
                "image_archive",
                "video_archive",
                "analysis",
                "publication",
                "maintenance",
            ):
                self.assertIn(q_name, ops_before_hb["queues"])

            # Range execution DTO includes history_ingested
            execs = (await client.get(f"/api/v1/processing-ranges/{rng_id}/executions")).json()[
                "items"
            ]
            self.assertEqual(execs[0]["history_ingested"], False)

            # Retry range_execution restores parent ProcessingRange to active; 2nd retry -> 409
            r_rex = await client.post(
                "/api/v1/operations/retry",
                headers=headers,
                json={"step_kind": "range_execution", "target_id": str(rex_failed_id)},
            )
            self.assertEqual(r_rex.status_code, 200)
            self.assertEqual(
                (await client.get(f"/api/v1/processing-ranges/{rng_id}")).json()["status"],
                "active",
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/operations/retry",
                        headers=headers,
                        json={"step_kind": "range_execution", "target_id": str(rex_failed_id)},
                    )
                ).status_code,
                409,
            )

            # Retry image_archive -> restores parent MessageRecord from failed to processing
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/operations/retry",
                        headers=headers,
                        json={"step_kind": "image_archive", "target_id": str(img_failed_id)},
                    )
                ).status_code,
                200,
            )
            self.assertEqual(
                (await client.get(f"/api/v1/messages/{msg_failed_id}")).json()["message"][
                    "processing_status"
                ],
                "processing",
            )

            # 4. Retry cleanup_job restores deleting asset & failed item so repo replays cleanly
            r_cj = await client.post(
                "/api/v1/operations/retry",
                headers=headers,
                json={"step_kind": "cleanup_job", "target_id": cj_id},
            )
            self.assertEqual(r_cj.status_code, 200)

            replay_now = now + timedelta(minutes=8)
            claim2 = await cleanup_repo.claim(
                job_id=cj_id,
                now=replay_now,
                lease_duration=timedelta(minutes=5),
            )
            assert claim2 is not None
            replayed_assets: list[str] = []
            while True:
                item_claim = await cleanup_repo.claim_next_item(claim=claim2, now=replay_now)
                if item_claim is None:
                    break
                replayed_assets.append(item_claim.asset.asset_id)
                self.assertTrue(
                    await cleanup_repo.finalize_item_deleted(
                        claim=claim2,
                        item=item_claim,
                        now=replay_now,
                    )
                )
            self.assertCountEqual(replayed_assets, [str(img_cleanup_id), str(vid_cleanup_id)])
            self.assertTrue(await cleanup_repo.complete(claim=claim2, now=replay_now))

            cj_final = (await client.get(f"/api/v1/archive-cleanup/jobs/{cj_id}")).json()
            self.assertEqual(cj_final["status"], "completed")
            self.assertEqual(cj_final["deleted_count"], 2)
            self.assertEqual(cj_final["skipped_active_count"], 1)

            # 5. Cross-process service_heartbeats + SQL publication/flood_wait counters + /metrics
            async with self.db.session() as session, session.begin():
                await session.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS service_heartbeats (
                            service_name VARCHAR(64) PRIMARY KEY,
                            database_ready BOOLEAN NOT NULL,
                            broker_ready BOOLEAN NOT NULL,
                            last_heartbeat_at TIMESTAMPTZ NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                        """
                    )
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO service_heartbeats
                            (service_name, database_ready, broker_ready, last_heartbeat_at)
                        VALUES
                            ('scheduler', TRUE, TRUE, :fresh_ts),
                            ('worker-analysis', TRUE, TRUE, :fresh_ts)
                        """
                    ),
                    {
                        "fresh_ts": self.clock[0] - timedelta(seconds=30),
                    },
                )

            ops_after_hb = (await client.get("/api/v1/operations/status")).json()["operations"]
            self.assertTrue(ops_after_hb["scheduler"]["ready"])
            self.assertTrue(ops_after_hb["worker"]["ready"])
            self.assertGreaterEqual(ops_after_hb["flood_wait"]["events"], 1)
            self.assertGreaterEqual(ops_after_hb["publication"]["reconciled_parts"], 1)

            metrics_resp = await client.get("/metrics")
            self.assertEqual(metrics_resp.status_code, 200)
            metrics_body = metrics_resp.text
            for metric_name in (
                "tgcurator_messages_total",
                "tgcurator_messages_blocked_total",
                "tgcurator_processing_ranges_total",
                "tgcurator_processing_range_lag_messages",
                "tgcurator_media_assets_total",
                "tgcurator_archive_bytes_total",
                "tgcurator_analysis_stage_runs_total",
                "tgcurator_analysis_cache_hits_total",
                "tgcurator_inference_calls_total",
                "tgcurator_inference_latency_ms_sum",
                "tgcurator_inference_latency_ms_count",
                "tgcurator_inference_tokens_total",
                "tgcurator_routing_evaluations_total",
                "tgcurator_routing_matched_actions_total",
                "tgcurator_publication_events_total",
                "tgcurator_publication_intents_total",
                "tgcurator_telegram_flood_wait_events_total",
                "tgcurator_worker_backlog_items",
                "tgcurator_service_ready",
            ):
                self.assertIn(metric_name, metrics_body)
            self.assertIn('tgcurator_inference_tokens_total{kind="prompt_tokens"} 64', metrics_body)
            self.assertIn('tgcurator_service_ready{service="scheduler"} 1', metrics_body)
            self.assertIn('tgcurator_service_ready{service="worker"} 1', metrics_body)


if __name__ == "__main__":
    unittest.main()
