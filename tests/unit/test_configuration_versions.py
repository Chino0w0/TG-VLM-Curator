import unittest
from datetime import UTC, datetime

from tgcurator.domain.configuration import ConfigurationState, ConfigurationVersion
from tgcurator.shared import DomainValidationError


class ConfigurationVersionTests(unittest.TestCase):
    def test_draft_is_canonical_and_published_version_cannot_be_revised(self) -> None:
        draft = ConfigurationVersion.draft(
            version_id="profile-v1",
            configuration_kind="source_channel_profile",
            version_number=1,
            definition={"filters": ["text"], "enabled": True},
        )
        self.assertEqual(draft.state, ConfigurationState.DRAFT)
        self.assertEqual(draft.definition["filters"], ["text"])

        published = draft.publish(datetime(2026, 9, 4, tzinfo=UTC))
        self.assertEqual(published.state, ConfigurationState.PUBLISHED)
        with self.assertRaises(DomainValidationError):
            published.revise({"filters": []})

    def test_revised_draft_has_new_snapshot_hash(self) -> None:
        draft = ConfigurationVersion.draft(
            version_id="prompt-v1",
            configuration_kind="prompt",
            version_number=1,
            definition={"text": "first"},
        )
        revised = draft.revise({"text": "second"})
        self.assertNotEqual(draft.content_hash, revised.content_hash)
        self.assertEqual(revised.definition, {"text": "second"})

    def test_retired_version_preserves_its_publication_timestamp(self) -> None:
        published_at = datetime(2026, 9, 4, tzinfo=UTC)
        retired = (
            ConfigurationVersion.draft(
                version_id="pipeline-v1",
                configuration_kind="pipeline",
                version_number=1,
                definition={"stages": []},
            )
            .publish(published_at)
            .retire()
        )

        self.assertEqual(retired.state, ConfigurationState.RETIRED)
        self.assertEqual(retired.published_at, published_at)
        with self.assertRaises(DomainValidationError):
            ConfigurationVersion(
                version_id="bad",
                configuration_kind="pipeline",
                version_number=1,
                content_json="{}",
                content_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21e1b7a22c7e0a9e9fb87f5d3",
                state=ConfigurationState.RETIRED,
            )


if __name__ == "__main__":
    unittest.main()
