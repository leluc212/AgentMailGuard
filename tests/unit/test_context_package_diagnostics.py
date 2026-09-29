"""The ContextPackage carries what the retrieval saw, as typed fields (R10.6, R10.10, R21).

``retrieval_degraded`` and ``retrieval_underfilled`` come from the ``RetrievalResult`` the builder
already held and dropped; ``rerank_applied`` is filled by the reranking builder. All three are
diagnostics, never prompt text, and None means unknown: no retrieval ran, or nothing could tell.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.domain.entities import (
    Classification,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.state_machine import JobState
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import RetrievalQuery
from packages.retrieval.retriever import HybridRetriever, RetrievalResult


def _message() -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=uuid4(),
        provider="mock",
        provider_message_id="p-1",
        sender=EmailAddress(email="alice@example.com"),
        received_at=datetime.now(UTC),
        subject="Invoice question",
        body_text="What are the payment terms for invoice INV-2026-001?",
        body_text_clean="What are the payment terms for invoice INV-2026-001?",
    )


async def _build(
    retriever: HybridRetriever | None, *, retrieval_required: bool = True
) -> ContextPackage:
    message = _message()
    job = Job(
        id=uuid4(),
        organization_id=message.organization_id,
        thread_id=message.thread_id,
        message_id=message.message_id,
        state=JobState.QUEUED,
    )
    builder = ContextBuilder(thread_assembler=ThreadContextAssembler(), retriever=retriever)
    return await builder.build_context(
        job=job,
        message=message,
        classification=Classification(
            category="billing", intent="invoice_inquiry", retrieval_required=retrieval_required
        ),
        thread_messages=[message],
    )


class _ScriptedRetriever(HybridRetriever):
    """A retriever that answers with a prepared RetrievalResult."""

    def __init__(self, result: RetrievalResult) -> None:
        super().__init__(FakeSearchBackend())
        self._result = result

    async def retrieve(self, query: RetrievalQuery, **_: object) -> RetrievalResult:
        return self._result


def test_a_package_starts_with_every_diagnostic_unknown() -> None:
    package = ContextPackage(
        agent_instructions="a", category_instructions="b", current_message=_message()
    )

    assert package.retrieval_degraded is None
    assert package.retrieval_underfilled is None
    assert package.rerank_applied is None


@pytest.mark.parametrize(
    ("degraded", "underfilled"), [(True, True), (True, False), (False, True), (False, None)]
)
async def test_the_builder_copies_what_the_retrieval_saw(
    degraded: bool, underfilled: bool | None
) -> None:
    result = RetrievalResult(retrieval_degraded=degraded, retrieval_underfilled=underfilled)

    package = await _build(_ScriptedRetriever(result))

    assert package.retrieval_degraded is degraded
    assert package.retrieval_underfilled is underfilled


async def test_a_failed_vector_branch_marks_the_package_degraded() -> None:
    """Through the real retriever: lexical answers, the vector branch fails (R10.6)."""
    backend = FakeSearchBackend()
    backend.simulate_vector_error = RuntimeError("embedder timed out")

    package = await _build(HybridRetriever(backend))

    assert package.retrieval_degraded is True
    assert package.retrieval_underfilled is None  # the failed branch cannot say


async def test_a_healthy_retrieval_marks_the_package_not_degraded() -> None:
    package = await _build(HybridRetriever(FakeSearchBackend()))

    assert package.retrieval_degraded is False


async def test_no_retrieval_leaves_every_diagnostic_unknown() -> None:
    skipped = await _build(HybridRetriever(FakeSearchBackend()), retrieval_required=False)
    unwired = await _build(None)

    for package in (skipped, unwired):
        assert package.retrieval_degraded is None
        assert package.retrieval_underfilled is None
        assert package.rerank_applied is None
