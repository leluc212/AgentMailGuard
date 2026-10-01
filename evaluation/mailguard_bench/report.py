"""Score a benchmark run and write manifest.json, metrics.csv and report.md.

Makes no model calls. Run from the rag-email root with the AgentMailGuard worktree on
the path (its scorer and metrics are used unchanged)::

    make mailguard-report RUN=<run_id>

Reads ``<run_dir>/cases.jsonl``, ``case_manifest.json`` and ``raw/<config>.jsonl`` (+
``raw/<config>.meta.json``, and ``analysis/meaning__<config>.jsonl`` when the meaning reader
has run, see ``meaning.py``); writes ``ragemail__<config>.jsonl`` (AgentMailGuard
``CaseResult`` rows, the file pattern its ``evaluation/report.py`` loads),
``summary.json``, ``manifest.json``, ``metrics.csv`` and ``report.md``.

A run of the live pipeline (``mailguard-bench-result.v3`` rows, task 7.20) is scored the same
way; its report also states the ASR over the attacks that reached the drafting step (the guard
ASR, on which the C3 target is judged) next to the pipeline ASR, and adds a triage table.

(docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md §4, §4b;
docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md §E; specs/tasks.md 7.6, 7.19,
7.20; R22.12, R24.5)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from evaluation.mailguard_bench.amg import (
    load_amg_harness,
    load_amg_metrics,
    resolve_mailguard_dir,
)
from evaluation.mailguard_bench.artifacts import (
    ConfigSummary,
    LayerAblation,
    LayerAblationRow,
    LayerPair,
    MeaningOutcome,
    MeaningSummary,
    RateCI,
    ReportInputs,
    ServiceFailures,
    build_manifest,
    git_head,
    guard_escalated,
    layer_ablation_rows,
    metrics_rows,
    partial_note,
    render_report,
    scheme_v2_rows,
    sha256_file,
    summarize_config,
    summarize_fallbacks,
    summarize_meaning,
    summarize_triage,
    template_successes,
    write_metrics_csv,
)
from evaluation.mailguard_bench.first_layer import Attribution, attribute_attacks
from evaluation.mailguard_bench.guard_build import ABLATION_CONFIGS, LAYER_ABLATIONS
from evaluation.mailguard_bench.guard_env import guard_layout
from evaluation.mailguard_bench.meaning import (
    PROMPT_SHA256,
    RUBRIC_VERSION,
    current_verdict,
    meaning_path,
)
from evaluation.mailguard_bench.model_profiles import BENCH_MODELS
from evaluation.mailguard_bench.overhead import Overhead, overhead
from evaluation.mailguard_bench.results import ResultStore
from evaluation.mailguard_bench.run_setup import render_run_setup, run_setup
from evaluation.mailguard_bench.runmeta import prices_from_meta, strict_utility_from_meta
from evaluation.mailguard_bench.scheme import (
    SCHEME_KEY,
    SCHEME_V1,
    SCHEME_V2,
    V2_LAYERS,
    V2_TARGET_CONFIG,
    configs_for,
    folder_scheme,
    scheme_of_meta,
    target_config,
    v2_ai_layer_names,
    v2_guard_name,
    v2_required_stages,
)
from evaluation.mailguard_bench.scheme_report import (
    VECTOR_LLMAIL,
    VECTOR_RAG,
    SchemeV2Section,
    build_control,
    build_pairs,
)
from evaluation.mailguard_bench.scoring import (
    FAIL_CLOSED_KIND,
    GUARD_ROUTE_FAILURE_KIND,
    LIVE_TRANSPORT,
    RETRIEVAL_DEGRADED_KIND,
    TRIAGE_STAGE_FAILURE_KIND,
    RawRecord,
    read_raw,
    real_benign_drafts,
    score_records,
)
from packages.core.settings import AppSettings, ModelPricing

REPO_ROOT = Path(__file__).resolve().parents[2]
# C0 = rag-email native (no AgentMailGuard code); C0T/C1/C2/C3 = AgentMailGuard presets.
# C0 and C0T are required baselines (B1 = a, b); a missing one is stated in the report, never
# refused. C1 and C2 are optional ablation extras.
CONFIG_ORDER = ("C0", "C0T", "C1", "C2", "C3")
# The layer ablation (task 7.22) adds C3-L1..C3-L5: the full guard minus one layer, full case set.
ALL_CONFIGS = (*CONFIG_ORDER, *ABLATION_CONFIGS)
# Same-run reference rows of the layer-ablation table, and what each removes from the guard.
REFERENCE_REMOVED = {"C0": "all", "C3": "none"}
MAIN_CONFIGS = ("C0", "C0T", "C3")  # full case set; C1/C2 run only the ablation subset
BASELINES = ("C0", "C0T")  # each is paired against C3 with McNemar when both ran
AGENT = "ragemail"
FULL_LAYERS = ("l1", "l2", "l3", "l3b", "l4", "l5")
# Settings every compared config must share (spec Q6: "same model, same settings").
# preset, guard, live_layers, guard_models and degraded_allowed differ by design. transport,
# reranker and triage are the live pipeline's (task 7.20); a v1 meta has none of them, so they
# are equal (None) across the configs of a v1 run. guarded_prompt_version is the prompt of the
# guarded configs; every config of a run records it, so a v1 (none) and a v2 run never mix. scheme
# is the config scheme (scheme.py): a meta without it is v1, so old and new v1 metas agree.
SHARED_SETTINGS = (
    "scheme",
    "cases_sha256",
    "rag_email_commit",
    "mailguard_commit",
    "guarded_prompt_version",
    "generation_model",
    "generation",
    "l1_model_sha256",
    "embedding",
    "retrieval",
    "database",
    "transport",
    "reranker",
    "triage",
)


def _v2_degradation_problems(config: str, meta: Mapping[str, Any] | None) -> list[str]:
    """``degradation_problems`` for a scheme-v2 config: exactly its layers, and its guard config.

    C0 is rag-email's native path (no guard at all); C0T runs the guard's template with no layer;
    C1 to C7 must be the guard config built from that config's explicit layer flags
    (``scheme.V2_LAYERS``, named ``v2-<config>``), with every stage the config needs live.
    """
    if meta is None:
        return (
            [f"{config}: raw/{config}.meta.json is missing"] if config == V2_TARGET_CONFIG else []
        )
    guard = meta.get("guard") or {}
    active = tuple(guard.get("active_layers") or ())
    problems: list[str] = []
    if config == "C0":
        if meta.get("guard_preset") is not None:
            problems.append(
                f"C0: guard preset {meta.get('guard_preset')!r} ran, but C0 is rag-email's "
                "native path"
            )
        if active:
            problems.append(f"C0: active layers {list(active)} are not []")
        return problems
    name = v2_guard_name(config)
    if meta.get("guard_preset") != name:
        problems.append(f"{config}: guard preset {meta.get('guard_preset')!r} is not {name!r}")
    if active != V2_LAYERS[config]:
        problems.append(f"{config}: active layers {list(active)} are not {list(V2_LAYERS[config])}")
    missing = list(guard.get("missing_live_stages") or [])
    recorded = guard.get("live_stages") or {}
    # A stage the config needs is live only if live_stages says so: a meta that records none
    # (hand-made or truncated) verifies nothing, so it is not a pass (Amendment 2).
    off = [stage for stage in v2_required_stages(config) if not recorded.get(stage)]
    not_live = [*missing, *(stage for stage in off if stage not in missing)]
    if not_live:
        unrecorded = " (no live_stages recorded)" if off and not recorded else ""
        problems.append(f"{config}: guard stages not live: {', '.join(not_live)}{unrecorded}")
    if meta.get("degraded_allowed"):
        problems.append(f"{config}: started with --allow-degraded")
    return problems


def degradation_problems(
    config: str, meta: Mapping[str, Any] | None, *, scheme: str | None = None
) -> list[str]:
    """Why a guarded run would describe a weaker guard than its preset (empty when none).

    ``scheme`` is the config scheme of the run folder (``scheme.py``); by default it is the one
    the meta records, and a meta without one is v1. A scheme-v2 config is checked against its own
    layer flags (``_v2_degradation_problems``); the rest of this docstring is scheme v1.

    C0 is rag-email's native path: it must have run no guard preset and no layer
    (settings_problems requires its meta). C0T is a guarded config (the guard's template,
    preset "C0", no layer active) and is checked like C1/C2. A C3 run must have its run meta,
    all six layers active and every LLM stage it needs live; C1/C2 must have no missing stage.
    A layer-ablation config C3-L<n> must be the guard's preset of that name, with every layer
    active except L<n>, and no missing stage.
    """
    if (scheme or scheme_of_meta(meta)) == SCHEME_V2:
        return _v2_degradation_problems(config, meta)
    if meta is None:
        return [f"{config}: raw/{config}.meta.json is missing"] if config == "C3" else []
    guard = meta.get("guard") or {}
    active = tuple(guard.get("active_layers") or ())
    problems: list[str] = []
    if config == "C0":
        if meta.get("guard_preset") is not None:
            problems.append(
                f"C0: guard preset {meta.get('guard_preset')!r} ran, but C0 is rag-email's "
                "native path"
            )
        if active:
            problems.append(f"C0: active layers {list(active)} are not []")
        return problems
    if config in LAYER_ABLATIONS:
        # The preset must be the guard's own "C3 minus one layer": exactly that layer off.
        expected = [layer for layer in FULL_LAYERS if layer != LAYER_ABLATIONS[config]]
        if meta.get("guard_preset") != config:
            problems.append(
                f"{config}: guard preset {meta.get('guard_preset')!r} is not {config!r}"
            )
        if list(active) != expected:
            problems.append(f"{config}: active layers {list(active)} are not {expected}")
    if config == "C0T":
        if meta.get("guard_preset") != "C0":
            problems.append(f"C0T: guard preset {meta.get('guard_preset')!r} is not 'C0'")
        if active:
            problems.append(f"C0T: active layers {list(active)} are not []")
    missing = list(guard.get("missing_live_stages") or [])
    if missing:
        problems.append(f"{config}: guard stages not live: {', '.join(missing)}")
    if meta.get("degraded_allowed"):
        problems.append(f"{config}: started with --allow-degraded")
    if config == "C3" and active != FULL_LAYERS:
        problems.append(f"C3: active layers {list(active)} are not {list(FULL_LAYERS)}")
    return problems


def settings_problems(config: str, meta: Mapping[str, Any] | None) -> list[str]:
    """Why a run did not use one live benchmark model (empty when it did).

    Every scored config needs its run meta. The generation call must not be the fake
    provider and must use one of the benchmark's model profiles (owner decision
    2026-09-29: GPT-4o-mini, Llama-3.1-8B, Qwen2.5-7B, and the first test run's Gemma); a
    guarded config's guard stages must use that same model (the guard registry's own "fake"
    backend counts as a live stage, so the live-stage check alone does not catch it). C0 has
    no guard stage, so its guard model is ignored.
    """
    if meta is None:
        return [f"{config}: raw/{config}.meta.json is missing"]
    problems: list[str] = []
    generation = meta.get("generation") or {}
    if str(generation.get("provider") or "").strip().lower() == "fake":
        problems.append(f"{config}: generation provider is 'fake'")
    model = meta.get("generation_model")
    if model not in BENCH_MODELS:
        problems.append(f"{config}: generation model {model!r} is not a benchmark model profile")
    if config != "C0" and meta.get("guard_models") != model:
        problems.append(
            f"{config}: guard model {meta.get('guard_models')!r} is not the generation model "
            f"{model!r}"
        )
    return problems


def consistency_problems(
    run_meta: Mapping[str, Mapping[str, Any]], records: Mapping[str, Sequence[RawRecord]]
) -> list[str]:
    """Configs that ran under different settings, or rows whose model is not the meta's.

    A row of the live pipeline that did not reach the drafting step (a triage early exit or
    template draft) was written by no model, so its generation model is not checked. The rows
    of a config and its meta must also agree on whether the run was live: mixing them would
    pair a live fingerprint with in-process rows.
    """
    problems: list[str] = []
    for key in SHARED_SETTINGS:

        def shared(meta: Mapping[str, Any], key: str = key) -> Any:
            return scheme_of_meta(meta) if key == SCHEME_KEY else meta.get(key)

        values = {
            c: json.dumps(shared(m), sort_keys=True, default=str) for c, m in run_meta.items()
        }
        if len(set(values.values())) > 1:
            problems.append(
                f"configs ran with different {key}: "
                + ", ".join(f"{c}={shared(run_meta[c])!r}" for c in values)
            )
    for config, rows in records.items():
        want = (run_meta.get(config) or {}).get("generation_model")
        seen = sorted(
            {
                r.generation.model
                for r in rows
                if r.ok
                and r.generation.model
                and (r.pipeline is None or r.pipeline.reached_drafting)
            }
        )
        wrong = [model for model in seen if model != want]
        if wrong:
            problems.append(f"{config}: rows were generated by {wrong}, meta says {want!r}")
        live_rows = any(r.live for r in rows)  # error rows count: they have no pipeline block
        live_meta = (run_meta.get(config) or {}).get("transport") == LIVE_TRANSPORT
        if rows and live_rows and not live_meta:
            problems.append(
                f"{config}: its rows are from the live pipeline, but raw/{config}.meta.json "
                f"does not say transport {LIVE_TRANSPORT!r}"
            )
        if rows and live_meta and not live_rows:
            problems.append(
                f"{config}: raw/{config}.meta.json says transport {LIVE_TRANSPORT!r}, but its "
                "rows are not from the live pipeline"
            )
    return problems


def _commit_of(run_meta: Mapping[str, Mapping[str, Any]], key: str) -> dict[str, Any]:
    """The commit the runs used (all equal once consistency_problems passed)."""
    shas = sorted({str(m.get(key)) for m in run_meta.values() if m.get(key)})
    return {
        "sha": shas[0] if len(shas) == 1 else None,
        "runs": {c: m.get(key) for c, m in run_meta.items()},
    }


def poison_retrieved_ids(
    records: Mapping[str, Sequence[RawRecord]], configs: Sequence[str], ids: set[str]
) -> set[str]:
    """Cases of ``ids`` whose poisoned document reached the context in every config."""
    per_config = [{r.case_id for r in records[c] if r.ok and r.poison_retrieved} for c in configs]
    if not per_config:
        return set()
    return set.intersection(*per_config) & ids


def load_cases(path: Path) -> dict[str, dict[str, Any]]:
    """BenchCase dicts of the run keyed by ``case_id``."""
    cases: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                case = json.loads(line)
                cases[str(case["case_id"])] = case
    return cases


def _subset(rows: Sequence[Any], ids: set[str]) -> list[Any]:
    return [r for r in rows if r.case_id in ids]


def _errors_in(errors: Sequence[RawRecord], ids: set[str]) -> int:
    return sum(1 for e in errors if e.case_id in ids)


# Keys of a ConfigSummary that only a v2 run has: a v1 run's summary.json keeps the keys it had
# when published, so a regenerated one differs from the published one in no key.
V2_ONLY_SUMMARY_KEYS = ("utility_legacy", "sensitivity", "service_failures")


def summary_entry(summary: ConfigSummary) -> dict[str, Any]:
    """One config's ``summary.json`` entry, without the v2-only keys a v1 run leaves empty."""
    entry = asdict(summary)
    for key in V2_ONLY_SUMMARY_KEYS:
        if entry.get(key) is None:
            del entry[key]
    return entry


