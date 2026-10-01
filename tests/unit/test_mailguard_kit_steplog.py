"""The kit log: one JSON line per step, and what it says is finished (task 7.23)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.kit.steplog import StepLog, finished_configs


def _config(config: str, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "step": "config",
        "config": config,
        "model_profile": "gpt-4o-mini",
        "limit": None,
        "status": "ok",
        "counts": {"selected": 4, "ok": 4, "error": 0, "skipped_already_recorded": 0},
    }
    return {**base, **changes}


def test_every_append_is_one_line_and_survives_a_torn_last_line(tmp_path: Path) -> None:
    log = StepLog(tmp_path / "run" / "kit-log.jsonl")
    log.append({"step": "stack", "status": "ok"})
    log.append({"step": "config", "config": "C0"})
    with (tmp_path / "run" / "kit-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"step": "conf')  # the machine died mid-write
    assert [r["step"] for r in log.records()] == ["stack", "config"]
    log.append({"step": "reports"})
    assert [r["step"] for r in log.records()][-1] == "reports"


def test_a_missing_log_is_empty_and_a_log_without_a_path_writes_nothing(tmp_path: Path) -> None:
    assert StepLog(tmp_path / "none.jsonl").records() == []
    dry = StepLog(None)
    dry.append({"step": "x"})
    assert dry.records() == []


def test_a_config_is_finished_when_its_last_step_was_clean_and_complete() -> None:
    records = [_config("C0"), _config("C3", status="failed", counts=None)]
    assert finished_configs(records, "gpt-4o-mini", None) == {"C0"}


def test_the_last_step_of_a_config_decides() -> None:
    bad = _config("C0", counts={"selected": 4, "ok": 2, "error": 2, "skipped_already_recorded": 0})
    assert finished_configs([_config("C0"), bad], "gpt-4o-mini", None) == set()
    assert finished_configs([bad, _config("C0")], "gpt-4o-mini", None) == {"C0"}


def test_recorded_rows_from_an_earlier_start_count_toward_complete() -> None:
    resumed = _config(
        "C0", counts={"selected": 4, "ok": 1, "error": 0, "skipped_already_recorded": 3}
    )
    assert finished_configs([resumed], "gpt-4o-mini", None) == {"C0"}
    short = _config(
        "C0", counts={"selected": 4, "ok": 1, "error": 0, "skipped_already_recorded": 1}
    )
    assert finished_configs([short], "gpt-4o-mini", None) == set()


def test_another_model_limit_or_nothing_selected_is_not_finished() -> None:
    assert finished_configs([_config("C0")], "qwen2.5-7b", None) == set()
    assert finished_configs([_config("C0")], "gpt-4o-mini", 5) == set()
    empty = _config(
        "C0", counts={"selected": 0, "ok": 0, "error": 0, "skipped_already_recorded": 0}
    )
    assert finished_configs([empty], "gpt-4o-mini", None) == set()


def test_interrupted_and_non_config_steps_never_count() -> None:
    records = [_config("C0", status="interrupted"), {"step": "stack", "status": "ok"}]
    assert finished_configs(records, "gpt-4o-mini", None) == set()
