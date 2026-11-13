from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from tgcurator.application.ports.operations import OperationsSnapshot


@dataclass(frozen=True, slots=True)
class AdminAccountRecord:
    user_id: str
    username: str
    password_hash: str = field(repr=False)
    is_active: bool


@dataclass(frozen=True, slots=True)
class AdminSessionState:
    session_id: str
    admin_user_id: str
    username: str
    session_token_hash: str = field(repr=False)
    csrf_token_hash: str = field(repr=False)
    expires_at: datetime
    last_seen_at: datetime
    created_at: datetime
    revoked_at: datetime | None = None
    user_agent: str | None = None
    ip_address: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedAdmin:
    session_id: str
    user_id: str
    username: str
    csrf_token: str = field(repr=False)
    csrf_token_hash: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CursorPage[T]:
    items: tuple[T, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class MetricsCollectionSnapshot:
    operations: OperationsSnapshot
    messages_by_status: Mapping[str, int] = field(default_factory=dict)
    messages_blocked_total: int = 0
    ranges_by_status: Mapping[str, int] = field(default_factory=dict)
    range_lags: Mapping[str, int] = field(default_factory=dict)
    media_assets_by_kind_state: Mapping[tuple[str, str], int] = field(default_factory=dict)
    archive_bytes_total: int = 0
    stage_runs_by_status_origin: Mapping[tuple[str, str], int] = field(default_factory=dict)
    analysis_cache_hits_total: int = 0
    inference_calls_by_status_error: Mapping[tuple[str, str], int] = field(default_factory=dict)
    inference_latency_ms_sum: float = 0.0
    inference_latency_ms_count: int = 0
    inference_tokens_by_kind: Mapping[str, int] = field(default_factory=dict)
    routing_evaluations_total: int = 0
    routing_matched_actions_total: int = 0
    publication_intents_by_status: Mapping[str, int] = field(default_factory=dict)
    worker_backlog_by_queue: Mapping[str, int] = field(default_factory=dict)
    api_database_ready: bool = True


class AdminSessionRepository(Protocol):
    """Persistence port for single-administrator HTTP sessions and audit events."""

    async def find_active_admin_by_username(
        self, *, username: str
    ) -> AdminAccountRecord | None: ...

    async def create_session(
        self,
        *,
        session_id: str,
        admin_user_id: str,
        session_token_hash: str,
        csrf_token_hash: str,
        expires_at: datetime,
        now: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AdminSessionState: ...

    async def get_session_by_token_hash(
        self,
        *,
        session_token_hash: str,
    ) -> AdminSessionState | None: ...

    async def touch_session(self, *, session_id: str, now: datetime) -> None: ...

    async def revoke_session(self, *, session_id: str, now: datetime) -> None: ...

    async def record_audit(
        self,
        *,
        actor_admin_user_id: str | None,
        event_type: str,
        entity_type: str,
        entity_id: str | None,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> str: ...
