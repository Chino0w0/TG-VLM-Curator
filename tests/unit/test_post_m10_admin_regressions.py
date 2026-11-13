from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from apps.api import create_app
from apps.api.dependencies import _VaultInferenceSecretResolver, build_default_admin_service
from tests.unit.test_api_health import SyncAsgiClient
from tgcurator.application import Settings
from tgcurator.application.admin import (
    SESSION_COOKIE_NAME,
    AdminApplicationService,
    AdminSessionService,
    InMemoryAdminBackend,
    InMemorySecretVault,
    PromptTestBenchService,
)
from tgcurator.application.admin.config_validation import (
    build_inference_profile_domain,
    build_label_definition_domain,
    build_label_set_domain,
    build_prompt_template_domain,
    build_stage_template_domain,
    validate_inference_base_url,
)
from tgcurator.application.admin.errors import AdminValidationError
from tgcurator.application.admin.test_bench import sanitize_test_bench_json
from tgcurator.application.analysis import AnalysisTargetInput
from tgcurator.application.ports.admin_api import (
    AdminAccountRecord,
    AdminSessionState,
    AuthenticatedAdmin,
)
from tgcurator.application.ports.analysis import (
    InferenceProviderFailure,
    InferenceRequest,
    InferenceResponse,
)
from tgcurator.domain.analysis import AnalysisErrorCode
from tgcurator.domain.review import LabelValue
from tgcurator.infrastructure.security import Argon2idPasswordHasher


class _CountingHasher:
    def __init__(self) -> None:
        self._inner = Argon2idPasswordHasher()
        self.verify_calls: list[tuple[str, str]] = []

    def hash(self, password: str) -> str:
        return self._inner.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        self.verify_calls.append((password_hash, password))
        return self._inner.verify(password_hash, password)


