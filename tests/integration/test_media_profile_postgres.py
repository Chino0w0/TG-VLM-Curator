"""Native opt-in tests for M10 frozen media-profile runtime resolution."""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from PIL import Image
from postgres_support import temporary_postgres_database
from sqlalchemy import select, update

from tgcurator.application.media.archive_worker import ImageArchiveWorker
from tgcurator.application.media.images import ImageArchiveService
from tgcurator.application.media.video_archive_worker import VideoArchiveWorker
from tgcurator.application.media.videos import VideoFrameSamplingService, VideoSamplingProfile
from tgcurator.application.ports.media import (
    ImageNormalizationProfile,
    MediaProfileResolutionError,
    VideoProbeMetadata,
)
from tgcurator.infrastructure.database.image_archive_repository import (
    SqlAlchemyImageArchiveWorkRepository,
)
from tgcurator.infrastructure.database.media_profile_repository import (
    SqlAlchemyMediaProfileResolver,
)
from tgcurator.infrastructure.database.models import (
    DurableWakeupRecord,
    EncryptedSecret,
    ImageAssetRecord,
    MediaProcessingProfileRecord,
    MediaProcessingProfileVersionRecord,
    MessagePartRecord,
    MessageRecord,
    ProcessingRangeRecord,
    RangeExecutionRecord,
    SourceChannel,
    SourceChannelProfile,
    SourceChannelProfileVersion,
    TelegramIdentity,
    VideoAssetRecord,
    VideoFrameRecord,
)
from tgcurator.infrastructure.database.session import AsyncDatabase
from tgcurator.infrastructure.database.video_archive_repository import (
    SqlAlchemyVideoArchiveWorkRepository,
)
from tgcurator.infrastructure.media.pillow_processor import PillowImageProcessor

NOW = datetime(2026, 11, 12, tzinfo=UTC)


async def seed(db, *, broken=False, with_execution=True):
    ids = {
        key: uuid4()
        for key in (
            "secret",
            "identity",
            "source",
            "source_profile",
            "source_v1",
            "source_v2",
            "media",
            "media_v1",
            "media_v2",
            "message",
            "image",
            "video",
        )
    }
    async with db.session() as s:
        async with s.begin():
            s.add(
                EncryptedSecret(
                    id=ids["secret"],
                    secret_type="test",
                    ciphertext=b"0" * 16,
                    nonce=b"0" * 12,
                    key_id="test",
                )
            )
            await s.flush()
            s.add(
                TelegramIdentity(
                    id=ids["identity"],
                    name="identity",
                    identity_type="bot",
                    secret_id=ids["secret"],
                )
            )
            await s.flush()
            s.add(
                SourceChannel(
                    id=ids["source"],
                    telegram_peer_id=111,
                    display_name="source",
                    default_read_identity_id=ids["identity"],
                )
            )
            s.add(MediaProcessingProfileRecord(id=ids["media"], name="media"))
            await s.flush()
            s.add(
                SourceChannelProfile(
                    id=ids["source_profile"], source_channel_id=ids["source"], name="source-profile"
                )
            )
            for n in (1, 2):
                s.add(
                    MediaProcessingProfileVersionRecord(
                        id=ids[f"media_v{n}"],
                        media_processing_profile_id=ids["media"],
                        version_number=n,
                        content={
                            "image_max_dimension": 640 if n == 1 else 1280,
                            "webp_quality": 67 if n == 1 else 90,
                            "video_max_frames": 2 if n == 1 else 8,
                        },
                        content_hash=str(n) * 64,
                    )
                )
            await s.flush()
            for n in (1, 2):
                s.add(
                    SourceChannelProfileVersion(
                        id=ids[f"source_v{n}"],
                        profile_id=ids["source_profile"],
                        version_number=n,
                        content={
                            "media_processing_profile_version_id": str(
                                uuid4() if broken and n == 1 else ids[f"media_v{n}"]
                            )
                        },
                        content_hash=str(n) * 64,
                    )
                )
            await s.flush()
            await s.execute(
                update(MediaProcessingProfileVersionRecord)
                .where(MediaProcessingProfileVersionRecord.id == ids["media_v1"])
                .values(state="published", published_at=NOW)
            )
            await s.execute(
                update(SourceChannelProfileVersion)
                .where(SourceChannelProfileVersion.id == ids["source_v1"])
                .values(state="published", published_at=NOW)
            )
            await s.execute(
                update(SourceChannel)
                .where(SourceChannel.id == ids["source"])
                .values(active_profile_version_id=ids["source_v1"])
            )
            s.add(
                MessageRecord(
                    id=ids["message"],
                    source_channel_id=ids["source"],
                    telegram_message_ids=[100, 101],
                    telegram_grouped_id=999,
                    primary_telegram_message_id=100,
                    published_at=NOW,
                    media_count=1,
                )
            )
            await s.flush()
            for tg in (100, 101):
                s.add(
                    MessagePartRecord(
                        id=uuid4(),
                        message_id=ids["message"],
                        source_channel_id=ids["source"],
                        telegram_message_id=tg,
                        published_at=NOW,
                        media=[],
                        media_count=0,
                    )
                )
            s.add(
                ImageAssetRecord(
                    id=ids["image"],
                    message_id=ids["message"],
                    source_asset_id="component101",
                    source_telegram_message_id=101,
                    archive_state="pending",
                )
            )
            s.add(
                DurableWakeupRecord(
                    id=uuid4(),
                    queue="image_archive",
                    entity_id=ids["image"],
                    status="pending",
                    next_attempt_at=NOW,
                )
            )
            s.add(
                VideoAssetRecord(
                    id=ids["video"],
                    message_id=ids["message"],
                    source_asset_id="video101",
                    source_telegram_message_id=101,
                    archive_state="pending",
                )
            )
            s.add(
                DurableWakeupRecord(
                    id=uuid4(),
                    queue="video_archive",
                    entity_id=ids["video"],
                    status="pending",
                    next_attempt_at=NOW,
                )
            )
    if with_execution:
        ids["execution"] = await add_execution(db, ids, ids["source_v1"])
    return ids


