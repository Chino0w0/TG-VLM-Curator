from __future__ import annotations

import asyncio
import io
from hashlib import sha256
from math import cos, pi

from PIL import Image, ImageOps, UnidentifiedImageError

from tgcurator.application.ports.media import (
    ImageNormalizationProfile,
    NormalizedImageArtifact,
)


class ImageProcessingError(ValueError):
    """Safe application-facing image decoding/normalization failure."""


class PillowImageProcessor:
    """Pillow adapter that produces metadata-free WebP evidence and 64-bit DCT pHashes."""

    async def normalize(
        self, *, content: bytes, profile: ImageNormalizationProfile
    ) -> NormalizedImageArtifact:
        if not content:
            raise ImageProcessingError("source image content must not be empty")
        return await asyncio.to_thread(self._normalize_sync, content, profile)

    @staticmethod
    def _normalize_sync(
        content: bytes, profile: ImageNormalizationProfile
    ) -> NormalizedImageArtifact:
        try:
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail(
                    (profile.max_side_pixels, profile.max_side_pixels),
                    Image.Resampling.LANCZOS,
                )
                normalized = oriented.convert(
                    "RGBA"
                    if "A" in oriented.getbands() or "transparency" in oriented.info
                    else "RGB"
                )
        except (OSError, UnidentifiedImageError) as error:
            raise ImageProcessingError("source content is not a decodable image") from error

        encoded = io.BytesIO()
        normalized.save(
            encoded,
            format="WEBP",
            quality=profile.quality,
            method=6,
        )
        archive_content = encoded.getvalue()
        return NormalizedImageArtifact(
            content=archive_content,
            content_type="image/webp",
            width=normalized.width,
            height=normalized.height,
            source_sha256=sha256(content).hexdigest(),
            archive_sha256=sha256(archive_content).hexdigest(),
            perceptual_hash=_dct_perceptual_hash(normalized),
        )


def _dct_perceptual_hash(image: Image.Image) -> str:
    """Calculate a deterministic 64-bit pHash from the display-oriented normalized pixels."""
    size = 32
    frequency_size = 8
    grayscale = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    get_flattened_data = getattr(grayscale, "get_flattened_data", None)
    pixels = list(get_flattened_data() if get_flattened_data else grayscale.getdata())
    cosines = tuple(
        tuple(cos((2 * coordinate + 1) * frequency * pi / (2 * size)) for coordinate in range(size))
        for frequency in range(frequency_size)
    )
    coefficients: list[float] = []
    for vertical_frequency in range(frequency_size):
        for horizontal_frequency in range(frequency_size):
            value = 0.0
            for vertical_coordinate in range(size):
                row_offset = vertical_coordinate * size
                vertical_cosine = cosines[vertical_frequency][vertical_coordinate]
                for horizontal_coordinate in range(size):
                    value += (
                        pixels[row_offset + horizontal_coordinate]
                        * cosines[horizontal_frequency][horizontal_coordinate]
                        * vertical_cosine
                    )
            coefficients.append(value)
    median = sorted(coefficients[1:])[len(coefficients[1:]) // 2]
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient > median)
    return f"{bits:016x}"
