"""Context Builder reranks the fused candidates before it cuts to top-K (R11.1-R11.5; task 7.20).

The builder hands every fused candidate to the RerankService, keeps the top-K of the reranked
order, and reports on the ContextPackage whether the cross-encoder ordered them
(``rerank_applied``) with each chunk's ``rerank_score``. When the reranker is off, unavailable or
too slow the RRF order stays, the fallback is recorded, and the job carries on. The last section
follows the package into the complexity router, which reads those scores (R15.3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.db.job import InMemoryJobStore
from packages.domain.entities import (
    Classification,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import JobState
from packages.llm import ComplexityRouter, EscalationReason, RoutingDecision
from packages.llm.protocol import ModelTier
from packages.observability.metrics import create_pipeline_metrics, generate_metrics_payload
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.rerank import (
    CrossEncoderReranker,
    RerankPolicy,
    RerankService,
    StubReranker,
)
from packages.retrieval.retriever import HybridRetriever, RetrievalResult


def _message(org_id: UUID, thread_id: UUID) -> NormalizedMessage:
    body = "How do I get a refund for invoice INV-2026-001?"
    return NormalizedMessage(
        message_id=uuid4(),
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=thread_id,
        provider="mock",
        provider_message_id=f"prov-{uuid4()}",
        sender=EmailAddress(email="customer@example.com"),
        recipients=[EmailAddress(email="support@company.com")],
        subject="Refund request",
        body_text=body,
        body_text_clean=body,
        received_at=datetime.now(UTC),
    )


def _fused(count: int) -> list[Candidate]:
    """RRF order: c0 first. The debug value in the content tells the chunks apart."""
    return [
        Candidate(
            chunk_id=f"c{i}",
            document_id=f"doc-{i}",
            content=f"knowledge chunk {i}",
            metadata={"external_id": f"KB-{i}"},
            vector_rank=i + 1,
            vector_score=0.9 - i / 100,
            fused_score=1 / (60 + i),
        )
        for i in range(count)
    ]


class _FusedRetriever:
    """Returns a fixed fused list and remembers the query it was asked."""

    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.queries: list[RetrievalQuery] = []

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        self.queries.append(query)
        return RetrievalResult(candidates=list(self.candidates))


def _build(
    builder: ContextBuilder,
    org_id: UUID,
    *,
    category: str = "billing",
    retrieval_required: bool = True,
    job_store: InMemoryJobStore | None = None,
) -> tuple[ContextPackage, Job]:
    thread_id = uuid4()
    message = _message(org_id, thread_id)
    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=message.message_id,
        state=JobState.QUEUED,
    )
    if job_store is not None:
        asyncio.run(job_store.create_job(job))
    classification = Classification(
        category=category, intent="refund", retrieval_required=retrieval_required
    )
    pkg = asyncio.run(
        builder.build_context(
            job=job, message=message, classification=classification, thread_messages=[message]
        )
    )
    return pkg, job


def _ids(pkg: ContextPackage) -> list[str]:
    return [chunk.chunk_id for chunk in pkg.retrieved_chunks]


SEVEN_SCORES = {"c0": 0.1, "c1": 0.2, "c2": 0.3, "c3": 0.8, "c4": 0.4, "c5": 0.5, "c6": 0.9}


def test_the_whole_fused_pool_is_reranked_then_cut_to_top_k() -> None:
    """c6 is RRF-seventh: reranking only the RRF top-K would never surface it (R11.1, R11.3)."""
    org_id = uuid4()
    retriever = _FusedRetriever(_fused(7))
    reranker = StubReranker(score_map=SEVEN_SCORES)
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=retriever,  # type: ignore[arg-type]
        top_k=3,
        rerank_service=RerankService(reranker),
    )

    pkg, _ = _build(builder, org_id)

    assert _ids(pkg) == ["c6", "c3", "c5"]
    assert [c.rerank_score for c in pkg.retrieved_chunks] == [0.9, 0.8, 0.5]
    assert pkg.rerank_applied is True
    assert reranker.calls == [(retriever.queries[0].semantic_text, 7)]


def test_reranked_chunks_keep_everything_retrieval_knew_about_them() -> None:
    org_id = uuid4()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(2)),  # type: ignore[arg-type]
        top_k=2,
        rerank_service=RerankService(StubReranker(score_map={"c0": 0.1, "c1": 0.9})),
    )

    pkg, _ = _build(builder, org_id)

    first = pkg.retrieved_chunks[0]
    assert (first.chunk_id, first.document_id, first.content) == (
        "c1",
        "doc-1",
        "knowledge chunk 1",
    )
    assert first.external_id == "KB-1"
    assert (first.vector_rank, first.vector_score) == (2, 0.89)
    assert first.fused_score == pytest.approx(1 / 61)
    assert first.rerank_score == 0.9


def test_top_k_holds_even_when_a_reranker_ignores_it() -> None:
    """R11.3: the cut is the builder's own, not something every reranker must honour."""

    class ReturnsEverything:
        async def rerank(
            self, query: str, candidates: Sequence[Candidate], top_k: int | None = None
        ) -> list[Candidate]:
            return [replace(c, rerank_score=1.0 - i / 100) for i, c in enumerate(candidates)]

    org_id = uuid4()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(7)),  # type: ignore[arg-type]
        top_k=3,
        rerank_service=RerankService(ReturnsEverything()),
    )

    pkg, _ = _build(builder, org_id)

    assert _ids(pkg) == ["c0", "c1", "c2"]
    assert pkg.rerank_applied is True


