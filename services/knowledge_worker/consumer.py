"""Knowledge ingestion AMQP consumer service (R9.1, R9.8, R9.9, R9.10, R3.5).

Consumes document ingestion requests from `knowledge.ingest`, coordinates parsing, chunking,
deduplicated embedding, and atomic versioned persistence.
- Terminal parsing or schema errors raise FatalError to route directly to dead-letter (R3.5).
- Transient network or embedding timeout errors raise TransientError for retry backoff.
"""

from __future__ import annotations

import logging

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.job import JobStoreProtocol
from packages.knowledge.embedder import EmbeddingError, EmbeddingTimeoutError
from packages.knowledge.parsers.base import UnsupportedDocumentTypeError
from packages.knowledge.pipeline import (
    DocumentNotFoundError,
    IngestionError,
    IngestionResult,
    KnowledgeIngestionPipeline,
)
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator

logger = logging.getLogger(__name__)


class KnowledgeIngestConsumer(BaseConsumer):
    """Consumer processing asynchronous knowledge document ingestion jobs."""

    def __init__(
        self,
        pipeline: KnowledgeIngestionPipeline,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        job_store: JobStoreProtocol | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        settings = broker_settings or BrokerSettings()
        super().__init__(
            queue_name=settings.queue_knowledge,
            broker_settings=settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=job_store,
            metrics=metrics,
        )
        self.pipeline = pipeline

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        """Process a document ingestion job envelope to completion."""
        org_id = envelope.organization_id
        if not org_id:
            raise FatalError("Missing mandatory organization_id in JobEnvelope (R23.6)")

        doc_id_raw = envelope.payload.get("document_id") or envelope.message_id
        if not doc_id_raw:
            raise FatalError("Missing document_id in JobEnvelope payload")

        filename = envelope.payload.get("filename")
        content_type = envelope.payload.get("content_type")

        logger.info(
            "Processing knowledge ingestion job %s for document %s (org: %s)",
            envelope.job_id,
            doc_id_raw,
            org_id,
        )

        try:
            result: IngestionResult = await self.pipeline.ingest_document(
                document_id=doc_id_raw,
                organization_id=org_id,
                filename=filename,
                content_type=content_type,
            )
            logger.info(
                "Document %s ingestion successful: version=%d chunks=%d (embedded=%d, carried=%d)",
                result.document_id,
                result.version,
                result.total_chunks,
                result.embedded_chunks,
                result.carried_forward_chunks,
            )
        except DocumentNotFoundError as err:
            logger.warning("Document not found for ingestion: %s", err)
            raise FatalError(f"Document not found: {err}") from err
        except UnsupportedDocumentTypeError as err:
            logger.error("Unsupported document type during ingestion: %s", err)
            raise FatalError(f"Unsupported document type: {err}") from err
        except EmbeddingTimeoutError as err:
            logger.warning("Embedding service timed out during ingestion: %s", err)
            raise TransientError(f"Embedding timeout: {err}") from err
        except (EmbeddingError, IngestionError) as err:
            logger.error("Ingestion failed: %s", err)
            raise FatalError(f"Ingestion failed: {err}") from err
        except Exception as err:
            logger.exception("Unexpected error during document ingestion: %s", err)
            raise TransientError(f"Unexpected ingestion failure: {err}") from err
