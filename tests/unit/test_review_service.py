from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID

from tgcurator.application.review import ReviewService
from tgcurator.domain.review import (
    ManualLabelEvent,
    ManualLabelOperation,
    MessageReviewEvent,
    ReviewStatus,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
MESSAGE_ID = "11111111-1111-4111-8111-111111111111"
LABEL_VERSION_ID = "22222222-2222-4222-8222-222222222222"
ADMIN_ID = "33333333-3333-4333-8333-333333333333"
EVENT_IDS = (
    UUID("44444444-4444-4444-8444-444444444441"),
    UUID("44444444-4444-4444-8444-444444444442"),
    UUID("44444444-4444-4444-8444-444444444443"),
)


class FakeReviewRepository:
    def __init__(self) -> None:
        self.manual_events: list[ManualLabelEvent] = []
        self.review_events: list[MessageReviewEvent] = []
        self.transition_arguments: dict[str, object] | None = None

    async def append_manual_label(self, *, event: ManualLabelEvent) -> ManualLabelEvent:
        self.manual_events.append(event)
        return event

    async def transition_review_status(self, **arguments: object) -> MessageReviewEvent:
        self.transition_arguments = arguments
        event = MessageReviewEvent(
            event_id=str(arguments["event_id"]),
            message_id=str(arguments["message_id"]),
            old_status=ReviewStatus.UNREVIEWED,
            new_status=arguments["new_status"],  # type: ignore[arg-type]
            actor_admin_user_id=str(arguments["actor_admin_user_id"]),
            created_at=arguments["created_at"],  # type: ignore[arg-type]
            reason=arguments["reason"],  # type: ignore[arg-type]
        )
        self.review_events.append(event)
        return event

    async def list_manual_label_history(self, *, message_id: str) -> tuple[ManualLabelEvent, ...]:
        return tuple(event for event in self.manual_events if event.message_id == message_id)

    async def list_review_history(self, *, message_id: str) -> tuple[MessageReviewEvent, ...]:
        return tuple(event for event in self.review_events if event.message_id == message_id)


class ReviewServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_clear_and_status_transition_create_explicit_append_only_events(self) -> None:
        repository = FakeReviewRepository()
        identifiers = iter(EVENT_IDS)
        service = ReviewService(
            repository,  # type: ignore[arg-type]
            clock=lambda: NOW,
            id_factory=lambda: next(identifiers),
        )

        set_event = await service.set_label(
            message_id=MESSAGE_ID,
            target_scope="global",
            target_kind="message",
            target_id=MESSAGE_ID,
            label_definition_version_id=LABEL_VERSION_ID,
            label_key="real_ugc",
            score=0.91,
            activated=True,
            actor_admin_user_id=ADMIN_ID,
            reason="confirmed",
        )
        clear_event = await service.clear_label(
            message_id=MESSAGE_ID,
            target_scope="global",
            target_kind="message",
            target_id=MESSAGE_ID,
            label_definition_version_id=LABEL_VERSION_ID,
            label_key="real_ugc",
            actor_admin_user_id=ADMIN_ID,
        )
        review_event = await service.transition_status(
            message_id=MESSAGE_ID,
            new_status=ReviewStatus.REVIEWED,
            actor_admin_user_id=ADMIN_ID,
            reason="finished",
        )

        self.assertEqual(set_event.operation, ManualLabelOperation.SET)
        self.assertEqual(set_event.score, 0.91)
        self.assertTrue(set_event.activated)
        self.assertEqual(clear_event.operation, ManualLabelOperation.CLEAR)
        self.assertIsNone(clear_event.score)
        self.assertIsNone(clear_event.activated)
        self.assertEqual(review_event.new_status, ReviewStatus.REVIEWED)
        self.assertEqual(review_event.created_at, NOW)
        self.assertEqual(
            [event.event_id for event in repository.manual_events],
            [str(EVENT_IDS[0]), str(EVENT_IDS[1])],
        )
        self.assertEqual(review_event.event_id, str(EVENT_IDS[2]))
        self.assertEqual(
            await service.manual_label_history(message_id=MESSAGE_ID),
            (set_event, clear_event),
        )
        self.assertEqual(await service.review_history(message_id=MESSAGE_ID), (review_event,))


if __name__ == "__main__":
    unittest.main()
