"""HTML pages of the Playground.

``GET /guard`` renders the full page; the case and run fragments live in ``routes/cases.py``
and ``routes/runs.py``. The context keys are documented in ``templates/guard/playground.html``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from services.guard_playground.core.cases import ALL, CASE_KINDS, Case, demo_set, filter_cases
from services.guard_playground.core.models import (
    MODELS_BY_KEY,
    credential_present,
    missing_credential_message,
    model_choices,
)
from services.guard_playground.core.replay import STATUS_UNAVAILABLE
from services.guard_playground.core.schema import PRESETS
from services.guard_playground.guard_import import import_guard

pages_router = APIRouter()

# Draft_Model choices (key, label, route_label) in the fixed order of Requirement 7.14.
DEFAULT_MODELS: tuple[tuple[str, str, str], ...] = tuple(model_choices())
LIVE_DISABLED_REASON = (
    "Live mode is off: start the Playground with run_playground.ps1 -Live or the Docker service"
)
CHOOSE_CASE = "Choose a case first"
CUSTOM_REPLAY = "Your own email requires Live mode"


def model_label(model_key: str) -> str:
    """Display label of a Draft_Model key; unknown keys are shown as they are."""
    for key, label, _route in DEFAULT_MODELS:
        if key == model_key:
            return label
    return model_key


def requested_presets(compare: bool, left: str, right: str, single: str) -> list[str]:
    """Presets of one Run request: left and right in comparison mode, else the single one."""
    return [left, right] if compare else [single]


def run_disabled_reason(
    request: Request,
    case: Case | None,
    *,
    mode: str,
    presets: Sequence[str],
    model_key: str,
    custom: bool = False,
) -> str | None:
    """Why the Run button is disabled, or None when it can be pressed (Req 2.6, 7.13, 7.16)."""
    state = request.app.state
    if mode == "live":
        if not state.settings.live_enabled:
            return LIVE_DISABLED_REASON
        model = MODELS_BY_KEY.get(model_key)
        if model is not None and state.runner is None and not credential_present(model):
            return missing_credential_message(model)
        if custom:
            return None
    elif custom:
        return CUSTOM_REPLAY
    if case is None:
        return CHOOSE_CASE
    if mode == "live":
        return None
    replay = state.replay
    if any(replay.unavailable(case.case_id, p, model_key) for p in presets):
        return STATUS_UNAVAILABLE
    return None


def _tour_json(tour: Any) -> Markup:
    """The tour steps as JSON for a <script type="application/json"> block, safe in HTML."""
    if tour is None or not tour.valid:
        return Markup("[]")
    steps = [
        {
            "case_id": s.case_id,
            "subject": s.subject if s.case_id is None else "",
            "body": s.body if s.case_id is None else "",
            "presets": list(s.presets),
            "mode": s.mode,
            "talking_point": s.talking_point,
        }
        for s in tour.steps
    ]
    text = json.dumps(steps, ensure_ascii=False)
    # "<" cannot close the <script> element once escaped as a JSON unicode escape.
    return Markup(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _templates(request: Request) -> Jinja2Templates:
    templates: Jinja2Templates = request.app.state.templates
    return templates


def _refresh_guard_status(request: Request) -> None:
    """Retry the guard import when it failed at start-up (Requirement 9.8: start or request)."""
    state = request.app.state
    if not state.guard_available:
        status = import_guard(state.settings.guard_root)
        state.guard_available = status.available
        state.guard_error = status.error


def default_page_context(request: Request) -> dict[str, Any]:
    """Defaults of the Playground page: Replay, C0 vs C3, gpt-4o-mini, no filter, no run."""
    state = request.app.state
    catalog = state.cases
    cases = filter_cases(catalog, ALL, ALL)
    live_enabled = bool(state.settings.live_enabled)
    return {
        "nav": "playground",
        "guard_available": state.guard_available,
        "guard_error": state.guard_error,
        "catalog_error": catalog.error,
        "excluded_lines": catalog.excluded_lines,
        "cases": cases,
        "count": len(cases),
        "kind": ALL,
        "source": ALL,
        "kinds": [ALL, *CASE_KINDS],
        "sources": [ALL, *catalog.sources],
        "demo_ids": set(demo_set(state.settings)),
        "selected": None,
        "mode": "replay",
        "live_enabled": live_enabled,
        "live_disabled_reason": None if live_enabled else LIVE_DISABLED_REASON,
        "presets": list(PRESETS),
        "compare": True,
        "left_preset": "C0",
        "right_preset": "C3",
        "single_preset": "C3",
        "models": list(DEFAULT_MODELS),
        "model_key": DEFAULT_MODELS[0][0],
        "run_disabled_reason": CHOOSE_CASE,
        "columns": [],
        "replay_badge": None,
        "kpi": getattr(state, "kpi", None),
        "samples": getattr(state, "samples", []),
        "tour": getattr(state, "tour", None),
        "tour_json": _tour_json(getattr(state, "tour", None)),
        "custom": None,
        "custom_error": None,
    }


@pages_router.get("/guard", response_class=HTMLResponse)
async def playground_page(request: Request, case_id: str | None = None) -> Response:
    _refresh_guard_status(request)
    context = default_page_context(request)
    selected = request.app.state.cases.get(case_id) if case_id else None
    context["selected"] = selected
    context["run_disabled_reason"] = run_disabled_reason(
        request,
        selected,
        mode=context["mode"],
        presets=requested_presets(
            context["compare"],
            context["left_preset"],
            context["right_preset"],
            context["single_preset"],
        ),
        model_key=context["model_key"],
    )
    return _templates(request).TemplateResponse(request, "guard/playground.html", context)
