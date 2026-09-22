"""Pydantic V2 schemas for evaluation and benchmark datasets (R22.1, R22.2, R6.4).

Defines strict schemas for:
1. Classification seed dataset items (emails -> gold category, intent, flags).
2. Retrieval seed dataset items (queries -> gold chunk IDs).
"""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.taxonomy import Category as ClassificationCategory


class WorkflowHint(StrEnum):
    """Workflow execution hints for downstream routing (R6.12)."""

    AI_GENERATE = "ai_generate"
    TEMPLATE = "template"
    NONE = "none"


class ClassificationDatasetItem(BaseModel):
    """Individual labelled email instance for triage classification benchmarks (R22.1)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(..., description="Unique deterministic identifier for the email example")
    subject: str = Field(..., description="Subject line of the email")
    body: str = Field(..., description="Body text content of the email")
    sender: str = Field(..., description="Sender email address")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Header key-value pairs (e.g. List-Unsubscribe, Auto-Submitted)",
    )
    gold_category: ClassificationCategory = Field(
        ...,
        description="Ground truth triage category per R6.4",
    )
    gold_intent: str = Field(
        ...,
        description="Specific business intent (e.g. bug_report, invoice_dispute)",
    )
    gold_reply_required: bool = Field(
        ...,
        description="Whether an automated or agent reply is required",
    )
    gold_workflow_hint: WorkflowHint = Field(
        ...,
        description="Workflow execution hint: template, ai_generate, or none",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional contextual metadata or rule tags",
    )


class QueryType(StrEnum):
    """Type of search query distinguishing semantic vs lexical evaluation (H1)."""

    NATURAL_LANGUAGE = "natural_language"
    IDENTIFIER_BEARING = "identifier_bearing"


class RetrievalDatasetItem(BaseModel):
    """Individual search query instance for knowledge retrieval benchmarks (R22.2)."""

    model_config = ConfigDict(frozen=True)

    query_id: str = Field(
        ...,
        description="Unique deterministic identifier for the search query",
    )
    organization_id: UUID = Field(
        ...,
        description="Tenant organization ID scoping the search",
    )
    query_type: QueryType = Field(
        ...,
        description="Query type: natural_language or identifier_bearing",
    )
    query: str = Field(
        ...,
        description="Search query string",
    )
    gold_chunk_ids: list[UUID] = Field(
        ...,
        description="List of relevant knowledge chunk UUIDs",
    )
    difficulty: str = Field(
        "medium",
        description="Query difficulty level (easy, medium, hard)",
    )
    target_entity: str | None = Field(
        None,
        description="Explicit entity cited if identifier-bearing (e.g. ORD-9901)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional query metadata",
    )


__all__ = [
    "ClassificationCategory",
    "ClassificationDatasetItem",
    "QueryType",
    "RetrievalDatasetItem",
    "WorkflowHint",
]

