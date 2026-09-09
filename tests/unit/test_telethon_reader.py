from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from tgcurator.application.ports.processing import LatestMessageBoundary
from tgcurator.infrastructure.telegram import (
    StaticTelegramSourcePeerResolver,
    TelethonMessageMapper,
    TelethonReadGateway,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class TelethonReadGatewayTests(unittest.TestCase):
    def test_fetches_an_immutable_message_id_window_in_ascending_order(self) -> None:
        source_channel_id = str(uuid4())
        client = _Client(
            (
                _raw(id=8, date=NOW, photo=object()),
                _raw(id=6, date=NOW - timedelta(minutes=3)),
                _raw(id=7, date=NOW - timedelta(minutes=1), video=object()),
                _raw(id=5, date=NOW - timedelta(minutes=4)),
                _raw(id=9, date=NOW + timedelta(minutes=1)),
            )
        )
        gateway = _gateway(client=client, source_channel_id=source_channel_id)

        messages = asyncio.run(
            gateway.fetch_history(
                source_channel_id=source_channel_id,
                from_message_id_exclusive=5,
                to_message_id_inclusive=8,
            )
        )

        self.assertEqual([message.telegram_message_id for message in messages], [6, 7, 8])
        self.assertEqual([asset.kind.value for asset in messages[1].media], ["video"])
        self.assertEqual(client.calls, [(12345, 5, 9, True, None)])

    def test_maps_edit_caption_group_and_json_safe_metadata(self) -> None:
        source_channel_id = str(uuid4())
        mapper = TelethonMessageMapper()
        edited_at = NOW + timedelta(minutes=1)

        message = mapper.map(
            source_channel_id=source_channel_id,
            message=_raw(
                id=9,
                date=NOW,
                edit_date=edited_at,
                grouped_id=22,
                raw_text="caption",
                photo=object(),
                views=42,
                post=True,
            ),
        )

        self.assertEqual(message.telegram_grouped_id, 22)
        self.assertEqual(message.original_text, "caption")
        self.assertEqual(message.published_at, NOW)
        self.assertEqual(message.edited_at, edited_at)
        self.assertEqual(message.telegram_metadata["views"], 42)
        self.assertIs(message.telegram_metadata["post"], True)
        self.assertEqual(message.media[0].kind.value, "image")
        self.assertEqual(message.media[0].asset_id, f"telegram:{source_channel_id}:9:0")
        self.assertEqual(message.media[0].source_telegram_message_id, 9)

    def test_newest_boundary_and_missing_peer_behavior(self) -> None:
        source_channel_id = str(uuid4())
        gateway = _gateway(
            client=_Client((_raw(id=2, date=NOW),)), source_channel_id=source_channel_id
        )

        self.assertEqual(
            asyncio.run(gateway.newest_message(source_channel_id=source_channel_id)),
            LatestMessageBoundary(message_id=2, published_at=NOW),
        )
        with self.assertRaises(DomainValidationError):
            asyncio.run(gateway.newest_message(source_channel_id=str(uuid4())))

    def test_rejects_invalid_window_and_naive_telethon_timestamp(self) -> None:
        source_channel_id = str(uuid4())
        gateway = _gateway(client=_Client(()), source_channel_id=source_channel_id)
        invalid_windows = ((5, 5), (-1, 5), (0, 0), (True, 5))
        for left, right in invalid_windows:
            with self.subTest(left=left, right=right), self.assertRaises(DomainValidationError):
                asyncio.run(
                    gateway.fetch_history(
                        source_channel_id=source_channel_id,
                        from_message_id_exclusive=left,
                        to_message_id_inclusive=right,
                    )
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
        "edit_date": None,
        "grouped_id": None,
        "raw_text": None,
        "photo": None,
        "video": None,
        "audio": None,
        "document": None,
        "post": None,
        "silent": None,
        "legacy": None,
        "edit_hide": None,
        "pinned": None,
        "noforwards": None,
        "views": None,
        "forwards": None,
        "via_bot_id": None,
        "post_author": None,
    }
    defaults.update(fields)
    return SimpleNamespace(**defaults)


class _Client:
    def __init__(self, messages: tuple[SimpleNamespace, ...]) -> None:
        self._messages = messages
        self.calls: list[tuple[int | str, int, int, bool, int | None]] = []

    async def iter_messages(
        self,
        entity,
        *,
        min_id=0,
        max_id=0,
        reverse=False,
        limit=None,
    ):
        self.calls.append((entity, min_id, max_id, reverse, limit))
        messages = self._messages
        if limit is not None:
            messages = messages[:limit]
        for message in messages:
            yield message


if __name__ == "__main__":
    unittest.main()
