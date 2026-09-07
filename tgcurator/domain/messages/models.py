from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from tgcurator.shared import DomainValidationError

_PHASH_PATTERN = re.compile(r"^[0-9a-fA-F]{16,}$")


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    DOCUMENT = "document"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """A normalized visual or non-visual asset belonging to one logical Message."""

    asset_id: str
    kind: MediaKind
    original_visual_phash: str | None = None
    video_cover_phash: str | None = None
    representative_frame_phashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, str) or not self.asset_id.strip():
            raise DomainValidationError("asset_id must not be blank")
        if not isinstance(self.kind, MediaKind):
            raise DomainValidationError("kind must be a MediaKind")
        if not isinstance(self.representative_frame_phashes, tuple):
            raise DomainValidationError("representative_frame_phashes must be a tuple")

        _validate_optional_phash("original_visual_phash", self.original_visual_phash)
        _validate_optional_phash("video_cover_phash", self.video_cover_phash)
        for value in self.representative_frame_phashes:
            _validate_phash("representative_frame_phashes", value)

        if self.kind is not MediaKind.IMAGE and self.original_visual_phash is not None:
            raise DomainValidationError("only image assets may define original_visual_phash")
        if self.kind is not MediaKind.VIDEO and self.video_cover_phash is not None:
            raise DomainValidationError("only video assets may define video_cover_phash")
        if self.kind is not MediaKind.VIDEO and self.representative_frame_phashes:
            raise DomainValidationError("only video assets may define representative_frame_phashes")

    @property
    def duplicate_identity_phash(self) -> str | None:
        """Return the only hash this asset may contribute to Message visual identity."""
        if self.kind is MediaKind.IMAGE:
            return self.original_visual_phash.lower() if self.original_visual_phash else None
        if self.kind is MediaKind.VIDEO:
            return self.video_cover_phash.lower() if self.video_cover_phash else None
        return None


@dataclass(frozen=True, slots=True)
class MessageContent:
    """The immutable, normalized logical Message payload."""

    text: str | None = None
    media: tuple[MediaAsset, ...] = ()

    def __post_init__(self) -> None:
        if self.text is not None and not isinstance(self.text, str):
            raise DomainValidationError("text must be a string or None")
        if not isinstance(self.media, tuple) or any(
            not isinstance(asset, MediaAsset) for asset in self.media
        ):
            raise DomainValidationError("media must be a tuple of MediaAsset values")
        if len({asset.asset_id for asset in self.media}) != len(self.media):
            raise DomainValidationError("a message cannot contain duplicate asset_id values")

    @property
    def visual_fingerprint(self) -> str | None:
        return message_visual_fingerprint(self.media)


def message_visual_fingerprint(assets: Iterable[MediaAsset]) -> str | None:
    """Return an order-independent fingerprint of the visual pHash multiset.

    The fingerprint deliberately excludes message text, source identity, media order, and
    video representative frames. A missing visual hash means no identity can be asserted.
    """
    hashes: list[str] = []
    for asset in assets:
        if not isinstance(asset, MediaAsset):
            raise DomainValidationError("visual fingerprint inputs must be MediaAsset values")
        if asset.duplicate_identity_phash is not None:
            hashes.append(asset.duplicate_identity_phash)
    hashes.sort()
    if not hashes:
        return None
    payload = "message-visual-fingerprint:v1\x1f" + "\x1f".join(hashes)
    return sha256(payload.encode("ascii")).hexdigest()


def _validate_optional_phash(field: str, value: str | None) -> None:
    if value is not None:
        _validate_phash(field, value)


def _validate_phash(field: str, value: str) -> None:
    if not isinstance(value, str) or not _PHASH_PATTERN.fullmatch(value):
        raise DomainValidationError(f"{field} must contain hexadecimal perceptual hashes")
