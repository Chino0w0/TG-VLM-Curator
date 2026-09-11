from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from tgcurator.application.ports.review import ReviewRepository
from tgcurator.domain.review import (
    ManualLabelEvent,
    ManualLabelOperation,
    MessageReviewEvent,
    ReviewStatus,
)


class ReviewService:
    """Coordinate append-only manual labels and explicit review workflow state."""

    def __init__(
        self,
        repository: ReviewRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4

    async def set_label(
        self,
        *,
        message_id: str,
        target_scope: str,
        target_kind: str,
        target_id: str,
        label_definition_version_id: str,
        label_key: str,
        score: float,
        activated: bool,
        actor_admin_user_id: str,
        image_asset_id: str | None = None,
        video_asset_id: str | None = None,
        reason: str | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ManualLabelEvent:
        return await self._append_label(
            event_id=event_id,
            message_id=message_id,
            target_scope=target_scope,
            target_kind=target_kind,
            target_id=target_id,
            image_asset_id=image_asset_id,
            video_asset_id=video_asset_id,
            label_definition_version_id=label_definition_version_id,
            label_key=label_key,
            operation=ManualLabelOperation.SET,
            score=score,
            activated=activated,
            actor_admin_user_id=actor_admin_user_id,
            reason=reason,
            created_at=created_at,
        )

    async def clear_label(
        self,
        *,
        message_id: str,
        target_scope: str,
        target_kind: str,
        target_id: str,
        label_definition_version_id: str,
        label_key: str,
        actor_admin_user_id: str,
        image_asset_id: str | None = None,
        video_asset_id: str | None = None,
        reason: str | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ManualLabelEvent:
        return await self._append_label(
            event_id=event_id,
            message_id=message_id,
            target_scope=target_scope,
            target_kind=target_kind,
            target_id=target_id,
            image_asset_id=image_asset_id,
            video_asset_id=video_asset_id,
            label_definition_version_id=label_definition_version_id,
            label_key=label_key,
            operation=ManualLabelOperation.CLEAR,
            score=None,
            activated=None,
            actor_admin_user_id=actor_admin_user_id,
            reason=reason,
            created_at=created_at,
        )

    async def transition_status(
        self,
        *,
        message_id: str,
        new_status: ReviewStatus,
        actor_admin_user_id: str,
        reason: str | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> MessageReviewEvent:
        return await self._repository.transition_review_status(
            event_id=event_id or str(self._id_factory()),
            message_id=message_id,
            new_status=new_status,
            actor_admin_user_id=actor_admin_user_id,
            created_at=created_at or self._clock(),
            reason=reason,
        )

    async def manual_label_history(self, *, message_id: str) -> tuple[ManualLabelEvent, ...]:
        return await self._repository.list_manual_label_history(message_id=message_id)

    async def review_history(self, *, message_id: str) -> tuple[MessageReviewEvent, ...]:
        return await self._repository.list_review_history(message_id=message_id)

    async def _append_label(
        self,
        *,
        event_id: str | None,
        message_id: str,
        target_scope: str,
        target_kind: str,
        target_id: str,
        image_asset_id: str | None,
        video_asset_id: str | None,
        label_definition_version_id: str,
        label_key: str,
        operation: ManualLabelOperation,
        score: float | None,
        activated: bool | None,
        actor_admin_user_id: str,
        reason: str | None,
        created_at: datetime | None,
    ) -> ManualLabelEvent:
        event = ManualLabelEvent(
            event_id=event_id or str(self._id_factory()),
            message_id=message_id,
            target_scope=target_scope,
            target_kind=target_kind,
            target_id=target_id,
            image_asset_id=image_asset_id,
            video_asset_id=video_asset_id,
            label_definition_version_id=label_definition_version_id,
            label_key=label_key,
            operation=operation,
            score=score,
            activated=activated,
            actor_admin_user_id=actor_admin_user_id,
            created_at=created_at or self._clock(),
            reason=reason,
        )
        return await self._repository.append_manual_label(event=event)
