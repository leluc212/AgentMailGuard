"""Unit tests for triage funnel instrumentation and reconciliation (R6.10, R6.15, R21.4, NFR14)."""

from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from packages.domain.entities import Classification, Job
from packages.domain.rules import EmailContext
from packages.domain.state_machine import JobState
from packages.domain.templates import TemplateDefinition, TemplateRegistry
from packages.observability.funnel import (
    FunnelOutcome,
    RAGMode,
    compute_funnel_reconciliation,
    record_funnel_outcome,
)
from packages.observability.metrics import (
    PipelineMetrics,
    create_pipeline_metrics,
    generate_metrics_payload,
)
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.gate import EarlyExitGate, GateAction


@pytest.fixture
def isolated_metrics() -> PipelineMetrics:
    """Create an isolated Prometheus metrics container on a fresh registry."""
    reg = CollectorRegistry(auto_describe=True)
    return create_pipeline_metrics(registry=reg)


@pytest.fixture
def sample_template_registry() -> TemplateRegistry:
    """Create an in-memory template registry with sample templates."""
    reg = TemplateRegistry()
    reg.register(
        TemplateDefinition(
            id="ack_v1",
            category="acknowledgement",
            intent="receipt_confirmation",
            subject="Re: {{ subject }}",
            body="Thank you {{ sender.name }}. We received your inquiry.",
            version="v1",
        )
    )
    return reg


def test_funnel_metrics_registration(isolated_metrics: PipelineMetrics) -> None:
    """Verify all funnel accounting counters are registered on the collector registry (R21.4)."""
    m = isolated_metrics

    assert m.emails_early_exit_total is not None
    assert m.triage_funnel_outcomes_total is not None
    assert m.emails_templated_total is not None
    assert m.emails_generated_total is not None
    assert m.emails_classified_total is not None

    # Test label dimensions
    m.emails_early_exit_total.labels(
        organization="org-1", category="marketing", reason="no_reply_required"
    ).inc()
    m.triage_funnel_outcomes_total.labels(
        organization="org-1", category="marketing", outcome="early_exit", rag_mode="none"
    ).inc()
    m.emails_templated_total.labels(organization="org-1", template_id="ack_v1").inc()
    m.emails_generated_total.labels(organization="org-1", model_tier="fast").inc()


def test_early_exit_gate_metrics_emission_all_outcomes(
    isolated_metrics: PipelineMetrics, sample_template_registry: TemplateRegistry
) -> None:
    """Verify EarlyExitGate increments correct counters for each of the 4 gate actions."""
    m = isolated_metrics
    gate = EarlyExitGate(template_registry=sample_template_registry, metrics=m)
    org_id = uuid4()

    # 1. Early Exit (Outcome 1: ~45%)
    job_1 = Job(id=uuid4(), organization_id=org_id, state=JobState.NORMALIZED)
    cls_1 = Classification(
        category="marketing",
        intent="newsletter",
        priority="low",
        reply_required=False,
        workflow_hint="none",
        retrieval_required=False,
        confidence=0.99,
        decided_by="rules",
    )
    dec_1 = gate.evaluate_decision(job=job_1, classification=cls_1)
    assert dec_1.action == GateAction.EARLY_EXIT

    # 2. Template Reply (Outcome 2: ~20%)
    job_2 = Job(id=uuid4(), organization_id=org_id, state=JobState.NORMALIZED)
    cls_2 = Classification(
        category="acknowledgement",
        intent="receipt_confirmation",
        priority="normal",
        reply_required=True,
        workflow_hint="template",
        retrieval_required=False,
        confidence=0.95,
        decided_by="rules",
    )
    dec_2 = gate.evaluate_decision(job=job_2, classification=cls_2)
    assert dec_2.action == GateAction.TEMPLATE_REPLY

    # 3. AI Generation with RAG (Outcome 3a: ~70% of AI)
    job_3 = Job(id=uuid4(), organization_id=org_id, state=JobState.NORMALIZED)
    cls_3 = Classification(
        category="technical_support",
        intent="bug_report",
        priority="high",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=True,
        confidence=0.92,
        decided_by="ml",
    )
    dec_3 = gate.evaluate_decision(job=job_3, classification=cls_3)
    assert dec_3.action == GateAction.PROCEED_RAG

    # 4. AI Generation without RAG (Outcome 3b: ~30% of AI)
    job_4 = Job(id=uuid4(), organization_id=org_id, state=JobState.NORMALIZED)
    cls_4 = Classification(
        category="general_inquiry",
        intent="greeting",
        priority="low",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=False,
        confidence=0.88,
        decided_by="ml",
    )
    dec_4 = gate.evaluate_decision(job=job_4, classification=cls_4)
    assert dec_4.action == GateAction.PROCEED_NO_RAG

    # Verify reconciliation report
    report = compute_funnel_reconciliation(m, organization=org_id)
    assert report.total_triaged == 4
    assert report.early_exit_count == 1
    assert report.template_count == 1
    assert report.ai_generation_count == 2
    assert report.ai_rag_count == 1
    assert report.ai_no_rag_count == 1
    assert report.residual_count == 0
    assert report.is_reconciled is True
    assert report.rag_share_of_ai == 0.5


