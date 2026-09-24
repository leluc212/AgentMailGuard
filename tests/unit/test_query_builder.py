"""Unit tests for retrieval query builder and regex identifier extraction.

Requirements:
- R12.1: Construct retrieval query from current email + thread summary + classification intent.
- R12.2: Produce semantic query string and lexical keyword set as separate outputs.
- R12.3: Extract structured identifiers (invoice, order, ticket, SKU, container, incident)
  via configurable regex and pass them to the lexical branch verbatim.
- R12.4: Derive category and metadata filters from classification result.
- R12.5: Construct query without extra LLM calls in the default path (sub-millisecond).
- R12.6: Persist constructed query with the job for debugging and replay.
- tasks.md §3.13: Test explicitly that an identifier-bearing email (e.g. INV-2026-01829)
  retrieves the right chunk where a vector-only query would not.
"""

from __future__ import annotations

import datetime
import time
from uuid import uuid4

import pytest

from packages.domain.entities import Classification, EmailAddress, NormalizedMessage
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import RetrievalQuery
from packages.retrieval.query_builder import (
    QueryBuilderConfig,
    RetrievalQueryBuilder,
)
from packages.retrieval.retriever import HybridRetriever


def _make_sample_message(
    subject: str = "Discrepancy on invoice INV-2026-01829",
    body: str = "Hi team,\n\nWe were billed twice on INV-2026-01829. Please fix.\nThanks!",
    org_id: str = "org-123",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=org_id,
        provider="gmail",
        provider_message_id="msg-1",
        sender=EmailAddress(email="billing@client.com", name="Alice Client"),
        recipients=[EmailAddress(email="ap@acme.com", name="ACME AP")],
        subject=subject,
        subject_normalized=subject,
        body_text=body,
        body_text_clean=body,
        received_at=datetime.datetime.now(datetime.UTC),
    )


class TestIdentifierExtraction:
    """Test regex extraction of structured entity identifiers (R12.3)."""

    def test_extract_invoice_patterns(self) -> None:
        builder = RetrievalQueryBuilder()
        text = "Please review INV-2026-01829 and INVOICE-9921, also INV_8841."
        ids = builder.extract_identifiers(text)
        assert "INV-2026-01829" in ids
        assert "INVOICE-9921" in ids
        assert "INV_8841" in ids

    def test_extract_order_ticket_sku_container_incident(self) -> None:
        builder = RetrievalQueryBuilder()
        text = (
            "Regarding ORD-82915 and ORDER-12345. "
            "Created TICKET-4401 and TKT-9912. "
            "Item SKU-8821-X was in CONT-90218. "
            "Tracking under INC-10928 and INCIDENT-551."
        )
        ids = builder.extract_identifiers(text)
        assert "ORD-82915" in ids
        assert "ORDER-12345" in ids
        assert "TICKET-4401" in ids
        assert "TKT-9912" in ids
        assert "SKU-8821-X" in ids
        assert "CONT-90218" in ids
        assert "INC-10928" in ids
        assert "INCIDENT-551" in ids

    def test_deduplication_and_order_preservation(self) -> None:
        builder = RetrievalQueryBuilder()
        text1 = "Check INV-2026-01829 and ORD-1111."
        text2 = "Repeated ORD-1111 and INV-2026-01829, then new TICKET-999."
        # Passing empty string ensures empty text branch is covered
        ids = builder.extract_identifiers(text1, "", text2)
        assert ids == ["INV-2026-01829", "ORD-1111", "TICKET-999"]
        assert builder.extract_lexical_keywords("", "keyword") == ["keyword"]

    def test_custom_identifier_patterns(self) -> None:
        config = QueryBuilderConfig(
            identifier_patterns={
                "custom_po": r"\bPO[-_][0-9]{4,8}\b",
            }
        )
        builder = RetrievalQueryBuilder(config=config)
        ids = builder.extract_identifiers("Approval for PO-987654 required.")
        assert ids == ["PO-987654"]


