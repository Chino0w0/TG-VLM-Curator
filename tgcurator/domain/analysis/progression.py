from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import AnalysisRunStatus, CachePolicy, LabelScope
from .conditions import evaluate_run_if, validate_run_if

_ACTIVE_STAGE_STATUSES = frozenset({"pending", "running", "retry_wait"})


class PipelineNodeActionKind(StrEnum):
    SCHEDULE = "schedule"
    SKIP_CONDITION = "skip_condition"


@dataclass(frozen=True, slots=True)
class PipelineNodeSpec:
    """Topological stage node descriptor used for stateless DAG progression planning."""

    node_id: str
    node_key: str
    stage_version_id: str
    target_scope: LabelScope
    input_requires_media: bool
    is_text_only_prescreen: bool
    cache_policy: CachePolicy
    max_attempts: int
    depends_on: tuple[str, ...] = ()
    run_if: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.node_key.strip():
            raise DomainValidationError("pipeline node identifiers must not be blank")
        if not self.stage_version_id.strip():
            raise DomainValidationError("stage_version_id must not be blank")
        if not isinstance(self.target_scope, LabelScope):
            raise DomainValidationError("target_scope must be a LabelScope")
        if not isinstance(self.cache_policy, CachePolicy):
            raise DomainValidationError("cache_policy must be a CachePolicy")
        if self.max_attempts <= 0:
            raise DomainValidationError("max_attempts must be positive")
        validate_run_if(self.run_if)


@dataclass(frozen=True, slots=True)
class ExistingNodeTargetRun:
    target_kind: str
    target_id: str
    status: str


@dataclass(frozen=True, slots=True)
class MediaAssetTarget:
    target_kind: str
    asset_id: str
    archive_state: str

    def __post_init__(self) -> None:
        if self.target_kind not in {"image_asset", "video_asset"}:
            raise DomainValidationError("media target_kind must be image_asset or video_asset")
        if not self.asset_id.strip():
            raise DomainValidationError("asset_id must not be blank")


@dataclass(frozen=True, slots=True)
class MessageMediaReadiness:
    assets: tuple[MediaAssetTarget, ...] = ()

    @property
    def has_media(self) -> bool:
        return bool(self.assets)

    @property
    def any_failed(self) -> bool:
        return any(asset.archive_state == "failed" for asset in self.assets)

    @property
    def any_pending_archive(self) -> bool:
        return any(
            asset.archive_state in {"pending", "processing", "retry_wait"} for asset in self.assets
        )

    @property
    def all_settled(self) -> bool:
        return all(asset.archive_state in {"ready", "deleted", "failed"} for asset in self.assets)

    @property
    def ready_assets(self) -> tuple[MediaAssetTarget, ...]:
        return tuple(asset for asset in self.assets if asset.archive_state == "ready")


@dataclass(frozen=True, slots=True)
class PlannedNodeTarget:
    node_id: str
    node_key: str
    stage_version_id: str
    target_scope: LabelScope
    target_kind: str
    target_id: str
    cache_policy: CachePolicy
    max_attempts: int
    action: PipelineNodeActionKind


@dataclass(frozen=True, slots=True)
class PipelineProgressionPlan:
    planned_targets: tuple[PlannedNodeTarget, ...]
    waiting_for_media: bool
    waiting_for_active_stages: bool
    run_status: AnalysisRunStatus
    all_nodes_skipped_or_empty: bool


def pipeline_has_prescreen_root(nodes: Sequence[PipelineNodeSpec]) -> bool:
    """Return True when at least one root DAG node can run before media archiving."""

    return any(not node.depends_on and node.is_text_only_prescreen for node in nodes)


