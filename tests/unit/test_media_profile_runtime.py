"""Media-profile runtime contract: real worker control flow and frozen resolver policy."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import wraps
from types import SimpleNamespace as NS
from uuid import UUID

import pytest
import test_image_archive_worker as images
import test_video_archive_worker as videos

from tgcurator.application.media.videos import VideoFrameSamplingService, VideoSamplingProfile
from tgcurator.application.ports.media import (
    ImageNormalizationProfile,
    MediaProfileResolution,
    MediaProfileResolutionError,
    NormalizedImageArtifact,
)
from tgcurator.infrastructure.database.media_profile_repository import (
    SqlAlchemyMediaProfileResolver,
    _profiles_from_content,
)

_SOURCE_ID = UUID(int=1)
_MESSAGE_ID = UUID(int=3)
_MEDIA_ID = str(UUID(int=50))


def run_async(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return run


class Resolver:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def resolve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def configured_resolution():
    return MediaProfileResolution(
        image_profile=ImageNormalizationProfile(max_side_pixels=640, quality=67),
        sampling_profile=VideoSamplingProfile(
            min_candidate_frames=2, max_candidate_frames=2, max_representative_frames=1
        ),
        media_profile_version_id=str(UUID(int=50)),
    )


def make_worker(kind, resolver):
    if kind == "image":
        repo = images._Repository(images._claim())
        downloader = images._Downloader(content=b"image-source")
        worker = images._worker(repository=repo, downloader=downloader, storage=images._Storage())

        class Processor(images._Processor):
            def __init__(self):
                self.profiles = []

            async def normalize(self, *, content, profile):
                self.profiles.append(profile)
                return await super().normalize(content=content, profile=profile)

        processor = Processor()
        worker.archive_service.image_processor = processor
        captured = processor.profiles
        key = "image_asset_id"
    else:
        repo = videos._Repository(videos._claim())
        downloader = videos._Downloader(content=b"video-source")

        class Sampling(videos._SamplingService):
            def __init__(self):
                super().__init__()
                self.profiles = []

            async def sample(self, *, content, sampling_profile, image_profile, **kwargs):
                self.profiles.append((image_profile, sampling_profile))
                return await super().sample(content=content)

        sampling = Sampling()
        worker = videos._worker(repository=repo, downloader=downloader, sampling=sampling)
        captured = sampling.profiles
        key = "video_asset_id"
    worker.profile_resolver = resolver
    return worker, repo, downloader, captured, {key: repo.asset_id, "now": images.NOW}


@run_async
@pytest.mark.parametrize("kind", ["image", "video"])
async def test_workers_consume_frozen_parameters_for_exact_claim_component(kind):
    resolution = configured_resolution()
    resolver = Resolver(result=resolution)
    worker, repo, downloader, captured, args = make_worker(kind, resolver)
    assert await worker.process(**args)
    assert len(repo.ready) == 1 and not repo.failures
    claim = repo.claim_result.work_item
    assert resolver.calls == [
        {
            "source_channel_id": claim.source_channel_id,
            "source_telegram_message_id": claim.source_telegram_message_id,
        }
    ]
    expected = (
        resolution.image_profile
        if kind == "image"
        else (resolution.image_profile, resolution.sampling_profile)
    )
    assert captured == [expected]
    assert len(downloader.requests) == 1


@run_async
@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("reason", ["NO_FROZEN_RANGE_EXECUTION", "MEDIA_PROFILE_UNCONFIGURED"])
async def test_only_explicit_unconfigured_resolution_preserves_worker_defaults(
    kind, reason, caplog
):
    resolver = Resolver(result=MediaProfileResolution(fallback_reason=reason))
    worker, repo, _, captured, args = make_worker(kind, resolver)
    expected = (
        worker.normalization_profile
        if kind == "image"
        else (worker.image_profile, worker.sampling_profile)
    )
    with caplog.at_level("INFO"):
        assert await worker.process(**args)
    assert captured == [expected]
    assert reason in caplog.text
    assert not repo.failures


@run_async
@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize(
    "error,code,retryable",
    [
        (
            MediaProfileResolutionError("MEDIA_PROFILE_VERSION_MISSING"),
            "MEDIA_PROFILE_VERSION_MISSING",
            False,
        ),
        (
            MediaProfileResolutionError("MEDIA_PROFILE_CONTENT_INVALID"),
            "MEDIA_PROFILE_CONTENT_INVALID",
            False,
        ),
        (
            MediaProfileResolutionError("MEDIA_PROFILE_FROZEN_CONFLICT"),
            "MEDIA_PROFILE_FROZEN_CONFLICT",
            False,
        ),
        (
            RuntimeError("postgres://secret-password@private"),
            "MEDIA_PROFILE_RESOLUTION_FAILED",
            True,
        ),
    ],
)
async def test_resolution_error_settles_claim_before_any_external_media_io(
    kind, error, code, retryable
):
    worker, repo, downloader, captured, args = make_worker(kind, Resolver(error=error))
    assert await worker.process(**args)
    assert repo.failures[0][:3] == (code, "MediaProfileResolutionError", retryable)
    assert not repo.ready and not downloader.requests and not captured
    assert "secret-password" not in repr(repo.failures)


class Database:
    def __init__(self, snapshots, version=None, message=_MESSAGE_ID):
        self.snapshots = snapshots
        self.version = version
        self.message = message
        self.queries = []

    @asynccontextmanager
    async def session(self):
        owner = self

        class Session:
            async def scalar(self, statement):
                owner.queries.append(statement)
                return owner.message if len(owner.queries) == 1 else owner.version

            async def execute(self, statement):
                owner.queries.append(statement)
                return owner.snapshots

        yield Session()


def snapshot(media_id=_MEDIA_ID, *, state="published", source_id=_SOURCE_ID):
    content = {} if media_id is None else {"media_processing_profile_version_id": media_id}
    return (
        UUID(int=10),
        NS(id=UUID(int=20), state=state, published_at=images.NOW, content=content),
        source_id,
    )


@run_async
async def test_resolver_loads_immutable_versions_and_never_queries_active_profile():
    db = Database(
        [snapshot()],
        NS(
            state="archived",
            published_at=images.NOW,
            content={"image_max_dimension": 640, "webp_quality": 67, "video_max_frames": 2},
        ),
    )
    result = await SqlAlchemyMediaProfileResolver(db).resolve(
        source_channel_id=str(UUID(int=1)), source_telegram_message_id=101
    )
    assert result.image_profile == configured_resolution().image_profile
    assert result.sampling_profile.max_candidate_frames == 2
    assert result.media_profile_version_id == str(UUID(int=50))
    assert result.execution_ids == (str(UUID(int=10)),)
    assert all("active_profile_version_id" not in str(query) for query in db.queries)


@run_async
@pytest.mark.parametrize(
    "snapshots,version,code",
    [
        ([snapshot()], None, "MEDIA_PROFILE_VERSION_MISSING"),
        (
            [snapshot()],
            NS(state="draft", published_at=None, content={}),
            "MEDIA_PROFILE_VERSION_NOT_PUBLISHED",
        ),
        (
            [snapshot()],
            NS(state="published", published_at=images.NOW, content={"image_max_dimension": True}),
            "MEDIA_PROFILE_CONTENT_INVALID",
        ),
        ([snapshot(), snapshot(str(UUID(int=51)))], None, "MEDIA_PROFILE_FROZEN_CONFLICT"),
        ([snapshot(), snapshot(None)], None, "MEDIA_PROFILE_FROZEN_CONFLICT"),
        ([snapshot(state="draft")], None, "MEDIA_SOURCE_PROFILE_NOT_PUBLISHED"),
    ],
)
async def test_explicit_broken_or_ambiguous_versions_never_default(snapshots, version, code):
    resolver = SqlAlchemyMediaProfileResolver(Database(snapshots, version))
    with pytest.raises(MediaProfileResolutionError) as error:
        await resolver.resolve(source_channel_id=str(UUID(int=1)), source_telegram_message_id=101)
    assert error.value.error_code == code


@run_async
@pytest.mark.parametrize(
    "snapshots,reason",
    [([], "NO_FROZEN_RANGE_EXECUTION"), ([snapshot(None)], "MEDIA_PROFILE_UNCONFIGURED")],
)
async def test_resolver_fallback_is_explicit_and_does_not_load_current_source(snapshots, reason):
    db = Database(snapshots)
    result = await SqlAlchemyMediaProfileResolver(db).resolve(
        source_channel_id=str(UUID(int=1)), source_telegram_message_id=101
    )
    assert result.fallback_reason == reason and result.image_profile is None
    assert len(db.queries) == 2


def test_configured_empty_content_uses_admin_schema_defaults_not_worker_defaults():
    image, video = _profiles_from_content({})
    assert image == ImageNormalizationProfile(max_side_pixels=2048, quality=85)
    assert video.max_candidate_frames == 8
    assert video.max_representative_frames == 7 and video.keep_cover


@run_async
@pytest.mark.parametrize("limit", [1, 2, 8, 64])
async def test_video_max_frames_bounds_actual_candidate_decode_and_total_evidence(limit):
    from hashlib import sha256

    calls = []

    class Extractor:
        async def extract_frame(self, *, content, timestamp_seconds):
            calls.append(timestamp_seconds)
            return str(timestamp_seconds).encode()

    class Processor:
        async def normalize(self, *, content, profile):
            digest = sha256(content).hexdigest()
            return NormalizedImageArtifact(
                content=b"webp",
                content_type="image/webp",
                width=1,
                height=1,
                source_sha256=digest,
                archive_sha256=digest,
                perceptual_hash=digest[:16],
            )

    image, video = _profiles_from_content({"video_max_frames": limit})
    result = await VideoFrameSamplingService(Extractor(), Processor()).sample(
        content=b"fixture", sampling_profile=video, image_profile=image, duration_seconds=1000
    )
    assert len(calls) == limit
    assert result.cover is not None
    assert 1 + len(result.representative_frames) <= limit
    if limit == 1:
        assert result.representative_frames == ()
