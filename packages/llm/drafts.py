"""Map a validated generation result onto the persisted draft record.

Requirements: R16.4 (persist citations, model, tier, prompt version, tokens, cost),
R16.2 (never persist an unvalidated draft), R16.5 (persist only verified citations),
R15.4 (escalation reason or ``none``), R21.6 (cost from the price table).
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

from packages.core.pricing import estimate_inference_cost
from packages.core.settings import ModelPricing
from packages.domain.entities import ContextPackage, GeneratedDraft
from packages.llm.generator import GenerationResult

NO_ESCALATION = "none"
"""Escalation reason recorded when the job stayed on its default tier (R15.4)."""

_REPLY_PREFIX = "Re: "


class UnpersistableDraftError(ValueError):
    """A generation result lacks something every persisted draft must carry."""


def reply_subject(subject: str) -> str | None:
    """Return the reply subject for ``subject``: ``Re: <subject>``, unprefixed twice.

    An empty subject yields ``None`` (stored as NULL) rather than a bare ``Re:``.
    """
    clean = subject.strip()
    if not clean:
        return None
    if clean.lower().startswith("re:"):
        return clean
    return f"{_REPLY_PREFIX}{clean}"


def build_generated_draft(
    result: GenerationResult,
    context: ContextPackage,
    *,
    job_id: UUID | str,
    price_table: Mapping[str, ModelPricing],
    draft_id: UUID | None = None,
) -> GeneratedDraft:
    """Build the ``generated_draft`` record for one validated generation.

    Args:
        result: Output of ``SinglePassGenerator.generate_draft``.
        context: The context package the draft was generated from.
        job_id: Processing job that owns the draft.
        price_table: Configured ``{model: ModelPricing}`` table.
        draft_id: Optional explicit id; a new UUID otherwise.

    Returns:
        A ``GeneratedDraft`` in status ``draft``, not yet persisted.

    Raises:
        UnpersistableDraftError: If the result was never validated, its citations were
            never verified, or the current message has no thread.
    """
    payload = result.validated_payload
    if payload is None:
        raise UnpersistableDraftError(
            "Generation result was never schema-validated; refusing to persist (R16.2)"
        )
    verdict = result.citation_verdict
    if verdict is None:
        raise UnpersistableDraftError(
            "Generation result carries no verified citations; refusing to persist (R16.5)"
        )
    message = context.current_message
    if not str(message.thread_id or "").strip():
        raise UnpersistableDraftError(
            f"Message {message.message_id} has no thread; generated_draft.thread_id is required"
        )

    return GeneratedDraft(
        id=draft_id or uuid4(),
        organization_id=message.organization_id,
        job_id=job_id,
        message_id=message.message_id,
        thread_id=message.thread_id,
        action=payload.action,
        subject=reply_subject(message.subject),
        body=payload.draft,
        confidence=payload.confidence,
        citations=[dict(citation) for citation in verdict.citations],
        citation_mismatch=verdict.mismatch,
        model_name=result.model,
        model_tier=str(result.tier),
        escalation_reason=(
            str(result.escalation_reason) if result.escalation_reason else NO_ESCALATION
        ),
        prompt_version=result.prompt_version,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_estimate=estimate_inference_cost(
            result.model, result.input_tokens, result.output_tokens, price_table
        ),
        status="draft",
    )
