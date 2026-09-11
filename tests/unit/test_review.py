from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from tgcurator.domain.review import (
    LabelValue,
    ManualLabelEvent,
    ManualLabelOperation,
    ReviewStatus,
    ReviewTransition,
    labels_to_routing_snapshot,
    resolve_label_namespaces,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class ReviewDomainTests(unittest.TestCase):
    def test_manual_set_overrides_effective_without_mutating_model_history(self) -> None:
        model = self._label(score=0.81, activated=True)
        manual = self._event(
            event_id="manual-1",
            operation=ManualLabelOperation.SET,
            score=0.2,
            activated=False,
        )

        resolved = resolve_label_namespaces((model,), (manual,))

        self.assertEqual(resolved.model, (model,))
        self.assertEqual(resolved.manual[0].score, 0.2)
        self.assertEqual(resolved.effective[0].score, 0.2)
        self.assertFalse(resolved.effective[0].activated)
        self.assertEqual(model.score, 0.81)

    def test_latest_clear_removes_manual_override_and_falls_back_to_model(self) -> None:
        model = self._label(score=0.81, activated=True)
        set_event = self._event(
            event_id="manual-1",
            operation=ManualLabelOperation.SET,
            score=0.2,
            activated=False,
        )
        clear_event = self._event(
            event_id="manual-2",
            operation=ManualLabelOperation.CLEAR,
            created_at=NOW + timedelta(seconds=1),
        )

        resolved = resolve_label_namespaces((model,), (clear_event, set_event))

        self.assertEqual(resolved.model, (model,))
        self.assertEqual(resolved.manual, ())
        self.assertEqual(resolved.effective, (model,))

    def test_namespaces_serialize_global_and_media_labels_separately(self) -> None:
        global_label = self._label(score=0.81, activated=True)
        media_label = LabelValue(
            message_id="message-1",
            target_scope="media",
            target_kind="image_asset",
            target_id="image-1",
            image_asset_id="image-1",
            label_definition_version_id="image-quality-v1",
            label_key="image_quality",
            score=0.73,
            activated=True,
        )

        snapshot = labels_to_routing_snapshot(
            resolve_label_namespaces((media_label, global_label), ())
        )

        self.assertEqual(snapshot["model"]["global"]["real_ugc"]["score"], 0.81)
        self.assertEqual(
            snapshot["effective"]["media"]["image-1"]["labels"]["image_quality"]["score"],
            0.73,
        )

    def test_routing_snapshot_rejects_duplicate_label_key_versions_for_one_target(
        self,
    ) -> None:
        version_one = self._label(score=0.81, activated=True)
        version_two = LabelValue(
            message_id="message-1",
            target_scope="global",
            target_kind="message",
            target_id="message-1",
            label_definition_version_id="real-ugc-v2",
            label_key="real_ugc",
            score=0.42,
            activated=False,
        )
        resolved = resolve_label_namespaces((version_two, version_one), ())

        self.assertEqual(len(resolved.model), 2)
        with self.assertRaisesRegex(
            DomainValidationError,
            "multiple label definition versions",
        ):
            labels_to_routing_snapshot(resolved)

    def test_target_identity_and_manual_operation_payloads_are_validated(self) -> None:
        with self.assertRaises(DomainValidationError):
            LabelValue(
                message_id="message-1",
                target_scope="global",
                target_kind="message",
                target_id="different-message",
                label_definition_version_id="real-ugc-v1",
                label_key="real_ugc",
                score=0.8,
                activated=True,
            )
        with self.assertRaises(DomainValidationError):
            self._event(
                event_id="bad-clear",
                operation=ManualLabelOperation.CLEAR,
                score=0.5,
            )
        with self.assertRaises(DomainValidationError):
            self._event(
                event_id="bad-set",
                operation=ManualLabelOperation.SET,
                score=None,
                activated=True,
            )

    def test_review_status_is_explicit_workflow_state_only(self) -> None:
        transition = ReviewTransition(
            message_id="message-1",
            old_status=ReviewStatus.UNREVIEWED,
            new_status=ReviewStatus.REVIEWED,
            actor_admin_user_id="admin-1",
            reason="checked",
        )
        self.assertEqual(transition.new_status, ReviewStatus.REVIEWED)
        with self.assertRaises(DomainValidationError):
            ReviewTransition(
                message_id="message-1",
                old_status=ReviewStatus.REVIEWED,
                new_status=ReviewStatus.REVIEWED,
                actor_admin_user_id="admin-1",
            )

    def _label(self, *, score: float, activated: bool) -> LabelValue:
        return LabelValue(
            message_id="message-1",
            target_scope="global",
            target_kind="message",
            target_id="message-1",
            label_definition_version_id="real-ugc-v1",
            label_key="real_ugc",
            score=score,
            activated=activated,
        )

    def _event(
        self,
        *,
        event_id: str,
        operation: ManualLabelOperation,
        score: float | None = None,
        activated: bool | None = None,
        created_at: datetime = NOW,
    ) -> ManualLabelEvent:
        return ManualLabelEvent(
            event_id=event_id,
            message_id="message-1",
            target_scope="global",
            target_kind="message",
            target_id="message-1",
            label_definition_version_id="real-ugc-v1",
            label_key="real_ugc",
            operation=operation,
            score=score,
            activated=activated,
            actor_admin_user_id="admin-1",
            created_at=created_at,
        )


if __name__ == "__main__":
    unittest.main()
