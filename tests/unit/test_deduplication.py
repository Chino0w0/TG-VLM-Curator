from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from tgcurator.domain.messages import (
    DuplicateMatchKind,
    DuplicatePolicy,
    DuplicateStatus,
    VisualCandidate,
    compute_archived_visual_identity,
    evaluate_visual_duplicate,
    multiset_hamming_distance,
    phash_hamming_distance,
)
from tgcurator.shared import DomainValidationError

T0 = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


class DeduplicationDomainTests(unittest.TestCase):
    def test_visual_identity_is_order_independent_and_excludes_non_cover_signals(self) -> None:
        # Different ordering of image pHashes and video cover pHashes yields identical identity.
        fp_a, tokens_a = compute_archived_visual_identity(
            image_phashes=("f0f0f0f0f0f0f0f0", "0f0f0f0f0f0f0f0f"),
            video_cover_phashes=("aaaaaaaaaaaaaaaa",),
        )
        fp_b, tokens_b = compute_archived_visual_identity(
            image_phashes=("0F0F0F0F0F0F0F0F", "f0f0f0f0f0f0f0f0"),
            video_cover_phashes=("AAAAAAAAAAAAAAAA",),
        )

        self.assertIsNotNone(fp_a)
        self.assertEqual(fp_a, fp_b)
        self.assertEqual(tokens_a, tokens_b)
        self.assertEqual(
            tokens_a,
            (
                "image:0f0f0f0f0f0f0f0f",
                "image:f0f0f0f0f0f0f0f0",
                "video_cover:aaaaaaaaaaaaaaaa",
            ),
        )

        # Text-only messages have no visual identity and never collide visually.
        empty_fp, empty_tokens = compute_archived_visual_identity(
            image_phashes=(),
            video_cover_phashes=(),
        )
        self.assertIsNone(empty_fp)
        self.assertEqual(empty_tokens, ())

    def test_image_and_video_cover_tokens_never_cross_match(self) -> None:
        distance = multiset_hamming_distance(
            ("image:1111111111111111",),
            ("video_cover:1111111111111111",),
            max_distance=64,
        )
        self.assertIsNone(distance)

    def test_unordered_multiset_bipartite_dp_finds_optimal_pairing_across_reordered_album(
        self,
    ) -> None:
        # Canonical album: [0000000000000000, ffffffffffffffff, aaaaaaaaaaaaaaaa]
        # Re-uploaded album in scrambled order with 1-bit + 2-bit + 1-bit recompression drift = 4:
        canonical_fp, canonical_tokens = compute_archived_visual_identity(
            image_phashes=(
                "0000000000000000",
                "ffffffffffffffff",
                "aaaaaaaaaaaaaaaa",
            ),
            video_cover_phashes=(),
        )
        reupload_fp, reupload_tokens = compute_archived_visual_identity(
            image_phashes=(
                "aaaaaaaaaaaaaaab",  # 1 bit from aaaa...
                "0000000000000003",  # 2 bits from 0000...
                "fffffffffffffffe",  # 1 bit from ffff...
            ),
            video_cover_phashes=(),
        )
        self.assertNotEqual(canonical_fp, reupload_fp)
        self.assertEqual(
            phash_hamming_distance("0000000000000000", "0000000000000003"),
            2,
        )

        candidate = VisualCandidate(
            message_id="msg-canonical",
            visual_fingerprint=canonical_fp,
            visual_tokens=canonical_tokens,
            published_at=T0,
        )

        matched = evaluate_visual_duplicate(
            message_id="msg-reupload",
            visual_fingerprint=reupload_fp,
            visual_tokens=reupload_tokens,
            candidates=(candidate,),
            duplicate_policy=DuplicatePolicy.SKIP,
            phash_distance_threshold=4,
        )
        self.assertEqual(matched.status, DuplicateStatus.DUPLICATE_SKIPPED)
        self.assertEqual(matched.canonical_message_id, "msg-canonical")
        self.assertEqual(matched.match_kind, DuplicateMatchKind.PERCEPTUAL_MULTISET)
        self.assertEqual(matched.hamming_distance, 4)

        not_matched = evaluate_visual_duplicate(
            message_id="msg-reupload",
            visual_fingerprint=reupload_fp,
            visual_tokens=reupload_tokens,
            candidates=(candidate,),
            duplicate_policy=DuplicatePolicy.SKIP,
            phash_distance_threshold=3,
        )
        self.assertEqual(not_matched.status, DuplicateStatus.UNIQUE)
        self.assertIsNone(not_matched.canonical_message_id)
        self.assertIsNone(not_matched.hamming_distance)

    def test_exact_fingerprint_selects_earliest_canonical_and_applies_reclassify_policy(
        self,
    ) -> None:
        fp, tokens = compute_archived_visual_identity(
            image_phashes=("123456789abcdef0",),
            video_cover_phashes=(),
        )
        later = VisualCandidate(
            message_id="msg-2",
            visual_fingerprint=fp,
            visual_tokens=tokens,
            published_at=T0 + timedelta(minutes=10),
        )
        earlier = VisualCandidate(
            message_id="msg-1",
            visual_fingerprint=fp,
            visual_tokens=tokens,
            published_at=T0,
        )
        self_candidate = VisualCandidate(
            message_id="msg-current",
            visual_fingerprint=fp,
            visual_tokens=tokens,
            published_at=T0 - timedelta(minutes=5),
        )

        result = evaluate_visual_duplicate(
            message_id="msg-current",
            visual_fingerprint=fp,
            visual_tokens=tokens,
            candidates=(later, earlier, self_candidate),
            duplicate_policy=DuplicatePolicy.RECLASSIFY_WITH_TEXT,
            phash_distance_threshold=6,
        )
        self.assertEqual(result.status, DuplicateStatus.DUPLICATE_RECLASSIFY)
        self.assertEqual(result.canonical_message_id, "msg-1")
        self.assertEqual(result.match_kind, DuplicateMatchKind.EXACT_FINGERPRINT)
        self.assertEqual(result.hamming_distance, 0)
        self.assertEqual(result.duplicate_policy, DuplicatePolicy.RECLASSIFY_WITH_TEXT)

    def test_invalid_phash_or_threshold_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            phash_hamming_distance("not-a-hex-phash!", "0000000000000000")
        with self.assertRaises(DomainValidationError):
            evaluate_visual_duplicate(
                message_id="msg-1",
                visual_fingerprint=None,
                visual_tokens=(),
                candidates=(),
                duplicate_policy=DuplicatePolicy.SKIP,
                phash_distance_threshold=65,
            )


if __name__ == "__main__":
    unittest.main()
