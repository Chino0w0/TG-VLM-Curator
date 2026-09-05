from __future__ import annotations


class TelegramMediaDownloadError(RuntimeError):
    """Base error for source-media retrieval failures that callers may handle explicitly."""


class TelegramProtectedContentError(TelegramMediaDownloadError):
    """Telegram refused retrieval because the source content is protected."""


class TelegramMediaUnavailableError(TelegramMediaDownloadError):
    """The referenced Telegram message has no retrievable media bytes."""
