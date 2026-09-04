from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from tgcurator.infrastructure.telegram import (
    StaticTelegramSourcePeerResolver,
    TelethonMessageMapper,
    TelethonReadGateway,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class TelethonReadGatewayTests(unittest.TestCase):
    def test_fetches_a_bounded_window_and_stops_once_messages_are_too_old(self) -> None:
        source_channel_id = str(uuid4())
        client = _Client(
            (
                _raw(id=8, date=NOW, photo=object()),
                _raw(id=7, date=NOW - timedelta(minutes=1), video=object()),
                _raw(id=6, date=NOW - timedelta(minutes=3)),
                _raw(id=5, date=NOW - timedelta(minutes=4)),
            )
        )
        gateway = _gateway(client=client, source_channel_id=source_channel_id)

        messages = asyncio.run(
            gateway.fetch_history(
                source_channel_id=source_channel_id,
                from_at=NOW - timedelta(minutes=3),
                to_at=NOW,
            )
        )

        self.assertEqual([message.telegram_message_id for message in messages], [6, 7])
        self.assertEqual([asset.kind.value for asset in messages[1].media], ["video"])
        self.assertEqual(client.calls, [(12345, NOW, None)])
        self.assertEqual(client.yielded_ids, [8, 7, 6, 5])

    def test_maps_photo_document_and_grouped_caption(self) -> None:
        source_channel_id = str(uuid4())
        mapper = TelethonMessageMapper()

        message = mapper.map(
            source_channel_id=source_channel_id,
            message=_raw(id=9, date=NOW, grouped_id=22, raw_text="caption", photo=object()),
        )

        self.assertEqual(message.grouped_id, 22)
        self.assertEqual(message.text, "caption")
        self.assertEqual(message.media[0].kind.value, "image")
        self.assertEqual(message.media[0].asset_id, f"telegram:{source_channel_id}:9:0")

    def test_newest_and_missing_peer_behavior(self) -> None:
        source_channel_id = str(uuid4())
        gateway = _gateway(
            client=_Client((_raw(id=2, date=NOW),)), source_channel_id=source_channel_id
        )

        self.assertEqual(
            asyncio.run(gateway.newest_message_at(source_channel_id=source_channel_id)), NOW
        )
        with self.assertRaises(DomainValidationError):
            asyncio.run(gateway.newest_message_at(source_channel_id=str(uuid4())))

    def test_rejects_invalid_window_and_naive_telethon_timestamp(self) -> None:
        source_channel_id = str(uuid4())
        gateway = _gateway(client=_Client(()), source_channel_id=source_channel_id)
        with self.assertRaises(DomainValidationError):
            asyncio.run(
                gateway.fetch_history(source_channel_id=source_channel_id, from_at=NOW, to_at=NOW)
            )
        with self.assertRaises(DomainValidationError):
            TelethonMessageMapper().map(
                source_channel_id=source_channel_id,
                message=_raw(id=1, date=datetime(2026, 9, 4, 12, 0)),
            )


def _gateway(*, client: _Client, source_channel_id: str) -> TelethonReadGateway:
    return TelethonReadGateway(
        client=client,
        source_peer_resolver=StaticTelegramSourcePeerResolver({source_channel_id: 12345}),
        message_mapper=TelethonMessageMapper(),
    )


def _raw(**fields: object) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "date": NOW,
        "grouped_id": None,
        "raw_text": None,
        "photo": None,
        "video": None,
        "audio": None,
        "document": None,
    }
    defaults.update(fields)
    return SimpleNamespace(**defaults)


class _Client:
    def __init__(self, messages: tuple[SimpleNamespace, ...]) -> None:
        self._messages = messages
        self.calls: list[tuple[int | str, datetime | None, int | None]] = []
        self.yielded_ids: list[int] = []

    async def iter_messages(self, entity, *, offset_date=None, limit=None):
        self.calls.append((entity, offset_date, limit))
        for message in self._messages:
            self.yielded_ids.append(message.id)
            yield message


if __name__ == "__main__":
    unittest.main()
