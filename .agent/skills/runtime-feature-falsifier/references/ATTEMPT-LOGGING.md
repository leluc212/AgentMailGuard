# Attempt Logging Protocol

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

The audit log is an append-only event history, not a cleaned-up narrative. The most important failures are often crashes, timeouts, or interrupted probes, so logging only after execution is insufficient.

## Two-phase rule

Every runtime attempt has exactly two lifecycle events:

1. `STARTED` — append **before** executing the runtime action.
2. `FINISHED` — append immediately after the action, including crashes, timeouts, blockers, and inconclusive outcomes.

Use `scripts/auditctl.py attempt-start` and `attempt-finish`; do not hand-edit `attempts.jsonl`.

An unmatched `STARTED` event is evidence of an interrupted audit. The final gate rejects it.

## Hash-chain integrity

`auditctl` adds these fields to every event:

- `sequence`
- `prev_event_sha256`
- `event_sha256`

The gate recomputes the chain. This is tamper-evident rather than tamper-proof: an operator with filesystem access could rewrite the entire file, but accidental/manual deletion or alteration is detected during ordinary operation.

## One attempt = one primary question

Prefer one precise `probe_intent`:

- `environment_start`
- `baseline_valid`
- `valid_variation`
- `format_variation`
- `causal_sensitivity`
- `invalid_type_or_shape`
- `mime_extension_mismatch`
- `corrupt_input`
- `boundary_min`
- `boundary_max`
- `boundary_above_max`
- `state_transition`
- `round_trip`
- `persistence`
- `repeat_or_duplicate`
- `authorization`
- `dependency_authenticity`
- `workflow_completion`
- `hardcode_discrimination`
- `mock_discrimination`
- `bttlp_discrimination`

Custom intents are allowed when they are more precise. Avoid `test1`, `misc`, or similarly opaque labels.

## Required chronology

For each probe:

```text
plan entry
  -> attempt-start
  -> real runtime interaction
  -> capture direct + downstream evidence
  -> attempt-finish
  -> next probe
```

Never reconstruct only successful attempts at the end.

Setup failures count when they affect auditability: application startup failure, browser connection failure, missing provider, failed login, timeout, malformed fixture, or corrected retry. Keep both the failed fixture attempt and the corrected retry.

## Result semantics

- `SURVIVED`: this one probe did not falsify its proposition.
- `FALSIFIED`: observed behavior contradicts a claimed obligation.
- `BLOCKED`: a required real environment/dependency/access condition prevented the probe.
- `INCONCLUSIVE`: the contract or evidence is too ambiguous to decide.

A correct rejection of an invalid input is `SURVIVED`, not a failure.

## Evidence references

Use stable paths under `.runtime-feature-audit/evidence/` when possible. Examples:

- before/after screenshots,
- request/response or HAR captures,
- stdout/stderr files,
- content hashes,
- retrieved/downloaded artifacts,
- state snapshots,
- provider/log excerpts.

A status code, toast, generated ID, or exit code without the advertised end effect is weak evidence.

## Batch logging

For large audits, use `attempt-batch` to reduce controller ceremony without weakening chronology. A `START` batch pre-registers several attempts before execution; a later `FINISH` batch records their observed outcomes. The controller validates the entire batch, serializes accepted events under one lock, and continues the same hash chain. Batching never authorizes invented observations or post-hoc logging of probes that were not executed.

Workers may execute pre-registered probes concurrently, but **only `auditctl.py` writes canonical JSONL**. Use unique evidence filenames per worker. The controller uses a cross-platform lock (POSIX `flock`, Windows `msvcrt`) so concurrent controller processes serialize safely. Readers of canonical JSONL take that same exclusive lock; summary/report/gate and chain-head reads therefore wait for an in-progress append instead of parsing a partial final line.

Every controller response includes current `chain_heads` for attempts and hypotheses. Preserve command output in the agent transcript when possible; this gives an external record of the observed chain head even though a filesystem-capable agent could theoretically rewrite the local log and recompute hashes.


### Batch document example

Register several probes before execution:

```json
{
  "schema_version": "1.0",
  "phase": "START",
  "attempts": [
    {
      "feature_id": "upload-image",
      "probe_id": "png-a",
      "action": "Upload valid-a.png through the public upload surface"
    },
    {
      "feature_id": "upload-image",
      "probe_id": "png-b",
      "action": "Upload valid-b.png through the public upload surface"
    }
  ]
}
```

After those registered actions actually run, ingest their results using `phase: "FINISH"` and the returned `attempt_id` values. Do not combine START and FINISH for a probe into one post-hoc batch; two-phase chronology is the integrity property.
