"""Persisted Telegram identity/source revocation must invalidate queued subscriptions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from postgres_support import temporary_postgres_database
from runtime_fixtures import ExternalTelegram, runtime_settings
from sqlalchemy import select

from apps.runtime_dependencies import configured_secret_vault
from apps.telegram_ingestor.runtime import create_telegram_ingestor_runtime
from tgcurator.infrastructure.database import models as m
from tgcurator.infrastructure.telegram import configured_runtime


def test_disabled_identity_disconnects_and_rejects_already_queued_update(tmp_path, monkeypatch):
    import json
    from uuid import uuid4

    peer = ExternalTelegram()
    first = peer.add_message(1)
    first.photo = first.media = None
    monkeypatch.setattr(configured_runtime, "_default_client_factory", peer.factory)
    with temporary_postgres_database("runtime_revoke") as url, asyncio.Runner() as runner:
        settings = runtime_settings(url, "redis://127.0.0.1:1/15", tmp_path)
        runtime = create_telegram_ingestor_runtime(settings=settings)

        async def scenario():
            vault = configured_secret_vault(settings=settings, database=runtime.database)
            secret = await vault.store(
                secret_type="telegram_session",
                plaintext=json.dumps(
                    {"api_id": 12345, "api_hash": "fixture", "session_string": "fixture"}
                ).encode(),
            )
            identity, source = uuid4(), uuid4()
            async with runtime.database.session() as session, session.begin():
                session.add(
                    m.TelegramIdentity(
                        id=identity,
                        name="revocable",
                        identity_type="mtproto_user",
                        secret_id=secret,
                    )
                )
                await session.flush()
                session.add(
                    m.SourceChannel(
                        id=source,
                        telegram_peer_id=-100111,
                        display_name="revocable",
                        default_read_identity_id=identity,
                    )
                )
            now = datetime.now(UTC)
            try:
                await runtime.poll_updates(now=now)
                old = peer.clients[0]
                callback = next(h for h, b in old.handlers if type(b).__name__ == "NewMessage")
                assert old.connected and runtime.health_snapshot().connected
                async with runtime.database.session() as session, session.begin():
                    (await session.get(m.TelegramIdentity, identity)).enabled = False
                new = peer.add_message(2, text="queued after disable")
                new.photo = new.media = None
                # Simulate a callback already queued before the polling snapshot refresh.
                await callback(SimpleNamespace(chat_id=-100111, message=new))
                async with runtime.database.session() as session:
                    ids = list(
                        await session.scalars(select(m.MessageRecord.primary_telegram_message_id))
                    )
                    assert ids == [1]
                pool = runtime.subscriptions.pool
                with pytest.raises(configured_runtime.TelegramSessionExpired):
                    await pool.resolve_client(identity_id=str(identity))
                assert not old.connected
                assert str(identity) not in pool._clients
                await runtime.poll_updates(now=now + timedelta(seconds=2))
                assert not old.handlers
                assert not runtime.health_snapshot().connected
            finally:
                await runtime.shutdown()

        runner.run(scenario())
        assert all(not client.connected and not client.handlers for client in peer.clients)
