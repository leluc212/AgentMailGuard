"""Pending-draft queue, draft review and decisions (R23.4, R16.6, R16.7; design.md §5.8).

Every read and decision is a /v1 call. A decision carries the review time measured in the
browser (review.js, sent as review_ms); without JavaScript it falls back to the time since the
page was rendered. htmx requests get the re-rendered draft panel plus an out-of-band message
for the live region; plain form posts get a 303 back to the draft with a notice.
"""

from __future__ import annotations

import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from services.frontend.annotate import annotate_draft
from services.frontend.api_client import DraftDetail
from services.frontend.web import (
    api_client,
    is_htmx,
    optional_text,
    require_organization,
    templates,
)

NOTICES = {
    "saved": "Changes saved.",
    "approved": "Draft approved.",
    "rejected": "Draft rejected.",
}

drafts_router = APIRouter(dependencies=[Depends(require_organization)])

FormText = Annotated[str | None, Form()]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def normalize_body(text: str) -> str:
    """Browsers submit textarea line breaks as CRLF; drafts are stored with LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _non_negative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def resolve_review_ms(review_ms: str | None, rendered_at_ms: str | None, *, now: int) -> int:
    """Review time in ms: the browser's measurement, else time since render, else 0 (R16.7)."""
    measured = _non_negative_int(review_ms)
    if measured is not None:
        return measured
    rendered = _non_negative_int(rendered_at_ms)
    if rendered is not None:
        return max(0, now - rendered)
    return 0


def _detail_context(draft: DraftDetail, **extra: Any) -> dict[str, Any]:
    return {
        "draft": draft,
        "annotated": annotate_draft(draft.body, draft.citations, draft.business_facts),
        "rendered_at_ms": now_ms(),
        "nav": "drafts",
        **extra,
    }


async def _after_decision(request: Request, draft_id: UUID, notice: str) -> Response:
    if not is_htmx(request):
        return RedirectResponse(f"/drafts/{draft_id}?notice={notice}", status_code=303)
    draft = await api_client(request).get_draft(draft_id)
    return templates(request).TemplateResponse(
        request, "drafts/_panel.html", _detail_context(draft, status_message=NOTICES[notice])
    )


@drafts_router.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse("/drafts", status_code=303)


@drafts_router.get("/drafts", response_class=HTMLResponse)
async def draft_queue(
    request: Request, category: str | None = None, cursor: str | None = None
) -> Response:
    """Pending-draft queue (R23.4), one cursor page at a time (R23.6)."""
    chosen = optional_text(category)
    page = await api_client(request).list_drafts(category=chosen, cursor=optional_text(cursor))
    return templates(request).TemplateResponse(
        request, "drafts/queue.html", {"page": page, "category": chosen, "nav": "drafts"}
    )


@drafts_router.get("/drafts/{draft_id}", response_class=HTMLResponse)
async def draft_detail(request: Request, draft_id: UUID, notice: str | None = None) -> Response:
    """Original email, thread summary, facts and the annotated draft (R23.4)."""
    draft = await api_client(request).get_draft(draft_id)
    context = _detail_context(draft, page_notice=NOTICES.get(notice or ""))
    return templates(request).TemplateResponse(request, "drafts/detail.html", context)


@drafts_router.post("/drafts/{draft_id}/edit")
async def save_edit(request: Request, draft_id: UUID, body: Annotated[str, Form()]) -> Response:
    await api_client(request).update_draft_body(draft_id, normalize_body(body))
    return await _after_decision(request, draft_id, "saved")


@drafts_router.post("/drafts/{draft_id}/approve")
async def approve(
    request: Request,
    draft_id: UUID,
    body: FormText = None,
    original_body: FormText = None,
    review_ms: FormText = None,
    rendered_at_ms: FormText = None,
    reviewer: FormText = None,
) -> Response:
    """Save an edited body first (PATCH), then approve (R16.6, R16.7)."""
    client = api_client(request)
    if body is not None and original_body is not None:
        edited = normalize_body(body)
        if edited != normalize_body(original_body):
            await client.update_draft_body(draft_id, edited)
    await client.approve_draft(
        draft_id,
        review_ms=resolve_review_ms(review_ms, rendered_at_ms, now=now_ms()),
        reviewer=optional_text(reviewer),
    )
    return await _after_decision(request, draft_id, "approved")


@drafts_router.post("/drafts/{draft_id}/reject")
async def reject(
    request: Request,
    draft_id: UUID,
    review_ms: FormText = None,
    rendered_at_ms: FormText = None,
    reviewer: FormText = None,
    comment: FormText = None,
) -> Response:
    await api_client(request).reject_draft(
        draft_id,
        review_ms=resolve_review_ms(review_ms, rendered_at_ms, now=now_ms()),
        reviewer=optional_text(reviewer),
        comment=optional_text(comment),
    )
    return await _after_decision(request, draft_id, "rejected")
