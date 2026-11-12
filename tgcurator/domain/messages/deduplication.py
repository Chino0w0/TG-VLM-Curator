from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tgcurator.shared import DomainValidationError, ensure_aware

_HEX_64_BIT = re.compile(r"^[0-9a-f]{16}$")


class DuplicatePolicy(StrEnum):
    SKIP = "skip"
    RECLASSIFY_WITH_TEXT = "reclassify_with_text"


class DuplicateStatus(StrEnum):
    UNCHECKED = "unchecked"
    UNIQUE = "unique"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    DUPLICATE_RECLASSIFY = "duplicate_reclassify"


class DuplicateMatchKind(StrEnum):
    EXACT_FINGERPRINT = "exact_fingerprint"
    PERCEPTUAL_MULTISET = "perceptual_multiset"


@dataclass(frozen=True, slots=True)
class VisualCandidate:
    """Visual identity of an earlier message eligible to serve as canonical."""

    message_id: str
    visual_fingerprint: str | None
    visual_tokens: tuple[str, ...]
    published_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str) or not self.message_id.strip():
            raise DomainValidationError("candidate message_id must not be blank")
        ensure_aware(self.published_at, field="published_at")
        normalized_tokens = tuple(_normalize_visual_token(token) for token in self.visual_tokens)
        object.__setattr__(self, "visual_tokens", tuple(sorted(normalized_tokens)))


@dataclass(frozen=True, slots=True)
class DuplicateMatchResult:
    """Deterministic outcome of message-level visual deduplication."""

    status: DuplicateStatus
    canonical_message_id: str | None
    match_kind: DuplicateMatchKind | None
    hamming_distance: int | None
    duplicate_policy: DuplicatePolicy


def normalize_phash(value: str | None, *, field: str = "perceptual_hash") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field} must be a 16-char hex string or null")
    normalized = value.strip().lower()
    if not _HEX_64_BIT.fullmatch(normalized):
        raise DomainValidationError(f"{field} must be a 16-char lowercase hex string")
    return normalized


def phash_hamming_distance(left: str, right: str) -> int:
    normalized_left = normalize_phash(left, field="left")
    normalized_right = normalize_phash(right, field="right")
    assert normalized_left is not None and normalized_right is not None
    return (int(normalized_left, 16) ^ int(normalized_right, 16)).bit_count()


def compute_archived_visual_identity(
    *,
    image_phashes: Iterable[str | None],
    video_cover_phashes: Iterable[str | None],
) -> tuple[str | None, tuple[str, ...]]:
    """Compute canonical order-independent visual fingerprint and tokens from archived media.

    Only image perceptual hashes and video cover perceptual hashes participate. Message text,
    source channel, media order, and representative video frames never participate.
    """

    tokens: list[str] = []
    for phash in image_phashes:
        normalized = normalize_phash(phash, field="image_phash")
        if normalized is not None:
            tokens.append(f"image:{normalized}")
    for phash in video_cover_phashes:
        normalized = normalize_phash(phash, field="video_cover_phash")
        if normalized is not None:
            tokens.append(f"video_cover:{normalized}")

    if not tokens:
        return None, ()
    ordered = tuple(sorted(tokens))
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    return digest, ordered


def multiset_hamming_distance(
    left_tokens: Sequence[str],
    right_tokens: Sequence[str],
    *,
    max_distance: int,
) -> int | None:
    """Return exact minimum unordered kind-matched Hamming distance within max_distance."""

    if max_distance < 0:
        raise DomainValidationError("max_distance must be non-negative")
    if not left_tokens or len(left_tokens) != len(right_tokens):
        return None

    left_parsed = [_parse_visual_token(token) for token in left_tokens]
    right_parsed = [_parse_visual_token(token) for token in right_tokens]

    left_by_kind: dict[str, list[str]] = {"image": [], "video_cover": []}
    right_by_kind: dict[str, list[str]] = {"image": [], "video_cover": []}
    for kind, phash in left_parsed:
        left_by_kind[kind].append(phash)
    for kind, phash in right_parsed:
        right_by_kind[kind].append(phash)

    if len(left_by_kind["image"]) != len(right_by_kind["image"]):
        return None
    if len(left_by_kind["video_cover"]) != len(right_by_kind["video_cover"]):
        return None

    total_distance = 0
    for kind in ("image", "video_cover"):
        remaining_budget = max_distance - total_distance
        kind_distance = _min_bipartite_phash_distance(
            left_by_kind[kind],
            right_by_kind[kind],
            max_distance=remaining_budget,
        )
        if kind_distance is None:
            return None
        total_distance += kind_distance
        if total_distance > max_distance:
            return None
    return total_distance