class _ConfigurableBenchProvider:
    def __init__(
        self,
        *,
        response: InferenceResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class _FakeDbProbe:
    async def ping(self) -> bool:
        return True

    async def dispose(self) -> None:
        return None


def _build_hardened_stack(
    *,
    allow_local_inference: bool = False,
    provider: Any = None,
    clock_box: list[datetime] | None = None,
) -> tuple[SyncAsgiClient, InMemoryAdminBackend, InMemorySecretVault, _CountingHasher]:
    now_box = clock_box or [datetime(2026, 11, 12, 12, 0, tzinfo=UTC)]
    hasher = _CountingHasher()
    backend = InMemoryAdminBackend(
        clock=lambda: now_box[0],
        allow_local_inference=allow_local_inference,
    )
    backend.seed_admin(
        username="curator-admin",
        password_hash=hasher.hash("correct-horse-battery-staple"),
        user_id=str(uuid4()),
    )
    vault = InMemorySecretVault()
    sessions = AdminSessionService(
        repository=backend,
        password_hasher=hasher,
        csrf_secret=b"post-m10-csrf-secret-32-bytes!!!",
        clock=lambda: now_box[0],
    )
    service = AdminApplicationService(
        sessions=sessions,
        secrets=vault,
        backend=backend,
        test_bench=PromptTestBenchService(provider=provider),
        clock=lambda: now_box[0],
        allow_local_inference=allow_local_inference,
    )
    key_b64 = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    settings = Settings(
        environment="test",
        app_name="tgcurator-post-m10-test",
        app_master_key=key_b64,  # type: ignore[arg-type]
    )
    app = create_app(settings, _FakeDbProbe(), admin_service=service)
    return SyncAsgiClient(app), backend, vault, hasher


class PostM10AdminSecurityAndParityTests(unittest.TestCase):
    def test_main_module_is_lazy_on_import_and_resolves_fastapi_app_on_access(self) -> None:
        probe = (
            "import sys; "
            "import apps.api.main as m; "
            "assert m._app is None, 'app created eagerly at import time'; "
            "assert 'app' not in m.__dict__, 'app bound in module dict before access'; "
            "from fastapi import FastAPI; "
            "resolved = m.app; "
            "assert isinstance(resolved, FastAPI); "
            "assert m.get_app() is resolved"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_validation_422_redacts_passwords_and_secret_plaintexts(self) -> None:
        client, _backend, _vault, _hasher = _build_hardened_stack()
        leaked_pw = "ultra-sensitive-login-passphrase-9911"
        leaked_sec = "sk-live-never-echo-in-422-body-8822"
        leaked_rot = "sk-rotated-never-echo-in-422-body-7733"

        with client:
            # 1. Login validation error (extra field + missing required type / forbidden extra)
            r_login = client.post(
                "/api/v1/auth/login",
                json={"password": leaked_pw, "unexpected_field": "boom"},
            )
            self.assertEqual(r_login.status_code, 422)
            self.assertNotIn(leaked_pw, r_login.text)
            body = r_login.json()
            self.assertEqual(body["error"]["code"], "validation_error")
            for item in body["detail"]:
                self.assertEqual(set(item.keys()), {"type", "loc", "msg"})

            # Authenticate to test /secrets and /secrets/{id} 422 redaction
            ok_login = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "curator-admin",
                    "password": "correct-horse-battery-staple",
                },
            )
            self.assertEqual(ok_login.status_code, 200)
            headers = {"X-CSRF-Token": ok_login.json()["csrf_token"]}

            r_create = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"plaintext": leaked_sec},
            )
            self.assertEqual(r_create.status_code, 422)
            self.assertNotIn(leaked_sec, r_create.text)
            for item in r_create.json()["detail"]:
                self.assertNotIn("input", item)
                self.assertNotIn("ctx", item)

            sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "api_key", "plaintext": "sk-valid-seed"},
            ).json()
            r_rotate = client.put(
                f"/api/v1/secrets/{sec['secret_id']}",
                headers=headers,
                json={"plaintext": leaked_rot, "extra_forbidden": True},
            )
            self.assertEqual(r_rotate.status_code, 422)
            self.assertNotIn(leaked_rot, r_rotate.text)

    def test_build_default_admin_service_fails_closed_without_master_key_when_db_present(
        self,
    ) -> None:
        settings = Settings(environment="test", app_name="fail-closed-check")
        with self.assertRaisesRegex(RuntimeError, "APP_MASTER_KEY"):
            build_default_admin_service(settings=settings, database=object())

        # In-memory / database=None fallback remains allowed for isolated non-DB unit usage
        svc = build_default_admin_service(settings=settings, database=None)
        self.assertIsInstance(svc, AdminApplicationService)

    def test_secret_type_isolation_across_identities_inference_profiles_and_resolver(
        self,
    ) -> None:
        client, _backend, vault, _hasher = _build_hardened_stack(allow_local_inference=False)
        with client:
            login = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "curator-admin",
                    "password": "correct-horse-battery-staple",
                },
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}

            bot_sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "bot_token", "plaintext": "123456:ABCDEF-bot-token"},
            ).json()["secret_id"]
            tg_sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "telegram_session", "plaintext": "1BVtsOHwBu-session"},
            ).json()["secret_id"]
            api_sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "api_key", "plaintext": "sk-openai-compatible-key"},
            ).json()["secret_id"]

            # mtproto_user cannot bind bot_token or api_key
            self.assertEqual(
                client.post(
                    "/api/v1/telegram-identities",
                    headers=headers,
                    json={
                        "name": "bad-mtproto",
                        "identity_type": "mtproto_user",
                        "secret_id": bot_sec,
                    },
                ).status_code,
                422,
            )
            self.assertEqual(
                client.post(
                    "/api/v1/telegram-identities",
                    headers=headers,
                    json={
                        "name": "bad-mtproto-2",
                        "identity_type": "mtproto_user",
                        "secret_id": api_sec,
                    },
                ).status_code,
                422,
            )

            # bot cannot bind telegram_session or api_key
            self.assertEqual(
                client.post(
                    "/api/v1/telegram-identities",
                    headers=headers,
                    json={"name": "bad-bot", "identity_type": "bot", "secret_id": tg_sec},
                ).status_code,
                422,
            )

            # Valid mtproto_user cannot be updated to reference bot_token
            ok_mtproto = client.post(
                "/api/v1/telegram-identities",
                headers=headers,
                json={"name": "good-mtproto", "identity_type": "mtproto_user", "secret_id": tg_sec},
            )
            self.assertEqual(ok_mtproto.status_code, 201)
            mtproto_id = ok_mtproto.json()["id"]
            self.assertEqual(
                client.patch(
                    f"/api/v1/telegram-identities/{mtproto_id}",
                    headers=headers,
                    json={"secret_id": bot_sec},
                ).status_code,
                422,
            )

            # InferenceProfile cannot reference bot_token or telegram_session
            ip_ent = client.post(
                "/api/v1/config/inference-profiles",
                headers=headers,
                json={"name": "strict-ip"},
            ).json()
            self.assertEqual(
                client.post(
                    f"/api/v1/config/inference-profiles/{ip_ent['id']}/versions",
                    headers=headers,
                    json={
                        "base_url": "https://api.openai.com/v1",
                        "model_name": "gpt-4o-mini",
                        "api_secret_reference": bot_sec,
                    },
                ).status_code,
                422,
            )
            self.assertEqual(
                client.post(
                    f"/api/v1/config/inference-profiles/{ip_ent['id']}/versions",
                    headers=headers,
                    json={
                        "base_url": "https://api.openai.com/v1",
                        "model_name": "gpt-4o-mini",
                        "api_secret_reference": tg_sec,
                    },
                ).status_code,
                422,
            )
            ok_ip_ver = client.post(
                f"/api/v1/config/inference-profiles/{ip_ent['id']}/versions",
                headers=headers,
                json={
                    "base_url": "https://api.openai.com/v1",
                    "model_name": "gpt-4o-mini",
                    "api_secret_reference": api_sec,
                },
            )
            self.assertEqual(ok_ip_ver.status_code, 201)

            # _VaultInferenceSecretResolver enforces UUID + inference secret_type at resolution time
            resolver = _VaultInferenceSecretResolver(vault)
            self.assertEqual(
                asyncio.run(resolver.resolve(secret_reference=api_sec)),
                "sk-openai-compatible-key",
            )
            with self.assertRaises(ValueError):
                asyncio.run(resolver.resolve(secret_reference=bot_sec))
            with self.assertRaises(ValueError):
                asyncio.run(resolver.resolve(secret_reference=tg_sec))
            with self.assertRaises(ValueError):
                asyncio.run(resolver.resolve(secret_reference="not-a-uuid"))

    def test_ssrf_base_url_validation_blocks_metadata_link_local_and_unapproved_local_targets(
        self,
    ) -> None:
        always_blocked = [
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.170.2/v2/credentials",
            "https://metadata.google.internal/computeMetadata/v1/",
            "http://sub.metadata.google.internal/v1",
            "http://[fd00:ec2::254]/latest/api/token",
            "http://[::ffff:169.254.169.254]/latest/meta-data/",
            "http://100.100.100.200/latest/meta-data/",
            "http://0.0.0.0:8080/v1",
            "http://[::]:8080/v1",
            "ftp://api.openai.com/v1",
            "https://user:secret@api.openai.com/v1",
            "https://api.openai.com/v1?api_key=sk-leak",
        ]
        for bad_url in always_blocked:
            with self.subTest(url=bad_url):
                with self.assertRaises(AdminValidationError):
                    validate_inference_base_url(bad_url, allow_local=True)
                with self.assertRaises(AdminValidationError):
                    validate_inference_base_url(bad_url, allow_local=False)

        local_urls = [
            "http://127.0.0.1:11434/v1",
            "http://localhost:8000/v1",
            "http://vllm.localhost:8000/v1",
            "http://10.10.0.5:8000/v1",
            "http://192.168.1.20:8000/v1",
        ]
        for local_url in local_urls:
            with self.subTest(local_url=local_url):
                with self.assertRaises(AdminValidationError):
                    validate_inference_base_url(local_url, allow_local=False)
                self.assertTrue(validate_inference_base_url(local_url, allow_local=True))

        self.assertEqual(
            validate_inference_base_url("https://api.openai.com/v1/", allow_local=False),
            "https://api.openai.com/v1",
        )

    def test_auth_cookie_max_age_dummy_verify_failed_login_audit_touch_throttle_and_repr(
        self,
    ) -> None:
        clock = [datetime(2026, 11, 12, 12, 0, tzinfo=UTC)]
        client, backend, _vault, hasher = _build_hardened_stack(clock_box=clock)

        # Sensitive dataclass fields have repr=False
        acc = AdminAccountRecord(
            user_id="u1",
            username="admin",
            password_hash="$argon2id$secret-hash",
            is_active=True,
        )
        sess = AdminSessionState(
            session_id="s1",
            admin_user_id="u1",
            username="admin",
            session_token_hash="tok-hash-secret",
            csrf_token_hash="csrf-hash-secret",
            expires_at=clock[0],
            created_at=clock[0],
            last_seen_at=clock[0],
            revoked_at=None,
        )
        auth_admin = AuthenticatedAdmin(
            session_id="s1",
            user_id="u1",
            username="admin",
            csrf_token="raw-csrf-secret",
            csrf_token_hash="csrf-hash-secret",
            expires_at=clock[0],
        )
        self.assertNotIn("$argon2id$secret-hash", repr(acc))
        self.assertNotIn("tok-hash-secret", repr(sess))
        self.assertNotIn("csrf-hash-secret", repr(sess))
        self.assertNotIn("raw-csrf-secret", repr(auth_admin))
        self.assertNotIn("csrf-hash-secret", repr(auth_admin))

        with client:
            hasher.verify_calls.clear()
            bad_user_pw = "attempted-password-unknown-user"
            r_unknown = client.post(
                "/api/v1/auth/login",
                json={"username": "nonexistent-user", "password": bad_user_pw},
            )
            self.assertEqual(r_unknown.status_code, 401)
            self.assertEqual(len(hasher.verify_calls), 1)
            self.assertEqual(hasher.verify_calls[0][1], bad_user_pw)

            bad_pw = "wrong-passphrase-for-known-admin"
            r_bad_pw = client.post(
                "/api/v1/auth/login",
                json={"username": "curator-admin", "password": bad_pw},
            )
            self.assertEqual(r_bad_pw.status_code, 401)

            ok = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "curator-admin",
                    "password": "correct-horse-battery-staple",
                },
            )
            self.assertEqual(ok.status_code, 200)
            set_cookie = ok.headers.get("set-cookie", "").lower()
            self.assertIn(SESSION_COOKIE_NAME, set_cookie)
            self.assertIn("max-age=43200", set_cookie)

            # Inspect failed-login audit events and verify passwords never leaked
            audits = client.get(
                "/api/v1/audit-events",
                params={"event_type": "admin.login_failed"},
            ).json()["items"]
            self.assertEqual(len(audits), 2)
            reasons = {a["payload"]["reason"] for a in audits}
            self.assertEqual(reasons, {"unknown_or_inactive_user", "invalid_password"})
            serialized_audits = json.dumps(audits)
            self.assertNotIn(bad_user_pw, serialized_audits)
            self.assertNotIn(bad_pw, serialized_audits)

            # Verify 60s touch_session write throttling
            touch_calls: list[datetime] = []
            orig_touch = backend.touch_session

            async def _spy_touch(*, session_id: str, now: datetime) -> None:
                touch_calls.append(now)
                await orig_touch(session_id=session_id, now=now)

            backend.touch_session = _spy_touch  # type: ignore[method-assign]

            clock[0] = clock[0] + timedelta(seconds=30)
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
            self.assertEqual(len(touch_calls), 0)

            clock[0] = clock[0] + timedelta(seconds=29)
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
            self.assertEqual(len(touch_calls), 0)

            clock[0] = clock[0] + timedelta(seconds=1)
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
            self.assertEqual(len(touch_calls), 1)

            clock[0] = clock[0] + timedelta(seconds=15)
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
            self.assertEqual(len(touch_calls), 1)

    def test_prompt_test_bench_handles_none_token_usage_and_sanitizes_credentials(self) -> None:
        lbl_vid = str(uuid4())
        lbl = build_label_definition_domain(
            version_id=lbl_vid,
            entity_id=str(uuid4()),
            version_number=1,
            state="published",
            content={
                "key": "quality",
                "display_name": "Quality",
                "description": "High quality",
                "scope": "global",
                "negative": False,
            },
        )
        lset = build_label_set_domain(
            version_id=str(uuid4()),
            entity_id=str(uuid4()),
            version_number=1,
            state="published",
            content={
                "bindings": [
                    {
                        "label_definition_version_id": lbl_vid,
                        "activation_threshold": 0.5,
                        "output_order": 0,
                    }
                ]
            },
            resolve_label_def=lambda _: lbl,
        )
        prompt = build_prompt_template_domain(
            version_id=str(uuid4()),
            entity_id=str(uuid4()),
            version_number=1,
            state="published",
            content={
                "system_prompt": "Classify {{ label_definitions }}",
                "user_prompt_template": "Text: {{ message_text }}",
                "declared_variables": ["label_definitions", "message_text"],
            },
        )
        ip = build_inference_profile_domain(
            version_id=str(uuid4()),
            entity_id=str(uuid4()),
            version_number=1,
            state="published",
            content={
                "base_url": "https://api.openai.com/v1",
                "model_name": "gpt-4o-mini",
                "api_secret_reference": str(uuid4()),
            },
        )
        stage = build_stage_template_domain(
            version_id=str(uuid4()),
            entity_id=str(uuid4()),
            version_number=1,
            state="published",
            content={
                "name": "bench-stage",
                "target_scope": "global",
                "execution_mode": "single_message",
                "prompt_template_version_id": prompt.version_id,
                "label_set_version_id": lset.version_id,
                "inference_profile_version_id": ip.version_id,
                "input_policy": {"text": True},
            },
            resolve_prompt=lambda _: prompt,
            resolve_label_set=lambda _: lset,
            resolve_inference_profile=lambda _: ip,
        )
        target_id = str(uuid4())
        targets = (
            AnalysisTargetInput(
                target_id=target_id,
                message_id=target_id,
                text="Sample bench text",
                telegram_metadata={},
                media=(),
            ),
        )

        # 1. Completed with token_usage=None and sensitive raw_response
        ok_provider = _ConfigurableBenchProvider(
            response=InferenceResponse(
                payload={"result": {"scores": {"quality": 0.91}}},
                provider_adapter="openai_compatible",
                model_name="gpt-4o-mini",
                raw_response={
                    "api_key": "sk-raw-leak-001",
                    "auth_header": "Bearer sk-bearer-leak-002",
                    "bot_ref": "1234567:ABCDEFGHIJKLMNOPQRSTUV",
                    "endpoint": "https://admin:pw123@example.com/v1?token=abc",
                    "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
                },
                latency_ms=19.0,
                http_status=200,
                token_usage=None,
            )
        )
        res_ok = asyncio.run(
            PromptTestBenchService(provider=ok_provider).run(stage=stage, targets=targets)
        )
        self.assertEqual(res_ok.status, "completed")
        self.assertIsNone(res_ok.token_usage)
        assert res_ok.raw_response is not None
        raw_dump = json.dumps(dict(res_ok.raw_response))
        self.assertNotIn("sk-raw-leak-001", raw_dump)
        self.assertNotIn("sk-bearer-leak-002", raw_dump)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUV", raw_dump)
        self.assertNotIn("pw123", raw_dump)
        self.assertEqual(
            res_ok.raw_response["usage"],
            {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
        )

        # 2. Validation failed with token_usage=None
        bad_schema_provider = _ConfigurableBenchProvider(
            response=InferenceResponse(
                payload={"unexpected": "shape"},
                provider_adapter="openai_compatible",
                model_name="gpt-4o-mini",
                raw_response={"secret_token": "leak-val"},
                latency_ms=8.0,
                http_status=200,
                token_usage=None,
            )
        )
        res_invalid = asyncio.run(
            PromptTestBenchService(provider=bad_schema_provider).run(stage=stage, targets=targets)
        )
        self.assertEqual(res_invalid.status, "validation_failed")
        self.assertIsNone(res_invalid.token_usage)
        assert res_invalid.raw_response is not None
        self.assertEqual(res_invalid.raw_response["secret_token"], "[REDACTED]")

        # 3. Provider failed with token_usage=None vs token_usage dict
        err_provider = _ConfigurableBenchProvider(
            error=InferenceProviderFailure(
                code=AnalysisErrorCode.PROVIDER_ERROR,
                error_type="HttpStatusError",
                retryable=True,
                http_status=503,
                raw_response={"authorization": "Bearer top-secret"},
                token_usage=None,
            )
        )
        res_err = asyncio.run(
            PromptTestBenchService(provider=err_provider).run(stage=stage, targets=targets)
        )
        self.assertEqual(res_err.status, "provider_failed")
        self.assertIsNone(res_err.token_usage)
        assert res_err.raw_response is not None
        self.assertEqual(res_err.raw_response["authorization"], "[REDACTED]")

        sanitized = sanitize_test_bench_json(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "access_token": "secret",
            }
        )
        self.assertEqual(
            sanitized,
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "access_token": "[REDACTED]",
            },
        )

    def test_in_memory_operations_dto_contracts_cleanup_aliases_and_guards(self) -> None:
        clock = [datetime(2026, 11, 12, 14, 0, tzinfo=UTC)]
        client, backend, _vault, _hasher = _build_hardened_stack(clock_box=clock)
        with client:
            login = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "curator-admin",
                    "password": "correct-horse-battery-staple",
                },
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}

            tg_sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "telegram_session", "plaintext": "1BVtsOHwBu-session"},
            ).json()["secret_id"]
            bot_sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "bot_token", "plaintext": "999999:ABCDEFGHIJKLMNOPQRSTUV"},
            ).json()["secret_id"]

            mt_ident = client.post(
                "/api/v1/telegram-identities",
                headers=headers,
                json={"name": "reader-mt", "identity_type": "mtproto_user", "secret_id": tg_sec},
            ).json()["id"]
            bot_ident = client.post(
                "/api/v1/telegram-identities",
                headers=headers,
                json={"name": "pub-bot", "identity_type": "bot", "secret_id": bot_sec},
            ).json()["id"]

            # Source channel rejects bot reader identity
            self.assertEqual(
                client.post(
                    "/api/v1/source-channels",
                    headers=headers,
                    json={
                        "telegram_peer_id": -1009001,
                        "display_name": "Bad Reader Channel",
                        "default_read_identity_id": bot_ident,
                    },
                ).status_code,
                422,
            )

            src_a = client.post(
                "/api/v1/source-channels",
                headers=headers,
                json={
                    "telegram_peer_id": -1009002,
                    "display_name": "Channel A",
                    "default_read_identity_id": mt_ident,
                },
            ).json()["id"]
            src_b = client.post(
                "/api/v1/source-channels",
                headers=headers,
                json={
                    "telegram_peer_id": -1009003,
                    "display_name": "Channel B",
                    "default_read_identity_id": mt_ident,
                },
            ).json()["id"]

            # Source profile ownership & published guard
            prof_b = client.post(
                "/api/v1/config/source-channel-profiles",
                headers=headers,
                json={
                    "name": "prof-b",
                    "source_channel_id": src_b,
                    "initial_version": {},
                },
            ).json()
            prof_b_vid = prof_b["versions"][0]["id"]
            # Draft profile rejected
            self.assertEqual(
                client.patch(
                    f"/api/v1/source-channels/{src_b}",
                    headers=headers,
                    json={"active_profile_version_id": prof_b_vid},
                ).status_code,
                422,
            )
            client.post(
                f"/api/v1/config/source-channel-profiles/{prof_b['id']}/versions/{prof_b_vid}/publish",
                headers=headers,
            )
            # Published profile owned by src_b rejected on src_a
            self.assertEqual(
                client.patch(
                    f"/api/v1/source-channels/{src_a}",
                    headers=headers,
                    json={"active_profile_version_id": prof_b_vid},
                ).status_code,
                422,
            )

            # ProcessingRange guards + execution history_ingested DTO
            rng = client.post(
                "/api/v1/processing-ranges",
                headers=headers,
                json={
                    "source_channel_id": src_a,
                    "start_at": "2026-11-01T00:00:00Z",
                    "end_mode": "latest",
                    "steady_after_seconds": 60,
                },
            ).json()
            rng_id = rng["id"]
            # Cannot resume non-paused range
            self.assertEqual(
                client.patch(
                    f"/api/v1/processing-ranges/{rng_id}",
                    headers=headers,
                    json={"status": "active"},
                ).status_code,
                409,
            )

            rex_failed_id = str(uuid4())
            backend.range_executions[rex_failed_id] = {
                "id": rex_failed_id,
                "processing_range_id": rng_id,
                "source_profile_version_id": prof_b_vid,
                "from_message_id_exclusive": 0,
                "to_message_id_inclusive": 50,
                "watermark_message_id": 10,
                "status": "failed",
                "attempt_count": 3,
                "max_attempts": 5,
                "last_error_code": "TG_TIMEOUT",
                "last_error_type": "TimeoutError",
                "updated_at": "2026-11-12T14:01:00+00:00",
                "created_at": "2026-11-12T14:00:00+00:00",
            }
            backend.processing_ranges[rng_id]["status"] = "failed"
            backend.processing_ranges[rng_id]["enabled"] = False
            # Terminal range rejects pause/resume via PATCH
            self.assertEqual(
                client.patch(
                    f"/api/v1/processing-ranges/{rng_id}",
                    headers=headers,
                    json={"enabled": True},
                ).status_code,
                409,
            )

            execs = client.get(f"/api/v1/processing-ranges/{rng_id}/executions").json()["items"]
            self.assertEqual(execs[0]["history_ingested"], False)

            # Seed messages, media, stage_run, publication_intent & cleanup_job across 6 step_kinds
            msg_failed_id = str(uuid4())
            msg_done_id = str(uuid4())
            blocked_assign_id = str(uuid4())
            backend.messages[msg_failed_id] = {
                "id": msg_failed_id,
                "source_channel_id": src_a,
                "telegram_message_ids": [101],
                "primary_telegram_message_id": 101,
                "published_at": "2026-11-12T10:00:00+00:00",
                "original_text": "100% literal_match\\check",
                "processing_status": "failed",
                "failure_stage": "media_failed",
                "last_error_code": "DL_FAIL",
                "review_status": "unreviewed",
                "duplicate_status": "duplicate_skipped",
                "blocked_from_analysis": True,
                "blocked_by_label_assignment_id": blocked_assign_id,
            }
            backend.model_labels[msg_failed_id] = [
                LabelValue(
                    message_id=msg_failed_id,
                    target_scope="global",
                    target_kind="message",
                    target_id=msg_failed_id,
                    label_definition_version_id=str(uuid4()),
                    label_key="nsfw_block",
                    score=0.99,
                    activated=True,
                )
            ]
            backend.messages[msg_done_id] = {
                "id": msg_done_id,
                "source_channel_id": src_a,
                "telegram_message_ids": [102],
                "primary_telegram_message_id": 102,
                "published_at": "2026-11-12T10:05:00+00:00",
                "original_text": "100X literalZmatchcheck",
                "processing_status": "processed",
                "terminal_reason": "completed",
                "review_status": "unreviewed",
                "duplicate_status": "unique",
                "blocked_from_analysis": False,
            }

            # Escaped wildcard query + blocked_by_label_key
            msg_list = client.get(
                "/api/v1/messages",
                params={"query": "100% literal_match\\check"},
            ).json()["items"]
            self.assertEqual([m["id"] for m in msg_list], [msg_failed_id])
            self.assertEqual(msg_list[0]["blocked_by_label_key"], "nsfw_block")

            # 404 on nonexistent message review/labels; 409 on skipping processed message
            missing_mid = str(uuid4())
            self.assertEqual(
                client.post(
                    f"/api/v1/messages/{missing_mid}/review-status",
                    headers=headers,
                    json={"new_status": "reviewed"},
                ).status_code,
                404,
            )
            self.assertEqual(
                client.post(
                    f"/api/v1/messages/{missing_mid}/labels",
                    headers=headers,
                    json={
                        "operation": "set",
                        "label_definition_version_id": str(uuid4()),
                        "label_key": "k",
                    },
                ).status_code,
                404,
            )
            self.assertEqual(
                client.post(
                    f"/api/v1/messages/{msg_done_id}/skip",
                    headers=headers,
                    json={"reason": "already settled"},
                ).status_code,
                409,
            )

            img_failed_id = str(uuid4())
            img_dup_ready_id = str(uuid4())
            vid_failed_id = str(uuid4())
            sr_failed_id = str(uuid4())
            pi_failed_id = str(uuid4())
            cj_failed_id = str(uuid4())

            backend.image_assets[img_failed_id] = {
                "id": img_failed_id,
                "message_id": msg_failed_id,
                "archive_state": "failed",
                "archive_last_error_code": "IMG_DL",
                "archive_last_error_type": "NetworkError",
                "archive_attempt_count": 5,
                "updated_at": "2026-11-12T14:02:00+00:00",
            }
            backend.image_assets[img_dup_ready_id] = {
                "id": img_dup_ready_id,
                "message_id": msg_failed_id,
                "archive_state": "deleting",
                "cleanup_job_id": cj_failed_id,
                "archive_size_bytes": 4096,
                "updated_at": "2026-11-12T14:02:30+00:00",
            }
            backend.video_assets[vid_failed_id] = {
                "id": vid_failed_id,
                "message_id": msg_failed_id,
                "archive_state": "failed",
                "archive_last_error_code": "VID_FFMPEG",
                "archive_last_error_type": "DecodeError",
                "archive_attempt_count": 5,
                "updated_at": "2026-11-12T14:03:00+00:00",
            }
            run_id = str(uuid4())
            call_id = str(uuid4())
            backend.analysis_runs[run_id] = {
                "id": run_id,
                "message_id": msg_failed_id,
                "analysis_pipeline_version_id": str(uuid4()),
                "run_mode": "formal",
                "status": "failed",
                "created_at": "2026-11-12T14:03:30+00:00",
                "stage_run_ids": [sr_failed_id],
            }
            backend.inference_calls[call_id] = {
                "id": call_id,
                "provider_adapter": "openai_compatible",
                "model_name": "gpt-4o-mini",
                "status": "failed",
                "structured_output_schema_hash": "a" * 64,
                "token_usage": {"prompt_tokens": 50},
                "latency_ms": 112.0,
                "http_status": 502,
                "error_code": "BAD_GATEWAY",
                "error_type": "HttpStatusError",
                "raw_response": {"error": "bad gateway"},
            }
            backend.stage_runs[sr_failed_id] = {
                "id": sr_failed_id,
                "analysis_run_id": run_id,
                "message_id": msg_failed_id,
                "node_key": "root_gate",
                "analysis_stage_template_version_id": str(uuid4()),
                "target_scope": "global",
                "target_kind": "message",
                "target_id": msg_failed_id,
                "status": "failed",
                "attempt_count": 3,
                "last_error_code": "BAD_GATEWAY",
                "last_error_type": "HttpStatusError",
                "inference_call_id": call_id,
                "updated_at": "2026-11-12T14:04:00+00:00",
            }
            backend.publication_intents[pi_failed_id] = {
                "id": pi_failed_id,
                "message_id": msg_failed_id,
                "routing_evaluation_id": str(uuid4()),
                "destination_channel_id": str(uuid4()),
                "publish_identity_id": bot_ident,
                "publication_mode": "forward_only",
                "protected_content_fallback": "fail",
                "rendered_text": None,
                "status": "failed",
                "attempt_count": 4,
                "next_retry_at": None,
                "last_error_code": "CHAT_WRITE_FORBIDDEN",
                "last_error_type": "ChatWriteForbiddenError",
                "updated_at": "2026-11-12T14:05:00+00:00",
                "attempts": [
                    {
                        "id": str(uuid4()),
                        "attempt_number": 1,
                        "status": "failed",
                        "error_code": "CHAT_WRITE_FORBIDDEN",
                        "error_type": "ChatWriteForbiddenError",
                        "reconciled": False,
                        "started_at": "2026-11-12T14:04:55+00:00",
                    }
                ],
                "records": [],
            }
            backend.publish_attempts[pi_failed_id] = [
                {
                    "id": str(uuid4()),
                    "attempt_number": 1,
                    "status": "failed",
                    "error_code": "CHAT_WRITE_FORBIDDEN",
                    "error_type": "ChatWriteForbiddenError",
                    "reconciled": False,
                    "started_at": "2026-11-12T14:04:55+00:00",
                }
            ]
            backend.cleanup_items[cj_failed_id] = [
                {
                    "id": str(uuid4()),
                    "image_asset_id": img_dup_ready_id,
                    "status": "failed",
                },
                {
                    "id": str(uuid4()),
                    "image_asset_id": str(uuid4()),
                    "status": "skipped_in_use",
                },
            ]
            backend.cleanup_jobs[cj_failed_id] = {
                "id": cj_failed_id,
                "dry_run_id": str(uuid4()),
                "confirmed_by_admin_user_id": str(uuid4()),
                "filter_hash": "b" * 64,
                "estimated_asset_count": 2,
                "estimated_message_count": 1,
                "estimated_bytes": 4096,
                "item_count": 2,
                "status": "failed",
                "attempt_count": 5,
                "last_error_code": "IO_ERROR",
                "last_error_type": "OSError",
                "updated_at": "2026-11-12T14:06:00+00:00",
                "created_at": "2026-11-12T14:05:30+00:00",
                "_ts": clock[0],
            }

            # All 6 step_kinds returned newest-first
            failed_resp = client.get("/api/v1/operations/failed-items").json()["items"]
            self.assertEqual(
                [item["step_kind"] for item in failed_resp],
                [
                    "cleanup_job",
                    "publication_intent",
                    "stage_run",
                    "video_archive",
                    "image_archive",
                    "range_execution",
                ],
            )
            ops_status = client.get("/api/v1/operations/status").json()
            self.assertEqual(ops_status["failed_count"], 6)
            for q_key in (
                "range_execution",
                "image_archive",
                "video_archive",
                "analysis",
                "publication",
                "maintenance",
            ):
                self.assertIn(q_key, ops_status["queues"])

            # Message detail DTO fields check
            detail = client.get(f"/api/v1/messages/{msg_failed_id}").json()
            self.assertEqual(detail["message"]["blocked_by_label_key"], "nsfw_block")
            arun = detail["analysis_runs"][0]
            self.assertEqual(arun["created_at"], "2026-11-12T14:03:30+00:00")
            srun = arun["stage_runs"][0]
            self.assertEqual(srun["node_key"], "root_gate")
            self.assertEqual(srun["inference_call"]["http_status"], 502)
            self.assertEqual(srun["inference_call"]["error_type"], "HttpStatusError")
            self.assertEqual(srun["inference_call"]["raw_response"], {"error": "bad gateway"})
            intent_dto = detail["publication_intents"][0]
            self.assertIn("next_retry_at", intent_dto)
            self.assertEqual(intent_dto["attempts"][0]["error_type"], "ChatWriteForbiddenError")

            # Cleanup job DTO counts + retry restores deleting asset + resets failed items
            cj_dto = client.get(f"/api/v1/archive-cleanup/jobs/{cj_failed_id}").json()
            self.assertEqual(cj_dto["deleted_count"], 0)
            self.assertEqual(cj_dto["skipped_active_count"], 1)

            r_cj_retry = client.post(
                "/api/v1/operations/retry",
                headers=headers,
                json={"step_kind": "cleanup_job", "target_id": cj_failed_id},
            )
            self.assertEqual(r_cj_retry.status_code, 200)
            self.assertEqual(backend.image_assets[img_dup_ready_id]["archive_state"], "ready")
            self.assertEqual(backend.cleanup_items[cj_failed_id][0]["status"], "pending")
            # Retrying again when now pending -> 409 Conflict
            self.assertEqual(
                client.post(
                    "/api/v1/operations/retry",
                    headers=headers,
                    json={"step_kind": "cleanup_job", "target_id": cj_failed_id},
                ).status_code,
                409,
            )

            # Retry range_execution -> restores parent ProcessingRange to active
            r_rex_retry = client.post(
                "/api/v1/operations/retry",
                headers=headers,
                json={"step_kind": "range_execution", "target_id": rex_failed_id},
            )
            self.assertEqual(r_rex_retry.status_code, 200)
            self.assertEqual(backend.processing_ranges[rng_id]["status"], "active")

            # Retry image_archive -> restores parent MessageRecord to processing
            r_img_retry = client.post(
                "/api/v1/operations/retry",
                headers=headers,
                json={"step_kind": "image_archive", "target_id": img_failed_id},
            )
            self.assertEqual(r_img_retry.status_code, 200)
            self.assertEqual(backend.messages[msg_failed_id]["processing_status"], "processing")

            # Cleanup dry-run accepts duplicate_skipped alias -> canonical "duplicate"
            dry = client.post(
                "/api/v1/archive-cleanup/dry-runs",
                headers=headers,
                json={"duplicate_statuses": ["duplicate_skipped"]},
            )
            self.assertEqual(dry.status_code, 201)
            self.assertEqual(dry.json()["estimated_asset_count"], 1)

            # Prometheus metrics endpoint exposes all metric families
            metrics_text = client.get("/metrics").text
            for family_name in (
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
                self.assertIn(family_name, metrics_text)


if __name__ == "__main__":
    unittest.main()
