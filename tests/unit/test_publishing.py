import unittest

from tgcurator.domain.publishing import PublicationMode, publication_idempotency_key


class PublicationIdempotencyTests(unittest.TestCase):
    def test_key_is_stable_for_same_business_effect_and_changes_for_mode(self) -> None:
        values = {
            "source_message_id": "message-1",
            "destination_channel_id": "destination-1",
            "routing_policy_version_id": "policy-v3",
            "routing_rule_id": "rule-7",
            "action_id": "action-9",
        }
        first = publication_idempotency_key(
            **values,
            publication_mode=PublicationMode.FORWARD_ONLY,
        )
        second = publication_idempotency_key(
            **values,
            publication_mode=PublicationMode.FORWARD_ONLY,
        )
        changed = publication_idempotency_key(
            **values,
            publication_mode=PublicationMode.METADATA_ONLY,
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertTrue(first.startswith("publication:v1:"))


if __name__ == "__main__":
    unittest.main()
