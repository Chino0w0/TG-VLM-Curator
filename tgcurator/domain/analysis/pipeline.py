from __future__ import annotations

from dataclasses import dataclass

from tgcurator.shared import DomainValidationError


@dataclass(frozen=True, slots=True)
class PipelineStage:
    stage_id: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.stage_id.strip():
            raise DomainValidationError("stage_id must not be blank")
        if self.stage_id in self.depends_on:
            raise DomainValidationError(f"stage {self.stage_id!r} cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise DomainValidationError(f"stage {self.stage_id!r} has duplicate dependencies")


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    pipeline_version_id: str
    stages: tuple[PipelineStage, ...]

    def __post_init__(self) -> None:
        if not self.pipeline_version_id.strip():
            raise DomainValidationError("pipeline_version_id must not be blank")
        stage_ids = {stage.stage_id for stage in self.stages}
        if len(stage_ids) != len(self.stages):
            raise DomainValidationError("a pipeline cannot contain duplicate stage_id values")
        for stage in self.stages:
            missing = set(stage.depends_on) - stage_ids
            if missing:
                raise DomainValidationError(
                    f"stage {stage.stage_id!r} depends on undefined stages: {sorted(missing)!r}"
                )
        self.topological_stage_ids()

    def topological_stage_ids(self) -> tuple[str, ...]:
        """Return a stable valid execution order, or reject cycles before publication."""
        dependencies = {stage.stage_id: set(stage.depends_on) for stage in self.stages}
        ordered: list[str] = []
        ready = sorted(stage_id for stage_id, deps in dependencies.items() if not deps)
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for stage_id in sorted(dependencies):
                if current in dependencies[stage_id]:
                    dependencies[stage_id].remove(current)
                    if (
                        not dependencies[stage_id]
                        and stage_id not in ordered
                        and stage_id not in ready
                    ):
                        ready.append(stage_id)
            ready.sort()
        if len(ordered) != len(self.stages):
            cycle_members = sorted(stage_id for stage_id, deps in dependencies.items() if deps)
            raise DomainValidationError(f"pipeline contains a cycle: {cycle_members!r}")
        return tuple(ordered)
