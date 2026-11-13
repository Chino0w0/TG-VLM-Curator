from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

from tgcurator.application.ports.routing import (
    PublicationIntentDraft,
    RoutingEvaluationDraft,
    RoutingExecutionResult,
    RoutingRepository,
    RoutingRequest,
    RoutingSnapshot,
    validate_routing_request_reuse,
)
from tgcurator.domain.publishing import PublicationMode, publication_idempotency_key
from tgcurator.domain.review import labels_to_routing_snapshot, resolve_label_namespaces
from tgcurator.domain.routing import (
    RoutingDecision,
    canonical_facts_snapshot,
    evaluate_routing,
    facts_snapshot_hash,
)
from tgcurator.shared import DomainValidationError

from .rendering import RestrictedTemplateRenderer, build_rendering_context


class RoutingService:
    """Evaluate published routing policy snapshots without network side effects."""

    def __init__(
        self,
        repository: RoutingRepository,
        *,
        id_factory: Callable[[], UUID] | None = None,
        renderer: RestrictedTemplateRenderer | None = None,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory or uuid4
        self._renderer = renderer or RestrictedTemplateRenderer()

    async def evaluate(self, request: RoutingRequest) -> RoutingExecutionResult:
        if not isinstance(request, RoutingRequest):
            raise DomainValidationError("request must be a RoutingRequest")

        if not request.dry_run:
            existing = await self._repository.find_by_request_id(
                routing_request_id=request.request_id
            )
            if existing is not None:
                validate_routing_request_reuse(
                    result=existing,
                    routing_request_id=request.request_id,
                    message_id=request.message_id,
                    routing_policy_version_id=request.routing_policy_version_id,
                )
                return _as_reused(existing)

        snapshot = await self._repository.load_snapshot(
            message_id=request.message_id,
            routing_policy_version_id=request.routing_policy_version_id,
        )
        if snapshot.message_id != request.message_id:
            raise DomainValidationError("routing snapshot message does not match request")
        if snapshot.policy.policy_version_id != request.routing_policy_version_id:
            raise DomainValidationError("routing snapshot policy does not match request")

        facts = build_routing_facts(snapshot)
        decision = evaluate_routing(snapshot.policy, facts)
        evaluation_id = str(self._id_factory())
        evaluation = RoutingEvaluationDraft(
            evaluation_id=evaluation_id,
            routing_request_id=request.request_id,
            message_id=request.message_id,
            routing_policy_version_id=request.routing_policy_version_id,
            facts_snapshot=facts,
            facts_hash=facts_snapshot_hash(facts),
            rule_outcomes=tuple(asdict(outcome) for outcome in decision.outcomes),
            stopped_at_rule_id=decision.stopped_at_rule_id,
        )
        intents = self._build_intents(
            evaluation=evaluation,
            snapshot=snapshot,
            decision=decision,
        )

        if request.dry_run:
            return RoutingExecutionResult(
                evaluation_id=evaluation.evaluation_id,
                routing_request_id=evaluation.routing_request_id,
                message_id=evaluation.message_id,
                routing_policy_version_id=evaluation.routing_policy_version_id,
                facts_snapshot=evaluation.facts_snapshot,
                facts_hash=evaluation.facts_hash,
                decision=decision,
                intents=intents,
                persisted=False,
                dry_run=True,
            )

        return await self._repository.persist_evaluation(
            evaluation=evaluation,
            decision=decision,
            intents=intents,
        )

    def _build_intents(
        self,
        *,
        evaluation: RoutingEvaluationDraft,
        snapshot: RoutingSnapshot,
        decision: RoutingDecision,
    ) -> tuple[PublicationIntentDraft, ...]:
        intents: list[PublicationIntentDraft] = []
        rendering_context = build_rendering_context(evaluation.facts_snapshot)
        for action in decision.actions:
            if action.routing_rule_id is None:
                raise DomainValidationError("matched routing action has no owning rule")
            publish_identity_id = action.publish_identity_id
            if publish_identity_id is None:
                publish_identity_id = snapshot.destination_publish_identity_ids.get(
                    action.destination_channel_id
                )
            if not isinstance(publish_identity_id, str) or not publish_identity_id.strip():
                raise DomainValidationError("destination channel has no usable publish identity")
            mode = PublicationMode(action.publication_mode)
            rendered_text: str | None = None
            rendered_context: dict[str, Any] = {}
            rendered_snapshot_hash: str | None = None
            if mode is not PublicationMode.FORWARD_ONLY:
                template_version_id = action.rendering_template_version_id
                if template_version_id is None:
                    raise DomainValidationError("rendered publication action has no template")
                template_text = snapshot.rendering_template_texts.get(template_version_id)
                if template_text is None:
                    raise DomainValidationError("rendering template text is unavailable")
                rendered = self._renderer.render(
                    template_text=template_text,
                    context=rendering_context,
                )
                rendered_text = rendered.text
                rendered_context = dict(rendered.context)
                rendered_snapshot_hash = rendered.snapshot_hash
            intent_id = str(self._id_factory())
            intents.append(
                PublicationIntentDraft(
                    intent_id=intent_id,
                    routing_evaluation_id=evaluation.evaluation_id,
                    routing_request_id=evaluation.routing_request_id,
                    message_id=evaluation.message_id,
                    routing_policy_version_id=evaluation.routing_policy_version_id,
                    routing_rule_id=action.routing_rule_id,
                    routing_action_id=action.action_id,
                    destination_channel_id=action.destination_channel_id,
                    publish_identity_id=publish_identity_id,
                    publication_mode=mode.value,
                    rendering_template_version_id=action.rendering_template_version_id,
                    protected_content_fallback=action.protected_content_fallback,
                    supplement_as_reply=action.supplement_as_reply,
                    rendered_text=rendered_text,
                    rendered_context=rendered_context,
                    rendered_snapshot_hash=rendered_snapshot_hash,
                    business_idempotency_key=publication_idempotency_key(
                        source_message_id=evaluation.message_id,
                        destination_channel_id=action.destination_channel_id,
                        routing_policy_version_id=evaluation.routing_policy_version_id,
                        routing_rule_id=action.routing_rule_id,
                        action_id=action.action_id,
                        publication_mode=mode,
                        routing_request_id=evaluation.routing_request_id,
                        routing_evaluation_id=evaluation.evaluation_id,
                    ),
                )
            )
        return tuple(intents)


def build_routing_facts(snapshot: RoutingSnapshot) -> dict[str, Any]:
    resolved = resolve_label_namespaces(
        snapshot.model_assignments,
        snapshot.manual_events,
    )
    label_snapshot = labels_to_routing_snapshot(resolved)
    message = dict(snapshot.message)
    message.setdefault("id", snapshot.message_id)
    message["review_status"] = snapshot.review_status.value
    blocked = message.get("blocked_from_analysis", False)
    facts = {
        "message": message,
        "source_channel": dict(snapshot.source_channel),
        "duplicate": dict(snapshot.duplicate_state),
        "media": [dict(item) for item in snapshot.media],
        "blocked_from_analysis": blocked,
        "review_status": snapshot.review_status.value,
        "labels": label_snapshot,
        "model": _flat_namespace_scores(resolved.model),
        "manual": _flat_namespace_scores(resolved.manual),
        "effective": _flat_namespace_scores(resolved.effective),
    }
    return canonical_facts_snapshot(facts)


def _flat_namespace_scores(values: tuple[Any, ...]) -> dict[str, dict[str, float]]:
    global_scores: dict[str, float] = {}
    media_scores: dict[str, float] = {}
    for value in values:
        score = float(value.score)
        if value.target_scope == "global":
            global_scores[value.label_key] = score
        else:
            existing = media_scores.get(value.label_key)
            media_scores[value.label_key] = score if existing is None else max(existing, score)
    return {"global": global_scores, "media": media_scores}


def _as_reused(result: RoutingExecutionResult) -> RoutingExecutionResult:
    return RoutingExecutionResult(
        evaluation_id=result.evaluation_id,
        routing_request_id=result.routing_request_id,
        message_id=result.message_id,
        routing_policy_version_id=result.routing_policy_version_id,
        facts_snapshot=result.facts_snapshot,
        facts_hash=result.facts_hash,
        decision=result.decision,
        intents=result.intents,
        persisted=result.persisted,
        reused=True,
        dry_run=False,
    )
