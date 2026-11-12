from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from tgcurator.application.admin import (
    SESSION_COOKIE_NAME,
    SUPPORTED_CONFIG_FAMILIES,
    AdminApplicationService,
)
from tgcurator.application.ports.admin_api import AuthenticatedAdmin

from .dependencies import (
    get_admin_service,
    require_authenticated_admin,
    require_csrf_admin,
)

metrics_router = APIRouter(tags=["observability"])
api_router = APIRouter(prefix="/api/v1")

AdminServiceDep = Annotated[AdminApplicationService, Depends(get_admin_service)]
AuthenticatedAdminDep = Annotated[AuthenticatedAdmin, Depends(require_authenticated_admin)]
CsrfAdminDep = Annotated[AuthenticatedAdmin, Depends(require_csrf_admin)]
JsonObjectBody = Annotated[dict[str, Any], Body()]
OptionalJsonObjectBody = Annotated[dict[str, Any], Body(default_factory=dict)]


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateSecretRequest(BaseModel):
    secret_type: str
    plaintext: str


class RotateSecretRequest(BaseModel):
    plaintext: str


class CreateIdentityRequest(BaseModel):
    name: str
    identity_type: str
    secret_id: str
    enabled: bool = True


class EvaluateCapabilitiesRequest(BaseModel):
    source_protected_content: bool = False
    has_source_access: bool = True
    has_destination_post_permission: bool = True
    capability_overrides: dict[str, bool] | None = None


class PublishVersionRequest(BaseModel):
    supersede_published: bool = True


class ReviewStatusRequest(BaseModel):
    new_status: str
    reason: str | None = None


class ReanalyzeRequest(BaseModel):
    pipeline_version_id: str | None = None
    reason: str | None = None


class RerouteRequest(BaseModel):
    routing_policy_version_id: str | None = None
    dry_run: bool = True
    reason: str | None = None


class SkipMessageRequest(BaseModel):
    reason: str


class RetryStepRequest(BaseModel):
    step_kind: str
    target_id: str
    reason: str | None = None


class ConfirmCleanupJobRequest(BaseModel):
    dry_run_id: str
    expected_filter_hash: str = Field(..., min_length=1)


OptionalEvaluateCapabilitiesBody = Annotated[
    EvaluateCapabilitiesRequest,
    Body(default_factory=EvaluateCapabilitiesRequest),
]
OptionalPublishVersionBody = Annotated[
    PublishVersionRequest,
    Body(default_factory=PublishVersionRequest),
]
OptionalReanalyzeBody = Annotated[
    ReanalyzeRequest,
    Body(default_factory=ReanalyzeRequest),
]
OptionalRerouteBody = Annotated[
    RerouteRequest,
    Body(default_factory=RerouteRequest),
]


# --- Prometheus Metrics ---


@metrics_router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(
    service: AdminServiceDep,
) -> PlainTextResponse:
    body = await service.render_metrics()
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# --- Single-Administrator Session Authentication ---


@api_router.post("/auth/login", tags=["auth"])
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AdminServiceDep,
) -> dict[str, Any]:
    client_host = request.client.host if request.client is not None else None
    raw_token, admin = await service.sessions.login(
        username=payload.username,
        password=payload.password,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_host,
    )
    is_prod = request.app.state.settings.environment == "production"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        path="/",
    )
    return _format_session_response(admin)


@api_router.post("/auth/logout", tags=["auth"])
async def logout(
    request: Request,
    response: Response,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, str]:
    await service.sessions.logout(admin=admin)
    is_prod = request.app.state.settings.environment == "production"
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=is_prod,
        samesite="lax",
    )
    return {"status": "logged_out"}


@api_router.get("/auth/me", tags=["auth"])
async def current_admin(
    admin: AuthenticatedAdminDep,
) -> dict[str, Any]:
    return _format_session_response(admin)


# --- Dashboard, Operations, Retry & Audit Log ---


