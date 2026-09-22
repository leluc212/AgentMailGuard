"""Pure domain templates and deterministic variable substitution engine.

Requirements:
- R6.12: Emit workflow_hint in {template, ai, none}.
- R6.13: Render deterministic approved-template reply; zero retrieval, zero generation.
- R6.14: Template registry keyed by (category, intent) with variable substitution from
  message and business fields; fall back to workflow_hint='ai' when no template matches.
- R6.15: Exactly three mutually exclusive outcomes (early exit, template reply, AI generation).
- GEMINI.md: packages/domain imports standard library and packages/core ONLY.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.domain.entities import EmailAddress, NormalizedMessage
from packages.domain.rules import EmailContext

logger = logging.getLogger(__name__)

# Regex pattern matching {{ variable }} or {{ object.field }}
VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


@dataclass(frozen=True)
class TemplateDefinition:
    """Definition of an approved deterministic response template (R6.14, design.md §5.3)."""

    id: str
    category: str
    intent: str
    subject: str = "Re: {{ subject }}"
    body: str = ""
    version: str = "v1"
    is_active: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemplateDefinition:
        """Construct TemplateDefinition from dictionary structure."""
        match_spec = data.get("match", {})
        category = str(match_spec.get("category") or data.get("category") or "").strip().lower()
        intent = str(match_spec.get("intent") or data.get("intent") or "").strip().lower()

        if not category:
            raise ValueError("Template definition requires a non-empty 'category'.")
        if not intent:
            raise ValueError("Template definition requires a non-empty 'intent'.")

        template_id = str(data.get("id") or f"{category}_{intent}_{data.get('version', 'v1')}")
        version = str(data.get("version", "v1"))
        subject = str(data.get("subject", "Re: {{ subject }}"))
        body = str(data.get("body", ""))
        is_active = bool(data.get("is_active", True))

        return cls(
            id=template_id,
            category=category,
            intent=intent,
            subject=subject,
            body=body,
            version=version,
            is_active=is_active,
        )


@dataclass(frozen=True)
class TemplateRenderResult:
    """Rendered draft subject and body with variable tracking (R6.13)."""

    template_id: str
    version: str
    subject: str
    body: str
    variables_used: dict[str, Any] = field(default_factory=dict)


def _resolve_nested_key(context: dict[str, Any], path: str) -> Any:
    """Traverse a dotted path in a nested dictionary/object hierarchy.

    Returns:
        The resolved value, or None if the path does not exist.
    """
    if path in context:
        return context[path]

    parts = path.split(".")
    curr: Any = context
    for part in parts:
        if isinstance(curr, dict):
            if part in curr:
                curr = curr[part]
            else:
                return None
        elif hasattr(curr, part):
            curr = getattr(curr, part)
        else:
            return None

    return curr


def substitute_variables(
    template_str: str,
    context: dict[str, Any],
    fallback_empty: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Substitute {{ variable }} and {{ object.field }} tokens in a template.

    Args:
        template_str: Template string with mustache-style variables.
        context: Context mapping containing message and business fields.
        fallback_empty: If True, replace unresolved tokens with empty string.
                        If False, keep the unresolved token in the output.

    Returns:
        tuple of (rendered_string, variables_used_mapping).
    """
    variables_used: dict[str, Any] = {}

    def _replacer(match: re.Match[str]) -> str:
        var_name = match.group(1).strip()
        val = _resolve_nested_key(context, var_name)

        if val is None:
            # Check if variable exists inside 'business_data'
            biz_data = context.get("business_data")
            if isinstance(biz_data, dict) and var_name in biz_data:
                val = biz_data[var_name]

        if val is None:
            if fallback_empty:
                variables_used[var_name] = ""
                return ""
            return match.group(0)

        # Convert value to clean string representation
        if isinstance(val, EmailAddress):
            val_str = val.name or val.email
        elif isinstance(val, (dict, list)):
            val_str = json.dumps(val)
        else:
            val_str = str(val)

        variables_used[var_name] = val_str
        return val_str

    rendered = VARIABLE_PATTERN.sub(_replacer, template_str)
    return rendered, variables_used


