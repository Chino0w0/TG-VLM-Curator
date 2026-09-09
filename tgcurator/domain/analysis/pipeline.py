from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import VersionStatus, require_json_object, validate_non_blank, validate_positive_int
from .conditions import validate_run_if
from .stages import AnalysisStageTemplateVersion

_ALLOWED_STAGE_OVERRIDES = frozenset(
    {
        "timeout_seconds",
        "max_batch_size",
        "max_concurrency",
        "retry_policy",
        "visual_composition_policy",
    }
)


@dataclass(frozen=True, slots=True)
class PipelineStage:
    stage_id: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.stage_id, str) or not self.stage_id.strip():
            raise DomainValidationError("stage_id must not be blank")
        if not isinstance(self.depends_on, tuple) or any(
            not isinstance(dependency, str) or not dependency.strip()
            for dependency in self.depends_on
        ):
            raise DomainValidationError("depends_on must be a tuple of non-blank stage IDs")
        if self.stage_id in self.depends_on:
            raise DomainValidationError(f"stage {self.stage_id!r} cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise DomainValidationError(f"stage {self.stage_id!r} has duplicate dependencies")


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    pipeline_version_id: str
    stages: tuple[PipelineStage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.pipeline_version_id, str) or not self.pipeline_version_id.strip():
            raise DomainValidationError("pipeline_version_id must not be blank")
        if not isinstance(self.stages, tuple) or any(
            not isinstance(stage, PipelineStage) for stage in self.stages
        ):
            raise DomainValidationError("stages must be a tuple of PipelineStage values")
        _validate_graph(self.stages)

    def topological_stage_ids(self) -> tuple[str, ...]:
        return _topological_ids(self.stages)


@dataclass(frozen=True, slots=True)
class PipelineStageNode:
    node_id: str
    stage: AnalysisStageTemplateVersion
    depends_on: tuple[str, ...] = ()
    run_if: Mapping[str, Any] | None = None
    parameter_overrides: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", validate_non_blank(self.node_id, field="node_id"))
        if not isinstance(self.stage, AnalysisStageTemplateVersion):
            raise DomainValidationError(
                "pipeline node stage must be an AnalysisStageTemplateVersion"
            )
        if not isinstance(self.depends_on, tuple) or any(
            not isinstance(dependency, str) or not dependency.strip()
            for dependency in self.depends_on
        ):
            raise DomainValidationError("pipeline node dependencies must be non-blank strings")
        if self.node_id in self.depends_on:
            raise DomainValidationError("pipeline node cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise DomainValidationError("pipeline node dependencies cannot contain duplicates")
        validate_run_if(self.run_if)
        if self.run_if is not None:
            object.__setattr__(self, "run_if", require_json_object(self.run_if, field="run_if"))
        overrides = require_json_object(
            self.parameter_overrides or {}, field="stage parameter overrides"
        )
        unsupported = set(overrides) - _ALLOWED_STAGE_OVERRIDES
        if unsupported:
            raise DomainValidationError(
                f"pipeline node contains unsupported overrides: {sorted(unsupported)!r}"
            )
        object.__setattr__(self, "parameter_overrides", overrides)


@dataclass(frozen=True, slots=True)
class AnalysisPipelineVersion:
    version_id: str
    analysis_pipeline_id: str
    version_number: int
    nodes: tuple[PipelineStageNode, ...]
    status: VersionStatus = VersionStatus.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_id", validate_non_blank(self.version_id, field="pipeline version_id")
        )
        object.__setattr__(
            self,
            "analysis_pipeline_id",
            validate_non_blank(self.analysis_pipeline_id, field="analysis pipeline id"),
        )
        validate_positive_int(self.version_number, field="pipeline version number")
        if not isinstance(self.nodes, tuple) or any(
            not isinstance(node, PipelineStageNode) for node in self.nodes
        ):
            raise DomainValidationError(
                "pipeline nodes must be a tuple of PipelineStageNode values"
            )
        if not self.nodes:
            raise DomainValidationError("analysis pipeline must contain at least one node")
        if not isinstance(self.status, VersionStatus):
            raise DomainValidationError("pipeline status must be a VersionStatus")
        _validate_graph(self.nodes)
        self._validate_conditions()
        if self.status is VersionStatus.PUBLISHED:
            self.validate_for_publication()

    def topological_node_ids(self) -> tuple[str, ...]:
        return _topological_ids(self.nodes)

    def validate_for_publication(self) -> None:
        for node in self.nodes:
            if node.stage.status is not VersionStatus.PUBLISHED:
                raise DomainValidationError("published pipeline must reference published stages")
            node.stage.validate_for_publication()
        self._validate_conditions()

    def _validate_conditions(self) -> None:
        nodes_by_id = {node.node_id: node for node in self.nodes}

        def ancestor_ids(node: PipelineStageNode) -> set[str]:
            pending = list(node.depends_on)
            ancestors: set[str] = set()
            while pending:
                dependency_id = pending.pop()
                if dependency_id in ancestors:
                    continue
                ancestors.add(dependency_id)
                pending.extend(nodes_by_id[dependency_id].depends_on)
            return ancestors

        for node in self.nodes:
            allowed_facts = {"message.blocked_from_analysis"}
            for ancestor_id in ancestor_ids(node):
                ancestor = nodes_by_id[ancestor_id]
                prefix = "global" if ancestor.stage.target_scope.value == "global" else "media"
                allowed_facts.update(
                    f"{prefix}.{key}" for key in ancestor.stage.label_set.label_keys
                )
            validate_run_if(node.run_if, allowed_fact_keys=allowed_facts)


def _validate_graph(stages: tuple[Any, ...]) -> None:
    stage_ids = {_node_id(stage) for stage in stages}
    if len(stage_ids) != len(stages):
        raise DomainValidationError("a pipeline cannot contain duplicate node IDs")
    for stage in stages:
        missing = set(stage.depends_on) - stage_ids
        if missing:
            raise DomainValidationError(
                f"stage {_node_id(stage)!r} depends on undefined stages: {sorted(missing)!r}"
            )
    _topological_ids(stages)


def _topological_ids(stages: tuple[Any, ...]) -> tuple[str, ...]:
    dependencies = {_node_id(stage): set(stage.depends_on) for stage in stages}
    ordered: list[str] = []
    ready = sorted(stage_id for stage_id, deps in dependencies.items() if not deps)
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for stage_id in sorted(dependencies):
            if current in dependencies[stage_id]:
                dependencies[stage_id].remove(current)
                if not dependencies[stage_id] and stage_id not in ordered and stage_id not in ready:
                    ready.append(stage_id)
        ready.sort()
    if len(ordered) != len(stages):
        cycle_members = sorted(stage_id for stage_id, deps in dependencies.items() if deps)
        raise DomainValidationError(f"pipeline contains a cycle: {cycle_members!r}")
    return tuple(ordered)


def _node_id(stage: Any) -> str:
    return stage.stage_id if isinstance(stage, PipelineStage) else stage.node_id
