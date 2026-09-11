from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tgcurator.shared import DomainValidationError

from .models import LabelValue, ManualLabelEvent, ManualLabelOperation, ResolvedLabelNamespaces


def resolve_label_namespaces(
    model_assignments: Iterable[LabelValue],
    manual_events: Iterable[ManualLabelEvent],
) -> ResolvedLabelNamespaces:
    """Resolve current namespaces without rewriting model or manual history."""
    model_by_identity: dict[tuple[str, str, str], LabelValue] = {}
    for assignment in model_assignments:
        model_by_identity[assignment.identity] = assignment

    latest_manual: dict[tuple[str, str, str], ManualLabelEvent] = {}
    for event in sorted(manual_events, key=lambda item: (item.created_at, item.event_id)):
        latest_manual[event.identity] = event

    manual_by_identity = {
        identity: event.as_label_value()
        for identity, event in latest_manual.items()
        if event.operation is ManualLabelOperation.SET
    }
    effective_by_identity = dict(model_by_identity)
    for identity, event in latest_manual.items():
        if event.operation is ManualLabelOperation.SET:
            effective_by_identity[identity] = event.as_label_value()
        else:
            effective_by_identity.pop(identity, None)
            if identity in model_by_identity:
                effective_by_identity[identity] = model_by_identity[identity]

    return ResolvedLabelNamespaces(
        model=_ordered(model_by_identity.values()),
        manual=_ordered(manual_by_identity.values()),
        effective=_ordered(effective_by_identity.values()),
    )


def labels_to_routing_snapshot(resolved: ResolvedLabelNamespaces) -> dict[str, Any]:
    return {
        "model": _namespace_snapshot(resolved.model),
        "manual": _namespace_snapshot(resolved.manual),
        "effective": _namespace_snapshot(resolved.effective),
    }


def _ordered(values: Iterable[LabelValue]) -> tuple[LabelValue, ...]:
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.target_scope,
                item.target_id,
                item.label_key,
                item.label_definition_version_id,
            ),
        )
    )


def _namespace_snapshot(values: tuple[LabelValue, ...]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"global": {}, "media": {}}
    for value in values:
        label = {
            "label_definition_version_id": value.label_definition_version_id,
            "score": float(value.score),
            "activated": value.activated,
        }
        if value.target_scope == "global":
            labels = snapshot["global"]
            _store_unambiguous_label(labels=labels, value=value, label=label)
            continue
        media = snapshot["media"].setdefault(
            value.target_id,
            {"target_kind": value.target_kind, "labels": {}},
        )
        _store_unambiguous_label(labels=media["labels"], value=value, label=label)
    return snapshot


def _store_unambiguous_label(
    *,
    labels: dict[str, Any],
    value: LabelValue,
    label: dict[str, Any],
) -> None:
    if value.label_key in labels:
        raise DomainValidationError(
            "routing snapshot cannot represent multiple label definition versions "
            f"for {value.target_scope} target {value.target_id!r} "
            f"and label key {value.label_key!r}"
        )
    labels[value.label_key] = label