def build_template_context(
    message: NormalizedMessage | EmailContext | dict[str, Any],
    business_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble context dictionary from message attributes and business data."""
    context: dict[str, Any] = {}
    biz = business_data or {}
    context["business_data"] = biz

    # Flatten top-level business keys into context for direct access
    for k, v in biz.items():
        if k not in context:
            context[k] = v

    if isinstance(message, NormalizedMessage):
        context["subject"] = message.subject
        context["subject_normalized"] = message.subject_normalized
        context["sender_name"] = message.sender.name or ""
        context["sender_email"] = message.sender.email
        context["sender"] = {"name": message.sender.name or "", "email": message.sender.email}
        context["message_id"] = message.rfc822_message_id or str(message.message_id)
        context["thread_id"] = str(message.thread_id)
        context["body_text"] = message.body_text
        context["body_text_clean"] = message.body_text_clean or message.body_text
        context["received_at"] = message.received_at.isoformat() if message.received_at else ""
    elif isinstance(message, EmailContext):
        context["subject"] = message.subject
        context["subject_normalized"] = message.subject_normalized
        context["sender_name"] = message.sender_name
        context["sender_email"] = message.sender_email
        context["sender"] = {"name": message.sender_name, "email": message.sender_email}
        context["body_text"] = message.body_text
        context["body_text_clean"] = message.body_text_clean or message.body_text
        context["headers"] = message.headers
    elif isinstance(message, dict):
        sender = message.get("sender")
        if isinstance(sender, dict) and (sender.get("name") or sender.get("email")):
            s_name = str(sender.get("name") or "")
            s_email = str(sender.get("email") or "")
        else:
            s_name = str(message.get("sender_name") or "")
            s_email = str(message.get("sender_email") or "")

        context["subject"] = str(message.get("subject") or "")
        context["subject_normalized"] = str(message.get("subject_normalized") or "")
        context["sender_name"] = s_name
        context["sender_email"] = s_email
        context["sender"] = {"name": s_name, "email": s_email}
        context["message_id"] = str(message.get("message_id") or message.get("id") or "")
        context["thread_id"] = str(message.get("thread_id") or "")
        context["body_text"] = str(message.get("body_text") or "")
        context["body_text_clean"] = str(
            message.get("body_text_clean") or message.get("body_text") or ""
        )
    else:
        raise TypeError(f"Unsupported message type for template context: {type(message)}")

    return context


class TemplateRegistry:
    """Domain registry of approved deterministic response templates (R6.14).

    Templates are keyed by (category, intent) tuple.
    """

    def __init__(self, templates: Sequence[TemplateDefinition] | None = None) -> None:
        self._templates: list[TemplateDefinition] = []
        self._by_key: dict[tuple[str, str], TemplateDefinition] = {}

        if templates:
            for tmpl in templates:
                self.register(tmpl)

    @property
    def templates(self) -> list[TemplateDefinition]:
        """Return all registered templates."""
        return list(self._templates)

    def register(self, template: TemplateDefinition) -> None:
        """Register a template definition. Overwrites any existing template for the key."""
        key = (template.category.strip().lower(), template.intent.strip().lower())
        self._by_key[key] = template
        # Replace or append in list
        self._templates = [
            t
            for t in self._templates
            if (t.category, t.intent) != (template.category, template.intent)
        ]
        self._templates.append(template)

    def find_template(self, category: str, intent: str | None) -> TemplateDefinition | None:
        """Find an active template keyed by (category, intent).

        Returns:
            TemplateDefinition if found and active, else None.
        """
        cat_key = category.strip().lower()
        int_key = (intent or "").strip().lower()

        tmpl = self._by_key.get((cat_key, int_key))
        if tmpl and tmpl.is_active:
            return tmpl

        # If intent was None or not found, try empty string intent
        if not int_key:
            tmpl = self._by_key.get((cat_key, ""))
            if tmpl and tmpl.is_active:
                return tmpl

        return None

    def resolve_body(
        self,
        template: TemplateDefinition,
        base_dir: Path | str | None = None,
    ) -> str:
        """Resolve template body text, loading from disk if body refers to an existing file."""
        body = template.body
        # Check if body refers to a relative or absolute file path
        if body and (body.endswith((".txt", ".j2", ".md")) or "/" in body or "\\" in body):
            candidate_path = Path(body)
            if candidate_path.is_file():
                try:
                    return candidate_path.read_text(encoding="utf-8")
                except Exception as exc:
                    logger.warning("Failed to read template file %s: %s", candidate_path, exc)
            if base_dir:
                resolved_path = Path(base_dir) / candidate_path
                if resolved_path.is_file():
                    try:
                        return resolved_path.read_text(encoding="utf-8")
                    except Exception as exc:
                        logger.warning("Failed to read template file %s: %s", resolved_path, exc)
        return body

    def render(
        self,
        template: TemplateDefinition,
        message: NormalizedMessage | EmailContext | dict[str, Any],
        business_data: dict[str, Any] | None = None,
        base_dir: Path | str | None = None,
    ) -> TemplateRenderResult:
        """Render a deterministic template reply with variable substitution (R6.13)."""
        context = build_template_context(message, business_data)
        raw_body = self.resolve_body(template, base_dir=base_dir)

        rendered_subject, subj_vars = substitute_variables(template.subject, context)
        rendered_body, body_vars = substitute_variables(raw_body, context)

        merged_vars = {**subj_vars, **body_vars}
        return TemplateRenderResult(
            template_id=template.id,
            version=template.version,
            subject=rendered_subject.strip(),
            body=rendered_body.strip(),
            variables_used=merged_vars,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemplateRegistry:
        """Construct TemplateRegistry from dictionary data."""
        raw_templates = data.get("templates", [])
        if not isinstance(raw_templates, list):
            raise ValueError("Invalid template configuration: 'templates' must be a list.")

        parsed = [TemplateDefinition.from_dict(t) for t in raw_templates]
        return cls(templates=parsed)

    @classmethod
    def from_json(cls, json_str: str) -> TemplateRegistry:
        """Construct TemplateRegistry from JSON string."""
        return cls.from_dict(json.loads(json_str))
