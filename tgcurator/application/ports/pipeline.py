from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from tgcurator.domain.analysis import (
    ExistingNodeTargetRun,
    MessageMediaReadiness,
    PipelineNodeSpec,
    PlannedNodeTarget,
)
from tgcurator.domain.messages import DuplicateMatchResult, VisualCandidate
from tgcurator.domain.processing import SourceChannelProfileConfig, WindowMessageProgress


@dataclass(frozen=True, slots=True)
class ActiveRangeExecutionSnapshot:
    execution_id: str
    processing_range_id: str
    source_channel_id: str
    source_profile_version_id: str
    from_message_id_exclusive: int
    to_message_id_inclusive: int
    watermark_message_id: int
    status: str
    profile: SourceChannelProfileConfig


@dataclass(frozen=True, slots=True)
class WindowMessageSnapshot:
    message_id: str
    source_channel_id: str
    primary_telegram_message_id: int
    published_at: datetime
    processing_status: str
    terminal_reason: str | None
    failure_stage: str | None
    last_error_code: str | None
    blocked_from_analysis: bool
    duplicate_status: str
    canonical_message_id: str | None
    duplicate_distance: int | None
    visual_fingerprint: str | None
    image_phashes: tuple[str | None, ...]
    video_cover_phashes: tuple[str | None, ...]
    media: MessageMediaReadiness
    formal_analysis_run_id: str | None
    formal_analysis_run_status: str | None
    formal_analysis_error_code: str | None
    formal_routing_evaluation_id: str | None
    publication_intent_statuses: tuple[str, ...]
    publication_failed_error_code: str | None


@dataclass(frozen=True, slots=True)
class AnalysisRunDagSnapshot:
    analysis_run_id: str
    message_id: str
    pipeline_version_id: str
    status: str
    blocked_from_analysis: bool
    nodes: tuple[PipelineNodeSpec, ...]
    existing_runs_by_node: Mapping[str, tuple[ExistingNodeTargetRun, ...]]
    facts: Mapping[str, Any]
    media: MessageMediaReadiness


@dataclass(frozen=True, slots=True)
class PipelineProgressionReport:
    executions_checked: int = 0
    executions_completed: int = 0
    watermarks_advanced: int = 0
    messages_terminalized: int = 0
    stage_runs_scheduled: int = 0
    stage_runs_cached: int = 0
    media_wakeups_created: int = 0
    duplicates_detected: int = 0
    routings_evaluated: int = 0

    def merged(self, other: PipelineProgressionReport) -> PipelineProgressionReport:
        return PipelineProgressionReport(
            executions_checked=self.executions_checked + other.executions_checked,
            executions_completed=self.executions_completed + other.executions_completed,
            watermarks_advanced=self.watermarks_advanced + other.watermarks_advanced,
            messages_terminalized=self.messages_terminalized + other.messages_terminalized,
            stage_runs_scheduled=self.stage_runs_scheduled + other.stage_runs_scheduled,
            stage_runs_cached=self.stage_runs_cached + other.stage_runs_cached,
            media_wakeups_created=self.media_wakeups_created + other.media_wakeups_created,
            duplicates_detected=self.duplicates_detected + other.duplicates_detected,
            routings_evaluated=self.routings_evaluated + other.routings_evaluated,
        )


@dataclass(frozen=True, slots=True)
class WakeupRepairReport:
    repaired_range_executions: int = 0
    repaired_image_archives: int = 0
    repaired_video_archives: int = 0
    repaired_analysis_stages: int = 0
    repaired_publications: int = 0
    repaired_cleanups: int = 0

    @property
    def total_repaired(self) -> int:
        return (
            self.repaired_range_executions
            + self.repaired_image_archives
            + self.repaired_video_archives
            + self.repaired_analysis_stages
            + self.repaired_publications
            + self.repaired_cleanups
        )


class MessagePipelineRepository(Protocol):
    async def handoff_execution(
        self, *, execution_id: str, lease_token: str, now: datetime
    ) -> bool: ...

    async def list_awaiting_execution_ids(self, *, limit: int) -> tuple[str, ...]: ...

    async def find_awaiting_execution_ids_for_message(
        self, *, message_id: str
    ) -> tuple[str, ...]: ...

    async def find_message_id_for_image_asset(self, *, image_asset_id: str) -> str | None: ...

    async def find_message_id_for_video_asset(self, *, video_asset_id: str) -> str | None: ...

    async def find_message_ids_for_stage_run(self, *, stage_run_id: str) -> tuple[str, ...]: ...

    async def find_message_id_for_publication_intent(self, *, intent_id: str) -> str | None: ...

    async def load_execution(self, *, execution_id: str) -> ActiveRangeExecutionSnapshot | None: ...

    async def load_window_messages(
        self,
        *,
        execution: ActiveRangeExecutionSnapshot,
    ) -> tuple[WindowMessageSnapshot, ...]: ...

    async def load_pipeline_nodes(
        self, *, pipeline_version_id: str
    ) -> tuple[PipelineNodeSpec, ...]: ...

    async def ensure_formal_analysis_run(
        self,
        *,
        message_id: str,
        pipeline_version_id: str,
        now: datetime,
    ) -> str: ...

    async def load_analysis_dag_snapshot(
        self,
        *,
        analysis_run_id: str,
    ) -> AnalysisRunDagSnapshot | None: ...

    async def materialize_planned_targets(
        self,
        *,
        analysis_run_id: str,
        message_id: str,
        canonical_message_id: str | None,
        duplicate_status: str,
        planned_targets: tuple[PlannedNodeTarget, ...],
        run_status: str,
        now: datetime,
    ) -> tuple[int, int, bool]:
        """Persist planned StageRuns (and cached canonical visual stage reuses).

        Returns `(scheduled_count, cached_count, negative_gate_triggered)`.
        """

    async def set_analysis_run_status(
        self,
        *,
        analysis_run_id: str,
        status: str,
        now: datetime,
    ) -> None: ...

    async def ensure_media_archive_wakeups(
        self,
        *,
        message_id: str,
        now: datetime,
    ) -> int: ...

    async def cancel_pending_media_wakeups(
        self,
        *,
        message_id: str,
        now: datetime,
    ) -> int: ...

    async def load_visual_duplicate_candidates(
        self,
        *,
        message_id: str,
        published_at: datetime,
        visual_fingerprint: str | None,
        limit: int = 500,
    ) -> tuple[VisualCandidate, ...]: ...

    async def record_visual_deduplication(
        self,
        *,
        message_id: str,
        visual_fingerprint: str | None,
        match: DuplicateMatchResult,
        now: datetime,
    ) -> None: ...

    async def mark_message_processing(self, *, message_id: str, now: datetime) -> None: ...

    async def mark_message_processed(
        self,
        *,
        message_id: str,
        terminal_reason: str,
        now: datetime,
    ) -> None: ...

    async def mark_message_failed(
        self,
        *,
        message_id: str,
        failure_stage: str,
        error_code: str,
        now: datetime,
    ) -> None: ...

    async def persist_watermark_advancement(
        self,
        *,
        execution_id: str,
        next_watermark_message_id: int,
        window_completed: bool,
        progress_snapshot: tuple[WindowMessageProgress, ...],
        now: datetime,
    ) -> tuple[bool, bool]:
        """Return `(watermark_advanced, execution_completed)`."""

    async def repair_wakeups(
        self,
        *,
        now: datetime,
        limit: int = 500,
    ) -> WakeupRepairReport: ...
