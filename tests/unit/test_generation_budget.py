"""Unit tests for full budget and single-pass generation test suite.

Requirements: R14.3, R14.4, R14.9, R14.10, R15.5.

Verifies and proves:
- R14.3: Normal email processed with approximately one retrieval and one generation operation.
- R14.4: No chained planner/critic/writer multi-agent pipeline in the default path.
- R14.9: Bound total model invocations per email job to: <=1 triage-LLM call,
         <=1 thread-summarization call, exactly 1 generation call, and <=1 schema-repair retry.
         Ceiling is 4, common case is 1.
- R14.10: Export llm_calls_per_job by call kind (triage, summarize, generate, repair, total)
          and llm_calls_total{kind, model}.
- R15.5: Tier escalation replaces the generation call; never adds one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry

from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.core.settings import SummarizationSettings
from packages.domain.entities import (
    Classification,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import JobState
from packages.llm import (
    AgentProfileRegistry,
    BudgetedLLMProvider,
    CallBudgetExceededError,
    CallBudgetTracker,
    CallKind,
    ChatMessage,
    FakeLLMProvider,
    GenerationResult,
    ModelTier,
    SinglePassGenerator,
)
from packages.observability.metrics import create_pipeline_metrics, generate_metrics_payload
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.query_builder import RetrievalQueryBuilder
from packages.retrieval.retriever import HybridRetriever


def _create_test_message(
    org_id: UUID,
    thread_id: UUID,
    msg_id: UUID | None = None,
    subject: str = "Invoice question",
    body: str = "What are the payment terms for INV-2026-001?",
    received_at: datetime | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=msg_id or uuid4(),
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=thread_id,
        provider="mock",
        provider_message_id=f"prov-{uuid4()}",
        sender=EmailAddress(email="customer@example.com", name="Alice Customer"),
        recipients=[EmailAddress(email="billing@company.com", name="Company Billing")],
        subject=subject,
        subject_normalized=subject,
        body_text=body,
        body_text_clean=body,
        received_at=received_at or datetime.now(UTC),
    )


async def _build_real_context(
    org_id: UUID,
    thread_id: UUID,
    category: str = "billing",
    intent: str = "invoice_terms",
    body: str = "What are the payment terms for INV-2026-001?",
    chunk_content: str = "Payment terms for invoices are strictly Net-30 from issuance date.",
    chunk_id: str = "chunk-billing-01",
) -> tuple[ContextBuilder, ContextPackage, NormalizedMessage]:
    now = datetime.now(UTC)
    prev_msg = _create_test_message(
        org_id,
        thread_id,
        subject="Earlier question",
        body="Hi, I received my monthly invoice yesterday.",
        received_at=now - timedelta(minutes=10),
    )
    curr_msg = _create_test_message(
        org_id,
        thread_id,
        subject="Invoice question",
        body=body,
        received_at=now,
    )

    backend = FakeSearchBackend()
    backend.add_chunk(
        chunk_id=chunk_id,
        document_id="doc-billing-terms",
        organization_id=str(org_id),
        content=chunk_content,
        category=category,
    )
    retriever = HybridRetriever(backend=backend)
    assembler = ThreadContextAssembler(settings=SummarizationSettings(keep_latest_messages=2))
    builder = ContextBuilder(
        thread_assembler=assembler,
        retriever=retriever,
        query_builder=RetrievalQueryBuilder(),
    )

    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=curr_msg.message_id,
        state=JobState.QUEUED,
    )
    classification = Classification(
        category=category,
        intent=intent,
        retrieval_required=True,
    )

    pkg = await builder.build_context(
        job=job,
        message=curr_msg,
        classification=classification,
        thread_messages=[prev_msg, curr_msg],
    )
    return builder, pkg, curr_msg


def _validate_reply_v1(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate that a generated reply dictionary strictly conforms to schemas/reply.v1.json."""
    required_keys = schema.get("required", ["action", "draft", "confidence", "knowledge_chunks"])
    for key in required_keys:
        assert key in data, f"Missing required schema field {key!r} in draft content"

    action_enum = schema["properties"]["action"]["enum"]
    assert data["action"] in action_enum, (
        f"Invalid action {data['action']!r}, expected one of {action_enum}"
    )

    assert isinstance(data["draft"], str) and len(data["draft"]) > 0, (
        "draft must be non-empty string"
    )
    assert isinstance(data["confidence"], (int, float)), "confidence must be numeric"
    assert 0.0 <= float(data["confidence"]) <= 1.0, "confidence must be between 0.0 and 1.0"

    assert isinstance(data["knowledge_chunks"], list), "knowledge_chunks must be a list"
    for chunk in data["knowledge_chunks"]:
        assert isinstance(chunk, str), f"knowledge chunk ID {chunk!r} must be string"

    if "thread_summary_updated" in data:
        assert isinstance(data["thread_summary_updated"], bool), (
            "thread_summary_updated must be bool"
        )

    if "model_tier" in data:
        assert isinstance(data["model_tier"], str), "model_tier must be str"

    if schema.get("additionalProperties") is False:
        allowed_properties = set(schema["properties"].keys())
        for k in data:
            assert k in allowed_properties, (
                f"Unexpected extra property {k!r} violates additionalProperties=false"
            )


