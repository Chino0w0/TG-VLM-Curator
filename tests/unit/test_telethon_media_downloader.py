from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from uuid import uuid4

from tgcurator.application.media import (
    TelegramMediaUnavailableError,
    TelegramProtectedContentError,
)
from tgcurator.application.ports.media import TelegramMediaDownloadRequest
from tgcurator.infrastructure.telegram import (
    StaticTelegramSourcePeerResolver,
    TelethonMediaDownloader,
)


class TelegramMediaDownloadRequestTests(unittest.TestCase):
    def test_rejects_invalid_stable_references(self) -> None:
        valid = _request()
        invalid_values = (
            {"source_channel_id": "not-a-uuid"},
            {"telegram_message_id": 0},
            {"telegram_message_id": True},
            {"source_asset_id": " "},
        )

        for changes in invalid_values:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                TelegramMediaDownloadRequest(
                    source_channel_id=changes.get("source_channel_id", valid.source_channel_id),
                    telegram_message_id=changes.get(
                        "telegram_message_id", valid.telegram_message_id
                    ),
                    source_asset_id=changes.get("source_asset_id", valid.source_asset_id),
                )


class TelethonMediaDownloaderTests(unittest.TestCase):
    def test_downloads_exact_resolved_source_message(self) -> None:
        request = _request()
        message = SimpleNamespace(media=object(), noforwards=False)
        client = _FakeTelethonMediaClient(message=message, content=b"telegram-media")
        downloader = TelethonMediaDownloader(
            client=client,
            source_peer_resolver=StaticTelegramSourcePeerResolver(
                {request.source_channel_id: -100123}
            ),
        )

        result = asyncio.run(downloader.download(request=request))

        self.assertEqual(result, b"telegram-media")
        self.assertEqual(client.get_calls, [(-100123, request.telegram_message_id)])
        self.assertEqual(client.download_calls, [(message, bytes)])

    def test_rejects_message_marked_content_protected_without_download(self) -> None:
        request = _request()
        client = _FakeTelethonMediaClient(
            message=SimpleNamespace(media=object(), noforwards=True), content=b"ignored"
        )
        downloader = _downloader_for(request=request, client=client)

        with self.assertRaises(TelegramProtectedContentError):
            asyncio.run(downloader.download(request=request))

        self.assertEqual(client.download_calls, [])

    def test_translates_telethon_protected_content_error(self) -> None:
        request = _request()
        client = _FakeTelethonMediaClient(
            message=SimpleNamespace(media=object(), noforwards=False),
            content=ChatForwardsRestrictedError("restricted"),
        )
        downloader = _downloader_for(request=request, client=client)

        with self.assertRaises(TelegramProtectedContentError) as raised:
            asyncio.run(downloader.download(request=request))

        self.assertIsInstance(raised.exception.__cause__, ChatForwardsRestrictedError)

    def test_rejects_missing_or_empty_media(self) -> None:
        request = _request()
        cases = (
            _FakeTelethonMediaClient(message=None, content=b"unused"),
            _FakeTelethonMediaClient(
                message=SimpleNamespace(media=None, noforwards=False), content=b"unused"
            ),
            _FakeTelethonMediaClient(
                message=SimpleNamespace(media=object(), noforwards=False), content=None
            ),
            _FakeTelethonMediaClient(
                message=SimpleNamespace(media=object(), noforwards=False), content=b""
            ),
        )

        for client in cases:
            with self.subTest(client=client), self.assertRaises(TelegramMediaUnavailableError):
                asyncio.run(
                    _downloader_for(request=request, client=client).download(request=request)
                )

    def test_does_not_hide_unrelated_telegram_errors(self) -> None:
        request = _request()
        client = _FakeTelethonMediaClient(
            message=RuntimeError("network unavailable"), content=b"unused"
        )

        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            asyncio.run(_downloader_for(request=request, client=client).download(request=request))


class ChatForwardsRestrictedError(Exception):
    pass


class _FakeTelethonMediaClient:
    def __init__(self, *, message: object, content: bytes | None | Exception) -> None:
        self._message = message
        self._content = content
        self.get_calls: list[tuple[int | str, int]] = []
        self.download_calls: list[tuple[object, type[bytes]]] = []

    async def get_messages(self, entity: int | str, *, ids: int) -> object:
        self.get_calls.append((entity, ids))
        if isinstance(self._message, Exception):
            raise self._message
        return self._message

    async def download_media(self, message: object, *, file: type[bytes]) -> bytes | None:
        self.download_calls.append((message, file))
        if isinstance(self._content, Exception):
            raise self._content
        return self._content


def _request() -> TelegramMediaDownloadRequest:
    return TelegramMediaDownloadRequest(
        source_channel_id=str(uuid4()),
        telegram_message_id=11,
        source_asset_id="telegram:source:11:0",
    )


def _downloader_for(
    *, request: TelegramMediaDownloadRequest, client: _FakeTelethonMediaClient
) -> TelethonMediaDownloader:
    return TelethonMediaDownloader(
        client=client,
        source_peer_resolver=StaticTelegramSourcePeerResolver({request.source_channel_id: -100123}),
    )


if __name__ == "__main__":
    unittest.main()
