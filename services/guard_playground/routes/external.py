"""Upload and Batch_Run of External_Data (Requirement 21). Memory only, Live_Mode only."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from services.guard_playground.core.external import MAX_BYTES, ExternalEmail, parse_upload
from services.guard_playground.core.models import MODELS_BY_KEY
from services.guard_playground.core.schema import PRESETS, ChunkInput, EmailInput
from services.guard_playground.core.view_model import (
    BANNER_TEXT,
    banner_key,
    build_column,
    deciding_layer,
)
from services.guard_playground.routes.pages import _templates, requested_presets

external_router = APIRouter()
EXTERNAL_NEEDS_LIVE = "External data requires Live mode"


@dataclass
class Batch:
    batch_id: str
    emails: list[ExternalEmail]
    presets: list[str]
    model_key: str
    run_ids: dict[tuple[int, str], str] = field(default_factory=dict)  # (position, preset)
    stopped: bool = False
    task: asyncio.Task[None] | None = None

    @property
    def total(self) -> int:
        return len(self.emails) * len(self.presets)


def _email_input(item: ExternalEmail) -> EmailInput:
    return EmailInput(
        subject=item.subject,
        body=item.body,
        chunks=[ChunkInput(chunk_id=f"kb-{i}", content=c) for i, c in enumerate(item.chunks, 1)],
        source="external",
        case_id=None,
    )


def _alert(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f'<p class="pg-alert" role="alert">{text}</p>', status_code=status)


@external_router.post("/guard/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...)) -> Response:  # noqa: B008
    state = request.app.state
    if not state.settings.live_enabled:
        return _alert(EXTERNAL_NEEDS_LIVE)
    data = await file.read(MAX_BYTES + 1)
    parsed = parse_upload(file.filename or "", data)
    if parsed.error:
        return _templates(request).TemplateResponse(
            request, "guard/_upload_result.html", {"error": parsed.error, "parsed": None}
        )
    upload_id = uuid.uuid4().hex
    state.uploads = {upload_id: parsed.emails}  # one upload kept at a time, in memory only
    context: dict[str, Any] = {"error": None, "parsed": parsed, "upload_id": upload_id}
    if len(parsed.emails) == 1:
        item = parsed.emails[0]
        context["custom"] = {
            "subject": item.subject,
            "body": item.body,
            "chunks": list(item.chunks),
        }
        context["samples"] = state.samples
        context["mode"] = "live"
    return _templates(request).TemplateResponse(request, "guard/_upload_result.html", context)


@external_router.post("/guard/batch", response_class=HTMLResponse)
async def start_batch(
    request: Request,
    upload_id: str = Form(...),
    model_key: str = Form("gpt-4o-mini"),
    compare: str = Form("1"),
    left_preset: str = Form("C0"),
    right_preset: str = Form("C3"),
    single_preset: str = Form("C3"),
) -> Response:
    state = request.app.state
    emails = getattr(state, "uploads", {}).get(upload_id)
    if not state.settings.live_enabled:
        return _alert(EXTERNAL_NEEDS_LIVE)
    if emails is None:
        return _alert("Upload not found; upload the file again", 404)
    presets = requested_presets(compare != "0", left_preset, right_preset, single_preset)
    if model_key not in MODELS_BY_KEY or any(p not in PRESETS for p in presets):
        return _alert("Unknown preset or model", 400)
    batch = Batch(uuid.uuid4().hex, emails, presets, model_key)
    registry = state.registry
    gate = asyncio.Semaphore(state.settings.batch_concurrency)  # at most 2 Runs at a time

    async def work() -> None:
        for item in emails:
            for preset in presets:
                if batch.stopped:
                    return
                await gate.acquire()
                if batch.stopped:
                    gate.release()
                    return
                rid = registry.start(
                    _email_input(item),
                    preset,
                    model_key,
                    l1_model_path=state.settings.l1_model_path,
                )
                batch.run_ids[(item.position, preset)] = rid
                run_state = registry.get(rid)

                async def release(task: Any) -> None:
                    try:
                        await task
                    finally:
                        gate.release()

                asyncio.create_task(release(run_state.task))

    batch.task = asyncio.create_task(work())
    state.batches = {batch.batch_id: batch}
    return _batch_view(request, batch)


def _summary(
    request: Request, batch: Batch
) -> tuple[int, list[dict[str, Any]], dict[str, dict[str, int]]]:
    registry = request.app.state.registry
    finished = 0
    rows: list[dict[str, Any]] = []
    totals: dict[str, dict[str, int]] = {p: {} for p in batch.presets}
    for item in batch.emails:
        cells = []
        for preset in batch.presets:
            rid = batch.run_ids.get((item.position, preset))
            state = registry.get(rid) if rid else None
            if state is None or state.status == "running":
                cells.append({"text": "running" if state else "waiting"})
                continue
            finished += 1
            if state.status == "done" and state.result is not None:
                r = state.result
                text = BANNER_TEXT[banner_key(r)]
                rule = r.final.matched_rule_id if r.final else "—"
                layer = deciding_layer(r)
                cells.append(
                    {
                        "text": text,
                        "rule": rule,
                        "layer": layer.layer if layer else "—",
                        "run_id": rid,
                    }
                )
            else:
                text = f"failed ({state.error_category or 'timeout'})"
                cells.append({"text": text})
            totals[preset][text] = totals[preset].get(text, 0) + 1
        rows.append({"item": item, "subject": item.subject[:80], "cells": cells})
    return finished, rows, totals


def _batch_view(request: Request, batch: Batch) -> Response:
    finished, rows, totals = _summary(request, batch)
    done = finished == batch.total or (
        batch.stopped
        and batch.task is not None
        and batch.task.done()
        and all(
            request.app.state.registry.get(r).status != "running" for r in batch.run_ids.values()
        )
    )
    context = {"batch": batch, "finished": finished, "rows": rows, "totals": totals, "done": done}
    return _templates(request).TemplateResponse(request, "guard/_batch.html", context)


@external_router.get("/guard/batch/{batch_id}", response_class=HTMLResponse)
async def batch_status(request: Request, batch_id: str) -> Response:
    batch = getattr(request.app.state, "batches", {}).get(batch_id)
    if batch is None:
        return _alert("Batch not found", 404)
    return _batch_view(request, batch)


@external_router.post("/guard/batch/{batch_id}/stop", response_class=HTMLResponse)
async def stop_batch(request: Request, batch_id: str) -> Response:
    batch = getattr(request.app.state, "batches", {}).get(batch_id)
    if batch is None:
        return _alert("Batch not found", 404)
    batch.stopped = True  # running Runs finish; no further Run starts (21.9)
    return _batch_view(request, batch)


@external_router.get("/guard/batch/{batch_id}/row/{position}", response_class=HTMLResponse)
async def batch_row(request: Request, batch_id: str, position: int) -> Response:
    """Full Run_Results of one email of the batch (Requirement 21.11)."""
    state = request.app.state
    batch = getattr(state, "batches", {}).get(batch_id)
    if batch is None:
        return _alert("Batch not found", 404)
    columns: list[Any] = []
    compare_c0 = len(batch.presets) == 2 and "C0" in batch.presets
    for prefix, preset in zip(("a", "b"), batch.presets, strict=False):
        rid = batch.run_ids.get((position, preset))
        run_state = state.registry.get(rid) if rid else None
        if run_state is not None and run_state.result is not None:
            columns.append(
                build_column(
                    run_state.result, state.policy, id_prefix=prefix, compare_with_c0=compare_c0
                )
            )
        else:
            columns.append({"error": "no result for this email", "preset": preset})
    return _templates(request).TemplateResponse(
        request,
        "guard/_results.html",
        {"columns": columns, "replay_badge": "External data", "results_message": None},
    )


@external_router.post("/guard/external/clear", response_class=HTMLResponse)
async def clear(request: Request) -> Response:
    state = request.app.state
    for batch in getattr(state, "batches", {}).values():
        batch.stopped = True
    state.uploads = {}
    state.batches = {}
    return HTMLResponse('<p class="note">External data cleared.</p>')
