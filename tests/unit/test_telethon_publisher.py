from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from telethon import functions, types

from tgcurator.application.ports.publishing import (
    TelegramFloodWait,
    TelegramMediaUnavailable,
    TelegramPermissionDenied,
    TelegramProtectedContent,
    TelegramPublisherUnavailable,
    TelegramPublishRequest,
    TelegramSessionExpired,
)
from tgcurator.domain.publishing import PublicationOperation, PublicationPart
from tgcurator.infrastructure.telegram.telethon_publisher import (
    StaticTelegramPublishClientResolver,
    TelethonPublisher,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
IDENTITY_ID = "11111111-1111-4111-8111-111111111111"
INTENT_ID = "22222222-2222-4222-8222-222222222222"


class TelethonPublisherTests(unittest.TestCase):
    def test_forwards_with_persisted_random_ids_and_orders_response_by_random_id(self) -> None:
        client = _Client(
            responses=[
                _updates(
                    (102, 902),
                    (101, 901),
                )
            ]
        )
        publisher = _publisher(client)
        request = _request(
            operation=PublicationOperation.NATIVE_FORWARD,
            source_message_ids=(11, 12),
            random_ids=(101, 102),
        )

        result = asyncio.run(publisher.publish(request=request))

        self.assertEqual(result.remote_message_ids, (901, 902))
        self.assertEqual(len(client.requests), 1)
        raw = client.requests[0]
        self.assertIsInstance(raw, functions.messages.ForwardMessagesRequest)
        self.assertEqual(raw.from_peer, -100111)
        self.assertEqual(raw.to_peer, -100222)
        self.assertEqual(raw.id, [11, 12])
        self.assertEqual(raw.random_id, [101, 102])

    def test_sends_literal_text_with_stable_random_id_and_reply_target(self) -> None:
        client = _Client(responses=[SimpleNamespace(id=777, random_id=None, updates=None)])
        request = _request(
            operation=PublicationOperation.SEND_TEXT,
            source_message_ids=(11,),
            random_ids=(303,),
            text="rendered text",
            reply_to_message_id=700,
            part=PublicationPart.SUPPLEMENT,
        )

        result = asyncio.run(_publisher(client).publish(request=request))

        self.assertEqual(result.remote_message_ids, (777,))
        raw = client.requests[0]
        self.assertIsInstance(raw, functions.messages.SendMessageRequest)
        self.assertEqual(raw.peer, -100222)
        self.assertEqual(raw.message, "rendered text")
        self.assertEqual(raw.random_id, 303)
        self.assertIsInstance(raw.reply_to, types.InputReplyToMessage)
        self.assertEqual(raw.reply_to.reply_to_msg_id, 700)

    def test_copies_media_group_in_source_order_with_caption_only_on_first_item(self) -> None:
        messages = (
            _source_message(message_id=12, media=_input_photo(12)),
            _source_message(message_id=11, media=_input_photo(11)),
        )
        client = _Client(
            responses=[_updates((402, 802), (401, 801))],
            messages=messages,
        )
        request = _request(
            operation=PublicationOperation.COPY_WITH_CAPTION,
            source_message_ids=(11, 12),
            random_ids=(401, 402),
            text="album caption",
        )

        result = asyncio.run(_publisher(client).publish(request=request))

        self.assertEqual(result.remote_message_ids, (801, 802))
        self.assertEqual(client.get_calls, [(-100111, (11, 12))])
        raw = client.requests[0]
        self.assertIsInstance(raw, functions.messages.SendMultiMediaRequest)
        self.assertEqual(
            [(item.random_id, item.message) for item in raw.multi_media],
            [(401, "album caption"), (402, "")],
        )
        self.assertEqual(raw.multi_media[0].media.id.id, 11)
        self.assertEqual(raw.multi_media[1].media.id.id, 12)

    def test_copies_one_media_item_with_its_persisted_random_id(self) -> None:
        client = _Client(
            responses=[_updates((501, 901))],
            messages=(_source_message(message_id=11, media=_input_photo(11)),),
        )
        request = _request(
            operation=PublicationOperation.COPY_WITH_CAPTION,
            source_message_ids=(11,),
            random_ids=(501,),
            text="caption",
        )

        result = asyncio.run(_publisher(client).publish(request=request))

        self.assertEqual(result.remote_message_ids, (901,))
        raw = client.requests[0]
        self.assertIsInstance(raw, functions.messages.SendMediaRequest)
        self.assertEqual(raw.random_id, 501)
        self.assertEqual(raw.message, "caption")

    def test_reconcile_replays_the_same_idempotent_mtproto_request(self) -> None:
        client = _Client(responses=[_updates((101, 901))])
        request = _request(
            operation=PublicationOperation.NATIVE_FORWARD,
            source_message_ids=(11,),
            random_ids=(101,),
        )

        result = asyncio.run(_publisher(client).reconcile(request=request))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.remote_message_ids, (901,))
        self.assertEqual(client.requests[0].random_id, [101])

    def test_refuses_protected_or_missing_source_media_before_send(self) -> None:
        cases = (
            (
                (_source_message(message_id=11, media=_input_photo(11), noforwards=True),),
                "caption",
                TelegramProtectedContent,
            ),
            (
                (
                    SimpleNamespace(
                        id=11,
                        media=_input_photo(11),
                        noforwards=False,
                        chat=SimpleNamespace(noforwards=True),
                    ),
                ),
                "caption",
                TelegramProtectedContent,
            ),
            ((_source_message(message_id=11, media=None),), "", TelegramMediaUnavailable),
            ((), "caption", TelegramMediaUnavailable),
        )

        for messages, text, expected_error in cases:
            with self.subTest(expected_error=expected_error.__name__, text=text):
                request = _request(
                    operation=PublicationOperation.COPY_WITH_CAPTION,
                    source_message_ids=(11,),
                    random_ids=(601,),
                    text=text,
                )
                client = _Client(responses=[], messages=messages)
                with self.assertRaises(expected_error):
                    asyncio.run(_publisher(client).publish(request=request))
                self.assertEqual(client.requests, [])

    def test_copy_with_caption_on_text_only_source_sends_message_request(self) -> None:
        client = _Client(
            responses=[SimpleNamespace(id=777, random_id=601, updates=None)],
            messages=(_source_message(message_id=11, media=None),),
        )
        request = _request(
            operation=PublicationOperation.COPY_WITH_CAPTION,
            source_message_ids=(11,),
            random_ids=(601,),
            text="rendered text-only copy",
        )

        result = asyncio.run(_publisher(client).publish(request=request))

        self.assertEqual(result.remote_message_ids, (777,))
        self.assertEqual(len(client.requests), 1)
        self.assertIsInstance(client.requests[0], functions.messages.SendMessageRequest)
        self.assertEqual(client.requests[0].message, "rendered text-only copy")
        self.assertEqual(client.requests[0].random_id, 601)

    def test_translates_named_telethon_capability_failures(self) -> None:
        cases = (
            ("ChatWriteForbiddenError", TelegramPermissionDenied),
            ("SessionRevokedError", TelegramSessionExpired),
            ("ChatForwardsRestrictedError", TelegramProtectedContent),
            ("MediaEmptyError", TelegramMediaUnavailable),
            ("ServerError", TelegramPublisherUnavailable),
        )
        request = _request(
            operation=PublicationOperation.NATIVE_FORWARD,
            source_message_ids=(11,),
            random_ids=(701,),
        )

        for error_name, expected_error in cases:
            with self.subTest(error_name=error_name):
                error = type(error_name, (Exception,), {})("telegram failure")
                client = _Client(responses=[], request_error=error)
                with self.assertRaises(expected_error) as raised:
                    asyncio.run(_publisher(client).publish(request=request))
                self.assertEqual(raised.exception.error_type, error_name)
                self.assertIs(raised.exception.__cause__, error)

    def test_translates_flood_wait_to_exact_persistable_retry_time(self) -> None:
        error = type("FloodWaitError", (Exception,), {})()
        error.seconds = 47
        client = _Client(responses=[], request_error=error)
        request = _request(
            operation=PublicationOperation.NATIVE_FORWARD,
            source_message_ids=(11,),
            random_ids=(801,),
        )

        with self.assertRaises(TelegramFloodWait) as raised:
            asyncio.run(_publisher(client).publish(request=request))

        self.assertEqual(raised.exception.retry_at, NOW + timedelta(seconds=47))
        self.assertEqual(raised.exception.error_type, "FloodWaitError")

    def test_rejects_response_without_remote_message_ids(self) -> None:
        client = _Client(responses=[SimpleNamespace(updates=[])])
        request = _request(
            operation=PublicationOperation.NATIVE_FORWARD,
            source_message_ids=(11,),
            random_ids=(901,),
        )

        with self.assertRaises(TelegramPublisherUnavailable) as raised:
            asyncio.run(_publisher(client).publish(request=request))

        self.assertEqual(raised.exception.error_type, "TelegramResponseMissingMessageIds")

    def test_missing_identity_client_is_a_non_retryable_session_failure(self) -> None:
        publisher = TelethonPublisher(
            client_resolver=StaticTelegramPublishClientResolver({}),
            clock=lambda: NOW,
        )
        request = _request(
            operation=PublicationOperation.NATIVE_FORWARD,
            source_message_ids=(11,),
            random_ids=(1001,),
        )

        with self.assertRaises(TelegramSessionExpired) as raised:
            asyncio.run(publisher.publish(request=request))

        self.assertEqual(raised.exception.error_type, "TelegramIdentityUnavailable")


