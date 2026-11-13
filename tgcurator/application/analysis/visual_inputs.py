from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.application.ports.analysis import InferenceImage
from tgcurator.application.ports.cleanup import ArchiveStorageResolver
from tgcurator.application.ports.media import (
    ContactSheetTile,
    VisualImageBackend,
    VisualImageInspectionError,
    get_default_visual_image_backend,
)
from tgcurator.domain.analysis import AnalysisErrorCode, VisualCompositionPolicy

from .input_composer import AnalysisInputError, AnalysisTargetInput


@dataclass(frozen=True, slots=True)
class VisualInputLimits:
    max_source_images: int = 64
    max_source_bytes: int = 20 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_pixels: int = 24_000_000
    sheet_cell_size: int = 512
    sheet_max_tiles: int = 4

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in (
                    self.max_source_images,
                    self.max_source_bytes,
                    self.max_total_bytes,
                    self.max_pixels,
                    self.sheet_cell_size,
                    self.sheet_max_tiles,
                )
            )
            or self.sheet_cell_size < 64
            or self.sheet_max_tiles > 16
        ):
            raise ValueError("invalid visual input limits")


class ArchiveVisualInputResolver:
    """Resolve immutable archive snapshots, never provider URLs or host paths.

    All storage I/O occurs in prepare_async after the repository transaction has
    returned. Local data URLs remain supported for explicit/test input, but are
    decoded, validated and hashed; the URL itself is never persisted.
    """

    def __init__(
        self,
        *,
        storage_resolver: ArchiveStorageResolver | None,
        limits: VisualInputLimits | None = None,
        visual_backend: VisualImageBackend | None = None,
    ) -> None:
        self._storage_resolver = storage_resolver
        self.limits = limits or VisualInputLimits()
        self._visual_backend = visual_backend

    @property
    def visual_backend(self) -> VisualImageBackend | None:
        return self._visual_backend

    async def resolve(
        self,
        targets: tuple[AnalysisTargetInput, ...],
    ) -> tuple[InferenceImage, ...]:
        descriptors = _sources(targets)
        if len(descriptors) > self.limits.max_source_images:
            _fail("VisualSourceBudgetExceeded", budget=True)
        backend = _require_visual_backend(self._visual_backend)
        images: list[InferenceImage] = []
        total = 0
        for identity, descriptor in descriptors:
            content = await self._read(descriptor)
            total += len(content)
            if total > self.limits.max_total_bytes:
                _fail("VisualByteBudgetExceeded", budget=True)
            digest = hashlib.sha256(content).hexdigest()
            expected = descriptor.get("archive_sha256")
            if expected is not None and expected != digest:
                _fail("VisualArchiveHashMismatch")
            content_type, width, height = await asyncio.to_thread(
                _inspect_image, content, self.limits, backend
            )
            source = {
                **identity,
                "source_index": len(images),
                "sha256": digest,
                "width": width,
                "height": height,
                "region": [0, 0, width, height],
            }
            images.append(
                InferenceImage(
                    content=content,
                    content_type=content_type,
                    sha256=digest,
                    width=width,
                    height=height,
                    sources=(source,),
                )
            )
        return tuple(images)

    async def _read(self, descriptor: Mapping[str, Any]) -> bytes:
        backend, key = descriptor.get("storage_backend"), descriptor.get("storage_key")
        if backend is not None or key is not None:
            if not isinstance(backend, str) or not backend or not isinstance(key, str) or not key:
                _fail("VisualArchiveReferenceInvalid")
            if self._storage_resolver is None:
                _fail("VisualArchiveResolverUnavailable")
            try:
                storage = self._storage_resolver.resolve(storage_backend=backend)
                size = await storage.size(key=key)
                if not isinstance(size, int) or size <= 0:
                    _fail("VisualArchiveEmpty")
                if size > self.limits.max_source_bytes:
                    _fail("VisualSourceByteBudgetExceeded", budget=True)
                content = await storage.open(key=key)
            except AnalysisInputError:
                raise
            except Exception as error:
                raise AnalysisInputError(
                    code=AnalysisErrorCode.MEDIA_UNAVAILABLE,
                    error_type="VisualArchiveReadFailed",
                    retryable=True,
                ) from error
        else:
            data_url = descriptor.get("data_url")
            if not isinstance(data_url, str):
                # No unverified external URL fallback (including signed URLs).
                _fail("VisualArchiveReferenceMissing")
            if len(data_url) > self.limits.max_source_bytes * 4 // 3 + 256:
                _fail("VisualSourceByteBudgetExceeded", budget=True)
            prefix, separator, encoded = data_url.partition(",")
            if (
                not separator
                or not prefix.startswith("data:image/")
                or not prefix.endswith(";base64")
            ):
                _fail("VisualDataUrlInvalid")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise AnalysisInputError(
                    code=AnalysisErrorCode.MEDIA_UNAVAILABLE,
                    error_type="VisualDataUrlInvalid",
                    retryable=True,
                ) from error
        if not isinstance(content, bytes) or not content:
            _fail("VisualArchiveEmpty")
        if len(content) > self.limits.max_source_bytes:
            _fail("VisualSourceByteBudgetExceeded", budget=True)
        return content


