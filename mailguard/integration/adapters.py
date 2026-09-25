"""Adapters between the rag-email core (branch ``RAG_Email_System``) and MailGuard.

The core exposes ``packages.domain.entities.ContextPackage`` with a
``NormalizedMessage`` (``current_message``), ``Candidate`` chunks
(``retrieved_chunks``), ``business_data`` and the agent/category instructions.
Nothing here imports the core: every accessor is duck-typed so the guard can be
deployed as a separate worker or embedded in the core's AI worker.

Typical embedding inside the core's reply agent::

    guard = GuardedReplyAgent(pipeline=MailGuardPipeline(), provider=core_llm_provider)
    draft, report = await guard.generate_reply(context_package)
    if report.decision.action in (PolicyAction.BLOCK, PolicyAction.QUARANTINE):
        ...  # do not persist a draft; notify the operator
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from mailguard.contracts.email import DraftCandidate, GuardedEmail, RetrievedChunk
from mailguard.contracts.policy import GuardReport, PolicyAction
from mailguard.llm.protocol import ChatMessage, LLMError, LLMProvider, ModelTier
from mailguard.llm.structured import call_structured
from mailguard.pipeline import MailGuardPipeline, PromptBundle

logger = logging.getLogger(__name__)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def guarded_email_from_context(ctx: Any) -> GuardedEmail:
    """``ContextPackage.current_message`` (NormalizedMessage) -> GuardedEmail."""
    message = _get(ctx, "current_message", ctx)
    email = GuardedEmail.from_any(message)
    classification = _get(ctx, "classification")
    if classification is not None and not email.category:
        email = email.model_copy(update={"category": _get(classification, "category")})
    return email


def chunks_from_context(ctx: Any) -> list[RetrievedChunk]:
    return [RetrievedChunk.from_any(c) for c in (_get(ctx, "retrieved_chunks", []) or [])]


def recent_messages_from_context(ctx: Any, limit: int = 5) -> list[str]:
    out = []
    for m in (_get(ctx, "recent_messages", []) or [])[-limit:]:
        sender = _get(m, "sender")
        sender_email = (
            _get(sender, "email", "") if sender is not None else _get(m, "sender_email", "")
        )
        body = _get(m, "body_text_clean") or _get(m, "body_text", "")
        out.append(f"From: {sender_email}\nSubject: {_get(m, 'subject', '')}\n{body}")
    return out


class ReplySchema(BaseModel):
    action: str = "reply"
    subject: str = ""
    body: str = ""
    citations: list[str] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    security_notes: str = ""
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class GuardedReplyAgent:
    """Wrap a core LLM provider so every reply is generated through the guarded prompt."""

    def __init__(
        self,
        pipeline: MailGuardPipeline,
        provider: LLMProvider,
        *,
        tier: ModelTier = ModelTier.ROUTINE,
        max_tokens: int = 900,
    ) -> None:
        self.pipeline = pipeline
        self.provider = provider
        self.tier = tier
        self.max_tokens = max_tokens

    async def _generate(self, messages: list[ChatMessage]) -> DraftCandidate:
        try:
            out, result = await call_structured(
                self.provider, messages, ReplySchema, tier=self.tier, max_tokens=self.max_tokens
            )
        except LLMError as exc:
            logger.error("reply generation failed: %s", exc)
            return DraftCandidate(body="", subject="[generation failed]", action="escalate")
        return DraftCandidate(
            body=out.body,
            subject=out.subject or None,
            citations=[str(c) for c in out.citations],
            recipients=[r.lower() for r in out.recipients if isinstance(r, str)],
            action=out.action if out.action in ("reply", "forward", "escalate") else "reply",
            model_name=result.model,
        )

    async def generate_reply(
        self, ctx: Any, *, system_instructions: str | None = None, category: str | None = None
    ) -> tuple[DraftCandidate | None, GuardReport, PromptBundle | None]:
        email = guarded_email_from_context(ctx)
        chunks = chunks_from_context(ctx)
        system = system_instructions or str(_get(ctx, "agent_instructions", "") or "")
        category_instructions = str(_get(ctx, "category_instructions", "") or "")
        report, draft, bundle = await self.pipeline.run(
            email,
            chunks,
            self._generate,
            system_instructions=system,
            category_instructions=category_instructions,
            thread_summary=_get(ctx, "thread_summary"),
            recent_messages=recent_messages_from_context(ctx),
            business_data=dict(_get(ctx, "business_data", {}) or {}),
            category=category or email.category,
        )
        return draft, report, bundle


def decision_to_job_result(report: GuardReport, draft: DraftCandidate | None) -> dict[str, Any]:
    """Serialise the guard outcome into the core's ``Job.result_ref`` shape."""
    d = report.decision
    action = d.action if d else PolicyAction.DRAFT_ONLY
    return {
        "mailguard": {
            "action": str(action),
            "risk_tier": str(d.risk_tier) if d else None,
            "audit_id": d.audit_id if d else None,
            "rule": d.matched_rule_id if d else None,
            "requires_human": bool(d and d.requires_human),
            "quarantined_chunks": d.quarantined_chunk_ids if d else [],
            "redactions": d.redactions_applied if d else 0,
            "max_severity": str(report.max_severity),
            "latency_ms": report.total_latency_ms,
        },
        "draft": None
        if draft is None or action in (PolicyAction.BLOCK, PolicyAction.QUARANTINE)
        else {
            "subject": draft.subject,
            "body": draft.body,
            "citations": draft.citations,
            "action": draft.action,
        },
    }


def report_json(report: GuardReport, *, indent: int | None = None) -> str:
    return json.dumps(
        report.model_dump(mode="json"), ensure_ascii=False, indent=indent, default=str
    )


def dispatch_allowed(report: GuardReport, requested: Sequence[str]) -> bool:
    """Dispatcher-side check: never send when the decision is not AUTO_SEND."""
    d = report.decision
    return bool(d and d.action is PolicyAction.AUTO_SEND and not d.requires_human and requested)


__all__ = [
    "GuardedReplyAgent",
    "ReplySchema",
    "chunks_from_context",
    "decision_to_job_result",
    "dispatch_allowed",
    "guarded_email_from_context",
    "recent_messages_from_context",
    "report_json",
]