def test_the_reranker_scores_the_retrieval_queries_semantic_text() -> None:
    org_id = uuid4()
    retriever = _FusedRetriever(_fused(3))
    reranker = StubReranker()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=retriever,  # type: ignore[arg-type]
        rerank_service=RerankService(reranker),
    )

    _build(builder, org_id)

    semantic_text = retriever.queries[0].semantic_text
    assert "Refund request" in semantic_text and "INV-2026-001" in semantic_text
    assert [query for query, _ in reranker.calls] == [semantic_text]


@pytest.mark.parametrize("disabled", ["category", "organization"])
def test_policy_sees_the_organization_and_the_category(disabled: str) -> None:
    """R11.2: rerank can be switched off for one category or one organization."""
    org_id = uuid4()
    policy = (
        RerankPolicy(disabled_categories={"billing"})
        if disabled == "category"
        else RerankPolicy(disabled_organizations={str(org_id)})
    )
    reranker = StubReranker(score_map=SEVEN_SCORES)
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(7)),  # type: ignore[arg-type]
        top_k=3,
        rerank_service=RerankService(reranker, policy=policy),
    )

    named, _ = _build(builder, org_id, category="billing")
    unnamed, _ = _build(builder, uuid4(), category="support")

    assert _ids(named) == ["c0", "c1", "c2"] and named.rerank_applied is False
    assert all(c.rerank_score is None for c in named.retrieved_chunks)
    # The same builder still reranks everyone the policy does not name.
    assert _ids(unnamed) == ["c6", "c3", "c5"] and unnamed.rerank_applied is True
    assert len(reranker.calls) == 1


def test_an_unavailable_reranker_keeps_rrf_order_records_the_fallback_and_the_job_goes_on() -> None:
    """R11.5."""
    org_id = uuid4()
    metrics = create_pipeline_metrics()
    job_store = InMemoryJobStore()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(7)),  # type: ignore[arg-type]
        job_store=job_store,
        top_k=3,
        rerank_service=RerankService(StubReranker(is_available=False), metrics=metrics),
    )

    pkg, job = _build(builder, org_id, job_store=job_store)

    assert _ids(pkg) == ["c0", "c1", "c2"]
    assert pkg.rerank_applied is False
    assert all(c.rerank_score is None for c in pkg.retrieved_chunks)
    assert job.state == JobState.CONTEXT_READY
    payload, _ = generate_metrics_payload(metrics.registry)
    assert f'rerank_fallback_total{{reason="unavailable",tenant="{org_id}"}}' in payload.decode()


def test_a_reranker_over_its_budget_keeps_rrf_order() -> None:
    """R11.5: a timeout is a fallback, not a failed job."""
    org_id = uuid4()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(7)),  # type: ignore[arg-type]
        top_k=3,
        rerank_service=RerankService(StubReranker(delay_seconds=0.3), timeout_seconds=0.05),
    )

    pkg, _ = _build(builder, org_id)

    assert _ids(pkg) == ["c0", "c1", "c2"]
    assert pkg.rerank_applied is False


