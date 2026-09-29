"""Score a benchmark run and write manifest.json, metrics.csv and report.md.

Makes no model calls. Run from the rag-email root with the AgentMailGuard worktree on
the path (its scorer and metrics are used unchanged)::

    make mailguard-report RUN=<run_id>

Reads ``<run_dir>/cases.jsonl``, ``case_manifest.json`` and ``raw/<config>.jsonl`` (+
``raw/<config>.meta.json``); writes ``ragemail__<config>.jsonl`` (AgentMailGuard
``CaseResult`` rows, the file pattern its ``evaluation/report.py`` loads),
``summary.json``, ``manifest.json``, ``metrics.csv`` and ``report.md``.

(docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md §4, §4b; specs/tasks.md
7.6, 7.19; R22.12, R24.5)
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
    ReportInputs,
    build_manifest,
    git_head,
    metrics_rows,
    render_report,
    sha256_file,
    summarize_config,
    write_metrics_csv,
)
from evaluation.mailguard_bench.guard_env import DEFAULT_GUARD_MODEL
from evaluation.mailguard_bench.overhead import Overhead, overhead
from evaluation.mailguard_bench.scoring import RawRecord, read_raw, score_records
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
# preset, guard, live_layers, guard_models and degraded_allowed differ by design.
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
)


def degradation_problems(config: str, meta: Mapping[str, Any] | None) -> list[str]:
    """Why a guarded run would describe a weaker guard than its preset (empty when none).

    C0 is rag-email's native path, runs no AgentMailGuard code and is not checked here
    (settings_problems still requires its meta). C0T is a guarded config (the guard's
    template, preset "C0") and is checked like C1/C2. A C3 run must have its run meta, all six
    layers active and every LLM stage it needs live; C1/C2 must have no missing stage.
    """
    if config == "C0":
        return []
    if meta is None:
        return [f"{config}: raw/{config}.meta.json is missing"] if config == "C3" else []
    guard = meta.get("guard") or {}
    problems: list[str] = []
    missing = list(guard.get("missing_live_stages") or [])
    if missing:
        problems.append(f"{config}: guard stages not live: {', '.join(missing)}")
    if meta.get("degraded_allowed"):
        problems.append(f"{config}: started with --allow-degraded")
    active = tuple(guard.get("active_layers") or ())
    if config == "C3" and active != FULL_LAYERS:
        problems.append(f"C3: active layers {list(active)} are not {list(FULL_LAYERS)}")
    return problems


def settings_problems(config: str, meta: Mapping[str, Any] | None) -> list[str]:
    """Why a run did not use the live pinned model (empty when it did).

    Every scored config needs its run meta. The generation call must not be the fake
    provider and must use the pinned model; a guarded config's guard stages must too
    (the guard registry's own "fake" backend counts as a live stage, so the live-stage
    check alone does not catch it). C0 has no guard stage, so its guard model is ignored.
    """
    if meta is None:
        return [f"{config}: raw/{config}.meta.json is missing"]
    problems: list[str] = []
    generation = meta.get("generation") or {}
    if str(generation.get("provider") or "").strip().lower() == "fake":
        problems.append(f"{config}: generation provider is 'fake'")
    if meta.get("generation_model") != DEFAULT_GUARD_MODEL:
        problems.append(
            f"{config}: generation model {meta.get('generation_model')!r} is not the pinned "
            f"{DEFAULT_GUARD_MODEL!r}"
        )
    if config != "C0" and meta.get("guard_models") != DEFAULT_GUARD_MODEL:
        problems.append(
            f"{config}: guard model {meta.get('guard_models')!r} is not the pinned "
            f"{DEFAULT_GUARD_MODEL!r}"
        )
    return problems


def consistency_problems(
    run_meta: Mapping[str, Mapping[str, Any]], records: Mapping[str, Sequence[RawRecord]]
) -> list[str]:
    """Configs that ran under different settings, or rows whose model is not the meta's."""
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
        seen = sorted({r.generation.model for r in rows if r.ok and r.generation.model})
        wrong = [model for model in seen if model != want]
        if wrong:
            problems.append(f"{config}: rows were generated by {wrong}, meta says {want!r}")
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


def analysis_inputs(
    run_dir: Path, scored: Mapping[str, list[Any]], *, metrics: ModuleType, llmail_ids: set[str]
) -> tuple[list[str], list[str]]:
    """Extra headline lines and report sections from the no-API analyses (task 6)."""
    return [], []


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

    overheads: dict[str, Overhead] = {c: overhead(c, records[c], prices) for c in configs}
    headline_extra, extra_sections = analysis_inputs(
        run_dir, scored, metrics=metrics, llmail_ids=llmail_ids
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
    )
    tables = {
        "llmail": llmail,
        "rag": rag,
        "rag_poison_retrieved": rag_retrieved,
        "all": all_cases,
        "ablation": ablation,
    }
    write_metrics_csv(run_dir / "metrics.csv", metrics_rows(tables, overheads, paired))
    summary = {
        name: {c: asdict(s) for c, s in by_config.items()} for name, by_config in tables.items()
    }
    (run_dir / "summary.json").write_text(
        json.dumps({"tables": summary, "paired": paired}, indent=2, default=str) + "\n",
        encoding="utf-8",
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
