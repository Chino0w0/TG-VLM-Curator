from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import AnalysisErrorCode, ExecutionMode, StructuredOutputMode, validate_score
from .labels import LabelSetVersion, ResolvedLabel
from .schema import StructuredOutputPolicy, is_batch_mode, target_field_for_mode


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: AnalysisErrorCode
    error_type: str
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedTargetResult:
    target_id: str
    labels: tuple[ResolvedLabel, ...]
    reason: str | None = None
    evidence: tuple[str, ...] = ()

    @property
    def activated_labels(self) -> tuple[ResolvedLabel, ...]:
        return tuple(label for label in self.labels if label.activated)

    @property
    def activated_negative_labels(self) -> tuple[ResolvedLabel, ...]:
        return tuple(label for label in self.labels if label.activated and label.negative)


@dataclass(frozen=True, slots=True)
class StructuredOutputValidation:
    valid_results: tuple[ValidatedTargetResult, ...]
    issues: tuple[ValidationIssue, ...]
    retry_target_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.issues and not self.retry_target_ids

    @property
    def negative_gate_hit(self) -> bool:
        return any(result.activated_negative_labels for result in self.valid_results)


class StructuredOutputValidator:
    """Validate model structure and stable target mapping before assignments exist."""

    def validate(
        self,
        *,
        payload: Mapping[str, Any],
        execution_mode: ExecutionMode,
        policy: StructuredOutputPolicy,
        label_set: LabelSetVersion,
        expected_target_ids: tuple[str, ...],
    ) -> StructuredOutputValidation:
        expected = _validate_expected_targets(expected_target_ids)
        if not isinstance(payload, Mapping):
            return _whole_payload_failure(expected, "response_not_object")
        if not isinstance(execution_mode, ExecutionMode):
            raise DomainValidationError("execution_mode must be an ExecutionMode")
        if not isinstance(policy, StructuredOutputPolicy):
            raise DomainValidationError("policy must be a StructuredOutputPolicy")
        if not isinstance(label_set, LabelSetVersion):
            raise DomainValidationError("label_set must be a LabelSetVersion")

        if is_batch_mode(execution_mode):
            return self._validate_batch(
                payload=payload,
                execution_mode=execution_mode,
                policy=policy,
                label_set=label_set,
                expected=expected,
            )
        return self._validate_single(
            payload=payload,
            policy=policy,
            label_set=label_set,
            expected=expected,
        )

    def _validate_batch(
        self,
        *,
        payload: Mapping[str, Any],
        execution_mode: ExecutionMode,
        policy: StructuredOutputPolicy,
        label_set: LabelSetVersion,
        expected: tuple[str, ...],
    ) -> StructuredOutputValidation:
        if set(payload) != {"results"} or not isinstance(payload.get("results"), list):
            return _whole_payload_failure(expected, "batch_envelope_invalid")
        target_field = target_field_for_mode(execution_mode)
        assert target_field is not None
        items: list[Any] = payload["results"]
        target_values = [
            item.get(target_field)
            for item in items
            if isinstance(item, Mapping) and isinstance(item.get(target_field), str)
        ]
        counts = Counter(target_values)
        duplicate_targets = {target for target, count in counts.items() if count > 1}
        expected_set = set(expected)
        valid_by_target: dict[str, ValidatedTargetResult] = {}
        issues: list[ValidationIssue] = []

        for item in items:
            if not isinstance(item, Mapping):
                issues.append(
                    ValidationIssue(
                        AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
                        "batch_result_not_object",
                    )
                )
                continue
            target = item.get(target_field)
            if not isinstance(target, str) or not target.strip():
                issues.append(
                    ValidationIssue(
                        AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
                        "target_id_invalid",
                    )
                )
                continue
            if target not in expected_set:
                issues.append(
                    ValidationIssue(
                        AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
                        "unknown_target_result",
                        target,
                    )
                )
                continue
            if target in duplicate_targets:
                if not any(
                    issue.error_type == "duplicate_target_result" and issue.target_id == target
                    for issue in issues
                ):
                    issues.append(
                        ValidationIssue(
                            AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
                            "duplicate_target_result",
                            target,
                        )
                    )
                continue
            result, issue = _validate_result_item(
                item=item,
                target_id=target,
                target_field=target_field,
                policy=policy,
                label_set=label_set,
            )
            if issue is not None:
                issues.append(issue)
            else:
                assert result is not None
                valid_by_target[target] = result

        missing = tuple(target for target in expected if target not in counts)
        issues.extend(
            ValidationIssue(
                AnalysisErrorCode.MISSING_TARGET_RESULT,
                "missing_target_result",
                target,
            )
            for target in missing
        )
        valid_results = tuple(
            valid_by_target[target] for target in expected if target in valid_by_target
        )
        retry_targets = tuple(target for target in expected if target not in valid_by_target)
        return StructuredOutputValidation(valid_results, tuple(issues), retry_targets)

    def _validate_single(
        self,
        *,
        payload: Mapping[str, Any],
        policy: StructuredOutputPolicy,
        label_set: LabelSetVersion,
        expected: tuple[str, ...],
    ) -> StructuredOutputValidation:
        if len(expected) != 1:
            raise DomainValidationError("non-batch validation requires exactly one target")
        if set(payload) != {"result"} or not isinstance(payload.get("result"), Mapping):
            return _whole_payload_failure(expected, "single_envelope_invalid")
        result, issue = _validate_result_item(
            item=payload["result"],
            target_id=expected[0],
            target_field=None,
            policy=policy,
            label_set=label_set,
        )
        if issue is not None:
            return StructuredOutputValidation((), (issue,), expected)
        assert result is not None
        return StructuredOutputValidation((result,), (), ())