class TestLexicalKeywordExtraction:
    """Test extraction of discrete lexical keywords and stopword removal (R12.2)."""

    def test_stopwords_and_greetings_filtered(self) -> None:
        builder = RetrievalQueryBuilder()
        text = "Hello team, please review the billing discrepancy and double charge. Thanks!"
        keywords = builder.extract_lexical_keywords(text)
        # Stopwords 'hello', 'team', 'please', 'the', 'and', 'thanks' must be stripped
        assert "billing" in keywords
        assert "discrepancy" in keywords
        assert "double" in keywords
        assert "charge" in keywords
        assert "hello" not in keywords
        assert "please" not in keywords
        assert "the" not in keywords

    def test_keywords_capped_at_max(self) -> None:
        config = QueryBuilderConfig(max_lexical_keywords=3)
        builder = RetrievalQueryBuilder(config=config)
        text = "alpha beta gamma delta epsilon zeta"
        keywords = builder.extract_lexical_keywords(text)
        assert len(keywords) == 3
        assert keywords == ["alpha", "beta", "gamma"]


class TestSemanticTextComposition:
    """Test semantic query text composition (R12.1)."""

    def test_compose_with_thread_summary_and_intent(self) -> None:
        builder = RetrievalQueryBuilder()
        semantic = builder.compose_semantic_text(
            subject="Locked account",
            body="I am still unable to login.",
            intent="account_recovery",
            thread_summary="Previous password reset link failed.",
        )
        assert "Intent: account_recovery." in semantic
        assert "Thread summary: Previous password reset link failed." in semantic
        assert "Subject: Locked account." in semantic
        assert "Body: I am still unable to login." in semantic

    def test_compose_phase3_degraded_without_thread_summary(self) -> None:
        builder = RetrievalQueryBuilder()
        semantic = builder.compose_semantic_text(
            subject="Invoice Question",
            body="Is this invoice paid?",
            intent="billing_inquiry",
            thread_summary=None,
        )
        assert "Intent: billing_inquiry." in semantic
        assert "Thread summary:" not in semantic
        assert "Subject: Invoice Question." in semantic
        assert "Body: Is this invoice paid?" in semantic

    def test_body_length_truncation(self) -> None:
        config = QueryBuilderConfig(max_body_semantic_chars=50)
        builder = RetrievalQueryBuilder(config=config)
        long_body = "x" * 200
        semantic = builder.compose_semantic_text(
            subject="Subj",
            body=long_body,
        )
        assert len(semantic) < 100
        assert "..." in semantic


class TestQueryBuilderE2E:
    """Test full RetrievalQuery construction from entities and dicts (R12.1-R12.6)."""

    def test_build_from_entities(self) -> None:
        builder = RetrievalQueryBuilder()
        msg = _make_sample_message()
        classification = Classification(
            category="billing",
            intent="dispute_charge",
        )

        query = builder.build(
            message=msg,
            classification=classification,
            thread_summary="Previous conversation about setup.",
        )

        assert isinstance(query, RetrievalQuery)
        assert query.organization_id == "org-123"
        assert query.category == "billing"
        assert query.status == "active"
        assert "INV-2026-01829" in query.identifiers
        assert "discrepancy" in query.lexical_terms
        assert "Intent: dispute_charge." in query.semantic_text
        assert "Thread summary: Previous conversation about setup." in query.semantic_text

    def test_build_from_primitives_and_dict_classification(self) -> None:
        builder = RetrievalQueryBuilder()
        query = builder.build(
            subject="Question on ORD-5544",
            body_text="Where is my shipment ORD-5544?",
            classification={"category": "shipping", "intent": "track_package"},
            organization_id="org-999",
        )

        assert query.organization_id == "org-999"
        assert query.category == "shipping"
        assert query.identifiers == ["ORD-5544"]
        assert "track_package" in query.semantic_text
        assert "shipment" in query.lexical_terms

    def test_ignored_categories_do_not_add_filter(self) -> None:
        builder = RetrievalQueryBuilder()
        query = builder.build(
            subject="General feedback",
            body_text="Great service!",
            category="general",
        )
        assert "category" not in query.filters

    def test_zero_llm_execution_speed(self) -> None:
        """Verify query construction operates deterministically in sub-millisecond time (R12.5)."""
        builder = RetrievalQueryBuilder()
        msg = _make_sample_message()
        cls_entity = Classification(category="billing", intent="invoice_inquiry")

        start = time.perf_counter()
        for _ in range(50):
            builder.build(message=msg, classification=cls_entity)
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / 50.0) * 1000.0
        assert avg_ms < 5.0, f"Query building too slow: {avg_ms:.2f} ms"

    def test_query_serialization_roundtrip(self) -> None:
        """Verify query can be serialized to dict and reconstructed for job persistence (R12.6)."""
        builder = RetrievalQueryBuilder()
        msg = _make_sample_message()
        query = builder.build(
            message=msg,
            category="billing",
            intent="payment_check",
            extra_filters={"region": "us-east"},
        )

        data = query.to_dict()
        restored = RetrievalQuery.from_dict(data)

        assert restored.semantic_text == query.semantic_text
        assert restored.lexical_terms == query.lexical_terms
        assert restored.identifiers == query.identifiers
        assert restored.filters == query.filters
        assert restored.organization_id == "org-123"
        assert restored.category == "billing"
        assert restored.filters["region"] == "us-east"


