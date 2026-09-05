from __future__ import annotations

from datetime import datetime

from tgcurator.application.ingestion import MessageIngestService
from tgcurator.application.ports.contracts import TelegramGateway
from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.application.processing.execution_worker import RangeExecutionWorker
from tgcurator.domain.messages import TelegramMessage
from tgcurator.shared import DomainValidationError, ensure_aware


class RangeExecutionHistoryIngestion:
    """Fetch and persist one immutable history window without a long database transaction.

    Telegram I/O happens first, followed by idempotent persistence, then short watermark
    and completion transactions. A crash at any boundary is safe because the lease can expire
    and replay the immutable window.
    """

    def __init__(
        self,
        *,
        range_execution_worker: RangeExecutionWorker,
        telegram_gateway: TelegramGateway,
        message_ingest_service: MessageIngestService,
    ) -> None:
        self._range_execution_worker = range_execution_worker
        self._telegram_gateway = telegram_gateway
        self._message_ingest_service = message_ingest_service

    async def process(self, *, claim: ClaimedRangeExecution, now: datetime) -> bool:
        ensure_aware(now, field="now")
        messages = await self._telegram_gateway.fetch_history(
            source_channel_id=claim.source_channel_id,
            from_at=claim.execution.from_at,
            to_at=claim.execution.to_at,
        )
        self._validate_history_window(messages=messages, claim=claim)
        await self._message_ingest_service.ingest_history(messages)

        advanced = await self._range_execution_worker.advance_watermark(
            execution_id=claim.execution.execution_id,
            lease_token=claim.lease_token,
            watermark_at=claim.execution.to_at,
            now=now,
        )
        if not advanced:
            return False
        return await self._range_execution_worker.complete(
            execution_id=claim.execution.execution_id,
            lease_token=claim.lease_token,
            now=now,
        )

    @staticmethod
    def _validate_history_window(
        *, messages: tuple[TelegramMessage, ...], claim: ClaimedRangeExecution
    ) -> None:
        for message in messages:
            if message.source_channel_id != claim.source_channel_id:
                raise DomainValidationError(
                    "Telegram history returned a message for another source channel"
                )
            if not claim.execution.from_at <= message.sent_at < claim.execution.to_at:
                raise DomainValidationError(
                    "Telegram history returned a message outside the execution window"
                )