async def add_execution(db, ids, profile_version):
    rid, eid = uuid4(), uuid4()
    async with db.session() as s:
        async with s.begin():
            s.add(
                ProcessingRangeRecord(
                    id=rid,
                    source_channel_id=ids["source"],
                    start_at=NOW,
                    end_mode="latest",
                    steady_after_seconds=1,
                    status="active",
                )
            )
            await s.flush()
            s.add(
                RangeExecutionRecord(
                    id=eid,
                    processing_range_id=rid,
                    source_profile_version_id=profile_version,
                    from_message_id_exclusive=100,
                    to_message_id_inclusive=101,
                    watermark_message_id=100,
                    status="awaiting_pipeline",
                )
            )
    return eid


async def publish_new_active(db, ids):
    async with db.session() as s:
        async with s.begin():
            await s.execute(
                update(SourceChannelProfileVersion)
                .where(SourceChannelProfileVersion.id == ids["source_v1"])
                .values(state="retired")
            )
            await s.execute(
                update(MediaProcessingProfileVersionRecord)
                .where(MediaProcessingProfileVersionRecord.id == ids["media_v1"])
                .values(state="archived", archived_at=NOW)
            )
            await s.execute(
                update(SourceChannelProfileVersion)
                .where(SourceChannelProfileVersion.id == ids["source_v2"])
                .values(state="published", published_at=NOW)
            )
            await s.execute(
                update(MediaProcessingProfileVersionRecord)
                .where(MediaProcessingProfileVersionRecord.id == ids["media_v2"])
                .values(state="published", published_at=NOW)
            )
            await s.execute(
                update(SourceChannel)
                .where(SourceChannel.id == ids["source"])
                .values(active_profile_version_id=ids["source_v2"])
            )


class Storage:
    def __init__(self):
        self.objects = {}

    async def put(self, *, key, content, content_type):
        self.objects[key] = content
        return len(content)

    async def size(self, *, key):
        return len(self.objects[key])


class Downloader:
    def __init__(self):
        self.calls = []

    async def download(self, *, request):
        self.calls.append(request)
        out = io.BytesIO()
        Image.new("RGB", (1600, 800), "green").save(out, format="PNG")
        return out.getvalue()


def worker(db, resolver, downloader):
    return ImageArchiveWorker(
        repository=SqlAlchemyImageArchiveWorkRepository(db),
        media_downloader=downloader,
        archive_service=ImageArchiveService(PillowImageProcessor(), Storage()),
        normalization_profile=ImageNormalizationProfile(max_side_pixels=200, quality=85),
        storage_backend="test-memory",
        profile_resolver=resolver,
    )


class Extractor:
    def __init__(self):
        self.calls = []

    async def extract_frame(self, *, content, timestamp_seconds):
        self.calls.append(timestamp_seconds)
        output = io.BytesIO()
        Image.new("RGB", (1600, 800), "green").save(output, format="PNG")
        return output.getvalue()


class Probe:
    async def probe(self, *, content):
        return VideoProbeMetadata(
            duration_seconds=12, width=1600, height=800, content_type="video/mp4"
        )


def video_worker(db, resolver, downloader, extractor=None):
    return VideoArchiveWorker(
        repository=SqlAlchemyVideoArchiveWorkRepository(db),
        media_downloader=downloader,
        metadata_probe=Probe(),
        sampling_service=VideoFrameSamplingService(
            extractor or Extractor(), PillowImageProcessor()
        ),
        archive_storage=Storage(),
        sampling_profile=VideoSamplingProfile(),
        image_profile=ImageNormalizationProfile(max_side_pixels=200, quality=85),
        storage_backend="test-memory",
        profile_resolver=resolver,
    )


