from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tgcurator.application.ports.publishing import (
    TelegramFloodWait,
    TelegramMediaUnavailable,
    TelegramPermissionDenied,
    TelegramProtectedContent,
    TelegramPublicationError,
    TelegramPublisherUnavailable,
    TelegramPublishRequest,
    TelegramPublishResult,
    TelegramSessionExpired,
)
from tgcurator.domain.publishing import PublicationOperation
from tgcurator.shared import ensure_aware


class TelethonPublishClient(Protocol):
    """Minimal async Telethon client surface required by publication."""

    async def __call__(self, request: Any) -> Any: ...

    async def get_messages(
        self,
        entity: int | str,
        *,
        ids: int | Sequence[int],
    ) -> Any: ...


class TelegramPublishClientResolver(Protocol):
    """Resolve the already-authenticated client for one durable publish identity."""

    async def resolve_client(self, *, identity_id: str) -> TelethonPublishClient: ...


class StaticTelegramPublishClientResolver:
    """Small composition helper for processes that already own identity clients."""

    def __init__(self, clients_by_identity_id: Mapping[str, TelethonPublishClient]) -> None:
        self._clients = dict(clients_by_identity_id)

    async def resolve_client(self, *, identity_id: str) -> TelethonPublishClient:
        try:
            return self._clients[identity_id]
        except KeyError as error:
            raise TelegramSessionExpired("TelegramIdentityUnavailable") from error


