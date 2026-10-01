"""Declarative pure rule engine for Stage 1 email triage (R6.1, R6.8, R24.3).

This module is a pure domain component without external I/O or network dependencies.
Evaluates email attributes (sender, headers, subject, body, attachments) against
declarative rules and produces a structured Classification entity with sub-millisecond
latency.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from packages.domain.entities import Classification, NormalizedMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailContext:
    """Normalized email context evaluated by declarative rule predicates."""

    sender_email: str = ""
    sender_name: str = ""
    recipients: tuple[str, ...] = field(default_factory=tuple)
    cc: tuple[str, ...] = field(default_factory=tuple)
    subject: str = ""
    subject_normalized: str = ""
    body_text: str = ""
    body_text_clean: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    attachments_count: int = 0
    attachments_filenames: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_message(cls, message: NormalizedMessage) -> EmailContext:
        """Create an EmailContext from a canonical NormalizedMessage entity."""
        headers_lower = {str(k).lower(): str(v) for k, v in message.headers.items()}
        return cls(
            sender_email=message.sender.email.lower() if message.sender else "",
            sender_name=message.sender.name or "" if message.sender else "",
            recipients=tuple(r.email.lower() for r in message.recipients),
            cc=tuple(c.email.lower() for c in message.cc),
            subject=message.subject or "",
            subject_normalized=message.subject_normalized or "",
            body_text=message.body_text or "",
            body_text_clean=message.body_text_clean or message.body_text or "",
            headers=headers_lower,
            attachments_count=len(message.attachments),
            attachments_filenames=tuple(a.filename for a in message.attachments),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmailContext:
        """Create an EmailContext from a dictionary contract or raw JSON payload."""
        sender = data.get("sender") or {}
        if isinstance(sender, dict):
            sender_email = str(sender.get("email") or "")
            sender_name = str(sender.get("name") or "")
        else:
            sender_email = str(data.get("sender_email") or "")
            sender_name = str(data.get("sender_name") or "")

        recipients = data.get("recipients") or []
        rec_emails = [
            (r.get("email", "") if isinstance(r, dict) else str(r)).lower() for r in recipients
        ]

        cc = data.get("cc") or []
        cc_emails = [(c.get("email", "") if isinstance(c, dict) else str(c)).lower() for c in cc]

        raw_headers = data.get("headers") or {}
        headers_lower = {str(k).lower(): str(v) for k, v in raw_headers.items()}

        attachments = data.get("attachments") or []
        att_filenames = [
            a.get("filename", "") if isinstance(a, dict) else str(a) for a in attachments
        ]

        return cls(
            sender_email=sender_email.lower(),
            sender_name=sender_name,
            recipients=tuple(rec_emails),
            cc=tuple(cc_emails),
            subject=str(data.get("subject") or ""),
            subject_normalized=str(data.get("subject_normalized") or ""),
            body_text=str(data.get("body_text") or ""),
            body_text_clean=str(data.get("body_text_clean") or data.get("body") or ""),
            headers=headers_lower,
            attachments_count=len(attachments),
            attachments_filenames=tuple(att_filenames),
        )


class ConditionEvaluator(Protocol):
    """Protocol for rule condition evaluators."""

    def evaluate(self, context: EmailContext) -> bool:
        """Evaluate predicate against an email context."""
        ...


@dataclass(frozen=True)
class FieldPredicate:
    """Atomic field-level predicate (e.g. header.Auto-Submitted exists, body matches)."""

    field_path: str
    exists: bool | None = None
    equals: str | None = None
    contains: str | None = None
    starts_with: str | None = None
    ends_with: str | None = None
    regex_pattern: str | None = None
    compiled_regex: re.Pattern[str] | None = None

    def __post_init__(self) -> None:
        if self.regex_pattern and self.compiled_regex is None:
            object.__setattr__(
                self,
                "compiled_regex",
                re.compile(self.regex_pattern, re.IGNORECASE),
            )
        if (
            self.exists is None
            and self.equals is None
            and self.contains is None
            and self.starts_with is None
            and self.ends_with is None
            and self.regex_pattern is None
        ):
            raise ValueError(
                f"FieldPredicate for '{self.field_path}' must specify at least one "
                "condition operator"
            )

    def _extract_field_value(self, context: EmailContext) -> Any:
        path = self.field_path.strip().lower()

        if path.startswith("header."):
            header_name = path[7:].strip()
            return context.headers.get(header_name)

        if path in ("sender.email", "sender"):
            return context.sender_email
        if path == "sender.name":
            return context.sender_name
        if path == "subject":
            return context.subject
        if path == "subject_normalized":
            return context.subject_normalized
        if path in ("body", "body_text", "body_text_clean"):
            return context.body_text_clean or context.body_text
        if path in ("recipients", "recipient.email"):
            return context.recipients
        if path == "cc":
            return context.cc
        if path == "has_attachments":
            return context.attachments_count > 0
        if path in ("attachments.count", "attachments_count"):
            return context.attachments_count
        if path == "attachments.filenames":
            return context.attachments_filenames

        return None

    def evaluate(self, context: EmailContext) -> bool:
        val = self._extract_field_value(context)

        # 1. Existence check
        if self.exists is not None:
            has_val = val is not None and val != "" and val is not False
            if has_val != self.exists:
                return False

        if val is None:
            return False

        # Convert to string for text comparisons if it is not a collection
        val_str = str(val) if not isinstance(val, (list, tuple)) else None

        # 2. Equality check (case-insensitive)
        if self.equals is not None and (
            val_str is None or val_str.strip().lower() != self.equals.strip().lower()
        ):
            return False

        # 3. Contains check (substring or sequence item)
        if self.contains is not None:
            expected = self.contains.lower()
            if isinstance(val, (list, tuple)):
                if not any(expected in item.lower() for item in val):
                    return False
            elif val_str is not None:
                if expected not in val_str.lower():
                    return False
            else:
                return False

        # 4. Starts with check
        if self.starts_with is not None and (
            val_str is None or not val_str.lower().startswith(self.starts_with.lower())
        ):
            return False

        # 5. Ends with check
        if self.ends_with is not None and (
            val_str is None or not val_str.lower().endswith(self.ends_with.lower())
        ):
            return False

        # 6. Regex match check
        if self.compiled_regex is not None:
            if isinstance(val, (list, tuple)):
                if not any(self.compiled_regex.search(item) for item in val):
                    return False
            elif val_str is not None:
                if not self.compiled_regex.search(val_str):
                    return False
            else:
                return False

        return True


KNOWN_OPERATORS = frozenset(
    {
        "matches",
        "equals",
        "contains",
        "starts_with",
        "ends_with",
        "exists",
    }
)


@dataclass(frozen=True)
class CompositeCondition:
    """Boolean composite condition (any / all / not)."""

    operator: str  # 'any' | 'all' | 'not'
    children: tuple[ConditionEvaluator, ...]

    def evaluate(self, context: EmailContext) -> bool:
        if self.operator == "any":
            return any(c.evaluate(context) for c in self.children)
        if self.operator == "all":
            return all(c.evaluate(context) for c in self.children)
        if self.operator == "not":
            return not any(c.evaluate(context) for c in self.children)
        raise ValueError(f"Unknown composite operator: {self.operator}")


def parse_condition(spec: dict[str, Any]) -> ConditionEvaluator:
    """Recursively parse declarative condition dictionaries into ConditionEvaluators."""
    if "any" in spec:
        children = [parse_condition(item) for item in spec["any"]]
        return CompositeCondition(operator="any", children=tuple(children))

    if "all" in spec:
        children = [parse_condition(item) for item in spec["all"]]
        return CompositeCondition(operator="all", children=tuple(children))

    if "not" in spec:
        not_spec = spec["not"]
        child = parse_condition(not_spec if isinstance(not_spec, dict) else {"all": not_spec})
        return CompositeCondition(operator="not", children=(child,))

    # Handle single or multiple field predicates at this level
    predicates: list[ConditionEvaluator] = []
    for raw_key, raw_val in spec.items():
        # Shorthand notation support: e.g. "body.matches: '\\bINV-\\d+\\b'"
        if "." in raw_key:
            prefix, suffix = raw_key.rsplit(".", 1)
            if suffix.lower() in KNOWN_OPERATORS:
                predicates.append(parse_field_predicate(prefix, {suffix.lower(): raw_val}))
                continue
        predicates.append(parse_field_predicate(raw_key, raw_val))

    if len(predicates) == 1:
        return predicates[0]
    return CompositeCondition(operator="all", children=tuple(predicates))


def parse_field_predicate(field_path: str, val_spec: Any) -> FieldPredicate:
    """Parse a single field predicate specification."""
    if isinstance(val_spec, dict):
        return FieldPredicate(
            field_path=field_path,
            exists=val_spec.get("exists"),
            equals=val_spec.get("equals"),
            contains=val_spec.get("contains"),
            starts_with=val_spec.get("starts_with"),
            ends_with=val_spec.get("ends_with"),
            regex_pattern=val_spec.get("matches"),
        )
    # Scalar shorthand: {"header.Auto-Submitted": true} or {"body": "pattern"}
    if isinstance(val_spec, bool):
        return FieldPredicate(field_path=field_path, exists=val_spec)
    if isinstance(val_spec, str):
        return FieldPredicate(field_path=field_path, equals=val_spec)

    raise ValueError(f"Invalid condition predicate for field {field_path}: {val_spec}")


@dataclass(frozen=True)
class RuleAction:
    """Action and classification attributes emitted when a rule matches."""

    category: str
    intent: str | None = None
    priority: str = "normal"  # urgent | high | normal | low
    reply_required: bool = True
    workflow_hint: str = "ai"  # ai | template | none
    retrieval_required: bool = True
    confidence: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleAction:
        reply_req = bool(data.get("reply_required", True))
        default_workflow_hint = "none" if not reply_req else "ai"
        workflow_hint = str(data.get("workflow_hint", default_workflow_hint))
        retrieval_req = bool(data.get("retrieval_required", reply_req))

        return cls(
            category=str(data.get("category", "general_inquiry")),
            intent=data.get("intent"),
            priority=str(data.get("priority", "normal")),
            reply_required=reply_req,
            workflow_hint=workflow_hint,
            retrieval_required=retrieval_req,
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class Rule:
    """Declarative triage rule with unique identifier, condition, and action."""

    id: str
    condition: ConditionEvaluator
    action: RuleAction
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        rule_id = str(data.get("id", "unnamed_rule"))
        description = str(data.get("description", ""))
        when_spec = data.get("when") or {}
        then_spec = data.get("then") or {}

        condition = parse_condition(when_spec)
        action = RuleAction.from_dict(then_spec)

        return cls(
            id=rule_id,
            condition=condition,
            action=action,
            description=description,
        )

    def evaluate(self, context: EmailContext) -> bool:
        """Evaluate condition against email context."""
        return self.condition.evaluate(context)


class RuleEngine:
    """Pure domain rule engine evaluating declarative conditions against emails (R6.1, R6.8)."""

    def __init__(self, rules: Sequence[Rule] | None = None) -> None:
        self.rules: list[Rule] = list(rules or [])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleEngine:
        """Construct RuleEngine from dictionary data containing 'rules' list."""
        raw_rules = data.get("rules", [])
        parsed_rules: list[Rule] = [Rule.from_dict(r) for r in raw_rules]
        return cls(rules=parsed_rules)

    @classmethod
    def from_json(cls, json_str: str) -> RuleEngine:
        """Construct RuleEngine from JSON string."""
        return cls.from_dict(json.loads(json_str))

    def evaluate(
        self,
        message: NormalizedMessage | EmailContext | dict[str, Any],
    ) -> Classification | None:
        """Evaluate email message against registered rules in order.

        Returns:
            Classification with decided_by='rule' if a rule matches, else None.
        """
        start_time = time.perf_counter()

        if isinstance(message, EmailContext):
            context = message
        elif isinstance(message, NormalizedMessage):
            context = EmailContext.from_message(message)
        elif isinstance(message, dict):
            context = EmailContext.from_dict(message)
        else:
            raise TypeError(f"Unsupported message type for rule evaluation: {type(message)}")

        for rule in self.rules:
            if rule.evaluate(context):
                elapsed_ms = int((time.perf_counter() - start_time) * 1000)
                return Classification(
                    category=rule.action.category,
                    intent=rule.action.intent,
                    priority=rule.action.priority,
                    reply_required=rule.action.reply_required,
                    workflow_hint=rule.action.workflow_hint,
                    retrieval_required=rule.action.retrieval_required,
                    confidence=rule.action.confidence,
                    decided_by="rule",
                    latency_ms=elapsed_ms,
                    model=None,
                    raw={
                        "rule_id": rule.id,
                        "rule_description": rule.description,
                    },
                )

        return None