def test_cascading_triage_engine_classification_metrics(
    isolated_metrics: PipelineMetrics,
) -> None:
    """Verify CascadingTriageEngine records classification count and latency."""
    m = isolated_metrics
    engine = CascadingTriageEngine(metrics=m)
    org_id = uuid4()
    msg_id = uuid4()

    context = EmailContext(
        sender_email="noreply@company.com",
        sender_name="No Reply",
        headers={"auto-submitted": "auto-generated"},
        subject="Automated Alert",
        body_text="Alert payload",
    )

    result = engine.triage_sync(context=context, organization_id=org_id, message_id=msg_id)
    assert result.classification is not None

    # Check that emails_classified_total was incremented
    payload, _ = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")
    assert "emails_classified_total" in payload_str
    assert "classification_latency_ms" in payload_str


def test_funnel_reconciliation_exact_mathematical_reference(
    isolated_metrics: PipelineMetrics,
) -> None:
    """Verify mathematical reconciliation on reference 100,000 emails per design.md and NFR14.

    Reference numbers:
      100,000 emails/day:
        - 45,000 (45%) -> no reply required (early exit)
        - 20,000 (20%) -> deterministic template reply
        - 35,000 (35%) -> AI generated response
            - 24,500 (70% of AI) -> requiring external RAG
            - 10,500 (30% of AI) -> requiring no external RAG
    """
    m = isolated_metrics
    org = "enterprise-corp"

    # Simulate 45,000 Early Exits
    for _ in range(45):
        m.triage_funnel_outcomes_total.labels(
            organization=org, category="marketing", outcome="early_exit", rag_mode="none"
        ).inc(1000)

    # Simulate 20,000 Template Replies
    for _ in range(20):
        m.triage_funnel_outcomes_total.labels(
            organization=org, category="acknowledgement", outcome="template", rag_mode="none"
        ).inc(1000)

    # Simulate 24,500 AI Generation with RAG
    for _ in range(24):
        m.triage_funnel_outcomes_total.labels(
            organization=org, category="technical_support", outcome="ai_generation", rag_mode="rag"
        ).inc(1000)
    m.triage_funnel_outcomes_total.labels(
        organization=org, category="technical_support", outcome="ai_generation", rag_mode="rag"
    ).inc(500)

    # Simulate 10,500 AI Generation without RAG
    for _ in range(10):
        m.triage_funnel_outcomes_total.labels(
            organization=org, category="general_inquiry", outcome="ai_generation", rag_mode="no_rag"
        ).inc(1000)
    m.triage_funnel_outcomes_total.labels(
        organization=org, category="general_inquiry", outcome="ai_generation", rag_mode="no_rag"
    ).inc(500)

    report = compute_funnel_reconciliation(m, organization=org)

    # 1. Exact counts
    assert report.total_triaged == 100_000
    assert report.early_exit_count == 45_000
    assert report.template_count == 20_000
    assert report.ai_generation_count == 35_000
    assert report.ai_rag_count == 24_500
    assert report.ai_no_rag_count == 10_500

    # 2. Strict zero residual proof (R6.15)
    assert report.residual_count == 0
    assert report.is_reconciled is True

    # 3. Ratios against architectural targets (R6.10, NFR14)
    assert abs(report.early_exit_ratio - 0.45) < 1e-6
    assert abs(report.template_ratio - 0.20) < 1e-6
    assert abs(report.ai_generation_ratio - 0.35) < 1e-6

    # 4. RAG share of AI traffic (~70% per NFR14)
    assert abs(report.rag_share_of_ai - 0.70) < 1e-6


