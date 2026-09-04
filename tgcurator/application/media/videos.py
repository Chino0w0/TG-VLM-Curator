from __future__ import annotations

from dataclasses import dataclass
from math import ceil, isfinite

from tgcurator.application.ports.media import (
    ImageNormalizationProfile,
    ImageProcessor,
    NormalizedImageArtifact,
    VideoFrameExtractor,
)
from tgcurator.shared import DomainValidationError


@dataclass(frozen=True, slots=True)
class VideoSamplingProfile:
    """Deterministic limits for extracting a compact set of video visual evidence."""

    min_candidate_frames: int = 6
    max_candidate_frames: int = 24
    max_representative_frames: int = 6
    min_temporal_gap_seconds: float = 2.0
    phash_distance_threshold: int = 8
    keep_cover: bool = True

    def __post_init__(self) -> None:
        for field, value in (
            ("min_candidate_frames", self.min_candidate_frames),
            ("max_candidate_frames", self.max_candidate_frames),
            ("max_representative_frames", self.max_representative_frames),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if self.min_candidate_frames > self.max_candidate_frames:
            raise ValueError("min_candidate_frames must not exceed max_candidate_frames")
        if (
            not isinstance(self.min_temporal_gap_seconds, (int, float))
            or isinstance(self.min_temporal_gap_seconds, bool)
            or not isfinite(self.min_temporal_gap_seconds)
            or self.min_temporal_gap_seconds <= 0
        ):
            raise ValueError("min_temporal_gap_seconds must be a finite positive number")
        if (
            not isinstance(self.phash_distance_threshold, int)
            or isinstance(self.phash_distance_threshold, bool)
            or not 0 <= self.phash_distance_threshold <= 64
        ):
            raise ValueError("phash_distance_threshold must be an integer between 0 and 64")
        if not isinstance(self.keep_cover, bool):
            raise ValueError("keep_cover must be a boolean")


@dataclass(frozen=True, slots=True)
class VideoFrameEvidence:
    """One decoded video frame represented by a normalized image artifact."""

    timestamp_seconds: float
    artifact: NormalizedImageArtifact

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timestamp_seconds, (int, float))
            or isinstance(self.timestamp_seconds, bool)
            or not isfinite(self.timestamp_seconds)
            or self.timestamp_seconds < 0
        ):
            raise ValueError("timestamp_seconds must be a finite non-negative number")
        if not isinstance(self.artifact, NormalizedImageArtifact):
            raise TypeError("artifact must be a NormalizedImageArtifact")


@dataclass(frozen=True, slots=True)
class VideoSamplingResult:
    """In-memory evidence chosen for later durable video archive persistence."""

    duration_seconds: float
    cover: VideoFrameEvidence | None
    representative_frames: tuple[VideoFrameEvidence, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.duration_seconds, (int, float))
            or isinstance(self.duration_seconds, bool)
            or not isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be a finite positive number")
        if self.cover is not None:
            if not isinstance(self.cover, VideoFrameEvidence):
                raise TypeError("cover must be a VideoFrameEvidence or None")
            if self.cover.timestamp_seconds > self.duration_seconds:
                raise ValueError("cover timestamp_seconds must not exceed duration_seconds")
        if not isinstance(self.representative_frames, tuple):
            raise TypeError("representative_frames must be a tuple")
        if any(not isinstance(frame, VideoFrameEvidence) for frame in self.representative_frames):
            raise TypeError("representative_frames must contain VideoFrameEvidence values")
        timestamps = tuple(frame.timestamp_seconds for frame in self.representative_frames)
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("representative frame timestamps must be unique")
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("representative frame timestamps must be chronological")
        if any(timestamp > self.duration_seconds for timestamp in timestamps):
            raise ValueError("representative frame timestamps must not exceed duration_seconds")
        if self.cover is not None and self.cover.timestamp_seconds in timestamps:
            raise ValueError("cover timestamp_seconds must not appear in representative_frames")


