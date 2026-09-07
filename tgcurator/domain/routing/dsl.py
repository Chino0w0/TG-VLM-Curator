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
    """Evaluate a validated condition defensively; unknown facts never match."""
    try:
        _validate(condition, allowed_fact_keys=None, depth=0)
        return _evaluate(condition, facts)
    except (DomainValidationError, TypeError, ValueError):
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


def _validate_fact(fact: Any, allowed_fact_keys: set[str] | None) -> None:
    if not isinstance(fact, str) or not _FACT_KEY_PATTERN.fullmatch(fact):
        raise DomainValidationError("fact must be a dotted identifier")
    if allowed_fact_keys is not None and fact not in allowed_fact_keys:
        raise DomainValidationError(f"fact is not allowed by the routing schema: {fact!r}")


def _evaluate(condition: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    if "fact" in condition or "op" in condition:
        return _evaluate_leaf(condition, facts)
    operator, payload = next(iter(condition.items()))
    if operator == "all":
        return all(_evaluate(child, facts) for child in payload)
    if operator == "any":
        return any(_evaluate(child, facts) for child in payload)
    if operator == "not":
        return not _evaluate(payload, facts)
    if operator == "label_score":
        return _compare(
            facts.get(payload["fact"], _MISSING), payload["op"], payload["value"], numeric_only=True
        )
    if operator == "label_present":
        value = facts.get(payload["fact"], _MISSING)
        return _is_number(value) and value >= payload.get("minimum_score", 0.0)
    return False


def _evaluate_leaf(payload: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    if payload["op"] == "exists":
        return payload["fact"] in facts and facts[payload["fact"]] is not None
    return _compare(
        facts.get(payload["fact"], _MISSING),
        payload["op"],
        payload["value"],
        numeric_only=False,
    )


class _Missing:
    pass


_MISSING = _Missing()


def _compare(actual: Any, operator: str, expected: Any, *, numeric_only: bool) -> bool:
    if actual is _MISSING or actual is None:
        return False
    if numeric_only and (not _is_number(actual) or not _is_number(expected)):
        return False
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
        return False
    if operator == "eq":
        return type(actual) is type(expected) and actual == expected
    if operator == "neq":
        return type(actual) is type(expected) and actual != expected
    if operator == "in":
        return isinstance(expected, (list, tuple, set, frozenset)) and actual in expected
    if operator == "contains":
        return isinstance(actual, (str, list, tuple, set, frozenset)) and expected in actual
    if operator == "starts_with":
        return isinstance(actual, str) and isinstance(expected, str) and actual.startswith(expected)
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)
