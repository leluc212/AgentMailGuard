"""Sample_Case fragments (design.md "Routes và htmx"; Requirements 1.2-1.4, 1.6, 7.13).

``GET /guard/cases`` returns the filtered list, ``GET /guard/cases/{id}`` the case detail plus
an out-of-band ``#run-status`` so the Run button reflects the Replay availability of the
newly selected case.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from services.guard_playground.core.cases import ALL, demo_set, filter_cases
from services.guard_playground.routes.pages import (
    DEFAULT_MODELS,
    _templates,
    requested_presets,
    run_disabled_reason,
)

cases_router = APIRouter()

CASE_NOT_FOUND = "Case not found"


@cases_router.get("/guard/cases", response_class=HTMLResponse)
async def case_list(request: Request, kind: str = ALL, source: str = ALL) -> Response:
    """Filtered case list; unknown filter values simply match nothing (count 0)."""
    state = request.app.state
    cases = filter_cases(state.cases, kind or ALL, source or ALL)
    context = {
        "cases": cases,
        "count": len(cases),
        "demo_ids": set(demo_set(state.settings)),
        "selected": None,
    }
    return _templates(request).TemplateResponse(request, "guard/_case_list.html", context)


@cases_router.get("/guard/cases/{case_id}", response_class=HTMLResponse)
async def case_detail(request: Request, case_id: str) -> Response:
    state = request.app.state
    case = state.cases.get(case_id)
    if case is None:
        return HTMLResponse(
            f'<p class="pg-alert" role="alert">{CASE_NOT_FOUND}</p>', status_code=404
        )
    # The toolbar defaults (Replay, C0 vs C3, first Draft_Model) decide the button state; a
    # later change of a toolbar control refreshes it through GET /guard/run-status.
    reason = run_disabled_reason(
        request,
        case,
        mode="replay",
        presets=requested_presets(True, "C0", "C3", "C3"),
        model_key=DEFAULT_MODELS[0][0],
    )
    presets = ["C0", "C3"]
    bench_rows = state.benchmark.table(case.case_id, presets) if case.origin == "pinned" else []
    context = {
        "selected": case,
        "bench_rows": bench_rows,
        "bench_presets": presets,
        "run_disabled_reason": reason,
        "guard_available": state.guard_available,
        "oob": True,
    }
    return _templates(request).TemplateResponse(request, "guard/_case_selected.html", context)
