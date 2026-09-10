from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tgcurator.application.ports.routing import (
    PublicationIntentDraft,
    RoutingEvaluationDraft,
    RoutingExecutionResult,
    RoutingSnapshot,
    validate_routing_request_reuse,
)
from tgcurator.domain.review import (
    LabelValue,
    ManualLabelEvent,
    ManualLabelOperation,
    ReviewStatus,
)
from tgcurator.domain.routing import (
    PublicationAction,
    RoutingDecision,
    RoutingPolicy,
    RoutingRule,
    RuleOutcome,
)
from tgcurator.shared import DomainValidationError

from .models import (
    AnalysisRunRecord,
    DestinationChannel,
    ImageAssetRecord,
    ManualLabelAssignmentRecord,
    MessageRecord,
    ModelLabelAssignmentRecord,
    PublicationIntentRecord,
    RenderingTemplateVersionRecord,
    RoutingActionRecord,
    RoutingEvaluationRecord,
    RoutingPolicyVersionRecord,
    RoutingRuleRecord,
    SourceChannel,
    TelegramIdentity,
    VideoAssetRecord,
)
from .session import AsyncDatabase


def routing_evaluation_insert_statement(evaluation: RoutingEvaluationDraft):
    """Insert one evaluation or yield no row when the request was already persisted."""

    return (
        postgresql_insert(RoutingEvaluationRecord)
        .values(
            id=_as_uuid(evaluation.evaluation_id, field="evaluation_id"),
            routing_request_id=evaluation.routing_request_id,
            message_id=_as_uuid(evaluation.message_id, field="message_id"),
            routing_policy_version_id=_as_uuid(
                evaluation.routing_policy_version_id,
                field="routing_policy_version_id",
            ),
            facts_snapshot=dict(evaluation.facts_snapshot),
            facts_hash=evaluation.facts_hash,
            rule_outcomes=[dict(outcome) for outcome in evaluation.rule_outcomes],
            stopped_at_rule_id=_optional_uuid(
                evaluation.stopped_at_rule_id,
                field="stopped_at_rule_id",
            ),
        )
        .on_conflict_do_nothing(index_elements=[RoutingEvaluationRecord.routing_request_id])
        .returning(RoutingEvaluationRecord.id)
    )


