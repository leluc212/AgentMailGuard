"""What a report of a scheme-v3 run adds: the config table and the protocol tables.

Scheme v3 (``scheme.py``) reads a run as one experiment on the guard v3 presets (ADR-0015): C0 is
rag-email without a guard, C0T the guard's prompt template with no layer, C1 the Inbound Guard and
Gate, C2 adds the Doc Scanner and channel isolation, C3 is the full guard, and each of C3-L1 ..
C3-L4 and C3-ISO removes one part of C3. The headline tests (C0 and C0T against C3) and the
ablation pairs are the report's own; this module adds what the v3 report must show beyond the
final action (plan_v3 Task 13, review 10-15): where each job stopped and why.

    1. exit points        gate pass 1, gate pass 2, output guard, delivered (counts per config)
    2. inbound floor      jobs whose inbound floor won the merge in the Output Guard
    3. guard calls        judge calls per stage, mean and max per job; ``doc_set`` has its own
                          column so "at most one set call, only with a chunk in the band" is visible
    4. fail-closed        jobs in which a stage failed closed (``judge_invalid``); a quarantine or a
                          review caused that way is its own column and no detection
    5. route metrics      F8: the inbound catch rate and "every poisoned chunk caught"

ASR and the poisoned-chunk catch keep their v1 definitions, so the tables stay comparable with
Review 1. Pure: it reads ``ProtocolTable`` and ``Overhead`` values and prints text, and imports
nothing from the guard, so CI covers it.

(docs/adr/0015-guard-architecture-v3.md; plan_v3/exec-plan-chi-tiet.md Task 13; R22.12)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from evaluation.mailguard_bench.overhead import Overhead
from evaluation.mailguard_bench.protocol import EXIT_POINTS, ProtocolTable
from evaluation.mailguard_bench.scheme import GUARD_STAGES, V3_AI_STAGES, V3_CONFIGS, V3_LAYERS

VECTOR_LLMAIL = "LLMail-Inject"
VECTOR_RAG = "RAG vector"
MAX_GUARD_CALLS_PER_JOB = 5
"""The guard's ceiling on judge calls per job (ADR-0015 decision 12)."""

LAYER_LABELS = {
    "inbound_guard": "L1 Inbound Guard",
    "doc_scanner": "L2 Doc Scanner",
    "inbound_gate": "L3 Inbound Gate",
    "output_guard": "L4 Output Guard",
    "channel_isolation": "channel isolation",
}
STAGE_LABELS = {
    "inbound_judge": "L1 scan judge",
    "inbound_extractor": "L1 intent extraction",
    "doc_judge": "L2 chunk judge",
    "doc_set_judge": "L2 set judge",
    "output_judge": "L4 output check",
}
NO_LAYER_TEXT = {
    "C0": "none (rag-email's own prompt, no guard)",
    "C0T": "none (the guard's prompt template)",
}
EXIT_LABELS = {
    "gate_pass_1": "gate pass 1",
    "gate_pass_2": "gate pass 2",
    "output_guard": "output guard",
    "delivered": "delivered",
}


@dataclass(frozen=True)
class SchemeV3Section:
    """The v3-only parts of a report: which configs ran and the per-config protocol tables."""

    configs_run: tuple[str, ...]
    protocol: Mapping[str, ProtocolTable] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """The ``scheme_v3`` block of ``summary.json``."""
        return {
            "configs_run": list(self.configs_run),
            "protocol": {config: table.as_dict() for config, table in self.protocol.items()},
        }


def _names(configs: Sequence[str]) -> str:
    """``C0, C0T and C6`` from ``["C0", "C0T", "C6"]``."""
    if len(configs) < 2:
        return "".join(configs)
    return f"{', '.join(configs[:-1])} and {configs[-1]}"


def configs_without_ai_stage(configs_run: Sequence[str]) -> list[str]:
    """The configs of ``configs_run`` that run no guard AI step (C0, C0T)."""
    return [c for c in configs_run if not V3_AI_STAGES[c]]


def _config_table(configs_run: Sequence[str]) -> list[str]:
    out = [
        "| Config | Parts active | Guard AI stages live | Ran |",
        "|---|---|---|---|",
    ]
    for config in V3_CONFIGS:
        layers = V3_LAYERS[config]
        layer_text = NO_LAYER_TEXT.get(config) or " + ".join(
            LAYER_LABELS[layer] for layer in layers
        )
        stages = ", ".join(STAGE_LABELS[s] for s in V3_AI_STAGES[config]) or "none"
        ran = "yes" if config in configs_run else "no"
        out.append(f"| {config} | {layer_text} | {stages} | {ran} |")
    return out


def render_config_section(section: SchemeV3Section) -> list[str]:
    """``## Config scheme v3``: what each name means in this run."""
    return [
        "## Config scheme v3",
        "",
        "This run uses config scheme v3 (ADR-0015, plan_v3 Task 12). The names are the guard v3 "
        "presets, not the names of the published v1 or v2 runs, so v1/v2 and v3 numbers are never "
        "compared. Every config runs on the same pinned cases; every judge of a part the config "
        "runs is live, and a judge that fails or answers outside its schema fails closed.",
        "",
        *_config_table(section.configs_run),
        "",
    ]


def _frac(count: int, total: int) -> str:
    return f"{count} of {total} ({100 * count / total:.1f} %)" if total else "n/a"


def _calls_cell(table: ProtocolTable, stage: str) -> str:
    calls = table.calls[stage]
    return f"{calls.mean:.2f} / {calls.max}"