def _fail_closed_attacks_in(
    errors: Sequence[RawRecord], ids: set[str], cases: Mapping[str, Mapping[str, Any]]
) -> int:
    """Attack error rows of kind ``fail_closed_validation`` among ``ids`` (Amendment 1, E.1)."""
    return sum(
        1
        for e in errors
        if e.case_id in ids
        and e.error_kind == FAIL_CLOSED_KIND
        and cases[e.case_id].get("kind") == "attack"
    )


def _service_failures_in(errors: Sequence[RawRecord], ids: set[str]) -> ServiceFailures:
    """Error rows among ``ids`` that a live-service failure changed, by kind (Amendment 3)."""
    in_table = [e for e in errors if e.case_id in ids]
    return ServiceFailures(
        triage_stage_failure=sum(e.error_kind == TRIAGE_STAGE_FAILURE_KIND for e in in_table),
        retrieval_degraded=sum(e.error_kind == RETRIEVAL_DEGRADED_KIND for e in in_table),
        guard_route_failure=sum(e.error_kind == GUARD_ROUTE_FAILURE_KIND for e in in_table),
    )


# Files analyses.md is built from; their sha256 is kept in analysis/inputs.json so a report
# rescored after a resume never appends a first-layer tally or examples from older data.
ANALYSIS_SOURCES = ("ragemail__C0.jsonl", "ragemail__C3.jsonl", "raw/C0.jsonl", "raw/C3.jsonl")