@api_router.get("/dashboard/summary", tags=["dashboard"])
async def dashboard_summary(
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.dashboard_summary()


@api_router.get("/operations/status", tags=["operations"])
async def operations_status(
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.operations_status()


@api_router.get("/operations/failed-items", tags=["operations"])
async def list_failed_items(
    *,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
    step_kind: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    items = await service.list_failed_items(step_kind=step_kind, limit=limit)
    return {"items": items}


@api_router.post("/operations/retry", tags=["operations"])
async def retry_failed_step(
    payload: RetryStepRequest,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.retry_step(
        admin=admin,
        step_kind=payload.step_kind,
        target_id=payload.target_id,
        reason=payload.reason,
    )


@api_router.get("/audit-events", tags=["audit"])
async def list_audit_events(
    *,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
    entity_type: str | None = None,
    entity_id: str | None = None,
    event_type: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await service.list_audit_events(
        filters={
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
        },
        cursor=cursor,
        limit=limit,
    )


# --- Write-Only Encrypted Secrets ---


@api_router.get("/secrets", tags=["secrets"])
async def list_secrets(
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return {"items": await service.list_secrets()}


@api_router.post("/secrets", status_code=201, tags=["secrets"])
async def create_secret(
    payload: CreateSecretRequest,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.create_secret(
        admin=admin,
        secret_type=payload.secret_type,
        plaintext=payload.plaintext,
    )


@api_router.get("/secrets/{secret_id}", tags=["secrets"])
async def get_secret(
    secret_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_secret(secret_id=secret_id)


@api_router.put("/secrets/{secret_id}", tags=["secrets"])
async def rotate_secret(
    secret_id: str,
    payload: RotateSecretRequest,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.rotate_secret(
        admin=admin,
        secret_id=secret_id,
        plaintext=payload.plaintext,
    )


# --- Telegram Identities & Capability Resolver ---


@api_router.get("/telegram-identities", tags=["telegram-identities"])
async def list_identities(
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return {"items": await service.list_identities()}


@api_router.post("/telegram-identities", status_code=201, tags=["telegram-identities"])
async def create_identity(
    payload: CreateIdentityRequest,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.create_identity(
        admin=admin,
        name=payload.name,
        identity_type=payload.identity_type,
        secret_id=payload.secret_id,
        enabled=payload.enabled,
    )


@api_router.get("/telegram-identities/{identity_id}", tags=["telegram-identities"])
async def get_identity(
    identity_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_identity(identity_id=identity_id)


@api_router.patch("/telegram-identities/{identity_id}", tags=["telegram-identities"])
async def update_identity(
    identity_id: str,
    changes: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.update_identity(
        admin=admin,
        identity_id=identity_id,
        changes=changes,
    )


@api_router.post(
    "/telegram-identities/{identity_id}/evaluate-capabilities",
    tags=["telegram-identities"],
)
async def evaluate_identity_capabilities(
    identity_id: str,
    payload: OptionalEvaluateCapabilitiesBody,
    _admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.evaluate_identity_capabilities(
        identity_id=identity_id,
        source_protected_content=payload.source_protected_content,
        has_source_access=payload.has_source_access,
        has_destination_post_permission=payload.has_destination_post_permission,
        capability_overrides=payload.capability_overrides,
    )


# --- Source & Destination Channels ---


@api_router.get("/source-channels", tags=["channels"])
async def list_source_channels(
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return {"items": await service.list_source_channels()}


@api_router.post("/source-channels", status_code=201, tags=["channels"])
async def create_source_channel(
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.create_source_channel(admin=admin, payload=payload)


@api_router.get("/source-channels/{channel_id}", tags=["channels"])
async def get_source_channel(
    channel_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_source_channel(channel_id=channel_id)


@api_router.patch("/source-channels/{channel_id}", tags=["channels"])
async def update_source_channel(
    channel_id: str,
    changes: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.update_source_channel(
        admin=admin,
        channel_id=channel_id,
        changes=changes,
    )


@api_router.get("/destination-channels", tags=["channels"])
async def list_destination_channels(
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return {"items": await service.list_destination_channels()}


@api_router.post("/destination-channels", status_code=201, tags=["channels"])
async def create_destination_channel(
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.create_destination_channel(admin=admin, payload=payload)


@api_router.get("/destination-channels/{channel_id}", tags=["channels"])
async def get_destination_channel(
    channel_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_destination_channel(channel_id=channel_id)


@api_router.patch("/destination-channels/{channel_id}", tags=["channels"])
async def update_destination_channel(
    channel_id: str,
    changes: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.update_destination_channel(
        admin=admin,
        channel_id=channel_id,
        changes=changes,
    )


# --- Processing Ranges ---


@api_router.get("/processing-ranges", tags=["processing-ranges"])
async def list_processing_ranges(
    *,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
    source_channel_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    items = await service.list_processing_ranges(
        source_channel_id=source_channel_id,
        status=status,
    )
    return {"items": items}


@api_router.post("/processing-ranges", status_code=201, tags=["processing-ranges"])
async def create_processing_range(
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.create_processing_range(admin=admin, payload=payload)


@api_router.get("/processing-ranges/{range_id}", tags=["processing-ranges"])
async def get_processing_range(
    range_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_processing_range(range_id=range_id)


@api_router.patch("/processing-ranges/{range_id}", tags=["processing-ranges"])
async def update_processing_range(
    range_id: str,
    changes: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.update_processing_range(
        admin=admin,
        range_id=range_id,
        changes=changes,
    )


@api_router.post("/processing-ranges/{range_id}/pause", tags=["processing-ranges"])
async def pause_processing_range(
    range_id: str,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.update_processing_range(
        admin=admin,
        range_id=range_id,
        changes={"status": "paused"},
    )


@api_router.post("/processing-ranges/{range_id}/resume", tags=["processing-ranges"])
async def resume_processing_range(
    range_id: str,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.update_processing_range(
        admin=admin,
        range_id=range_id,
        changes={"status": "active"},
    )


@api_router.get("/processing-ranges/{range_id}/executions", tags=["processing-ranges"])
async def list_range_executions(
    range_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return {"items": await service.list_range_executions(range_id=range_id)}


# --- Versioned Configurations (DRAFT -> PUBLISHED -> ARCHIVED) ---


@api_router.get("/config/families", tags=["config"])
async def list_config_families(
    _admin: AuthenticatedAdminDep,
) -> dict[str, list[str]]:
    return {"families": sorted(SUPPORTED_CONFIG_FAMILIES)}


@api_router.get("/config/{family}", tags=["config"])
async def list_config_entities(
    family: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return {"items": await service.list_config_entities(family=family)}


@api_router.post("/config/{family}", status_code=201, tags=["config"])
async def create_config_entity(
    family: str,
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.create_config_entity(admin=admin, family=family, payload=payload)


@api_router.get("/config/{family}/{entity_id}", tags=["config"])
async def get_config_entity(
    family: str,
    entity_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_config_entity(family=family, entity_id=entity_id)


@api_router.post("/config/{family}/{entity_id}/versions", status_code=201, tags=["config"])
async def create_config_version(
    family: str,
    entity_id: str,
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    content = _unwrap_version_content(payload)
    return await service.create_config_version(
        admin=admin,
        family=family,
        entity_id=entity_id,
        content=content,
    )


@api_router.patch(
    "/config/{family}/{entity_id}/versions/{version_id}",
    tags=["config"],
)
async def update_config_version(
    family: str,
    entity_id: str,
    version_id: str,
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    content = _unwrap_version_content(payload)
    return await service.update_config_version(
        admin=admin,
        family=family,
        entity_id=entity_id,
        version_id=version_id,
        content=content,
    )


@api_router.post(
    "/config/{family}/{entity_id}/versions/{version_id}/publish",
    tags=["config"],
)
async def publish_config_version(
    family: str,
    entity_id: str,
    version_id: str,
    payload: OptionalPublishVersionBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.publish_config_version(
        admin=admin,
        family=family,
        entity_id=entity_id,
        version_id=version_id,
        supersede_published=payload.supersede_published,
    )


@api_router.post(
    "/config/{family}/{entity_id}/versions/{version_id}/archive",
    tags=["config"],
)
async def archive_config_version(
    family: str,
    entity_id: str,
    version_id: str,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.archive_config_version(
        admin=admin,
        family=family,
        entity_id=entity_id,
        version_id=version_id,
    )


# --- Isolated Prompt Test Bench ---


@api_router.post("/analysis/test-bench", tags=["analysis"])
async def run_prompt_test_bench(
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.run_prompt_test_bench(admin=admin, payload=payload)


# --- Messages Browser, Causal Detail, Review, Reanalyze, Reroute & Skip ---


@api_router.get("/messages", tags=["messages"])
async def list_messages(
    *,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
    source_channel_id: str | None = None,
    published_from: str | None = None,
    published_before: str | None = None,
    processing_status: str | None = None,
    review_status: str | None = None,
    duplicate_status: str | None = None,
    blocked_from_analysis: bool | None = None,
    negative_label: bool | None = None,
    label_key: str | None = None,
    label_namespace: str | None = None,
    label_scope: str | None = None,
    pipeline_version_id: str | None = None,
    routing_policy_version_id: str | None = None,
    destination_channel_id: str | None = None,
    archive_state: str | None = None,
    query: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await service.list_messages(
        filters={
            "source_channel_id": source_channel_id,
            "published_from": published_from,
            "published_before": published_before,
            "processing_status": processing_status,
            "review_status": review_status,
            "duplicate_status": duplicate_status,
            "blocked_from_analysis": blocked_from_analysis,
            "negative_label": negative_label,
            "label_key": label_key,
            "label_namespace": label_namespace,
            "label_scope": label_scope,
            "pipeline_version_id": pipeline_version_id,
            "routing_policy_version_id": routing_policy_version_id,
            "destination_channel_id": destination_channel_id,
            "archive_state": archive_state,
            "query": query,
        },
        cursor=cursor,
        limit=limit,
    )


@api_router.get("/messages/{message_id}", tags=["messages"])
async def get_message_detail(
    message_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_message_detail(message_id=message_id)


@api_router.post("/messages/{message_id}/labels", tags=["review"])
async def apply_manual_label(
    message_id: str,
    payload: JsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.apply_manual_label(
        admin=admin,
        message_id=message_id,
        payload=payload,
    )


@api_router.post("/messages/{message_id}/review-status", tags=["review"])
async def transition_review_status(
    message_id: str,
    payload: ReviewStatusRequest,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.transition_review_status(
        admin=admin,
        message_id=message_id,
        new_status=payload.new_status,
        reason=payload.reason,
    )


@api_router.post("/messages/{message_id}/reanalyze", tags=["review"])
async def reanalyze_message(
    message_id: str,
    payload: OptionalReanalyzeBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.reanalyze_message(
        admin=admin,
        message_id=message_id,
        pipeline_version_id=payload.pipeline_version_id,
        reason=payload.reason,
    )


@api_router.post("/messages/{message_id}/reroute", tags=["review"])
async def reroute_message(
    message_id: str,
    payload: OptionalRerouteBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.reroute_message(
        admin=admin,
        message_id=message_id,
        routing_policy_version_id=payload.routing_policy_version_id,
        dry_run=payload.dry_run,
        reason=payload.reason,
    )


@api_router.post("/messages/{message_id}/skip", tags=["review"])
async def skip_message(
    message_id: str,
    payload: SkipMessageRequest,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.skip_message(
        admin=admin,
        message_id=message_id,
        reason=payload.reason,
    )


# --- Two-Step Archive Cleanup (Dry-Run -> Confirm) ---


@api_router.get("/archive-cleanup/dry-runs", tags=["archive-cleanup"])
async def list_cleanup_dry_runs(
    *,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
    limit: int = 50,
) -> dict[str, Any]:
    return {"items": await service.list_cleanup_dry_runs(limit=limit)}


@api_router.post("/archive-cleanup/dry-runs", status_code=201, tags=["archive-cleanup"])
async def create_cleanup_dry_run(
    payload: OptionalJsonObjectBody,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.create_cleanup_dry_run(admin=admin, filter_payload=payload)


@api_router.get("/archive-cleanup/dry-runs/{dry_run_id}", tags=["archive-cleanup"])
async def get_cleanup_dry_run(
    dry_run_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_cleanup_dry_run(dry_run_id=dry_run_id)


@api_router.get("/archive-cleanup/jobs", tags=["archive-cleanup"])
async def list_cleanup_jobs(
    *,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await service.list_cleanup_jobs(cursor=cursor, limit=limit)


@api_router.post("/archive-cleanup/jobs", status_code=201, tags=["archive-cleanup"])
async def confirm_cleanup_job(
    payload: ConfirmCleanupJobRequest,
    admin: CsrfAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.confirm_cleanup_job(
        admin=admin,
        dry_run_id=payload.dry_run_id,
        expected_filter_hash=payload.expected_filter_hash,
    )


@api_router.get("/archive-cleanup/jobs/{job_id}", tags=["archive-cleanup"])
async def get_cleanup_job(
    job_id: str,
    _admin: AuthenticatedAdminDep,
    service: AdminServiceDep,
) -> dict[str, Any]:
    return await service.get_cleanup_job(job_id=job_id)


def _format_session_response(admin: AuthenticatedAdmin) -> dict[str, Any]:
    return {
        "user": {
            "id": admin.user_id,
            "username": admin.username,
        },
        "session_id": admin.session_id,
        "csrf_token": admin.csrf_token,
        "expires_at": admin.expires_at.isoformat(),
    }


def _unwrap_version_content(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("content")
    if isinstance(nested, dict) and set(payload.keys()) <= {"content"}:
        return nested
    return payload


__all__ = ["api_router", "metrics_router"]
