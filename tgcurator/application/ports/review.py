from __future__ import annotations

from datetime import datetime
from typing import Protocol

from tgcurator.domain.review import (
    ManualLabelEvent,
    MessageReviewEvent,
    ReviewStatus,
)


class ReviewRepository(Protocol):
    async def append_manual_label(self, *, event: ManualLabelEvent) -> ManualLabelEvent: ...

    async def transition_review_status(
        self,
        *,
        event_id: str,
        message_id: str,
        new_status: ReviewStatus,
        actor_admin_user_id: str,
        created_at: datetime,
        reason: str | None = None,
    ) -> MessageReviewEvent: ...

    async def list_manual_label_history(
        self, *, message_id: str
    ) -> tuple[ManualLabelEvent, ...]: ...

    async def list_review_history(self, *, message_id: str) -> tuple[MessageReviewEvent, ...]: ...