class SqlAlchemyRoutingRepository:
    """Load PostgreSQL facts and atomically persist deterministic routing output."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def find_by_request_id(self, *, routing_request_id: str) -> RoutingExecutionResult | None:
        if not isinstance(routing_request_id, str) or not routing_request_id.strip():
            raise DomainValidationError("routing_request_id must not be blank")
        async with self._database.session() as session:
            return await _load_execution_by_request(
                session=session,
                routing_request_id=routing_request_id,
            )

    async def load_snapshot(
        self,
        *,
        message_id: str,
        routing_policy_version_id: str,
    ) -> RoutingSnapshot:
        message_uuid = _as_uuid(message_id, field="message_id")
        policy_version_uuid = _as_uuid(
            routing_policy_version_id,
            field="routing_policy_version_id",
        )
        async with self._database.session(isolation_level="REPEATABLE READ") as session:
            selected = (
                await session.execute(
                    select(MessageRecord, SourceChannel)
                    .join(SourceChannel, SourceChannel.id == MessageRecord.source_channel_id)
                    .where(MessageRecord.id == message_uuid)
                )
            ).one_or_none()
            if selected is None:
                raise DomainValidationError("message does not exist")
            message, source_channel = selected

            policy_version = await session.scalar(
                select(RoutingPolicyVersionRecord).where(
                    RoutingPolicyVersionRecord.id == policy_version_uuid
                )
            )
            if policy_version is None or policy_version.state != "published":
                raise DomainValidationError(
                    "routing requires the selected published policy version"
                )

            rules = tuple(
                await session.scalars(
                    select(RoutingRuleRecord)
                    .where(RoutingRuleRecord.routing_policy_version_id == policy_version_uuid)
                    .order_by(RoutingRuleRecord.priority.desc(), RoutingRuleRecord.id)
                )
            )
            actions = await _load_actions(session=session, rules=rules)
            policy, destination_defaults = await _build_policy(
                session=session,
                policy_version=policy_version,
                rules=rules,
                actions=actions,
            )
            model_assignments = await _load_current_model_assignments(
                session=session,
                message_id=message_uuid,
            )
            manual_events = await _load_manual_events(
                session=session,
                message_id=message_uuid,
            )
            images = tuple(
                await session.scalars(
                    select(ImageAssetRecord)
                    .where(ImageAssetRecord.message_id == message_uuid)
                    .order_by(ImageAssetRecord.id)
                )
            )
            videos = tuple(
                await session.scalars(
                    select(VideoAssetRecord)
                    .where(VideoAssetRecord.message_id == message_uuid)
                    .order_by(VideoAssetRecord.id)
                )
            )
            duplicate_state = await _load_duplicate_state(
                session=session,
                message=message,
            )

        media: list[Mapping[str, Any]] = [_image_fact(row) for row in images]
        media.extend(_video_fact(row) for row in videos)
        return RoutingSnapshot(
            message_id=str(message.id),
            message=_message_fact(message),
            source_channel=_source_channel_fact(source_channel),
            duplicate_state=duplicate_state,
            media=tuple(media),
            review_status=ReviewStatus(message.review_status),
            model_assignments=model_assignments,
            manual_events=manual_events,
            policy=policy,
            destination_publish_identity_ids=destination_defaults,
        )

    async def persist_evaluation(
        self,
        *,
        evaluation: RoutingEvaluationDraft,
        decision: RoutingDecision,
        intents: tuple[PublicationIntentDraft, ...],
    ) -> RoutingExecutionResult:
        _validate_persistence_payload(
            evaluation=evaluation,
            decision=decision,
            intents=intents,
        )
        async with self._database.session() as session:
            async with session.begin():
                inserted_id = await session.scalar(routing_evaluation_insert_statement(evaluation))
                if inserted_id is None:
                    existing = await _load_execution_by_request(
                        session=session,
                        routing_request_id=evaluation.routing_request_id,
                    )
                    if existing is None:
                        raise RuntimeError("routing request conflict could not be reloaded")
                    validate_routing_request_reuse(
                        result=existing,
                        routing_request_id=evaluation.routing_request_id,
                        message_id=evaluation.message_id,
                        routing_policy_version_id=evaluation.routing_policy_version_id,
                    )
                    return _with_reused(existing)

                for intent in intents:
                    session.add(_intent_record(intent))

        return RoutingExecutionResult(
            evaluation_id=evaluation.evaluation_id,
            routing_request_id=evaluation.routing_request_id,
            message_id=evaluation.message_id,
            routing_policy_version_id=evaluation.routing_policy_version_id,
            facts_snapshot=evaluation.facts_snapshot,
            facts_hash=evaluation.facts_hash,
            decision=decision,
            intents=intents,
            persisted=True,
        )


async def _load_actions(
    *,
    session: AsyncSession,
    rules: Sequence[RoutingRuleRecord],
) -> tuple[RoutingActionRecord, ...]:
    rule_ids = tuple(rule.id for rule in rules)
    if not rule_ids:
        return ()
    return tuple(
        await session.scalars(
            select(RoutingActionRecord)
            .where(RoutingActionRecord.routing_rule_id.in_(rule_ids))
            .order_by(
                RoutingActionRecord.routing_rule_id,
                RoutingActionRecord.output_order,
                RoutingActionRecord.id,
            )
        )
    )


async def _build_policy(
    *,
    session: AsyncSession,
    policy_version: RoutingPolicyVersionRecord,
    rules: Sequence[RoutingRuleRecord],
    actions: Sequence[RoutingActionRecord],
) -> tuple[RoutingPolicy, dict[str, str]]:
    destination_ids = tuple(sorted({action.destination_channel_id for action in actions}))
    destinations = (
        tuple(
            await session.scalars(
                select(DestinationChannel).where(DestinationChannel.id.in_(destination_ids))
            )
        )
        if destination_ids
        else ()
    )
    destination_by_id = {row.id: row for row in destinations}
    if set(destination_by_id) != set(destination_ids) or any(
        not row.enabled for row in destinations
    ):
        raise DomainValidationError("routing policy references a missing or disabled destination")

    identity_ids = tuple(
        sorted(
            {row.default_publish_identity_id for row in destinations}
            | {
                action.publish_identity_id
                for action in actions
                if action.publish_identity_id is not None
            }
        )
    )
    identities = (
        tuple(
            await session.scalars(
                select(TelegramIdentity).where(TelegramIdentity.id.in_(identity_ids))
            )
        )
        if identity_ids
        else ()
    )
    identity_by_id = {row.id: row for row in identities}
    if set(identity_by_id) != set(identity_ids) or any(not row.enabled for row in identities):
        raise DomainValidationError("routing policy references a missing or disabled identity")

    template_ids = tuple(
        sorted(
            {
                action.rendering_template_version_id
                for action in actions
                if action.rendering_template_version_id is not None
            }
        )
    )
    templates = (
        tuple(
            await session.scalars(
                select(RenderingTemplateVersionRecord).where(
                    RenderingTemplateVersionRecord.id.in_(template_ids)
                )
            )
        )
        if template_ids
        else ()
    )
    template_by_id = {row.id: row for row in templates}
    if set(template_by_id) != set(template_ids) or any(
        row.state != "published" for row in templates
    ):
        raise DomainValidationError(
            "routing policy references a missing or unpublished rendering template"
        )

    actions_by_rule: dict[UUID, list[RoutingActionRecord]] = {rule.id: [] for rule in rules}
    for action in actions:
        if action.routing_rule_id not in actions_by_rule:
            raise DomainValidationError("routing action belongs to a different policy version")
        actions_by_rule[action.routing_rule_id].append(action)

    domain_rules = tuple(
        RoutingRule(
            rule_id=str(rule.id),
            priority=rule.priority,
            condition=rule.condition,
            actions=tuple(
                PublicationAction(
                    action_id=str(action.id),
                    destination_channel_id=str(action.destination_channel_id),
                    publication_mode=action.publication_mode,
                    rendering_template_version_id=(
                        str(action.rendering_template_version_id)
                        if action.rendering_template_version_id is not None
                        else None
                    ),
                    publish_identity_id=(
                        str(action.publish_identity_id)
                        if action.publish_identity_id is not None
                        else None
                    ),
                    routing_rule_id=str(rule.id),
                )
                for action in sorted(
                    actions_by_rule[rule.id],
                    key=lambda item: (item.output_order, str(item.id)),
                )
            ),
            stop_on_match=rule.stop_on_match,
            enabled=rule.enabled,
        )
        for rule in rules
    )
    defaults = {
        str(destination.id): str(destination.default_publish_identity_id)
        for destination in destinations
    }
    return RoutingPolicy(policy_version_id=str(policy_version.id), rules=domain_rules), defaults


async def _load_current_model_assignments(
    *,
    session: AsyncSession,
    message_id: UUID,
) -> tuple[LabelValue, ...]:
    analysis_run = await session.scalar(
        select(AnalysisRunRecord)
        .where(
            AnalysisRunRecord.message_id == message_id,
            AnalysisRunRecord.run_mode.in_(("formal", "reanalysis")),
            AnalysisRunRecord.status.in_(("completed", "blocked_negative_gate")),
        )
        .order_by(AnalysisRunRecord.created_at.desc(), AnalysisRunRecord.id.desc())
        .limit(1)
    )
    if analysis_run is None:
        return ()
    rows = tuple(
        await session.scalars(
            select(ModelLabelAssignmentRecord)
            .where(ModelLabelAssignmentRecord.analysis_run_id == analysis_run.id)
            .order_by(
                ModelLabelAssignmentRecord.created_at,
                ModelLabelAssignmentRecord.id,
            )
        )
    )
    return tuple(_model_label_from_record(row) for row in rows)


async def _load_manual_events(
    *,
    session: AsyncSession,
    message_id: UUID,
) -> tuple[ManualLabelEvent, ...]:
    rows = tuple(
        await session.scalars(
            select(ManualLabelAssignmentRecord)
            .where(ManualLabelAssignmentRecord.message_id == message_id)
            .order_by(
                ManualLabelAssignmentRecord.created_at,
                ManualLabelAssignmentRecord.id,
            )
        )
    )
    return tuple(_manual_label_from_record(row) for row in rows)


async def _load_duplicate_state(
    *,
    session: AsyncSession,
    message: MessageRecord,
) -> Mapping[str, Any]:
    if message.visual_fingerprint is None:
        return {
            "status": "unknown",
            "canonical_message_id": None,
            "matching_message_ids": [],
            "match_count": 0,
        }
    matches = tuple(
        await session.scalars(
            select(MessageRecord)
            .where(MessageRecord.visual_fingerprint == message.visual_fingerprint)
            .order_by(MessageRecord.published_at, MessageRecord.id)
        )
    )
    canonical = matches[0] if matches else message
    other_ids = [str(row.id) for row in matches if row.id != message.id]
    if not other_ids:
        status = "unique"
    elif canonical.id == message.id:
        status = "canonical"
    else:
        status = "duplicate"
    return {
        "status": status,
        "canonical_message_id": str(canonical.id),
        "matching_message_ids": other_ids,
        "match_count": len(other_ids),
        "visual_fingerprint": message.visual_fingerprint,
    }


async def _load_execution_by_request(
    *,
    session: AsyncSession,
    routing_request_id: str,
) -> RoutingExecutionResult | None:
    evaluation = await session.scalar(
        select(RoutingEvaluationRecord).where(
            RoutingEvaluationRecord.routing_request_id == routing_request_id
        )
    )
    if evaluation is None:
        return None
    intents = tuple(
        await session.scalars(
            select(PublicationIntentRecord).where(
                PublicationIntentRecord.routing_evaluation_id == evaluation.id
            )
        )
    )
    action_ids = tuple(sorted({row.routing_action_id for row in intents}))
    action_records = (
        tuple(
            await session.scalars(
                select(RoutingActionRecord).where(RoutingActionRecord.id.in_(action_ids))
            )
        )
        if action_ids
        else ()
    )
    action_by_id = {row.id: row for row in action_records}
    if set(action_by_id) != set(action_ids):
        raise DomainValidationError("stored publication intent references a missing routing action")
    outcomes = tuple(_rule_outcome_from_snapshot(item) for item in evaluation.rule_outcomes)
    rank = {outcome.rule_id: index for index, outcome in enumerate(outcomes)}
    ordered_intents = tuple(
        sorted(
            intents,
            key=lambda row: (
                rank.get(str(row.routing_rule_id), len(rank)),
                action_by_id[row.routing_action_id].output_order,
                str(row.id),
            ),
        )
    )
    intent_drafts = tuple(_intent_draft_from_record(row) for row in ordered_intents)
    decision = RoutingDecision(
        policy_version_id=str(evaluation.routing_policy_version_id),
        outcomes=outcomes,
        actions=tuple(
            PublicationAction(
                action_id=str(row.routing_action_id),
                destination_channel_id=str(row.destination_channel_id),
                publication_mode=row.publication_mode,
                rendering_template_version_id=(
                    str(row.rendering_template_version_id)
                    if row.rendering_template_version_id is not None
                    else None
                ),
                publish_identity_id=str(row.publish_identity_id),
                routing_rule_id=str(row.routing_rule_id),
            )
            for row in ordered_intents
        ),
        stopped_at_rule_id=(
            str(evaluation.stopped_at_rule_id)
            if evaluation.stopped_at_rule_id is not None
            else None
        ),
    )
    return RoutingExecutionResult(
        evaluation_id=str(evaluation.id),
        routing_request_id=evaluation.routing_request_id,
        message_id=str(evaluation.message_id),
        routing_policy_version_id=str(evaluation.routing_policy_version_id),
        facts_snapshot=evaluation.facts_snapshot,
        facts_hash=evaluation.facts_hash,
        decision=decision,
        intents=intent_drafts,
        persisted=True,
    )


def _validate_persistence_payload(
    *,
    evaluation: RoutingEvaluationDraft,
    decision: RoutingDecision,
    intents: tuple[PublicationIntentDraft, ...],
) -> None:
    if decision.policy_version_id != evaluation.routing_policy_version_id:
        raise DomainValidationError("routing decision policy does not match evaluation")
    if tuple(dict(item) for item in evaluation.rule_outcomes) != tuple(
        {
            "rule_id": item.rule_id,
            "enabled": item.enabled,
            "checked": item.checked,
            "evaluated": item.evaluated,
            "matched": item.matched,
            "stopped_after_match": item.stopped_after_match,
        }
        for item in decision.outcomes
    ):
        raise DomainValidationError("routing decision outcomes do not match evaluation snapshot")
    if evaluation.stopped_at_rule_id != decision.stopped_at_rule_id:
        raise DomainValidationError("routing stop position does not match evaluation")
    if len(intents) != len(decision.actions):
        raise DomainValidationError("publication intents do not match routing actions")
    for action, intent in zip(decision.actions, intents, strict=True):
        if (
            intent.routing_evaluation_id != evaluation.evaluation_id
            or intent.routing_request_id != evaluation.routing_request_id
            or intent.message_id != evaluation.message_id
            or intent.routing_policy_version_id != evaluation.routing_policy_version_id
            or intent.status != "pending"
        ):
            raise DomainValidationError("publication intent identity does not match evaluation")
        if action.routing_rule_id is None:
            raise DomainValidationError("matched routing action has no owning rule")
        if (
            intent.routing_rule_id != action.routing_rule_id
            or intent.routing_action_id != action.action_id
            or intent.destination_channel_id != action.destination_channel_id
            or intent.publication_mode != action.publication_mode
            or intent.rendering_template_version_id != action.rendering_template_version_id
            or (
                action.publish_identity_id is not None
                and intent.publish_identity_id != action.publish_identity_id
            )
        ):
            raise DomainValidationError("publication intent does not match routing action")


def _intent_record(intent: PublicationIntentDraft) -> PublicationIntentRecord:
    return PublicationIntentRecord(
        id=_as_uuid(intent.intent_id, field="intent_id"),
        routing_evaluation_id=_as_uuid(
            intent.routing_evaluation_id,
            field="routing_evaluation_id",
        ),
        routing_request_id=intent.routing_request_id,
        message_id=_as_uuid(intent.message_id, field="message_id"),
        routing_policy_version_id=_as_uuid(
            intent.routing_policy_version_id,
            field="routing_policy_version_id",
        ),
        routing_rule_id=_as_uuid(intent.routing_rule_id, field="routing_rule_id"),
        routing_action_id=_as_uuid(intent.routing_action_id, field="routing_action_id"),
        destination_channel_id=_as_uuid(
            intent.destination_channel_id,
            field="destination_channel_id",
        ),
        publish_identity_id=_as_uuid(
            intent.publish_identity_id,
            field="publish_identity_id",
        ),
        publication_mode=intent.publication_mode,
        rendering_template_version_id=_optional_uuid(
            intent.rendering_template_version_id,
            field="rendering_template_version_id",
        ),
        business_idempotency_key=intent.business_idempotency_key,
        status="pending",
    )


def _intent_draft_from_record(row: PublicationIntentRecord) -> PublicationIntentDraft:
    return PublicationIntentDraft(
        intent_id=str(row.id),
        routing_evaluation_id=str(row.routing_evaluation_id),
        routing_request_id=row.routing_request_id,
        message_id=str(row.message_id),
        routing_policy_version_id=str(row.routing_policy_version_id),
        routing_rule_id=str(row.routing_rule_id),
        routing_action_id=str(row.routing_action_id),
        destination_channel_id=str(row.destination_channel_id),
        publish_identity_id=str(row.publish_identity_id),
        publication_mode=row.publication_mode,
        rendering_template_version_id=(
            str(row.rendering_template_version_id)
            if row.rendering_template_version_id is not None
            else None
        ),
        business_idempotency_key=row.business_idempotency_key,
        status=row.status,
    )


def _rule_outcome_from_snapshot(value: Mapping[str, Any]) -> RuleOutcome:
    try:
        return RuleOutcome(
            rule_id=str(value["rule_id"]),
            enabled=_stored_bool(value["enabled"]),
            checked=_stored_bool(value["checked"]),
            evaluated=_stored_bool(value["evaluated"]),
            matched=_stored_bool(value["matched"]),
            stopped_after_match=_stored_bool(value["stopped_after_match"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainValidationError("stored routing outcome is invalid") from exc


def _stored_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("stored outcome flag is not boolean")
    return value


def _model_label_from_record(row: ModelLabelAssignmentRecord) -> LabelValue:
    return LabelValue(
        message_id=str(row.message_id),
        target_scope=row.target_scope,
        target_kind=row.target_kind,
        target_id=str(row.target_id),
        image_asset_id=(str(row.image_asset_id) if row.image_asset_id is not None else None),
        video_asset_id=(str(row.video_asset_id) if row.video_asset_id is not None else None),
        label_definition_version_id=str(row.label_definition_version_id),
        label_key=row.label_key,
        score=row.score,
        activated=row.activated,
    )


def _manual_label_from_record(row: ManualLabelAssignmentRecord) -> ManualLabelEvent:
    return ManualLabelEvent(
        event_id=str(row.id),
        message_id=str(row.message_id),
        target_scope=row.target_scope,
        target_kind=row.target_kind,
        target_id=str(row.target_id),
        image_asset_id=(str(row.image_asset_id) if row.image_asset_id is not None else None),
        video_asset_id=(str(row.video_asset_id) if row.video_asset_id is not None else None),
        label_definition_version_id=str(row.label_definition_version_id),
        label_key=row.label_key,
        operation=ManualLabelOperation(row.operation),
        score=row.score,
        activated=row.activated,
        actor_admin_user_id=str(row.actor_admin_user_id),
        created_at=row.created_at,
        reason=row.reason,
    )


def _message_fact(row: MessageRecord) -> Mapping[str, Any]:
    return {
        "id": str(row.id),
        "source_channel_id": str(row.source_channel_id),
        "telegram_message_ids": list(row.telegram_message_ids),
        "telegram_grouped_id": row.telegram_grouped_id,
        "primary_telegram_message_id": row.primary_telegram_message_id,
        "published_at": _datetime_text(row.published_at),
        "edited_at": _datetime_text(row.edited_at),
        "original_text": row.original_text,
        "telegram_metadata": row.telegram_metadata,
        "processing_status": row.processing_status,
        "review_status": row.review_status,
        "media_count": row.media_count,
        "visual_fingerprint": row.visual_fingerprint,
        "source_changed_after_processing": row.source_changed_after_processing,
        "source_deleted": row.source_deleted,
        "source_deleted_at": _datetime_text(row.source_deleted_at),
        "blocked_from_analysis": row.blocked_from_analysis,
    }


def _source_channel_fact(row: SourceChannel) -> Mapping[str, Any]:
    return {
        "id": str(row.id),
        "telegram_peer_id": row.telegram_peer_id,
        "username": row.username,
        "display_name": row.display_name,
        "enabled": row.enabled,
        "active_profile_version_id": (
            str(row.active_profile_version_id)
            if row.active_profile_version_id is not None
            else None
        ),
    }


def _image_fact(row: ImageAssetRecord) -> Mapping[str, Any]:
    return {
        "kind": "image",
        "asset_id": str(row.id),
        "source_asset_id": row.source_asset_id,
        "source_telegram_message_id": row.source_telegram_message_id,
        "archive_state": row.archive_state,
        "content_type": row.content_type,
        "width": row.width,
        "height": row.height,
        "source_sha256": row.source_sha256,
        "archive_sha256": row.archive_sha256,
        "perceptual_hash": row.perceptual_hash,
    }


def _video_fact(row: VideoAssetRecord) -> Mapping[str, Any]:
    return {
        "kind": "video",
        "asset_id": str(row.id),
        "source_asset_id": row.source_asset_id,
        "source_telegram_message_id": row.source_telegram_message_id,
        "archive_state": row.archive_state,
        "source_content_type": row.source_content_type,
        "source_size_bytes": row.source_size_bytes,
        "source_sha256": row.source_sha256,
        "source_cover_phash": row.source_cover_phash,
        "duration_seconds": row.duration_seconds,
        "width": row.source_width,
        "height": row.source_height,
    }


def _datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _with_reused(result: RoutingExecutionResult) -> RoutingExecutionResult:
    return RoutingExecutionResult(
        evaluation_id=result.evaluation_id,
        routing_request_id=result.routing_request_id,
        message_id=result.message_id,
        routing_policy_version_id=result.routing_policy_version_id,
        facts_snapshot=result.facts_snapshot,
        facts_hash=result.facts_hash,
        decision=result.decision,
        intents=result.intents,
        persisted=True,
        reused=True,
    )


def _as_uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainValidationError(f"{field} must be a UUID") from exc


def _optional_uuid(value: str | None, *, field: str) -> UUID | None:
    return None if value is None else _as_uuid(value, field=field)
