"""Pydantic schemas for knowledge document endpoints (R23.7, R23.2, R9.10)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from packages.domain.knowledge import KnowledgeDocument


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


class KnowledgeDocumentResponse(BaseModel):
    """Authoritative API response schema representing a knowledge document."""

    id: UUID = Field(description="Unique knowledge document UUID.")
    organization_id: UUID = Field(description="Tenant organization UUID scoping this document.")
    title: str = Field(description="Document title.")
    source_uri: str | None = Field(
        default=None,
        description="Source URI or original filesystem path.",
    )
    mime_type: str | None = Field(
        default=None,
        description="Document MIME type (e.g. application/pdf, text/markdown).",
    )
    category: str | None = Field(
        default=None,
        description="Business or functional category (e.g. support, billing, policy).",
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Document version number, monotonically incrementing upon re-ingestion.",
    )
    checksum: str | None = Field(
        default=None,
        description="Content SHA-256 hash formatted as sha256:<hex>.",
    )
    object_key: str | None = Field(
        default=None,
        description="MinIO/S3 object storage key where original document bytes are retained.",
    )
    status: str = Field(
        description=(
            "Per-document ingestion lifecycle status "
            "(pending, parsing, chunking, embedding, active, failed)."
        ),
    )
    failure_reason: str | None = Field(
        default=None,
        description="Error description if ingestion failed, or None if healthy.",
    )
    created_at: datetime = Field(description="UTC timestamp when document was created.")
    updated_at: datetime = Field(description="UTC timestamp when document was last modified.")

    @classmethod
    def from_domain(cls, doc: KnowledgeDocument) -> KnowledgeDocumentResponse:
        """Construct API response schema from domain KnowledgeDocument entity."""
        return cls(
            id=_to_uuid(doc.id),
            organization_id=_to_uuid(doc.organization_id),
            title=doc.title,
            source_uri=doc.source_uri,
            mime_type=doc.mime_type,
            category=doc.category,
            version=doc.version,
            checksum=doc.checksum,
            object_key=doc.object_key,
            status=doc.status,
            failure_reason=doc.failure_reason,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )


class DocumentUploadResponse(BaseModel):
    """Response payload returned upon accepting a document for ingestion (HTTP 202)."""

    document: KnowledgeDocumentResponse = Field(
        description="Created or updated knowledge document metadata.",
    )
    job_id: str = Field(description="Unique job execution identifier for asynchronous ingestion.")
    message: str = Field(
        default="Document accepted for asynchronous ingestion",
        description="Human-readable status summary.",
    )
