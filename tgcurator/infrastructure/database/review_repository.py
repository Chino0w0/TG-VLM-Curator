from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tgcurator.domain.review import (
    ManualLabelEvent,
    ManualLabelOperation,
    MessageReviewEvent,
    ReviewStatus,
)
from tgcurator.shared import DomainValidationError

from .models import (
    ImageAssetRecord,
    LabelDefinitionVersionRecord,
    ManualLabelAssignmentRecord,
    MessageRecord,
    MessageReviewEventRecord,
    VideoAssetRecord,
)
from .session import AsyncDatabase


class SqlAlchemyReviewRepository:
    """Persist append-only review history while locking mutable workflow state."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def append_manual_label(self, *, event: ManualLabelEvent) -> ManualLabelEvent:
        if not isinstance(event, ManualLabelEvent):
            raise DomainValidationError("event must be a ManualLabelEvent")

        message_id = _as_uuid(event.message_id, field="message_id")
        label_version_id = _as_uuid(
            event.label_definition_version_id,
            field="label_definition_version_id",
        )
        async with self._database.session() as session:
            async with session.begin():
                message = await _lock_message(session=session, message_id=message_id)
                label_version = await session.scalar(
                    select(LabelDefinitionVersionRecord).where(
                        LabelDefinitionVersionRecord.id == label_version_id
                    )
                )
                _validate_label_version(event=event, label_version=label_version)
                await _validate_media_target(
                    session=session,
                    event=event,
                    message_id=message.id,
                )
                session.add(
                    ManualLabelAssignmentRecord(
                        id=_as_uuid(event.event_id, field="event_id"),
                        message_id=message.id,
                        target_scope=event.target_scope,
                        target_kind=event.target_kind,
                        target_id=_as_uuid(event.target_id, field="target_id"),
                        image_asset_id=_optional_uuid(
                            event.image_asset_id,
                            field="image_asset_id",
                        ),
                        video_asset_id=_optional_uuid(
                            event.video_asset_id,
                            field="video_asset_id",
                        ),
                        label_definition_version_id=label_version_id,
                        label_key=event.label_key,
                        operation=event.operation.value,
                        score=(float(event.score) if event.score is not None else None),
                        activated=event.activated,
                        actor_admin_user_id=_as_uuid(
                            event.actor_admin_user_id,
                            field="actor_admin_user_id",
                        ),
                        reason=event.reason,
                        created_at=event.created_at,
                    )
                )
        return event

    async def transition_review_status(
        self,
        *,
        event_id: str,
        message_id: str,
        new_status: ReviewStatus,
        actor_admin_user_id: str,
        created_at: datetime,
        reason: str | None = None,
    ) -> MessageReviewEvent:
        if not isinstance(new_status, ReviewStatus):
            raise DomainValidationError("new_status must be a ReviewStatus")
        message_uuid = _as_uuid(message_id, field="message_id")
        async with self._database.session() as session:
            async with session.begin():
                message = await _lock_message(session=session, message_id=message_uuid)
                event = MessageReviewEvent(
                    event_id=event_id,
                    message_id=message_id,
                    old_status=ReviewStatus(message.review_status),
                    new_status=new_status,
                    actor_admin_user_id=actor_admin_user_id,
                    created_at=created_at,
                    reason=reason,
                )
                message.review_status = new_status.value
                message.updated_at = created_at
                session.add(
                    MessageReviewEventRecord(
                        id=_as_uuid(event.event_id, field="event_id"),
                        message_id=message.id,
                        old_status=event.old_status.value,
                        new_status=event.new_status.value,
                        actor_admin_user_id=_as_uuid(
                            event.actor_admin_user_id,
                            field="actor_admin_user_id",
                        ),
                        reason=event.reason,
                        created_at=event.created_at,
                    )
                )
        return event

    async def list_manual_label_history(self, *, message_id: str) -> tuple[ManualLabelEvent, ...]:
        message_uuid = _as_uuid(message_id, field="message_id")
        async with self._database.session() as session:
            rows = tuple(
                await session.scalars(
                    select(ManualLabelAssignmentRecord)
                    .where(ManualLabelAssignmentRecord.message_id == message_uuid)
                    .order_by(
                        ManualLabelAssignmentRecord.created_at,
                        ManualLabelAssignmentRecord.id,
                    )
                )
            )
        return tuple(_manual_event_from_record(row) for row in rows)

    async def list_review_history(self, *, message_id: str) -> tuple[MessageReviewEvent, ...]:
        message_uuid = _as_uuid(message_id, field="message_id")
        async with self._database.session() as session:
            rows = tuple(
                await session.scalars(
                    select(MessageReviewEventRecord)
                    .where(MessageReviewEventRecord.message_id == message_uuid)
                    .order_by(
                        MessageReviewEventRecord.created_at,
                        MessageReviewEventRecord.id,
                    )
                )
            )
        return tuple(_review_event_from_record(row) for row in rows)


async def _lock_message(*, session: AsyncSession, message_id: UUID) -> MessageRecord:
    message = await session.scalar(
        select(MessageRecord).where(MessageRecord.id == message_id).with_for_update()
    )
    if message is None:
        raise DomainValidationError("message does not exist")
    return message


def _validate_label_version(
    *,
    event: ManualLabelEvent,
    label_version: LabelDefinitionVersionRecord | None,
) -> None:
    if label_version is None:
        raise DomainValidationError("label definition version does not exist")
    if label_version.state != "published" or not label_version.enabled:
        raise DomainValidationError("manual labels require an enabled published label version")
    if label_version.key != event.label_key:
        raise DomainValidationError("label key does not match label definition version")
    if label_version.scope != event.target_scope:
        raise DomainValidationError("label scope does not match manual label target")


async def _validate_media_target(
    *,
    session: AsyncSession,
    event: ManualLabelEvent,
    message_id: UUID,
) -> None:
    if event.target_kind == "message":
        return
    target_id = _as_uuid(event.target_id, field="target_id")
    if event.target_kind == "image_asset":
        exists = await session.scalar(
            select(ImageAssetRecord.id).where(
                ImageAssetRecord.id == target_id,
                ImageAssetRecord.message_id == message_id,
            )
        )
    else:
        exists = await session.scalar(
            select(VideoAssetRecord.id).where(
                VideoAssetRecord.id == target_id,
                VideoAssetRecord.message_id == message_id,
            )
        )
    if exists is None:
        raise DomainValidationError("manual label media target does not belong to message")


def _manual_event_from_record(row: ManualLabelAssignmentRecord) -> ManualLabelEvent:
    return ManualLabelEvent(
        event_id=str(row.id),
        message_id=str(row.message_id),
        target_scope=row.target_scope,
        target_kind=row.target_kind,
        target_id=str(row.target_id),
        image_asset_id=(str(row.image_asset_id) if row.image_asset_id is not None else None),
        video_asset_id=(str(row.video_asset_id) if row.video_asset_id is not None else None),
        label_definition_version_id=str(row.label_definition_version_id),
        label_key=row.label_key,
        operation=ManualLabelOperation(row.operation),
        score=row.score,
        activated=row.activated,
        actor_admin_user_id=str(row.actor_admin_user_id),
        created_at=row.created_at,
        reason=row.reason,
    )


def _review_event_from_record(row: MessageReviewEventRecord) -> MessageReviewEvent:
    return MessageReviewEvent(
        event_id=str(row.id),
        message_id=str(row.message_id),
        old_status=ReviewStatus(row.old_status),
        new_status=ReviewStatus(row.new_status),
        actor_admin_user_id=str(row.actor_admin_user_id),
        created_at=row.created_at,
        reason=row.reason,
    )


def _as_uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainValidationError(f"{field} must be a UUID") from exc


def _optional_uuid(value: str | None, *, field: str) -> UUID | None:
    return None if value is None else _as_uuid(value, field=field)
