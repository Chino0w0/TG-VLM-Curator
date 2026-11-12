from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, or_, select

from tgcurator.application.admin.config_validation import validate_media_profile_content
from tgcurator.application.admin.errors import AdminValidationError
from tgcurator.application.media.videos import VideoSamplingProfile
from tgcurator.application.ports.media import (
    ImageNormalizationProfile,
    MediaProfileResolution,
    MediaProfileResolutionError,
)
from tgcurator.domain.processing import SourceChannelProfileConfig
from tgcurator.shared import DomainValidationError

from .models import (
    MediaProcessingProfileVersionRecord,
    MessagePartRecord,
    MessageRecord,
    ProcessingRangeRecord,
    RangeExecutionRecord,
    SourceChannelProfile,
    SourceChannelProfileVersion,
)
from .session import AsyncDatabase


class SqlAlchemyMediaProfileResolver:
    """Resolve a component's frozen execution policy before archive side effects.

    Every matching execution must agree on the media version, including a queued retry.
    Ignoring a pending execution could silently replace its explicit policy with defaults.
    Ordering by the current active source
    profile or arbitrarily choosing one overlapping window would corrupt shared assets.
    Retired/archived versions remain valid when they were previously published.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def resolve(
        self, *, source_channel_id: str, source_telegram_message_id: int
    ) -> MediaProfileResolution:
        try:
            source_uuid = UUID(source_channel_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise MediaProfileResolutionError("MEDIA_PROFILE_SOURCE_INVALID") from exc
        if (
            not isinstance(source_telegram_message_id, int)
            or isinstance(source_telegram_message_id, bool)
            or source_telegram_message_id <= 0
        ):
            raise MediaProfileResolutionError("MEDIA_PROFILE_COMPONENT_INVALID")

        async with self._database.session() as session:
            # Resolve the precise retained component, not the album's minimum Telegram ID.
            message_id = await session.scalar(
                select(MessageRecord.id).where(
                    MessageRecord.source_channel_id == source_uuid,
                    or_(
                        MessageRecord.primary_telegram_message_id == source_telegram_message_id,
                        exists().where(
                            MessagePartRecord.message_id == MessageRecord.id,
                            MessagePartRecord.source_channel_id == source_uuid,
                            MessagePartRecord.telegram_message_id == source_telegram_message_id,
                        ),
                    ),
                )
            )
            if message_id is None:
                raise MediaProfileResolutionError("MEDIA_PROFILE_MESSAGE_NOT_FOUND")

            snapshots = tuple(
                await session.execute(
                    select(
                        RangeExecutionRecord.id,
                        SourceChannelProfileVersion,
                        SourceChannelProfile.source_channel_id,
                    )
                    .join(
                        ProcessingRangeRecord,
                        ProcessingRangeRecord.id == RangeExecutionRecord.processing_range_id,
                    )
                    .join(
                        SourceChannelProfileVersion,
                        SourceChannelProfileVersion.id
                        == RangeExecutionRecord.source_profile_version_id,
                    )
                    .join(
                        SourceChannelProfile,
                        SourceChannelProfile.id == SourceChannelProfileVersion.profile_id,
                    )
                    .where(
                        ProcessingRangeRecord.source_channel_id == source_uuid,
                        RangeExecutionRecord.from_message_id_exclusive < source_telegram_message_id,
                        RangeExecutionRecord.to_message_id_inclusive >= source_telegram_message_id,
                    )
                    .order_by(RangeExecutionRecord.created_at, RangeExecutionRecord.id)
                )
            )
            if not snapshots:
                return MediaProfileResolution(fallback_reason="NO_FROZEN_RANGE_EXECUTION")

            media_versions: set[str | None] = set()
            source_versions: set[str] = set()
            execution_ids: list[str] = []
            for execution_id, source_version, profile_source_id in snapshots:
                if (
                    profile_source_id != source_uuid
                    or source_version.state not in {"published", "retired"}
                    or source_version.published_at is None
                ):
                    raise MediaProfileResolutionError("MEDIA_SOURCE_PROFILE_NOT_PUBLISHED")
                try:
                    config = SourceChannelProfileConfig.from_content(source_version.content)
                except (DomainValidationError, TypeError, ValueError) as exc:
                    raise MediaProfileResolutionError("MEDIA_SOURCE_PROFILE_INVALID") from exc
                media_versions.add(config.media_processing_profile_version_id)
                source_versions.add(str(source_version.id))
                execution_ids.append(str(execution_id))
            if len(media_versions) != 1:
                raise MediaProfileResolutionError("MEDIA_PROFILE_FROZEN_CONFLICT")
            media_version_id = media_versions.pop()
            metadata = dict(
                source_profile_version_ids=tuple(sorted(source_versions)),
                execution_ids=tuple(execution_ids),
            )
            if media_version_id is None:
                return MediaProfileResolution(
                    fallback_reason="MEDIA_PROFILE_UNCONFIGURED",
                    **metadata,
                )

            version = await session.scalar(
                select(MediaProcessingProfileVersionRecord).where(
                    MediaProcessingProfileVersionRecord.id == UUID(media_version_id)
                )
            )
            if version is None:
                raise MediaProfileResolutionError("MEDIA_PROFILE_VERSION_MISSING")
            if version.state not in {"published", "archived"} or version.published_at is None:
                raise MediaProfileResolutionError("MEDIA_PROFILE_VERSION_NOT_PUBLISHED")
            image_profile, sampling_profile = _profiles_from_content(version.content)
            return MediaProfileResolution(
                image_profile=image_profile,
                sampling_profile=sampling_profile,
                media_profile_version_id=media_version_id,
                **metadata,
            )


def _profiles_from_content(content: dict) -> tuple[ImageNormalizationProfile, VideoSamplingProfile]:
    """Use the exact admin schema; video_max_frames bounds total decoding and evidence."""
    try:
        validate_media_profile_content(content)
        frame_limit = content.get("video_max_frames", 8)
        image_profile = ImageNormalizationProfile(
            max_side_pixels=content.get("image_max_dimension", 2048),
            quality=content.get("webp_quality", 85),
        )
        sampling_profile = VideoSamplingProfile(
            min_candidate_frames=min(6, frame_limit),
            max_candidate_frames=frame_limit,
            # The cover is one of the candidates. At limit=1 no representative exists,
            # even though the sampling value type requires a positive representative cap.
            max_representative_frames=max(1, frame_limit - 1),
            keep_cover=True,
        )
    except (AdminValidationError, TypeError, ValueError) as exc:
        raise MediaProfileResolutionError("MEDIA_PROFILE_CONTENT_INVALID") from exc
    return image_profile, sampling_profile
