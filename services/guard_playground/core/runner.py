"""Live_Mode runs (design.md "core/runner.py"; Requirements 7.5, 7.7, 7.15, 8.4, 9.4, 20).

``execute`` runs one email through ``MailGuardPipeline`` with every judge on the selected
Draft_Model (D9) and ``audit=False``. Each provider is wrapped so the progress display knows
which Stage is calling a model, and so a failed model call is reported as a Live failure
instead of being hidden behind the guard's fail-closed fallback (Requirement 7.5).

``RunRegistry`` keeps runs in memory only (TD8). The Playground listens on 127.0.0.1 for a
single presenter, so one registry per process is enough.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from services.guard_playground.core.models import (
    JUDGES,
    DraftModel,
    draft_agent,
    get_model,
    guard_providers,
)
from services.guard_playground.core.redact import redact
from services.guard_playground.core.schema import EmailInput, RunResult

Progress = Callable[[str], None]
ErrorCategory = Literal["connection error", "authentication error", "rate limit", "provider error"]

STAGE_OF_JUDGE: dict[str, str] = {
    "inbound_judge": "L1 Inbound Guard",
    "inbound_extractor": "L1 Inbound Guard",
    "doc_judge": "L2 Doc Scanner",
    "doc_set_judge": "L2 Doc Scanner",
    "output_judge": "L4 Output Guard",
}
GENERATION = "Generation"
STARTING = "Starting"


class LiveRunError(Exception):
    """A model API call of a Live run failed; ``category`` is one of Requirement 7.5."""

    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


def classify(exc: BaseException) -> ErrorCategory:
    text = str(exc)
    if any(code in text for code in ("HTTP 401", "HTTP 403")):
        return "authentication error"
    if "HTTP 429" in text or "rate limit" in text.lower():
        return "rate limit"
    if "transport error" in text or "timed out" in text or isinstance(exc, ConnectionError):
        return "connection error"
    return "provider error"


def system_instructions() -> str:
    """The benchmark's trusted system text: profile instructions, then reply-format rules."""
    from packages.llm.profile import DEFAULT_ENTERPRISE_INSTRUCTIONS
    from packages.llm.reply_format import render_reply_format_rules

    rules = "Reply format rules:\n" + render_reply_format_rules(start=1)
    return f"{DEFAULT_ENTERPRISE_INSTRUCTIONS}\n\n{rules}"


@dataclass
class _CallLog:
    errors: list[BaseException] = field(default_factory=list)


class _StageProvider:
    """Wraps a guard LLM provider: reports the Stage and records failed calls."""

    def __init__(self, inner: Any, stage: str, progress: Progress, log: _CallLog) -> None:
        self._inner = inner
        self._stage = stage
        self._progress = progress
        self._log = log
        self.model = getattr(inner, "model", None)

    async def generate(self, **kwargs: Any) -> Any:
        self._progress(self._stage)
        try:
            return await self._inner.generate(**kwargs)
        except Exception as exc:
            self._log.errors.append(exc)
            raise


def guard_settings(model: DraftModel, l1_model_path: Path) -> Any:
    """Guard settings as the benchmark C3 runs used: every judge live, fail closed, no .env."""
    from mailguard.config.settings import MailGuardSettings

    settings = MailGuardSettings(_env_file=None)
    gm = settings.guard_models
    for name in JUDGES:
        setattr(gm, name, model.model_id)
    settings.inbound_guard.ml_model_path = str(l1_model_path)
    settings.doc_scanner.llm_enabled = True
    settings.doc_scanner.set_judge_llm_enabled = True
    settings.output_guard.llm_enabled = True
    settings.mailguard.fail_closed_on_judge_error = True
    return settings


def build_pipeline(preset: str, settings: Any, providers: dict[str, Any]) -> Any:
    """The one place a ``MailGuardPipeline`` is built; ``audit=False`` always (Req 9.4)."""
    from mailguard.pipeline import GuardConfig, MailGuardPipeline

    return MailGuardPipeline(settings, GuardConfig.preset(preset), audit=False, **providers)