def test_frozen_execution_profile_survives_active_switch_and_real_image_normalization():
    with temporary_postgres_database("m7_media_frozen") as url:

        async def scenario():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db)
                resolver = SqlAlchemyMediaProfileResolver(db)
                args = dict(source_channel_id=str(ids["source"]), source_telegram_message_id=101)
                before = await resolver.resolve(**args)
                await publish_new_active(db, ids)
                after = await resolver.resolve(**args)
                assert after == before
                assert after.media_profile_version_id == str(ids["media_v1"])
                assert after.image_profile.max_side_pixels == 640
                assert after.sampling_profile.max_candidate_frames == 2
                assert after.execution_ids == (str(ids["execution"]),)
                downloader = Downloader()
                assert await worker(db, resolver, downloader).process(
                    image_asset_id=str(ids["image"]), now=NOW
                )
                async with db.session() as s:
                    image = await s.get(ImageAssetRecord, ids["image"])
                    assert (
                        image.archive_state == "ready"
                        and image.width == 640
                        and image.height == 320
                    )
                    assert image.archive_lease_token is None
                assert len(downloader.calls) == 1 and downloader.calls[0].telegram_message_id == 101
                extractor = Extractor()
                assert await video_worker(db, resolver, Downloader(), extractor).process(
                    video_asset_id=str(ids["video"]),
                    now=NOW,
                )
                assert len(extractor.calls) == 2
                async with db.session() as s:
                    video = await s.get(VideoAssetRecord, ids["video"])
                    assert video.archive_state == "ready" and video.archive_lease_token is None
                    frames = tuple(
                        await s.scalars(
                            select(VideoFrameRecord).where(
                                VideoFrameRecord.video_asset_id == ids["video"]
                            )
                        )
                    )
                    assert 1 <= len(frames) <= 2
                    assert all(frame.width == 640 and frame.height == 320 for frame in frames)
            finally:
                await db.dispose()

        asyncio.run(scenario())


@pytest.mark.parametrize("transient", [False, True])
@pytest.mark.parametrize("kind", ["image", "video"])
def test_profile_failures_persist_archive_failure_or_retry_and_release_lease(transient, kind):
    with temporary_postgres_database("m7_media_failure") as url:

        async def scenario():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db, broken=not transient)
                if transient:

                    class Resolver:
                        async def resolve(self, **kwargs):
                            raise RuntimeError("private connection string")

                    resolver = Resolver()
                else:
                    resolver = SqlAlchemyMediaProfileResolver(db)
                downloader = Downloader()
                archive_worker = (
                    worker(db, resolver, downloader)
                    if kind == "image"
                    else (video_worker(db, resolver, downloader))
                )
                assert await archive_worker.process(
                    **{f"{kind}_asset_id": str(ids[kind]), "now": NOW}
                )
                async with db.session() as s:
                    image = await s.get(
                        ImageAssetRecord if kind == "image" else VideoAssetRecord, ids[kind]
                    )
                    assert image.archive_state == ("retry_wait" if transient else "failed")
                    assert (
                        image.archive_lease_token is None and image.archive_lease_expires_at is None
                    )
                    assert image.archive_last_error_code == (
                        "MEDIA_PROFILE_RESOLUTION_FAILED"
                        if transient
                        else "MEDIA_PROFILE_VERSION_MISSING"
                    )
                    assert "private" not in image.archive_last_error_type
                    if transient:
                        wakeup = await s.scalar(
                            select(DurableWakeupRecord).where(
                                DurableWakeupRecord.entity_id == ids[kind]
                            )
                        )
                        assert wakeup.status == "pending"
                assert not downloader.calls
            finally:
                await db.dispose()

        asyncio.run(scenario())


def test_overlapping_frozen_media_versions_fail_closed_and_no_range_never_uses_active():
    with temporary_postgres_database("m7_media_conflict") as url:

        async def scenario():
            db = AsyncDatabase(url)
            try:
                ids = await seed(db, with_execution=False)
                resolver = SqlAlchemyMediaProfileResolver(db)
                args = dict(source_channel_id=str(ids["source"]), source_telegram_message_id=101)
                result = await resolver.resolve(**args)
                assert result.fallback_reason == "NO_FROZEN_RANGE_EXECUTION"
                assert result.media_profile_version_id is None
                await add_execution(db, ids, ids["source_v1"])
                await publish_new_active(db, ids)
                await add_execution(db, ids, ids["source_v2"])
                with pytest.raises(MediaProfileResolutionError) as error:
                    await resolver.resolve(**args)
                assert error.value.error_code == "MEDIA_PROFILE_FROZEN_CONFLICT"
                assert not error.value.retryable
            finally:
                await db.dispose()

        asyncio.run(scenario())