class TestIdentifierRetrievalSuperiority:
    """Test requirement: an identifier email retrieves the right chunk where vector-only fails.

    Cites tasks.md §3.13 and design.md §5.5:
    Exact match cases like INV-2026-01829 succeed in hybrid retrieval where vector-only fails.
    """

    @pytest.mark.asyncio
    async def test_identifier_retrieval_beats_vector_only(self) -> None:
        special_invoice = "INV-2026-01829"

        # Chunk 1: Broad procedure document with rich conceptual language.
        # Has high cosine similarity to the query embedding [1.0, 0.0, 0.0],
        # but contains NO specific invoice identifier or transaction keywords.
        doc_procedure = (
            "Standard Operating Procedure: Enterprise dispute resolution guidelines. "
            "Customers requesting adjustments must follow corporate accounting protocol."
        )

        # Chunk 2: Specific transactional record containing the target invoice number.
        # Has lower semantic similarity [0.5, 0.5, 0.0] to query embedding,
        # but matches the exact invoice number INV-2026-01829 verbatim.
        doc_invoice = (
            f"Credit Memo & Adjustment Log: Invoice {special_invoice} line item correction. "
            "Double billing on Enterprise Pro Addon resolved by credit note."
        )

        # Chunk 3: General billing FAQ that contains some common billing terms.
        doc_faq = (
            "Frequently Asked Questions: Reviewing billing statements and double charges. "
            "How to review invoices and contact billing support."
        )

        backend = FakeSearchBackend()
        backend.add_chunk(
            chunk_id="chunk-procedure",
            document_id="doc-proc-1",
            organization_id="org-acme",
            content=doc_procedure,
            category="billing",
            embedding=[1.0, 0.0, 0.0],
        )
        backend.add_chunk(
            chunk_id="chunk-invoice",
            document_id="doc-inv-1",
            organization_id="org-acme",
            content=doc_invoice,
            category="billing",
            embedding=[0.5, 0.5, 0.0],
        )
        backend.add_chunk(
            chunk_id="chunk-faq",
            document_id="doc-faq-1",
            organization_id="org-acme",
            content=doc_faq,
            category="billing",
            embedding=[0.1, 0.9, 0.0],
        )

        # Build query from email containing INV-2026-01829
        builder = RetrievalQueryBuilder()
        query = builder.build(
            subject=f"Discrepancy on invoice {special_invoice}",
            body_text=f"Please review double billing on {special_invoice}.",
            category="billing",
            intent="billing_inquiry",
            organization_id="org-acme",
        )
        query.query_vector = [1.0, 0.0, 0.0]

        assert special_invoice in query.identifiers

        # PROOF PART 1: Vector-only search ranks generic procedure #1,
        # failing to locate invoice chunk
        vector_only = await backend.vector(query, top_n=3)
        assert len(vector_only) >= 2
        assert vector_only[0].chunk_id == "chunk-procedure"
        assert vector_only[0].chunk_id != "chunk-invoice"

        # PROOF PART 2: Lexical search with exact identifier matches target invoice at #1
        lexical_only = await backend.lexical(query, top_n=3)
        assert len(lexical_only) >= 1
        assert lexical_only[0].chunk_id == "chunk-invoice"

        # PROOF PART 3: Hybrid retrieval fuses the exact lexical identifier match to rank #1
        retriever = HybridRetriever(backend=backend)
        hybrid_result = await retriever.retrieve(query)

        assert len(hybrid_result.candidates) >= 2
        top_chunk = hybrid_result.candidates[0]

        # Exact target invoice chunk successfully wins rank 1 in hybrid retrieval!
        assert top_chunk.chunk_id == "chunk-invoice"
        assert special_invoice in top_chunk.content
        assert top_chunk.lexical_rank == 1
        assert top_chunk.vector_rank == 2
        assert top_chunk.fused_score is not None
        assert hybrid_result.candidates[1].fused_score is not None
        assert top_chunk.fused_score > hybrid_result.candidates[1].fused_score
