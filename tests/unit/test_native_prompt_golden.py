"""Golden proof that rag-email's native (C0) prompt does not change (task 7.20, ADR-0012 2a).

The reply-format rules of the profile templates moved into one shared source
(``packages/llm/reply_format.py``) so the guarded prompt can carry them too. The native prompt
must render byte for byte as it did before that move. The golden files in
``tests/unit/golden/native_prompt`` were rendered from the templates at 49fab03, before the move;
a change to a golden file is a change to the native prompt and belongs to a decision, not to a
test fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityType,
    FactStatus,
)
from packages.domain.entities import Candidate, ContextPackage, EmailAddress, NormalizedMessage
from packages.llm.profile import DEFAULT_ENTERPRISE_INSTRUCTIONS, AgentProfileRegistry

GOLDEN = Path(__file__).parent / "golden" / "native_prompt"
REGISTRY_PATH = "config/agent_profiles.yaml"
PROFILES = ("technical_support", "billing", "sales", "general_inquiry")
STEMS = {
    "technical_support": "support",
    "billing": "billing",
    "sales": "sales",
    "general_inquiry": "general",
}
AS_OF = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
ORG = UUID("00000000-0000-0000-0000-0000000000a1")


def _message(subject: str, body: str, *, n: int) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=UUID(int=n),
        organization_id=ORG,
        mailbox_id=UUID(int=100 + n),
        thread_id=UUID(int=200),
        provider="mock",
        provider_message_id=f"msg-{n}",
        sender=EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith"),
        subject=subject,
        body_text=body,
        body_text_clean=body,
        received_at=AS_OF,
    )


def _business() -> BusinessContext:
    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"), ("tier", "enterprise")),
        facts=(
            BusinessFact(
                entity=EntityType.ORDER,
                reference="ORD-82915",
                status=FactStatus.FOUND,
                attributes=(("status", "shipped"),),
            ),
        ),
    )


def package(*, full: bool) -> ContextPackage:
    """A fixed context: ``full`` adds thread history, knowledge chunks and business data."""
    chunks = (
        [
            Candidate(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Order status is visible online.",
                external_id="KB-ORDERS-01",
            ),
            Candidate(chunk_id="chunk-2", document_id="doc-2", content="Refunds take 5 days."),
        ]
        if full
        else []
    )
    return ContextPackage(
        agent_instructions=DEFAULT_ENTERPRISE_INSTRUCTIONS,
        category_instructions="Answer inquiries accurately and provide polite, helpful assistance.",
        current_message=_message("Order status", "What is the status of order 82915?", n=1),
        thread_summary="The customer asked about an order last week." if full else None,
        recent_messages=[_message("Order", "Earlier question about shipping.", n=2)]
        if full
        else [],
        retrieved_chunks=chunks,
        business_data=_business() if full else None,
    )


@pytest.mark.parametrize("full", [True, False], ids=["full_context", "email_only"])
@pytest.mark.parametrize("profile", PROFILES)
def test_v2_native_prompt_is_byte_identical_to_the_pre_move_golden(
    profile: str, full: bool
) -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    active = registry.get_profile(profile)
    assert active is not None
    # The goldens pin the v2 templates, which stay on disk for the runs that recorded them. The
    # active profiles moved to v3 (task 7.27); test_reply_prompt_task_line.py pins those.
    v2 = active.model_copy(update={"prompt_template": f"prompts/{STEMS[profile]}.v2.j2"})
    rendered = registry.render_prompt(v2, package(full=full))
    golden = (GOLDEN / f"{profile}.{'full' if full else 'email_only'}.txt").read_bytes()
    assert rendered.encode("utf-8") == golden
