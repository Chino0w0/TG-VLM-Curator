from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tgcurator.domain.routing.dsl import evaluate_condition, validate_condition
from tgcurator.shared import DomainValidationError


def validate_run_if(
    condition: Mapping[str, Any] | None, *, allowed_fact_keys: set[str] | None = None
) -> None:
    if condition is None:
        return
    if not isinstance(condition, Mapping):
        raise DomainValidationError("run_if must be an object or null")
    validate_condition(condition, allowed_fact_keys=allowed_fact_keys)


def evaluate_run_if(condition: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> bool:
    """Missing or incompatible facts stay unknown, including below a not node."""
    if condition is None:
        return True
    if not isinstance(facts, Mapping):
        return False
    return evaluate_condition(condition, facts)