def test_funnel_reconciliation_multi_tenant_isolation(
    isolated_metrics: PipelineMetrics,
) -> None:
    """Verify multi-tenant isolation in funnel calculations."""
    m = isolated_metrics
    org_a = "tenant-alpha"
    org_b = "tenant-beta"

    # Seed Tenant Alpha
    record_funnel_outcome(
        m, organization=org_a, category="support", outcome=FunnelOutcome.EARLY_EXIT
    )
    record_funnel_outcome(m, organization=org_a, category="support", outcome=FunnelOutcome.TEMPLATE)

    # Seed Tenant Beta
    record_funnel_outcome(
        m,
        organization=org_b,
        category="billing",
        outcome=FunnelOutcome.AI_GENERATION,
        rag_mode=RAGMode.RAG,
    )

    report_a = compute_funnel_reconciliation(m, organization=org_a)
    assert report_a.total_triaged == 2
    assert report_a.early_exit_count == 1
    assert report_a.template_count == 1
    assert report_a.ai_generation_count == 0

    report_b = compute_funnel_reconciliation(m, organization=org_b)
    assert report_b.total_triaged == 1
    assert report_b.early_exit_count == 0
    assert report_b.ai_generation_count == 1
    assert report_b.ai_rag_count == 1

    report_all = compute_funnel_reconciliation(m, organization=None)
    assert report_all.total_triaged == 3
    assert report_all.residual_count == 0
    assert report_all.is_reconciled is True


def test_funnel_reconciliation_zero_emails_edge_case(
    isolated_metrics: PipelineMetrics,
) -> None:
    """Verify zero traffic edge case gracefully returns zero counts without division by zero."""
    m = isolated_metrics
    report = compute_funnel_reconciliation(m)

    assert report.total_triaged == 0
    assert report.early_exit_ratio == 0.0
    assert report.template_ratio == 0.0
    assert report.ai_generation_ratio == 0.0
    assert report.rag_share_of_ai == 0.0
    assert report.residual_count == 0
    assert report.is_reconciled is True


def test_emails_templated_total_exported_alongside_emails_generated_total(
    isolated_metrics: PipelineMetrics,
) -> None:
    """Verify emails_templated_total exported alongside emails_generated_total (R21.4)."""
    m = isolated_metrics
    org = "enterprise-corp"

    m.emails_templated_total.labels(organization=org, template_id="ack_v1").inc(12)
    m.emails_generated_total.labels(organization=org, model_tier="strong").inc(8)
    m.emails_early_exit_total.labels(
        organization=org, category="newsletter", reason="no_reply"
    ).inc(20)

    payload_bytes, content_type = generate_metrics_payload(m.registry)
    payload_str = payload_bytes.decode("utf-8")

    assert "emails_templated_total" in payload_str
    assert "emails_generated_total" in payload_str
    assert "emails_early_exit_total" in payload_str
    assert "triage_funnel_outcomes_total" in payload_str
    assert 'template_id="ack_v1"' in payload_str
    assert 'model_tier="strong"' in payload_str
