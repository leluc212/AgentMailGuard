"""Composition roots of the email and knowledge workers (RA.8)."""

from __future__ import annotations

from packages.core.settings import EmailWorkerSettings, KnowledgeWorkerSettings
from services.email_worker import main as email_main
from services.knowledge_worker import main as knowledge_main
from tests.stubs.worker_resources import fake_worker_resources


def test_email_worker_consumer_uses_shared_resources() -> None:
    settings = EmailWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    consumer = email_main.build_consumer(res)

    assert consumer.queue_name == settings.broker.queue_normalize
    assert consumer._connection is res.connection
    assert consumer.prefetch_count == settings.concurrency.email_worker_concurrency
    assert consumer.shutdown_coordinator is res.shutdown


def test_knowledge_worker_consumer_uses_shared_resources() -> None:
    settings = KnowledgeWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    consumer = knowledge_main.build_consumer(res)

    assert consumer.queue_name == settings.broker.queue_knowledge
    assert consumer._connection is res.connection
    assert consumer.prefetch_count == settings.concurrency.knowledge_worker_concurrency
    assert consumer.shutdown_coordinator is res.shutdown


async def test_knowledge_worker_counts_ingestion_embedding_tokens() -> None:
    """R9.11: ingestion embeddings are counted in embedding_tokens_total."""
    settings = KnowledgeWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    embedder = knowledge_main.build_consumer(res).pipeline.embedder
    await embedder.embed_texts(["refund policy for annual plans"])

    model = settings.embedding.model_name
    assert res.metrics.embedding_tokens_total.labels(model=model)._value.get() > 0


def test_knowledge_worker_closes_its_embedder_after_the_consumer() -> None:
    from packages.core.settings import EmbeddingSettings
    from packages.knowledge.embedder import HttpEmbedder

    settings = KnowledgeWorkerSettings(_env_file=None, embedding=EmbeddingSettings(mock=False))
    res = fake_worker_resources(settings)

    consumer = knowledge_main.build_consumer(res)
    embedder = consumer.pipeline.embedder

    assert isinstance(embedder, HttpEmbedder)
    callbacks = res.shutdown._cleanup_callbacks
    closes = [i for i, cb in enumerate(callbacks) if cb == consumer.close]
    assert closes and callbacks.index(embedder.aclose) > max(closes)