def candidate_timestamps(
    *, duration_seconds: float, profile: VideoSamplingProfile
) -> tuple[float, ...]:
    """Evenly spread bounded sample times including the decoded cover at timestamp zero."""

    _validate_duration(duration_seconds)
    ideal_count = ceil(duration_seconds / profile.min_temporal_gap_seconds) + 1
    frame_count = min(profile.max_candidate_frames, max(profile.min_candidate_frames, ideal_count))
    if frame_count == 1:
        return (0.0,)
    return tuple(duration_seconds * index / (frame_count - 1) for index in range(frame_count))


def perceptual_hash_distance(left: str, right: str) -> int:
    """Return the Hamming distance for normalized 64-bit hexadecimal pHashes."""

    for field, value in (("left", left), ("right", right)):
        if (
            not isinstance(value, str)
            or len(value) != 16
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise DomainValidationError(
                f"{field} must be a 16-character lowercase hexadecimal pHash"
            )
    return (int(left, 16) ^ int(right, 16)).bit_count()


def select_representative_frames(
    *,
    candidates: tuple[VideoFrameEvidence, ...],
    profile: VideoSamplingProfile,
    cover: VideoFrameEvidence | None,
) -> tuple[VideoFrameEvidence, ...]:
    """Keep chronologically first, sufficiently separated visual evidence deterministically."""

    selected: list[VideoFrameEvidence] = []
    for candidate in sorted(candidates, key=lambda item: item.timestamp_seconds):
        if cover is not None and candidate.timestamp_seconds == cover.timestamp_seconds:
            continue
        existing = (cover, *selected) if cover is not None else tuple(selected)
        if any(
            abs(candidate.timestamp_seconds - item.timestamp_seconds)
            < profile.min_temporal_gap_seconds
            for item in existing
        ):
            continue
        if any(
            perceptual_hash_distance(
                candidate.artifact.perceptual_hash, item.artifact.perceptual_hash
            )
            < profile.phash_distance_threshold
            for item in existing
        ):
            continue
        selected.append(candidate)
        if len(selected) == profile.max_representative_frames:
            break
    return tuple(selected)


@dataclass(slots=True)
class VideoFrameSamplingService:
    """Probe and sample video bytes without retaining source video or invoking inference."""

    frame_extractor: VideoFrameExtractor
    image_processor: ImageProcessor

    async def sample(
        self,
        *,
        content: bytes,
        sampling_profile: VideoSamplingProfile,
        image_profile: ImageNormalizationProfile,
    ) -> VideoSamplingResult:
        if not isinstance(content, bytes) or not content:
            raise DomainValidationError("video content must not be empty")
        duration_seconds = await self.frame_extractor.probe_duration(content=content)
        _validate_duration(duration_seconds)
        evidence: list[VideoFrameEvidence] = []
        for timestamp_seconds in candidate_timestamps(
            duration_seconds=duration_seconds, profile=sampling_profile
        ):
            frame_content = await self.frame_extractor.extract_frame(
                content=content, timestamp_seconds=timestamp_seconds
            )
            if not isinstance(frame_content, bytes) or not frame_content:
                raise RuntimeError("video frame extractor returned empty frame content")
            evidence.append(
                VideoFrameEvidence(
                    timestamp_seconds=timestamp_seconds,
                    artifact=await self.image_processor.normalize(
                        content=frame_content, profile=image_profile
                    ),
                )
            )
        cover = evidence[0] if sampling_profile.keep_cover else None
        return VideoSamplingResult(
            duration_seconds=duration_seconds,
            cover=cover,
            representative_frames=select_representative_frames(
                candidates=tuple(evidence), profile=sampling_profile, cover=cover
            ),
        )


def _validate_duration(duration_seconds: float) -> None:
    if (
        not isinstance(duration_seconds, (int, float))
        or isinstance(duration_seconds, bool)
        or not isfinite(duration_seconds)
        or duration_seconds <= 0
    ):
        raise DomainValidationError("video duration_seconds must be a finite positive number")
