from __future__ import annotations

import asyncio
import io
import math
import warnings
from hashlib import sha256
from math import cos, pi

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from tgcurator.application.ports.media import (
    ContactSheetTile,
    ImageNormalizationProfile,
    NormalizedImageArtifact,
    RenderedContactSheet,
    VisualImageInspection,
    VisualImageInspectionError,
    register_default_visual_image_backend,
)

_DCT_SIZE = 32
_DCT_FREQ_SIZE = 8
_DCT_COSINES: tuple[tuple[float, ...], ...] = tuple(
    tuple(
        cos((2 * coordinate + 1) * frequency * pi / (2 * _DCT_SIZE))
        for coordinate in range(_DCT_SIZE)
    )
    for frequency in range(_DCT_FREQ_SIZE)
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

    def inspect_visual_image_sync(
        self, *, content: bytes, max_pixels: int
    ) -> VisualImageInspection:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    mime = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "WEBP": "image/webp",
                        "GIF": "image/gif",
                    }.get(image.format or "")
                    if mime is None:
                        raise VisualImageInspectionError("VisualImageFormatUnsupported")
                    if image.width * image.height > max_pixels:
                        raise VisualImageInspectionError("VisualPixelBudgetExceeded", budget=True)
                    if getattr(image, "n_frames", 1) != 1:
                        raise VisualImageInspectionError("AnimatedVisualRequiresFrames")
                    width, height = image.size
                    image.verify()
                with Image.open(io.BytesIO(content)) as image:
                    image.load()
            return VisualImageInspection(content_type=mime, width=width, height=height)
        except VisualImageInspectionError:
            raise
        except Exception as error:
            raise VisualImageInspectionError("VisualImageDecodeFailed") from error

    def render_contact_sheet_sync(
        self, *, tiles: tuple[ContactSheetTile, ...], cell_size: int
    ) -> RenderedContactSheet:
        cell, footer = cell_size, 24
        columns = math.ceil(math.sqrt(len(tiles)))
        rows = math.ceil(len(tiles) / columns)
        canvas = Image.new("RGB", (columns * cell, rows * (cell + footer)), "white")
        draw = ImageDraw.Draw(canvas)
        sources: list[dict[str, object]] = []
        for index, part in enumerate(tiles):
            left, top = (index % columns) * cell, (index // columns) * (cell + footer)
            with Image.open(io.BytesIO(part.content)) as original:
                tile = ImageOps.exif_transpose(original).convert("RGB")
                tile.thumbnail((cell, cell), Image.Resampling.LANCZOS)
                x, y = left + (cell - tile.width) // 2, top + (cell - tile.height) // 2
                canvas.paste(tile, (x, y))
                source = dict(part.source)
                source["region"] = [x, y, x + tile.width, y + tile.height]
                sources.append(source)
                draw.text(
                    (left + 4, top + cell + 4),
                    f"source {source['source_index']}",
                    fill="black",
                )
        output = io.BytesIO()
        canvas.save(output, format="JPEG", quality=90, optimize=False, progressive=False)
        content = output.getvalue()
        return RenderedContactSheet(
            content=content,
            content_type="image/jpeg",
            width=canvas.width,
            height=canvas.height,
            sources=tuple(sources),
        )

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
    """Calculate a deterministic 64-bit pHash using a separable 2-pass 32x32 DCT."""
    grayscale = image.convert("L").resize((_DCT_SIZE, _DCT_SIZE), Image.Resampling.LANCZOS)
    get_flattened_data = getattr(grayscale, "get_flattened_data", None)
    pixels = list(get_flattened_data() if get_flattened_data else grayscale.getdata())

    horizontal_projection: list[list[float]] = []
    for horizontal_frequency in range(_DCT_FREQ_SIZE):
        cos_row = _DCT_COSINES[horizontal_frequency]
        projected_rows: list[float] = []
        for vertical_coordinate in range(_DCT_SIZE):
            row_offset = vertical_coordinate * _DCT_SIZE
            row_sum = 0.0
            for horizontal_coordinate in range(_DCT_SIZE):
                row_sum += (
                    pixels[row_offset + horizontal_coordinate] * cos_row[horizontal_coordinate]
                )
            projected_rows.append(row_sum)
        horizontal_projection.append(projected_rows)

    coefficients: list[float] = []
    for vertical_frequency in range(_DCT_FREQ_SIZE):
        cos_col = _DCT_COSINES[vertical_frequency]
        for horizontal_frequency in range(_DCT_FREQ_SIZE):
            projected_rows = horizontal_projection[horizontal_frequency]
            value = 0.0
            for vertical_coordinate in range(_DCT_SIZE):
                value += projected_rows[vertical_coordinate] * cos_col[vertical_coordinate]
            coefficients.append(value)

    median = sorted(coefficients[1:])[len(coefficients[1:]) // 2]
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient > median)
    return f"{bits:016x}"


register_default_visual_image_backend(PillowImageProcessor)
