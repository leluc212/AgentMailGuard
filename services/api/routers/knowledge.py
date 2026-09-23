"""Knowledge document upload and ingestion status endpoints (R23.7, R23.2, R5.8, R9.10).

Provides:
- POST /v1/knowledge/documents (multipart upload to MinIO, DB record creation, and AMQP enqueue)
- GET /v1/knowledge/documents (paginated list with status and category filtering)
- GET /v1/knowledge/documents/{id} (single document ingestion status and metadata)
- DELETE /v1/knowledge/documents/{id} (document, chunk, and storage cleanup)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi import (
    Path as FastPath,
)

from packages.broker.envelope import JobEnvelope
from packages.core.pagination import PaginatedResponse, paginate
from packages.core.settings import AppSettings
from packages.core.storage import ObjectKeyBuilder
from packages.domain.knowledge import KnowledgeDocument
from packages.observability.context import bind_log_context
from packages.observability.tracing import trace_span
from services.api.dependencies import (
    KnowledgeStoreDep,
    PublisherDep,
    StorageClientDep,
    get_organization_id,
)
from services.api.pagination import PaginationParamsDep
from services.api.schemas.knowledge import (
    DocumentUploadResponse,
    KnowledgeDocumentResponse,
)

logger = logging.getLogger(__name__)

knowledge_router = APIRouter(prefix="/knowledge/documents", tags=["knowledge"])

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".html",
    ".htm",
    ".md",
    ".markdown",
    ".txt",
}

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/html",
    "text/markdown",
    "text/plain",
    "application/octet-stream",
}


def _validate_document_format(filename: str, content_type: str | None) -> None:
    """Validate that uploaded document is an accepted format per R9.2."""
    suffix = Path(filename).suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return

    if content_type and content_type.lower() in ALLOWED_MIME_TYPES:
        return

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": (
                f"Unsupported document format for '{filename}'. Supported formats: "
                "PDF, DOCX, HTML, Markdown, and Plain Text (R9.2)."
            ),
            "code": "UNSUPPORTED_DOCUMENT_FORMAT",
        },
    )


@knowledge_router.post(
    "",
    summary="Upload Knowledge Document",
    description=(
        "Upload a knowledge document for asynchronous ingestion per R23.7, R23.2, and R5.8. "
        "Saves original file to object storage, creates a database tracking record in 'pending' "
        "state, and dispatches a job to the 'knowledge.ingest' queue."
    ),
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentUploadResponse,
)
async def upload_document(
    org_id: Annotated[UUID, Depends(get_organization_id)],
    knowledge_store: KnowledgeStoreDep,
    storage_client: StorageClientDep,
    publisher: PublisherDep,
    file: Annotated[
        UploadFile,
        File(description="Source document file (PDF, DOCX, HTML, MD, TXT)."),
    ],
    title: Annotated[
        str | None,
        Form(description="Document title. Defaults to original filename."),
    ] = None,
    category: Annotated[
        str | None,
        Form(description="Optional document category (e.g. policy)."),
    ] = None,
    document_id: Annotated[
        UUID | None,
        Form(description="Optional existing document UUID to re-ingest as version N+1."),
    ] = None,
) -> DocumentUploadResponse:
    """Process multipart upload, retain in object storage, and enqueue for ingestion."""
    bind_log_context(organization_id=str(org_id))

    filename = Path(file.filename or "document.txt").name
    content_type = file.content_type

    # 1. Format validation (R9.2)
    _validate_document_format(filename, content_type)

    # 2. Read bytes & check empty payload
    content = await file.read()
    if not content or len(content.strip()) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "Uploaded document file is empty.",
                "code": "EMPTY_DOCUMENT",
            },
        )

    checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"

    with trace_span(
        "api.knowledge.upload",
        attributes={
            "organization_id": str(org_id),
            "filename": filename,
            "size_bytes": len(content),
            "content_type": content_type or "",
        },
    ):
        # 3. Resolve document identity and target version (R9.8)
        if document_id is not None:
            existing = await knowledge_store.get_document(org_id, document_id)
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": f"Document '{document_id}' not found for re-ingestion.",
                        "code": "DOCUMENT_NOT_FOUND",
                    },
                )
            doc_id = document_id
            target_version = (
                (existing.version + 1)
                if (existing.status == "active" or existing.version > 1)
                else max(1, existing.version)
            )
            resolved_title = title or existing.title or filename
            resolved_category = category or existing.category
        else:
            doc_id = uuid4()
            target_version = 1
            resolved_title = title or filename
            resolved_category = category

        # 4. Upload payload to MinIO/S3 object storage (R5.8)
        settings = AppSettings()
        bucket = settings.object_storage.bucket_knowledge
        object_key = ObjectKeyBuilder.knowledge_doc(
            organization_id=org_id,
            document_id=doc_id,
            version=target_version,
            filename=filename,
        )

        try:
            await storage_client.put_bytes(
                bucket=bucket,
                key=object_key,
                data=content,
                content_type=content_type or "application/octet-stream",
            )
        except Exception as err:
            logger.error(
                "Failed to upload document %s to object storage %s/%s: %s",
                doc_id,
                bucket,
                object_key,
                err,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error": f"Object storage upload failed: {err}",
                    "code": "STORAGE_UPLOAD_ERROR",
                },
            ) from err

        # 5. Insert / update pending document record in database (R23.7, R9.10)
        doc = KnowledgeDocument(
            id=doc_id,
            organization_id=org_id,
            title=resolved_title,
            source_uri=None,
            mime_type=content_type,
            category=resolved_category,
            version=target_version,
            checksum=checksum,
            object_key=object_key,
            status="pending",
            failure_reason=None,
        )
        saved_doc = await knowledge_store.insert_document(doc)

        # 6. Enqueue asynchronous ingestion job to RabbitMQ (R3.1, R9.1)
        job_id = str(uuid4())
        envelope = JobEnvelope(
            job_id=job_id,
            job_type="knowledge_ingest",
            idempotency_key=f"ingest-{doc_id}-v{target_version}",
            organization_id=str(org_id),
            payload={
                "document_id": str(doc_id),
                "filename": filename,
                "content_type": content_type,
            },
        )

        if publisher is not None:
            try:
                await publisher.publish(
                    exchange_name=settings.broker.exchange_knowledge_ingest,
                    routing_key=settings.broker.queue_knowledge,
                    envelope=envelope,
                )
            except Exception as exc:
                logger.warning(
                    "AMQP job publish deferred or failed for document %s: %s",
                    doc_id,
                    exc,
                )

        logger.info(
            "Document %s (v%d) uploaded successfully for org %s; enqueued job %s",
            doc_id,
            target_version,
            org_id,
            job_id,
        )

        return DocumentUploadResponse(
            document=KnowledgeDocumentResponse.from_domain(saved_doc),
            job_id=job_id,
            message="Document accepted for asynchronous ingestion",
        )


@knowledge_router.get(
    "",
    summary="List Knowledge Documents",
    description=(
        "List knowledge documents for the authenticated organization with pagination "
        "and ingestion status filtering per R23.7, R23.2, and R5.3."
    ),
    response_model=PaginatedResponse[KnowledgeDocumentResponse],
)
async def list_documents(
    org_id: Annotated[UUID, Depends(get_organization_id)],
    knowledge_store: KnowledgeStoreDep,
    pagination: PaginationParamsDep,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Filter by status (pending, parsing, chunking, embedding, active, failed).",
        ),
    ] = None,
    category_filter: Annotated[
        str | None,
        Query(alias="category", description="Filter by business/functional category."),
    ] = None,
) -> PaginatedResponse[KnowledgeDocumentResponse]:
    """Retrieve paginated knowledge documents for the authenticated organization."""
    docs, total_count = await knowledge_store.list_documents(
        organization_id=org_id,
        status=status_filter,
        category=category_filter,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    items = [KnowledgeDocumentResponse.from_domain(d) for d in docs]
    return paginate(items=items, total_count=total_count, params=pagination)


@knowledge_router.get(
    "/{id}",
    summary="Get Knowledge Document Details",
    description=(
        "Retrieve status and metadata for a single knowledge document by ID (R23.7, R9.10)."
    ),
    response_model=KnowledgeDocumentResponse,
)
async def get_document(
    id: Annotated[UUID, FastPath(description="Document UUID to inspect.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    knowledge_store: KnowledgeStoreDep,
) -> KnowledgeDocumentResponse:
    """Fetch single knowledge document within tenant scope."""
    doc = await knowledge_store.get_document(org_id, id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Knowledge document '{id}' not found.",
                "code": "DOCUMENT_NOT_FOUND",
            },
        )
    return KnowledgeDocumentResponse.from_domain(doc)


@knowledge_router.delete(
    "/{id}",
    summary="Delete Knowledge Document",
    description="Delete knowledge document, chunks, and storage object within tenant scope.",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    id: Annotated[UUID, FastPath(description="Document UUID to delete.")],
    org_id: Annotated[UUID, Depends(get_organization_id)],
    knowledge_store: KnowledgeStoreDep,
    storage_client: StorageClientDep,
) -> None:
    """Delete document, cascading to chunks and deleting object from storage."""
    doc = await knowledge_store.get_document(org_id, id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Knowledge document '{id}' not found.",
                "code": "DOCUMENT_NOT_FOUND",
            },
        )

    # Clean up object in storage if object_key exists
    if doc.object_key:
        settings = AppSettings()
        bucket = settings.object_storage.bucket_knowledge
        try:
            await storage_client.delete_object(bucket, doc.object_key)
        except Exception as exc:
            logger.warning("Could not delete storage object %s/%s: %s", bucket, doc.object_key, exc)

    await knowledge_store.delete_document(org_id, id)