_SUPPORTED_PROBE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


def probe_visual_descriptors(
    targets: tuple[AnalysisTargetInput, ...],
    *,
    policy: VisualCompositionPolicy,
    max_images: int,
    limits: VisualInputLimits,
) -> tuple[InferenceImage, ...] | None:
    """Build metadata-only visual descriptors without opening archive blobs when raw/adaptive."""

    descriptors = _sources(targets)
    if not descriptors or len(descriptors) > limits.max_source_images:
        return None
    if policy is VisualCompositionPolicy.CONTACT_SHEET:
        return None
    if policy not in {VisualCompositionPolicy.RAW, VisualCompositionPolicy.ADAPTIVE}:
        return None
    if len(descriptors) > max_images:
        return None

    probed: list[InferenceImage] = []
    total_bytes = 0
    for identity, descriptor in descriptors:
        digest = descriptor.get("archive_sha256")
        size = (
            descriptor.get("archive_size_bytes")
            if descriptor.get("archive_size_bytes") is not None
            else descriptor.get("size_bytes")
        )
        width = descriptor.get("width")
        height = descriptor.get("height")
        content_type = descriptor.get("content_type")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or size > limits.max_source_bytes
            or not isinstance(width, int)
            or isinstance(width, bool)
            or width <= 0
            or not isinstance(height, int)
            or isinstance(height, bool)
            or height <= 0
            or width * height > limits.max_pixels
            or content_type not in _SUPPORTED_PROBE_CONTENT_TYPES
        ):
            return None
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            return None
        source = {
            **identity,
            "source_index": len(probed),
            "sha256": digest,
            "width": width,
            "height": height,
            "region": [0, 0, width, height],
        }
        probed.append(
            InferenceImage(
                content=b"",
                content_type=content_type,
                sha256=digest,
                width=width,
                height=height,
                sources=(source,),
                composition="raw",
                size_bytes=size,
            )
        )
    return tuple(probed)


def compose_visual_inputs(
    images: tuple[InferenceImage, ...],
    *,
    policy: VisualCompositionPolicy,
    max_images: int,
    limits: VisualInputLimits,
    visual_backend: VisualImageBackend | None = None,
) -> tuple[InferenceImage, ...]:
    if not images:
        _fail("RequiredMediaUnavailable")
    if policy is VisualCompositionPolicy.RAW or (
        policy is VisualCompositionPolicy.ADAPTIVE and len(images) <= max_images
    ):
        if len(images) > max_images:
            _fail("VisualImageBudgetExceeded", budget=True)
        return images
    if policy not in {VisualCompositionPolicy.CONTACT_SHEET, VisualCompositionPolicy.ADAPTIVE}:
        _fail("VisualCompositionUnsupported", budget=True)
    # Bound tile count/resolution; never squeeze unbounded content into one image
    # or drop assets/frames to satisfy max_images. Split into multiple sheets.
    required = math.ceil(len(images) / limits.sheet_max_tiles)
    if required > max_images:
        _fail("VisualImageBudgetExceeded", budget=True)
    backend = _require_visual_backend(visual_backend)
    sheets = tuple(
        _contact_sheet(images[start : start + limits.sheet_max_tiles], limits, backend)
        for start in range(0, len(images), limits.sheet_max_tiles)
    )
    if sum(len(image.content) for image in sheets) > limits.max_total_bytes:
        _fail("VisualByteBudgetExceeded", budget=True)
    return sheets


