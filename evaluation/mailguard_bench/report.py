"""Score a benchmark run and write manifest.json, metrics.csv and report.md.

Makes no model calls. Run from the rag-email root with the AgentMailGuard worktree on
the path (its scorer and metrics are used unchanged)::

    make mailguard-report RUN=<run_id>

Reads ``<run_dir>/cases.jsonl``, ``case_manifest.json`` and ``raw/<config>.jsonl`` (+
``raw/<config>.meta.json``); writes ``ragemail__<config>.jsonl`` (AgentMailGuard
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
from collections.abc import Mapping, Sequence
from dataclasses import asdict
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
    RateCI,
    ReportInputs,
    build_manifest,
    git_head,
    metrics_rows,
    render_report,
    sha256_file,
    summarize_config,
    summarize_triage,
    write_metrics_csv,
)
from evaluation.mailguard_bench.model_profiles import BENCH_MODELS
from evaluation.mailguard_bench.overhead import Overhead, overhead
from evaluation.mailguard_bench.scoring import LIVE_TRANSPORT, RawRecord, read_raw, score_records
from packages.core.settings import AppSettings, ModelPricing

REPO_ROOT = Path(__file__).resolve().parents[2]
# C0 = rag-email native (no AgentMailGuard code); C0T/C1/C2/C3 = AgentMailGuard presets.
# C0 and C0T are required baselines (B1 = a, b); a missing one is stated in the report, never
# refused. C1 and C2 are optional ablation extras.
CONFIG_ORDER = ("C0", "C0T", "C1", "C2", "C3")
MAIN_CONFIGS = ("C0", "C0T", "C3")  # full case set; C1/C2 run only the ablation subset
BASELINES = ("C0", "C0T")  # each is paired against C3 with McNemar when both ran
AGENT = "ragemail"
FULL_LAYERS = ("l1", "l2", "l3", "l3b", "l4", "l5")
# Settings every compared config must share (spec Q6: "same model, same settings").
# preset, guard, live_layers, guard_models and degraded_allowed differ by design. transport,
# reranker and triage are the live pipeline's (task 7.20); a v1 meta has none of them, so they
# are equal (None) across the configs of a v1 run.
SHARED_SETTINGS = (
    "cases_sha256",
    "rag_email_commit",
    "mailguard_commit",
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


def degradation_problems(config: str, meta: Mapping[str, Any] | None) -> list[str]:
    """Why a guarded run would describe a weaker guard than its preset (empty when none).

    C0 is rag-email's native path: it must have run no guard preset and no layer
    (settings_problems requires its meta). C0T is a guarded config (the guard's template,
    preset "C0", no layer active) and is checked like C1/C2. A C3 run must have its run meta,
    all six layers active and every LLM stage it needs live; C1/C2 must have no missing stage.
    """
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
        values = {
            c: json.dumps(m.get(key), sort_keys=True, default=str) for c, m in run_meta.items()
        }
        if len(set(values.values())) > 1:
            problems.append(
                f"configs ran with different {key}: "
                + ", ".join(f"{c}={run_meta[c].get(key)!r}" for c in values)
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
        live_rows = any(r.pipeline is not None for r in rows)
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
    the guard ASR and the guard FPR, so the restated lines count only the cases that reached it.
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
            rate = RateCI.of(metrics.Proportion(sum(r.blocked for r in kept), len(kept)))
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
    configs = [c for c in CONFIG_ORDER if (run_dir / "raw" / f"{c}.jsonl").exists()]
    if not configs:
        raise FileNotFoundError(f"no raw/<config>.jsonl under {run_dir}")

    scored: dict[str, list[Any]] = {}
    errors: dict[str, list[RawRecord]] = {}
    records: dict[str, list[RawRecord]] = {}
    run_meta: dict[str, Any] = {}
    for config in configs:
        records[config] = read_raw(run_dir / "raw" / f"{config}.jsonl")
        scored[config], errors[config] = score_records(
            records[config], cases, harness=harness, metrics=metrics
        )
        with (run_dir / f"{AGENT}__{config}.jsonl").open("w", encoding="utf-8") as handle:
            for result in scored[config]:
                handle.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        meta_path = run_dir / "raw" / f"{config}.meta.json"
        if meta_path.exists():
            run_meta[config] = json.loads(meta_path.read_text(encoding="utf-8"))
    problems = [p for c in configs for p in degradation_problems(c, run_meta.get(c))]
    if problems:
        raise ValueError("refusing to score a weakened guard run: " + "; ".join(problems))
    problems = [p for c in configs for p in settings_problems(c, run_meta.get(c))]
    problems += consistency_problems(run_meta, records)
    if problems:
        raise ValueError("refusing to score runs off the pinned settings: " + "; ".join(problems))

    def table(ids: set[str], names: Sequence[str]) -> dict[str, ConfigSummary]:
        return {
            c: summarize_config(
                c,
                _subset(scored[c], ids),
                metrics=metrics,
                n_errors=_errors_in(errors[c], ids),
            )
            for c in names
        }

    main_configs = [c for c in MAIN_CONFIGS if c in configs]
    llmail = table(llmail_ids, main_configs)
    rag = table(rag_ids, main_configs) if rag_ids else {}
    retrieved_ids = poison_retrieved_ids(records, main_configs, rag_ids)
    rag_retrieved = table(retrieved_ids, main_configs) if retrieved_ids else {}
    all_cases = table(set(cases), main_configs)
    has_ablation = any(c in configs for c in ("C1", "C2"))
    ablation = table(ablation_ids, configs) if has_ablation else {}

    paired: dict[str, dict[str, Any]] = {}
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
    overheads: dict[str, Overhead] = {c: overhead(c, records[c], prices) for c in configs}
    headline_extra, extra_sections = analysis_inputs(
        run_dir,
        scored,
        metrics=metrics,
        llmail_ids=llmail_ids,
        planned_attacks=len(case_manifest["llmail_attack_ids"]),
        planned_benign=len(case_manifest["benign_ids"]),
        errored_ids=frozenset(e.case_id for e in errors.get("C3", [])),
    )
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
    )
    tables = {
        "llmail": llmail,
        "rag": rag,
        "rag_poison_retrieved": rag_retrieved,
        "all": all_cases,
        "ablation": ablation,
    }
    write_metrics_csv(run_dir / "metrics.csv", metrics_rows(tables, overheads, paired, triage))
    summary = {
        name: {c: asdict(s) for c, s in by_config.items()} for name, by_config in tables.items()
    }
    payload: dict[str, Any] = {"tables": summary, "paired": paired}
    if triage:
        payload["triage"] = {c: asdict(t) for c, t in triage.items()}
    (run_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    models = {
        c: {
            "generation": meta.get("generation_model"),
            "guard": meta.get("guard_models"),
            "l1_model_sha256": meta.get("l1_model_sha256"),
            "live_layers": meta.get("live_layers"),
        }
        for c, meta in run_meta.items()
    }
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
            "at_report_time": git_head(mailguard_dir),
        },
        case_manifest_sha256=sha256_file(manifest_path),
        run_meta=run_meta,
        models=models,
        counts={
            c: {"records": len(records[c]), "scored": len(scored[c]), "errors": len(errors[c])}
            for c in configs
        },
        task="7.20"
        if any(m.get("transport") == LIVE_TRANSPORT for m in run_meta.values())
        else "7.19",
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
