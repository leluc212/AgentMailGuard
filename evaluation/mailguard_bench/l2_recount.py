"""Recount L2 schema fallbacks in a v1 run, where its stored rows allow it (Amendment 1, E.2).

    python -m evaluation.mailguard_bench.l2_recount --run-dir evaluation/results/mailguard_bench/RUN
        [--config C3]

The v1 guard (AgentMailGuard 81df5d07) cannot mark a failed L2 answer. Its ``ExtractorOutput``
gives every field a default, so an answer that does not carry the schema is accepted and merged
as far as it goes; the new guard requires every field and marks such an answer ``schema_missing``
(or ``non_json`` for prose). Two groups of stored verdicts are told apart:

* ``possible_fallback``: only the heuristic entity keys (``order_ids``, ``emails``, ``amounts``,
  ``dates``), the default confidence 0.5 and no instructions, next to ``llm_used: true``. That is
  what prose (``{"raw_text": ...}``) or an empty JSON object leaves.
* ``candidate_partial_answer``: only the heuristic entity keys but a confidence, a finding or
  instructions the model chose. That is what a JSON answer without the ``entities`` field leaves;
  a complete answer with an empty ``entities`` object looks the same.

Neither is an upper bound on Amendment 1 E.2's definition ("the answer did not carry the schema"):
a partial answer that carries ``entities`` but lacks another required field (``user_intent``,
``requested_actions``, ``contains_assistant_instructions``, ``instructions_to_assistant``,
``confidence``) leaves no trace in a v1 row. The two groups together are the rows that can be
answers without the entities field or without the whole schema; a low or zero number is not proof.

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

from evaluation.mailguard_bench.scheme import SCHEME_V2, SchemeMixError, folder_scheme
from evaluation.mailguard_bench.scoring import RawRecord, read_raw

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


def recount(records: Sequence[RawRecord]) -> L2Recount:
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


def render(config: str, counted: L2Recount) -> str:
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; prints the recount or ``FAIL ...`` and returns 1."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", default="C3")
    args = parser.parse_args(argv)
    try:
        run_scheme = folder_scheme(args.run_dir)
    except SchemeMixError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if run_scheme == SCHEME_V2:
        print(
            f"FAIL {args.run_dir} is a scheme v2 run: this recount is for the v1 guard, which "
            "cannot mark a failed L2 answer, and its default C3 has no L2 in v2. The v2 guard "
            "records its fallbacks and `make mailguard-report` counts them exactly",
            file=sys.stderr,
        )
        return 1
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