def _sources(
    targets: tuple[AnalysisTargetInput, ...],
) -> list[tuple[dict[str, Any], Mapping[str, Any]]]:
    result: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    for target in targets:
        if not target.media:
            _fail("RequiredMediaUnavailable")
        for media in target.media:
            kind = media.get("kind", "image")
            asset_id = media.get("asset_id") or target.asset_id
            if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 256:
                _fail("VisualAssetIdentityMissing")
            identity = {
                "target_id": target.target_id,
                "message_id": target.message_id,
                "asset_id": asset_id,
                "kind": kind,
            }
            if target.asset_id is not None and target.asset_id != asset_id:
                _fail("VisualAssetIdentityMismatch")
            if kind == "image":
                result.append((identity, media))
            elif kind == "video":
                frames = media.get("frames")
                if not isinstance(frames, (list, tuple)) or not frames:
                    _fail("VideoFramesUnavailable")
                seen: set[tuple[str, int]] = set()
                for frame in frames:
                    if not isinstance(frame, Mapping):
                        _fail("VideoFrameInvalid")
                    role, index = frame.get("frame_role"), frame.get("frame_index")
                    timestamp = frame.get("timestamp_seconds")
                    if (
                        not isinstance(role, str)
                        or not role
                        or len(role) > 128
                        or not isinstance(index, int)
                        or isinstance(index, bool)
                        or index < 0
                        or not isinstance(timestamp, (int, float))
                        or isinstance(timestamp, bool)
                        or not math.isfinite(timestamp)
                        or timestamp < 0
                        or (role, index) in seen
                    ):
                        _fail("VideoFrameIdentityInvalid")
                    seen.add((role, index))
                    result.append(
                        (
                            {
                                **identity,
                                "frame_role": role,
                                "frame_index": index,
                                "timestamp_seconds": timestamp,
                            },
                            frame,
                        )
                    )
            else:
                _fail("VisualMediaKindUnsupported")
    return result


def _require_visual_backend(backend: VisualImageBackend | None) -> VisualImageBackend:
    resolved = backend or get_default_visual_image_backend()
    if resolved is None:
        _fail("VisualImageBackendUnavailable")
    assert resolved is not None
    return resolved


def _inspect_image(
    content: bytes, limits: VisualInputLimits, backend: VisualImageBackend
) -> tuple[str, int, int]:
    try:
        inspected = backend.inspect_visual_image_sync(content=content, max_pixels=limits.max_pixels)
        return inspected.content_type, inspected.width, inspected.height
    except VisualImageInspectionError as error:
        _fail(error.error_type, budget=error.budget)
    except AnalysisInputError:
        raise
    except Exception as error:
        raise AnalysisInputError(
            code=AnalysisErrorCode.MEDIA_UNAVAILABLE,
            error_type="VisualImageDecodeFailed",
            retryable=True,
        ) from error


def _contact_sheet(
    images: tuple[InferenceImage, ...],
    limits: VisualInputLimits,
    backend: VisualImageBackend,
) -> InferenceImage:
    tiles = tuple(ContactSheetTile(content=part.content, source=part.sources[0]) for part in images)
    rendered = backend.render_contact_sheet_sync(tiles=tiles, cell_size=limits.sheet_cell_size)
    return InferenceImage(
        content=rendered.content,
        content_type=rendered.content_type,
        sha256=hashlib.sha256(rendered.content).hexdigest(),
        width=rendered.width,
        height=rendered.height,
        sources=rendered.sources,
        composition="contact_sheet",
    )


def _fail(error_type: str, *, budget: bool = False) -> None:
    raise AnalysisInputError(
        code=AnalysisErrorCode.INPUT_BUILD_ERROR if budget else AnalysisErrorCode.MEDIA_UNAVAILABLE,
        error_type=error_type,
        retryable=True,
    )
