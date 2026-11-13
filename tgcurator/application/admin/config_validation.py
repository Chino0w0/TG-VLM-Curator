from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from tgcurator.domain.analysis import (
    AnalysisPipelineVersion,
    AnalysisStageTemplateVersion,
    CachePolicy,
    ExecutionMode,
    InferenceProfileVersion,
    LabelBinding,
    LabelDefinitionVersion,
    LabelScope,
    LabelSetVersion,
    PipelineStageNode,
    PromptTemplateVersion,
    StageInputPolicy,
    StageRetryPolicy,
    StructuredOutputMode,
    StructuredOutputPolicy,
    VersionStatus,
    VisualCompositionPolicy,
)
from tgcurator.domain.processing import SourceChannelProfileConfig
from tgcurator.domain.publishing import PublicationMode
from tgcurator.domain.routing import validate_condition
from tgcurator.shared import DomainValidationError

from .errors import AdminValidationError

SUPPORTED_CONFIG_FAMILIES: frozenset[str] = frozenset(
    {
        "media-processing-profiles",
        "source-channel-profiles",
        "label-definitions",
        "label-sets",
        "prompt-templates",
        "inference-profiles",
        "analysis-stages",
        "analysis-pipelines",
        "rendering-templates",
        "routing-policies",
    }
)

INFERENCE_SECRET_TYPES: frozenset[str] = frozenset({"api_key", "inference_api_key"})
MTPROTO_SECRET_TYPES: frozenset[str] = frozenset(
    {"telegram_session", "telethon_session", "mtproto_session"}
)
BOT_SECRET_TYPES: frozenset[str] = frozenset({"bot_token"})

_INTERPOLATION_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}")
_FORBIDDEN_TEMPLATE_MARKERS = ("{%", "%}", "{#", "#}")
_SENSITIVE_QUERY_KEYS = (
    "secret",
    "password",
    "apikey",
    "authorization",
    "token",
    "credential",
    "signature",
    "key",
)
_CLOUD_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "metadata.azure.internal",
        "metadata.tencentyun.com",
    }
)
_LOCAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)
_AWS_IMDS_IPV6 = ipaddress.ip_network("fd00:ec2::/32")
_ALIBABA_METADATA_IP = ipaddress.ip_address("100.100.100.200")
_ORACLE_METADATA_IP = ipaddress.ip_address("192.0.0.192")


def env_allow_local_inference(*, default: bool = False) -> bool:
    raw = os.environ.get("TGCURATOR_ALLOW_LOCAL_INFERENCE")
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def validate_secret_type_for_identity(*, identity_type: str, secret_type: str) -> None:
    normalized_identity = identity_type.strip() if isinstance(identity_type, str) else ""
    normalized_secret = secret_type.strip() if isinstance(secret_type, str) else ""
    if normalized_identity == "mtproto_user":
        if normalized_secret not in MTPROTO_SECRET_TYPES:
            raise AdminValidationError("mtproto_user identity requires a telegram_session secret")
    elif normalized_identity == "bot":
        if normalized_secret not in BOT_SECRET_TYPES:
            raise AdminValidationError("bot identity requires a bot_token secret")
    else:
        raise AdminValidationError("identity_type must be mtproto_user or bot")


def validate_secret_type_for_inference(*, secret_type: str) -> None:
    normalized_secret = secret_type.strip() if isinstance(secret_type, str) else ""
    if normalized_secret not in INFERENCE_SECRET_TYPES:
        raise AdminValidationError("inference profile requires an api_key secret")