async def execute(
    email: EmailInput,
    preset: str,
    model_key: str,
    *,
    l1_model_path: Path,
    progress: Progress = lambda _stage: None,
) -> RunResult:
    """One Live run. Raises ``LiveRunError`` when any model call failed (Requirement 7.5)."""
    from services.guard_playground.core.from_guard import run_result_from_guard

    model = get_model(model_key)
    settings = guard_settings(model, l1_model_path)
    log = _CallLog()
    raw = guard_providers(model)
    providers = {
        name: _StageProvider(p, STAGE_OF_JUDGE[name], progress, log) for name, p in raw.items()
    }
    agent = draft_agent(model)

    async def generate(messages: list[Any]) -> dict[str, Any]:
        progress(GENERATION)
        try:
            return await agent(messages)
        except Exception as exc:
            log.errors.append(exc)
            raise

    pipeline = build_pipeline(preset, settings, providers)
    mail = {
        "message_id": f"playground-{uuid.uuid4().hex[:8]}",
        "sender_email": "customer@example.com",
        "subject": email.subject,
        "body_text": email.body,
        "category": "support",
    }
    chunks = [{"chunk_id": c.chunk_id, "content": c.content} for c in email.chunks]
    try:
        report, draft, bundle = await pipeline.run(
            mail, chunks, generate, system_instructions=system_instructions(), category="support"
        )
    except Exception as exc:
        first = log.errors[0] if log.errors else exc
        raise LiveRunError(classify(first), redact(str(first))[:300]) from None
    if log.errors:
        first = log.errors[0]
        raise LiveRunError(classify(first), redact(str(first))[:300])
    return run_result_from_guard(
        report,
        draft,
        bundle,
        email=email,
        preset=preset,
        mode="live",
        draft_model=model.key,
        guard_models=dict.fromkeys(JUDGES, model.model_id),
        settings=settings,
    )


# --------------------------------------------------------------------------- registry

Status = Literal["running", "done", "failed", "timeout"]


@dataclass
class RunState:
    run_id: str
    preset: str
    model_key: str
    started: float
    stage: str = STARTING
    status: Status = "running"
    result: RunResult | None = None
    error_category: str | None = None
    error_message: str | None = None
    case_id: str | None = None  # pinned/fixture case, for the Replay offer (Requirement 7.6)
    task: asyncio.Task[None] | None = None

    @property
    def elapsed_s(self) -> int:
        return min(int(time.monotonic() - self.started), 120)


Executor = Callable[..., Any]


class RunRegistry:
    """In-memory Live runs (TD8). ``executor`` is ``execute`` or a test stub."""

    def __init__(self, executor: Executor = execute, *, timeout_s: float = 120.0) -> None:
        self._executor = executor
        self._timeout_s = timeout_s
        self._runs: dict[str, RunState] = {}

    def start(self, email: EmailInput, preset: str, model_key: str, *, l1_model_path: Path) -> str:
        run_id = uuid.uuid4().hex
        state = RunState(run_id, preset, model_key, started=time.monotonic())
        self._runs[run_id] = state

        def progress(stage: str) -> None:
            state.stage = stage

        async def work() -> None:
            try:
                state.result = await asyncio.wait_for(
                    self._executor(
                        email, preset, model_key, l1_model_path=l1_model_path, progress=progress
                    ),
                    timeout=self._timeout_s,
                )
                state.status = "done"
            except TimeoutError:
                state.status = "timeout"
                state.error_message = f"Run timed out after {int(self._timeout_s)} seconds"
            except LiveRunError as exc:
                state.status = "failed"
                state.error_category = exc.category
                state.error_message = exc.message
            except Exception as exc:  # unexpected: never show a partial result
                state.status = "failed"
                state.error_category = classify(exc)
                state.error_message = redact(str(exc))[:300]

        state.task = asyncio.create_task(work())
        return run_id

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def cancel(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is not None and state.task is not None and not state.task.done():
            state.task.cancel()
