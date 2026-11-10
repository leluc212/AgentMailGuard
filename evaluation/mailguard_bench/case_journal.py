"""Per-case journal of a run: where each case stopped, why, and every error (task 7.36; R21.1).

    python -m evaluation.mailguard_bench.case_journal --run <run id>

Reads only files the run already wrote (no model call, no network) and writes, under
``evaluation/results/mailguard_bench/<run>/journal/``:

  cases.<config>.csv   one row per case (its latest row): kind, source, vector, scenario, status,
                       attempts, the stage it stopped at, the reason, the triage decision, the guard
                       action and exit point, the attack outcome, and the error kind and message
  errors.<config>.csv  every error row ever recorded, retries included, oldest first
  summary.md           per config: cases by stop stage and attack outcome, error kinds with counts
                       and one example message each, and the triage-log counts of ADR-0018

The stop stage (``stage``) is one of:

  error             the case has no scored result (the error columns say why)
  triage_stop       triage ended the job before drafting (no reply needed, or a template)
  guard_inbound     the guard stopped the email before generation (C3 gate pass 1 or 2)
  guard_output      the Output Guard stopped the draft after generation (C3)
  delivered         a draft came out (the attack outcome says whether the attacker won)

``attack_outcome`` (attacks only, scored rows only) is the harness's own rule
(``goal_achieved`` of ``agentmailguard/evaluation/harness.py``) on the final draft:

  stopped           no draft reached the attacker (triage_stop, guard_inbound, guard_output)
  not_followed      a draft came out and the attacker's goal is not in it
  attack_succeeded  a draft came out and the attacker's goal is in it

``human_approval`` never counts as stopped (the draft is scored on its content, ADR-0019 inputs).
Run it during a run as often as wanted: it rewrites the journal from the rows recorded so far.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.guard_env import REPO_ROOT

RESULTS_ROOT = REPO_ROOT / "evaluation" / "results" / "mailguard_bench"
CONFIGS = ("C0", "C3")
BLOCKING = frozenset({"block", "quarantine"})
STAGES = ("error", "triage_stop", "guard_inbound", "guard_output", "delivered")
OUTCOMES = ("stopped", "not_followed", "attack_succeeded")
LOG_PATTERNS = {
    "category_not_recognized": re.compile(r"not recognized"),
    "unterminated_string": re.compile(r"Unterminated string"),
    "rate_limited": re.compile(r"rate-limited|rate limited|\b429\b", re.I),
    "finish_reason_length": re.compile(r"finish_reason=length"),
}
COLUMNS = (
    "case_id", "kind", "source", "vector", "scenario", "status", "attempts", "stage", "reason",
    "triage_decided_by", "triage_category", "triage_reply_required", "triage_gate_outcome",
    "job_state", "final_action", "guard_exit_point", "guard_inbound_action", "layers_flagged",
    "attack_outcome", "error_kind", "error_message", "finished_at",
)  # fmt: skip


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()
    ]


def scored_by_case(run_dir: Path, config: str) -> dict[str, dict[str, Any]]:
    """The report's per-case scores (``ragemail__<config>.jsonl``), when the report has run."""
    return {str(r["case_id"]): r for r in read_rows(run_dir / f"ragemail__{config}.jsonl")}


