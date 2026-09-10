from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgcurator.shared import DomainValidationError

from .common import VersionStatus, canonical_json, validate_non_blank, validate_positive_int

ALLOWED_PROMPT_VARIABLES = frozenset(
    {
        "label_definitions",
        "message_text",
        "asset_manifest",
        "previous_results",
        "source_channel_context",
    }
)
_VARIABLE_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_EXPRESSION_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True, slots=True)
class PromptTemplateVersion:
    version_id: str
    prompt_template_id: str
    version_number: int
    system_prompt: str
    user_prompt_template: str
    declared_variables: tuple[str, ...]
    status: VersionStatus = VersionStatus.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_id", validate_non_blank(self.version_id, field="prompt version_id")
        )
        object.__setattr__(
            self,
            "prompt_template_id",
            validate_non_blank(self.prompt_template_id, field="prompt template id"),
        )
        validate_positive_int(self.version_number, field="prompt version number")
        if not isinstance(self.system_prompt, str) or not isinstance(
            self.user_prompt_template, str
        ):
            raise DomainValidationError("prompt templates must be strings")
        if not self.system_prompt.strip() and not self.user_prompt_template.strip():
            raise DomainValidationError("at least one prompt template must not be blank")
        if not isinstance(self.declared_variables, tuple) or any(
            not isinstance(variable, str) for variable in self.declared_variables
        ):
            raise DomainValidationError("declared_variables must be a tuple of strings")
        if len(set(self.declared_variables)) != len(self.declared_variables):
            raise DomainValidationError("declared_variables cannot contain duplicates")
        unsupported = set(self.declared_variables) - ALLOWED_PROMPT_VARIABLES
        if unsupported:
            raise DomainValidationError(
                f"prompt declares unsupported variables: {sorted(unsupported)!r}"
            )
        if not isinstance(self.status, VersionStatus):
            raise DomainValidationError("prompt status must be a VersionStatus")

        referenced = _referenced_variables(self.system_prompt, self.user_prompt_template)
        if referenced != set(self.declared_variables):
            missing = referenced - set(self.declared_variables)
            unused = set(self.declared_variables) - referenced
            raise DomainValidationError(
                "prompt declared variables must exactly match template references; "
                f"missing declarations={sorted(missing)!r}, unused declarations={sorted(unused)!r}"
            )

    def render(self, values: Mapping[str, Any]) -> RenderedPrompt:
        if not isinstance(values, Mapping):
            raise DomainValidationError("prompt values must be an object")
        expected = set(self.declared_variables)
        supplied = set(values)
        if supplied != expected:
            raise DomainValidationError(
                "prompt values must exactly match declared variables; "
                f"missing={sorted(expected - supplied)!r}, unknown={sorted(supplied - expected)!r}"
            )
        replacements = {key: _render_value(values[key]) for key in self.declared_variables}
        return RenderedPrompt(
            system_prompt=_render_template(self.system_prompt, replacements),
            user_prompt=_render_template(self.user_prompt_template, replacements),
        )


def _referenced_variables(*templates: str) -> set[str]:
    referenced: set[str] = set()
    for template in templates:
        if "{%" in template or "{#" in template:
            raise DomainValidationError("prompt control blocks and comments are not supported")
        expressions = _EXPRESSION_PATTERN.findall(template)
        simple = _VARIABLE_PATTERN.findall(template)
        if len(expressions) != len(simple):
            raise DomainValidationError("prompt placeholders must contain only a variable name")
        referenced.update(simple)
    unsupported = referenced - ALLOWED_PROMPT_VARIABLES
    if unsupported:
        raise DomainValidationError(
            f"prompt references unsupported variables: {sorted(unsupported)!r}"
        )
    return referenced


def _render_template(template: str, replacements: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return replacements[match.group(1)]

    rendered = _VARIABLE_PATTERN.sub(replace, template)
    if _EXPRESSION_PATTERN.search(rendered):
        raise DomainValidationError("prompt contains an unresolved placeholder")
    return rendered


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return canonical_json(value)
