from __future__ import annotations

import asyncio
import base64
import json
import unittest
from typing import Any
from uuid import uuid4

import httpx

from apps.api import create_app
from tgcurator.application import Settings
from tgcurator.application.admin import (
    SESSION_COOKIE_NAME,
    AdminApplicationService,
    AdminSessionService,
    InMemoryAdminBackend,
    InMemorySecretVault,
    PromptTestBenchService,
)
from tgcurator.application.ports.analysis import InferenceRequest, InferenceResponse
from tgcurator.domain.review import LabelValue
from tgcurator.infrastructure.security import Argon2idPasswordHasher


class FakeDatabaseProbe:
    async def ping(self) -> bool:
        return True

    async def dispose(self) -> None:
        return None


class DeterministicTestBenchProvider:
    def __init__(self) -> None:
        self.calls: list[InferenceRequest] = []
        self.response_payload: dict[str, object] = {
            "result": {
                "scores": {"illustration": 0.92, "spam_ad": 0.05},
            }
        }

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.calls.append(request)
        return InferenceResponse(
            payload=self.response_payload,
            provider_adapter=request.profile.provider_adapter,
            model_name=request.profile.model_name,
            raw_response={"id": "cmpl-test-bench", "choices": [{"index": 0}]},
            latency_ms=14.5,
            http_status=200,
            token_usage={"prompt_tokens": 85, "completion_tokens": 22, "total_tokens": 107},
        )


class SyncAsgiClient:
    def __init__(self, app: Any, *, base_url: str = "http://testserver") -> None:
        self.app = app
        self._base_url = base_url
        self._cookies = httpx.Cookies()
        self._runner: asyncio.Runner | None = None
        self._lifespan_cm: Any = None
        self._client: httpx.AsyncClient | None = None

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies if self._client is not None else self._cookies

    def __enter__(self) -> SyncAsgiClient:
        if self._runner is None:
            self._runner = asyncio.Runner()
            self._lifespan_cm = self.app.router.lifespan_context(self.app)
            self._runner.run(self._lifespan_cm.__aenter__())
            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self._base_url,
                cookies=self._cookies,
            )
            self._runner.run(self._client.__aenter__())
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._runner is not None:
            try:
                if self._client is not None:
                    self._cookies = self._client.cookies
                    self._runner.run(self._client.__aexit__(exc_type, exc_val, exc_tb))
                if self._lifespan_cm is not None:
                    self._runner.run(self._lifespan_cm.__aexit__(exc_type, exc_val, exc_tb))
            finally:
                self._client = None
                self._lifespan_cm = None
                self._runner.close()
                self._runner = None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._runner is not None and self._client is not None:
            return self._runner.run(self._client.request(method, url, **kwargs))

        async def _oneshot() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self._base_url,
                cookies=self._cookies,
            ) as client:
                resp = await client.request(method, url, **kwargs)
                self._cookies = client.cookies
                return resp

        return asyncio.run(_oneshot())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)


def _build_test_stack(
    *,
    environment: str = "test",
    with_provider: bool = True,
) -> tuple[SyncAsgiClient, InMemoryAdminBackend, DeterministicTestBenchProvider | None]:
    hasher = Argon2idPasswordHasher()
    backend = InMemoryAdminBackend()
    backend.seed_admin(
        username="curator-admin",
        password_hash=hasher.hash("correct-horse-battery-staple"),
        user_id=str(uuid4()),
    )
    secrets_vault = InMemorySecretVault()
    sessions = AdminSessionService(
        repository=backend,
        password_hasher=hasher,
        csrf_secret=b"unit-test-csrf-secret-32-bytes!!",
    )
    provider = DeterministicTestBenchProvider() if with_provider else None
    service = AdminApplicationService(
        sessions=sessions,
        secrets=secrets_vault,
        backend=backend,
        test_bench=PromptTestBenchService(provider=provider),
    )
    master_key_b64 = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    settings = Settings(
        environment=environment,  # type: ignore[arg-type]
        app_name="tg-vlm-curator-api-test",
        app_master_key=master_key_b64,  # type: ignore[arg-type]
    )
    app = create_app(settings, FakeDatabaseProbe(), admin_service=service)
    return SyncAsgiClient(app), backend, provider


def _login_and_csrf(client: SyncAsgiClient) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={
            "username": "curator-admin",
            "password": "correct-horse-battery-staple",
        },
    )
    assert resp.status_code == 200, resp.text
    csrf_token = resp.json()["csrf_token"]
    return {"X-CSRF-Token": csrf_token}