@pytest.fixture
def profile_registry() -> AgentProfileRegistry:
    return AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")


@pytest.fixture
def canned_reply() -> dict[str, Any]:
    return {
        "action": "reply",
        "draft": "Hello Alice, payment terms for invoice INV-2026-001 are strictly Net-30.",
        "confidence": 0.95,
        "knowledge_chunks": ["chunk-billing-01"],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }


# ============================================================================
# Test 1: Full pipeline lifecycle simulation (R14.9, §5.7)
# ============================================================================


@pytest.mark.asyncio
async def test_full_pipeline_lifecycle_simulation(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Simulate full job lifecycle up to 4 calls: triage, summarize, generate, repair (R14.9)."""
    tracker = CallBudgetTracker(job_id="job-lifecycle-001")
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    budgeted_provider = BudgetedLLMProvider(provider=fake_llm, tracker=tracker)

    # 1. Simulate Stage 3 triage LLM fallback
    triage_messages = [
        ChatMessage(role="user", content="Classify category and intent for unconfident email")
    ]
    triage_result = await budgeted_provider.generate(
        messages=triage_messages,
        tier=ModelTier.FAST,
        call_kind=CallKind.TRIAGE,
    )
    assert triage_result is not None
    assert tracker.count(CallKind.TRIAGE) == 1
    assert tracker.total_calls == 1

    # 2. Simulate thread summarization on long conversation threshold trigger
    summarize_messages = [
        ChatMessage(role="user", content="Summarize the earlier 5 messages in this thread")
    ]
    summarize_result = await budgeted_provider.generate(
        messages=summarize_messages,
        tier=ModelTier.FAST,
        call_kind=CallKind.SUMMARIZE,
    )
    assert summarize_result is not None
    assert tracker.count(CallKind.SUMMARIZE) == 1
    assert tracker.total_calls == 2

    # 3. Build real ContextPackage from ContextBuilder (R14.8, R6.6)
    org_id = uuid4()
    thread_id = uuid4()
    _, context, _ = await _build_real_context(
        org_id=org_id,
        thread_id=thread_id,
        category="billing",
        chunk_id="chunk-billing-01",
    )

    # 4. Execute single-pass generation via SinglePassGenerator
    generator = SinglePassGenerator(
        llm_provider=budgeted_provider,
        profile_registry=profile_registry,
    )
    gen_result = await generator.generate_draft(
        context=context,
        category="billing",
        budget_tracker=tracker,
    )
    assert isinstance(gen_result, GenerationResult)
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 3

    # 5. Simulate schema-repair retry call
    repair_messages = [
        ChatMessage(role="user", content="Repair malformed JSON output to match reply.v1.json")
    ]
    repair_result = await budgeted_provider.generate(
        messages=repair_messages,
        tier=ModelTier.ROUTINE,
        call_kind=CallKind.REPAIR,
    )
    assert repair_result is not None
    assert tracker.count(CallKind.REPAIR) == 1
    assert tracker.total_calls == 4

    # Assert ceiling respected, counts match limits, and assert_generation_budget passes
    assert tracker.total_calls == CallBudgetTracker.BUDGET_CEILING
    assert tracker.get_counts() == {
        "triage": 1,
        "summarize": 1,
        "generate": 1,
        "repair": 1,
        "total": 4,
    }
    tracker.assert_generation_budget(require_generation=True)

    # Any 5th call is strictly rejected
    with pytest.raises(CallBudgetExceededError):
        await budgeted_provider.generate(
            messages=[ChatMessage(role="user", content="Disallowed 5th call")],
            call_kind=CallKind.GENERATE,
        )


# ============================================================================
# Test 2: Invariant enforcement — attempting 2nd generation call (R14.9)
# ============================================================================


@pytest.mark.asyncio
async def test_second_generation_call_rejected(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify attempting a second generation call on same tracker raises CallBudgetExceededError."""
    tracker = CallBudgetTracker(job_id="job-attempt-second-gen")
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    org_id = uuid4()
    thread_id = uuid4()
    _, context, _ = await _build_real_context(
        org_id=org_id, thread_id=thread_id, category="support"
    )

    # First generation call succeeds
    result1 = await generator.generate_draft(
        context=context,
        category="support",
        budget_tracker=tracker,
    )
    assert result1 is not None
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1

    # Second generation call must raise CallBudgetExceededError before LLM invocation
    with pytest.raises(CallBudgetExceededError) as exc_info:
        await generator.generate_draft(
            context=context,
            category="support",
            budget_tracker=tracker,
        )

    assert "generate" in str(exc_info.value).lower()
    # Ensure provider was invoked exactly once; second call was stopped by budget gate
    assert len(fake_llm.recorded_calls) == 1
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1


# ============================================================================
# Test 3: Invariant enforcement — attempting 5th call (R14.9)
# ============================================================================


@pytest.mark.asyncio
async def test_fifth_call_ceiling_rejected(canned_reply: dict[str, Any]) -> None:
    """Verify budget ceiling prevents any 5th model invocation across all kinds."""
    tracker = CallBudgetTracker(job_id="job-ceiling-exceeded")
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    budgeted = BudgetedLLMProvider(provider=fake_llm, tracker=tracker)

    # Exhaust all 4 budget slots: 1 triage + 1 summarize + 1 generate + 1 repair
    await budgeted.generate(
        messages=[ChatMessage(role="user", content="1")], call_kind=CallKind.TRIAGE
    )
    await budgeted.generate(
        messages=[ChatMessage(role="user", content="2")], call_kind=CallKind.SUMMARIZE
    )
    await budgeted.generate(
        messages=[ChatMessage(role="user", content="3")], call_kind=CallKind.GENERATE
    )
    await budgeted.generate(
        messages=[ChatMessage(role="user", content="4")], call_kind=CallKind.REPAIR
    )

    assert tracker.total_calls == 4
    tracker.assert_generation_budget(require_generation=True)

    # Attempting any 5th call raises CallBudgetExceededError
    for kind in CallKind:
        with pytest.raises(CallBudgetExceededError) as exc_info:
            tracker.check_can_call(kind)
        assert "ceiling" in str(exc_info.value).lower() or "budget" in str(exc_info.value).lower()

        with pytest.raises(CallBudgetExceededError):
            await budgeted.generate(
                messages=[ChatMessage(role="user", content="5th call")],
                call_kind=kind,
            )

    # Inner provider was not called past the 4 allowed
    assert len(fake_llm.recorded_calls) == 4
    assert tracker.total_calls == 4


# ============================================================================
# Test 4: Common case path (R14.3, R14.4, R14.9)
# ============================================================================


@pytest.mark.asyncio
async def test_common_case_single_call_path(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify common case executes exactly 1 generation call and passes cleanly (R14.3, R14.4)."""
    tracker = CallBudgetTracker(job_id="job-common-case-001")
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    org_id = uuid4()
    thread_id = uuid4()
    _, context, _ = await _build_real_context(
        org_id=org_id, thread_id=thread_id, category="billing"
    )

    # Execute common case: 0 triage + 0 summarize + 1 generate + 0 repair = 1 call
    result = await generator.generate_draft(
        context=context,
        category="billing",
        budget_tracker=tracker,
    )

    assert result is not None
    assert tracker.count(CallKind.TRIAGE) == 0
    assert tracker.count(CallKind.SUMMARIZE) == 0
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.count(CallKind.REPAIR) == 0
    assert tracker.total_calls == 1

    # Assert invariant passes cleanly without raising
    tracker.assert_generation_budget(require_generation=True)

    # Exactly 1 call was received by the provider
    assert len(fake_llm.recorded_calls) == 1
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.ROUTINE


# ============================================================================
# Test 5: Tier escalation replacement (R15.5)
# ============================================================================


@pytest.mark.asyncio
async def test_tier_escalation_replaces_generation_call(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify tier escalation replaces the generation call rather than adding one (R15.5)."""
    tracker = CallBudgetTracker(job_id="job-escalation-001")
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    org_id = uuid4()
    thread_id = uuid4()
    _, context, _ = await _build_real_context(
        org_id=org_id, thread_id=thread_id, category="support"
    )

    result = await generator.generate_draft(
        context=context,
        category="support",
        budget_tracker=tracker,
        escalated_tier=ModelTier.HIGH_CAPABILITY,
        escalation_reason="low_classification_confidence",
    )

    # Escalated tier is reflected in result and call parameters
    assert result.tier == ModelTier.HIGH_CAPABILITY
    assert result.escalation_reason == "low_classification_confidence"
    assert len(fake_llm.recorded_calls) == 1
    assert fake_llm.recorded_calls[0]["tier"] == ModelTier.HIGH_CAPABILITY

    # Exactly 1 generation call recorded; total calls == 1 (never added a call)
    assert tracker.count(CallKind.GENERATE) == 1
    assert tracker.total_calls == 1
    tracker.assert_generation_budget(require_generation=True)


# ============================================================================
# Test 6: Metrics validation (R14.10)
# ============================================================================


@pytest.mark.asyncio
async def test_metrics_validation_all_kinds(
    profile_registry: AgentProfileRegistry,
    canned_reply: dict[str, Any],
) -> None:
    """Verify llm_calls_total and llm_calls_per_job export all call kinds and labels (R14.10)."""
    reg = CollectorRegistry(auto_describe=True)
    metrics = create_pipeline_metrics(registry=reg)

    tracker = CallBudgetTracker(job_id="job-metrics-validation", metrics=metrics)
    fake_llm = FakeLLMProvider(default_response=canned_reply)
    budgeted = BudgetedLLMProvider(
        provider=fake_llm,
        tracker=tracker,
        metrics=metrics,
    )
    generator = SinglePassGenerator(
        llm_provider=budgeted,
        profile_registry=profile_registry,
        metrics=metrics,
    )

    org_id = uuid4()
    thread_id = uuid4()
    _, context, _ = await _build_real_context(
        org_id=org_id, thread_id=thread_id, category="support"
    )

    # 1. Triage call
    await budgeted.generate(
        messages=[ChatMessage(role="user", content="Triage")],
        call_kind=CallKind.TRIAGE,
    )
    # 2. Summarize call
    await budgeted.generate(
        messages=[ChatMessage(role="user", content="Summarize")],
        call_kind=CallKind.SUMMARIZE,
    )
    # 3. Generate call
    gen_result = await generator.generate_draft(
        context=context,
        category="support",
        budget_tracker=tracker,
    )
    # 4. Repair call
    await budgeted.generate(
        messages=[ChatMessage(role="user", content="Repair")],
        call_kind=CallKind.REPAIR,
    )

    # Export tracker metrics to histogram
    tracker.export_metrics(metrics)

    # Verify llm_calls_total counter increments for all kinds
    assert metrics.llm_calls_total.labels(kind="triage", model=gen_result.model)._value.get() == 1.0
    assert (
        metrics.llm_calls_total.labels(kind="summarize", model=gen_result.model)._value.get() == 1.0
    )
    assert (
        metrics.llm_calls_total.labels(kind="generate", model=gen_result.model)._value.get() == 1.0
    )
    assert metrics.llm_calls_total.labels(kind="repair", model=gen_result.model)._value.get() == 1.0

    # Verify llm_calls_per_job histogram observes all kinds and total
    observed_counts = {
        s.labels["kind"]: s.value
        for s in list(metrics.llm_calls_per_job.collect())[0].samples
        if s.name == "llm_calls_per_job_count"
    }
    for kind_label in ["triage", "summarize", "generate", "repair", "total"]:
        assert observed_counts.get(kind_label, 0.0) >= 1.0, (
            f"Expected observation for {kind_label!r}"
        )

    # Verify Prometheus output payload formatting
    payload, _ = generate_metrics_payload(reg)
    payload_str = payload.decode("utf-8")
    assert 'llm_calls_total{kind="triage"' in payload_str
    assert 'llm_calls_total{kind="generate"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="generate"' in payload_str
    assert 'llm_calls_per_job_bucket{kind="total"' in payload_str


# ============================================================================
# Test 7: Integration with ContextBuilder (R14.3, R14.8, reply.v1.json)
# ============================================================================


@pytest.mark.asyncio
async def test_integration_context_builder_to_single_pass_generator(
    profile_registry: AgentProfileRegistry,
) -> None:
    """Verify end-to-end integration: ContextBuilder -> SinglePassGenerator -> validated schema."""
    org_id = uuid4()
    thread_id = uuid4()
    chunk_id = "chunk-kb-billing-terms-99"
    builder, context, curr_msg = await _build_real_context(
        org_id=org_id,
        thread_id=thread_id,
        category="billing",
        intent="invoice_inquiry",
        body="Please confirm the payment terms for invoice INV-9900.",
        chunk_content="Invoice payment terms are Net-30 from receipt.",
        chunk_id=chunk_id,
    )

    # 1. Assert ContextPackage was assembled with real chunks and ordering
    assert isinstance(context, ContextPackage)
    assert len(context.retrieved_chunks) >= 1
    assert context.retrieved_chunks[0].chunk_id == chunk_id
    assert context.current_message.message_id == curr_msg.message_id

    # 2. Setup mock LLM with valid reply conforming to schemas/reply.v1.json
    expected_reply = {
        "action": "reply",
        "draft": "Hello, invoice INV-9900 payment terms are strictly Net-30 from receipt.",
        "confidence": 0.98,
        "knowledge_chunks": [chunk_id],
        "thread_summary_updated": False,
        "model_tier": "routine",
    }
    fake_llm = FakeLLMProvider(default_response=expected_reply)
    generator = SinglePassGenerator(
        llm_provider=fake_llm,
        profile_registry=profile_registry,
    )

    # 3. Execute generation
    tracker = CallBudgetTracker(job_id="job-cb-integration")
    result = await generator.generate_draft(
        context=context,
        category="billing",
        budget_tracker=tracker,
    )

    # 4. Verify GenerationResult attributes
    assert isinstance(result, GenerationResult)
    assert result.profile.profile == "billing"
    assert result.prompt_version == "billing.v1"
    assert result.tier == ModelTier.ROUTINE
    assert result.budget_tracker.count(CallKind.GENERATE) == 1
    assert result.budget_tracker.total_calls == 1

    # 5. Validate schema conformity against schemas/reply.v1.json
    schema = profile_registry.get_schema(result.profile)
    _validate_reply_v1(result.content, schema)
    assert result.content["action"] == "reply"
    assert result.content["knowledge_chunks"] == [chunk_id]
    assert result.content["confidence"] == 0.98
