"""Demo_Tour, Presenter_Mode and Benchmark_Record (tasks 9.1, 9.4, 10.1-10.4).

Requirements 6.1-6.5, 17.1, 17.2, 17.8, 19.1.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from services.guard_playground.core.benchmark import NO_RECORD, BenchmarkIndex
from services.guard_playground.core.cases import load_cases
from services.guard_playground.core.tour import load_tour
from services.guard_playground.settings import PlaygroundSettings
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    asgi_client,
    fixture_block_network,
    fixture_guard_import_isolation,
    fixture_no_credentials,
    fixture_playground_app_factory,
)

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")
REAL = PlaygroundSettings()


def test_shipped_tour_and_demo_set_are_valid_on_the_real_data() -> None:
    catalog = load_cases(REAL)
    tour = load_tour(REAL.data_dir, catalog)
    assert tour.valid, tour.error
    kinds = [catalog.get(s.case_id).kind if s.case_id else "custom" for s in tour.steps]  # type: ignore[union-attr]
    assert kinds == ["benign", "attack", "rag_poison", "rag_poison", "custom"]  # Req 17.2
    assert [s.mode for s in tour.steps][-1] == "live"
    assert all(s.presets == ("C0", "C3") for s in tour.steps)
    demo = json.loads((REAL.data_dir / "demo_set.json").read_text(encoding="utf-8"))["cases"]
    assert len(demo) == 12 and all(catalog.get(c) is not None for c in demo)
    for step in tour.steps:
        assert step.case_id is None or step.case_id in demo


@pytest.mark.parametrize(
    ("steps", "error"),
    [
        ([], "the tour has 0 steps"),
        ([{"input": {"case_id": "x"}, "presets": ["C0"], "mode": "replay",
           "talking_point": "t"}] * 4, "Step 1: unknown case id"),
        ([{"input": {"custom": {"subject": "s", "body": "b"}}, "presets": ["C9"],
           "mode": "live", "talking_point": "t"}] * 4, "Step 1: unknown preset"),
        ([{"input": {"custom": {"subject": "s", "body": "b"}}, "presets": ["C3"],
           "mode": "live", "talking_point": "x" * 201}] * 4, "longer than 200"),
    ],
)  # fmt: skip
def test_invalid_tours_name_the_rule(tmp_path: Path, steps: list[Any], error: str) -> None:
    (tmp_path / "demo_tour.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    tour = load_tour(tmp_path, load_cases(REAL))
    assert not tour.valid and error in (tour.error or "")


async def test_page_has_tour_and_presenter_toggle_off(playground_app_factory: Callable[..., Any],
                                                       tmp_path: Path) -> None:  # fmt: skip
    data = tmp_path / "data"
    data.mkdir()
    steps = [{"input": {"custom": {"subject": "s", "body": "<b>x</b>"}}, "presets": ["C0", "C3"],
              "mode": "live", "talking_point": "Try it."}] * 4  # fmt: skip
    (data / "demo_tour.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    async with asgi_client(playground_app_factory()) as client:
        html = (await client.get("/guard")).text
    assert 'id="presenter-toggle" aria-pressed="false"' in html
    assert 'id="tour-start"' in html and "Step 1 of 4" in html
    data_block = html.split('id="tour-data">', 1)[1].split("</script>", 1)[0]
    assert "<b>" not in data_block and json.loads(data_block)[0]["body"] == "<b>x</b>"


async def test_invalid_tour_hides_controls(playground_app_factory: Callable[..., Any],
                                           tmp_path: Path) -> None:  # fmt: skip
    async with asgi_client(playground_app_factory()) as client:  # no demo_tour.json in tmp
        html = (await client.get("/guard")).text
    assert 'id="tour-start"' not in html and "Demo tour unavailable" in html
    assert 'id="presenter-toggle"' in html


@pytest.mark.skipif(
    not (REAL.results_root / "2026-10-19-gpt4omini-v3-1800").is_dir(), reason="results absent"
)
def test_benchmark_rows_from_real_results() -> None:
    rows = BenchmarkIndex(REAL.results_root).table("attack-llmail-0a41d5272ecd", ["C0", "C3"])
    assert [r.model for r in rows] == ["gpt-4o-mini", "Llama 3.1 8B", "Qwen 2.5 7B"]
    for row in rows:
        c0, c3 = row.cells
        assert (c0.goal, c0.blocked) == ("yes", "no")
        assert c3.blocked and c3.blocked.startswith("yes") and c3.goal == "no"
    missing = BenchmarkIndex(REAL.results_root).table("no-such-case", ["C3"])
    assert all(r.cells[0].text == NO_RECORD for r in missing)


def test_benchmark_missing_folder_is_no_record(tmp_path: Path) -> None:
    rows = BenchmarkIndex(tmp_path).table("x", ["C0", "C3"])
    assert all(cell.text == NO_RECORD for row in rows for cell in row.cells)


async def test_case_detail_shows_benchmark_only_for_pinned(
    playground_app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    cases = tmp_path / "evaluation" / "datasets" / "mailguard" / "cases.jsonl"
    cases.parent.mkdir(parents=True)
    record = {"case_id": "p-1", "kind": "attack", "source": "s",
              "email": {"subject": "s", "body_text": "b"}}  # fmt: skip
    cases.write_text(json.dumps(record) + "\n", encoding="utf-8")
    async with asgi_client(playground_app_factory()) as client:
        pinned = (await client.get("/guard/cases/p-1")).text
        fixture = (await client.get("/guard/cases/fixture-b-001")).text
    assert "Recorded benchmark results" in pinned and NO_RECORD in pinned
    assert "Recorded benchmark results" not in fixture
