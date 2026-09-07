from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tgcurator.domain.messages import TelegramMessage
from tgcurator.shared import ensure_aware, ensure_positive_duration


@dataclass(slots=True)
class _PendingMediaGroup:
    first_received_at: datetime
    messages_by_id: dict[int, TelegramMessage] = field(default_factory=dict)


class MediaGroupAggregationBuffer:
    """Hold native Telegram media-group parts briefly before normalized ingestion.

    The buffer is deliberately in-memory and reconstructable. A restart can lose pending parts,
    but the next history reconciliation or execution replay safely re-ingests them through the
    durable idempotent message path.
    """

    def __init__(self, *, aggregation_window: timedelta = timedelta(seconds=3)) -> None:
        ensure_positive_duration(aggregation_window, field="aggregation_window")
        self._aggregation_window = aggregation_window
        self._pending: dict[tuple[str, int], _PendingMediaGroup] = {}

    def add(self, *, message: TelegramMessage, now: datetime) -> tuple[TelegramMessage, ...]:
        """Accept one update and return regular messages plus media groups whose wait expired."""

        ensure_aware(now, field="now")
        ready = self.flush_due(now=now)
        if message.grouped_id is None:
            return (*ready, message)

        key = (message.source_channel_id, message.grouped_id)
        pending = self._pending.setdefault(key, _PendingMediaGroup(first_received_at=now))
        # At-least-once Update delivery may repeat a component. Keep the newest DTO until release.
        pending.messages_by_id[message.telegram_message_id] = message
        return ready

    def flush_due(self, *, now: datetime) -> tuple[TelegramMessage, ...]:
        """Release media groups whose short aggregation wait has elapsed."""

        ensure_aware(now, field="now")
        due_keys = tuple(
            sorted(
                (
                    key
                    for key, pending in self._pending.items()
                    if pending.first_received_at + self._aggregation_window <= now
                ),
                key=lambda key: (
                    self._pending[key].first_received_at,
                    key[0],
                    key[1],
                ),
            )
        )
        return self._release(keys=due_keys)

    def flush_all(self) -> tuple[TelegramMessage, ...]:
        """Release every pending group in deterministic order, for controlled shutdown."""

        keys = tuple(
            sorted(
                self._pending,
                key=lambda key: (
                    self._pending[key].first_received_at,
                    key[0],
                    key[1],
                ),
            )
        )
        return self._release(keys=keys)

    @property
    def pending_group_count(self) -> int:
        return len(self._pending)

    def _release(self, *, keys: tuple[tuple[str, int], ...]) -> tuple[TelegramMessage, ...]:
        released: list[TelegramMessage] = []
        for key in keys:
            pending = self._pending.pop(key)
            released.extend(message for _, message in sorted(pending.messages_by_id.items()))
        return tuple(released)
