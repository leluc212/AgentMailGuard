"""HTTP client for the review UI: the only way the frontend reaches the system.

Requirements: R23.4, R23.6; design.md §5.8 "Review UI"; ADR-0009.

The frontend never imports services.api or the database layer: it calls /v1 over HTTP and
sends the configured tenant as the X-Organization-Id header (R23.6). Response models ignore
unknown fields, so an additive API change never breaks a page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from packages.core.settings import FrontendSettings

ORG_HEADER = "X-Organization-Id"
API_TIMEOUT_S = 10.0
DRAFT_PAGE_SIZE = 25
TIMELINE_PAGE_SIZE = 100  # the API's page cap (services/api/pagination.py)
DOCUMENT_PAGE_SIZE = 100


class ApiError(Exception):
    """A /v1 call that did not succeed: HTTP status, API error code and message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DraftSummary(_ApiModel):
    """One pending-queue row (GET /v1/drafts item)."""

    id: UUID
    job_id: UUID | None = None
    message_id: UUID | None = None
    thread_id: UUID | None = None
    mailbox_id: UUID | None = None
    category: str | None = None
    status: str
    subject: str | None = None
    confidence: float | None = None
    created_at: datetime | None = None


class DraftPage(_ApiModel):
    """A cursor page of drafts (R23.6)."""

    items: list[DraftSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class OriginalMessage(_ApiModel):
    """The inbound email the draft answers (the API's `original` object)."""

    id: UUID | None = Field(default=None, validation_alias=AliasChoices("message_id", "id"))
    sender_email: str | None = None
    sender_name: str | None = None
    subject: str = ""
    body_text: str = ""
    received_at: datetime | None = None


class CitedChunk(_ApiModel):
    """A knowledge chunk the draft cites (verified by packages/llm/citations.py, R16.5)."""

    citation_id: str
    chunk_id: str | None = None
    document_id: str | None = None
    title: str | None = None
    text: str | None = Field(default=None, validation_alias=AliasChoices("content", "text"))

    @model_validator(mode="before")
    @classmethod
    def title_from_headings(cls, data: Any) -> Any:
        """The API sends `heading_path` and `external_id`; the UI shows one title."""
        if isinstance(data, dict) and not data.get("title"):
            headings = [str(h) for h in data.get("heading_path") or [] if h]
            title = " › ".join(headings) or data.get("external_id")
            if title:
                data = {**data, "title": title}
        return data


class BusinessFactView(_ApiModel):
    """One [BUSINESS DATA] fact the draft was generated with (R13.5)."""

    entity: str
    reference: str | None = None
    status: str
    reason: str | None = None


class DraftDetail(DraftSummary):
    """GET /v1/drafts/{id}: everything a reviewer needs on one screen (R23.4)."""

    body: str = ""
    citation_mismatch: bool = False
    job_state: str | None = None
    dispatch_mode: str | None = None
    original_message: OriginalMessage | None = Field(
        default=None, validation_alias=AliasChoices("original", "original_message")
    )
    thread_summary: str | None = None
    # The API's `citations` are raw records; the chunks with text arrive as `cited_chunks`.
    citations: list[CitedChunk] = Field(
        default_factory=list, validation_alias=AliasChoices("cited_chunks")
    )
    business_facts: list[BusinessFactView] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def flatten_business_data(cls, data: Any) -> Any:
        """The API nests the facts under `business_data.facts`."""
        if isinstance(data, dict) and "business_facts" not in data:
            business = data.get("business_data") or {}
            data = {**data, "business_facts": business.get("facts") or []}
        return data


class SenderView(_ApiModel):
    email: str
    name: str | None = None


class MessageHeader(_ApiModel):
    """GET /v1/messages/{id}: what the timeline page shows about the email."""

    id: UUID
    subject: str = ""
    sender: SenderView | None = None
    received_at: datetime | None = None
    direction: str = "inbound"


class TimelineEvent(_ApiModel):
    """One processing_event row (R18.4, R23.5)."""

    id: int | None = None
    job_id: UUID | None = None
    event_type: str
    state_from: str | None = None
    state_to: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class MessageTimeline(_ApiModel):
    """GET /v1/messages/{id}/timeline (R23.5)."""

    message_id: UUID
    current_state: str | None = None
    total_events: int = 0
    events: list[TimelineEvent] = Field(default_factory=list)


class KnowledgeDocumentView(_ApiModel):
    """A knowledge document and its ingestion status (R9.10, R23.7)."""

    id: UUID
    title: str
    category: str | None = None
    version: int = 1
    status: str
    failure_reason: str | None = None
    updated_at: datetime | None = None


class DocumentPage(_ApiModel):
    items: list[KnowledgeDocumentView] = Field(default_factory=list)
    total_count: int = 0


class DocumentUpload(_ApiModel):
    """POST /v1/knowledge/documents 202 response."""

    document: KnowledgeDocumentView
    job_id: str


def build_http_client(
    settings: FrontendSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """httpx client for /v1 with the tenant header set once (R23.6)."""
    headers: dict[str, str] = {}
    if settings.organization_id is not None:
        headers[ORG_HEADER] = str(settings.organization_id)
    return httpx.AsyncClient(
        base_url=settings.api_base_url,
        headers=headers,
        timeout=API_TIMEOUT_S,
        transport=transport,
    )


def _error_from(response: httpx.Response) -> ApiError:
    code = "HTTP_ERROR"
    message = response.reason_phrase or "Request failed"
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("code") or code)
        message = str(payload.get("error") or payload.get("detail") or message)
    return ApiError(response.status_code, code, message)


class ReviewApiClient:
    """Typed /v1 calls used by the review pages (R23.4, R23.6)."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(502, "API_UNREACHABLE", f"The API could not be reached: {exc}") from exc
        if response.status_code >= 400:
            raise _error_from(response)
        if not response.content:
            return None
        return response.json()

    async def list_drafts(
        self,
        *,
        status: str = "draft",
        category: str | None = None,
        cursor: str | None = None,
        limit: int = DRAFT_PAGE_SIZE,
    ) -> DraftPage:
        params: dict[str, str | int] = {"status": status, "limit": limit}
        if category:
            params["category"] = category
        if cursor:
            params["cursor"] = cursor
        return DraftPage.model_validate(await self._request("GET", "/v1/drafts", params=params))

    async def get_draft(self, draft_id: UUID) -> DraftDetail:
        return DraftDetail.model_validate(await self._request("GET", f"/v1/drafts/{draft_id}"))

    async def update_draft_body(self, draft_id: UUID, body: str) -> None:
        await self._request("PATCH", f"/v1/drafts/{draft_id}", json={"body": body})

    async def approve_draft(self, draft_id: UUID, *, review_ms: int, reviewer: str | None) -> None:
        await self._request(
            "POST",
            f"/v1/drafts/{draft_id}/approve",
            json={"review_ms": review_ms, "reviewer": reviewer},
        )

    async def reject_draft(
        self, draft_id: UUID, *, review_ms: int, reviewer: str | None, comment: str | None
    ) -> None:
        await self._request(
            "POST",
            f"/v1/drafts/{draft_id}/reject",
            json={"review_ms": review_ms, "reviewer": reviewer, "comment": comment},
        )

    async def get_message(self, message_id: UUID) -> MessageHeader:
        return MessageHeader.model_validate(
            await self._request("GET", f"/v1/messages/{message_id}")
        )

    async def get_message_timeline(self, message_id: UUID) -> MessageTimeline:
        payload = await self._request(
            "GET", f"/v1/messages/{message_id}/timeline", params={"limit": TIMELINE_PAGE_SIZE}
        )
        return MessageTimeline.model_validate(payload)

    async def list_documents(self, *, limit: int = DOCUMENT_PAGE_SIZE) -> DocumentPage:
        payload = await self._request("GET", "/v1/knowledge/documents", params={"limit": limit})
        return DocumentPage.model_validate(payload)

    async def get_document(self, document_id: UUID) -> KnowledgeDocumentView:
        return KnowledgeDocumentView.model_validate(
            await self._request("GET", f"/v1/knowledge/documents/{document_id}")
        )

    async def upload_document(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        title: str | None,
        category: str | None,
    ) -> DocumentUpload:
        data = {k: v for k, v in (("title", title), ("category", category)) if v is not None}
        files = {"file": (filename, content, content_type)}
        payload = await self._request("POST", "/v1/knowledge/documents", data=data, files=files)
        return DocumentUpload.model_validate(payload)