def _validate_result_item(
    *,
    item: Mapping[str, Any],
    target_id: str,
    target_field: str | None,
    policy: StructuredOutputPolicy,
    label_set: LabelSetVersion,
) -> tuple[ValidatedTargetResult | None, ValidationIssue | None]:
    expected_fields = {"scores" if policy.mode is StructuredOutputMode.DENSE_SCORES else "matches"}
    if target_field is not None:
        expected_fields.add(target_field)
    if policy.include_reason:
        expected_fields.add("reason")
    if policy.include_evidence:
        expected_fields.add("evidence")
    if set(item) != expected_fields:
        return None, _invalid(target_id, "result_fields_invalid")

    try:
        if policy.mode is StructuredOutputMode.DENSE_SCORES:
            scores = item.get("scores")
            if not isinstance(scores, Mapping) or set(scores) != set(label_set.label_keys):
                return None, _invalid(target_id, "dense_scores_incomplete_or_unknown")
            normalized_scores = {
                key: validate_score(scores[key], field=f"score for {key}")
                for key in label_set.label_keys
            }
        else:
            matches = item.get("matches")
            if not isinstance(matches, list):
                return None, _invalid(target_id, "sparse_matches_not_array")
            normalized_scores: dict[str, float] = {}
            for match in matches:
                if not isinstance(match, Mapping) or set(match) != {"label_key", "score"}:
                    return None, _invalid(target_id, "sparse_match_invalid")
                key = match.get("label_key")
                if not isinstance(key, str) or key not in label_set.label_keys:
                    return None, _invalid(target_id, "sparse_match_unknown_label")
                if key in normalized_scores:
                    return None, _invalid(target_id, "sparse_match_duplicate_label")
                normalized_scores[key] = validate_score(
                    match.get("score"), field=f"score for {key}"
                )
    except DomainValidationError:
        return None, _invalid(target_id, "label_score_invalid")

    reason = item.get("reason")
    if policy.include_reason and not isinstance(reason, str):
        return None, _invalid(target_id, "reason_invalid")
    evidence_value = item.get("evidence", [])
    if policy.include_evidence and (
        not isinstance(evidence_value, list)
        or any(not isinstance(value, str) for value in evidence_value)
    ):
        return None, _invalid(target_id, "evidence_invalid")

    return (
        ValidatedTargetResult(
            target_id=target_id,
            labels=label_set.resolve_scores(normalized_scores),
            reason=reason if isinstance(reason, str) else None,
            evidence=tuple(evidence_value),
        ),
        None,
    )


def _validate_expected_targets(target_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_ids, tuple) or not target_ids:
        raise DomainValidationError("expected target IDs must be a non-empty tuple")
    if any(not isinstance(target, str) or not target.strip() for target in target_ids):
        raise DomainValidationError("expected target IDs must be non-blank strings")
    if len(set(target_ids)) != len(target_ids):
        raise DomainValidationError("expected target IDs must be unique")
    return target_ids


def _invalid(target_id: str, error_type: str) -> ValidationIssue:
    return ValidationIssue(
        AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
        error_type,
        target_id,
    )


def _whole_payload_failure(
    expected: tuple[str, ...], error_type: str
) -> StructuredOutputValidation:
    return StructuredOutputValidation(
        (),
        (
            ValidationIssue(
                AnalysisErrorCode.INVALID_STRUCTURED_OUTPUT,
                error_type,
            ),
        ),
        expected,
    )
