"""Which AgentMailGuard layer stopped each defended C3 attack (from saved reports, no API).

Definition used in the report: for a blocked case, the first layer in pipeline order
(L1, L2, L3b, L3, L4) whose verdict flagged it (severity >= MEDIUM, L3b: a quarantined
chunk); if none flagged, the L5 rule that blocked it. For an unblocked case that did not
succeed: "L4 redaction" when the draft before L4 carried out the attack, otherwise no
layer can be credited (the model did not follow the injection, possibly helped by L3's
spotlighting, which leaves no verdict to attribute).

(docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md §4b "First catching
layer"; specs/tasks.md 7.19)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

from evaluation.mailguard_bench.scoring import L1, L2, L3, L3B, L4, RawRecord, flagged_layers

LABELS = {
    L1: "L1 injection scanner",
    L2: "L2 intent extractor",
    L3B: "L3b document scanner",
    L3: "L3 channel isolation",
    L4: "L4 output scanner",
}
L4_REDACTION = "L4 redaction (draft not blocked)"
NO_LAYER = "no layer: the model did not follow the injection"


def first_catching_layer(record: RawRecord, *, goal: bool, goal_pre_l4: bool) -> str | None:
    """Label of the layer that stopped this attack, or ``None`` if the attack succeeded."""
    if goal:
        return None
    if record.blocked:
        flagged = flagged_layers(record.report)
        if flagged:
            return LABELS[flagged[0]]
        decision = (record.report or {}).get("decision") or {}
        return f"L5 policy rule {decision.get('matched_rule_id') or 'unknown'}"
    if goal_pre_l4:
        return L4_REDACTION
    return NO_LAYER


def tally(labels: Iterable[str | None]) -> dict[str, int]:
    """Counts per label (successful attacks, ``None``, are left out), largest first."""
    counts = Counter(label for label in labels if label is not None)
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def bar_chart(counts: Mapping[str, int], *, width: int = 40) -> str:
    """Plain-text horizontal bar chart (renders in any Markdown viewer and on a slide)."""
    if not counts:
        return "(no defended attacks)"
    total = sum(counts.values())
    top = max(counts.values())
    label_w = max(len(k) for k in counts)
    lines = []
    for label, n in counts.items():
        bar = "#" * max(1, round(width * n / top))
        lines.append(f"{label:<{label_w}}  {bar:<{width}}  {n:>4}  ({100 * n / total:.1f} %)")
    return "\n".join(lines)


def render_first_layer(counts: Mapping[str, int]) -> str:
    """Markdown section for ``analyses.md``."""
    return "\n".join(
        [
            "## First catching layer (C3, defended attacks)",
            "",
            "```text",
            bar_chart(counts),
            "```",
            "",
            "Pipeline order L1 → L2 → L3b → L3 → (generation) → L4 → L5. A layer is credited "
            "when it is the first to flag a blocked case (severity ≥ MEDIUM); a block with no "
            "flag is credited to the L5 rule that fired.",
        ]
    )
