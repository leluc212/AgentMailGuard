"""Knowledge upload with per-document ingestion status (R23.7, R9.10; design.md §5.8).

Uploads are forwarded to POST /v1/knowledge/documents. Each row whose status is not final
re-polls GET /v1/knowledge/documents/{id} every 3 s and announces the final status in the
live region.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from services.frontend.api_client import ApiError
from services.frontend.web import (
    api_client,
    is_htmx,
    optional_text,
    require_organization,
    templates,
)

TERMINAL_DOCUMENT_STATUSES = frozenset({"active", "failed", "superseded"})
UPLOADED_NOTICE = "Upload accepted. Ingestion status updates below."

knowledge_router = APIRouter(dependencies=[Depends(require_organization)])


@knowledge_router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, notice: str | None = None) -> Response:
    page = await api_client(request).list_documents()
    return templates(request).TemplateResponse(
        request,
        "knowledge/index.html",
        {
            "page": page,
            "terminal": TERMINAL_DOCUMENT_STATUSES,
            "nav": "knowledge",
            "page_notice": UPLOADED_NOTICE if notice == "uploaded" else None,
        },
    )


@knowledge_router.post("/knowledge")
async def upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    category: Annotated[str | None, Form()] = None,
) -> Response:
    content = await file.read()
    if not content:
        raise ApiError(400, "EMPTY_FILE", "Choose a non-empty file to upload.")
    client = api_client(request)
    result = await client.upload_document(
        filename=file.filename or "document",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        title=optional_text(title),
        category=optional_text(category),
    )
    if not is_htmx(request):
        return RedirectResponse("/knowledge?notice=uploaded", status_code=303)
    page = await client.list_documents()
    message = f"Upload accepted: {result.document.title} is {result.document.status}."
    return templates(request).TemplateResponse(
        request,
        "knowledge/_documents.html",
        {"page": page, "terminal": TERMINAL_DOCUMENT_STATUSES, "status_message": message},
    )


@knowledge_router.get("/knowledge/documents/{document_id}/status", response_class=HTMLResponse)
async def document_status(request: Request, document_id: UUID) -> Response:
    doc = await api_client(request).get_document(document_id)
    finished = doc.status in TERMINAL_DOCUMENT_STATUSES
    return templates(request).TemplateResponse(
        request,
        "knowledge/_row.html",
        {
            "doc": doc,
            "terminal": TERMINAL_DOCUMENT_STATUSES,
            "row_notice": f"{doc.title}: {doc.status}" if finished and is_htmx(request) else None,
        },
    )
