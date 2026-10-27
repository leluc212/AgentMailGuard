"""Run routes (design.md "Routes và htmx"; Requirements 2, 3, 5.8, 7, 20).

``POST /guard/run`` renders the result columns. In Replay_Mode every column comes from the
Replay_Store and no runner or model is ever called (Requirement 7.2). In Live_Mode it starts
one Run per preset and returns progress frames that poll ``GET /guard/progress/{run_id}``
every second until the Run ends (Requirement 20). ``GET /guard/run-status`` re-renders the
Run button when a toolbar control changes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from services.guard_playground.core.custom import split_subject
from services.guard_playground.core.limits import check_email, clean_chunks, limits_message
from services.guard_playground.core.models import (
    MODELS_BY_KEY,
    credential_present,
    missing_credential_message,
)
from services.guard_playground.core.replay import STATUS_UNAVAILABLE
from services.guard_playground.core.runner import RunState
from services.guard_playground.core.schema import PRESETS, ChunkInput, EmailInput, RunResult
from services.guard_playground.core.view_model import build_column
from services.guard_playground.routes.cases import CASE_NOT_FOUND
from services.guard_playground.routes.pages import (
    DEFAULT_MODELS,
    LIVE_DISABLED_REASON,
    _templates,
    model_label,
    requested_presets,
    run_disabled_reason,
)

runs_router = APIRouter()

CUSTOM_NEEDS_LIVE = "Your own email requires Live mode"
UNKNOWN_PRESET = "Unknown preset"
UNKNOWN_MODE = "Unknown mode"
UNKNOWN_MODEL = "Unknown draft model"
MODES = ("replay", "live")


def _fragment(text: str, status_code: int = 200) -> HTMLResponse:
    # Every text passed here is a fixed string or a value checked against a fixed list.
    return HTMLResponse(f'<p class="pg-alert" role="alert">{text}</p>', status_code=status_code)


def input_difference(results: list[RunResult]) -> str | None:
    """Name the differing input ("email" or "KB chunks") of a comparison (Requirement 3.4)."""
    if len(results) < 2:
        return None
    first = results[0].email
    for other in results[1:]:
        if (other.email.subject, other.email.body) != (first.subject, first.body):
            return "email"
        if other.email.chunks != first.chunks:
            return "KB chunks"
    return None


def replay_badge(entry: RunResult) -> str:
    return (
        f"REPLAY · generated {entry.generated_at:%Y-%m-%d %H:%M} · {model_label(entry.draft_model)}"
    )


def live_badge(model_key: str) -> str:
    m = MODELS_BY_KEY[model_key]
    return f"LIVE · {m.label} ({m.route_label})"


def _results(request: Request, **context: Any) -> Response:
    values = {"columns": [], "replay_badge": None, "results_message": None, **context}
    return _templates(request).TemplateResponse(request, "guard/_results.html", values)


def _custom_context(subject: str, body: str, chunks: list[str]) -> dict[str, Any]:
    return {"subject": subject, "body": body, "chunks": chunks}


def _email_from_case(request: Request, case_id: str) -> EmailInput | None:
    case = request.app.state.cases.get(case_id)
    if case is None:
        return None
    return EmailInput(
        subject=case.subject,
        body=case.body,
        chunks=[ChunkInput(chunk_id=cid, content=content) for cid, content in case.chunks],
        source="pinned" if case.origin == "pinned" else "fixture",
        case_id=case.case_id,
    )


def _render_custom_error(request: Request, custom: dict[str, Any], error: str) -> Response:
    """Validation failure: re-render the form (values kept) out-of-band, start no Run."""
    state = request.app.state
    form = (
        _templates(request)
        .get_template("guard/_custom_form.html")
        .render(
            custom=custom,
            samples=state.samples,
            custom_error=error,
            mode="live",
        )
    )
    oob = form.replace(
        '<section id="custom-email"', '<section id="custom-email" hx-swap-oob="true"', 1
    )
    return HTMLResponse(f'<p class="pg-alert" role="alert">{error}</p>{oob}')


@runs_router.post("/guard/run", response_class=HTMLResponse)
async def run(
    request: Request,
    mode: str = Form("replay"),
    model_key: str = Form(DEFAULT_MODELS[0][0]),
    compare: str = Form("1"),
    left_preset: str = Form("C0"),
    right_preset: str = Form("C3"),
    single_preset: str = Form("C3"),
    case_id: str = Form(""),
    input_source: str = Form("case"),
    custom_subject: str = Form(""),
    custom_body: str = Form(""),
    custom_chunk: list[str] | None = Form(None),  # noqa: B008 (FastAPI form declaration)
) -> Response:
    state = request.app.state
    if mode not in MODES:
        return _fragment(UNKNOWN_MODE, 400)
    if model_key not in MODELS_BY_KEY:
        return _fragment(UNKNOWN_MODEL, 400)
    is_compare = compare != "0"
    presets = requested_presets(is_compare, left_preset, right_preset, single_preset)
    if any(p not in PRESETS for p in presets):
        return _fragment(UNKNOWN_PRESET, 400)
    use_custom = input_source == "custom" or not case_id.strip()

    if mode == "live":
        return await _run_live(
            request,
            presets,
            model_key,
            is_compare,
            case_id=None if use_custom else case_id,
            custom=(custom_subject, custom_body, list(custom_chunk or [])),
        )

    # Replay_Mode: Replay_Store only, zero runner and model calls (Requirement 7.2).
    if use_custom:
        return _results(request, results_message=CUSTOM_NEEDS_LIVE)
    if state.cases.get(case_id) is None:
        return _fragment(CASE_NOT_FOUND, 404)

    entries = [state.replay.get(case_id, p, model_key) for p in presets]
    found = [e for e in entries if e is not None]
    differs = input_difference(found)
    if differs is not None:
        return _results(
            request,
            results_message=f"The compared runs use different {differs}; nothing was run.",
        )

    compare_with_c0 = is_compare and "C0" in presets
    columns: list[Any] = []
    for prefix, preset, entry in zip(("a", "b"), presets, entries, strict=False):
        if entry is None:
            columns.append({"error": STATUS_UNAVAILABLE, "preset": preset})
        else:
            columns.append(
                build_column(entry, state.policy, id_prefix=prefix, compare_with_c0=compare_with_c0)
            )
    badge = replay_badge(found[0]) if found else None
    return _results(request, columns=columns, replay_badge=badge)


async def _run_live(
    request: Request,
    presets: list[str],
    model_key: str,
    is_compare: bool,
    *,
    case_id: str | None,
    custom: tuple[str, str, list[str]],
) -> Response:
    state = request.app.state
    if not state.settings.live_enabled:
        return _results(request, results_message=LIVE_DISABLED_REASON)
    if not state.guard_available:
        return _results(request, results_message="Guard library unavailable")
    model = MODELS_BY_KEY[model_key]
    if state.runner is None and not credential_present(model):
        return _results(request, results_message=missing_credential_message(model))

    if case_id is not None:
        email = _email_from_case(request, case_id)
        if email is None:
            return _fragment(CASE_NOT_FOUND, 404)
    else:
        subject, body, raw_chunks = custom
        subject, body = split_subject(subject, body)
        chunks = clean_chunks(raw_chunks)
        message = limits_message(check_email(subject, body, chunks))
        if message is not None:
            return _render_custom_error(
                request, _custom_context(subject, body, raw_chunks), message
            )
        email = EmailInput(
            subject=subject,
            body=body,
            chunks=[ChunkInput(chunk_id=f"kb-{i}", content=c) for i, c in enumerate(chunks, 1)],
            source="custom",
            case_id=None,
        )

    registry = state.registry
    run_ids = [
        registry.start(email, p, model_key, l1_model_path=state.settings.l1_model_path)
        for p in presets
    ]
    for rid in run_ids:
        run_state = registry.get(rid)
        if run_state is not None:
            run_state.case_id = case_id
    compare_flag = "1" if is_compare and "C0" in presets else "0"
    frames = [
        {"run_id": rid, "preset": p, "prefix": prefix, "stage": "Starting", "elapsed": 0}
        for rid, p, prefix in zip(run_ids, presets, ("a", "b"), strict=False)
    ]
    return _templates(request).TemplateResponse(
        request,
        "guard/_live_frames.html",
        {"frames": frames, "badge": live_badge(model_key), "compare_c0": compare_flag},
    )


def _state_column(request: Request, state: RunState, prefix: str, compare_c0: bool) -> Any:
    if state.status == "done" and state.result is not None:
        return build_column(
            state.result, request.app.state.policy, id_prefix=prefix, compare_with_c0=compare_c0
        )
    if state.status == "timeout":
        message = state.error_message or "Run timed out after 120 seconds"
    else:
        category = state.error_category or "provider error"
        message = f"{category}: {state.error_message or ''}".rstrip(": ")
    column: dict[str, Any] = {"error": message, "preset": state.preset}
    replay = request.app.state.replay
    if state.case_id and not replay.unavailable(state.case_id, state.preset, state.model_key):
        # Requirement 7.6: offer the recorded result of the same case and preset.
        column["replay_url"] = (
            f"/guard/replay-column?case_id={state.case_id}&preset={state.preset}"
            f"&model_key={state.model_key}&prefix={prefix}&c0={'1' if compare_c0 else '0'}"
        )
    return column


@runs_router.get("/guard/replay-column", response_class=HTMLResponse)
async def replay_column(
    request: Request, case_id: str, preset: str, model_key: str, prefix: str = "a", c0: str = "0"
) -> Response:
    """One Replay column, offered after a failed or timed-out Live run (Requirement 7.6)."""
    state = request.app.state
    entry = state.replay.get(case_id, preset, model_key)
    if entry is None:
        column: Any = {"error": STATUS_UNAVAILABLE, "preset": preset}
    else:
        column = build_column(entry, state.policy, id_prefix=prefix, compare_with_c0=c0 == "1")
    return _templates(request).TemplateResponse(request, "guard/_column.html", {"column": column})


@runs_router.get("/guard/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str, prefix: str = "a", c0: str = "0") -> Response:
    """One Live Run: progress frame while running, its result column once it ends."""
    registry = request.app.state.registry
    state = registry.get(run_id)
    if state is None:
        return HTMLResponse(
            '<div class="column column--error" role="alert">Run not found</div>', status_code=200
        )
    if state.status == "running":
        frame = {
            "run_id": run_id,
            "preset": state.preset,
            "prefix": prefix,
            "stage": state.stage,
            "elapsed": state.elapsed_s,
        }
        return _templates(request).TemplateResponse(
            request, "guard/_progress.html", {"frame": frame, "compare_c0": c0}
        )
    column = _state_column(request, state, prefix, c0 == "1")
    return _templates(request).TemplateResponse(request, "guard/_column.html", {"column": column})


@runs_router.get("/guard/samples/{key}", response_class=HTMLResponse)
async def sample(request: Request, key: str) -> Response:
    """Fill the Custom_Email form with one real benchmark case, verbatim (Requirement 2.8)."""
    state = request.app.state
    found = next((s for s in state.samples if s.key == key), None)
    if found is None:
        return _fragment("Sample not found", 404)
    custom = _custom_context(found.subject, found.body, list(found.chunks))
    return _templates(request).TemplateResponse(
        request,
        "guard/_custom_form.html",
        {"custom": custom, "samples": state.samples, "custom_error": None, "mode": "live"},
    )


@runs_router.get("/guard/run-status", response_class=HTMLResponse)
async def run_status(
    request: Request,
    mode: str = "replay",
    model_key: str = DEFAULT_MODELS[0][0],
    compare: str = "1",
    left_preset: str = "C0",
    right_preset: str = "C3",
    single_preset: str = "C3",
    case_id: str = "",
    input_source: str = "case",
) -> Response:
    """The Run button for the current toolbar values (Requirements 2.6, 7.13, 7.16)."""
    state = request.app.state
    case = state.cases.get(case_id) if case_id else None
    reason = run_disabled_reason(
        request,
        case,
        mode=mode,
        presets=requested_presets(compare != "0", left_preset, right_preset, single_preset),
        model_key=model_key,
        custom=input_source == "custom",
    )
    context = {"run_disabled_reason": reason, "guard_available": state.guard_available}
    return _templates(request).TemplateResponse(request, "guard/_run_status.html", context)
