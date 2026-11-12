"""Real M8 repository regressions on migration-created isolated PostgreSQL.

Opt in with TGCURATOR_TEST_SERVICES_FILE or TGCURATOR_TEST_POSTGRES_ADMIN_URL.
No InMemory backend, SQLite schema adaptation, or weakened database triggers.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from postgres_support import temporary_postgres_database
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tgcurator.application.admin.errors import AdminConflictError, AdminValidationError
from tgcurator.infrastructure.database import models as m
from tgcurator.infrastructure.database.admin_api_repository import SqlAlchemyAdminBackend
from tgcurator.infrastructure.database.analysis_repository import (
    SqlAlchemyAnalysisStageRunRepository,
)
from tgcurator.infrastructure.database.publication_repository import SqlAlchemyPublicationRepository
from tgcurator.infrastructure.database.session import AsyncDatabase

NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)


class M8PostgresRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database_context = temporary_postgres_database("m8", revision="a8b2d4e1f305")
        url = self.database_context.__enter__()
        self.addCleanup(self.database_context.__exit__, None, None, None)
        self.db = AsyncDatabase(url)

    async def asyncSetUp(self):
        self.addAsyncCleanup(self.db.dispose)
        self.backend = SqlAlchemyAdminBackend(self.db)
        self.secret, self.identity, self.source, self.dest, self.admin = (uuid4() for _ in range(5))
        async with self.db.session() as session, session.begin():
            session.add(
                m.EncryptedSecret(
                    id=self.secret,
                    secret_type="api_key",
                    ciphertext=b"x" * 16,
                    nonce=b"n" * 12,
                    key_id="test",
                )
            )
            session.add(
                m.AdminUser(id=self.admin, username="review", password_hash="not-authenticated")
            )
            await session.flush()
            session.add(
                m.TelegramIdentity(
                    id=self.identity,
                    name="review",
                    identity_type="mtproto_user",
                    secret_id=self.secret,
                )
            )
            await session.flush()
            session.add(
                m.SourceChannel(
                    id=self.source,
                    telegram_peer_id=-1001,
                    display_name="source",
                    default_read_identity_id=self.identity,
                )
            )
            session.add(
                m.DestinationChannel(
                    id=self.dest,
                    name="dest",
                    telegram_peer_id=-1002,
                    default_publish_identity_id=self.identity,
                )
            )

    async def create(self, family, content, *, publish=False):
        entity = await self.backend.create_config_entity(
            family=family,
            payload={"name": family + "-" + uuid4().hex, "initial_version": content},
            now=NOW,
        )
        version = entity["versions"][0]
        if publish:
            version = await self.backend.publish_config_version(
                family=family,
                entity_id=entity["id"],
                version_id=version["id"],
                supersede_published=False,
                now=NOW,
            )
        return entity, version

    async def stack(self, *, two_nodes=False):
        self.label_ent, self.label = await self.create(
            "label-definitions",
            {"key": "sample", "display_name": "Sample", "scope": "global"},
            publish=True,
        )
        self.prompt_ent, self.prompt = await self.create(
            "prompt-templates",
            {
                "system_prompt": "Analyze input",
                "user_prompt_template": "Analyze the input",
                "declared_variables": [],
            },
            publish=True,
        )
        self.profile_ent, self.profile = await self.create(
            "inference-profiles",
            {
                "base_url": "https://example.invalid/v1",
                "model_name": "test",
                "api_secret_reference": str(self.secret),
            },
            publish=True,
        )
        self.labels_ent, self.labels = await self.create(
            "label-sets",
            {
                "description": "original",
                "bindings": [
                    {"label_definition_version_id": self.label["id"], "activation_threshold": 0.5}
                ],
            },
            publish=True,
        )
        self.stage_ent, self.stage = await self.create(
            "analysis-stages",
            {
                "name": "first",
                "prompt_template_version_id": self.prompt["id"],
                "label_set_version_id": self.labels["id"],
                "inference_profile_version_id": self.profile["id"],
                "input_policy": {"text": True},
            },
            publish=True,
        )
        nodes = [
            {
                "node_key": "root",
                "analysis_stage_template_version_id": self.stage["id"],
                "depends_on": [],
            }
        ]
        if two_nodes:
            nodes.append(
                {
                    "node_key": "child",
                    "analysis_stage_template_version_id": self.stage["id"],
                    "depends_on": ["root"],
                }
            )
        self.pipeline_ent, self.pipeline = await self.create(
            "analysis-pipelines", {"description": "original", "nodes": nodes}, publish=True
        )

    async def message(self, status="processed"):
        mid = uuid4()
        async with self.db.session() as session, session.begin():
            session.add(
                m.MessageRecord(
                    id=mid,
                    source_channel_id=self.source,
                    telegram_message_ids=[100],
                    primary_telegram_message_id=100,
                    original_text="review",
                    published_at=NOW,
                    processing_status=status,
                )
            )
        return mid

    async def test_six_version_families_create_clone_read_publish_archive(self):
        await self.stack()
        pairs = [
            ("label-definitions", self.label_ent, self.label),
            ("prompt-templates", self.prompt_ent, self.prompt),
            ("inference-profiles", self.profile_ent, self.profile),
            ("label-sets", self.labels_ent, self.labels),
            ("analysis-stages", self.stage_ent, self.stage),
            ("analysis-pipelines", self.pipeline_ent, self.pipeline),
        ]
        for family, entity, published in reversed(pairs):
            with self.subTest(family=family):
                clone = await self.backend.create_config_version(
                    family=family, entity_id=entity["id"], content=published["content"], now=NOW
                )
                self.assertEqual(clone["version_number"], 2)
                self.assertEqual(clone["content"], published["content"])
                result = await self.backend.publish_config_version(
                    family=family,
                    entity_id=entity["id"],
                    version_id=clone["id"],
                    supersede_published=True,
                    now=NOW,
                )
                self.assertEqual(result["state"], "published")
                archived = await self.backend.archive_config_version(
                    family=family, entity_id=entity["id"], version_id=clone["id"], now=NOW
                )
                self.assertEqual(archived["state"], "archived")
                loaded = await self.backend.get_config_entity(family=family, entity_id=entity["id"])
                self.assertEqual({v["state"] for v in loaded["versions"]}, {"archived"})

    async def test_five_draft_updates_replace_children_and_rollback_invalid_changes(self):
        await self.stack()
        _, route = await self.create(
            "routing-policies",
            {
                "description": "old",
                "rules": [
                    {
                        "priority": 1,
                        "condition": {
                            "label": {"namespace": "effective", "scope": "global", "key": "sample"}
                        },
                        "actions": [
                            {
                                "destination_channel_id": str(self.dest),
                                "publication_mode": "forward_only",
                            }
                        ],
                    }
                ],
            },
        )
        pairs = [
            ("inference-profiles", self.profile),
            ("label-sets", self.labels),
            ("analysis-stages", self.stage),
            ("analysis-pipelines", self.pipeline),
            ("routing-policies", route),
        ]
        for family, original in pairs:
            draft = await self.backend.create_config_version(
                family=family, entity_id=original["entity_id"], content=original["content"], now=NOW
            )
            content = json.loads(json.dumps(draft["content"]))
            if family == "inference-profiles":
                content.update(
                    model_name="changed",
                    max_concurrency=4,
                    sampling_parameters={"temperature": 0.2},
                )
            elif family == "analysis-stages":
                content.update(name="changed", timeout_seconds=12, retry_policy={"max_attempts": 2})
            elif family == "label-sets":
                content.update(description="changed")
                content["bindings"][0].update(prompt_hint="changed", activation_threshold=0.8)
            elif family == "analysis-pipelines":
                content.update(description="changed")
                content["nodes"][0]["node_key"] = "replacement"
            else:
                content.update(description="changed")
                content["rules"][0].update(priority=20, stop_on_match=True)
                content["rules"][0]["actions"][0]["protected_content_fallback"] = "skip"
            updated = await self.backend.update_config_version(
                family=family,
                entity_id=draft["entity_id"],
                version_id=draft["id"],
                content=content,
                now=NOW + timedelta(seconds=1),
            )
            loaded = await self.backend.get_config_entity(
                family=family, entity_id=draft["entity_id"]
            )
            persisted = next(v for v in loaded["versions"] if v["id"] == draft["id"])
            self.assertEqual(persisted["content"], updated["content"])
            self.assertNotEqual(updated["content"], original["content"])
            if family == "routing-policies":
                self.assertEqual(len(updated["content"]["rules"]), 1)
                self.assertEqual(updated["content"]["rules"][0]["priority"], 20)
                self.assertEqual(
                    updated["content"]["rules"][0]["actions"][0]["protected_content_fallback"],
                    "skip",
                )
            elif family == "analysis-pipelines":
                self.assertEqual(
                    [n["node_key"] for n in updated["content"]["nodes"]], ["replacement"]
                )
            elif family == "label-sets":
                self.assertEqual(updated["content"]["bindings"][0]["activation_threshold"], 0.8)
            bad = dict(content)
            if family == "label-sets":
                bad["bindings"] = [{"label_definition_version_id": str(uuid4())}]
                with self.assertRaises(AdminValidationError):
                    await self.backend.update_config_version(
                        family=family,
                        entity_id=draft["entity_id"],
                        version_id=draft["id"],
                        content=bad,
                        now=NOW,
                    )
                after = await self.backend.get_config_entity(
                    family=family, entity_id=draft["entity_id"]
                )
                self.assertEqual(
                    next(v for v in after["versions"] if v["id"] == draft["id"])["content"],
                    persisted["content"],
                )

                # Exercise rollback after DELETE + main-row UPDATE, not just validation.
                bad["description"] = "must roll back"
                bad["bindings"] = [content["bindings"][0], content["bindings"][0]]
                with self.assertRaises(IntegrityError):
                    async with self.db.session() as session, session.begin():
                        version = await session.get(m.LabelSetVersionRecord, UUID(draft["id"]))
                        await self.backend._apply_db_version_update(
                            session, family, version, bad, NOW
                        )
                after = await self.backend.get_config_entity(
                    family=family, entity_id=draft["entity_id"]
                )
                self.assertEqual(
                    next(v for v in after["versions"] if v["id"] == draft["id"])["content"],
                    persisted["content"],
                )

    async def finish_stage(self, stage_id):
        """Persist the inference boundary output through real mapped constraints."""
        async with self.db.session() as session, session.begin():
            stage = await session.get(m.StageRunRecord, UUID(str(stage_id)))
            manifest_id = uuid4()
            session.add(
                m.InputManifestRecord(
                    id=manifest_id,
                    analysis_stage_template_version_id=stage.analysis_stage_template_version_id,
                    manifest_version=1,
                    content={},
                    input_manifest_hash="a" * 64,
                )
            )
            await session.flush()
            stage.input_manifest_id = manifest_id
            stage.parsed_result = {"scores": {"sample": 1.0}}
            stage.result_origin = "inference"
            stage.status = "succeeded"
            stage.started_at = NOW
            stage.completed_at = NOW
            stage.lease_token = None
            stage.lease_expires_at = None

    async def test_reanalysis_root_child_progresses_without_formal_or_publication_mutation(self):
        await self.stack(two_nodes=True)
        mid = await self.message()
        old = uuid4()
        async with self.db.session() as session, session.begin():
            session.add(
                m.AnalysisRunRecord(
                    id=old,
                    message_id=mid,
                    analysis_pipeline_version_id=UUID(self.pipeline["id"]),
                    run_mode="formal",
                    status="completed",
                    started_at=NOW,
                    completed_at=NOW,
                    facts_snapshot={"history": True},
                )
            )
        async with self.db.session() as session:
            original_formal_facts = dict(
                (await session.get(m.AnalysisRunRecord, old)).facts_snapshot
            )
        response = await self.backend.reanalyze_message(
            message_id=str(mid),
            pipeline_version_id=self.pipeline["id"],
            reason="regression",
            now=NOW,
        )
        self.assertEqual(len(response["stage_run_ids"]), 1)
        repo = SqlAlchemyAnalysisStageRunRepository(self.db)
        root = response["stage_run_ids"][0]
        self.assertIsNotNone(
            await repo.claim(stage_run_id=root, now=NOW, lease_duration=timedelta(minutes=1))
        )
        await self.finish_stage(root)
        await repo.complete_analysis_run(analysis_run_id=response["analysis_run_id"], now=NOW)
        async with self.db.session() as session:
            run = await session.get(m.AnalysisRunRecord, UUID(response["analysis_run_id"]))
            self.assertEqual(run.status, "running")
            stages = list(
                await session.scalars(
                    select(m.StageRunRecord).where(m.StageRunRecord.analysis_run_id == run.id)
                )
            )
            self.assertEqual(len(stages), 2)
            child = next(s for s in stages if str(s.id) != root)
            self.assertEqual(child.status, "pending")
            wake = await session.scalar(
                select(m.DurableWakeupRecord).where(m.DurableWakeupRecord.entity_id == child.id)
            )
            self.assertEqual(wake.queue, "analysis")
        await self.finish_stage(child.id)
        await repo.complete_analysis_run(analysis_run_id=response["analysis_run_id"], now=NOW)
        await repo.complete_analysis_run(analysis_run_id=response["analysis_run_id"], now=NOW)
        async with self.db.session() as session:
            self.assertEqual(
                (await session.get(m.AnalysisRunRecord, UUID(response["analysis_run_id"]))).status,
                "completed",
            )
            self.assertEqual(
                (await session.get(m.AnalysisRunRecord, old)).facts_snapshot, original_formal_facts
            )
            self.assertEqual(
                (await session.get(m.MessageRecord, mid)).processing_status, "processed"
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(m.StageRunRecord)), 2
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(m.PublicationIntentRecord)), 0
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(m.RoutingEvaluationRecord)), 0
            )

    async def test_failed_stage_retry_restores_parent_and_claim_without_rewinding_success(self):
        await self.stack(two_nodes=True)
        mid = await self.message("failed")
        response = await self.backend.reanalyze_message(
            message_id=str(mid), pipeline_version_id=self.pipeline["id"], reason=None, now=NOW
        )
        root = response["stage_run_ids"][0]
        await self.finish_stage(root)
        repo = SqlAlchemyAnalysisStageRunRepository(self.db)
        await repo.complete_analysis_run(analysis_run_id=response["analysis_run_id"], now=NOW)
        async with self.db.session() as session, session.begin():
            run = await session.get(m.AnalysisRunRecord, UUID(response["analysis_run_id"]))
            # Formal retry must also restore message processing, unlike explicit reanalysis.
            run.run_mode = "formal"
            run.status = "failed"
            run.completed_at = NOW
            run.last_error_code = "TEST_FAILURE"
            run.last_error_type = "Timeout"
            child = await session.scalar(
                select(m.StageRunRecord).where(
                    m.StageRunRecord.analysis_run_id == run.id, m.StageRunRecord.id != UUID(root)
                )
            )
            child.status = "failed"
            child.attempt_count = child.max_attempts
            child.completed_at = NOW
            child.last_error_code = "TEST_FAILURE"
            child.last_error_type = "Timeout"
            child.retryable = True
            child.last_failure_at = NOW
            child_id = str(child.id)
        await self.backend.retry_step(
            step_kind="stage_run",
            target_id=child_id,
            reason="retry",
            actor_admin_user_id=str(self.admin),
            now=NOW,
        )
        async with self.db.session() as session:
            run = await session.get(m.AnalysisRunRecord, UUID(response["analysis_run_id"]))
            self.assertEqual(run.status, "running")
            self.assertIsNone(run.completed_at)
            self.assertEqual(
                (await session.get(m.MessageRecord, mid)).processing_status, "processing"
            )
            self.assertEqual((await session.get(m.StageRunRecord, UUID(root))).status, "succeeded")
        claim = await repo.claim(
            stage_run_id=child_id, now=NOW, lease_duration=timedelta(minutes=1)
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.attempt_count, 1)
        with self.assertRaises(AdminConflictError):
            await self.backend.retry_step(
                step_kind="stage_run",
                target_id=root,
                reason="invalid",
                actor_admin_user_id=str(self.admin),
                now=NOW,
            )

    async def test_latest_manual_set_clear_and_effective_filters_execute_real_sql(self):
        await self.stack()
        mid = await self.message()
        response = await self.backend.reanalyze_message(
            message_id=str(mid), pipeline_version_id=self.pipeline["id"], reason=None, now=NOW
        )
        sid = UUID(response["stage_run_ids"][0])
        await self.finish_stage(sid)
        async with self.db.session() as session, session.begin():
            session.add(
                m.ModelLabelAssignmentRecord(
                    id=uuid4(),
                    stage_run_id=sid,
                    analysis_run_id=UUID(response["analysis_run_id"]),
                    message_id=mid,
                    target_scope="global",
                    target_kind="message",
                    target_id=mid,
                    label_definition_version_id=UUID(self.label["id"]),
                    label_key="sample",
                    score=1.0,
                    activated=True,
                    negative=False,
                    evidence=[],
                    created_at=NOW,
                )
            )

        repo = SqlAlchemyAnalysisStageRunRepository(self.db)
        await repo.complete_analysis_run(analysis_run_id=response["analysis_run_id"], now=NOW)

        async def matches(namespace, **extra):
            page = await self.backend.list_messages(
                filters={
                    "label_key": "sample",
                    "label_namespace": namespace,
                    "label_scope": "global",
                    **extra,
                },
                cursor=None,
                limit=20,
            )
            return [item["id"] for item in page.items]

        self.assertEqual(await matches("effective"), [str(mid)])
        # Equal timestamps are deliberately resolved by event UUID, not insertion order.
        for sequence, operation, activated, expected_effective, expected_manual in [
            (1, "set", True, True, True),
            (2, "set", False, False, False),
            (3, "clear", None, True, False),
            (4, "set", True, True, True),
        ]:
            async with self.db.session() as session, session.begin():
                session.add(
                    m.ManualLabelAssignmentRecord(
                        id=UUID(int=sequence),
                        message_id=mid,
                        target_scope="global",
                        target_kind="message",
                        target_id=mid,
                        label_definition_version_id=UUID(self.label["id"]),
                        label_key="sample",
                        operation=operation,
                        activated=activated,
                        score=None if activated is None else float(activated),
                        actor_admin_user_id=self.admin,
                        created_at=NOW,
                    )
                )
            self.assertEqual(bool(await matches("manual")), expected_manual)
            self.assertEqual(bool(await matches("effective")), expected_effective)
            self.assertEqual(await matches("model"), [str(mid)])
        combined = dict(
            source_channel_id=str(self.source),
            pipeline_version_id=self.pipeline["id"],
            negative_label=False,
            published_from=(NOW - timedelta(days=1)).isoformat(),
            published_before=(NOW + timedelta(days=1)).isoformat(),
            processing_status="processed",
            review_status="unreviewed",
            duplicate_status="unchecked",
            blocked_from_analysis=False,
            query="review",
        )
        self.assertEqual(await matches("effective", **combined), [str(mid)])
        for filters in (
            {"negative_label": True},
            {"pipeline_version_id": str(uuid4())},
            {"routing_policy_version_id": str(uuid4())},
            {"destination_channel_id": str(self.dest)},
            {"archive_state": "ready"},
        ):
            self.assertEqual(
                (await self.backend.list_messages(filters=filters, cursor=None, limit=20)).items, ()
            )
        # A later model false must replace historical true, once CLEAR removes the override.
        response2 = await self.backend.reanalyze_message(
            message_id=str(mid),
            pipeline_version_id=self.pipeline["id"],
            reason=None,
            now=NOW + timedelta(seconds=1),
        )
        sid2 = UUID(response2["stage_run_ids"][0])
        await self.finish_stage(sid2)
        async with self.db.session() as session, session.begin():
            session.add(
                m.ModelLabelAssignmentRecord(
                    id=uuid4(),
                    stage_run_id=sid2,
                    analysis_run_id=UUID(response2["analysis_run_id"]),
                    message_id=mid,
                    target_scope="global",
                    target_kind="message",
                    target_id=mid,
                    label_definition_version_id=UUID(self.label["id"]),
                    label_key="sample",
                    score=0.0,
                    activated=False,
                    negative=False,
                    evidence=[],
                    created_at=NOW + timedelta(seconds=1),
                )
            )
            session.add(
                m.ManualLabelAssignmentRecord(
                    id=UUID(int=5),
                    message_id=mid,
                    target_scope="global",
                    target_kind="message",
                    target_id=mid,
                    label_definition_version_id=UUID(self.label["id"]),
                    label_key="sample",
                    operation="clear",
                    activated=None,
                    score=None,
                    actor_admin_user_id=self.admin,
                    created_at=NOW,
                )
            )
        # A newer RUNNING reanalysis cannot replace settled facts, even with output rows.
        self.assertEqual(await matches("model"), [str(mid)])
        self.assertEqual(await matches("effective"), [str(mid)])
        detail = await self.backend.get_message_detail(message_id=str(mid))
        self.assertTrue(
            detail["labels"]["namespaces"]["effective"]["global"]["sample"]["activated"]
        )
        self.assertEqual(len(detail["labels"]["model"]), 2)
        await repo.complete_analysis_run(
            analysis_run_id=response2["analysis_run_id"], now=NOW + timedelta(seconds=1)
        )
        self.assertEqual(await matches("model"), [])
        self.assertEqual(await matches("effective"), [])
        detail = await self.backend.get_message_detail(message_id=str(mid))
        self.assertFalse(
            detail["labels"]["namespaces"]["effective"]["global"]["sample"]["activated"]
        )
        self.assertEqual(len(detail["labels"]["model"]), 2)

    async def test_reanalysis_media_stage_targets_each_ready_asset(self):
        await self.stack()
        _, label = await self.create(
            "label-definitions",
            {"key": "visual", "display_name": "Visual", "scope": "media"},
            publish=True,
        )
        _, labels = await self.create(
            "label-sets", {"bindings": [{"label_definition_version_id": label["id"]}]}, publish=True
        )
        _, profile = await self.create(
            "inference-profiles",
            {
                "base_url": "https://example.invalid/v1",
                "model_name": "vision",
                "api_secret_reference": str(self.secret),
                "capabilities": ["text", "vision"],
            },
            publish=True,
        )
        _, stage = await self.create(
            "analysis-stages",
            {
                "name": "visual",
                "target_scope": "media",
                "execution_mode": "per_asset",
                "prompt_template_version_id": self.prompt["id"],
                "label_set_version_id": labels["id"],
                "inference_profile_version_id": profile["id"],
                "input_policy": {"text": True, "media": True},
            },
            publish=True,
        )
        _, pipeline = await self.create(
            "analysis-pipelines",
            {
                "nodes": [
                    {
                        "node_key": "media",
                        "analysis_stage_template_version_id": stage["id"],
                        "depends_on": [],
                    }
                ]
            },
            publish=True,
        )
        mid = await self.message()
        assets = [uuid4(), uuid4()]
        async with self.db.session() as session, session.begin():
            for idx, asset_id in enumerate(assets):
                session.add(
                    m.ImageAssetRecord(
                        id=asset_id,
                        message_id=mid,
                        source_asset_id=f"image{idx}",
                        archive_state="ready",
                        storage_backend="local",
                        storage_key=f"image{idx}.webp",
                        content_type="image/webp",
                        width=10,
                        height=10,
                        source_sha256="a" * 64,
                        archive_sha256="b" * 64,
                        perceptual_hash="0" * 16,
                        archive_size_bytes=10,
                        archive_ready_at=NOW,
                    )
                )
        result = await self.backend.reanalyze_message(
            message_id=str(mid), pipeline_version_id=pipeline["id"], reason=None, now=NOW
        )
        self.assertEqual(len(result["stage_run_ids"]), 2)
        async with self.db.session() as session:
            stages = list(
                await session.scalars(
                    select(m.StageRunRecord).where(
                        m.StageRunRecord.analysis_run_id == UUID(result["analysis_run_id"])
                    )
                )
            )
            self.assertEqual({s.target_id for s in stages}, set(assets))
            self.assertEqual({s.image_asset_id for s in stages}, set(assets))
            self.assertEqual(
                {(s.target_scope, s.target_kind) for s in stages}, {("media", "image_asset")}
            )
        repo = SqlAlchemyAnalysisStageRunRepository(self.db)
        await self.finish_stage(stages[0].id)
        await repo.complete_analysis_run(analysis_run_id=result["analysis_run_id"], now=NOW)
        async with self.db.session() as session:
            self.assertEqual(
                (await session.get(m.AnalysisRunRecord, UUID(result["analysis_run_id"]))).status,
                "running",
            )
        await self.finish_stage(stages[1].id)
        await repo.complete_analysis_run(analysis_run_id=result["analysis_run_id"], now=NOW)
        async with self.db.session() as session:
            self.assertEqual(
                (await session.get(m.AnalysisRunRecord, UUID(result["analysis_run_id"]))).status,
                "completed",
            )

    async def test_publication_retry_retains_history_and_resets_only_budget(self):
        mid = await self.message()
        _, policy = await self.create(
            "routing-policies",
            {
                "rules": [
                    {
                        "priority": 1,
                        "condition": {
                            "label": {"namespace": "effective", "scope": "global", "key": "sample"}
                        },
                        "actions": [
                            {
                                "destination_channel_id": str(self.dest),
                                "publication_mode": "forward_only",
                            }
                        ],
                    }
                ]
            },
            publish=True,
        )
        rule = policy["content"]["rules"][0]
        eid, intent_id = uuid4(), uuid4()
        async with self.db.session() as session, session.begin():
            session.add(
                m.RoutingEvaluationRecord(
                    id=eid,
                    routing_request_id="retry-test",
                    message_id=mid,
                    routing_policy_version_id=UUID(policy["id"]),
                    facts_snapshot={},
                    facts_hash="a" * 64,
                    rule_outcomes=[],
                )
            )
            await session.flush()
            session.add(
                m.PublicationIntentRecord(
                    id=intent_id,
                    routing_evaluation_id=eid,
                    routing_request_id="retry-test",
                    message_id=mid,
                    routing_policy_version_id=UUID(policy["id"]),
                    routing_rule_id=UUID(rule["id"]),
                    routing_action_id=UUID(rule["actions"][0]["id"]),
                    destination_channel_id=self.dest,
                    publish_identity_id=self.identity,
                    publication_mode="forward_only",
                    business_idempotency_key="retry-test",
                    max_attempts=2,
                )
            )
        repo = SqlAlchemyPublicationRepository(self.db)
        now = NOW
        for cycle in range(2):
            for budget in (1, 2):
                claim = await repo.claim(
                    intent_id=str(intent_id), now=now, lease_duration=timedelta(minutes=1)
                )
                self.assertIsNotNone(claim)
                self.assertEqual(claim.attempt_number, budget)
                await repo.retry_or_fail(
                    claim=claim,
                    error_code="TEST_TIMEOUT",
                    error_type="Timeout",
                    retryable=True,
                    retry_at=now + timedelta(seconds=1),
                    now=now,
                )
                now += timedelta(seconds=2)
            async with self.db.session() as session:
                row = await session.get(m.PublicationIntentRecord, intent_id)
                self.assertEqual(
                    (row.status, row.attempt_count, row.max_attempts), ("failed", 2, 2)
                )
            if cycle == 0:
                await self.backend.retry_step(
                    step_kind="publication_intent",
                    target_id=str(intent_id),
                    reason="operator retry",
                    actor_admin_user_id=str(self.admin),
                    now=now,
                )
        async with self.db.session() as session:
            attempts = list(
                await session.scalars(
                    select(m.PublishAttemptRecord)
                    .where(m.PublishAttemptRecord.publication_intent_id == intent_id)
                    .order_by(m.PublishAttemptRecord.attempt_number)
                )
            )
            self.assertEqual([a.attempt_number for a in attempts], [1, 2, 3, 4])
            self.assertEqual(
                [a.status for a in attempts], ["retry_wait", "failed", "retry_wait", "failed"]
            )