class TelethonPublisher:
    """Execute identity-aware MTProto publication using persisted random IDs.

    ``reconcile`` deliberately replays the same raw MTProto request. Telegram's random-id
    deduplication then either returns the already-created message or performs the send that never
    crossed the remote boundary. This keeps retry behavior deterministic without a live session in
    unit tests and without pretending that Redis delivery is the business source of truth.
    """

    def __init__(
        self,
        *,
        client_resolver: TelegramPublishClientResolver,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client_resolver = client_resolver
        self._clock = clock or _utc_now

    async def publish(self, *, request: TelegramPublishRequest) -> TelegramPublishResult:
        return await self._execute(request=request)

    async def reconcile(self, *, request: TelegramPublishRequest) -> TelegramPublishResult | None:
        return await self._execute(request=request)

    async def _execute(self, *, request: TelegramPublishRequest) -> TelegramPublishResult:
        try:
            publish_client = await self._client_resolver.resolve_client(
                identity_id=request.identity_id
            )
            read_client = publish_client
            force_reupload = bool(getattr(publish_client, "is_bot", False))
            if (
                request.operation is PublicationOperation.COPY_WITH_CAPTION
                and request.source_read_identity_id is not None
                and request.source_read_identity_id != request.identity_id
            ):
                read_client = await self._client_resolver.resolve_client(
                    identity_id=request.source_read_identity_id
                )
                force_reupload = True

            raw_request = await self._build_request(
                publish_client=publish_client,
                read_client=read_client,
                request=request,
                force_reupload=force_reupload,
            )
            try:
                response = await publish_client(raw_request)
            except Exception as send_error:
                if (
                    request.operation is PublicationOperation.COPY_WITH_CAPTION
                    and not force_reupload
                    and {base.__name__ for base in type(send_error).__mro__}
                    & _REUPLOAD_FALLBACK_ERRORS
                    and hasattr(read_client, "download_media")
                    and hasattr(publish_client, "upload_file")
                ):
                    raw_request = await self._build_request(
                        publish_client=publish_client,
                        read_client=read_client,
                        request=request,
                        force_reupload=True,
                    )
                    response = await publish_client(raw_request)
                else:
                    raise
            return TelegramPublishResult(
                remote_message_ids=_response_message_ids(
                    response=response,
                    random_ids=request.random_ids,
                )
            )
        except TelegramPublicationError:
            raise
        except Exception as error:
            raise _translate_telegram_error(error=error, now=self._clock()) from error

    @staticmethod
    async def _build_request(
        *,
        publish_client: TelethonPublishClient,
        read_client: TelethonPublishClient,
        request: TelegramPublishRequest,
        force_reupload: bool = False,
    ) -> Any:
        functions, types, utils = _telethon_components()
        if request.operation is PublicationOperation.NATIVE_FORWARD:
            return functions.messages.ForwardMessagesRequest(
                from_peer=request.source_peer_id,
                id=list(request.source_message_ids),
                to_peer=request.destination_peer_id,
                random_id=list(request.random_ids),
            )

        reply_to = (
            types.InputReplyToMessage(reply_to_msg_id=request.reply_to_message_id)
            if request.reply_to_message_id is not None
            else None
        )
        if request.operation is PublicationOperation.SEND_TEXT:
            return functions.messages.SendMessageRequest(
                peer=request.destination_peer_id,
                message=request.text,
                random_id=request.random_ids[0],
                reply_to=reply_to,
            )

        source_messages = await _source_messages(client=read_client, request=request)
        for message in source_messages:
            if _is_message_protected(message):
                raise TelegramProtectedContent("SourceMessageProtected")

        if all(getattr(message, "media", None) is None for message in source_messages):
            if len(source_messages) == 1 and request.text:
                return functions.messages.SendMessageRequest(
                    peer=request.destination_peer_id,
                    message=request.text,
                    random_id=request.random_ids[0],
                    reply_to=reply_to,
                )
            raise TelegramMediaUnavailable("SourceMessageMediaUnavailable")

        input_media: list[Any] = []
        if force_reupload:
            input_media = await _reupload_input_media(
                read_client=read_client,
                publish_client=publish_client,
                source_messages=source_messages,
                types=types,
            )
        else:
            for message in source_messages:
                media = getattr(message, "media", None)
                if media is None:
                    raise TelegramMediaUnavailable("SourceMessageMediaUnavailable")
                try:
                    input_media.append(utils.get_input_media(media))
                except (AttributeError, TypeError, ValueError) as error:
                    raise TelegramMediaUnavailable("SourceMessageMediaUnavailable") from error

        if len(input_media) == 1:
            return functions.messages.SendMediaRequest(
                peer=request.destination_peer_id,
                media=input_media[0],
                message=request.text,
                random_id=request.random_ids[0],
            )

        return functions.messages.SendMultiMediaRequest(
            peer=request.destination_peer_id,
            multi_media=[
                types.InputSingleMedia(
                    media=media,
                    message=request.text if index == 0 else "",
                    random_id=request.random_ids[index],
                )
                for index, media in enumerate(input_media)
            ],
        )


def _is_message_protected(message: Any) -> bool:
    return bool(
        getattr(message, "noforwards", False)
        or getattr(getattr(message, "chat", None), "noforwards", False)
    )


async def _reupload_input_media(
    *,
    read_client: Any,
    publish_client: Any,
    source_messages: tuple[Any, ...],
    types: Any,
) -> list[Any]:
    download_fn = getattr(read_client, "download_media", None)
    upload_fn = getattr(publish_client, "upload_file", None)
    if download_fn is None or upload_fn is None:
        raise TelegramMediaUnavailable("SourceMessageMediaUnavailable")

    input_media: list[Any] = []
    for index, message in enumerate(source_messages):
        media = getattr(message, "media", None)
        if media is None:
            raise TelegramMediaUnavailable("SourceMessageMediaUnavailable")
        media_bytes = await download_fn(message, file=bytes)
        if not isinstance(media_bytes, (bytes, bytearray)) or not media_bytes:
            raise TelegramMediaUnavailable("SourceMessageMediaUnavailable")
        raw_bytes = bytes(media_bytes)
        msg_id = getattr(message, "id", index)
        is_photo = (
            isinstance(media, (types.MessageMediaPhoto, types.InputMediaPhoto))
            or getattr(media, "photo", None) is not None
        )
        if is_photo:
            uploaded = await upload_fn(raw_bytes, file_name=f"photo_{msg_id}.jpg")
            if isinstance(uploaded, (types.InputMediaPhoto, types.InputMediaUploadedPhoto)):
                input_media.append(uploaded)
            else:
                input_media.append(types.InputMediaUploadedPhoto(file=uploaded))
            continue

        doc = getattr(media, "document", None)
        mime_type = getattr(doc, "mime_type", None) or "application/octet-stream"
        attributes = list(getattr(doc, "attributes", None) or [])
        file_name = next(
            (str(name) for attr in attributes if (name := getattr(attr, "file_name", None))),
            None,
        )
        if not file_name:
            if mime_type.startswith("video/"):
                ext = ".mp4"
            elif mime_type == "image/webp":
                ext = ".webp"
            elif mime_type == "image/jpeg":
                ext = ".jpg"
            elif mime_type == "image/png":
                ext = ".png"
            else:
                ext = ".bin"
            file_name = f"media_{msg_id}{ext}"
            attributes.append(types.DocumentAttributeFilename(file_name=file_name))
        uploaded = await upload_fn(raw_bytes, file_name=file_name)
        if isinstance(uploaded, (types.InputMediaDocument, types.InputMediaUploadedDocument)):
            input_media.append(uploaded)
        else:
            input_media.append(
                types.InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type=mime_type,
                    attributes=attributes,
                )
            )
    return input_media