class _Client:
    def __init__(
        self,
        *,
        responses: list[Any],
        messages: tuple[Any, ...] = (),
        request_error: Exception | None = None,
    ) -> None:
        self.responses = list(responses)
        self.messages = messages
        self.request_error = request_error
        self.requests: list[Any] = []
        self.get_calls: list[tuple[int | str, tuple[int, ...]]] = []

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        if self.request_error is not None:
            raise self.request_error
        return self.responses.pop(0)

    async def get_messages(
        self,
        entity: int | str,
        *,
        ids: int | list[int] | tuple[int, ...],
    ) -> Any:
        normalized = (ids,) if isinstance(ids, int) else tuple(ids)
        self.get_calls.append((entity, normalized))
        return self.messages


def _publisher(client: _Client) -> TelethonPublisher:
    return TelethonPublisher(
        client_resolver=StaticTelegramPublishClientResolver({IDENTITY_ID: client}),
        clock=lambda: NOW,
    )


def _request(
    *,
    operation: PublicationOperation,
    source_message_ids: tuple[int, ...],
    random_ids: tuple[int, ...],
    text: str | None = None,
    reply_to_message_id: int | None = None,
    part: PublicationPart = PublicationPart.MAIN,
) -> TelegramPublishRequest:
    return TelegramPublishRequest(
        intent_id=INTENT_ID,
        part=part,
        operation=operation,
        identity_id=IDENTITY_ID,
        source_peer_id=-100111,
        source_message_ids=source_message_ids,
        destination_peer_id=-100222,
        random_ids=random_ids,
        text=text,
        reply_to_message_id=reply_to_message_id,
    )


def _updates(*pairs: tuple[int, int]) -> SimpleNamespace:
    return SimpleNamespace(
        updates=[
            SimpleNamespace(random_id=random_id, id=message_id) for random_id, message_id in pairs
        ]
    )


def _input_photo(message_id: int) -> types.InputMediaPhoto:
    return types.InputMediaPhoto(
        id=types.InputPhoto(
            id=message_id,
            access_hash=message_id + 1000,
            file_reference=b"file-reference",
        )
    )


def _source_message(
    *,
    message_id: int,
    media: object | None,
    noforwards: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(id=message_id, media=media, noforwards=noforwards)


if __name__ == "__main__":
    unittest.main()
