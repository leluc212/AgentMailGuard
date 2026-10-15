"""Read-only reader of the committed scheme-v1/v2 run folders (plan_v3 Task 13, ADR-0015 d.6).

The v3 reader (``report.py``, ``scoring.py``) refuses a guard report below schema 3 and a run meta
without ``scheme: v3``. The one exception is this module: it re-reads what a v1/v2 run folder
*committed* so the Review 1 numbers stay inspectable at HEAD, under the old layer names (L1
injection scanner .. L5 policy engine) and with nothing converted to schema 3.

    committed folder                          legacy reader
    summary.json  ─ tables, paired ─────────▶ render_tables(): the "Security and usefulness" and
    metrics.csv   ─ one number per cell ────▶   "ASR by vector" tables, and an ablation folder's
    manifest.json, case_manifest.json,          layer-ablation tables, row for row what the
                                                committed ``report.md`` printed
    analysis/first_layer.csv ─ (when present)

The raw inputs of those runs (``raw/*.jsonl``, ``cases.jsonl``: attack text and drafts) are
gitignored on purpose, so a clean clone cannot score them again; this reader never scores. It
raises on any attempt to run, score or ablate (``LegacyReadOnlyError``), refuses a v3 folder
(``LegacyRunError``) and writes nothing into the folder it reads (the committed folders are
historical data and are never rewritten).

``recount`` is the v1-only L2 schema-fallback recount (Amendment 1, E.2) that used to be its
own module: it reads a v1 run's stored rows, which need the raw folder an owner still has
locally. ``LegacyRawRecord`` parses them under the old names, since ``scoring.RawRecord`` refuses
a pre-v3 report.

(docs/adr/0015-guard-architecture-v3.md decision 6; plan_v3/exec-plan-chi-tiet.md Task 13;
R22.12)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.artifacts import (
    ConfigSummary,
    RateCI,
    _cell,
    _grouped,
    _side_by_side,
)
from evaluation.mailguard_bench.scheme import (
    SCHEME_V1,
    SCHEME_V2,
    SCHEME_V3,
    SchemeMixError,
    folder_scheme,
)
from evaluation.mailguard_bench.scoring import flatten_runner_row

# The names of the retired guard (v1/v2), kept here and nowhere else (ADR-0015 allowlist).
L2_LAYER = "l2_intent_extractor"
SCHEMA_FALLBACK_REASONS = frozenset({"non_json", "schema_missing"})


class LegacyReaderError(ValueError):
    """Base of what the legacy reader refuses."""


class LegacyRunError(LegacyReaderError):
    """The folder is not a committed v1/v2 run (a v3 folder, or no committed outputs)."""


class LegacyReadOnlyError(LegacyReaderError):
    """Something tried to run, score or ablate through the legacy reader, which only reads."""


# --- the committed folder -------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rate(data: Mapping[str, Any] | None) -> RateCI | None:
    if data is None:
        return None
    return RateCI(int(data["successes"]), int(data["total"]), float(data["lo"]), float(data["hi"]))


def _summary_of(entry: Mapping[str, Any]) -> ConfigSummary:
    """A ``ConfigSummary`` back from its ``summary.json`` entry (the keys a v1/v2 run wrote)."""
    asr, der = _rate(entry["asr"]), _rate(entry["der"])
    if asr is None or der is None:
        raise LegacyRunError(f"summary entry of {entry.get('config')!r} has no asr/der")
    return ConfigSummary(
        config=str(entry["config"]),
        asr=asr,
        der=der,
        fpr=_rate(entry.get("fpr")),
        utility=_rate(entry.get("utility")),
        by_scenario={k: r for k, v in (entry.get("by_scenario") or {}).items() if (r := _rate(v))},
        by_vector={k: r for k, v in (entry.get("by_vector") or {}).items() if (r := _rate(v))},
        poison_retrieved=_rate(entry.get("poison_retrieved")),
        n_errors=int(entry.get("n_errors") or 0),
        guard_asr=_rate(entry.get("guard_asr")),
        guard_fpr=_rate(entry.get("guard_fpr")),
        guard_fpr_blocked=_rate(entry.get("guard_fpr_blocked")),
        guard_review=entry.get("guard_review"),
        utility_legacy=_rate(entry.get("utility_legacy")),
    )


@dataclass(frozen=True)
class LegacyRun:
    """A committed v1/v2 run folder, read from its outputs only.

    ``tables`` holds one ``{config: ConfigSummary}`` mapping per ``summary.json`` table
    (``llmail``, ``rag``, ``rag_poison_retrieved``, ``all``, ``ablation``); ``paired`` the paired
    tests as ``summary.json`` wrote them; ``metrics`` the rows of ``metrics.csv``.
    """

    run_dir: Path
    scheme: str
    summary: Mapping[str, Any]
    manifest: Mapping[str, Any]
    case_manifest: Mapping[str, Any]
    tables: Mapping[str, Mapping[str, ConfigSummary]]
    paired: Mapping[str, Mapping[str, Any]]
    metrics: Sequence[Mapping[str, str]]
    first_layer: Sequence[Mapping[str, str]] | None = None

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    def run(self, *_args: object, **_kwargs: object) -> None:
        """Refuse: the legacy reader never runs a benchmark."""
        raise LegacyReadOnlyError("the legacy reader is read-only: it never runs a benchmark")

    def score(self, *_args: object, **_kwargs: object) -> None:
        """Refuse: the raw inputs are not committed and old runs are never scored again."""
        raise LegacyReadOnlyError(
            "the legacy reader is read-only: it never scores (the raw inputs are not committed)"
        )

    def ablate(self, *_args: object, **_kwargs: object) -> None:
        """Refuse: the legacy reader never ablates a layer."""
        raise LegacyReadOnlyError("the legacy reader is read-only: it never ablates a layer")


def _marks_v3(run_dir: Path, summary: Mapping[str, Any], manifest: Mapping[str, Any]) -> bool:
    if summary.get("scheme") == SCHEME_V3 or "scheme_v3" in summary:
        return True
    if manifest.get("scheme") == SCHEME_V3:
        return True
    try:
        return folder_scheme(run_dir) == SCHEME_V3
    except SchemeMixError:
        return False


def load_legacy_run(run_dir: Path) -> LegacyRun:
    """Read the committed outputs of a v1/v2 run folder.

    A folder that records no scheme (the committed ones: their metas are not committed) is v1,
    as ``scheme.scheme_of_meta`` reads a meta without one.

    Raises:
        LegacyRunError: If the folder is a v3 run (its reader is ``report.py``) or holds no
            committed ``summary.json``.
    """
    run_dir = Path(run_dir)
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise LegacyRunError(f"{run_dir} has no committed summary.json: nothing to read")
    summary = _read_json(summary_path)
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    if _marks_v3(run_dir, summary, manifest):
        raise LegacyRunError(
            f"{run_dir} is a scheme v3 run: the legacy reader refuses it (it reads v1/v2 folders "
            "only); use `make mailguard-report RUN=<run>` without LEGACY"
        )
    scheme = str(summary.get("scheme") or manifest.get("scheme") or SCHEME_V1)
    if scheme not in (SCHEME_V1, SCHEME_V2):
        raise LegacyRunError(f"{run_dir} records scheme {scheme!r}, which is not v1 or v2")
    case_manifest_path = run_dir / "case_manifest.json"
    metrics_path = run_dir / "metrics.csv"
    first_layer_path = run_dir / "analysis" / "first_layer.csv"
    return LegacyRun(
        run_dir=run_dir,
        scheme=scheme,
        summary=summary,
        manifest=manifest,
        case_manifest=_read_json(case_manifest_path) if case_manifest_path.exists() else {},
        tables={
            name: {config: _summary_of(entry) for config, entry in by_config.items()}
            for name, by_config in (summary.get("tables") or {}).items()
        },
        paired=dict(summary.get("paired") or {}),
        metrics=_read_csv(metrics_path) if metrics_path.exists() else [],
        first_layer=_read_csv(first_layer_path) if first_layer_path.exists() else None,
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --- the tables -----------------------------------------------------------------------------


LAYER_PAIR_PREFIX = "Layer ablation "
_LAYER_PAIR = re.compile(r"^Layer ablation (?P<config>\S+) vs C3 \((?P<vector>.+)\)$")
_LAYER_VECTORS = (("llmail", "LLMail-Inject"), ("rag", "RAG vector"))
_LAYER_REMOVED = {"C0": "all", "C3": "none"}


def _counts(counts: Mapping[str, int]) -> str:
    return "; ".join(f"{label}: {n}" for label, n in counts.items()) or "none"


def _layer_ablation_lines(run: LegacyRun) -> list[str]:
    """The "Layer ablation: remove one layer" tables of an old (v1/v2) ablation folder.

    Built from ``summary.json`` (``layer_ablation_llmail``, ``layer_ablation_rag``, the
    ``layer_ablation`` block and the paired tests) with the old config names (``C3-L1`` ..
    ``C3-L5``, v2 meanings) kept verbatim. The row layout is the one the committed report used;
    the prose around the tables is not reproduced.
    """
    llmail_rows = run.tables.get("layer_ablation_llmail")
    rag_rows = run.tables.get("layer_ablation_rag")
    if not llmail_rows or not rag_rows:
        return []
    block = run.summary.get("layer_ablation") or {}
    drafts = {k: _rate(v) for k, v in (block.get("benign_real_drafts") or {}).items()}
    llmail = {c: s for c, s in run.tables.get("llmail", {}).items() if c in ("C0", "C3")}
    rag = {c: s for c, s in run.tables.get("rag", {}).items() if c in ("C0", "C3")}
    llmail.update(llmail_rows)
    rag.update(rag_rows)
    lines = [
        "## Layer ablation: remove one layer",
        "",
        "Each `C3-L<n>` config is the old guard's preset with that layer removed (names and "
        "meanings of the run, kept verbatim), on the same cases as C3.",
        "",
        "| Config | Layer removed | LLMail ASR | RAG ASR | Benign FPR | "
        "Benign drafts (not blocked, ≥ 40 characters) |",
        "|---|---|---|---|---|---|",
    ]
    for config, summary in llmail.items():
        label = {"C0": "C0 (no guard)", "C3": "C3 (full guard)"}.get(config, config)
        removed = _LAYER_REMOVED.get(config, config.removeprefix("C3-"))
        rag_asr = rag[config].asr.fmt() if config in rag else "n/a"
        real = drafts.get(config)
        lines.append(
            f"| {label} | {removed} | {summary.asr.fmt()} | {rag_asr} | {_cell(summary.fpr)} | "
            f"{_cell(real)} |"
        )
    lines.append("")
    pairs = {n: c for n, c in run.paired.items() if n.startswith(LAYER_PAIR_PREFIX)}
    if pairs:
        raises = block.get("raises_asr_p05") or {}
        lines += [
            "### Paired test against the same-run C3 (McNemar exact, same case ids)",
            "",
            "| Config | Vector | pairs | ASR config | ASR C3 | only config succeeded | "
            "only C3 succeeded | p | raises ASR, p < 0.05 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name, cmp in pairs.items():
            match = _LAYER_PAIR.match(name)
            if match is None:
                raise LegacyRunError(f"unreadable layer-ablation pair name {name!r}")
            lines.append(
                f"| {match['config']} | {match['vector']} | {cmp['n']} | "
                f"{100 * cmp['a_rate']:.1f} % | {100 * cmp['b_rate']:.1f} % | "
                f"{cmp['discordant_a_only']} | {cmp['discordant_b_only']} | "
                f"{cmp['p_value']:.3g} | {'yes' if raises.get(name) else 'no'} |"
            )
        lines.append("")
    attribution = block.get("attribution") or {}
    if attribution:
        lines += [
            "### Which layer stopped each attack",
            "",
            "| Config | Vector | attacks | succeeded | first catching layer | flagged by |",
            "|---|---|---|---|---|---|",
        ]
        for config, by_vector in attribution.items():
            for vector, att in by_vector.items():
                lines.append(
                    f"| {config} | {vector} | {att['attacks']} | {att['succeeded']} | "
                    f"{_counts(att['first_catching'])} | {_counts(att['flagged'])} |"
                )
        lines.append("")
    return lines


def _paired_rows(paired: Mapping[str, Mapping[str, Any]]) -> list[str]:
    rows = [
        "### Paired test (McNemar exact, same cases)",
        "",
        "| Comparison | pairs | ASR A | ASR B | only A succeeded | only B succeeded | p |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, cmp in paired.items():
        if name.startswith(LAYER_PAIR_PREFIX):
            continue  # the layer-ablation pairs have their own table
        rows.append(
            f"| {name} | {cmp['n']} | {100 * cmp['a_rate']:.1f} % | "
            f"{100 * cmp['b_rate']:.1f} % | {cmp['discordant_a_only']} | "
            f"{cmp['discordant_b_only']} | {cmp['p_value']:.3g} |"
        )
    return [*rows, ""]


def render_tables(run: LegacyRun) -> str:
    """The run's headline tables as the committed ``report.md`` printed them, nothing else.

    Every table row is built by the same renderers the report used and from the numbers
    ``summary.json`` stored, so each appears verbatim in the committed ``report.md``. The old
    layer names are not touched: no layer is named in these tables.
    """
    lines = [
        f"# Legacy scheme {run.scheme} tables, run `{run.run_id}`",
        "",
        f"Regenerated read-only from the committed outputs of `{run.run_dir}` (summary.json): "
        "nothing was run, scored or rewritten. The old layer names and the old guard "
        f"(`mailguard_commit` {run.manifest.get('git', {}).get('agentmailguard', {}).get('sha')}) "
        "are kept as they were.",
        "",
    ]
    tables = run.tables
    if tables.get("llmail"):
        lines += ["## LLMail-Inject (email vector; the 95 % target is stated here)", ""]
        lines += _side_by_side("Security and usefulness", tables["llmail"])
        lines += _grouped("ASR by LLMail scenario", tables["llmail"], "by_scenario")
    if tables.get("rag"):
        lines += ["## RAG vector (poisoned knowledge documents)", ""]
        lines += _side_by_side("Security", tables["rag"])
        if tables.get("rag_poison_retrieved"):
            lines += _side_by_side(
                "Security, only cases whose poisoned document was retrieved",
                tables["rag_poison_retrieved"],
            )
    if tables.get("all"):
        lines += _grouped("ASR by vector (email vs rag)", tables["all"], "by_vector")
    if run.paired:
        lines += _paired_rows(run.paired)
    if tables.get("ablation"):
        lines += ["## Reduced ablation (fixed 100-attack subset + the same benign emails)", ""]
        lines += _side_by_side("Layers add up", tables["ablation"])
    lines += _layer_ablation_lines(run)
    return "\n".join(lines).rstrip() + "\n"


# --- the v1 L2 recount --------------------------------------------------

# The keys of the v1 guard's ``heuristic_entities`` (extractor.py): all a prose answer leaves.
HEURISTIC_ENTITY_KEYS = frozenset({"order_ids", "emails", "amounts", "dates"})
DEFAULT_CONFIDENCE = 0.5  # ExtractorOutput.confidence when the answer carried none
LLM_FINDING_RULE = "llm:assistant_instructions"

ANSWERED = "answered"
POSSIBLE_FALLBACK = "possible_fallback"
CANDIDATE_PARTIAL = "candidate_partial_answer"
LOUD_FAILURE = "loud_failure"
AI_STEP_NOT_RUN = "ai_step_not_run"
NO_L2_VERDICT = "no_l2_verdict"


@dataclass(frozen=True)
class LegacyRawRecord:
    """One row of a v1 run's ``raw/<config>.jsonl``, as far as the L2 recount needs it."""

    case_id: str
    status: str
    report: Mapping[str, Any] | None
    guard_fallbacks: tuple[Mapping[str, Any], ...] | None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def l2_schema_fallback(self) -> bool:
        """True when a recorded fallback is L2's model answer without the schema."""
        return any(
            f.get("layer") == L2_LAYER and f.get("reason") in SCHEMA_FALLBACK_REASONS
            for f in self.guard_fallbacks or ()
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LegacyRawRecord:
        flat = flatten_runner_row(data)
        report = flat.get("report")
        fallbacks = flat.get("guard_fallbacks")
        return cls(
            case_id=str(flat["case_id"]),
            status=str(flat.get("status") or "error"),
            report=report if isinstance(report, Mapping) else None,
            guard_fallbacks=(
                tuple(f for f in fallbacks if isinstance(f, Mapping))
                if isinstance(fallbacks, Sequence) and not isinstance(fallbacks, str)
                else None
            ),
        )


def read_legacy_raw(path: Path) -> list[LegacyRawRecord]:
    """Read a v1 raw results file; the last line per case wins (as ``scoring.read_raw`` does)."""
    latest: dict[str, LegacyRawRecord] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = LegacyRawRecord.from_dict(json.loads(line))
                latest[record.case_id] = record
    return list(latest.values())


def classify_l2(verdict: Mapping[str, Any] | None) -> str:
    """What one stored L2 verdict of the v1 guard can prove about its AI step.

    Returns:
        ``no_l2_verdict`` (nothing stored), ``loud_failure`` (the guard recorded ``llm_error``),
        ``ai_step_not_run`` (``llm_used`` is not true), ``answered`` (the verdict carries entity
        keys of the model's own, so its answer had the ``entities`` field),
        ``possible_fallback`` (only what a prose answer leaves) or ``candidate_partial_answer``
        (heuristic entity keys only, next to data the model chose).
    """
    if verdict is None:
        return NO_L2_VERDICT
    metadata = verdict.get("metadata") or {}
    if metadata.get("llm_error"):
        return LOUD_FAILURE
    if metadata.get("llm_used") is not True:
        return AI_STEP_NOT_RUN
    entities = verdict.get("entities") or {}
    findings = verdict.get("findings") or []
    if not set(entities) <= HEURISTIC_ENTITY_KEYS:
        return ANSWERED
    model_data = (
        verdict.get("confidence") != DEFAULT_CONFIDENCE
        or bool(metadata.get("instructions_to_assistant"))
        or any(isinstance(f, Mapping) and f.get("rule_id") == LLM_FINDING_RULE for f in findings)
    )
    return CANDIDATE_PARTIAL if model_data else POSSIBLE_FALLBACK


@dataclass(frozen=True)
class L2Recount:
    """L2's AI step over one config's rows, as far as the stored rows show it.

    The row categories are exclusive: a row that records its fallbacks is ``recorded_rows`` and
    counted exactly (``recorded_schema_fallback``); every other row is classified by its L2
    verdict. ``scored_rows`` are the ``ok`` rows among ``rows``.
    """

    rows: int
    scored_rows: int
    answered: int
    possible_fallback: int
    candidate_partial_answer: int
    loud_failure: int
    ai_step_not_run: int
    no_l2_verdict: int
    recorded_schema_fallback: int
    recorded_rows: int


def recount(records: Sequence[LegacyRawRecord]) -> L2Recount:
    """Count the rows by what their L2 verdicts show, an exact recording taking precedence."""
    counts = {
        ANSWERED: 0,
        POSSIBLE_FALLBACK: 0,
        CANDIDATE_PARTIAL: 0,
        LOUD_FAILURE: 0,
        AI_STEP_NOT_RUN: 0,
        NO_L2_VERDICT: 0,
    }
    recorded = recorded_schema = 0
    for record in records:
        if record.guard_fallbacks is not None:
            recorded += 1
            recorded_schema += int(record.l2_schema_fallback)
            continue
        verdict = (record.report or {}).get("l2")
        counts[classify_l2(verdict if isinstance(verdict, Mapping) else None)] += 1
    return L2Recount(
        rows=len(records),
        scored_rows=sum(1 for r in records if r.ok),
        answered=counts[ANSWERED],
        possible_fallback=counts[POSSIBLE_FALLBACK],
        candidate_partial_answer=counts[CANDIDATE_PARTIAL],
        loud_failure=counts[LOUD_FAILURE],
        ai_step_not_run=counts[AI_STEP_NOT_RUN],
        no_l2_verdict=counts[NO_L2_VERDICT],
        recorded_schema_fallback=recorded_schema,
        recorded_rows=recorded,
    )


def render_recount(config: str, counted: L2Recount) -> str:
    """The recount as plain text, with what the stored rows do not allow said out loud."""
    judged = counted.answered + counted.possible_fallback + counted.candidate_partial_answer
    candidates = counted.possible_fallback + counted.candidate_partial_answer
    lines = [
        f"{config}: {counted.rows} rows ({counted.scored_rows} scored).",
        f"L2's AI step ran without a recorded failure in {judged} row(s); "
        f"{counted.possible_fallback} of them look like an L2 answer without the schema (only the "
        "heuristic entity keys order_ids, emails, amounts and dates, the default confidence 0.5 "
        f"and no instructions); {counted.candidate_partial_answer} more carry the heuristic "
        "entity keys next to a confidence, a finding or instructions the model chose (a partial "
        "answer without the entities field looks like this; so does a complete answer with an "
        f"empty entities object); the other {counted.answered} carry entity keys of the model's "
        "own.",
        f"{candidates} row(s) are candidates for an L2 answer without the entities field or "
        "without the whole schema. This is not an upper bound on 'the answer did not carry the "
        "schema': a partial answer that carries entities but lacks another required field "
        "(user_intent, requested_actions, contains_assistant_instructions, "
        "instructions_to_assistant, confidence) leaves no trace in a v1 row, so a low or zero "
        "number is not proof that no schema fallback happened.",
    ]
    if counted.recorded_rows:
        lines.append(
            f"{counted.recorded_rows} row(s) record their fallbacks (a guard that marks them): "
            f"counted exactly, {counted.recorded_schema_fallback} with an L2 schema fallback."
        )
    if counted.loud_failure:
        lines.append(
            f"{counted.loud_failure} row(s) recorded an llm_error: a loud failure, which the v1 "
            "runner already turned into an error row. It is not a silent fallback."
        )
    if counted.ai_step_not_run:
        lines.append(
            f"{counted.ai_step_not_run} row(s) ran L2 without its AI step: nothing to count."
        )
    if counted.no_l2_verdict:
        lines.append(
            f"{counted.no_l2_verdict} row(s) have no L2 verdict in their stored report (stopped "
            "before L2, or an error row without one): the data does not allow a count."
        )
    lines.append(
        f"audit__{config}.jsonl (the guard's L5 policy log) holds only each layer's severity, "
        "score and decider, not L2's answer, so it cannot tell either way."
    )
    return "\n".join(lines)


def recount_run(run_dir: Path, config: str = "C3") -> str:
    """The L2 recount of a v1 run folder's raw rows (read-only).

    Raises:
        LegacyRunError: If the folder is not scheme v1 (v2 records its fallbacks, and the report
            counts them exactly; v3 is read by ``report.py``).
        FileNotFoundError: If ``raw/<config>.jsonl`` does not exist.
    """
    scheme = folder_scheme(run_dir)
    if scheme not in (None, SCHEME_V1):
        raise LegacyRunError(
            f"{run_dir} is a scheme {scheme} run: this recount is for the v1 guard, which cannot "
            "mark a failed L2 answer. Newer guards record their fallbacks and "
            "`make mailguard-report` counts them exactly"
        )
    return render_recount(config, recount(read_legacy_raw(run_dir / "raw" / f"{config}.jsonl")))


# --- command line ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """``tables --run-dir D`` prints a committed folder's tables; ``recount`` the v1 L2 recount."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    tables = sub.add_parser("tables", help="print the tables of a committed v1/v2 run folder")
    tables.add_argument("--run-dir", type=Path, required=True)
    recount_cmd = sub.add_parser("recount", help="the v1 L2 schema-fallback recount")
    recount_cmd.add_argument("--run-dir", type=Path, required=True)
    recount_cmd.add_argument("--config", default="C3")
    args = parser.parse_args(argv)
    try:
        if args.command == "tables":
            print(render_tables(load_legacy_run(args.run_dir)), end="")
        else:
            print(recount_run(args.run_dir, args.config))
    except (LegacyReaderError, SchemeMixError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"FAIL {exc.filename or exc} does not exist", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