def test_a_crashing_reranker_keeps_rrf_order() -> None:
    org_id = uuid4()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(7)),  # type: ignore[arg-type]
        top_k=3,
        rerank_service=RerankService(StubReranker(should_raise=RuntimeError("out of memory"))),
    )

    pkg, _ = _build(builder, org_id)

    assert _ids(pkg) == ["c0", "c1", "c2"]
    assert pkg.rerank_applied is False


def test_without_a_rerank_service_the_builder_cuts_the_rrf_order() -> None:
    """RETRIEVAL__RERANK_ENABLED=false builds no service: the pre-rerank behaviour, unchanged."""
    org_id = uuid4()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(7)),  # type: ignore[arg-type]
        top_k=3,
    )

    pkg, _ = _build(builder, org_id)

    assert _ids(pkg) == ["c0", "c1", "c2"]
    assert pkg.rerank_applied is False
    assert all(c.rerank_score is None for c in pkg.retrieved_chunks)


def test_no_retrieval_means_no_rerank_and_an_unknown_outcome() -> None:
    """R6.6: retrieval_required=false is zero retrieval work, so nothing to rerank."""
    org_id = uuid4()
    reranker = StubReranker()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(7)),  # type: ignore[arg-type]
        rerank_service=RerankService(reranker),
    )

    pkg, _ = _build(builder, org_id, retrieval_required=False)

    assert pkg.retrieved_chunks == []
    assert pkg.rerank_applied is None
    assert reranker.calls == []


def test_no_retriever_means_no_rerank_and_an_unknown_outcome() -> None:
    org_id = uuid4()
    reranker = StubReranker()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(), rerank_service=RerankService(reranker)
    )

    pkg, _ = _build(builder, org_id)

    assert pkg.rerank_applied is None
    assert reranker.calls == []


def test_a_retrieval_with_no_candidates_reranks_nothing() -> None:
    org_id = uuid4()
    reranker = StubReranker()
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever([]),  # type: ignore[arg-type]
        rerank_service=RerankService(reranker),
    )

    pkg, _ = _build(builder, org_id)

    assert pkg.retrieved_chunks == []
    assert pkg.rerank_applied is False
    assert reranker.calls == []


def test_reranking_the_real_hybrid_retriever_output() -> None:
    """The chain as the ai-worker runs it: FakeSearchBackend -> HybridRetriever -> rerank."""
    org_id = uuid4()
    backend = FakeSearchBackend()
    for chunk_id, content in [
        ("cA", "Refund policy: refunds for invoice INV-2026-001 take 5 days."),
        ("cB", "Refund of invoice INV-2026-001 needs a signed request."),
        ("cC", "Refund requests for invoice INV-2026-001 are approved by finance."),
    ]:
        backend.add_chunk(
            chunk_id=chunk_id,
            document_id=f"doc-{chunk_id}",
            organization_id=str(org_id),
            content=content,
            category="billing",
        )
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=HybridRetriever(backend=backend),
        top_k=2,
        rerank_service=RerankService(StubReranker(score_map={"cA": 0.2, "cB": 0.4, "cC": 0.95})),
    )

    pkg, _ = _build(builder, org_id)

    assert _ids(pkg) == ["cC", "cB"]
    assert [c.rerank_score for c in pkg.retrieved_chunks] == [0.95, 0.4]
    assert pkg.rerank_applied is True


def test_rerank_applied_defaults_to_unknown_and_is_not_prompt_text() -> None:
    """The flag is diagnostics: it must never reach the prompt (R14.8)."""
    org_id = uuid4()
    base = ContextPackage(
        agent_instructions="You are an assistant.",
        category_instructions="Category: billing.",
        current_message=_message(org_id, uuid4()),
        retrieved_chunks=[],
    )

    assert base.rerank_applied is None
    assert replace(base, rerank_applied=True).get_ordered_sections() == base.get_ordered_sections()


# --- What the complexity router makes of the scores (R11.1, R11.5, R15.3) ---


class _RawLogits:
    """A cross-encoder that answers with each chunk's raw ms-marco logit (identity activation)."""

    def __init__(self, logits: Sequence[float]) -> None:
        self.logits = {f"knowledge chunk {i}": logit for i, logit in enumerate(logits)}

    def predict(
        self, pairs: list[tuple[str, str]], activation_fn: Callable[[object], object] | None = None
    ) -> list[float]:
        return [self.logits[passage] for _, passage in pairs]