def render_protocol_sections(section: SchemeV3Section) -> list[str]:
    """The five protocol tables, one row per guarded config that ran."""
    protocol = section.protocol
    if not protocol:
        return []
    out = [
        "## Where each job stopped, and why",
        "",
        "Read from each scored job's saved guard report and its generation call count, no API. "
        "Jobs are the scored emails that carry a guard report (C0 and triage-stopped emails have "
        "none). The exit point is the gate pass that blocked or quarantined the job before any "
        "model call, the output guard when the draft was held (human approval, block or "
        "quarantine) after the one generation call, and delivered otherwise.",
        "",
        "### Exit points",
        "",
        "| Config | Jobs | " + " | ".join(EXIT_LABELS[e] for e in EXIT_POINTS) + " |",
        "|---|---|" + "---|" * len(EXIT_POINTS),
    ]
    for config, table in protocol.items():
        cells = " | ".join(str(table.exits[e]) for e in EXIT_POINTS)
        out.append(f"| {config} | {table.jobs} | {cells} |")
    out += [
        "",
        "### Inbound floor",
        "",
        "A job whose inbound gate decision was stricter than the Output Guard's keeps it: the "
        "inbound floor won the merge (`carried_from_inbound`). It does not change the exit point.",
        "",
        "| Config | Jobs | Inbound floor won |",
        "|---|---|---|",
    ]
    for config, table in protocol.items():
        out.append(f"| {config} | {table.jobs} | {table.floor_won} |")
    out += [
        "",
        "### Guard calls per stage",
        "",
        "Judge calls per job as the guard counts them (`guard_calls`), mean / max. `doc_chunk` "
        'counts per chunk and can exceed one; the claim "at most one set call, only when a chunk '
        'is in the band" is read in the `doc_set` column alone. The ceiling is '
        f"{MAX_GUARD_CALLS_PER_JOB} calls per job.",
        "",
        "| Config | Jobs | " + " | ".join(GUARD_STAGES) + " | most per job |",
        "|---|---|" + "---|" * (len(GUARD_STAGES) + 1),
    ]
    for config, table in protocol.items():
        cells = " | ".join(_calls_cell(table, stage) for stage in GUARD_STAGES)
        out.append(f"| {config} | {table.jobs} | {cells} | {table.max_calls_per_job} |")
    out += [
        "",
        "### Fail-closed events",
        "",
        "Jobs in which a judge failed or answered outside its schema and failed closed "
        "(`judge_invalid:<stage>`), per stage. A chunk quarantined only that way and a job the "
        "fail-closed rule (P00) held for review are counted in their own columns and are not "
        "detections.",
        "",
        "| Config | Jobs | "
        + " | ".join(GUARD_STAGES)
        + " | quarantined by fail-closed | held by P00 |",
        "|---|---|" + "---|" * (len(GUARD_STAGES) + 2),
    ]
    for config, table in protocol.items():
        cells = " | ".join(str(table.fail_closed[stage]) for stage in GUARD_STAGES)
        out.append(
            f"| {config} | {table.jobs} | {cells} | {table.fail_closed_quarantined} | "
            f"{table.fail_closed_review} |"
        )
    out += [
        "",
        "### Route metrics (ablation criterion e)",
        "",
        "The inbound catch rate is the LLMail-Inject attacks stopped at gate pass 1 or 2, before "
        'any model call, over all scored LLMail-Inject attacks. "Every poisoned chunk caught" '
        "is the RAG attacks whose poisoned chunk was retrieved, in which the Doc Scanner "
        "quarantined every retrieved poisoned chunk. The final ASR is reported for every config "
        "but is not the criterion: with L1 off the Output Guard can still catch an injection.",
        "",
        "| Config | Inbound catch rate | Every poisoned chunk caught |",
        "|---|---|---|",
    ]
    for config, table in protocol.items():
        out.append(
            f"| {config} | {_frac(table.inbound_caught, table.attacks)} | "
            f"{_frac(table.poison_all_caught, table.poison_cases)} |"
        )
    return [*out, ""]


def _ms(value: float) -> str:
    return f"{value / 1000:.2f} s"


def render_overhead_table(overheads: Mapping[str, Overhead]) -> list[str]:
    """Guard calls per job and the guard and end-to-end latency, for the table next to the ASR.

    Guard calls per job come from the guard's own count (``report.metadata["guard_calls"]``);
    the provider tally (``guard_calls_per_email``) prices the cost table and is shown beside it,
    because retries and route failures make the two differ.
    """
    if not overheads:
        return []
    out = [
        "### Guard overhead per job",
        "",
        "| Config | n | guard calls / job (guard count) | most calls in a job | "
        "guard calls / email (provider tally) | guard p50 / p95 | end-to-end p50 / p95 |",
        "|---|---|---|---|---|---|---|",
    ]
    for config, o in overheads.items():
        out.append(
            f"| {config} | {o.n} | {o.guard_calls_per_job:.2f} | {o.guard_calls_max} | "
            f"{o.guard_calls_per_email:.2f} | {_ms(o.guard_ms.p50)} / {_ms(o.guard_ms.p95)} | "
            f"{_ms(o.total_ms.p50)} / {_ms(o.total_ms.p95)} |"
        )
    return [*out, ""]


def render_no_ai_stage_note(configs_run: Sequence[str]) -> list[str]:
    """The fallback section's note on the configs that have no AI step to fall back."""
    without = configs_without_ai_stage(configs_run)
    if not without:
        return []
    if len(without) == 1:
        return [f"{without[0]} runs no guard AI step, so it has no fallbacks to report.", ""]
    return [f"{_names(without)} run no guard AI step, so they have no fallbacks to report.", ""]
