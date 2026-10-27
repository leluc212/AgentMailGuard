"""Locate and import the guard library (``mailguard``) without touching ``agentmailguard/``.

``mailguard`` is importable only when ``agentmailguard/`` is on ``sys.path`` (Docker sets
``PYTHONPATH=/app:/app/agentmailguard``). On the host the Playground appends the configured
guard root itself. It is *appended*, never prepended: ``agentmailguard/`` also holds top-level
``evaluation`` and ``tests`` packages that must not shadow the repository's own.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

GUARD_MODULE = "mailguard.pipeline"


@dataclass(frozen=True)
class GuardStatus:
    available: bool
    error: str | None = None


def _spec_found(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def import_guard(guard_root: Path) -> GuardStatus:
    """Make ``mailguard`` importable from ``guard_root`` if needed, then import the pipeline."""
    if not _spec_found("mailguard"):
        root = str(guard_root)
        if guard_root.is_dir() and root not in sys.path:
            sys.path.append(root)
    try:
        importlib.import_module(GUARD_MODULE)
    except Exception as exc:  # any failure means "unavailable"; the service keeps running
        return GuardStatus(False, f"{type(exc).__name__}: {exc}")
    return GuardStatus(True)
