"""scripts/playground/build_replay.py with a stub executor (task 7.5; Requirements 7.9, 7.10).

No model is called: the executor is replaced by a coroutine that builds RunResults by hand.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.guard_playground.core.replay import ReplayStore
from services.guard_playground.core.runner import LiveRunError
from services.guard_playground.core.schema import EmailInput, RunResult
from services.guard_playground.settings import PlaygroundSettings
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    fixture_block_network,
    fixture_no_credentials,
)

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")
SCRIPT = PlaygroundSettings().repo_root / "scripts" / "playground" / "build_replay.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("build_replay_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _layer(name: str) -> dict[str, Any]:
    return {
        "layer": name, "ran": False, "not_run_reason": "disabled by preset",
        "severity": "none", "score": 0.0, "decided_by": "none", "latency_ms": 0,
        "findings": [], "error": None, "fallback_reason": None, "judge_configured": True,
    }  # fmt: skip


def _result(email: EmailInput, preset: str, model_key: str, body: str) -> RunResult:
    return RunResult.model_validate(
        {
            "generated_at": datetime(2026, 10, 26, 14, 5, tzinfo=UTC),
            "mode": "live", "source": email.source, "case_id": email.case_id,
            "preset": preset, "draft_model": model_key, "guard_models": {},
            "email": email.model_dump(),
            "layers": [_layer(n) for n in ("inbound_guard", "doc_scanner", "output_guard")],
            "user_intent": None, "stripped": [], "removed_ratio": 0.0, "chunks": [],
            "quarantine_ratio": 0.0, "inbound": None, "outbound": None, "gate_passes": None,
            "isolation_applied": False, "draft_generated": True, "draft_body": body,
            "guarded_prompt": None,
            "thresholds": {"block": 0.85, "flag": 0.5, "strip": 0.5, "quarantine": 0.7},
        }
    )  # fmt: skip


def _settings(tmp_path: Path) -> PlaygroundSettings:
    cases = tmp_path / "evaluation" / "datasets" / "mailguard" / "cases.jsonl"
    cases.parent.mkdir(parents=True)
    lines = [
        {"case_id": cid, "kind": "attack", "source": "llmail_inject",
         "email": {"subject": cid, "body_text": f"body {cid}"}}
        for cid in ("a-1", "a-2")
    ]  # fmt: skip
    cases.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return PlaygroundSettings(repo_root=tmp_path, guard_root=PlaygroundSettings().guard_root)


async def test_writes_one_replay_file_per_pair_as_replay_mode(tmp_path: Path) -> None:
    script = _load_script()
    settings = _settings(tmp_path)
    calls: list[tuple[str, str, str]] = []

    async def stub(email: EmailInput, preset: str, model_key: str, **_: Any) -> RunResult:
        calls.append((email.case_id or "", preset, model_key))
        return _result(email, preset, model_key, "new draft")

    failed = await script.build(settings, ["a-1", "a-2"], ["C0", "C3"], ["qwen"], executor=stub)
    assert failed == []
    assert len(calls) == 4
    store = ReplayStore.load(settings.data_dir / "replay")
    assert len(store) == 4 and store.invalid == []
    entry = store.get("a-1", "C3", "qwen")
    assert entry is not None and entry.mode == "replay" and entry.draft_body == "new draft"


async def test_a_failed_pair_keeps_its_old_file_and_is_reported(tmp_path: Path) -> None:
    script = _load_script()
    settings = _settings(tmp_path)

    async def first(email: EmailInput, preset: str, model_key: str, **_: Any) -> RunResult:
        return _result(email, preset, model_key, "old draft")

    await script.build(settings, ["a-1"], ["C3"], ["llama"], executor=first)

    async def failing(email: EmailInput, preset: str, model_key: str, **_: Any) -> RunResult:
        raise LiveRunError("rate limit", "OpenAI HTTP 429")

    failed = await script.build(settings, ["a-1", "missing"], ["C3"], ["llama"], executor=failing)
    assert len(failed) == 2
    assert "a-1 C3 llama: rate limit" in failed[0] and "case not found" in failed[1]
    entry = ReplayStore.load(settings.data_dir / "replay").get("a-1", "C3", "llama")
    assert entry is not None and entry.draft_body == "old draft"


def test_dry_run_and_bad_options(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()
    assert script.main(["--dry-run", "--model", "all", "--cases", "x"]) == 1  # x not found
    out = capsys.readouterr().out
    assert "1 cases x 2 presets x 3 models = 6 runs" in out and "paid" not in out
    with pytest.raises(SystemExit):
        script.main(["--model", "gpt-5", "--cases", "x", "--dry-run"])
    with pytest.raises(SystemExit):
        script.main(["--presets", "C9", "--cases", "x", "--dry-run"])


def test_missing_key_stops_before_any_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "load_dot_env", lambda _path: None)
    assert script.main(["--model", "qwen", "--cases", "x"]) == 2
