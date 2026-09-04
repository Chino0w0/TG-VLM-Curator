from __future__ import annotations

import asyncio
import unittest

from tgcurator.application.media.videos import (
    VideoFrameEvidence,
    VideoFrameSamplingService,
    VideoSamplingProfile,
    VideoSamplingResult,
    candidate_timestamps,
    perceptual_hash_distance,
)
from tgcurator.application.ports.media import ImageNormalizationProfile, NormalizedImageArtifact
from tgcurator.shared import DomainValidationError


class VideoSamplingTests(unittest.TestCase):
    def test_candidate_timestamps_are_evenly_spread_and_bounded(self) -> None:
        profile = VideoSamplingProfile(
            min_candidate_frames=4,
            max_candidate_frames=4,
            max_representative_frames=2,
            min_temporal_gap_seconds=2,
        )

        self.assertEqual(
            candidate_timestamps(duration_seconds=10, profile=profile),
            (0.0, 10 / 3, 20 / 3, 10.0),
        )

    def test_perceptual_hash_distance_requires_normalized_64_bit_hashes(self) -> None:
        self.assertEqual(perceptual_hash_distance("0" * 16, "f" * 16), 64)
        with self.assertRaises(DomainValidationError):
            perceptual_hash_distance("A" * 16, "0" * 16)

    def test_sampling_result_rejects_invalid_persistence_evidence_shapes(self) -> None:
        cover = VideoFrameEvidence(timestamp_seconds=0, artifact=_artifact("0" * 16))
        representative = VideoFrameEvidence(timestamp_seconds=5, artifact=_artifact("f" * 16))

        result = VideoSamplingResult(
            duration_seconds=5,
            cover=cover,
            representative_frames=(representative,),
        )
        self.assertEqual(result.representative_frames, (representative,))
        with self.assertRaisesRegex(ValueError, "cover timestamp_seconds"):
            VideoSamplingResult(
                duration_seconds=5,
                cover=cover,
                representative_frames=(cover,),
            )
        with self.assertRaisesRegex(ValueError, "chronological"):
            VideoSamplingResult(
                duration_seconds=5,
                cover=None,
                representative_frames=(representative, cover),
            )
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            VideoSamplingResult(
                duration_seconds=4,
                cover=None,
                representative_frames=(representative,),
            )
        with self.assertRaisesRegex(TypeError, "NormalizedImageArtifact"):
            VideoFrameEvidence(timestamp_seconds=0, artifact=object())

    def test_sampling_keeps_cover_and_filters_near_duplicate_representative_frames(self) -> None:
        extractor = _FrameExtractor(duration_seconds=10)
        processor = _Processor(
            phashes={
                b"0.000000": "0" * 16,
                b"3.333333": "0000000000000001",
                b"6.666667": "f" * 16,
                b"10.000000": "fffffffffffffffe",
            }
        )
        profile = VideoSamplingProfile(
            min_candidate_frames=4,
            max_candidate_frames=4,
            max_representative_frames=2,
            min_temporal_gap_seconds=2,
            phash_distance_threshold=8,
            keep_cover=True,
        )

        result = asyncio.run(
            VideoFrameSamplingService(frame_extractor=extractor, image_processor=processor).sample(
                content=b"video",
                sampling_profile=profile,
                image_profile=ImageNormalizationProfile(max_side_pixels=100, quality=80),
            )
        )

        self.assertEqual(result.duration_seconds, 10)
        self.assertIsNotNone(result.cover)
        assert result.cover is not None
        self.assertEqual(result.cover.timestamp_seconds, 0)
        self.assertEqual(
            tuple(frame.timestamp_seconds for frame in result.representative_frames),
            (20 / 3,),
        )
        self.assertEqual(extractor.extracted_timestamps, [0.0, 10 / 3, 20 / 3, 10.0])

    def test_sampling_without_cover_can_retain_the_first_frame_as_representative_evidence(
        self,
    ) -> None:
        extractor = _FrameExtractor(duration_seconds=4)
        processor = _Processor(
            phashes={
                b"0.000000": "0" * 16,
                b"4.000000": "f" * 16,
            }
        )
        profile = VideoSamplingProfile(
            min_candidate_frames=2,
            max_candidate_frames=2,
            max_representative_frames=2,
            min_temporal_gap_seconds=1,
            keep_cover=False,
        )

        result = asyncio.run(
            VideoFrameSamplingService(frame_extractor=extractor, image_processor=processor).sample(
                content=b"video",
                sampling_profile=profile,
                image_profile=ImageNormalizationProfile(max_side_pixels=100, quality=80),
            )
        )

        self.assertIsNone(result.cover)
        self.assertEqual(
            tuple(frame.timestamp_seconds for frame in result.representative_frames), (0.0, 4.0)
        )

    def test_sampling_rejects_empty_video_and_invalid_duration_before_frame_extraction(
        self,
    ) -> None:
        service = VideoFrameSamplingService(
            frame_extractor=_FrameExtractor(duration_seconds=0),
            image_processor=_Processor(phashes={}),
        )
        profile = VideoSamplingProfile(min_candidate_frames=1, max_candidate_frames=1)
        image_profile = ImageNormalizationProfile(max_side_pixels=100, quality=80)

        with self.assertRaisesRegex(DomainValidationError, "video content"):
            asyncio.run(
                service.sample(content=b"", sampling_profile=profile, image_profile=image_profile)
            )
        with self.assertRaisesRegex(DomainValidationError, "duration_seconds"):
            asyncio.run(
                service.sample(
                    content=b"video", sampling_profile=profile, image_profile=image_profile
                )
            )
        self.assertEqual(service.frame_extractor.extracted_timestamps, [])


def _artifact(perceptual_hash: str) -> NormalizedImageArtifact:
    return NormalizedImageArtifact(
        content=b"normalized-frame",
        content_type="image/webp",
        width=10,
        height=10,
        source_sha256="a" * 64,
        archive_sha256="b" * 64,
        perceptual_hash=perceptual_hash,
    )


class _FrameExtractor:
    def __init__(self, *, duration_seconds: float) -> None:
        self.duration_seconds = duration_seconds
        self.extracted_timestamps: list[float] = []

    async def probe_duration(self, *, content: bytes) -> float:
        return self.duration_seconds

    async def extract_frame(self, *, content: bytes, timestamp_seconds: float) -> bytes:
        self.extracted_timestamps.append(timestamp_seconds)
        return f"{timestamp_seconds:.6f}".encode()


class _Processor:
    def __init__(self, *, phashes: dict[bytes, str]) -> None:
        self._phashes = phashes

    async def normalize(
        self, *, content: bytes, profile: ImageNormalizationProfile
    ) -> NormalizedImageArtifact:
        return NormalizedImageArtifact(
            content=b"normalized-" + content,
            content_type="image/webp",
            width=10,
            height=10,
            source_sha256="a" * 64,
            archive_sha256="b" * 64,
            perceptual_hash=self._phashes[content],
        )


if __name__ == "__main__":
    unittest.main()
