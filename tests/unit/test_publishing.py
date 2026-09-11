import unittest

from tgcurator.domain.publishing import PublicationMode, publication_idempotency_key
from tgcurator.shared import DomainValidationError


class PublicationIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {
            "source_message_id": "message-1",
            "destination_channel_id": "destination-1",
            "routing_policy_version_id": "policy-v3",
            "routing_rule_id": "rule-7",
            "action_id": "action-9",
        }

    def test_legacy_key_is_stable_and_preserves_v1_behavior(self) -> None:
        first = publication_idempotency_key(
            **self.values,
            publication_mode=PublicationMode.FORWARD_ONLY,
        )
        second = publication_idempotency_key(
            **self.values,
            publication_mode=PublicationMode.FORWARD_ONLY,
        )
        changed = publication_idempotency_key(
            **self.values,
            publication_mode=PublicationMode.METADATA_ONLY,
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertTrue(first.startswith("publication:v1:"))

    def test_v2_key_is_stable_for_one_request_and_distinct_for_explicit_reroutes(self) -> None:
        first = publication_idempotency_key(
            **self.values,
            publication_mode=PublicationMode.FORWARD_ONLY,
            routing_request_id="request-1",
            routing_evaluation_id="evaluation-1",
        )
        same = publication_idempotency_key(
            **self.values,
            publication_mode=PublicationMode.FORWARD_ONLY,
            routing_request_id="request-1",
            routing_evaluation_id="evaluation-1",
        )
        reroute = publication_idempotency_key(
            **self.values,
            publication_mode=PublicationMode.FORWARD_ONLY,
            routing_request_id="request-2",
            routing_evaluation_id="evaluation-2",
        )
        request_only = publication_idempotency_key(
            **self.values,
            publication_mode=PublicationMode.FORWARD_ONLY,
            routing_request_id="request-1",
        )

        self.assertEqual(first, same)
        self.assertNotEqual(first, reroute)
        self.assertNotEqual(first, request_only)
        self.assertTrue(first.startswith("publication:v2:"))
        self.assertTrue(request_only.startswith("publication:v2:"))

    def test_key_rejects_blank_identifiers_and_untyped_modes(self) -> None:
        with self.assertRaises(DomainValidationError):
            publication_idempotency_key(
                **{**self.values, "action_id": " "},
                publication_mode=PublicationMode.FORWARD_ONLY,
            )
        with self.assertRaises(DomainValidationError):
            publication_idempotency_key(
                **self.values,
                publication_mode=PublicationMode.FORWARD_ONLY,
                routing_request_id=" ",
            )
        with self.assertRaises(DomainValidationError):
            publication_idempotency_key(
                **self.values,
                publication_mode="forward_only",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