def plan_pipeline_progression(
    *,
    message_id: str,
    nodes: Sequence[PipelineNodeSpec],
    existing_runs_by_node: Mapping[str, Sequence[ExistingNodeTargetRun]],
    media: MessageMediaReadiness,
    facts: Mapping[str, Any],
    blocked_from_analysis: bool,
) -> PipelineProgressionPlan:
    """Plan the next deterministic batch of StageRuns for one AnalysisRun."""

    if not isinstance(message_id, str) or not message_id.strip():
        raise DomainValidationError("message_id must not be blank")

    all_existing = [run for runs in existing_runs_by_node.values() for run in runs]
    has_active_existing = any(run.status in _ACTIVE_STAGE_STATUSES for run in all_existing)
    has_failed_existing = any(run.status == "failed" for run in all_existing)

    if blocked_from_analysis:
        return PipelineProgressionPlan(
            planned_targets=(),
            waiting_for_media=False,
            waiting_for_active_stages=has_active_existing,
            run_status=(
                AnalysisRunStatus.RUNNING
                if has_active_existing
                else AnalysisRunStatus.BLOCKED_NEGATIVE_GATE
            ),
            all_nodes_skipped_or_empty=False,
        )

    if has_failed_existing:
        return PipelineProgressionPlan(
            planned_targets=(),
            waiting_for_media=False,
            waiting_for_active_stages=has_active_existing,
            run_status=(
                AnalysisRunStatus.RUNNING if has_active_existing else AnalysisRunStatus.FAILED
            ),
            all_nodes_skipped_or_empty=False,
        )

    ordered_nodes = _topological_order(nodes)
    resolutions: dict[str, str] = {}
    planned: list[PlannedNodeTarget] = []
    waiting_for_media = False
    any_succeeded_stage = any(run.status == "succeeded" for run in all_existing)

    for node in ordered_nodes:
        existing = tuple(existing_runs_by_node.get(node.node_key, ()))
        existing_by_target = {run.target_id: run for run in existing}

        if any(run.status in _ACTIVE_STAGE_STATUSES for run in existing):
            resolutions[node.node_key] = "active"
            continue
        if any(run.status == "failed" for run in existing):
            resolutions[node.node_key] = "failed"
            continue

        dep_states = [resolutions.get(dep_key, "missing") for dep_key in node.depends_on]
        if any(state == "failed" for state in dep_states):
            resolutions[node.node_key] = "failed"
            continue
        if any(state in {"active", "waiting_media", "waiting_upstream"} for state in dep_states):
            resolutions[node.node_key] = "waiting_upstream"
            continue

        requires_media = node.target_scope is LabelScope.MEDIA or node.input_requires_media
        if existing:
            # All instantiated targets for this node reached non-failed terminal states.
            if not (
                node.target_scope is LabelScope.MEDIA
                and media.has_media
                and media.any_pending_archive
            ):
                resolutions[node.node_key] = (
                    "satisfied" if any(run.status == "succeeded" for run in existing) else "skipped"
                )
                continue

        condition_met = evaluate_run_if(node.run_if, facts)
        if not condition_met:
            targets_to_skip = _skip_targets_for_node(
                message_id=message_id,
                node=node,
                media=media,
            )
            for target_kind, target_id in targets_to_skip:
                if target_id not in existing_by_target:
                    planned.append(
                        PlannedNodeTarget(
                            node_id=node.node_id,
                            node_key=node.node_key,
                            stage_version_id=node.stage_version_id,
                            target_scope=node.target_scope,
                            target_kind=target_kind,
                            target_id=target_id,
                            cache_policy=node.cache_policy,
                            max_attempts=node.max_attempts,
                            action=PipelineNodeActionKind.SKIP_CONDITION,
                        )
                    )
            resolutions[node.node_key] = "skipped"
            continue

        if requires_media and media.has_media:
            if media.any_failed:
                resolutions[node.node_key] = "failed"
                continue
            if media.any_pending_archive:
                waiting_for_media = True
                resolutions[node.node_key] = "waiting_media"
                continue

        runnable_targets = _runnable_targets_for_node(
            message_id=message_id,
            node=node,
            media=media,
        )
        if not runnable_targets:
            # E.g. MEDIA stage on a text-only message (0 media assets): vacuously satisfied.
            resolutions[node.node_key] = "skipped"
            continue

        newly_scheduled = False
        for target_kind, target_id in runnable_targets:
            if target_id not in existing_by_target:
                planned.append(
                    PlannedNodeTarget(
                        node_id=node.node_id,
                        node_key=node.node_key,
                        stage_version_id=node.stage_version_id,
                        target_scope=node.target_scope,
                        target_kind=target_kind,
                        target_id=target_id,
                        cache_policy=node.cache_policy,
                        max_attempts=node.max_attempts,
                        action=PipelineNodeActionKind.SCHEDULE,
                    )
                )
                newly_scheduled = True

        resolutions[node.node_key] = "active" if newly_scheduled else "satisfied"

    any_scheduled = any(item.action is PipelineNodeActionKind.SCHEDULE for item in planned)
    waiting_active = has_active_existing or any_scheduled
    any_waiting = (
        waiting_active
        or waiting_for_media
        or any(
            state in {"waiting_media", "waiting_upstream", "active"}
            for state in resolutions.values()
        )
    )
    any_failed = any(state == "failed" for state in resolutions.values())

    if any_waiting:
        run_status = AnalysisRunStatus.RUNNING
    elif any_failed:
        run_status = AnalysisRunStatus.FAILED
    else:
        run_status = AnalysisRunStatus.COMPLETED

    all_skipped_or_empty = (
        run_status is AnalysisRunStatus.COMPLETED and not any_succeeded_stage and not any_scheduled
    )
    return PipelineProgressionPlan(
        planned_targets=tuple(planned),
        waiting_for_media=waiting_for_media,
        waiting_for_active_stages=waiting_active,
        run_status=run_status,
        all_nodes_skipped_or_empty=all_skipped_or_empty,
    )


