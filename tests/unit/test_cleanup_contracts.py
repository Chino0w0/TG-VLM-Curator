from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from tgcurator.application.ports.cleanup import (
    ArchiveCleanupFilter,
    MappingArchiveStorageResolver,
    canonical_cleanup_filter_json,
    cleanup_filter_hash,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


class CleanupContractTests(unittest.TestCase):
    def test_filter_normalizes_order_and_round_trips_canonical_snapshot(self) -> None:
        channel_a = str(uuid4())
        channel_b = str(uuid4())
        cleanup_filter = ArchiveCleanupFilter(
            archived_from=NOW,
            source_channel_ids=(channel_b.upper(), channel_a, channel_b),
            model_label_keys=("zebra", "alpha", "zebra"),
            manual_label_keys=("manual-b", "manual-a"),
            effective_label_keys=("effective-b", "effective-a"),
            label_scopes=("MEDIA", "global"),
            negative_label=False,
            review_statuses=("reviewed", "unreviewed"),
            processing_statuses=("processed", "ingested"),
            duplicate_statuses=("duplicate", "canonical"),
            media_types=("video", "image"),
            blocked_from_analysis=False,
        )

        restored = ArchiveCleanupFilter.from_snapshot(cleanup_filter.snapshot())

        self.assertEqual(restored, cleanup_filter)
        self.assertEqual(restored.canonical_json, cleanup_filter.canonical_json)
        self.assertEqual(restored.sha256, cleanup_filter.sha256)
        self.assertEqual(tuple(restored.source_channel_ids), tuple(sorted({channel_a, channel_b})))
        self.assertEqual(restored.model_label_keys, ("alpha", "zebra"))
        self.assertEqual(restored.label_scopes, ("global", "media"))
        self.assertEqual(json.loads(restored.canonical_json), restored.snapshot())

    def test_canonical_json_and_hash_are_key_order_independent_ascii(self) -> None:
        left = {"emoji": "\u732b", "nested": {"b": 2, "a": 1}}
        right = {"nested": {"a": 1, "b": 2}, "emoji": "\u732b"}

        canonical = canonical_cleanup_filter_json(left)

        self.assertEqual(canonical, canonical_cleanup_filter_json(right))
        self.assertEqual(cleanup_filter_hash(left), cleanup_filter_hash(right))
        self.assertIn(r"\u732b", canonical)
        canonical.encode("ascii")

    def test_empty_filter_and_naive_timestamps_are_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            ArchiveCleanupFilter()
        with self.assertRaises(DomainValidationError):
            ArchiveCleanupFilter(archived_from=datetime(2026, 9, 16, 12, 0))
        with self.assertRaises(DomainValidationError):
            ArchiveCleanupFilter(
                archived_from=NOW,
                archived_before=NOW,
            )

    def test_false_boolean_filters_are_meaningful(self) -> None:
        self.assertFalse(ArchiveCleanupFilter(negative_label=False).negative_label)
        self.assertFalse(ArchiveCleanupFilter(blocked_from_analysis=False).blocked_from_analysis)

    def test_snapshot_shape_and_values_are_strict(self) -> None:
        snapshot = ArchiveCleanupFilter(media_types=("image",)).snapshot()
        snapshot["extra"] = True
        with self.assertRaises(DomainValidationError):
            ArchiveCleanupFilter.from_snapshot(snapshot)

        snapshot = ArchiveCleanupFilter(media_types=("image",)).snapshot()
        snapshot["media_types"] = "image"
        with self.assertRaises(DomainValidationError):
            ArchiveCleanupFilter.from_snapshot(snapshot)

    def test_storage_resolver_requires_explicit_backend_composition(self) -> None:
        storage = object()
        resolver = MappingArchiveStorageResolver({"filesystem": storage})  # type: ignore[arg-type]

        self.assertIs(resolver.resolve(storage_backend="filesystem"), storage)
        with self.assertRaises(DomainValidationError):
            resolver.resolve(storage_backend="unknown")


if __name__ == "__main__":
    unittest.main()
