"""KPI_Strip and Results_Page data (Requirement 18; design.md "core/kpi.py").

Read-only: every value comes from the ``metrics.csv``, ``cases.jsonl`` and ``manifest.json`` of
the three 1,800-case runs of 2026-10-19. Nothing is computed from model calls and nothing is
written. A missing file or row turns only the affected value into "not available".
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

NOT_AVAILABLE = "not available"

# (directory, display name) in the display order of Requirement 18.4.
RUNS: tuple[tuple[str, str], ...] = (
    ("2026-10-19-gpt4omini-v3-1800", "gpt-4o-mini"),
    ("2026-10-19-llama31-8b-openrouter-v3-1800", "Llama 3.1 8B"),
    ("2026-10-19-qwen25-7b-openrouter-v3-1800", "Qwen 2.5 7B"),
)

# Case kind of a metrics table, for the "planned" count (Requirement 18.6).
EMAIL_ATTACK, RAG_POISON, BENIGN = "email_attack", "rag_poison", "benign"


@dataclass(frozen=True)
class Card:
    key: str
    title: str
    subtitle: str
    table: str
    metric: str
    planned_kind: str
    defense_rate: bool  # True: 100 - ASR, shown C0 -> C3; False: value at C3 only


CARDS: tuple[Card, ...] = (
    Card(
        "email",
        "Email attacks (LLMail-Inject)",
        "Defense rate = 100 − ASR, C0 → C3",
        "llmail",
        "ASR",
        EMAIL_ATTACK,
        True,
    ),
    Card(
        "rag",
        "RAG poison cases",
        "Defense rate = 100 − ASR, C0 → C3",
        "rag",
        "ASR",
        RAG_POISON,
        True,
    ),
    Card(
        "fpr",
        "Benign emails blocked",
        "False-positive rate at C3",
        "llmail",
        "FPR",
        BENIGN,
        False,
    ),
)


@dataclass(frozen=True)
class Value:
    text: str  # "75.7%" or "not available"
    n: str  # "n = 999/1000" or "n = 999/not available"


@dataclass(frozen=True)
class CardLine:
    model: str
    run_dir: str
    c0: Value | None  # None on the false-positive card
    c3: Value


@dataclass(frozen=True)
class CardView:
    key: str
    title: str
    subtitle: str
    lines: list[CardLine]


@dataclass
class KpiStrip:
    cards: list[CardView] = field(default_factory=list)
    provenance: str | None = None


def round1(value: Decimal) -> str:
    """Percentage with exactly 1 decimal, half away from zero: ``Decimal("75.68") -> "75.7%"``."""
    sign = -1 if value < 0 else 1
    rounded = (abs(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) * sign
    return f"{rounded:.1f}%"


def _read_metrics(path: Path) -> dict[tuple[str, str, str, str], dict[str, str]] | None:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return {
        (r.get("table", ""), r.get("config", ""), r.get("metric", ""), r.get("group", "")): r
        for r in rows
    }


def _planned(path: Path) -> dict[str, int] | None:
    """Count of cases per kind in a run's ``cases.jsonl``; None when the file is unreadable."""
    counts: Counter[str] = Counter()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    case = json.loads(line)
                except ValueError:
                    continue
                if case.get("kind") == "benign":
                    counts[BENIGN] += 1
                elif case.get("vector") == "rag":
                    counts[RAG_POISON] += 1
                else:
                    counts[EMAIL_ATTACK] += 1
    except (OSError, UnicodeDecodeError):
        return None
    return dict(counts)


def _value(
    rows: dict[tuple[str, str, str, str], dict[str, str]] | None,
    card: Card,
    config: str,
    planned: dict[str, int] | None,
) -> Value:
    planned_text = NOT_AVAILABLE if planned is None else str(planned.get(card.planned_kind, 0))
    row = None if rows is None else rows.get((card.table, config, card.metric, "all"))
    if row is None:
        return Value(NOT_AVAILABLE, f"n = {NOT_AVAILABLE}/{planned_text}")
    total = (row.get("total") or "").strip() or NOT_AVAILABLE
    n = f"n = {total}/{planned_text}"
    try:
        pct = Decimal((row.get("pct") or "").strip())
    except InvalidOperation:
        return Value(NOT_AVAILABLE, n)
    return Value(round1(Decimal(100) - pct if card.defense_rate else pct), n)


def provenance_line(manifest_path: Path) -> str | None:
    """Guard commit, rag-email commit, case set sha256 and L1 sha256 of a run (Req 18.13)."""
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        run = manifest["runs"]["C3"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    parts = [
        ("guard", str(run.get("mailguard_commit") or "")[:7]),
        ("rag-email", str(run.get("rag_email_commit") or "")[:7]),
        ("cases sha256", str(run.get("cases_sha256") or "")[:12]),
        ("L1 sha256", str(run.get("l1_model_sha256") or "")[:12]),
    ]
    return " · ".join(f"{label} {value}" for label, value in parts if value)


def load_kpi(results_root: Path) -> KpiStrip:
    """The 3 cards × 3 models of Requirement 18 from the runs under ``results_root``."""
    per_run = []
    for run_dir, model in RUNS:
        base = results_root / run_dir
        per_run.append(
            (run_dir, model, _read_metrics(base / "metrics.csv"), _planned(base / "cases.jsonl"))
        )
    cards = []
    for card in CARDS:
        lines = [
            CardLine(
                model=model,
                run_dir=run_dir,
                c0=_value(rows, card, "C0", planned) if card.defense_rate else None,
                c3=_value(rows, card, "C3", planned),
            )
            for run_dir, model, rows, planned in per_run
        ]
        cards.append(CardView(card.key, card.title, card.subtitle, lines))
    return KpiStrip(
        cards=cards, provenance=provenance_line(results_root / RUNS[0][0] / "manifest.json")
    )


@dataclass(frozen=True)
class Figure:
    stem: str
    caption: str
    alt: str


FIGURES: tuple[Figure, ...] = (
    Figure(
        "fig1_guard_effect_c0_vs_c3",
        "Figure 1. Guard effect: defense rate without the guard (C0) and with the full guard (C3)",
        "Bar chart comparing the defense rate at C0 and C3 for email attacks and RAG poison "
        "cases, for gpt-4o-mini, Llama 3.1 8B and Qwen 2.5 7B.",
    ),
    Figure(
        "fig2_defense_rate_by_configuration",
        "Figure 2. Defense rate by guard configuration",
        "Bar chart of the defense rate for each guard configuration from C0 to C3 and the "
        "ablations, for the three models.",
    ),
    Figure(
        "fig3_layer_ablation",
        "Figure 3. Layer ablation: C3 with one layer removed",
        "Bar chart showing how the defense rate changes when one guard layer is removed from "
        "C3, for the three models.",
    ),
    Figure(
        "fig4_rag_defense_by_source",
        "Figure 4. RAG defense by source (PoisonedRAG, seed, synthetic_poison_v1)",
        "Bar chart of the RAG defense rate split by case source, for the three models.",
    ),
    Figure(
        "fig5_benign_cost",
        "Figure 5. Cost on benign emails: false positives and utility",
        "Bar chart of the false-positive rate and benign utility at C0 and C3, for the three "
        "models.",
    ),
    Figure(
        "fig6_summary_table",
        "Figure 6. Summary table of the three models",
        "Table summarising defense rates, false-positive rate and sample sizes for the three "
        "models.",
    ),
)

ALLOWED_FIGURE_FILES: frozenset[str] = frozenset(
    f"{f.stem}.{ext}" for f in FIGURES for ext in ("png", "pdf")
)
