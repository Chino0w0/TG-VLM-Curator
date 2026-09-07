from __future__ import annotations

import re
from collections.abc import Mapping
from numbers import Real
from typing import Any

from tgcurator.shared import DomainValidationError

_FACT_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_COMPARISON_OPERATORS = frozenset(
    {"eq", "neq", "gt", "gte", "lt", "lte", "in", "contains", "starts_with"}
)
_NUMERIC_COMPARISON_OPERATORS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "in"})
_MAX_DEPTH = 20


class _Unknown:
    """A missing fact or incompatible runtime value that must never become a match."""


_UNKNOWN = _Unknown()


def validate_condition(
    condition: Mapping[str, Any], *, allowed_fact_keys: set[str] | None = None
) -> None:
    """Validate the serializable, non-executable routing condition AST.

    A normal predicate follows the architecture document directly, for example::

        {"fact": "effective.global.real_ugc", "op": "gte", "value": 0.70}

    Boolean nodes use `all`, `any` and `not`. `label_score` and
    `label_present` are intentionally explicit helper nodes for label facts.
    """
    _validate(condition, allowed_fact_keys=allowed_fact_keys, depth=0)


def evaluate_condition(condition: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    """Evaluate defensively; invalid, missing, or incompatible facts never match.

    Evaluation uses an internal unknown state so a missing fact or type error cannot be
    inverted into a successful match by a `not` node.
    """
    try:
        _validate(condition, allowed_fact_keys=None, depth=0)
        return _evaluate(condition, facts) is True
    except Exception:
        return False


def _validate(
    condition: Mapping[str, Any], *, allowed_fact_keys: set[str] | None, depth: int
) -> None:
    if depth > _MAX_DEPTH:
        raise DomainValidationError("routing condition exceeds maximum nesting depth")
    if not isinstance(condition, Mapping):
        raise DomainValidationError("routing condition must be an object")
    if "fact" in condition or "op" in condition:
        _validate_leaf(condition, allowed_fact_keys=allowed_fact_keys)
        return
    if len(condition) != 1:
        raise DomainValidationError("boolean routing condition must contain exactly one operator")
    operator, payload = next(iter(condition.items()))
    if operator in {"all", "any"}:
        if not isinstance(payload, list) or not payload:
            raise DomainValidationError(f"{operator} requires a non-empty list")
        for child in payload:
            _validate(child, allowed_fact_keys=allowed_fact_keys, depth=depth + 1)
        return
    if operator == "not":
        _validate(payload, allowed_fact_keys=allowed_fact_keys, depth=depth + 1)
        return
    if operator == "label_score":
        _validate_comparison_payload(
            payload, allowed_fact_keys=allowed_fact_keys, numeric_only=True
        )
        return
    if operator == "label_present":
        if not isinstance(payload, Mapping) or set(payload) - {"fact", "minimum_score"}:
            raise DomainValidationError(
                "label_present only accepts fact and optional minimum_score"
            )
        _validate_fact(payload.get("fact"), allowed_fact_keys)
        threshold = payload.get("minimum_score", 0.0)
        if not _is_number(threshold) or not 0.0 <= threshold <= 1.0:
            raise DomainValidationError("label_present minimum_score must be in [0, 1]")
        return
    raise DomainValidationError(f"unsupported routing operator: {operator!r}")


def _validate_leaf(payload: Mapping[str, Any], *, allowed_fact_keys: set[str] | None) -> None:
    op = payload.get("op")
    if op == "exists":
        if set(payload) != {"fact", "op"}:
            raise DomainValidationError("exists requires exactly fact and op")
        _validate_fact(payload.get("fact"), allowed_fact_keys)
        return
    _validate_comparison_payload(payload, allowed_fact_keys=allowed_fact_keys, numeric_only=False)


def _validate_comparison_payload(
    payload: Any,
    *,
    allowed_fact_keys: set[str] | None,
    numeric_only: bool,
) -> None:
    if not isinstance(payload, Mapping) or set(payload) != {"fact", "op", "value"}:
        raise DomainValidationError("comparison requires exactly fact, op, and value")
    _validate_fact(payload["fact"], allowed_fact_keys)
    allowed_operators = _NUMERIC_COMPARISON_OPERATORS if numeric_only else _COMPARISON_OPERATORS
    if payload["op"] not in allowed_operators:
        raise DomainValidationError("unsupported comparison operator")
    if numeric_only and not _is_number(payload["value"]):
        raise DomainValidationError("label_score comparison value must be numeric")
    if not _is_json_value(payload["value"]):
        raise DomainValidationError("comparison value must be JSON-compatible")
    if payload["op"] == "in" and not isinstance(payload["value"], list):
        raise DomainValidationError("in comparison value must be a JSON array")


def _validate_fact(fact: Any, allowed_fact_keys: set[str] | None) -> None:
    if not isinstance(fact, str) or not _FACT_KEY_PATTERN.fullmatch(fact):
        raise DomainValidationError("fact must be a dotted identifier")
    if allowed_fact_keys is not None and fact not in allowed_fact_keys:
        raise DomainValidationError(f"fact is not allowed by the routing schema: {fact!r}")


def _evaluate(condition: Mapping[str, Any], facts: Mapping[str, Any]) -> bool | _Unknown:
    if "fact" in condition or "op" in condition:
        return _evaluate_leaf(condition, facts)
    operator, payload = next(iter(condition.items()))
    if operator == "all":
        saw_unknown = False
        for child in payload:
            result = _evaluate(child, facts)
            if result is False:
                return False
            saw_unknown = saw_unknown or result is _UNKNOWN
        return _UNKNOWN if saw_unknown else True
    if operator == "any":
        saw_unknown = False
        for child in payload:
            result = _evaluate(child, facts)
            if result is True:
                return True
            saw_unknown = saw_unknown or result is _UNKNOWN
        return _UNKNOWN if saw_unknown else False
    if operator == "not":
        result = _evaluate(payload, facts)
        return _UNKNOWN if result is _UNKNOWN else not result
    if operator == "label_score":
        return _compare(
            facts.get(payload["fact"], _UNKNOWN),
            payload["op"],
            payload["value"],
            numeric_only=True,
        )
    if operator == "label_present":
        value = facts.get(payload["fact"], _UNKNOWN)
        if value is _UNKNOWN or not _is_number(value):
            return _UNKNOWN
        return value >= payload.get("minimum_score", 0.0)
    return _UNKNOWN


def _evaluate_leaf(payload: Mapping[str, Any], facts: Mapping[str, Any]) -> bool | _Unknown:
    if payload["op"] == "exists":
        if payload["fact"] not in facts:
            return _UNKNOWN
        return facts[payload["fact"]] is not None
    return _compare(
        facts.get(payload["fact"], _UNKNOWN),
        payload["op"],
        payload["value"],
        numeric_only=False,
    )


def _compare(actual: Any, operator: str, expected: Any, *, numeric_only: bool) -> bool | _Unknown:
    if actual is _UNKNOWN or actual is None:
        return _UNKNOWN
    if numeric_only and (not _is_number(actual) or not _is_number(expected)):
        return _UNKNOWN
    if operator in {"gt", "gte", "lt", "lte"}:
        if _is_number(actual) and _is_number(expected):
            return {
                "gt": actual > expected,
                "gte": actual >= expected,
                "lt": actual < expected,
                "lte": actual <= expected,
            }[operator]
        if isinstance(actual, str) and isinstance(expected, str):
            return {
                "gt": actual > expected,
                "gte": actual >= expected,
                "lt": actual < expected,
                "lte": actual <= expected,
            }[operator]
        return _UNKNOWN
    if operator in {"eq", "neq"}:
        if type(actual) is not type(expected):
            return _UNKNOWN
        equal = actual == expected
        return equal if operator == "eq" else not equal
    if operator == "in":
        if not isinstance(expected, (list, tuple, set, frozenset)):
            return _UNKNOWN
        return actual in expected
    if operator == "contains":
        if isinstance(actual, str):
            return actual.find(expected) >= 0 if isinstance(expected, str) else _UNKNOWN
        if isinstance(actual, (list, tuple, set, frozenset)):
            return expected in actual
        return _UNKNOWN
    if operator == "starts_with":
        if not isinstance(actual, str) or not isinstance(expected, str):
            return _UNKNOWN
        return actual.startswith(expected)
    return _UNKNOWN


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if _is_number(value):
        return float("-inf") < float(value) < float("inf")
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False