def evaluate_visual_duplicate(
    *,
    message_id: str,
    visual_fingerprint: str | None,
    visual_tokens: Sequence[str],
    candidates: Sequence[VisualCandidate],
    duplicate_policy: DuplicatePolicy,
    phash_distance_threshold: int = 6,
) -> DuplicateMatchResult:
    """Match a message's visual identity against earlier canonical candidates."""

    if not isinstance(message_id, str) or not message_id.strip():
        raise DomainValidationError("message_id must not be blank")
    if not isinstance(duplicate_policy, DuplicatePolicy):
        raise DomainValidationError("duplicate_policy must be a DuplicatePolicy")
    if (
        not isinstance(phash_distance_threshold, int)
        or isinstance(phash_distance_threshold, bool)
        or not 0 <= phash_distance_threshold <= 64
    ):
        raise DomainValidationError("phash_distance_threshold must be an integer in [0, 64]")

    normalized_tokens = tuple(sorted(_normalize_visual_token(token) for token in visual_tokens))
    if visual_fingerprint is None and not normalized_tokens:
        return DuplicateMatchResult(
            status=DuplicateStatus.UNIQUE,
            canonical_message_id=None,
            match_kind=None,
            hamming_distance=None,
            duplicate_policy=duplicate_policy,
        )

    other_candidates = [item for item in candidates if item.message_id != message_id]
    if visual_fingerprint is not None:
        exact_matches = [
            item for item in other_candidates if item.visual_fingerprint == visual_fingerprint
        ]
        if exact_matches:
            canonical = min(exact_matches, key=lambda item: (item.published_at, item.message_id))
            return DuplicateMatchResult(
                status=_matched_status(duplicate_policy),
                canonical_message_id=canonical.message_id,
                match_kind=DuplicateMatchKind.EXACT_FINGERPRINT,
                hamming_distance=0,
                duplicate_policy=duplicate_policy,
            )

    if normalized_tokens:
        best: tuple[int, datetime, str] | None = None
        for candidate in other_candidates:
            if len(candidate.visual_tokens) != len(normalized_tokens):
                continue
            distance = multiset_hamming_distance(
                normalized_tokens,
                candidate.visual_tokens,
                max_distance=phash_distance_threshold,
            )
            if distance is None:
                continue
            rank = (distance, candidate.published_at, candidate.message_id)
            if best is None or rank < best:
                best = rank
        if best is not None:
            distance, _, canonical_id = best
            match_kind = (
                DuplicateMatchKind.EXACT_FINGERPRINT
                if distance == 0 and visual_fingerprint is not None
                else DuplicateMatchKind.PERCEPTUAL_MULTISET
            )
            return DuplicateMatchResult(
                status=_matched_status(duplicate_policy),
                canonical_message_id=canonical_id,
                match_kind=match_kind,
                hamming_distance=distance,
                duplicate_policy=duplicate_policy,
            )

    return DuplicateMatchResult(
        status=DuplicateStatus.UNIQUE,
        canonical_message_id=None,
        match_kind=None,
        hamming_distance=None,
        duplicate_policy=duplicate_policy,
    )


def _matched_status(policy: DuplicatePolicy) -> DuplicateStatus:
    return (
        DuplicateStatus.DUPLICATE_SKIPPED
        if policy is DuplicatePolicy.SKIP
        else DuplicateStatus.DUPLICATE_RECLASSIFY
    )


def _normalize_visual_token(token: str) -> str:
    kind, phash = _parse_visual_token(token)
    return f"{kind}:{phash}"


def _parse_visual_token(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or ":" not in token:
        raise DomainValidationError("visual token must have format '<kind>:<16-hex-phash>'")
    kind, raw_phash = token.split(":", maxsplit=1)
    if kind not in {"image", "video_cover"}:
        raise DomainValidationError("visual token kind must be 'image' or 'video_cover'")
    normalized = normalize_phash(raw_phash, field="visual_token_phash")
    assert normalized is not None
    return kind, normalized


def _min_bipartite_phash_distance(
    left: Sequence[str],
    right: Sequence[str],
    *,
    max_distance: int,
) -> int | None:
    if not left:
        return 0

    # Cancel exact matches first so identical items contribute 0 without enlarging DP state.
    left_counts = Counter(left)
    right_counts = Counter(right)
    common = left_counts & right_counts
    left_rem = list((left_counts - common).elements())
    right_rem = list((right_counts - common).elements())
    if not left_rem:
        return 0

    left_ints = [int(value, 16) for value in left_rem]
    right_ints = [int(value, 16) for value in right_rem]
    count = len(left_ints)

    if count <= 12:
        costs = [
            [(left_val ^ right_val).bit_count() for right_val in right_ints]
            for left_val in left_ints
        ]
        inf = max_distance + 1
        size = 1 << count
        dp = [inf] * size
        dp[0] = 0
        for mask in range(size - 1):
            current = dp[mask]
            if current > max_distance:
                continue
            row_idx = mask.bit_count()
            row_costs = costs[row_idx]
            for col_idx in range(count):
                bit = 1 << col_idx
                if mask & bit:
                    continue
                candidate = current + row_costs[col_idx]
                if candidate <= max_distance and candidate < dp[mask | bit]:
                    dp[mask | bit] = candidate
        answer = dp[size - 1]
        return answer if answer <= max_distance else None

    # Deterministic greedy fallback for synthetic albums larger than Telegram's 10-item limit.
    remaining = list(right_ints)
    total = 0
    for left_val in sorted(left_ints):
        best_idx = min(
            range(len(remaining)), key=lambda idx: (left_val ^ remaining[idx]).bit_count()
        )
        total += (left_val ^ remaining.pop(best_idx)).bit_count()
        if total > max_distance:
            return None
    return total