def _runnable_targets_for_node(
    *,
    message_id: str,
    node: PipelineNodeSpec,
    media: MessageMediaReadiness,
) -> tuple[tuple[str, str], ...]:
    if node.target_scope is LabelScope.GLOBAL:
        return (("message", message_id),)
    return tuple((asset.target_kind, asset.asset_id) for asset in media.ready_assets)


def _skip_targets_for_node(
    *,
    message_id: str,
    node: PipelineNodeSpec,
    media: MessageMediaReadiness,
) -> tuple[tuple[str, str], ...]:
    if node.target_scope is LabelScope.GLOBAL:
        return (("message", message_id),)
    return tuple((asset.target_kind, asset.asset_id) for asset in media.assets)


def _topological_order(nodes: Sequence[PipelineNodeSpec]) -> tuple[PipelineNodeSpec, ...]:
    by_key = {node.node_key: node for node in nodes}
    if len(by_key) != len(nodes):
        raise DomainValidationError("pipeline node keys must be unique")
    indegree: dict[str, int] = {node.node_key: 0 for node in nodes}
    dependents: dict[str, list[str]] = {node.node_key: [] for node in nodes}
    order_index = {node.node_key: index for index, node in enumerate(nodes)}
    for node in nodes:
        for dep in node.depends_on:
            if dep not in by_key:
                raise DomainValidationError(f"unknown pipeline node dependency: {dep}")
            indegree[node.node_key] += 1
            dependents[dep].append(node.node_key)

    ready = sorted(
        (key for key, deg in indegree.items() if deg == 0),
        key=lambda key: order_index[key],
    )
    ordered: list[PipelineNodeSpec] = []
    while ready:
        current = ready.pop(0)
        ordered.append(by_key[current])
        for child in dependents[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=lambda key: order_index[key])

    if len(ordered) != len(nodes):
        raise DomainValidationError("pipeline stage nodes contain a dependency cycle")
    return tuple(ordered)