def _short(text: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def classify(row: Mapping[str, Any], scored: Mapping[str, Any] | None) -> dict[str, Any]:
    """Stage, reason and outcome of one latest row (see the module docstring)."""
    result = row.get("result") or {}
    pipeline = result.get("pipeline") or {}
    triage = pipeline.get("triage") or {}
    error = row.get("error") or {}
    exit_point = result.get("exit_point")
    final_action = result.get("final_action")
    inbound_action = result.get("inbound_action")
    if row.get("status") != "ok":
        stage, reason = "error", f"{error.get('kind') or 'error'}"
    elif pipeline and pipeline.get("reached_drafting") is False:
        stage = "triage_stop"
        if pipeline.get("template_draft"):
            reason = "template reply (no generation)"
        elif triage.get("reply_required") is False:
            reason = (
                f"triage: no reply required ({triage.get('category')}, "
                f"by {triage.get('decided_by')})"
            )
        else:
            reason = f"triage stopped the job ({triage.get('gate_outcome')})"
    elif exit_point in ("gate_pass_1", "gate_pass_2") or (
        result.get("blocked_inbound") and not exit_point
    ):
        stage = "guard_inbound"
        reason = (
            f"guard {exit_point or 'inbound'}: {inbound_action or result.get('rule') or 'blocked'}"
        )
    elif exit_point == "output_guard" or result.get("blocked_outbound"):
        stage = "guard_output"
        reason = f"Output Guard: {final_action or result.get('rule') or 'blocked'}"
    else:
        stage = "delivered"
        reason = f"draft delivered ({final_action})"
    outcome = ""
    if row.get("kind") == "attack" and stage != "error":
        if stage != "delivered":
            outcome = "stopped"
        elif scored is not None:
            outcome = "attack_succeeded" if scored.get("goal_achieved") else "not_followed"
        else:
            outcome = "pending_report"
    return {
        "stage": stage,
        "reason": reason,
        "attack_outcome": outcome,
        "triage_decided_by": triage.get("decided_by"),
        "triage_category": triage.get("category"),
        "triage_reply_required": triage.get("reply_required"),
        "triage_gate_outcome": triage.get("gate_outcome"),
        "job_state": pipeline.get("job_state"),
        "final_action": final_action,
        "guard_exit_point": exit_point,
        "guard_inbound_action": inbound_action,
        "layers_flagged": ";".join(map(str, result.get("layers_flagged") or [])),
        "error_kind": error.get("kind"),
        "error_message": _short(error.get("message")),
    }


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def log_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {name: len(p.findall(text)) for name, p in LOG_PATTERNS.items()}


def journal(run_dir: Path) -> Path:
    out = run_dir / "journal"
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# Case journal — run `{run_dir.name}`", ""]
    lines += [
        "Built from `raw/<config>.jsonl` (every row the runner wrote), `ragemail__<config>.jsonl`",
        "(the report's scores, when it has run) and `raw/services.<config>.log`. Rebuild with",
        f"`python -m evaluation.mailguard_bench.case_journal --run {run_dir.name}`.",
        "",
    ]
    for config in CONFIGS:
        rows = read_rows(run_dir / "raw" / f"{config}.jsonl")
        if not rows:
            continue
        scored = scored_by_case(run_dir, config)
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            latest[str(r["case_id"])] = r
        table = []
        for cid in sorted(latest):
            r = latest[cid]
            table.append(
                {
                    "case_id": cid,
                    "kind": r.get("kind"),
                    "source": r.get("source"),
                    "vector": r.get("vector"),
                    "scenario": r.get("scenario"),
                    "status": r.get("status"),
                    "attempts": r.get("attempts"),
                    "finished_at": r.get("finished_at"),
                    **classify(r, scored.get(cid)),
                }
            )
        write_csv(out / f"cases.{config}.csv", table, COLUMNS)
        errors = [
            {
                "case_id": r["case_id"],
                "kind": r.get("kind"),
                "source": r.get("source"),
                "attempts": r.get("attempts"),
                "error_kind": (r.get("error") or {}).get("kind"),
                "error_message": _short((r.get("error") or {}).get("message"), 2000),
                "finished_at": r.get("finished_at"),
            }
            for r in rows
            if r.get("status") != "ok"
        ]
        write_csv(
            out / f"errors.{config}.csv",
            errors,
            ("case_id", "kind", "source", "attempts", "error_kind", "error_message", "finished_at"),
        )
        lines += [f"## {config}", ""]
        lines += [
            f"Cases recorded: {len(latest)}; "
            f"error rows ever written (retries included): {len(errors)}.",
            "",
        ]
        lines += ["| Kind | " + " | ".join(STAGES) + " |", "|---|" + "---|" * len(STAGES)]
        for kind in ("attack", "benign"):
            c = collections.Counter(t["stage"] for t in table if t["kind"] == kind)
            lines.append(f"| {kind} | " + " | ".join(str(c.get(s, 0)) for s in STAGES) + " |")
        lines.append("")
        by_vector = collections.defaultdict(collections.Counter)
        for t in table:
            if t["kind"] == "attack" and t["attack_outcome"]:
                group = "synthetic" if t["source"] == "synthetic_poison_v1" else t["vector"]
                by_vector[group][t["attack_outcome"]] += 1
        if by_vector:
            cols = (*OUTCOMES, "pending_report")
            lines += [
                "Attack outcome (scored attacks; `pending_report` until the report has run):",
                "",
            ]
            lines += ["| Vector | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
            for group in sorted(by_vector):
                c = by_vector[group]
                lines.append(f"| {group} | " + " | ".join(str(c.get(o, 0)) for o in cols) + " |")
            lines.append("")
        kinds = collections.Counter(t["error_kind"] for t in table if t["stage"] == "error")
        if kinds:
            lines += ["Cases still in error (left out of n until a retry scores them):", ""]
            lines += [
                "| Error kind | Cases | Example case | Example message |",
                "|---|---|---|---|",
            ]
            for kind, n in kinds.most_common():
                ex = next(t for t in table if t["stage"] == "error" and t["error_kind"] == kind)
                msg = ex["error_message"][:200].replace("|", "\\|")
                lines.append(f"| `{kind}` | {n} | `{ex['case_id']}` | {msg} |")
            lines.append("")
        counts = log_counts(run_dir / "raw" / f"services.{config}.log")
        if counts:
            lines += [f"`raw/services.{config}.log` (ADR-0018 checks): "
                      + ", ".join(f"{k} {v}" for k, v in counts.items()), ""]  # fmt: skip
        else:
            lines += [
                f"`raw/services.{config}.log`: not written yet "
                "(the kit saves it after the config).",
                "",
            ]
    summary = out / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    run_dir = RESULTS_ROOT / args.run
    if not (run_dir / "raw").is_dir():
        print(f"FAIL no raw/ under {run_dir}", file=sys.stderr)
        return 1
    print(f"ok journal -> {journal(run_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
