from __future__ import annotations

import asyncio
import io
import unittest
from hashlib import sha256

from PIL import Image

from tgcurator.application.media import ImageArchiveService
from tgcurator.application.ports.media import ImageNormalizationProfile, NormalizedImageArtifact
from tgcurator.infrastructure.media import ImageProcessingError, PillowImageProcessor


class PillowImageProcessorTests(unittest.TestCase):
    def test_normalize_orients_bounds_and_fingerprints_webp(self) -> None:
        source_content = _oriented_jpeg()
        profile = ImageNormalizationProfile(max_side_pixels=90, quality=82)

        artifact = asyncio.run(
            PillowImageProcessor().normalize(content=source_content, profile=profile)
        )

        self.assertEqual(artifact.content_type, "image/webp")
        self.assertEqual((artifact.width, artifact.height), (45, 90))
        self.assertEqual(artifact.source_sha256, sha256(source_content).hexdigest())
        self.assertEqual(artifact.archive_sha256, sha256(artifact.content).hexdigest())
        self.assertRegex(artifact.perceptual_hash, r"^[0-9a-f]{16}$")

        with Image.open(io.BytesIO(artifact.content)) as archived:
            self.assertEqual(archived.format, "WEBP")
            self.assertEqual((archived.width, archived.height), (45, 90))
            self.assertNotIn(274, archived.getexif())

    def test_normalize_preserves_transparency(self) -> None:
        source = Image.new("RGBA", (20, 10), (10, 20, 30, 0))
        source.putpixel((10, 5), (100, 110, 120, 255))
        encoded = io.BytesIO()
        source.save(encoded, format="PNG")

        artifact = asyncio.run(
            PillowImageProcessor().normalize(
                content=encoded.getvalue(),
                profile=ImageNormalizationProfile(max_side_pixels=100, quality=90),
            )
        )

        with Image.open(io.BytesIO(artifact.content)) as archived:
            self.assertEqual(archived.mode, "RGBA")
            self.assertEqual(archived.getpixel((0, 0))[3], 0)
            self.assertGreater(archived.getpixel((10, 5))[3], 0)

    def test_same_input_has_deterministic_artifact_and_visual_fingerprint(self) -> None:
        source_content = _oriented_jpeg()
        profile = ImageNormalizationProfile(max_side_pixels=90, quality=82)
        processor = PillowImageProcessor()

        first = asyncio.run(processor.normalize(content=source_content, profile=profile))
        second = asyncio.run(processor.normalize(content=source_content, profile=profile))

        self.assertEqual(first.perceptual_hash, second.perceptual_hash)
        self.assertEqual(first.archive_sha256, second.archive_sha256)
        self.assertEqual(first.content, second.content)

    def test_invalid_source_content_raises_safe_processing_error(self) -> None:
        profile = ImageNormalizationProfile(max_side_pixels=100, quality=80)
        processor = PillowImageProcessor()

        for source_content in (b"", b"not-an-image"):
            with self.subTest(source_content=source_content):
                with self.assertRaises(ImageProcessingError):
                    asyncio.run(processor.normalize(content=source_content, profile=profile))

    def test_invalid_profile_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ImageNormalizationProfile(max_side_pixels=0, quality=80)
        with self.assertRaises(ValueError):
            ImageNormalizationProfile(max_side_pixels=100, quality=0)
        with self.assertRaises(ValueError):
            ImageNormalizationProfile(max_side_pixels=100, quality=101)

    def test_image_archive_service_stores_normalized_content_only(self) -> None:
        artifact = NormalizedImageArtifact(
            content=b"normalized-webp",
            content_type="image/webp",
            width=10,
            height=20,
            source_sha256="0" * 64,
            archive_sha256="1" * 64,
            perceptual_hash="2" * 16,
        )
        processor = _RecordingImageProcessor(artifact)
        archive = _RecordingArchiveStorage()
        profile = ImageNormalizationProfile(max_side_pixels=100, quality=80)

        result = asyncio.run(
            ImageArchiveService(processor, archive).normalize_and_archive(
                content=b"source-image",
                storage_key="source/7/message/11/image.webp",
                profile=profile,
            )
        )

        self.assertIs(result, artifact)
        self.assertEqual(processor.calls, [(b"source-image", profile)])
        self.assertEqual(
            archive.put_calls,
            [("source/7/message/11/image.webp", b"normalized-webp", "image/webp")],
        )


class _RecordingImageProcessor:
    def __init__(self, artifact: NormalizedImageArtifact) -> None:
        self.artifact = artifact
        self.calls: list[tuple[bytes, ImageNormalizationProfile]] = []

    async def normalize(
        self, *, content: bytes, profile: ImageNormalizationProfile
    ) -> NormalizedImageArtifact:
        self.calls.append((content, profile))
        return self.artifact


class _RecordingArchiveStorage:
    def __init__(self) -> None:
        self.put_calls: list[tuple[str, bytes, str]] = []

    async def put(self, *, key: str, content: bytes, content_type: str) -> int:
        self.put_calls.append((key, content, content_type))
        return len(content)


def _oriented_jpeg() -> bytes:
    image = Image.new("RGB", (200, 100))
    for vertical in range(image.height):
        for horizontal in range(image.width):
            image.putpixel(
                (horizontal, vertical),
                ((horizontal * 3) % 256, (vertical * 5) % 256, (horizontal + vertical) % 256),
            )
    exif = Image.Exif()
    exif[274] = 6
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=95, exif=exif)
    return encoded.getvalue()


if __name__ == "__main__":
    unittest.main()
