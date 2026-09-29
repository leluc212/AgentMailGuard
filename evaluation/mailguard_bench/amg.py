"""Load AgentMailGuard's evaluation scorer and metrics from the pinned worktree.

AgentMailGuard ships a top-level ``evaluation`` package, and so does rag-email; ``python -m``
from the rag-email root keeps rag-email's in front (Task 1). The guard's
``evaluation/metrics.py`` and ``evaluation/harness.py`` are therefore loaded by file path
under private module names, never as ``evaluation.*``. ``harness.py`` does
``from evaluation.metrics import CaseResult``, so that name is aliased to the loaded metrics
module only while harness.py executes, then restored. The guard's code is used unchanged
(ADR-0010; spec §4 "Scoring", owner decision Q4).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from evaluation.mailguard_bench.guard_env import REPO_ROOT

DEFAULT_MAILGUARD_DIR = REPO_ROOT.parent / "AgentMailGuard-bench"
_METRICS = "_amg_evaluation_metrics"
_HARNESS = "_amg_evaluation_harness"


def resolve_mailguard_dir() -> Path:
    """MAILGUARD_DIR (set by ``$(MAILGUARD_UV)``), else ``../AgentMailGuard-bench``."""
    return Path(os.environ.get("MAILGUARD_DIR") or DEFAULT_MAILGUARD_DIR).resolve()


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load AgentMailGuard module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def load_amg_metrics(mailguard_dir: Path) -> ModuleType:
    """AgentMailGuard ``evaluation/metrics.py`` (CaseResult, Proportion, summarize, McNemar)."""
    return _load(_METRICS, mailguard_dir / "evaluation" / "metrics.py")


def load_amg_harness(mailguard_dir: Path) -> ModuleType:
    """AgentMailGuard ``evaluation/harness.py`` (BenchCase, goal_achieved, task_success)."""
    metrics = load_amg_metrics(mailguard_dir)
    previous = sys.modules.get("evaluation.metrics")
    sys.modules["evaluation.metrics"] = metrics
    try:
        return _load(_HARNESS, mailguard_dir / "evaluation" / "harness.py")
    finally:
        if previous is None:
            sys.modules.pop("evaluation.metrics", None)
        else:
            sys.modules["evaluation.metrics"] = previous