class AdminApiSecurityAndWorkflowTests(unittest.TestCase):
    def test_session_cookie_flags_csrf_enforcement_and_logout_revocation(self) -> None:
        client, _backend, _ = _build_test_stack(environment="production")
        with client:
            unauth = client.get("/api/v1/dashboard/summary")
            self.assertEqual(unauth.status_code, 401)
            self.assertEqual(unauth.json()["error"]["code"], "unauthenticated")

            bad_login = client.post(
                "/api/v1/auth/login",
                json={"username": "curator-admin", "password": "wrong-password"},
            )
            self.assertEqual(bad_login.status_code, 401)

            # Use https base_url so Secure cookie is sent back by SyncAsgiClient
            https_client = SyncAsgiClient(client.app, base_url="https://curator.example.test")
            login_resp = https_client.post(
                "/api/v1/auth/login",
                json={
                    "username": "curator-admin",
                    "password": "correct-horse-battery-staple",
                },
            )
            self.assertEqual(login_resp.status_code, 200)
            set_cookie = login_resp.headers.get("set-cookie", "").lower()
            self.assertIn(SESSION_COOKIE_NAME, set_cookie)
            self.assertIn("httponly", set_cookie)
            self.assertIn("secure", set_cookie)
            self.assertIn("samesite=lax", set_cookie)

            me_resp = https_client.get("/api/v1/auth/me")
            self.assertEqual(me_resp.status_code, 200)
            csrf = me_resp.json()["csrf_token"]
            self.assertEqual(me_resp.json()["user"]["username"], "curator-admin")

            # Mutating request without CSRF token must fail with 403
            missing_csrf = https_client.post(
                "/api/v1/secrets",
                json={"secret_type": "bot_token", "plaintext": "12345:secret"},
            )
            self.assertEqual(missing_csrf.status_code, 403)
            self.assertEqual(missing_csrf.json()["error"]["code"], "csrf_invalid")

            # Mutating request with forged CSRF token must fail with 403
            bad_csrf = https_client.post(
                "/api/v1/secrets",
                headers={"X-CSRF-Token": "0" * 64},
                json={"secret_type": "bot_token", "plaintext": "12345:secret"},
            )
            self.assertEqual(bad_csrf.status_code, 403)

            # Logout with valid CSRF revokes session
            logout_resp = https_client.post(
                "/api/v1/auth/logout",
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(logout_resp.status_code, 200)

            after_logout = https_client.get("/api/v1/auth/me")
            self.assertEqual(after_logout.status_code, 401)

    def test_secrets_are_write_only_and_never_leak_plaintext_in_responses_or_audits(self) -> None:
        client, _backend, _ = _build_test_stack()
        raw_secret_a = "sk-live-extremely-sensitive-credential-001"
        raw_secret_b = "sk-live-rotated-credential-002"
        with client:
            headers = _login_and_csrf(client)
            created = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "inference_api_key", "plaintext": raw_secret_a},
            )
            self.assertEqual(created.status_code, 201)
            body = created.json()
            secret_id = body["secret_id"]
            self.assertTrue(body["configured"])
            self.assertNotIn(raw_secret_a, json.dumps(body))
            self.assertNotIn("plaintext", body)
            self.assertNotIn("ciphertext", body)

            rotated = client.put(
                f"/api/v1/secrets/{secret_id}",
                headers=headers,
                json={"plaintext": raw_secret_b},
            )
            self.assertEqual(rotated.status_code, 200)
            self.assertNotIn(raw_secret_b, rotated.text)

            listed = client.get("/api/v1/secrets")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(len(listed.json()["items"]), 1)
            self.assertNotIn(raw_secret_a, listed.text)
            self.assertNotIn(raw_secret_b, listed.text)

            fetched = client.get(f"/api/v1/secrets/{secret_id}")
            self.assertEqual(fetched.status_code, 200)
            self.assertNotIn(raw_secret_b, fetched.text)

            audits = client.get("/api/v1/audit-events", params={"entity_type": "encrypted_secret"})
            self.assertEqual(audits.status_code, 200)
            self.assertGreaterEqual(len(audits.json()["items"]), 2)
            self.assertNotIn(raw_secret_a, audits.text)
            self.assertNotIn(raw_secret_b, audits.text)

    def test_config_lifecycle_immutability_dag_validation_and_prompt_test_bench(self) -> None:
        client, backend, provider = _build_test_stack(with_provider=True)
        assert provider is not None
        with client:
            headers = _login_and_csrf(client)

            families_resp = client.get("/api/v1/config/families")
            self.assertEqual(families_resp.status_code, 200)
            self.assertEqual(len(families_resp.json()["families"]), 10)

            # 1. Create secret + identities + source & destination channels
            sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "inference_api_key", "plaintext": "sk-test-provider"},
            ).json()
            tg_sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "telethon_session", "plaintext": "1BVtsOHwBu..."},
            ).json()

            user_id_resp = client.post(
                "/api/v1/telegram-identities",
                headers=headers,
                json={
                    "name": "primary-reader",
                    "identity_type": "mtproto_user",
                    "secret_id": tg_sec["secret_id"],
                },
            )
            self.assertEqual(user_id_resp.status_code, 201)
            identity_id = user_id_resp.json()["id"]
            self.assertTrue(user_id_resp.json()["capabilities"]["can_read_source"])

            cap_eval = client.post(
                f"/api/v1/telegram-identities/{identity_id}/evaluate-capabilities",
                headers=headers,
                json={"source_protected_content": True},
            )
            self.assertEqual(cap_eval.status_code, 200)
            self.assertFalse(cap_eval.json()["capabilities"]["can_native_forward"])

            dest = client.post(
                "/api/v1/destination-channels",
                headers=headers,
                json={
                    "telegram_peer_id": -100999888,
                    "name": "Curated Art Feed",
                    "default_publish_identity_id": identity_id,
                },
            )
            self.assertEqual(dest.status_code, 201)
            dest_id = dest.json()["id"]

            # 2. Media Processing Profile (DRAFT -> edit -> PUBLISH -> reject edit with 409)
            mp_ent = client.post(
                "/api/v1/config/media-processing-profiles",
                headers=headers,
                json={"name": "standard-webp"},
            ).json()
            mp_ver = client.post(
                f"/api/v1/config/media-processing-profiles/{mp_ent['id']}/versions",
                headers=headers,
                json={
                    "content": {
                        "image_max_dimension": 1536,
                        "webp_quality": 82,
                        "video_max_frames": 6,
                    }
                },
            )
            self.assertEqual(mp_ver.status_code, 201)
            mp_vid = mp_ver.json()["id"]

            edited_draft = client.patch(
                f"/api/v1/config/media-processing-profiles/{mp_ent['id']}/versions/{mp_vid}",
                headers=headers,
                json={
                    "image_max_dimension": 2048,
                    "webp_quality": 85,
                    "video_max_frames": 8,
                },
            )
            self.assertEqual(edited_draft.status_code, 200)

            pub_mp = client.post(
                f"/api/v1/config/media-processing-profiles/{mp_ent['id']}/versions/{mp_vid}/publish",
                headers=headers,
            )
            self.assertEqual(pub_mp.status_code, 200)
            self.assertEqual(pub_mp.json()["state"], "published")

            mutate_published = client.patch(
                f"/api/v1/config/media-processing-profiles/{mp_ent['id']}/versions/{mp_vid}",
                headers=headers,
                json={"image_max_dimension": 1024, "webp_quality": 80, "video_max_frames": 4},
            )
            self.assertEqual(mutate_published.status_code, 409)

            # 3. Labels, LabelSet, PromptTemplate, InferenceProfile, Stage, Pipeline
            pos_def = client.post(
                "/api/v1/config/label-definitions",
                headers=headers,
                json={"name": "illustration"},
            ).json()
            pos_ver = client.post(
                f"/api/v1/config/label-definitions/{pos_def['id']}/versions",
                headers=headers,
                json={
                    "key": "illustration",
                    "display_name": "Illustration",
                    "description": "Anime or digital illustration",
                    "scope": "global",
                    "negative": False,
                },
            ).json()
            client.post(
                f"/api/v1/config/label-definitions/{pos_def['id']}/versions/{pos_ver['id']}/publish",
                headers=headers,
            )

            neg_def = client.post(
                "/api/v1/config/label-definitions",
                headers=headers,
                json={"name": "spam_ad"},
            ).json()
            neg_ver = client.post(
                f"/api/v1/config/label-definitions/{neg_def['id']}/versions",
                headers=headers,
                json={
                    "key": "spam_ad",
                    "display_name": "Spam Ad",
                    "description": "Promotional spam",
                    "scope": "global",
                    "negative": True,
                },
            ).json()
            client.post(
                f"/api/v1/config/label-definitions/{neg_def['id']}/versions/{neg_ver['id']}/publish",
                headers=headers,
            )

            ls_ent = client.post(
                "/api/v1/config/label-sets",
                headers=headers,
                json={"name": "global-curation-set"},
            ).json()
            ls_ver = client.post(
                f"/api/v1/config/label-sets/{ls_ent['id']}/versions",
                headers=headers,
                json={
                    "bindings": [
                        {
                            "label_definition_version_id": pos_ver["id"],
                            "activation_threshold": 0.7,
                            "output_order": 0,
                            "prompt_hint": "High-quality artwork",
                        },
                        {
                            "label_definition_version_id": neg_ver["id"],
                            "activation_threshold": 0.5,
                            "output_order": 1,
                            "prompt_hint": "Commercial spam or gambling link",
                        },
                    ]
                },
            ).json()
            client.post(
                f"/api/v1/config/label-sets/{ls_ent['id']}/versions/{ls_ver['id']}/publish",
                headers=headers,
            )

            pt_ent = client.post(
                "/api/v1/config/prompt-templates",
                headers=headers,
                json={"name": "prescreen-prompt"},
            ).json()
            pt_ver = client.post(
                f"/api/v1/config/prompt-templates/{pt_ent['id']}/versions",
                headers=headers,
                json={
                    "system_prompt": (
                        "Classify Telegram posts strictly using JSON: {{ label_definitions }}"
                    ),
                    "user_prompt_template": "Analyze post:\n{{ message_text }}",
                    "declared_variables": ["label_definitions", "message_text"],
                },
            ).json()
            client.post(
                f"/api/v1/config/prompt-templates/{pt_ent['id']}/versions/{pt_ver['id']}/publish",
                headers=headers,
            )

            ip_ent = client.post(
                "/api/v1/config/inference-profiles",
                headers=headers,
                json={"name": "external-vllm"},
            ).json()
            ip_ver = client.post(
                f"/api/v1/config/inference-profiles/{ip_ent['id']}/versions",
                headers=headers,
                json={
                    "provider_adapter": "openai_compatible",
                    "base_url": "https://vlm.internal.example/v1",
                    "model_name": "qwen2.5-vl-72b",
                    "api_secret_reference": sec["secret_id"],
                    "capabilities": ["text", "vision"],
                    "structured_output_support": True,
                },
            ).json()
            client.post(
                f"/api/v1/config/inference-profiles/{ip_ent['id']}/versions/{ip_ver['id']}/publish",
                headers=headers,
            )

            st_ent = client.post(
                "/api/v1/config/analysis-stages",
                headers=headers,
                json={"name": "text-prescreen"},
            ).json()
            st_ver = client.post(
                f"/api/v1/config/analysis-stages/{st_ent['id']}/versions",
                headers=headers,
                json={
                    "name": "text_prescreen",
                    "target_scope": "global",
                    "execution_mode": "single_message",
                    "prompt_template_version_id": pt_ver["id"],
                    "label_set_version_id": ls_ver["id"],
                    "inference_profile_version_id": ip_ver["id"],
                    "input_policy": {"text": True},
                    "structured_output_policy": {"mode": "dense_scores"},
                },
            ).json()
            client.post(
                f"/api/v1/config/analysis-stages/{st_ent['id']}/versions/{st_ver['id']}/publish",
                headers=headers,
            )

            # Cyclic pipeline DAG must be rejected with 422
            pipe_ent = client.post(
                "/api/v1/config/analysis-pipelines",
                headers=headers,
                json={"name": "main-pipeline"},
            ).json()
            cyclic_resp = client.post(
                f"/api/v1/config/analysis-pipelines/{pipe_ent['id']}/versions",
                headers=headers,
                json={
                    "nodes": [
                        {
                            "node_key": "a",
                            "analysis_stage_template_version_id": st_ver["id"],
                            "depends_on": ["b"],
                        },
                        {
                            "node_key": "b",
                            "analysis_stage_template_version_id": st_ver["id"],
                            "depends_on": ["a"],
                        },
                    ]
                },
            )
            self.assertEqual(cyclic_resp.status_code, 422)

            pipe_ver = client.post(
                f"/api/v1/config/analysis-pipelines/{pipe_ent['id']}/versions",
                headers=headers,
                json={
                    "nodes": [
                        {
                            "node_key": "prescreen",
                            "analysis_stage_template_version_id": st_ver["id"],
                            "depends_on": [],
                        }
                    ]
                },
            ).json()
            pub_pipe = client.post(
                f"/api/v1/config/analysis-pipelines/{pipe_ent['id']}/versions/{pipe_ver['id']}/publish",
                headers=headers,
            )
            self.assertEqual(pub_pipe.status_code, 200)

            # 4. Rendering template & Routing policy (reject invalid DSL, publish valid)
            rt_ent = client.post(
                "/api/v1/config/rendering-templates",
                headers=headers,
                json={"name": "default-caption"},
            ).json()
            rt_ver = client.post(
                f"/api/v1/config/rendering-templates/{rt_ent['id']}/versions",
                headers=headers,
                json={"template_text": "Source: {{ source_channel.display_name }}"},
            ).json()
            client.post(
                f"/api/v1/config/rendering-templates/{rt_ent['id']}/versions/{rt_ver['id']}/publish",
                headers=headers,
            )

            rp_ent = client.post(
                "/api/v1/config/routing-policies",
                headers=headers,
                json={"name": "art-routing"},
            ).json()
            bad_dsl = client.post(
                f"/api/v1/config/routing-policies/{rp_ent['id']}/versions",
                headers=headers,
                json={
                    "rules": [
                        {
                            "priority": 100,
                            "condition": {"op": "arbitrary_python_eval", "expr": "1==1"},
                            "actions": [
                                {
                                    "destination_channel_id": dest_id,
                                    "publication_mode": "copy_with_caption",
                                    "rendering_template_version_id": rt_ver["id"],
                                }
                            ],
                        }
                    ]
                },
            )
            self.assertEqual(bad_dsl.status_code, 422)

            rp_ver = client.post(
                f"/api/v1/config/routing-policies/{rp_ent['id']}/versions",
                headers=headers,
                json={
                    "rules": [
                        {
                            "priority": 100,
                            "stop_on_match": True,
                            "condition": {
                                "label": {
                                    "namespace": "effective",
                                    "scope": "global",
                                    "key": "illustration",
                                }
                            },
                            "actions": [
                                {
                                    "destination_channel_id": dest_id,
                                    "publication_mode": "copy_with_caption",
                                    "rendering_template_version_id": rt_ver["id"],
                                }
                            ],
                        }
                    ]
                },
            ).json()
            self.assertEqual(
                client.post(
                    f"/api/v1/config/routing-policies/{rp_ent['id']}/versions/{rp_ver['id']}/publish",
                    headers=headers,
                ).status_code,
                200,
            )

            # 5. Enforce DRAFT -> PUBLISHED -> ARCHIVED lifecycle and archived immutability
            extra_mp_ent = client.post(
                "/api/v1/config/media-processing-profiles",
                headers=headers,
                json={"name": "legacy-media-profile"},
            ).json()
            extra_mp = client.post(
                f"/api/v1/config/media-processing-profiles/{extra_mp_ent['id']}/versions",
                headers=headers,
                json={"image_max_dimension": 1024, "webp_quality": 75, "video_max_frames": 4},
            ).json()
            # Archiving a draft directly is rejected (must follow DRAFT -> PUBLISHED -> ARCHIVED)
            self.assertEqual(
                client.post(
                    f"/api/v1/config/media-processing-profiles/{extra_mp_ent['id']}/versions/{extra_mp['id']}/archive",
                    headers=headers,
                ).status_code,
                409,
            )
            self.assertEqual(
                client.post(
                    f"/api/v1/config/media-processing-profiles/{extra_mp_ent['id']}/versions/{extra_mp['id']}/publish",
                    headers=headers,
                ).status_code,
                200,
            )
            arch_resp = client.post(
                f"/api/v1/config/media-processing-profiles/{extra_mp_ent['id']}/versions/{extra_mp['id']}/archive",
                headers=headers,
            )
            self.assertEqual(arch_resp.status_code, 200)
            self.assertEqual(arch_resp.json()["state"], "archived")
            self.assertEqual(
                client.patch(
                    f"/api/v1/config/media-processing-profiles/{extra_mp_ent['id']}/versions/{extra_mp['id']}",
                    headers=headers,
                    json={"image_max_dimension": 512, "webp_quality": 75, "video_max_frames": 4},
                ).status_code,
                409,
            )

            # 6. Source channel + source channel profile + Prompt Test Bench side-effect isolation
            src = client.post(
                "/api/v1/source-channels",
                headers=headers,
                json={
                    "telegram_peer_id": -100123456,
                    "display_name": "Artist Showcase",
                    "username": "artist_showcase",
                    "default_read_identity_id": identity_id,
                },
            ).json()

            scp_ent = client.post(
                "/api/v1/config/source-channel-profiles",
                headers=headers,
                json={"name": "showcase-profile", "source_channel_id": src["id"]},
            ).json()
            scp_ver = client.post(
                f"/api/v1/config/source-channel-profiles/{scp_ent['id']}/versions",
                headers=headers,
                json={
                    "analysis_pipeline_version_id": pipe_ver["id"],
                    "routing_policy_version_id": rp_ver["id"],
                    "media_processing_profile_version_id": mp_vid,
                    "duplicate_policy": "skip",
                    "phash_distance_threshold": 6,
                    "prescreen_enabled": True,
                },
            ).json()
            self.assertEqual(
                client.post(
                    f"/api/v1/config/source-channel-profiles/{scp_ent['id']}/versions/{scp_ver['id']}/publish",
                    headers=headers,
                ).status_code,
                200,
            )
            client.patch(
                f"/api/v1/source-channels/{src['id']}",
                headers=headers,
                json={"active_profile_version_id": scp_ver["id"]},
            )

            # Seed a formal message and verify Prompt Test Bench never mutates its formal state
            msg_id = str(uuid4())
            backend.messages[msg_id] = {
                "id": msg_id,
                "source_channel_id": src["id"],
                "primary_telegram_message_id": 101,
                "published_at": "2026-11-12T06:00:00+00:00",
                "original_text": "Original watercolor landscape sketch",
                "media_count": 0,
                "processing_status": "processed",
                "terminal_reason": "completed",
                "duplicate_status": "unique",
                "review_status": "unreviewed",
                "blocked_from_analysis": False,
                "blocked_by_label_key": None,
            }

            tb_resp = client.post(
                "/api/v1/analysis/test-bench",
                headers=headers,
                json={
                    "stage_version_id": st_ver["id"],
                    "message_id": msg_id,
                    "invoke_provider": True,
                },
            )
            self.assertEqual(tb_resp.status_code, 200)
            tb = tb_resp.json()
            self.assertEqual(tb["run_mode"], "test")
            self.assertEqual(tb["status"], "completed")
            self.assertIn("Original watercolor landscape sketch", tb["user_prompt"])
            self.assertEqual(len(tb["input_manifest_hash"]), 64)
            self.assertEqual(len(tb["structured_output_schema_hash"]), 64)
            self.assertEqual(tb["token_usage"]["total_tokens"], 107)
            self.assertEqual(len(tb["parsed_results"]), 1)
            # Formal labels and routing evaluations remain untouched!
            self.assertEqual(backend.model_labels.get(msg_id, []), [])
            self.assertFalse(backend.messages[msg_id]["blocked_from_analysis"])
            self.assertEqual(len(backend.routing_evaluations), 0)

    def test_messages_review_reroute_skip_retry_cleanup_and_prometheus_metrics(self) -> None:
        client, backend, _ = _build_test_stack()
        with client:
            headers = _login_and_csrf(client)

            # Provision secret, identity, source, two destinations, label def, template, policies
            tg_sec = client.post(
                "/api/v1/secrets",
                headers=headers,
                json={"secret_type": "telegram_session", "plaintext": "1BVtsOHwBu-mtproto-session"},
            ).json()
            ident = client.post(
                "/api/v1/telegram-identities",
                headers=headers,
                json={
                    "name": "mtproto-main",
                    "identity_type": "mtproto_user",
                    "secret_id": tg_sec["secret_id"],
                },
            ).json()
            src = client.post(
                "/api/v1/source-channels",
                headers=headers,
                json={
                    "telegram_peer_id": -100555001,
                    "display_name": "Source Alpha",
                    "default_read_identity_id": ident["id"],
                },
            ).json()
            dest_a = client.post(
                "/api/v1/destination-channels",
                headers=headers,
                json={
                    "telegram_peer_id": -100777001,
                    "name": "Dest Illustrations",
                    "default_publish_identity_id": ident["id"],
                },
            ).json()
            dest_b = client.post(
                "/api/v1/destination-channels",
                headers=headers,
                json={
                    "telegram_peer_id": -100777002,
                    "name": "Dest Curated VIP",
                    "default_publish_identity_id": ident["id"],
                },
            ).json()

            lbl_ent = client.post(
                "/api/v1/config/label-definitions",
                headers=headers,
                json={"name": "masterpiece"},
            ).json()
            lbl_ver = client.post(
                f"/api/v1/config/label-definitions/{lbl_ent['id']}/versions",
                headers=headers,
                json={
                    "key": "masterpiece",
                    "display_name": "Masterpiece",
                    "description": "Curator pick",
                    "scope": "global",
                    "negative": False,
                },
            ).json()
            client.post(
                f"/api/v1/config/label-definitions/{lbl_ent['id']}/versions/{lbl_ver['id']}/publish",
                headers=headers,
            )

            rt_ent = client.post(
                "/api/v1/config/rendering-templates",
                headers=headers,
                json={"name": "vip-caption"},
            ).json()
            rt_ver = client.post(
                f"/api/v1/config/rendering-templates/{rt_ent['id']}/versions",
                headers=headers,
                json={"template_text": "Curated from {{ source_channel.display_name }}"},
            ).json()
            client.post(
                f"/api/v1/config/rendering-templates/{rt_ent['id']}/versions/{rt_ver['id']}/publish",
                headers=headers,
            )

            rp_ent = client.post(
                "/api/v1/config/routing-policies",
                headers=headers,
                json={"name": "vip-policy"},
            ).json()
            rp_ver = client.post(
                f"/api/v1/config/routing-policies/{rp_ent['id']}/versions",
                headers=headers,
                json={
                    "rules": [
                        {
                            "priority": 100,
                            "stop_on_match": False,
                            "condition": {
                                "label": {
                                    "namespace": "effective",
                                    "scope": "global",
                                    "key": "masterpiece",
                                }
                            },
                            "actions": [
                                {
                                    "destination_channel_id": dest_b["id"],
                                    "publication_mode": "copy_with_caption",
                                    "rendering_template_version_id": rt_ver["id"],
                                }
                            ],
                        }
                    ]
                },
            ).json()
            client.post(
                f"/api/v1/config/routing-policies/{rp_ent['id']}/versions/{rp_ver['id']}/publish",
                headers=headers,
            )

            # Create processing range and pause/resume via HTTP
            pr = client.post(
                "/api/v1/processing-ranges",
                headers=headers,
                json={
                    "source_channel_id": src["id"],
                    "start_at": "2026-11-12T00:00:00+00:00",
                    "end_mode": "latest",
                    "steady_after_seconds": 300,
                },
            )
            self.assertEqual(pr.status_code, 201)
            range_id = pr.json()["id"]
            self.assertEqual(
                client.post(f"/api/v1/processing-ranges/{range_id}/pause", headers=headers).json()[
                    "status"
                ],
                "paused",
            )
            self.assertEqual(
                client.post(f"/api/v1/processing-ranges/{range_id}/resume", headers=headers).json()[
                    "status"
                ],
                "active",
            )

            # Seed 2 messages inside an awaiting_pipeline execution window (10..12):
            # msg1 (tg id 11) is failed -> blocks watermark at 10;
            # msg2 (tg id 12) is processed with model label (activated=False) + publish record!
            msg1_id = str(uuid4())
            msg2_id = str(uuid4())
            img2_id = str(uuid4())
            exec_id = str(uuid4())
            backend.messages[msg1_id] = {
                "id": msg1_id,
                "source_channel_id": src["id"],
                "primary_telegram_message_id": 11,
                "published_at": "2026-11-12T01:00:00+00:00",
                "original_text": "Corrupted upload item",
                "media_count": 1,
                "processing_status": "failed",
                "terminal_reason": None,
                "failure_stage": "media_failed",
                "last_error_code": "MEDIA_UNAVAILABLE",
                "duplicate_status": "unchecked",
                "review_status": "unreviewed",
                "blocked_from_analysis": False,
            }
            backend.messages[msg2_id] = {
                "id": msg2_id,
                "source_channel_id": src["id"],
                "primary_telegram_message_id": 12,
                "published_at": "2026-11-12T02:00:00+00:00",
                "original_text": "Stunning oil painting study",
                "media_count": 1,
                "processing_status": "processed",
                "terminal_reason": "completed",
                "duplicate_status": "unique",
                "review_status": "unreviewed",
                "blocked_from_analysis": False,
            }
            backend.image_assets[img2_id] = {
                "id": img2_id,
                "message_id": msg2_id,
                "archive_state": "ready",
                "archive_size_bytes": 204800,
                "storage_key": f"images/{img2_id}.webp",
                "archive_sha256": "c" * 64,
                "width": 1024,
                "height": 1024,
            }
            backend.model_labels[msg2_id] = [
                LabelValue(
                    message_id=msg2_id,
                    target_scope="global",
                    target_kind="message",
                    target_id=msg2_id,
                    label_definition_version_id=lbl_ver["id"],
                    label_key="masterpiece",
                    score=0.35,
                    activated=False,
                )
            ]
            prior_eval_id = str(uuid4())
            prior_intent_id = str(uuid4())
            prior_record_id = str(uuid4())
            backend.routing_evaluations[prior_eval_id] = {
                "id": prior_eval_id,
                "routing_request_id": "formal-req-1",
                "message_id": msg2_id,
                "routing_policy_version_id": rp_ver["id"],
                "facts_snapshot": {},
                "facts_hash": "d" * 64,
                "rule_outcomes": [],
                "stopped_at_rule_id": None,
                "created_at": "2026-11-12T02:05:00+00:00",
            }
            backend.publication_intents[prior_intent_id] = {
                "id": prior_intent_id,
                "routing_evaluation_id": prior_eval_id,
                "routing_request_id": "formal-req-1",
                "message_id": msg2_id,
                "routing_policy_version_id": rp_ver["id"],
                "destination_channel_id": dest_a["id"],
                "publish_identity_id": ident["id"],
                "publication_mode": "forward_only",
                "status": "sent",
                "attempt_count": 1,
                "created_at": "2026-11-12T02:05:00+00:00",
            }
            backend.publish_records[prior_intent_id] = [
                {
                    "id": prior_record_id,
                    "publication_intent_id": prior_intent_id,
                    "part_kind": "forward",
                    "remote_message_id": 8801,
                }
            ]
            backend.range_executions[exec_id] = {
                "id": exec_id,
                "processing_range_id": range_id,
                "from_message_id_exclusive": 10,
                "to_message_id_inclusive": 12,
                "watermark_message_id": 10,
                "history_ingested": True,
                "status": "awaiting_pipeline",
                "attempt_count": 1,
                "created_at": "2026-11-12T02:00:00+00:00",
            }

            # Cursor pagination over /api/v1/messages
            page1 = client.get("/api/v1/messages", params={"limit": 1}).json()
            self.assertEqual(len(page1["items"]), 1)
            self.assertEqual(page1["items"][0]["id"], msg2_id)
            self.assertTrue(page1["has_more"])

            page2 = client.get(
                "/api/v1/messages",
                params={"limit": 1, "cursor": page1["next_cursor"]},
            ).json()
            self.assertEqual(len(page2["items"]), 1)
            self.assertEqual(page2["items"][0]["id"], msg1_id)
            self.assertFalse(page2["has_more"])

            # Manual label override preserves ModelLabelAssignment!
            lbl_set_resp = client.post(
                f"/api/v1/messages/{msg2_id}/labels",
                headers=headers,
                json={
                    "operation": "set",
                    "target_scope": "global",
                    "target_kind": "message",
                    "target_id": msg2_id,
                    "label_definition_version_id": lbl_ver["id"],
                    "label_key": "masterpiece",
                    "score": 1.0,
                    "activated": True,
                    "reason": "Curator verified highlight",
                },
            )
            self.assertEqual(lbl_set_resp.status_code, 200)

            rev_resp = client.post(
                f"/api/v1/messages/{msg2_id}/review-status",
                headers=headers,
                json={"new_status": "reviewed", "reason": "Approved"},
            )
            self.assertEqual(rev_resp.status_code, 200)
            self.assertEqual(rev_resp.json()["new_status"], "reviewed")

            detail = client.get(f"/api/v1/messages/{msg2_id}").json()
            # Model score remains 0.35 / False while effective becomes 1.0 / True!
            self.assertFalse(detail["labels"]["model"][0]["activated"])
            self.assertEqual(detail["labels"]["model"][0]["score"], 0.35)
            self.assertTrue(
                detail["labels"]["namespaces"]["effective"]["global"]["masterpiece"]["activated"]
            )

            # Dry-run reroute shows diff without persisting evaluation or intents
            dry_reroute = client.post(
                f"/api/v1/messages/{msg2_id}/reroute",
                headers=headers,
                json={"routing_policy_version_id": rp_ver["id"], "dry_run": True},
            )
            self.assertEqual(dry_reroute.status_code, 200)
            self.assertTrue(dry_reroute.json()["dry_run"])
            self.assertFalse(dry_reroute.json()["persisted"])
            self.assertIn(dest_b["id"], dry_reroute.json()["comparison"]["added_destinations"])
            self.assertEqual(len(backend.routing_evaluations), 1)

            # Formal reroute creates new evaluation & intent WITHOUT deleting prior PublishRecord!
            formal_reroute = client.post(
                f"/api/v1/messages/{msg2_id}/reroute",
                headers=headers,
                json={
                    "routing_policy_version_id": rp_ver["id"],
                    "dry_run": False,
                    "reason": "Manual masterpiece promotion",
                },
            )
            self.assertEqual(formal_reroute.status_code, 200)
            self.assertTrue(formal_reroute.json()["persisted"])
            self.assertEqual(len(backend.routing_evaluations), 2)
            self.assertEqual(len(backend.publish_records[prior_intent_id]), 1)

            # Administrator skip unblocks continuous processing_watermark from 10 -> 12!
            skip_resp = client.post(
                f"/api/v1/messages/{msg1_id}/skip",
                headers=headers,
                json={"reason": "Source media deleted upstream before download"},
            )
            self.assertEqual(skip_resp.status_code, 200)
            self.assertEqual(skip_resp.json()["terminal_reason"], "admin_skipped")
            self.assertEqual(
                skip_resp.json()["settled_executions"][0]["new_watermark_message_id"],
                12,
            )
            self.assertTrue(skip_resp.json()["settled_executions"][0]["window_completed"])
            self.assertEqual(
                backend.processing_ranges[range_id]["processing_watermark_message_id"],
                12,
            )

            # Step-level failed item listing & exact step retry
            failed_sr_id = str(uuid4())
            backend.stage_runs[failed_sr_id] = {
                "id": failed_sr_id,
                "analysis_run_id": str(uuid4()),
                "message_id": msg2_id,
                "status": "failed",
                "attempt_count": 3,
                "last_error_code": "TIMEOUT",
                "last_error_type": "ReadTimeout",
            }
            failed_list = client.get("/api/v1/operations/failed-items").json()
            self.assertEqual(len(failed_list["items"]), 1)
            self.assertEqual(failed_list["items"][0]["target_id"], failed_sr_id)

            retry_resp = client.post(
                "/api/v1/operations/retry",
                headers=headers,
                json={
                    "step_kind": "stage_run",
                    "target_id": failed_sr_id,
                    "reason": "Provider recovered",
                },
            )
            self.assertEqual(retry_resp.status_code, 200)
            self.assertEqual(retry_resp.json()["enqueued_queue"], "analysis")
            self.assertEqual(backend.stage_runs[failed_sr_id]["status"], "pending")

            # Two-step Archive Cleanup: reject missing dry-run & hash mismatch, accept exact match
            missing_confirm = client.post(
                "/api/v1/archive-cleanup/jobs",
                headers=headers,
                json={"dry_run_id": str(uuid4()), "expected_filter_hash": "a" * 64},
            )
            self.assertEqual(missing_confirm.status_code, 404)

            dry_run = client.post(
                "/api/v1/archive-cleanup/dry-runs",
                headers=headers,
                json={"review_statuses": ["reviewed"], "media_types": ["image"]},
            )
            self.assertEqual(dry_run.status_code, 201)
            dry_body = dry_run.json()
            self.assertEqual(dry_body["estimated_asset_count"], 1)
            self.assertEqual(dry_body["estimated_bytes"], 204800)

            mismatch_confirm = client.post(
                "/api/v1/archive-cleanup/jobs",
                headers=headers,
                json={"dry_run_id": dry_body["id"], "expected_filter_hash": "f" * 64},
            )
            self.assertEqual(mismatch_confirm.status_code, 409)

            confirmed = client.post(
                "/api/v1/archive-cleanup/jobs",
                headers=headers,
                json={
                    "dry_run_id": dry_body["id"],
                    "expected_filter_hash": dry_body["filter_hash"],
                },
            )
            self.assertEqual(confirmed.status_code, 201)
            self.assertEqual(confirmed.json()["status"], "pending")

            # Section 37 multidimensional message filters & audit cursor pagination
            filtered_effective = client.get(
                "/api/v1/messages",
                params={
                    "source_channel_id": src["id"],
                    "review_status": "reviewed",
                    "duplicate_status": "unique",
                    "label_key": "masterpiece",
                    "label_namespace": "effective",
                    "label_scope": "global",
                    "routing_policy_version_id": rp_ver["id"],
                    "destination_channel_id": dest_b["id"],
                    "archive_state": "ready",
                    "query": "oil painting",
                },
            ).json()
            self.assertEqual([item["id"] for item in filtered_effective["items"]], [msg2_id])

            filtered_model = client.get(
                "/api/v1/messages",
                params={"label_key": "masterpiece", "label_namespace": "model"},
            ).json()
            self.assertEqual(filtered_model["items"], [])

            audit_page1 = client.get("/api/v1/audit-events", params={"limit": 3}).json()
            self.assertEqual(len(audit_page1["items"]), 3)
            self.assertTrue(audit_page1["has_more"])
            audit_page2 = client.get(
                "/api/v1/audit-events",
                params={"limit": 3, "cursor": audit_page1["next_cursor"]},
            ).json()
            self.assertGreaterEqual(len(audit_page2["items"]), 1)

            # Dashboard summary & Prometheus /metrics exposition
            dash = client.get("/api/v1/dashboard/summary")
            self.assertEqual(dash.status_code, 200)
            self.assertEqual(dash.json()["messages"]["total"], 2)
            self.assertEqual(dash.json()["archive"]["total_archive_bytes"], 204800)

            metrics_resp = client.get("/metrics")
            self.assertEqual(metrics_resp.status_code, 200)
            self.assertIn("text/plain; version=0.0.4", metrics_resp.headers["content-type"])
            metrics_text = metrics_resp.text
            self.assertIn("tgcurator_messages_total", metrics_text)
            self.assertIn("tgcurator_archive_bytes_total 204800", metrics_text)
            self.assertIn("tgcurator_publication_events_total", metrics_text)
            self.assertIn("tgcurator_telegram_flood_wait_events_total", metrics_text)
            self.assertIn("tgcurator_worker_backlog_items", metrics_text)
            self.assertIn("tgcurator_service_ready", metrics_text)


if __name__ == "__main__":
    unittest.main()
