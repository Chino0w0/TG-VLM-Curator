from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.domain.publishing import rendered_snapshot_hash
from tgcurator.domain.routing import canonical_facts_snapshot
from tgcurator.shared import DomainValidationError

_INTERPOLATION = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}")
_FORBIDDEN_MARKERS = ("{%", "%}", "{#", "#}")


@dataclass(frozen=True, slots=True)
class RenderedPublicationSnapshot:
    text: str
    context: Mapping[str, Any]
    snapshot_hash: str


class RestrictedTemplateRenderer:
    """Render strict mapping-only placeholders without object traversal or code execution.

    The first version intentionally supports only `{{ explicit.mapping.path }}` interpolation.
    Control blocks, filters, calls, indexing, and arbitrary attributes are rejected. This keeps the
    template boundary deterministic and safe while still covering message/source/label metadata.
    """

    def render(
        self,
        *,
        template_text: str,
        context: Mapping[str, Any],
    ) -> RenderedPublicationSnapshot:
        if not isinstance(template_text, str) or not template_text.strip():
            raise DomainValidationError("rendering template text must not be blank")
        if not isinstance(context, Mapping):
            raise DomainValidationError("rendering context must be a mapping")
        if any(marker in template_text for marker in _FORBIDDEN_MARKERS):
            raise DomainValidationError("rendering template control syntax is not supported")

        canonical_context = canonical_facts_snapshot(context)

        def replace(match: re.Match[str]) -> str:
            value: Any = canonical_context
            path = match.group(1)
            for segment in path.split("."):
                if not isinstance(value, Mapping) or segment not in value:
                    raise DomainValidationError(f"rendering template variable is undefined: {path}")
                value = value[segment]
            return _render_value(value)

        rendered = _INTERPOLATION.sub(replace, template_text)
        if "{{" in rendered or "}}" in rendered:
            raise DomainValidationError("rendering template contains unsupported interpolation")
        return RenderedPublicationSnapshot(
            text=rendered,
            context=canonical_context,
            snapshot_hash=rendered_snapshot_hash(
                text=rendered,
                context=dict(canonical_context),
            ),
        )


def build_rendering_context(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only the documented, JSON-like routing facts to a rendering template."""

    canonical = canonical_facts_snapshot(facts)
    message = canonical.get("message")
    source = canonical.get("source_channel")
    media = canonical.get("media")
    if not isinstance(message, Mapping) or not isinstance(source, Mapping):
        raise DomainValidationError("routing facts do not contain message/source rendering context")
    if not isinstance(media, list):
        raise DomainValidationError("routing facts media rendering context must be a list")

    primary_message_id = message.get("primary_telegram_message_id")
    username = source.get("username")
    source_link: str | None = None
    if isinstance(username, str) and username.strip() and isinstance(primary_message_id, int):
        source_link = f"https://t.me/{username.lstrip('@')}/{primary_message_id}"

    context = {
        "message": dict(message),
        "source_channel": dict(source),
        "labels": canonical.get("labels", {}),
        "duplicate": canonical.get("duplicate", {}),
        "media": media,
        "blocked_from_analysis": canonical.get("blocked_from_analysis", False),
        "review_status": canonical.get("review_status"),
        "telegram_source_link": source_link,
        "media_count": message.get("media_count", len(media)),
    }
    return canonical_facts_snapshot(context)


def _render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raise DomainValidationError("rendering template value must be JSON-compatible")
