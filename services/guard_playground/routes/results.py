"""Results_Page and figure files (Requirement 18.12-18.15). Read-only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from services.guard_playground.core.kpi import ALLOWED_FIGURE_FILES, FIGURES
from services.guard_playground.routes.pages import _templates

results_router = APIRouter()

_MEDIA = {"png": "image/png", "pdf": "application/pdf"}


@results_router.get("/guard/results", response_class=HTMLResponse)
async def results_page(request: Request) -> Response:
    state = request.app.state
    figures_dir = state.settings.figures_dir
    figures = [
        (f, (figures_dir / f"{f.stem}.png").is_file(), (figures_dir / f"{f.stem}.pdf").is_file())
        for f in FIGURES
    ]
    context = {
        "nav": "results",
        "kpi": state.kpi,
        "figures": figures,
        "provenance": state.kpi.provenance,
    }
    return _templates(request).TemplateResponse(request, "guard/results.html", context)


@results_router.get("/guard/figures/{name}")
async def figure_file(request: Request, name: str) -> Response:
    """Only the 12 known figure files are served; any other name is 404 (no path traversal)."""
    if name not in ALLOWED_FIGURE_FILES:
        raise HTTPException(status_code=404, detail="figure not available")
    path = request.app.state.settings.figures_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="figure not available")
    return FileResponse(path, media_type=_MEDIA[name.rsplit(".", 1)[1]])
