"""Recount L2 schema fallbacks in a v1 run, where its stored rows allow it (Amendment 1, E.2).

    python -m evaluation.mailguard_bench.l2_recount --run-dir evaluation/results/mailguard_bench/RUN
        [--config C3]

The v1 guard (AgentMailGuard 81df5d07) cannot mark a failed L2 answer. When the model answers
in prose, ``parse_json_or_text`` returns ``{"raw_text": ...}``; every ``ExtractorOutput`` field
has a default, so L2 reports a normal verdict that merged nothing from the model. Its stored
verdict then holds only the heuristic entity keys (``order_ids``, ``emails``, ``amounts``,
``dates``), the default confidence 0.5 and no instructions, next to ``llm_used: true``. A real
answer usually leaves more: entity keys of its own, a confidence it chose, a finding or
instructions. So the count is an upper bound: an answer with nothing to merge looks the same.

What the stored data cannot show is said in the output: the guard's own ``audit__<config>.jsonl``
is its L5 policy log (severity, score and decider per layer), and a row without an L2 verdict
gives nothing to count. Rows of a run that records its fallbacks (``guard_fallbacks``) are
counted exactly. The command only reads; it is never run on the published results by the
tooling that builds it.

(docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md Amendment 1, E.2; R22.12)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.scoring import RawRecord, read_raw

# The keys of the v1 guard's ``heuristic_entities`` (extractor.py): all a prose answer leaves.
HEURISTIC_ENTITY_KEYS = frozenset({"order_ids", "emails", "amounts", "dates"})
DEFAULT_CONFIDENCE = 0.5  # ExtractorOutput.confidence when the answer carried none
LLM_FINDING_RULE = "llm:assistant_instructions"

ANSWERED = "answered"
POSSIBLE_FALLBACK = "possible_fallback"
LOUD_FAILURE = "loud_failure"
AI_STEP_NOT_RUN = "ai_step_not_run"
NO_L2_VERDICT = "no_l2_verdict"


def classify_l2(verdict: Mapping[str, Any] | None) -> str:
    """What one stored L2 verdict of the v1 guard can prove about its AI step.

    Returns:
        ``no_l2_verdict`` (nothing stored), ``loud_failure`` (the guard recorded ``llm_error``),
        ``ai_step_not_run`` (``llm_used`` is not true), ``answered`` (the verdict carries data
        only a model answer has) or ``possible_fallback`` (only what a prose answer leaves).
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
    model_data = (
        not set(entities) <= HEURISTIC_ENTITY_KEYS
        or verdict.get("confidence") != DEFAULT_CONFIDENCE
        or bool(metadata.get("instructions_to_assistant"))
        or any(isinstance(f, Mapping) and f.get("rule_id") == LLM_FINDING_RULE for f in findings)
    )
    return ANSWERED if model_data else POSSIBLE_FALLBACK


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
    loud_failure: int
    ai_step_not_run: int
    no_l2_verdict: int
    recorded_schema_fallback: int
    recorded_rows: int


def recount(records: Sequence[RawRecord]) -> L2Recount:
    """Count the rows by what their L2 verdicts show, an exact recording taking precedence."""
    counts = {
        ANSWERED: 0,
        POSSIBLE_FALLBACK: 0,
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
        loud_failure=counts[LOUD_FAILURE],
        ai_step_not_run=counts[AI_STEP_NOT_RUN],
        no_l2_verdict=counts[NO_L2_VERDICT],
        recorded_schema_fallback=recorded_schema,
        recorded_rows=recorded,
    )


def render(config: str, counted: L2Recount) -> str:
    """The recount as plain text, with what the stored rows do not allow said out loud."""
    judged = counted.answered + counted.possible_fallback
    lines = [
        f"{config}: {counted.rows} rows ({counted.scored_rows} scored).",
        f"L2's AI step ran without a recorded failure in {judged} row(s); "
        f"{counted.possible_fallback} of them look like an L2 answer without the schema (only the "
        "heuristic entity keys order_ids, emails, amounts and dates, the default confidence 0.5 "
        f"and no instructions); the other {counted.answered} carry data only a model answer has.",
        "This is an upper bound, not a count: a genuine answer with nothing to merge looks the "
        "same, so a low or zero number is not proof that no schema fallback happened.",
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; prints the recount or ``FAIL ...`` and returns 1."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", default="C3")
    args = parser.parse_args(argv)
    path = args.run_dir / "raw" / f"{args.config}.jsonl"
    try:
        records = read_raw(path)
    except FileNotFoundError:
        print(f"FAIL {path} does not exist", file=sys.stderr)
        return 1
    print(render(args.config, recount(records)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
