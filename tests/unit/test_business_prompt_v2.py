"""Business facts render as one [BUSINESS DATA] block; the precedence rule reaches the model.

Requirements: R13.5 (business facts labelled distinctly from knowledge), R13.6 (missing
entities are explicit facts; NOT_FOUND and UNAVAILABLE stay distinct). Design: design.md §5.4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from packages.context.builder import DefaultInstructionProvider
from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
    unavailable_context,
)
from packages.domain.entities import Candidate, ContextPackage, EmailAddress, NormalizedMessage
from packages.llm.profile import (
    BUSINESS_DATA_PRECEDENCE_RULE,
    DEFAULT_ENTERPRISE_INSTRUCTIONS,
    AgentProfileRegistry,
)

REGISTRY_PATH = "config/agent_profiles.yaml"
PROFILES = {
    "technical_support": "support",
    "billing": "billing",
    "sales": "sales",
    "general_inquiry": "general",
}
OLD_HEADERS = (
    "--- Customer & System Account Context ---",
    "--- Customer Account & Billing Records ---",
    "--- CRM & Opportunity Context ---",
    "--- Business Context ---",
)
AS_OF = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


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
                attributes=(("status", "shipped"), ("placed_at", "2026-09-20")),
            ),
            BusinessFact(
                entity=EntityType.ORDER, reference="ORD-9901", status=FactStatus.NOT_FOUND
            ),
            BusinessFact(
                entity=EntityType.INVOICE,
                reference="INV-2026-8891",
                status=FactStatus.NOT_LOOKED_UP,
                reason=NotLookedUpReason.UNSUPPORTED_ENTITY.value,
            ),
        ),
    )


def _package(business: BusinessContext | None) -> ContextPackage:
    agent, category = DefaultInstructionProvider().get_instructions("general_inquiry")
    msg = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-5-5",
        sender=EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith"),
        subject="Order status",
        body_text="What is the status of order 82915?",
        body_text_clean="What is the status of order 82915?",
        received_at=AS_OF,
    )
    chunk = Candidate(
        chunk_id="chunk-orders",
        document_id="doc-fulfillment",
        content="Customers can check real-time order status online or via support inquiry.",
        external_id="KB-ORDERS-01",
    )
    return ContextPackage(
        agent_instructions=agent,
        category_instructions=category,
        current_message=msg,
        retrieved_chunks=[chunk],
        business_data=business,
    )


@pytest.mark.parametrize(("profile_name", "stem"), PROFILES.items())
def test_every_profile_uses_its_v2_template_and_version(profile_name: str, stem: str) -> None:
    profile = AgentProfileRegistry.from_yaml(REGISTRY_PATH).get_profile(profile_name)
    assert profile is not None
    assert profile.prompt_template == f"prompts/{stem}.v2.j2"
    assert profile.prompt_version == f"{stem}.v2"
    source = Path(profile.prompt_template).read_text(encoding="utf-8")
    assert "business_data.render()" in source
    assert ".items()" not in source


@pytest.mark.parametrize("profile_name", PROFILES)
def test_business_block_renders_once_after_knowledge_with_every_status(profile_name: str) -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    business = _business()
    block = business.render()

    rendered = registry.render_prompt(
        registry.get_profile(profile_name) or profile_name, _package(business)
    )

    assert rendered.count(block) == 1
    assert rendered.index("[CITATION: KB-ORDERS-01]") < rendered.index(block)
    for header in OLD_HEADERS:
        assert header not in rendered
    for text in ("ORD-82915", "shipped", "ORD-9901", "NOT_FOUND", "INV-2026-8891", "NOT_LOOKED_UP"):
        assert text in block


@pytest.mark.parametrize("profile_name", PROFILES)
def test_no_business_data_renders_no_block(profile_name: str) -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    header_line = _business().render().splitlines()[0]
    rendered = registry.render_prompt(
        registry.get_profile(profile_name) or profile_name, _package(None)
    )
    assert header_line not in rendered


def test_degraded_context_says_unavailable_never_not_found() -> None:
    plan = FetchPlan(refs=(EntityRef(entity=EntityType.ORDER, reference="ORD-82915"),))
    degraded = unavailable_context(plan, AS_OF)
    block = degraded.render()
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)

    rendered = registry.render_prompt("general_inquiry", _package(degraded))

    assert block in rendered
    assert "UNAVAILABLE" in block
    assert "NOT_FOUND" not in block


def test_precedence_rule_is_identical_on_both_instruction_paths() -> None:
    default_agent, _ = DefaultInstructionProvider().get_instructions("billing")
    registry_agent, _ = AgentProfileRegistry.from_yaml(REGISTRY_PATH).get_instructions("billing")
    assert BUSINESS_DATA_PRECEDENCE_RULE in default_agent
    assert default_agent == registry_agent == DEFAULT_ENTERPRISE_INSTRUCTIONS
    assert BUSINESS_DATA_PRECEDENCE_RULE == (
        "Order, ticket and invoice status, dates and amounts come only from [BUSINESS DATA]; "
        "if a fact is NOT_FOUND or UNAVAILABLE, say so; knowledge chunks explain procedure only."
    )


def test_rendered_prompt_carries_the_precedence_rule() -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    rendered = registry.render_prompt("billing", _package(_business()))
    assert BUSINESS_DATA_PRECEDENCE_RULE in rendered
