"""KPI_Strip and Results_Page (task 9.2, 9.3; Requirement 18)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from services.guard_playground.core.kpi import NOT_AVAILABLE, RUNS, load_kpi, round1
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
HEADER = "table,config,metric,group,successes,total,pct,ci95_lo,ci95_hi,value\n"


def _lines(card_key: str, kpi: Any) -> dict[str, Any]:
    card = next(c for c in kpi.cards if c.key == card_key)
    return {line.model: line for line in card.lines}


@pytest.mark.parametrize(
    ("value", "text"),
    [("75.68", "75.7%"), ("0.05", "0.1%"), ("0.04", "0.0%"), ("100", "100.0%"), ("-0.05", "-0.1%")],
)
def test_round_half_away_from_zero(value: str, text: str) -> None:
    assert round1(Decimal(value)) == text


@pytest.mark.skipif(
    not (REAL.results_root / RUNS[0][0] / "metrics.csv").is_file(), reason="results absent"
)
def test_real_metrics_match_the_thesis_numbers() -> None:
    kpi = load_kpi(REAL.results_root)
    assert [c.key for c in kpi.cards] == ["email", "rag", "fpr"]
    email = _lines("email", kpi)
    assert list(email) == ["gpt-4o-mini", "Llama 3.1 8B", "Qwen 2.5 7B"]
    gpt = email["gpt-4o-mini"]
    assert gpt.c0 is not None
    assert (gpt.c0.text, gpt.c0.n) == ("75.7%", "n = 999/1000")
    assert (gpt.c3.text, gpt.c3.n) == ("100.0%", "n = 1000/1000")
    rag = _lines("rag", kpi)["gpt-4o-mini"]
    assert rag.c0 is not None and (rag.c0.text, rag.c3.text) == ("77.3%", "97.0%")
    assert rag.c3.n == "n = 300/300"
    fpr = _lines("fpr", kpi)["gpt-4o-mini"]
    assert fpr.c0 is None and (fpr.c3.text, fpr.c3.n) == ("1.4%", "n = 500/500")
    assert _lines("fpr", kpi)["Llama 3.1 8B"].c3.text == "4.0%"
    assert kpi.provenance and "guard d95e542" in kpi.provenance


def test_missing_files_show_not_available(tmp_path: Path) -> None:
    run = tmp_path / RUNS[0][0]
    run.mkdir()
    (run / "metrics.csv").write_text(
        HEADER + "llmail,C3,ASR,all,0,1000,0.0,0,0,\n", encoding="utf-8"
    )
    kpi = load_kpi(tmp_path)
    gpt = _lines("email", kpi)["gpt-4o-mini"]
    assert gpt.c0 is not None and gpt.c0.text == NOT_AVAILABLE
    assert (gpt.c3.text, gpt.c3.n) == ("100.0%", f"n = 1000/{NOT_AVAILABLE}")
    llama = _lines("email", kpi)["Llama 3.1 8B"]
    assert llama.c3.text == NOT_AVAILABLE
    assert kpi.provenance is None


async def test_results_page_figures_and_allow_list(
    playground_app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    figures = tmp_path / "paper" / "figures" / "final" / "3models"
    figures.mkdir(parents=True)
    (figures / "fig1_guard_effect_c0_vs_c3.png").write_bytes(b"\x89PNG\r\n")
    (figures / "secret.txt").write_text("nope", encoding="utf-8")
    async with asgi_client(playground_app_factory()) as client:
        page = await client.get("/guard/results")
        png = await client.get("/guard/figures/fig1_guard_effect_c0_vs_c3.png")
        other = await client.get("/guard/figures/secret.txt")
        traversal = await client.get("/guard/figures/..%2F..%2Fsettings.py")
        guard = await client.get("/guard")
    assert page.status_code == 200
    html = page.text
    assert html.count('<figure class="figure"') == 6
    assert 'src="/guard/figures/fig1_guard_effect_c0_vs_c3.png"' in html
    assert "figure not available: fig2_defense_rate_by_configuration.png" in html
    assert 'href="/guard"' in html and "Defense rate = 100 − ASR" in html
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert other.status_code == 404 and traversal.status_code == 404
    assert 'href="/guard/results"' in guard.text and "Full results" in guard.text
