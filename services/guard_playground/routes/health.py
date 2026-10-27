"""Liveness and readiness routes (design.md, Routes table)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

health_router = APIRouter()


@health_router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Always 200 while the process serves requests, guard or no guard (Requirement 9.8)."""
    return {"status": "ok", "service": "guard-playground"}


@health_router.get("/readyz")
async def readyz(request: Request) -> dict[str, Any]:
    """Report what the demo can use: guard import, loaded cases, Replay_Store entries.

    Always 200 so the Fallback_Launcher can wait on it; Replay_Mode works without the guard.
    """
    state = request.app.state
    guard_available = bool(state.guard_available)
    case_count = int(state.case_count)
    replay_entries = int(state.replay_entry_count)
    return {
        "status": "ready" if guard_available else "degraded",
        "guard_importable": guard_available,
        "guard_error": state.guard_error,
        "case_count": case_count,
        "replay_entry_count": replay_entries,
        "live_enabled": bool(state.settings.live_enabled),
    }