def validate_inference_base_url(base_url: str, *, allow_local: bool | None = None) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise AdminValidationError("inference base_url must not be blank")
    normalized = base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise AdminValidationError("inference base_url must be a valid HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise AdminValidationError("inference base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise AdminValidationError("inference base_url must not embed credentials")
    if parsed.fragment:
        raise AdminValidationError("inference base_url must not include a URL fragment")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        cleaned = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(fragment in cleaned for fragment in _SENSITIVE_QUERY_KEYS):
            raise AdminValidationError(
                "inference base_url must not embed credential query parameters"
            )

    host = parsed.hostname.rstrip(".").lower()
    if host in _CLOUD_METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise AdminValidationError(
            "inference base_url cannot target cloud metadata or link-local endpoints"
        )

    resolved_allow_local = (
        env_allow_local_inference(default=False) if allow_local is None else allow_local
    )

    try:
        ip_raw = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError as exc:
        if not resolved_allow_local and (host in _LOCAL_HOSTNAMES or host.endswith(".localhost")):
            raise AdminValidationError(
                "inference base_url cannot target local hosts unless local inference is enabled"
            ) from exc
        return normalized

    ip = (
        ip_raw.ipv4_mapped
        if isinstance(ip_raw, ipaddress.IPv6Address) and ip_raw.ipv4_mapped
        else ip_raw
    )
    if (
        ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip == _ALIBABA_METADATA_IP
        or ip == _ORACLE_METADATA_IP
        or (isinstance(ip_raw, ipaddress.IPv6Address) and ip_raw in _AWS_IMDS_IPV6)
    ):
        raise AdminValidationError(
            "inference base_url cannot target cloud metadata, link-local, or unspecified addresses"
        )

    if not resolved_allow_local and (ip.is_loopback or ip.is_private or ip.is_reserved):
        raise AdminValidationError(
            "inference base_url cannot target loopback or private IP addresses "
            "unless local inference is enabled"
        )
    return normalized


def canonical_content_hash(payload: Mapping[str, Any]) -> str:
    try:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AdminValidationError("configuration payload must be valid JSON") from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_rendering_template_syntax(template_text: str) -> None:
    if not isinstance(template_text, str) or not template_text.strip():
        raise AdminValidationError("rendering template_text must not be blank")
    if any(marker in template_text for marker in _FORBIDDEN_TEMPLATE_MARKERS):
        raise AdminValidationError("rendering template control syntax is not supported")
    stripped = _INTERPOLATION_RE.sub("", template_text)
    if "{{" in stripped or "}}" in stripped:
        raise AdminValidationError("rendering template contains unsupported interpolation syntax")


def build_label_definition_domain(
    *,
    version_id: str,
    entity_id: str,
    version_number: int,
    state: str,
    content: Mapping[str, Any],
) -> LabelDefinitionVersion:
    if not isinstance(content, Mapping):
        raise AdminValidationError("label definition content must be an object")
    try:
        return LabelDefinitionVersion(
            version_id=version_id,
            definition_id=entity_id,
            version_number=version_number,
            key=str(content.get("key", "")),
            display_name=str(content.get("display_name", "")),
            description=str(content.get("description", "")),
            scope=LabelScope(str(content.get("scope", ""))),
            negative=bool(content.get("negative", False)),
            enabled=bool(content.get("enabled", True)),
            status=_to_version_status(state),
        )
    except (DomainValidationError, ValueError) as exc:
        raise AdminValidationError(str(exc)) from exc


def build_prompt_template_domain(
    *,
    version_id: str,
    entity_id: str,
    version_number: int,
    state: str,
    content: Mapping[str, Any],
) -> PromptTemplateVersion:
    if not isinstance(content, Mapping):
        raise AdminValidationError("prompt template content must be an object")
    raw_vars = content.get("declared_variables", [])
    if not isinstance(raw_vars, (list, tuple)):
        raise AdminValidationError("declared_variables must be a list of strings")
    try:
        return PromptTemplateVersion(
            version_id=version_id,
            prompt_template_id=entity_id,
            version_number=version_number,
            system_prompt=str(content.get("system_prompt", "")),
            user_prompt_template=str(content.get("user_prompt_template", "")),
            declared_variables=tuple(str(v) for v in raw_vars),
            status=_to_version_status(state),
        )
    except (DomainValidationError, ValueError) as exc:
        raise AdminValidationError(str(exc)) from exc


def build_inference_profile_domain(
    *,
    version_id: str,
    entity_id: str,
    version_number: int,
    state: str,
    content: Mapping[str, Any],
    allow_local: bool | None = None,
) -> InferenceProfileVersion:
    if not isinstance(content, Mapping):
        raise AdminValidationError("inference profile content must be an object")
    raw_caps = content.get("capabilities", ["text"])
    if not isinstance(raw_caps, (list, tuple)):
        raise AdminValidationError("capabilities must be a list of strings")
    sampling = content.get("sampling_parameters", {})
    if not isinstance(sampling, Mapping):
        raise AdminValidationError("sampling_parameters must be an object")
    validated_base_url = validate_inference_base_url(
        str(content.get("base_url", "")),
        allow_local=allow_local,
    )
    try:
        return InferenceProfileVersion(
            version_id=version_id,
            inference_profile_id=entity_id,
            version_number=version_number,
            provider_adapter=str(content.get("provider_adapter", "openai_compatible")),
            base_url=validated_base_url,
            model_name=str(content.get("model_name", "")),
            api_secret_reference=str(content.get("api_secret_reference", "")),
            capabilities=tuple(str(item) for item in raw_caps),
            timeout_seconds=float(content.get("timeout_seconds", 30.0)),
            max_concurrency=int(content.get("max_concurrency", 2)),
            max_images=int(content.get("max_images", 8)),
            max_input_tokens=int(content.get("max_input_tokens", 8192)),
            structured_output_support=bool(content.get("structured_output_support", True)),
            sampling_parameters=dict(sampling),
            status=_to_version_status(state),
        )
    except (DomainValidationError, ValueError, TypeError) as exc:
        raise AdminValidationError(str(exc)) from exc


def build_label_set_domain(
    *,
    version_id: str,
    entity_id: str,
    version_number: int,
    state: str,
    content: Mapping[str, Any],
    resolve_label_def: Callable[[str], LabelDefinitionVersion],
    require_published_refs: bool = False,
) -> LabelSetVersion:
    if not isinstance(content, Mapping):
        raise AdminValidationError("label set content must be an object")
    raw_bindings = content.get("bindings", [])
    if not isinstance(raw_bindings, (list, tuple)) or not raw_bindings:
        raise AdminValidationError("label set bindings must be a non-empty list")
    bindings: list[LabelBinding] = []
    for index, item in enumerate(raw_bindings):
        if not isinstance(item, Mapping):
            raise AdminValidationError("each label binding must be an object")
        def_version_id = str(item.get("label_definition_version_id", "")).strip()
        if not def_version_id:
            raise AdminValidationError("label_definition_version_id is required on each binding")
        label_def = resolve_label_def(def_version_id)
        if require_published_refs and label_def.status is not VersionStatus.PUBLISHED:
            raise AdminValidationError(
                f"referenced label definition version {def_version_id} must be published"
            )
        try:
            bindings.append(
                LabelBinding(
                    label=label_def,
                    activation_threshold=float(item.get("activation_threshold", 0.5)),
                    output_order=int(item.get("output_order", index)),
                    prompt_hint=str(item.get("prompt_hint", "")),
                )
            )
        except (DomainValidationError, ValueError, TypeError) as exc:
            raise AdminValidationError(str(exc)) from exc
    try:
        return LabelSetVersion(
            version_id=version_id,
            label_set_id=entity_id,
            version_number=version_number,
            bindings=tuple(bindings),
            status=_to_version_status(state),
        )
    except DomainValidationError as exc:
        raise AdminValidationError(str(exc)) from exc


def build_stage_template_domain(
    *,
    version_id: str,
    entity_id: str,
    version_number: int,
    state: str,
    content: Mapping[str, Any],
    resolve_prompt: Callable[[str], PromptTemplateVersion],
    resolve_label_set: Callable[[str], LabelSetVersion],
    resolve_inference_profile: Callable[[str], InferenceProfileVersion],
    require_published_refs: bool = False,
) -> AnalysisStageTemplateVersion:
    if not isinstance(content, Mapping):
        raise AdminValidationError("analysis stage content must be an object")
    prompt_vid = str(content.get("prompt_template_version_id", "")).strip()
    label_set_vid = str(content.get("label_set_version_id", "")).strip()
    profile_vid = str(content.get("inference_profile_version_id", "")).strip()
    if not prompt_vid or not label_set_vid or not profile_vid:
        raise AdminValidationError(
            "stage requires prompt_template_version_id, label_set_version_id, "
            "and inference_profile_version_id"
        )
    prompt = resolve_prompt(prompt_vid)
    label_set = resolve_label_set(label_set_vid)
    profile = resolve_inference_profile(profile_vid)
    if require_published_refs:
        for ref_name, ref_status in (
            ("prompt_template_version_id", prompt.status),
            ("label_set_version_id", label_set.status),
            ("inference_profile_version_id", profile.status),
        ):
            if ref_status is not VersionStatus.PUBLISHED:
                raise AdminValidationError(f"referenced {ref_name} must be published")

    raw_so = content.get("structured_output_policy", {})
    raw_in = content.get("input_policy", {"text": True})
    raw_retry = content.get("retry_policy", {})
    if (
        not isinstance(raw_so, Mapping)
        or not isinstance(raw_in, Mapping)
        or not isinstance(raw_retry, Mapping)
    ):
        raise AdminValidationError("stage policy fields must be JSON objects")

    try:
        so_policy = StructuredOutputPolicy(
            mode=StructuredOutputMode(
                str(raw_so.get("mode", StructuredOutputMode.DENSE_SCORES.value))
            ),
            include_reason=bool(raw_so.get("include_reason", False)),
            include_evidence=bool(raw_so.get("include_evidence", False)),
        )
        in_policy = StageInputPolicy(
            text=bool(raw_in.get("text", False)),
            telegram_metadata=bool(raw_in.get("telegram_metadata", False)),
            media=bool(raw_in.get("media", False)),
            previous_results=bool(raw_in.get("previous_results", False)),
            source_channel_context=bool(raw_in.get("source_channel_context", False)),
        )
        retry_policy = StageRetryPolicy(
            max_attempts=int(raw_retry.get("max_attempts", 3)),
            base_delay_seconds=int(raw_retry.get("base_delay_seconds", 5)),
            max_delay_seconds=int(raw_retry.get("max_delay_seconds", 300)),
        )
        vis_raw = content.get(
            "visual_composition_policy",
            VisualCompositionPolicy.ADAPTIVE.value,
        )
        if isinstance(vis_raw, Mapping):
            vis_raw = vis_raw.get("mode", VisualCompositionPolicy.ADAPTIVE.value)
        return AnalysisStageTemplateVersion(
            version_id=version_id,
            stage_template_id=entity_id,
            version_number=version_number,
            name=str(content.get("name", "stage")),
            target_scope=LabelScope(str(content.get("target_scope", LabelScope.GLOBAL.value))),
            execution_mode=ExecutionMode(
                str(content.get("execution_mode", ExecutionMode.SINGLE_MESSAGE.value))
            ),
            prompt=prompt,
            label_set=label_set,
            structured_output_policy=so_policy,
            input_policy=in_policy,
            visual_composition_policy=VisualCompositionPolicy(str(vis_raw)),
            inference_profile=profile,
            cache_policy=CachePolicy(str(content.get("cache_policy", CachePolicy.NONE.value))),
            timeout_seconds=float(content.get("timeout_seconds", 30.0)),
            retry_policy=retry_policy,
            max_batch_size=int(content.get("max_batch_size", 1)),
            max_concurrency=int(content.get("max_concurrency", 1)),
            status=_to_version_status(state),
        )
    except (DomainValidationError, ValueError, TypeError) as exc:
        raise AdminValidationError(str(exc)) from exc


def build_pipeline_domain(
    *,
    version_id: str,
    entity_id: str,
    version_number: int,
    state: str,
    content: Mapping[str, Any],
    resolve_stage: Callable[[str], AnalysisStageTemplateVersion],
    require_published_refs: bool = False,
) -> AnalysisPipelineVersion:
    if not isinstance(content, Mapping):
        raise AdminValidationError("analysis pipeline content must be an object")
    raw_nodes = content.get("nodes", [])
    if not isinstance(raw_nodes, (list, tuple)) or not raw_nodes:
        raise AdminValidationError("pipeline nodes must be a non-empty list")
    nodes: list[PipelineStageNode] = []
    for item in raw_nodes:
        if not isinstance(item, Mapping):
            raise AdminValidationError("each pipeline node must be an object")
        stage_vid = str(item.get("analysis_stage_template_version_id", "")).strip()
        if not stage_vid:
            raise AdminValidationError("analysis_stage_template_version_id is required on node")
        stage = resolve_stage(stage_vid)
        if require_published_refs and stage.status is not VersionStatus.PUBLISHED:
            raise AdminValidationError(
                f"referenced stage version {stage_vid} must be published before pipeline publish"
            )
        deps_raw = item.get("depends_on", [])
        if not isinstance(deps_raw, (list, tuple)):
            raise AdminValidationError("node depends_on must be a list of node keys")
        try:
            nodes.append(
                PipelineStageNode(
                    node_id=str(item.get("node_key") or item.get("node_id") or ""),
                    stage=stage,
                    depends_on=tuple(str(dep) for dep in deps_raw),
                    run_if=item.get("run_if"),
                    parameter_overrides=item.get("parameter_overrides") or {},
                )
            )
        except DomainValidationError as exc:
            raise AdminValidationError(str(exc)) from exc
    try:
        return AnalysisPipelineVersion(
            version_id=version_id,
            analysis_pipeline_id=entity_id,
            version_number=version_number,
            nodes=tuple(nodes),
            status=_to_version_status(state),
        )
    except DomainValidationError as exc:
        raise AdminValidationError(str(exc)) from exc


def validate_routing_policy_content(
    content: Mapping[str, Any],
    *,
    destination_exists: Callable[[str], bool] | None = None,
    identity_exists: Callable[[str], bool] | None = None,
    template_exists: Callable[[str], bool] | None = None,
    template_is_published: Callable[[str], bool] | None = None,
    require_published_refs: bool = False,
) -> None:
    if not isinstance(content, Mapping):
        raise AdminValidationError("routing policy content must be an object")
    raw_rules = content.get("rules", [])
    if not isinstance(raw_rules, (list, tuple)):
        raise AdminValidationError("routing policy rules must be a list")
    for rule in raw_rules:
        if not isinstance(rule, Mapping):
            raise AdminValidationError("each routing rule must be an object")
        priority = rule.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise AdminValidationError("routing rule priority must be an integer")
        condition = rule.get("condition")
        try:
            validate_condition(condition)
        except DomainValidationError as exc:
            raise AdminValidationError(str(exc)) from exc
        actions = rule.get("actions", [])
        if not isinstance(actions, (list, tuple)) or not actions:
            raise AdminValidationError("each routing rule must define at least one action")
        for action in actions:
            if not isinstance(action, Mapping):
                raise AdminValidationError("routing action must be an object")
            dest_id = str(action.get("destination_channel_id", "")).strip()
            if not dest_id:
                raise AdminValidationError("routing action destination_channel_id is required")
            _require_uuid(dest_id, field="destination_channel_id")
            if destination_exists is not None and not destination_exists(dest_id):
                raise AdminValidationError(f"destination channel {dest_id} does not exist")
            pub_identity_id = action.get("publish_identity_id")
            if pub_identity_id is not None:
                if not isinstance(pub_identity_id, str) or not pub_identity_id.strip():
                    raise AdminValidationError(
                        "routing action publish_identity_id must be a valid UUID"
                    )
                cleaned_pub_id = pub_identity_id.strip()
                _require_uuid(cleaned_pub_id, field="publish_identity_id")
                if identity_exists is not None and not identity_exists(cleaned_pub_id):
                    raise AdminValidationError(f"telegram identity {cleaned_pub_id} does not exist")
            try:
                mode = PublicationMode(str(action.get("publication_mode", "")))
            except ValueError as exc:
                raise AdminValidationError("unsupported publication_mode") from exc
            fallback = str(action.get("protected_content_fallback", "fail"))
            if fallback not in {"fail", "metadata_only", "skip"}:
                raise AdminValidationError(
                    "protected_content_fallback must be fail, metadata_only, or skip"
                )
            template_vid = action.get("rendering_template_version_id")
            if mode is PublicationMode.FORWARD_ONLY:
                if template_vid is not None:
                    raise AdminValidationError(
                        "forward_only action cannot reference a rendering_template_version_id"
                    )
            else:
                if not isinstance(template_vid, str) or not template_vid.strip():
                    raise AdminValidationError(
                        f"{mode.value} action requires rendering_template_version_id"
                    )
                cleaned_template_vid = template_vid.strip()
                _require_uuid(cleaned_template_vid, field="rendering_template_version_id")
                if template_exists is not None and not template_exists(cleaned_template_vid):
                    raise AdminValidationError(
                        f"rendering template version {cleaned_template_vid} does not exist"
                    )
                if (
                    require_published_refs
                    and template_is_published is not None
                    and not template_is_published(cleaned_template_vid)
                ):
                    raise AdminValidationError(
                        f"referenced rendering template version {cleaned_template_vid} "
                        "must be published"
                    )


def validate_media_profile_content(content: Mapping[str, Any]) -> None:
    if not isinstance(content, Mapping):
        raise AdminValidationError("media processing profile content must be an object")
    max_dim = content.get("image_max_dimension", 2048)
    webp_q = content.get("webp_quality", 85)
    max_frames = content.get("video_max_frames", 8)
    if not isinstance(max_dim, int) or isinstance(max_dim, bool) or max_dim < 64:
        raise AdminValidationError("image_max_dimension must be an integer >= 64")
    if not isinstance(webp_q, int) or isinstance(webp_q, bool) or not 1 <= webp_q <= 100:
        raise AdminValidationError("webp_quality must be an integer in [1, 100]")
    if not isinstance(max_frames, int) or isinstance(max_frames, bool) or not 1 <= max_frames <= 64:
        raise AdminValidationError("video_max_frames must be an integer in [1, 64]")


def validate_source_profile_content(content: Mapping[str, Any]) -> SourceChannelProfileConfig:
    try:
        return SourceChannelProfileConfig.from_content(content)
    except DomainValidationError as exc:
        raise AdminValidationError(str(exc)) from exc


def _require_uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise AdminValidationError(f"{field} must be a valid UUID") from exc


def _to_version_status(state: str) -> VersionStatus:
    normalized = state.strip().lower()
    if normalized == "retired":
        return VersionStatus.ARCHIVED
    return VersionStatus(normalized)
