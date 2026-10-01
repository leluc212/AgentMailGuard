"""Loading AgentMailGuard's scorer and metrics by file path (task 7.19; ADR-0010)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from evaluation.mailguard_bench.amg import resolve_mailguard_dir
from evaluation.mailguard_bench.guard_env import REPO_ROOT, default_guard_dir


def test_resolve_prefers_the_make_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAILGUARD_DIR", str(tmp_path))
    assert resolve_mailguard_dir() == tmp_path.resolve()
    monkeypatch.delenv("MAILGUARD_DIR")
    assert resolve_mailguard_dir() == default_guard_dir(REPO_ROOT).resolve()


def test_harness_and_metrics_share_one_case_result_and_leave_rag_email_alone() -> None:
    pytest.importorskip("mailguard")
    from evaluation.mailguard_bench.amg import load_amg_harness, load_amg_metrics

    mailguard_dir = resolve_mailguard_dir()
    if not (mailguard_dir / "evaluation" / "harness.py").exists():
        pytest.skip("AgentMailGuard worktree not found")
    metrics = load_amg_metrics(mailguard_dir)
    harness = load_amg_harness(mailguard_dir)
    assert harness.CaseResult is metrics.CaseResult
    assert "evaluation.metrics" not in sys.modules
    import evaluation

    assert Path(str(evaluation.__file__)).resolve().is_relative_to(REPO_ROOT)
