"""Job timeline and current state for one message (R23.5; design.md §5.8 "Review UI").

Reads processing_event through GET /v1/messages/{id}/timeline. While the job is not in a
terminal state the events fragment re-polls every 5 s (htmx), so a reviewer who approved a
draft can watch DISPATCHED -> COMPLETED without reloading.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from packages.domain.state_machine import JobState
from services.frontend.web import api_client, is_htmx, require_organization, templates

TERMINAL_JOB_STATES = frozenset({JobState.COMPLETED.value, JobState.DEAD_LETTER.value})
DETAIL_KEYS = ("reason", "error", "category", "priority", "model_tier", "replayed")

timeline_router = APIRouter(dependencies=[Depends(require_organization)])


@timeline_router.get("/messages/{message_id}/timeline", response_class=HTMLResponse)
async def message_timeline(request: Request, message_id: UUID) -> Response:
    client = api_client(request)
    timeline = await client.get_message_timeline(message_id)
    context: dict[str, Any] = {
        "timeline": timeline,
        "live": timeline.current_state not in TERMINAL_JOB_STATES,
        "detail_keys": DETAIL_KEYS,
        "nav": "drafts",
    }
    if is_htmx(request):
        return templates(request).TemplateResponse(request, "timeline/_events.html", context)
    context["message"] = await client.get_message(message_id)
    return templates(request).TemplateResponse(request, "timeline/message.html", context)
