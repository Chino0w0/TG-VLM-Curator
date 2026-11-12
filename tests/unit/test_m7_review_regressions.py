"""PR28 production-query/coordinator regressions (no Telegram or production DB)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import wraps
from io import StringIO
from types import SimpleNamespace as NS
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from test_pipeline_coordinator import (
    NOW,
    FakeRoutingService,
    InMemoryPipelineRepository,
    make_message,
)

from tgcurator.application.pipeline.coordinator import MessagePipelineCoordinator
from tgcurator.application.ports.pipeline import ActiveRangeExecutionSnapshot
from tgcurator.domain.processing import SourceChannelProfileConfig
from tgcurator.infrastructure.database.models import (
    ImageAssetRecord,
    MessagePartRecord,
    MessageRecord,
)
from tgcurator.infrastructure.database.pipeline_repository import (
    SqlAlchemyMessagePipelineRepository,
    _find_canonical_visual_stage_source,
)


def run_async(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return run


def execution(*, lower=99, upper=101, routing=False):
    return ActiveRangeExecutionSnapshot(
        str(UUID(int=10)),
        str(UUID(int=11)),
        str(UUID(int=1)),
        str(UUID(int=12)),
        lower,
        upper,
        lower,
        "awaiting_pipeline",
        SourceChannelProfileConfig(
            routing_policy_version_id=str(UUID(int=20)) if routing else None
        ),
    )


class QueryDB:
    """Execute production filters on SQLite, not a PostgreSQL lock substitute."""

    def __init__(self, messages, images=(), parts=()):
        self.messages = {row.id: row for row in messages}
        self.images = {row.id: row for row in images}
        self.engine = create_engine("sqlite:///:memory:")
        with self.engine.begin() as c:
            c.execute(
                text(
                    "CREATE TABLE messages (id CHAR(32), visual_fingerprint VARCHAR, published_at "
                    "DATETIME, media_count INT, duplicate_status VARCHAR, source_channel_id "
                    "CHAR(32), primary_telegram_message_id INT, processing_status VARCHAR)"
                )
            )
            c.execute(text("CREATE TABLE image_assets (id CHAR(32), message_id CHAR(32))"))
            c.execute(
                text(
                    "CREATE TABLE message_parts (id CHAR(32), message_id CHAR(32), "
                    "source_channel_id CHAR(32), telegram_message_id INT)"
                )
            )
            c.execute(
                text(
                    "CREATE TABLE range_executions (id CHAR(32), processing_range_id CHAR(32), "
                    "status VARCHAR, from_message_id_exclusive INT, to_message_id_inclusive INT, "
                    "created_at DATETIME)"
                )
            )
            c.execute(
                text("CREATE TABLE processing_ranges (id CHAR(32), source_channel_id CHAR(32))")
            )
            for row in messages:
                c.execute(
                    text(
                        "INSERT INTO messages VALUES "
                        "(:id,:fp,:dt,:count,:status,:source,:tg,:processing)"
                    ),
                    dict(
                        id=row.id.hex,
                        fp=row.visual_fingerprint,
                        dt=row.published_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
                        count=row.media_count,
                        status=row.duplicate_status,
                        source=row.source_channel_id.hex,
                        tg=row.primary_telegram_message_id,
                        processing=row.processing_status,
                    ),
                )
            for row in images:
                c.execute(
                    text("INSERT INTO image_assets VALUES (:id,:mid)"),
                    dict(id=row.id.hex, mid=row.message_id.hex),
                )
            for index, (mid, tg) in enumerate(parts):
                c.execute(
                    text("INSERT INTO message_parts VALUES (:id,:mid,:source,:tg)"),
                    dict(id=UUID(int=900 + index).hex, mid=mid.hex, source=UUID(int=1).hex, tg=tg),
                )

    @asynccontextmanager
    async def session(self):
        yield self

    async def scalar(self, stmt):
        rows = await self.scalars(stmt)
        return rows[0] if rows else None

    async def scalars(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity in (MessageRecord, ImageAssetRecord):
            with self.engine.connect() as c:
                ids = list(c.execute(stmt.with_only_columns(entity.id)).scalars())
            lookup = self.messages if entity is MessageRecord else self.images
            return [lookup[i] for i in ids]
        if entity is MessagePartRecord:
            with self.engine.connect() as c:
                rows = c.execute(
                    stmt.with_only_columns(
                        MessagePartRecord.message_id, MessagePartRecord.telegram_message_id
                    )
                ).all()
            return [NS(message_id=mid, telegram_message_id=tg) for mid, tg in rows]
        if entity.__tablename__ == "range_executions":
            with self.engine.connect() as c:
                return list(c.execute(stmt).scalars())
        return []

    def update_dedup(self, mid, fp, status):
        self.messages[mid].visual_fingerprint = fp
        self.messages[mid].duplicate_status = status
        with self.engine.begin() as c:
            c.execute(
                text(
                    "UPDATE messages SET visual_fingerprint=:fp, duplicate_status=:status WHERE "
                    "id=:id"
                ),
                dict(id=mid.hex, fp=fp, status=status),
            )


def message_row(mid, tg, **overrides):
    values = dict(
        id=mid,
        source_channel_id=UUID(int=1),
        primary_telegram_message_id=tg,
        telegram_message_ids=[tg],
        published_at=NOW,
        visual_fingerprint=None,
        duplicate_status="unchecked",
        media_count=1,
        processing_status="ingested",
        blocked_from_analysis=False,
        terminal_reason=None,
        original_text=None,
    )
    values.update(overrides)
    return NS(**values)


@run_async
async def test_same_second_messages_have_one_canonical_root_not_a_cycle():
    ids = [UUID(int=101), UUID(int=102)]
    rows = [message_row(mid, 100 + i) for i, mid in enumerate(ids)]
    imgs = [
        NS(
            id=UUID(int=201 + i),
            message_id=mid,
            perceptual_hash="0123456789abcdef",
            source_phash=None,
        )
        for i, mid in enumerate(ids)
    ]
    db = QueryDB(rows, imgs)
    sqlrepo = SqlAlchemyMessagePipelineRepository(db)

    class Repo(InMemoryPipelineRepository):
        async def load_visual_duplicate_candidates(self, **kw):
            return await sqlrepo.load_visual_duplicate_candidates(**kw)

        async def record_visual_deduplication(self, **kw):
            await super().record_visual_deduplication(**kw)
            db.update_dedup(
                UUID(kw["message_id"]), kw["visual_fingerprint"], kw["match"].status.value
            )

    repo = Repo()
    for i, mid in enumerate(ids):
        repo.messages[str(mid)] = replace(
            make_message(
                str(mid),
                100 + i,
                media_states=(("image_asset", str(imgs[i].id), "ready"),),
                image_phashes=("0123456789abcdef",),
            ),
            published_at=NOW,
        )
    ex = execution()
    repo.executions[ex.execution_id] = ex
    await MessagePipelineCoordinator(repository=repo).drive_execution(
        execution_id=ex.execution_id, now=NOW
    )
    assert [m.duplicate_status for m in repo.messages.values()] == ["unique", "duplicate_skipped"]
    assert repo.messages[str(ids[1])].canonical_message_id == str(ids[0])


@run_async
async def test_album_component_in_next_window_is_loaded_and_receives_events():
    mid = UUID(int=301)
    row = message_row(mid, 100, telegram_message_ids=[100, 101], media_count=2)
    imgs = [
        NS(
            id=UUID(int=302),
            message_id=mid,
            archive_state="pending",
            perceptual_hash=None,
            source_phash=None,
        )
    ]
    db = QueryDB([row], imgs, [(mid, 100), (mid, 101)])
    ex = execution(lower=100)
    with db.engine.begin() as c:
        c.execute(
            text("INSERT INTO processing_ranges VALUES (:id,:source)"),
            dict(id=UUID(ex.processing_range_id).hex, source=UUID(ex.source_channel_id).hex),
        )
        c.execute(
            text("INSERT INTO range_executions VALUES (:id,:rid,'awaiting_pipeline',100,101,:dt)"),
            dict(
                id=UUID(ex.execution_id).hex,
                rid=UUID(ex.processing_range_id).hex,
                dt=NOW.strftime("%Y-%m-%d %H:%M:%S.%f"),
            ),
        )
    repo = SqlAlchemyMessagePipelineRepository(db)
    rows = await repo.load_window_messages(execution=ex)
    assert len(rows) == 1
    assert rows[0].primary_telegram_message_id == 101  # Window-local progress coordinate.
    assert rows[0].media.any_pending_archive
    assert await repo.find_awaiting_execution_ids_for_message(message_id=str(mid)) == (
        ex.execution_id,
    )


@run_async
async def test_processed_album_with_new_pending_media_is_not_terminal():
    repo = InMemoryPipelineRepository()
    ex = execution()
    repo.executions[ex.execution_id] = ex
    repo.messages["msg"] = replace(
        make_message("msg", 100, media_states=(("image_asset", "new", "pending"),)),
        processing_status="processed",
        terminal_reason="completed",
        duplicate_status="unique",
    )
    await MessagePipelineCoordinator(repository=repo).drive_execution(
        execution_id=ex.execution_id, now=NOW
    )
    assert repo.media_wakeups
    assert repo.executions[ex.execution_id].status == "awaiting_pipeline"


class RaceDB:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.created = []

    @asynccontextmanager
    async def session(self):
        session = RaceSession(self)
        try:
            yield session
        finally:
            if session.locked:
                self.lock.release()


class RaceSession:
    def __init__(self, db):
        self.db = db
        self.locked = False

    @asynccontextmanager
    async def begin(self):
        yield self

    async def scalar(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is MessageRecord:
            assert stmt._for_update_arg is not None
            await self.db.lock.acquire()
            self.locked = True
            return message_row(UUID(int=401), 100)
        existing = self.db.created[0] if self.db.created else None
        await asyncio.sleep(0)
        return existing

    def add(self, row):
        self.db.created.append(row)


@run_async
async def test_first_formal_run_serializes_on_existing_parent():
    db = RaceDB()
    repo = SqlAlchemyMessagePipelineRepository(db)
    args = dict(message_id=str(UUID(int=401)), pipeline_version_id=str(UUID(int=402)), now=NOW)
    ids = await asyncio.wait_for(
        asyncio.gather(
            repo.ensure_formal_analysis_run(**args), repo.ensure_formal_analysis_run(**args)
        ),
        timeout=2,
    )
    assert len(set(ids)) == 1
    assert len(db.created) == 1


@run_async
async def test_overlap_processed_message_can_settle_missing_routing():
    repo = InMemoryPipelineRepository()
    routing = FakeRoutingService(repository=repo)
    ex = execution(upper=100, routing=True)
    repo.executions[ex.execution_id] = ex
    repo.messages["msg"] = replace(
        make_message("msg", 100),
        processing_status="processed",
        terminal_reason="completed",
        duplicate_status="unique",
        formal_routing_evaluation_id=None,
    )
    c = MessagePipelineCoordinator(repository=repo, routing_service=routing)
    await c.drive_execution(execution_id=ex.execution_id, now=NOW)
    assert repo.executions[ex.execution_id].status == "completed"
    assert len(routing.requests) == 1
    assert "exec:" not in routing.requests[0].request_id


@run_async
@pytest.mark.parametrize(
    "policy",
    [{"previous_results": True}, {"source_channel_context": True}, {"text": False, "media": True}],
)
async def test_canonical_shortcut_without_current_manifest_must_fall_back(policy):
    class Session:
        async def scalar(self, stmt):
            return NS(input_policy=policy)

        async def scalars(self, stmt):
            return [NS(id=UUID(int=501), target_id=UUID(int=601))]

    donor = await _find_canonical_visual_stage_source(
        session=Session(),
        stage_version_id=UUID(int=701),
        canonical_message_id=UUID(int=702),
        target_kind="message",
        current_target_id=UUID(int=801),
    )
    assert donor is None


@run_async
async def test_multiple_videos_never_reuse_unproven_first_donor():
    class Session:
        async def scalar(self, stmt):
            return NS(input_policy={"text": False, "media": True})

        async def scalars(self, stmt):
            return [
                NS(id=UUID(int=501), target_id=UUID(int=601)),
                NS(id=UUID(int=502), target_id=UUID(int=602)),
            ]

    selected = [
        await _find_canonical_visual_stage_source(
            session=Session(),
            stage_version_id=UUID(int=701),
            canonical_message_id=UUID(int=702),
            target_kind="video_asset",
            current_target_id=UUID(int=n),
        )
        for n in (801, 802)
    ]
    assert selected == [None, None]


def test_m7_constraint_names_match_m6_and_downgrade():
    output = StringIO()
    # This offline SQL contract needs migrations, not the CLI's global logging setup.
    # Loading alembic.ini here disables existing worker loggers for later tests.
    cfg = Config(output_buffer=output)
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", "postgresql+asyncpg://test:test@localhost/test")
    command.upgrade(cfg, "e6d4a8b2c951:f1a7c3d9e204", sql=True)
    sql = output.getvalue()
    assert "DROP CONSTRAINT ck_range_executions_range_execution_status" in sql
    assert "ck_range_executions_ck_" not in sql
    assert "ck_messages_ck_" not in sql
    assert "CREATE UNIQUE INDEX uq_analysis_runs_formal_input" in sql
    output.seek(0)
    output.truncate(0)
    command.downgrade(cfg, "f1a7c3d9e204:e6d4a8b2c951", sql=True)
    assert "ck_range_executions_ck_" not in output.getvalue()


@run_async
async def test_partial_album_watermark_uses_unsettled_new_component_coordinate():
    mid = UUID(int=301)
    row = message_row(mid, 100, telegram_message_ids=[100, 101], media_count=2)
    db = QueryDB([row], parts=[(mid, 100), (mid, 101)])
    ex = replace(execution(lower=99), watermark_message_id=100)
    rows = await SqlAlchemyMessagePipelineRepository(db).load_window_messages(execution=ex)
    assert rows[0].primary_telegram_message_id == 101


@run_async
async def test_exact_canonical_candidates_exclude_unchecked_skipped_failed_and_later():
    rows = [
        message_row(UUID(int=101), 100, visual_fingerprint="a" * 64, duplicate_status="unique"),
        message_row(
            UUID(int=102), 101, visual_fingerprint="a" * 64, duplicate_status="duplicate_skipped"
        ),
        message_row(UUID(int=103), 102, visual_fingerprint="a" * 64),
        message_row(
            UUID(int=104),
            103,
            visual_fingerprint="a" * 64,
            duplicate_status="unique",
            processing_status="failed",
        ),
        message_row(UUID(int=600), 104, visual_fingerprint="a" * 64, duplicate_status="unique"),
    ]
    repo = SqlAlchemyMessagePipelineRepository(QueryDB(rows))
    candidates = await repo.load_visual_duplicate_candidates(
        message_id=str(UUID(int=500)),
        published_at=NOW,
        visual_fingerprint="a" * 64,
    )
    assert [c.message_id for c in candidates] == [str(UUID(int=101))]


@run_async
async def test_multi_video_reclassify_schedules_distinct_targets_with_cache_policy(monkeypatch):
    from tgcurator.domain.analysis import (
        CachePolicy,
        LabelScope,
        PipelineNodeActionKind,
        PlannedNodeTarget,
    )
    from tgcurator.infrastructure.database import pipeline_repository as module
    from tgcurator.infrastructure.database.models import AnalysisRunRecord, StageRunRecord

    rows = []
    run = NS(status="running", started_at=NOW)
    msg = NS(blocked_from_analysis=False)
    donor = NS(
        id=UUID(int=501),
        input_manifest_id=UUID(int=502),
        parsed_result={},
        cache_scope_identity="donor",
        semantic_cache_key="old",
    )

    class Session:
        @asynccontextmanager
        async def begin(self):
            yield self

        async def scalar(self, stmt):
            entity = stmt.column_descriptions[0]["entity"]
            return run if entity is AnalysisRunRecord else msg if entity is MessageRecord else None

        async def scalars(self, stmt):
            return []

        def add(self, row):
            rows.append(row)

        async def flush(self):
            pass

    class DB:
        @asynccontextmanager
        async def session(self):
            yield Session()

    async def unsafe_donor(**kwargs):
        return donor

    async def wakeup(**kwargs):
        return True

    monkeypatch.setattr(module, "_find_canonical_visual_stage_source", unsafe_donor)
    monkeypatch.setattr(module, "_upsert_pending_wakeup", wakeup)
    planned = tuple(
        PlannedNodeTarget(
            node_id=str(UUID(int=701)),
            node_key="video",
            stage_version_id=str(UUID(int=702)),
            target_scope=LabelScope.MEDIA,
            target_kind="video_asset",
            target_id=str(UUID(int=i)),
            cache_policy=CachePolicy.ASSET,
            max_attempts=3,
            action=PipelineNodeActionKind.SCHEDULE,
        )
        for i in (801, 802)
    )
    result = await SqlAlchemyMessagePipelineRepository(DB()).materialize_planned_targets(
        analysis_run_id=str(UUID(int=401)),
        message_id=str(UUID(int=402)),
        canonical_message_id=str(UUID(int=403)),
        duplicate_status="duplicate_reclassify",
        planned_targets=planned,
        run_status="running",
        now=NOW,
    )
    assert result == (2, 0, False)
    stages = [r for r in rows if isinstance(r, StageRunRecord)]
    assert [r.target_id for r in stages] == [UUID(int=801), UUID(int=802)]
    assert all(r.status == "pending" and r.cache_policy == "asset" for r in stages)
    assert all(r.input_manifest_id is None and r.reused_from_stage_run_id is None for r in stages)


@run_async
@pytest.mark.parametrize(
    "overrides,expected", [({"retry_policy": {"max_attempts": 7}}, 7), ({}, 2)]
)
async def test_planner_consumes_retry_override_without_changing_scope_or_cache(overrides, expected):
    from tgcurator.infrastructure.database.pipeline_repository import (
        _load_pipeline_nodes_in_session,
    )

    node = NS(
        id=UUID(int=901), node_key="n", depends_on=[], run_if=None, parameter_overrides=overrides
    )
    stage = NS(
        id=UUID(int=902),
        input_policy={"media": True},
        retry_policy={"max_attempts": 2, "base_delay_seconds": 10, "max_delay_seconds": 30},
        target_scope="media",
        cache_policy="asset",
    )

    class Session:
        async def execute(self, stmt):
            return [(node, stage)]

    specs = await _load_pipeline_nodes_in_session(session=Session(), pipeline_uuid=UUID(int=903))
    assert specs[0].max_attempts == expected
    assert specs[0].input_requires_media and specs[0].target_scope.value == "media"
    assert specs[0].cache_policy.value == "asset"


@run_async
@pytest.mark.parametrize("key", ["cache_policy", "input_policy", "target_scope"])
async def test_planner_rejects_overrides_outside_domain_allowlist(key):
    from tgcurator.infrastructure.database.pipeline_repository import (
        _load_pipeline_nodes_in_session,
    )
    from tgcurator.shared import DomainValidationError

    node = NS(
        id=UUID(int=901),
        node_key="n",
        depends_on=[],
        run_if=None,
        parameter_overrides={key: "wrong"},
    )
    stage = NS(
        id=UUID(int=902),
        input_policy={},
        retry_policy={},
        target_scope="global",
        cache_policy="none",
    )

    class Session:
        async def execute(self, stmt):
            return [(node, stage)]

    with pytest.raises(DomainValidationError, match="unsupported"):
        await _load_pipeline_nodes_in_session(session=Session(), pipeline_uuid=UUID(int=903))


@run_async
async def test_sending_publication_is_not_watermark_terminal():
    repo = InMemoryPipelineRepository()
    ex = execution(upper=100, routing=True)
    repo.executions[ex.execution_id] = ex
    repo.messages["msg"] = replace(
        make_message("msg", 100),
        processing_status="processed",
        terminal_reason="completed",
        duplicate_status="unique",
        formal_routing_evaluation_id="existing",
        publication_intent_statuses=("sending",),
    )
    await MessagePipelineCoordinator(repository=repo).drive_execution(
        execution_id=ex.execution_id,
        now=NOW,
    )
    assert repo.executions[ex.execution_id].status == "awaiting_pipeline"
    assert repo.executions[ex.execution_id].watermark_message_id == 99


@run_async
async def test_failed_publication_after_overlap_routes_message_to_failed_not_completed():
    repo = InMemoryPipelineRepository()
    ex = execution(upper=100, routing=True)
    repo.executions[ex.execution_id] = ex
    repo.messages["msg"] = replace(
        make_message("msg", 100),
        processing_status="processed",
        terminal_reason="completed",
        duplicate_status="unique",
        formal_routing_evaluation_id="existing",
        publication_intent_statuses=("failed",),
    )
    await MessagePipelineCoordinator(repository=repo).drive_execution(
        execution_id=ex.execution_id,
        now=NOW,
    )
    assert repo.messages["msg"].processing_status == "failed"
    assert repo.executions[ex.execution_id].status == "awaiting_pipeline"
    assert repo.executions[ex.execution_id].watermark_message_id == 99
