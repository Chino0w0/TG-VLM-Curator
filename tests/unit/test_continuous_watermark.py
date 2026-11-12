from __future__ import annotations

import unittest
from uuid import uuid4

from tgcurator.domain.messages import DuplicatePolicy
from tgcurator.domain.processing import (
    MessageTerminalReason,
    SourceChannelProfileConfig,
    WindowMessageProgress,
    compute_continuous_watermark,
)
from tgcurator.shared import DomainValidationError


class ContinuousWatermarkTests(unittest.TestCase):
    def test_watermark_advances_monotonically_across_sparse_ids_and_stops_before_failure(
        self,
    ) -> None:
        # Window (100, 110] has sparse Telegram IDs 102, 105, 108.
        # 102 and 108 are terminal, while 105 failed -> watermark advances to 104 (never 108/110).
        messages = (
            WindowMessageProgress(
                primary_telegram_message_id=102,
                processing_status="processed",
                terminal_reason=MessageTerminalReason.COMPLETED.value,
            ),
            WindowMessageProgress(
                primary_telegram_message_id=105,
                processing_status="failed",
                terminal_reason=None,
            ),
            WindowMessageProgress(
                primary_telegram_message_id=108,
                processing_status="processed",
                terminal_reason=MessageTerminalReason.DUPLICATE_SKIPPED.value,
            ),
        )
        blocked = compute_continuous_watermark(
            from_message_id_exclusive=100,
            current_watermark_message_id=100,
            to_message_id_inclusive=110,
            history_ingested=True,
            messages=messages,
        )
        self.assertEqual(blocked.next_watermark_message_id, 104)
        self.assertFalse(blocked.window_completed)
        self.assertEqual(blocked.blocked_at_message_id, 105)

        # Once 105 is resolved via admin_skipped, watermark advances through 108 and 109-110.
        resolved = compute_continuous_watermark(
            from_message_id_exclusive=100,
            current_watermark_message_id=blocked.next_watermark_message_id,
            to_message_id_inclusive=110,
            history_ingested=True,
            messages=(
                messages[0],
                WindowMessageProgress(
                    primary_telegram_message_id=105,
                    processing_status="processed",
                    terminal_reason=MessageTerminalReason.ADMIN_SKIPPED.value,
                ),
                messages[2],
            ),
        )
        self.assertEqual(resolved.next_watermark_message_id, 110)
        self.assertTrue(resolved.window_completed)
        self.assertIsNone(resolved.blocked_at_message_id)

    def test_unsettled_publication_or_unfetched_history_holds_watermark(self) -> None:
        not_fetched = compute_continuous_watermark(
            from_message_id_exclusive=50,
            current_watermark_message_id=50,
            to_message_id_inclusive=60,
            history_ingested=False,
            messages=(),
        )
        self.assertEqual(not_fetched.next_watermark_message_id, 50)
        self.assertFalse(not_fetched.window_completed)

        pending_publish = compute_continuous_watermark(
            from_message_id_exclusive=50,
            current_watermark_message_id=50,
            to_message_id_inclusive=60,
            history_ingested=True,
            messages=(
                WindowMessageProgress(
                    primary_telegram_message_id=55,
                    processing_status="processed",
                    terminal_reason=MessageTerminalReason.COMPLETED.value,
                    has_blocking_work=True,
                ),
            ),
        )
        self.assertEqual(pending_publish.next_watermark_message_id, 54)
        self.assertFalse(pending_publish.window_completed)
        self.assertEqual(pending_publish.blocked_at_message_id, 55)

    def test_regression_below_current_watermark_raises_validation_error(self) -> None:
        with self.assertRaises(DomainValidationError):
            compute_continuous_watermark(
                from_message_id_exclusive=10,
                current_watermark_message_id=15,
                to_message_id_inclusive=20,
                history_ingested=True,
                messages=(
                    WindowMessageProgress(
                        primary_telegram_message_id=12,
                        processing_status="processing",
                    ),
                ),
            )

    def test_source_channel_profile_config_round_trip_and_validation(self) -> None:
        pipeline_id = str(uuid4())
        routing_id = str(uuid4())
        config = SourceChannelProfileConfig.from_content(
            {
                "analysis_pipeline_version_id": pipeline_id,
                "routing_policy_version_id": routing_id,
                "duplicate_policy": "reclassify_with_text",
                "phash_distance_threshold": 8,
                "prescreen_enabled": False,
            }
        )
        self.assertEqual(config.analysis_pipeline_version_id, pipeline_id)
        self.assertEqual(config.routing_policy_version_id, routing_id)
        self.assertEqual(config.duplicate_policy, DuplicatePolicy.RECLASSIFY_WITH_TEXT)
        self.assertEqual(config.phash_distance_threshold, 8)
        self.assertFalse(config.prescreen_enabled)
        self.assertEqual(
            SourceChannelProfileConfig.from_content(config.to_content()),
            config,
        )

        with self.assertRaises(DomainValidationError):
            SourceChannelProfileConfig.from_content({"duplicate_policy": "drop_forever"})


if __name__ == "__main__":
    unittest.main()
