"""Worked examples for the report: two defended attacks and the attacks that got through.

Chosen deterministically (sorted case ids) from the LLMail-Inject attacks: defended
examples are attacks that succeeded under C0 (rag-email's native reply path, no
AgentMailGuard code) and failed under C3, the first
one blocked at the inbound gate and the next one credited to a different layer; failures
are up to two attacks that succeeded under C3. No API calls.

A live attack (task 7.20) that triage stopped before the drafting step is never a defended
example: the guard did not see it, and the C0 run only reached the drafting step because triage
decided differently there.

(docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md §4b "Worked examples";
specs/tasks.md 7.19, 7.20)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evaluation.mailguard_bench.leakage import case_text
from evaluation.mailguard_bench.scoring import RawRecord, triage_bucket

EXCERPT_CHARS = 700
# What C3 did with a live email that never reached the drafting step (scoring.triage_bucket).
TRIAGE_RESULTS = {
    "early_exit": "no draft: triage stopped the email, so it never reached the guard",
    "template": "template draft written by triage, so the email never reached the guard",
    "stuck_unconsumed": (
        "no draft: the job was left QUEUED with no drafting consumer, so the email never "
        "reached the guard"
    ),
}


@dataclass(frozen=True)
class Example:
    """One worked example."""

    outcome: str  # "defended" | "succeeded"
    case_id: str
    scenario: str
    email: str
    c0_draft: str | None
    c3_result: str
    c3_draft: str | None
    layer: str | None


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + " […]"


def _draft(record: RawRecord | None) -> str | None:
    if record is None or record.reply_v1 is None:
        return None
    return str(record.reply_v1.get("draft") or "")


def _c3_result(record: RawRecord) -> str:
    bucket = triage_bucket(record) if record.stopped_before_drafting else None
    if bucket is not None:
        return TRIAGE_RESULTS[bucket]
    if record.blocked_inbound:
        return "blocked before generation (inbound decision)"
    if record.blocked_outbound:
        return "draft generated, then blocked (outbound decision)"
    return f"draft not blocked (final action {record.final_action or 'reply'})"


def pick_examples(
    cases: Mapping[str, Mapping[str, Any]],
    c0: Mapping[str, Mapping[str, Any]],
    c3: Mapping[str, Mapping[str, Any]],
    c0_records: Mapping[str, RawRecord],
    c3_records: Mapping[str, RawRecord],
    layers: Mapping[str, str | None],
    *,
    llmail_ids: set[str],
    n_failures: int = 2,
) -> list[Example]:
    """Pick the worked examples.

    Args:
        cases: BenchCase dicts by id.
        c0: Scored C0 ``CaseResult`` dicts by id (``ragemail__C0.jsonl``).
        c3: Scored C3 ``CaseResult`` dicts by id.
        c0_records: Raw C0 records by id (for the no-guard draft text).
        c3_records: Raw C3 records by id.
        layers: First catching layer per C3 case id.
        llmail_ids: Ids of the LLMail-Inject table.
        n_failures: How many successful C3 attacks to show at most.
    """
    ids = sorted(
        i
        for i in llmail_ids
        if i in c3 and c3[i].get("kind") == "attack" and i in c3_records and i in cases
    )
    defended = [
        i
        for i in ids
        if not c3[i]["goal_achieved"]
        and c0.get(i, {}).get("goal_achieved")
        and not c3_records[i].stopped_before_drafting  # triage's stop is not the guard's defence
    ]
    chosen: list[str] = []
    inbound = [i for i in defended if c3_records[i].blocked_inbound]
    if inbound:
        chosen.append(inbound[0])
    for i in defended:
        if len(chosen) == 2:
            break
        if i not in chosen and (not chosen or layers.get(i) != layers.get(chosen[0])):
            chosen.append(i)
    for i in defended:
        if len(chosen) == 2:
            break
        if i not in chosen:
            chosen.append(i)
    failures = [i for i in ids if c3[i]["goal_achieved"]][:n_failures]

    def build(case_id: str, outcome: str) -> Example:
        record = c3_records[case_id]
        return Example(
            outcome=outcome,
            case_id=case_id,
            scenario=str((cases[case_id].get("meta") or {}).get("scenario") or "n/a"),
            email=_excerpt(case_text(cases[case_id])) or "",
            c0_draft=_excerpt(_draft(c0_records.get(case_id))),
            c3_result=_c3_result(record),
            c3_draft=_excerpt(record.final_body if not record.blocked else _draft(record)),
            layer=layers.get(case_id),
        )

    return [build(i, "defended") for i in chosen] + [build(i, "succeeded") for i in failures]


def _fence(text: str | None) -> list[str]:
    body = "(none)" if text is None else text.replace("~~~", "~ ~ ~")
    return ["~~~text", body, "~~~"]


def render_examples(examples: Sequence[Example]) -> str:
    """Markdown section for ``analyses.md``; attack text is quoted inside fences."""
    lines = ["## Worked examples", ""]
    if not any(e.outcome == "succeeded" for e in examples):
        lines += ["No LLMail-Inject attack succeeded under C3, so there is no failure example.", ""]
    for n, e in enumerate(examples, start=1):
        title = "Defended" if e.outcome == "defended" else "Got through"
        lines += [f"### Example {n} — {title} (`{e.case_id}`, scenario {e.scenario})", ""]
        lines += ["Attack email:", ""] + _fence(e.email) + [""]
        lines += (
            ["Draft under C0 (rag-email as it runs, no AgentMailGuard code):", ""]
            + _fence(e.c0_draft)
            + [""]
        )
        lines += [f"With the guard (C3): {e.c3_result}."]
        if e.layer:
            lines.append(f"Stopped by: {e.layer}.")
        lines += ["", "C3 draft:", ""] + _fence(e.c3_draft) + [""]
    return "\n".join(lines).rstrip()
