"""Unit tests for KnowledgeIngestConsumer AMQP consumer.

Requirements:
- R9.1: Knowledge document ingestion coordination via AMQP consumer.
- R3.5: Terminal parsing or missing resource errors raise FatalError (DLX routing).
- R19.5: Transient timeouts raise TransientError (retry ladder backoff).
- R23.6: Strict tenant isolation enforcement (mandatory organization_id).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.consumer import FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.knowledge.embedder import EmbeddingTimeoutError
from packages.knowledge.parsers.base import UnsupportedDocumentTypeError
from packages.knowledge.pipeline import (
    DocumentNotFoundError,
    IngestionResult,
    KnowledgeIngestionPipeline,
)
from services.knowledge_worker.consumer import KnowledgeIngestConsumer


def make_mock_message(envelope: JobEnvelope) -> AbstractIncomingMessage:
    """Create a mock AMQP message containing the specified envelope."""
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = envelope.model_dump_json().encode("utf-8")
    msg.exchange = "knowledge.ingest"
    msg.routing_key = "knowledge.ingest"
    msg.headers = {}
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    msg.reject = AsyncMock()
    return msg


@pytest.fixture
def mock_pipeline() -> MagicMock:
    pipeline = MagicMock(spec=KnowledgeIngestionPipeline)
    pipeline.ingest_document = AsyncMock()
    return pipeline


@pytest.fixture
def consumer(mock_pipeline: MagicMock) -> KnowledgeIngestConsumer:
    return KnowledgeIngestConsumer(pipeline=mock_pipeline)


@pytest.mark.asyncio
async def test_consumer_processes_valid_job(
    consumer: KnowledgeIngestConsumer,
    mock_pipeline: MagicMock,
) -> None:
    """Happy path: consumer processes valid envelope and calls pipeline."""
    org_id = uuid4()
    doc_id = uuid4()

    mock_pipeline.ingest_document.return_value = IngestionResult(
        document_id=doc_id,
        organization_id=org_id,
        version=1,
        total_chunks=3,
        embedded_chunks=3,
        carried_forward_chunks=0,
        status="active",
    )

    envelope = JobEnvelope(
        job_type="knowledge_ingest",
        idempotency_key=f"ingest-{doc_id}",
        organization_id=str(org_id),
        payload={
            "document_id": str(doc_id),
            "filename": "policy.pdf",
            "content_type": "application/pdf",
        },
    )
    raw_msg = make_mock_message(envelope)

    await consumer.process_job(envelope, raw_msg)

    mock_pipeline.ingest_document.assert_awaited_once_with(
        document_id=str(doc_id),
        organization_id=str(org_id),
        filename="policy.pdf",
        content_type="application/pdf",
    )


@pytest.mark.asyncio
async def test_consumer_missing_org_id_raises_fatal(
    consumer: KnowledgeIngestConsumer,
) -> None:
    """Missing mandatory organization_id raises FatalError (R23.6)."""
    envelope = JobEnvelope(
        job_type="knowledge_ingest",
        idempotency_key="key-1",
        organization_id="",
        payload={"document_id": str(uuid4())},
    )
    raw_msg = make_mock_message(envelope)

    with pytest.raises(FatalError, match="Missing mandatory organization_id"):
        await consumer.process_job(envelope, raw_msg)


@pytest.mark.asyncio
async def test_consumer_missing_doc_id_raises_fatal(
    consumer: KnowledgeIngestConsumer,
) -> None:
    """Missing document_id raises FatalError."""
    envelope = JobEnvelope(
        job_type="knowledge_ingest",
        idempotency_key="key-2",
        organization_id=str(uuid4()),
        payload={},
    )
    raw_msg = make_mock_message(envelope)

    with pytest.raises(FatalError, match="Missing document_id"):
        await consumer.process_job(envelope, raw_msg)


@pytest.mark.asyncio
async def test_consumer_document_not_found_raises_fatal(
    consumer: KnowledgeIngestConsumer,
    mock_pipeline: MagicMock,
) -> None:
    """DocumentNotFoundError from pipeline is translated to FatalError (R3.5)."""
    org_id = uuid4()
    doc_id = uuid4()

    mock_pipeline.ingest_document.side_effect = DocumentNotFoundError("Doc missing")

    envelope = JobEnvelope(
        job_type="knowledge_ingest",
        idempotency_key="key-3",
        organization_id=str(org_id),
        payload={"document_id": str(doc_id)},
    )
    raw_msg = make_mock_message(envelope)

    with pytest.raises(FatalError, match="Document not found"):
        await consumer.process_job(envelope, raw_msg)


@pytest.mark.asyncio
async def test_consumer_unsupported_type_raises_fatal(
    consumer: KnowledgeIngestConsumer,
    mock_pipeline: MagicMock,
) -> None:
    """UnsupportedDocumentTypeError from pipeline is translated to FatalError (R3.5)."""
    org_id = uuid4()
    doc_id = uuid4()

    mock_pipeline.ingest_document.side_effect = UnsupportedDocumentTypeError("Unsupported MIME")

    envelope = JobEnvelope(
        job_type="knowledge_ingest",
        idempotency_key="key-4",
        organization_id=str(org_id),
        payload={"document_id": str(doc_id)},
    )
    raw_msg = make_mock_message(envelope)

    with pytest.raises(FatalError, match="Unsupported document type"):
        await consumer.process_job(envelope, raw_msg)


@pytest.mark.asyncio
async def test_consumer_embedding_timeout_raises_transient(
    consumer: KnowledgeIngestConsumer,
    mock_pipeline: MagicMock,
) -> None:
    """EmbeddingTimeoutError is translated to TransientError for retry ladder backoff (R19.5)."""
    org_id = uuid4()
    doc_id = uuid4()

    mock_pipeline.ingest_document.side_effect = EmbeddingTimeoutError("Embedder timed out")

    envelope = JobEnvelope(
        job_type="knowledge_ingest",
        idempotency_key="key-5",
        organization_id=str(org_id),
        payload={"document_id": str(doc_id)},
    )
    raw_msg = make_mock_message(envelope)

    with pytest.raises(TransientError, match="Embedding timeout"):
        await consumer.process_job(envelope, raw_msg)


@pytest.mark.asyncio
async def test_consumer_unexpected_error_raises_transient(
    consumer: KnowledgeIngestConsumer,
    mock_pipeline: MagicMock,
) -> None:
    """Unexpected exception is translated to TransientError."""
    org_id = uuid4()
    doc_id = uuid4()

    mock_pipeline.ingest_document.side_effect = RuntimeError("Database connection reset")

    envelope = JobEnvelope(
        job_type="knowledge_ingest",
        idempotency_key="key-6",
        organization_id=str(org_id),
        payload={"document_id": str(doc_id)},
    )
    raw_msg = make_mock_message(envelope)

    with pytest.raises(TransientError, match="Unexpected ingestion failure"):
        await consumer.process_job(envelope, raw_msg)
