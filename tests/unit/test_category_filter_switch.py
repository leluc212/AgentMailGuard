"""The retrieval category filter switch (task 7.28; R10.4, R12.4; design.md section 5.4).

RETRIEVAL__CATEGORY_FILTER_ENABLED (default true, so production is unchanged) sets
QueryBuilderConfig.category_filter_enabled. The live v2 benchmark turns it off: its case knowledge
documents are uploaded under the case's own category while live triage picks the category of the
query, and the category filter then hid the documents (0 retrieved for cases routed to retrieval).
Only the category filter goes; the organization and status filters stay (R10.4).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from packages.core.settings import AIWorkerSettings, AppSettings, RetrievalSettings
from packages.domain.entities import Classification
from packages.knowledge.token_counter import TokenCounter
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.query_builder import QueryBuilderConfig, RetrievalQueryBuilder
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.main import build_consumers
from tests.stubs.worker_resources import fake_worker_resources

WARRANTY_TEXT = "The X200 vacuum cleaner has a two year warranty from the purchase date."
QUESTION = "What is the warranty period for the X200 vacuum?"


def _filter_disabled() -> RetrievalSettings:
    return RetrievalSettings(category_filter_enabled=False)


# --- the setting --------------------------------------------------------------------------------


def test_the_category_filter_is_on_by_default_and_the_setting_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert RetrievalSettings().category_filter_enabled is True
    assert AppSettings(_env_file=None).retrieval.category_filter_enabled is True
    monkeypatch.setenv("RETRIEVAL__CATEGORY_FILTER_ENABLED", "false")
    assert AppSettings(_env_file=None).retrieval.category_filter_enabled is False
    assert AIWorkerSettings(_env_file=None).retrieval.category_filter_enabled is False
    monkeypatch.setenv("RETRIEVAL__CATEGORY_FILTER_ENABLED", "true")
    assert AppSettings(_env_file=None).retrieval.category_filter_enabled is True


def test_the_query_builder_configuration_follows_the_retrieval_settings() -> None:
    assert QueryBuilderConfig().category_filter_enabled is True  # the builder's own default
    assert QueryBuilderConfig.from_settings(RetrievalSettings()).category_filter_enabled is True
    assert QueryBuilderConfig.from_settings(_filter_disabled()).category_filter_enabled is False


# --- what the switch changes in the query and in what is retrieved ------------------------------


def test_a_disabled_category_filter_drops_only_the_category_filter() -> None:
    org = str(uuid4())
    classification = Classification(category="billing", intent="invoice_inquiry")

    enabled = RetrievalQueryBuilder(QueryBuilderConfig.from_settings(RetrievalSettings()))
    disabled = RetrievalQueryBuilder(QueryBuilderConfig.from_settings(_filter_disabled()))

    on = enabled.build(subject=QUESTION, classification=classification, organization_id=org)
    off = disabled.build(subject=QUESTION, classification=classification, organization_id=org)

    assert on.filters == {"status": "active", "organization_id": org, "category": "billing"}
    # R10.4: the tenant and the document status stay filtered
    assert off.filters == {"status": "active", "organization_id": org}
    assert off.semantic_text == on.semantic_text  # the text of the query does not change


def _three_tenants(documents_category: str) -> tuple[FakeSearchBackend, UUID]:
    """Three tenants holding the same knowledge text; only the first is the querying tenant's."""
    backend = FakeSearchBackend()
    tenant = uuid4()
    for chunk_id, owner, category in (
        ("CHUNK-OWN", tenant, documents_category),
        ("CHUNK-OTHER-A", uuid4(), documents_category),
        ("CHUNK-OTHER-B", uuid4(), "billing"),
    ):
        backend.add_chunk(
            chunk_id=chunk_id,
            document_id=f"DOC-{chunk_id}",
            organization_id=owner,
            content=WARRANTY_TEXT,
            category=category,
        )
    return backend, tenant


async def _retrieved(
    backend: FakeSearchBackend, tenant: UUID, settings: RetrievalSettings
) -> set[str]:
    builder = RetrievalQueryBuilder(QueryBuilderConfig.from_settings(settings))
    # live triage decided "billing"; the knowledge was uploaded under the case's own category
    query = builder.build(
        subject=QUESTION,
        classification=Classification(category="billing"),
        organization_id=str(tenant),
    )
    result = await HybridRetriever(backend=backend).retrieve(query)
    return {str(candidate.chunk_id) for candidate in result.candidates}


async def test_the_default_filter_hides_documents_filed_under_another_category() -> None:
    # The finding of the live v2 smoke: 0 documents for a case routed to retrieval.
    backend, tenant = _three_tenants(documents_category="support")

    assert await _retrieved(backend, tenant, RetrievalSettings()) == set()


async def test_a_disabled_filter_finds_them_and_never_another_tenants_documents() -> None:
    backend, tenant = _three_tenants(documents_category="support")

    # CHUNK-OTHER-A holds the same text under the same category, CHUNK-OTHER-B under billing,
    # the category the query carried: only the querying tenant's chunk may come back.
    assert await _retrieved(backend, tenant, _filter_disabled()) == {"CHUNK-OWN"}


# --- where the builder is made from the settings ------------------------------------------------


def test_the_ai_worker_builds_its_query_builder_from_the_retrieval_settings() -> None:
    default = build_consumers(
        fake_worker_resources(AIWorkerSettings(_env_file=None)), token_counter=TokenCounter()
    )
    disabled = build_consumers(
        fake_worker_resources(AIWorkerSettings(_env_file=None, retrieval=_filter_disabled())),
        token_counter=TokenCounter(),
    )

    assert default and disabled
    assert default[0].context_builder.query_builder.config.category_filter_enabled is True
    assert disabled[0].context_builder.query_builder.config.category_filter_enabled is False
    # one shared pipeline: every lane consumer reads the same builder
    assert len({id(consumer.context_builder.query_builder) for consumer in disabled}) == 1
