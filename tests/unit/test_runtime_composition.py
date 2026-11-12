"""Composition/lifecycle tests; the native PG/Redis suite covers actual I/O."""

from __future__ import annotations

import asyncio
import base64
import os
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from apps.worker.runtime import (
    _REGISTERED_EXTRACTORS,
    create_worker_runtime,
)
from tgcurator.application import Settings
from tgcurator.infrastructure.inference import OpenAICompatibleInferenceProvider
from tgcurator.infrastructure.telegram.configured_runtime import ConfiguredTelegramClientPool
from tgcurator.infrastructure.telegram.telethon_publisher import TelethonPublisher


def settings(tmp_path, *, vault=True):
    return Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://test@127.0.0.1/tgcurator_test_runtime_unit",
        app_master_key=SecretStr(base64.b64encode(os.urandom(32)).decode()) if vault else None,
        archive_root=tmp_path,
    )


def test_factory_composes_real_adapters_and_close_unregisters_extractors(tmp_path):
    runtime = create_worker_runtime(
        settings=settings(tmp_path), telegram_client_factory=lambda **k: None
    )
    assert runtime.range_execution_history_ingestion is not None
    assert runtime.image_archive_worker is not None
    assert runtime.video_archive_worker is not None
    from tgcurator.infrastructure.database.media_profile_repository import (
        SqlAlchemyMediaProfileResolver,
    )

    assert isinstance(runtime.image_archive_worker.profile_resolver, SqlAlchemyMediaProfileResolver)
    assert isinstance(runtime.video_archive_worker.profile_resolver, SqlAlchemyMediaProfileResolver)
    assert isinstance(runtime.telegram_client_pool, ConfiguredTelegramClientPool)
    assert isinstance(runtime.publication_worker.publisher, TelethonPublisher)
    assert isinstance(
        runtime.analysis_stage_run_worker.orchestrator._provider, OpenAICompatibleInferenceProvider
    )
    extractors = runtime.media_extractors
    assert len(extractors) == 1 and id(extractors[0]) in _REGISTERED_EXTRACTORS
    client = AsyncMock()
    runtime.telegram_client_pool._clients["external-fixture"] = client
    extractor_type = type(extractors[0])
    with patch.object(
        extractor_type,
        "terminate_active_processes",
        autospec=True,
        side_effect=extractor_type.terminate_active_processes,
    ) as terminate:
        asyncio.run(runtime.close())
        terminate.assert_called_once_with(extractors[0])
    client.disconnect.assert_awaited_once()
    assert id(extractors[0]) not in _REGISTERED_EXTRACTORS
    assert runtime.database._engine is None


def test_factory_without_vault_does_not_fake_telegram_readiness(tmp_path):
    runtime = create_worker_runtime(settings=settings(tmp_path, vault=False))
    assert runtime.range_execution_history_ingestion is None
    assert runtime.telegram_client_pool is None
    assert runtime.image_archive_worker is None
    assert runtime.publication_worker is None
    asyncio.run(runtime.close())


def test_close_disposes_database_even_if_pool_close_raises(tmp_path):
    runtime = create_worker_runtime(settings=settings(tmp_path))
    runtime.telegram_client_pool.close = AsyncMock(
        side_effect=RuntimeError("external close failed")
    )
    with patch.object(runtime.database, "dispose", new_callable=AsyncMock) as dispose:
        import pytest

        with pytest.raises(RuntimeError, match="external close failed"):
            asyncio.run(runtime.close())
        dispose.assert_awaited_once()
    assert all(id(item) not in _REGISTERED_EXTRACTORS for item in runtime.media_extractors)
