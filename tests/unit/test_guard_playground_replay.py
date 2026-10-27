"""Tests for ``services.guard_playground.core.replay`` (Requirements 7.2, 7.11-7.13, 7.17)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.guard_playground.core.replay import (
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ReplayStore,
    file_name,
    write,
)
from services.guard_playground.core.schema import RunResult
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    fixture_guard_import_isolation,
    fixture_playground_app_factory,
)

SECRET_BODY = "TOP-SECRET-BODY-MARKER"


def _layer(name: str) -> dict[str, Any]:
    return {
        "layer": name,
        "ran": True,
        "not_run_reason": None,
        "severity": "none",
        "score": 0.1,
        "decided_by": "rules",
        "latency_ms": 3,
        "findings": [],
        "error": None,
        "fallback_reason": None,
        "judge_configured": False,
    }


def _payload(
    case_id: str = "case-001", preset: str = "C3", model: str = "gpt-4o-mini"
) -> dict[str, Any]:
    return {
        "generated_at": datetime(2026, 10, 26, 14, 5, tzinfo=UTC).isoformat(),
        "mode": "replay",
        "source": "pinned",
        "case_id": case_id,
        "preset": preset,
        "draft_model": model,
        "guard_models": {"doc_judge": model},
        "email": {
            "subject": "Invoice",
            "body": SECRET_BODY,
            "chunks": [],
            "source": "pinned",
            "case_id": case_id,
        },
        "layers": [_layer("inbound_guard"), _layer("doc_scanner"), _layer("output_guard")],
        "user_intent": None,
        "stripped": [],
        "removed_ratio": 0.0,
        "chunks": [],
        "quarantine_ratio": 0.0,
        "inbound": None,
        "outbound": None,
        "gate_passes": 1,
        "isolation_applied": True,
        "draft_generated": True,
        "draft_body": "Thanks.",
        "guarded_prompt": None,
        "thresholds": {"block": 0.9, "flag": 0.5, "strip": 0.7, "quarantine": 0.8},
    }


def _result(**kwargs: str) -> RunResult:
    return RunResult.model_validate(_payload(**kwargs))


def _put(directory: Path, name: str, content: str | dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (directory / name).write_text(text, encoding="utf-8")


def test_missing_dir_gives_empty_store(tmp_path: Path) -> None:
    store = ReplayStore.load(tmp_path / "nope")
    assert len(store) == 0
    assert store.invalid == []
    assert store.status("case-001", "C3", "gpt-4o-mini") == STATUS_UNAVAILABLE


def test_valid_entry_lookup(tmp_path: Path) -> None:
    _put(tmp_path, "case-001__C3__gpt-4o-mini.json", _payload())
    store = ReplayStore.load(tmp_path)
    assert len(store) == 1
    entry = store.get("case-001", "C3", "gpt-4o-mini")
    assert entry == _result()
    assert store.has("case-001", "C3")
    assert not store.has("case-001", "C0")
    assert store.status("case-001", "C3", "gpt-4o-mini") == STATUS_OK
    assert store.unavailable("case-001", "C3", "qwen")
    assert store.get("case-001", "C3", "qwen") is None


def test_case_id_with_separator_splits_from_right(tmp_path: Path) -> None:
    _put(tmp_path, "enron__42__C0__qwen.json", _payload("enron__42", "C0", "qwen"))
    store = ReplayStore.load(tmp_path)
    assert store.get("enron__42", "C0", "qwen") is not None


@pytest.mark.parametrize(
    ("name", "content", "reason_part"),
    [
        ("case-001__C3__llama.json", "{not json " + SECRET_BODY, "schema validation failed"),
        (
            "case-001__C3__llama.json",
            _payload(model="llama") | {"final_decision": SECRET_BODY},
            "schema validation failed",
        ),
        ("case-002__C3__llama.json", _payload("case-001", "C3", "llama"), "case_id"),
        ("case-001__C0__llama.json", _payload("case-001", "C3", "llama"), "preset"),
        ("case-001__C3__llama.json", _payload("case-001", "C3", "qwen"), "draft_model"),
    ],
    ids=["invalid-json", "extra-field", "case-mismatch", "preset-mismatch", "model-mismatch"],
)
def test_invalid_files_skipped_and_marked(
    tmp_path: Path, name: str, content: str | dict[str, Any], reason_part: str
) -> None:
    _put(tmp_path, "case-001__C3__gpt-4o-mini.json", _payload())
    _put(tmp_path, name, content)
    store = ReplayStore.load(tmp_path)
    # the valid file is still loaded; the bad one is skipped and recorded by its name
    assert len(store) == 1
    assert len(store.invalid) == 1
    case_id, preset, model_key, reason = store.invalid[0]
    assert file_name(case_id, preset, model_key) == name
    assert reason_part in reason
    assert SECRET_BODY not in reason
    assert store.status(case_id, preset, model_key) == STATUS_UNAVAILABLE
    assert store.get(case_id, preset, model_key) is None


def test_models_for_fixed_order(tmp_path: Path) -> None:
    for model in ("zeta", "llama", "alpha", "gpt-4o-mini", "qwen"):
        _put(tmp_path, f"case-001__C0__{model}.json", _payload("case-001", "C0", model))
    _put(tmp_path, "case-001__C3__qwen.json", _payload("case-001", "C3", "qwen"))
    store = ReplayStore.load(tmp_path)
    assert store.models_for("case-001", "C0") == ["gpt-4o-mini", "qwen", "llama", "alpha", "zeta"]
    assert store.models_for("case-001", "C3") == ["qwen"]
    assert store.models_for("case-404", "C0") == []


def test_write_load_round_trip_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYGROUND_TEST_API_KEY", "sk-test-0123456789")
    payload = _payload()
    payload["guarded_prompt"] = "key=sk-test-0123456789"
    entry = RunResult.model_validate(payload)
    target = write(entry, tmp_path / "replay", "gpt-4o-mini")
    assert target.name == "case-001__C3__gpt-4o-mini.json"
    assert "sk-test-0123456789" not in target.read_text(encoding="utf-8")
    assert [p.name for p in (tmp_path / "replay").iterdir()] == [target.name]  # no temp left
    loaded = ReplayStore.load(tmp_path / "replay").get("case-001", "C3", "gpt-4o-mini")
    assert loaded == entry.model_copy(update={"guarded_prompt": "key=[redacted]"})


def test_write_refuses_entry_without_case_id(tmp_path: Path) -> None:
    payload = _payload()
    payload["case_id"] = None
    with pytest.raises(ValueError, match="case_id"):
        write(RunResult.model_validate(payload), tmp_path, "gpt-4o-mini")
    assert not any(tmp_path.iterdir())


def test_load_300_files_under_2s(tmp_path: Path) -> None:
    for i in range(100):
        case_id = f"case-{i:03d}"
        for model in ("gpt-4o-mini", "qwen", "llama"):
            _put(tmp_path, f"{case_id}__C3__{model}.json", _payload(case_id, "C3", model))
    # Touch each file once first: on Windows the on-access antivirus scan of freshly written
    # files costs seconds, while real Replay files have been on disk long before startup.
    for path in tmp_path.iterdir():
        path.read_bytes()
    start = time.perf_counter()
    store = ReplayStore.load(tmp_path)
    elapsed = time.perf_counter() - start
    assert len(store) == 300
    assert elapsed < 2.0


def test_app_state_counts_replay_entries(
    tmp_path: Path, playground_app_factory: Callable[..., Any]
) -> None:
    replay_dir = tmp_path / "data" / "replay"
    _put(replay_dir, "case-001__C3__gpt-4o-mini.json", _payload())
    _put(replay_dir, "case-001__C0__gpt-4o-mini.json", "{broken")
    app = playground_app_factory()
    assert isinstance(app.state.replay, ReplayStore)
    assert app.state.replay_entry_count == 1
    assert app.state.replay.status("case-001", "C0", "gpt-4o-mini") == STATUS_UNAVAILABLE