def analysis_fingerprint(run_dir: Path) -> dict[str, str | None]:
    """sha256 of each ``ANALYSIS_SOURCES`` file of ``run_dir`` (``None`` when absent)."""
    return {name: sha256_file(run_dir / name) for name in ANALYSIS_SOURCES}


def analysis_inputs(
    run_dir: Path,
    scored: Mapping[str, list[Any]],
    *,
    metrics: ModuleType,
    llmail_ids: set[str],
    planned_attacks: int = 0,
    planned_benign: int = 0,
    errored_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Extra headline lines and report sections from the no-API analyses (task 6).

    With ``analysis/leakage.json`` present, C3 ASR is restated without the attacks that
    are near-duplicates of the classifier-training half, and C3 FPR without the benign
    emails that were L1 training rows; like the headline, each restated line is labelled
    partial when fewer than the planned C3 cases (``planned_attacks``/``planned_benign``)
    were scored, and near-duplicates among the C3 ``errored_ids`` are counted as not scored.
    ``analyses.md`` is appended as-is only when
    ``analysis/inputs.json`` matches the current scoring; otherwise a line says it is out
    of date (the leakage restatement depends only on the pinned case set, so it stays).

    On a live run (rows that say whether the email reached the drafting step) the headline is
    the guard ASR and the guard FPR, so the restated lines count only the cases that reached it,
    and the FPR counts every guard escalation (``guard_escalated``), as the scorecard's does.
    """
    headline: list[str] = []
    sections: list[str] = []
    leak_path = run_dir / "analysis" / "leakage.json"
    if leak_path.exists() and "C3" in scored:
        leak = json.loads(leak_path.read_text(encoding="utf-8"))
        c3 = [r for r in scored["C3"] if r.case_id in llmail_ids]
        attacks = [r for r in c3 if r.kind == "attack"]
        benign = [r for r in c3 if r.kind == "benign"]
        live = any("reached_drafting" in r.extra for r in c3)
        counted_attacks = [r for r in attacks if not live or r.extra.get("reached_drafting")]
        counted_benign = [r for r in benign if not live or r.extra.get("reached_drafting")]
        train_half = leak.get("attacks_vs_train_half") or {}
        if train_half.get("n_reference"):
            dup = set(train_half.get("near_duplicate_ids") or [])
            kept = [r for r in counted_attacks if r.case_id not in dup]
            rate = RateCI.of(metrics.Proportion(sum(r.goal_achieved for r in kept), len(kept)))
            partial = " (partial)" if len(attacks) < planned_attacks else ""
            line = (
                f"C3 {'guard ASR' if live else 'ASR'} without the "
                f"{len(counted_attacks) - len(kept)} near-duplicate(s) of the "
                f"classifier-training half (TF-IDF cosine ≥ 0.9){partial}: {rate.fmt()}."
            )
            errored = len(dup & errored_ids)
            if errored:
                line += f" {errored} near-duplicate(s) errored and are not scored."
            headline.append(line)
        fp_rows = leak.get("benign_vs_l1_train_rows") or {}
        if fp_rows.get("n_reference") and benign:
            dup = set(fp_rows.get("near_duplicate_ids") or [])
            kept = [r for r in counted_benign if r.case_id not in dup]
            flagged = [guard_escalated(r) if live else bool(r.blocked) for r in kept]
            rate = RateCI.of(metrics.Proportion(sum(flagged), len(kept)))
            partial = " (partial)" if len(benign) < planned_benign else ""
            headline.append(
                f"C3 {'guard FPR' if live else 'FPR'} on benign emails that were not L1 "
                f"training rows ({len(counted_benign) - len(kept)} excluded){partial}: "
                f"{rate.fmt()}."
            )
    analyses_md = run_dir / "analyses.md"
    if analyses_md.exists():
        inputs_path = run_dir / "analysis" / "inputs.json"
        recorded = (
            json.loads(inputs_path.read_text(encoding="utf-8")) if inputs_path.exists() else None
        )
        if recorded == analysis_fingerprint(run_dir):
            sections.append(analyses_md.read_text(encoding="utf-8"))
        else:
            sections.append(
                "## No-API analyses\n\n"
                "analyses.md is out of date for this scoring (the scored or raw C0/C3 results "
                "changed since it was built), so its first-catching-layer tally and worked "
                f"examples are left out — run `make mailguard-analyses RUN={run_dir.name}`."
            )
    return headline, sections


@dataclass(frozen=True)
class LayerAblationResult:
    """The layer-ablation numbers of a run: the report section and the summary tables."""

    summary: LayerAblation
    tables: dict[str, dict[str, ConfigSummary]]


def layer_ablation(
    layer_configs: Sequence[str],
    *,
    scored: Mapping[str, list[Any]],
    records: Mapping[str, Sequence[RawRecord]],
    errors: Mapping[str, Sequence[RawRecord]],
    llmail: Mapping[str, ConfigSummary],
    rag: Mapping[str, ConfigSummary],
    table: Callable[[set[str], Sequence[str]], dict[str, ConfigSummary]],
    metrics: ModuleType,
    manifest: Mapping[str, Any],
) -> LayerAblationResult | None:
    """Per-config ASR, FPR, real benign drafts and McNemar against the same-run C3.

    Returns ``None`` when no ``C3-L<n>`` config ran. The C3 row and the paired tests use the
    same-run C3 only (``c3_ran`` is False without it); the paired tests compare the same
    case ids of one vector, the ablation config as A and C3 as B.
    """
    if not layer_configs:
        return None
    llmail_attacks = set(manifest["llmail_attack_ids"])
    benign = set(manifest["benign_ids"])
    rag_ids = set(manifest.get("rag_attack_ids") or [])
    llmail_ids = llmail_attacks | benign
    c3_ran = "C3" in scored
    llmail_table = table(llmail_ids, layer_configs)
    rag_table = table(rag_ids, layer_configs) if rag_ids else {}
    empty = RateCI(0, 0, 0.0, 0.0)

    def row(config: str, llmail_s: ConfigSummary, rag_s: ConfigSummary | None) -> LayerAblationRow:
        real, total = real_benign_drafts(records[config], benign)
        return LayerAblationRow(
            config=config,
            removed=REFERENCE_REMOVED.get(config) or LAYER_ABLATIONS[config].upper(),
            llmail_asr=llmail_s.asr,
            rag_asr=rag_s.asr if rag_s is not None else empty,
            benign_fpr=llmail_s.fpr,
            real_drafts=RateCI.of(metrics.Proportion(real, total)),
        )

    # Same-run reference rows: C0 (no guard) and C3 (the full guard), when they ran.
    rows = [
        row(ref, llmail[ref], rag.get(ref))
        for ref in REFERENCE_REMOVED
        if ref in scored and ref in llmail
    ]
    attribution: dict[tuple[str, str], Attribution] = {}
    vectors = (
        ("LLMail-Inject", llmail_attacks),
        ("RAG vector", rag_ids),
    )
    for config in (*(["C3"] if c3_ran else []), *layer_configs):
        for label, attack_ids in vectors:
            if attack_ids:
                attribution[(config, label)] = attribute_attacks(
                    scored[config], records[config], attack_ids
                )
    pairs: list[LayerPair] = []
    notes: list[str] = []
    for config in layer_configs:
        rows.append(row(config, llmail_table[config], rag_table.get(config)))
        for label, ids, planned_ids, what, by_config in (
            ("LLMail-Inject", llmail_ids, llmail_attacks, "LLMail attacks", llmail_table),
            ("RAG vector", rag_ids, rag_ids, "RAG attacks", rag_table),
        ):
            if not ids:
                continue
            attack_asr = by_config[config].asr
            if attack_asr.total < len(planned_ids):
                notes.append(
                    f"{config}: "
                    + partial_note(
                        attack_asr.total,
                        len(planned_ids),
                        _errors_in(errors[config], planned_ids),
                        what=what,
                    )
                )
            if c3_ran:
                pairs.append(
                    LayerPair(
                        config,
                        label,
                        metrics.paired_comparison(
                            _subset(scored[config], ids), _subset(scored["C3"], ids)
                        ),
                    )
                )
        benign_scored = real_benign_drafts(records[config], benign)[1]
        if benign_scored < len(benign):
            notes.append(
                f"{config}: "
                + partial_note(
                    benign_scored,
                    len(benign),
                    _errors_in(errors[config], benign),
                    what="benign emails",
                )
            )
    tables = {"layer_ablation_llmail": llmail_table}
    if rag_table:
        tables["layer_ablation_rag"] = rag_table
    return LayerAblationResult(
        LayerAblation(
            rows=rows, pairs=pairs, c3_ran=c3_ran, partial_notes=notes, attribution=attribution
        ),
        tables,
    )


def _attribution_summary(
    attribution: Mapping[tuple[str, str], Attribution],
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{config: {vector: {attacks, succeeded, first_catching, flagged}}}`` for summary.json."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for (config, vector), att in attribution.items():
        out.setdefault(config, {})[vector] = asdict(att)
    return out


def build_report(
    run_dir: Path,
    *,
    harness: ModuleType,
    metrics: ModuleType,
    prices: Mapping[str, ModelPricing],
    mailguard_dir: Path,
) -> Path:
    """Score every ``raw/<config>.jsonl`` in ``run_dir`` and write the artifacts.

    Returns:
        Path of the written ``report.md``.

    Raises:
        FileNotFoundError: If the case file, case manifest or every raw file is missing.
    """
    cases = load_cases(run_dir / "cases.jsonl")
    manifest_path = run_dir / "case_manifest.json"
    case_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    llmail_ids = set(case_manifest["llmail_attack_ids"]) | set(case_manifest["benign_ids"])
    rag_ids = set(case_manifest.get("rag_attack_ids") or [])
    ablation_ids = set(case_manifest.get("ablation_attack_ids") or []) | set(
        case_manifest["benign_ids"]
    )
    # The config scheme decides what the names mean (scheme.py): a meta without one is v1, and a
    # folder that mixes the two is refused here, before anything is scored or written.
    scheme = folder_scheme(run_dir) or SCHEME_V1
    v2 = scheme == SCHEME_V2
    configs = [c for c in configs_for(scheme) if (run_dir / "raw" / f"{c}.jsonl").exists()]
    if not configs:
        raise FileNotFoundError(f"no raw/<config>.jsonl under {run_dir}")

    scored: dict[str, list[Any]] = {}
    errors: dict[str, list[RawRecord]] = {}
    records: dict[str, list[RawRecord]] = {}
    run_meta: dict[str, Any] = {}
    for config in configs:
        meta_path = run_dir / "raw" / f"{config}.meta.json"
        if meta_path.exists():
            run_meta[config] = json.loads(meta_path.read_text(encoding="utf-8"))
        records[config] = read_raw(run_dir / "raw" / f"{config}.jsonl")
        # A meta that asks for the strict benign-utility rule is a new run (ADR-0012 2(e)); a v3
        # row is strict by its schema, and a v1 run without the key is scored as before.
        scored[config], errors[config] = score_records(
            records[config],
            cases,
            harness=harness,
            metrics=metrics,
            strict_utility=True if strict_utility_from_meta(run_meta.get(config)) else None,
        )
        with (run_dir / f"{AGENT}__{config}.jsonl").open("w", encoding="utf-8") as handle:
            for result in scored[config]:
                handle.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
    problems = [p for c in configs for p in degradation_problems(c, run_meta.get(c), scheme=scheme)]
    if problems:
        raise ValueError("refusing to score a weakened guard run: " + "; ".join(problems))
    problems = [p for c in configs for p in settings_problems(c, run_meta.get(c))]
    problems += consistency_problems(run_meta, records)
    if problems:
        raise ValueError("refusing to score runs off the pinned settings: " + "; ".join(problems))

    meaning_rows = {
        c: ResultStore(meaning_path(run_dir, c)).latest_records()
        for c in configs
        if meaning_path(run_dir, c).exists()
    }

    # The run's reader, from every file: a config whose attacks were all stopped before drafting
    # never called it, and its rows name none.
    run_readers = sorted(
        {
            str(row["reader_model"])
            for rows in meaning_rows.values()
            for row in rows.values()
            if row.get("reader_model")
        }
    )

    def meaning_of(config: str, ids: set[str]) -> MeaningSummary | None:
        """The config's meaning-based column on the attacks of ``ids``; None when not read."""
        rows = meaning_rows.get(config)
        if rows is None:
            return None
        outcomes = [
            MeaningOutcome(
                current_verdict(rows.get(r.case_id), r),
                None if r.pipeline is None else r.pipeline.reached_drafting,
            )
            for r in records[config]
            if r.ok and r.case_id in ids and cases[r.case_id].get("kind") == "attack"
        ]
        return summarize_meaning(
            outcomes, metrics=metrics, reader_models=run_readers, rubric=RUBRIC_VERSION
        )

    def table(ids: set[str], names: Sequence[str]) -> dict[str, ConfigSummary]:
        return {
            c: summarize_config(
                c,
                _subset(scored[c], ids),
                metrics=metrics,
                n_errors=_errors_in(errors[c], ids),
                meaning=meaning_of(c, ids),
                fail_closed_attacks=_fail_closed_attacks_in(errors[c], ids, cases),
                service_failures=_service_failures_in(errors[c], ids),
            )
            for c in names
        }

    # v1 runs C1/C2 on a subset, so only C0, C0T and C3 share the full table; v2 runs every config
    # on every case, so every config that ran is in it.
    main_configs = [c for c in (configs_for(SCHEME_V2) if v2 else MAIN_CONFIGS) if c in configs]
    llmail = table(llmail_ids, main_configs)
    rag = table(rag_ids, main_configs) if rag_ids else {}
    retrieved_ids = poison_retrieved_ids(records, main_configs, rag_ids)
    rag_retrieved = table(retrieved_ids, main_configs) if retrieved_ids else {}
    all_cases = table(set(cases), main_configs)
    has_ablation = not v2 and any(c in configs for c in ("C1", "C2"))
    ablation = (
        table(ablation_ids, [c for c in configs if c in CONFIG_ORDER]) if has_ablation else {}
    )

    paired: dict[str, dict[str, Any]] = {}
    section: SchemeV2Section | None = None
    if v2:
        # Scheme v2 reads the run as one experiment (scheme_report.py): each of C1..C6 against C0T
        # (what the layer adds on its own), C7 against C0, and the C6 control.
        vectors = {
            VECTOR_LLMAIL: set(case_manifest["llmail_attack_ids"]),
            VECTOR_RAG: rag_ids,
        }
        pairs = build_pairs(scored, vectors, metrics=metrics)
        section = SchemeV2Section(
            configs_run=tuple(main_configs),
            pairs=tuple(pairs),
            control=build_control(scored, vectors, metrics=metrics),
        )
        paired = section.pairs_by_name()
    else:
        # Headline: C0 vs C3. C0T vs C3 is added whenever the C0T run exists.
        for base in BASELINES:
            if base not in configs or "C3" not in configs:
                continue
            paired[f"LLMail-Inject {base} vs C3"] = metrics.paired_comparison(
                _subset(scored[base], llmail_ids), _subset(scored["C3"], llmail_ids)
            )
            if rag_ids:
                paired[f"RAG vector {base} vs C3"] = metrics.paired_comparison(
                    _subset(scored[base], rag_ids), _subset(scored["C3"], rag_ids)
                )
        for config in ("C1", "C2"):
            if config in configs and "C3" in configs:
                paired[f"Ablation {config} vs C3"] = metrics.paired_comparison(
                    _subset(scored[config], ablation_ids), _subset(scored["C3"], ablation_ids)
                )

    triage = {
        c: counts for c in configs if (counts := summarize_triage(scored[c])) is not None
    }  # live rows only
    fallbacks = {
        c: fallback_table
        for c in configs
        # scheme v2: only the AI steps the config runs are listed, and a config with none has
        # nothing to fall back (the report says so)
        if not (v2 and not v2_ai_layer_names(c))
        if (
            fallback_table := summarize_fallbacks(
                records[c], metrics=metrics, only=v2_ai_layer_names(c) if v2 else None
            )
        )
        is not None
    }  # rows of a guard that records its failed AI steps only
    template_wins = {c: template_successes(scored[c]) for c in triage}
    layer = layer_ablation(
        [c for c in ABLATION_CONFIGS if c in configs],
        scored=scored,
        records=records,
        errors=errors,
        llmail=llmail,
        rag=rag,
        table=table,
        metrics=metrics,
        manifest=case_manifest,
    )
    # Overhead is the drafting step's: a live email that triage stopped (early exit, template)
    # takes no drafting time, tokens or guard calls, and its zeros would understate the cost.
    # The prices the run recorded when it started, else the reporting machine's (a v1 run).
    overheads: dict[str, Overhead] = {
        c: overhead(
            c,
            [r for r in records[c] if r.pipeline is None or r.pipeline.reached_drafting],
            recorded if (recorded := prices_from_meta(run_meta.get(c))) is not None else prices,
        )
        for c in configs
    }
    # The no-API analyses are written for v1's C3 (scheme v2 has none yet, see analyses.py).
    headline_extra, extra_sections = (
        ([], [])
        if v2
        else analysis_inputs(
            run_dir,
            scored,
            metrics=metrics,
            llmail_ids=llmail_ids,
            planned_attacks=len(case_manifest["llmail_attack_ids"]),
            planned_benign=len(case_manifest["benign_ids"]),
            errored_ids=frozenset(e.case_id for e in errors.get("C3", [])),
        )
    )
    setup = run_setup(run_meta)  # None for an in-process (v1) run: its report is unchanged
    attack_sets = {
        "llmail": set(case_manifest["llmail_attack_ids"]),
        "rag": rag_ids,
        "ablation": set(case_manifest.get("ablation_attack_ids") or []),
    }
    inputs = ReportInputs(
        run_id=run_dir.name,
        planned_llmail_attacks=len(case_manifest["llmail_attack_ids"]),
        planned_rag_attacks=len(rag_ids),
        planned_ablation_attacks=len(attack_sets["ablation"]),
        attack_errors={
            name: {c: _errors_in(errors[c], ids) for c in configs}
            for name, ids in attack_sets.items()
        },
        llmail=llmail,
        rag=rag,
        all_cases=all_cases,
        ablation=ablation,
        paired=paired,
        overhead=overheads,
        errors={c: [(e.case_id, e.error or "unknown error") for e in errors[c]] for c in configs},
        headline_extra=headline_extra,
        extra_sections=extra_sections,
        rag_retrieved=rag_retrieved,
        triage=triage,
        layer_ablation=layer.summary if layer else None,
        fallbacks=fallbacks,
        template_successes=template_wins,
        scheme=scheme,
        target_config=target_config(scheme),
        scheme_v2=section,
        setup=render_run_setup(setup) if setup is not None else [],
    )
    tables = {
        "llmail": llmail,
        "rag": rag,
        "rag_poison_retrieved": rag_retrieved,
        "all": all_cases,
        "ablation": ablation,
    }
    layer_pairs = layer.summary.paired_by_name() if layer else {}
    if layer:
        tables.update(layer.tables)
    csv_rows = metrics_rows(
        tables, overheads, {**paired, **layer_pairs}, triage, fallbacks=fallbacks
    )
    write_metrics_csv(
        run_dir / "metrics.csv",
        csv_rows
        + (layer_ablation_rows(layer.summary) if layer else [])
        + (scheme_v2_rows(section) if section is not None else []),
    )
    summary = {
        name: {c: summary_entry(s) for c, s in by_config.items()}
        for name, by_config in tables.items()
    }
    payload: dict[str, Any] = {"tables": summary, "paired": {**paired, **layer_pairs}}
    if triage:
        payload["triage"] = {c: asdict(t) for c, t in triage.items()}
        payload["template_successes"] = {c: list(ids) for c, ids in template_wins.items()}
    if fallbacks:
        payload["fallbacks"] = {c: asdict(t) for c, t in fallbacks.items()}
    if section is not None:
        payload = {"scheme": scheme, **payload, "scheme_v2": section.summary()}
    if setup is not None:
        payload["setup"] = setup
    if layer:
        payload["layer_ablation"] = {
            "benign_real_drafts": {c: asdict(r) for c, r in layer.summary.real_drafts().items()},
            "raises_asr_p05": {pair.name: pair.raises_asr for pair in layer.summary.pairs},
            "attribution": _attribution_summary(layer.summary.attribution),
        }
    (run_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    models = {
        c: {
            "generation": meta.get("generation_model"),
            "guard": meta.get("guard_models"),
            "l1_model_sha256": meta.get("l1_model_sha256"),
            "live_layers": meta.get("live_layers"),
            # ADR-0014: the embedding is the runner's choice; a comparison across runs needs it
            "embedding": meta.get("embedding"),
        }
        for c, meta in run_meta.items()
    }
    live_run = any(m.get("transport") == LIVE_TRANSPORT for m in run_meta.values())
    # The commits the runs used (from their metas), not HEAD at report time; the report
    # time checkouts are kept alongside for reference.
    manifest = build_manifest(
        run_id=run_dir.name,
        rag_email={
            **_commit_of(run_meta, "rag_email_commit"),
            "at_report_time": git_head(REPO_ROOT),
        },
        mailguard={
            **_commit_of(run_meta, "mailguard_commit"),
            "layout": guard_layout(mailguard_dir),
            "at_report_time": git_head(mailguard_dir),
        },
        case_manifest_sha256=sha256_file(manifest_path),
        run_meta=run_meta,
        models=models,
        counts={
            c: {"records": len(records[c]), "scored": len(scored[c]), "errors": len(errors[c])}
            for c in configs
        },
        task="7.20" if live_run else "7.19",
        scheme=scheme if v2 else None,
        meaning=(
            {
                "rubric": RUBRIC_VERSION,
                "prompt_sha256": PROMPT_SHA256,
                "reader_models": run_readers,
                "files": {c: sha256_file(meaning_path(run_dir, c)) for c in meaning_rows},
            }
            if meaning_rows
            else None
        ),
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    report_path = run_dir / "report.md"
    report_path.write_text(render_report(inputs), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; prints ``REPORT OK <path>`` or ``FAIL ...``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mailguard-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    mailguard_dir = args.mailguard_dir or resolve_mailguard_dir()
    try:
        path = build_report(
            args.run_dir,
            harness=load_amg_harness(mailguard_dir),
            metrics=load_amg_metrics(mailguard_dir),
            prices=AppSettings().llm.price_table,
            mailguard_dir=mailguard_dir,
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"REPORT OK {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