async def _source_messages(
    *,
    client: TelethonPublishClient,
    request: TelegramPublishRequest,
) -> tuple[Any, ...]:
    raw_messages = await client.get_messages(
        request.source_peer_id,
        ids=list(request.source_message_ids),
    )
    if raw_messages is None:
        raise TelegramMediaUnavailable("SourceMessageMediaUnavailable")
    if isinstance(raw_messages, Sequence) and not isinstance(raw_messages, (str, bytes, bytearray)):
        candidates = tuple(raw_messages)
    else:
        candidates = (raw_messages,)

    by_id = {
        message_id: message
        for message in candidates
        if (message_id := getattr(message, "id", None)) is not None
    }
    try:
        ordered = tuple(by_id[message_id] for message_id in request.source_message_ids)
    except KeyError as error:
        raise TelegramMediaUnavailable("SourceMessageMediaUnavailable") from error
    if len(ordered) != len(request.source_message_ids):
        raise TelegramMediaUnavailable("SourceMessageMediaUnavailable")
    return ordered


def _response_message_ids(*, response: Any, random_ids: tuple[int, ...]) -> tuple[int, ...]:
    expected = set(random_ids)
    mapped: dict[int, int] = {}
    fallback: list[int] = []

    direct_id = _positive_int(getattr(response, "id", None))
    direct_random_id = _positive_int(getattr(response, "random_id", None))
    if direct_id is not None:
        fallback.append(direct_id)
        if direct_random_id in expected:
            mapped[direct_random_id] = direct_id

    updates = getattr(response, "updates", ()) or ()
    for update in updates:
        remote_id = _positive_int(getattr(update, "id", None))
        random_id = _positive_int(getattr(update, "random_id", None))
        if remote_id is not None and random_id in expected:
            mapped[random_id] = remote_id

        message = getattr(update, "message", None)
        message_id = _positive_int(getattr(message, "id", None))
        if message_id is not None and message_id not in fallback:
            fallback.append(message_id)

    if all(random_id in mapped for random_id in random_ids):
        return tuple(mapped[random_id] for random_id in random_ids)
    if len(random_ids) == 1 and len(fallback) == 1:
        return (fallback[0],)
    if len(fallback) == len(random_ids):
        return tuple(fallback)
    raise TelegramPublisherUnavailable("TelegramResponseMissingMessageIds")


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _translate_telegram_error(*, error: Exception, now: datetime) -> TelegramPublicationError:
    ensure_aware(now, field="now")
    error_type = type(error).__name__
    names = {base.__name__ for base in type(error).__mro__}

    if names & _FLOOD_WAIT_ERRORS:
        seconds = _positive_int(getattr(error, "seconds", None))
        if seconds is None:
            seconds = _positive_int(getattr(error, "value", None)) or 1
        return TelegramFloodWait(
            retry_at=now + timedelta(seconds=seconds),
            error_type=error_type,
        )
    if names & _PROTECTED_CONTENT_ERRORS:
        return TelegramProtectedContent(error_type)
    if names & _SESSION_ERRORS:
        return TelegramSessionExpired(error_type)
    if names & _PERMISSION_ERRORS:
        return TelegramPermissionDenied(error_type)
    if names & _MEDIA_ERRORS:
        return TelegramMediaUnavailable(error_type)
    return TelegramPublisherUnavailable(error_type)


def _telethon_components() -> tuple[Any, Any, Any]:
    try:
        from telethon import functions, types, utils
    except ImportError as error:  # pragma: no cover - runtime packaging failure
        raise TelegramPublisherUnavailable("TelethonUnavailable") from error
    return functions, types, utils


def _utc_now() -> datetime:
    return datetime.now(UTC)


_FLOOD_WAIT_ERRORS = {
    "FloodPremiumWaitError",
    "FloodTestPhoneWaitError",
    "FloodWaitError",
    "SlowModeWaitError",
}
_PROTECTED_CONTENT_ERRORS = {
    "ChatForwardsRestrictedError",
    "ChatRestrictedError",
    "ForwardsRestrictedError",
}
_SESSION_ERRORS = {
    "AuthKeyDuplicatedError",
    "AuthKeyPermEmptyError",
    "AuthKeyUnregisteredError",
    "SessionExpiredError",
    "SessionPasswordNeededError",
    "SessionRevokedError",
    "UnauthorizedError",
    "UserDeactivatedBanError",
    "UserDeactivatedError",
}
_PERMISSION_ERRORS = {
    "BotMethodInvalidError",
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "ChatForbiddenError",
    "ChatWriteForbiddenError",
    "PeerIdInvalidError",
    "SendMessagesForbiddenError",
    "UserBannedInChannelError",
}
_MEDIA_ERRORS = {
    "DocumentInvalidError",
    "FileReferenceExpiredError",
    "MediaEmptyError",
    "MediaInvalidError",
    "MessageIdInvalidError",
    "MessageIdsEmptyError",
    "PhotoInvalidError",
}
_REUPLOAD_FALLBACK_ERRORS = {
    "DocumentInvalidError",
    "FileReferenceExpiredError",
    "MediaInvalidError",
    "PhotoInvalidError",
}
