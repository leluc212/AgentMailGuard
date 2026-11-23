"""The run options of the job being processed (ADR-0018 decision 4, design.md §5.9, R25.3).

``BaseConsumer`` opens a scope with the envelope's ``run_options`` around ``process_job``, on
every attempt (a retry or a replay carries the same envelope field), so code deep in the call
stack (the model provider, the cost counters) reads them without every signature taking them.
A context variable is per asyncio task, so concurrent deliveries never see each other's options.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

MODEL_PROFILES = ("fake", "live")
GUARD_MODES = ("C0", "C3")
DEFAULT_PROFILE_LABEL = "default"
"""``model_profile`` metric label of a job without run options: the stack's configured provider."""

_RUN_OPTIONS: ContextVar[dict[str, Any] | None] = ContextVar("run_options", default=None)


def current_run_options() -> dict[str, Any] | None:
    """The run options of the job in progress, or None (behaves as before R25)."""
    return _RUN_OPTIONS.get()


@contextmanager
def run_options_scope(run_options: dict[str, Any] | None) -> Iterator[None]:
    """Make ``run_options`` the current job's for the duration of the block."""
    token = _RUN_OPTIONS.set(dict(run_options) if run_options else None)
    try:
        yield
    finally:
        _RUN_OPTIONS.reset(token)


def current_model_profile() -> str | None:
    """``fake`` or ``live`` when the job chose one, else None (the configured provider)."""
    options = _RUN_OPTIONS.get()
    profile = options.get("model_profile") if options else None
    return profile if profile in MODEL_PROFILES else None


def current_guard_mode() -> str | None:
    """``C0`` or ``C3`` when the job chose one, else None (``GUARD__DEFAULT_MODE`` applies)."""
    options = _RUN_OPTIONS.get()
    mode = options.get("guard_mode") if options else None
    return mode if mode in GUARD_MODES else None


def current_live_model() -> str | None:
    """The catalog name of the live model the job chose (ADR-0019), else None (LLM__*)."""
    options = _RUN_OPTIONS.get()
    name = options.get("live_model") if options else None
    return name if isinstance(name, str) and name else None


def model_profile_label() -> str:
    """The ``model_profile`` label of the R21 cost counters for the job in progress."""
    return current_model_profile() or DEFAULT_PROFILE_LABEL