def _cross_encoder_service(logits: Sequence[float]) -> RerankService:
    """The production reranker and service, with a fake model: nothing loads torch."""
    reranker = CrossEncoderReranker("org/model")
    reranker._model = _RawLogits(logits)
    return RerankService(reranker)


def _routed(builder: ContextBuilder) -> tuple[ContextPackage, RoutingDecision]:
    """Build the context as the ai-worker does, then route it with the default cascade."""
    pkg, _ = _build(builder, uuid4())
    classification = Classification(category="billing", intent="refund", retrieval_required=True)
    return pkg, ComplexityRouter().route(pkg, classification=classification)


def _reranking_builder(logits: Sequence[float], *, top_k: int = 3) -> ContextBuilder:
    return ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(len(logits))),  # type: ignore[arg-type]
        top_k=top_k,
        rerank_service=_cross_encoder_service(logits),
    )


def test_reranked_chunks_reach_the_router_on_the_probability_scale() -> None:
    """The router's bar is 0..1, so the score it is compared with has to be on that scale."""
    pkg, decision = _routed(_reranking_builder([1.2, 8.6, 5.5, -4.3]))

    assert pkg.rerank_applied is True
    assert _ids(pkg) == ["c1", "c2", "c0"]
    scores = [c.rerank_score for c in pkg.retrieved_chunks]
    assert all(score is not None and 0.0 <= score <= 1.0 for score in scores)
    assert decision.tier is ModelTier.ROUTINE
    assert decision.escalation_reason is EscalationReason.NONE


def test_a_logit_between_zero_and_the_bar_is_relevant_as_a_probability() -> None:
    """Logits 0.3 and 0.2 are 57 % and 55 % likely relevant: over the 0.50 bar as probabilities."""
    pkg, decision = _routed(_reranking_builder([0.3, 0.2], top_k=2))

    assert pkg.rerank_applied is True
    assert decision.tier is ModelTier.ROUTINE
    assert decision.escalation_reason is EscalationReason.NONE


def test_reranked_chunks_the_model_finds_irrelevant_still_escalate() -> None:
    pkg, decision = _routed(_reranking_builder([-4.3, -6.0, -7.7]))

    assert pkg.rerank_applied is True
    assert decision.tier is ModelTier.HIGH_CAPABILITY
    assert decision.escalation_reason is EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE
    assert decision.details["qualifying_chunks"] == 0


def test_after_a_fallback_the_router_compares_rrf_scores_with_the_relevance_bar() -> None:
    """Known gap, owner decision open (task 7.21): a fallback escalates the same job.

    The chunks of a fallback carry only RRF scores, at most 2/61 (about 0.03), and trigger 3 of
    the router compares the best score a chunk has with ROUTER_MIN_RELEVANCE_SCORE (0.50). So
    the pool that stays on the routine tier when the cross-encoder answers in time goes to
    high_capability when it does not. This pins today's behaviour so the gap is visible; when
    7.21 decides what a fallback should do, change this test with it.
    """
    logits = [8.6, 5.5, 1.2]
    reranked, reranked_decision = _routed(_reranking_builder(logits))
    fallback_builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        retriever=_FusedRetriever(_fused(len(logits))),  # type: ignore[arg-type]
        top_k=3,
        rerank_service=RerankService(StubReranker(is_available=False)),
    )

    fallback, fallback_decision = _routed(fallback_builder)

    assert reranked.rerank_applied is True and fallback.rerank_applied is False
    assert _ids(fallback) == ["c0", "c1", "c2"], "the same pool, in RRF order"
    assert all(c.rerank_score is None for c in fallback.retrieved_chunks)
    assert all(0 < (c.fused_score or 0) < 0.05 for c in fallback.retrieved_chunks)
    assert reranked_decision.tier is ModelTier.ROUTINE
    assert fallback_decision.tier is ModelTier.HIGH_CAPABILITY
    assert fallback_decision.escalation_reason is EscalationReason.INSUFFICIENT_RETRIEVAL_EVIDENCE
    assert fallback_decision.details["qualifying_chunks"] == 0
