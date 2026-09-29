"""Reranker settings (R11.1, R11.2, R11.5; task 7.20).

The ai-worker reads which cross-encoder to load, where the model is stored and how long a
rerank may take from RETRIEVAL__RERANK_*; RETRIEVAL__RERANK_ENABLED switches the stage off.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.settings import AppSettings, RetrievalSettings


def test_rerank_defaults() -> None:
    """R11.1, R11.5: MiniLM, the Hugging Face cache, a one-second budget, and rerank on."""
    retrieval = AppSettings(_env_file=None).retrieval

    assert retrieval.rerank_enabled is True
    assert retrieval.rerank_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert retrieval.rerank_model_dir == ""
    assert retrieval.rerank_timeout_ms == 1000


def test_rerank_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL__RERANK_ENABLED", "false")
    monkeypatch.setenv("RETRIEVAL__RERANK_MODEL", "cross-encoder/ms-marco-TinyBERT-L-2-v2")
    monkeypatch.setenv("RETRIEVAL__RERANK_MODEL_DIR", "/app/.cache/reranker")
    monkeypatch.setenv("RETRIEVAL__RERANK_TIMEOUT_MS", "250")

    retrieval = AppSettings(_env_file=None).retrieval

    assert retrieval.rerank_enabled is False
    assert retrieval.rerank_model == "cross-encoder/ms-marco-TinyBERT-L-2-v2"
    assert retrieval.rerank_model_dir == "/app/.cache/reranker"
    assert retrieval.rerank_timeout_ms == 250


def test_blank_rerank_model_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """R20.6: Docker Compose forwards an unset host variable as an empty string.

    An empty model name is never valid; it would only fail at the first rerank.
    """
    monkeypatch.setenv("RETRIEVAL__RERANK_MODEL", "  ")

    assert (
        AppSettings(_env_file=None).retrieval.rerank_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    )


def test_rerank_timeout_rejects_a_budget_no_model_can_meet() -> None:
    """R20.6: fail fast, like RETRIEVAL__RETRIEVAL_TIMEOUT_MS, below 10 ms."""
    with pytest.raises(ValidationError, match="rerank_timeout_ms"):
        RetrievalSettings(rerank_timeout_ms=5)
