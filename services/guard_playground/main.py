"""Guard Playground service entrypoint (design.md; Requirements 9.1, 9.8, 10.1).

A separate FastAPI + Jinja2 + htmx app on 127.0.0.1:3010 that calls the guard library
in-process. It has no login, so it must only be published on loopback (ADR-0009).
Building the app never fails because data files or the guard are missing: those states are
recorded on ``app.state`` and shown on the pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.guard_playground.core.benchmark import BenchmarkIndex
from services.guard_playground.core.cases import load_cases
from services.guard_playground.core.custom import load_samples
from services.guard_playground.core.kpi import load_kpi
from services.guard_playground.core.policy_text import PolicyText
from services.guard_playground.core.redact import install_log_redaction
from services.guard_playground.core.replay import ReplayStore
from services.guard_playground.core.runner import RunRegistry, execute
from services.guard_playground.core.tour import load_tour
from services.guard_playground.guard_import import import_guard
from services.guard_playground.routes.cases import cases_router
from services.guard_playground.routes.external import external_router
from services.guard_playground.routes.health import health_router
from services.guard_playground.routes.pages import pages_router
from services.guard_playground.routes.results import results_router
from services.guard_playground.routes.runs import runs_router
from services.guard_playground.settings import PlaygroundSettings

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
# Shared with the Review_UI (review.css, htmx.min.js); served read-only.
SHARED_STATIC_DIR = BASE_DIR.parent / "frontend" / "static"


def create_app(settings: PlaygroundSettings | None = None, *, runner: Any = None) -> FastAPI:
    """Build the Playground; ``runner`` lets tests replace the guard runner with a stub."""
    active = settings or PlaygroundSettings()
    install_log_redaction()  # Requirement 8.4: no secret value reaches a log line
    guard = import_guard(active.guard_root)

    app = FastAPI(
        title="AgentMailGuard Playground",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = active
    app.state.runner = runner
    app.state.guard_available = guard.available
    app.state.guard_error = guard.error
    # Case catalog and Replay_Store are loaded once (Requirements 1, 7.12).
    app.state.cases = load_cases(active)
    app.state.case_count = len(app.state.cases)
    app.state.replay = ReplayStore.load(active.data_dir / "replay")
    app.state.replay_entry_count = len(app.state.replay)
    # Rule reasons for the Outcome_Banner and decisions (Requirement 5.3), read once.
    app.state.policy = PolicyText.from_guard_root(active.guard_root)
    # KPI_Strip from the three 1,800-case runs, read once (Requirement 18).
    app.state.kpi = load_kpi(active.results_root)
    # One-click Custom_Email samples, verbatim from cases.jsonl (D12, Requirement 2.8).
    app.state.samples = load_samples(active.data_dir, app.state.cases)
    # Demo_Tour, validated once at start-up (Requirement 17.8).
    app.state.tour = load_tour(active.data_dir, app.state.cases)
    # Recorded benchmark rows of a pinned case, indexed lazily, read-only (Requirement 6).
    app.state.benchmark = BenchmarkIndex(active.results_root)
    # Live runs in memory (TD8). ``runner`` replaces the real executor in tests.
    app.state.registry = RunRegistry(runner or execute, timeout_s=active.run_timeout_s)
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.mount("/static", StaticFiles(directory=SHARED_STATIC_DIR, check_dir=False), name="static")
    app.mount("/pg-static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="pg-static")
    app.include_router(health_router)
    app.include_router(pages_router)
    app.include_router(cases_router)
    app.include_router(runs_router)
    app.include_router(results_router)
    app.include_router(external_router)
    return app


app = create_app()
