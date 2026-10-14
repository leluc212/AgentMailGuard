# ADR-0015: AgentMailGuard architecture v3 (four guard layers, no outbound gate)

- **Status:** Proposed (owner decisions of 2026-10-12, amended by the review of 2026-10-13); branch `refactor/guard-v3`
- **Date:** 2026-10-12
- **Decided by:** project owner (plan review of 2026-10-12, `plan_v3/plan_v3.md`)
- **Target architecture:** `plan_v3/architect_v3.jpg`
- **Amends:** ADR-0010 (the guard's layer list), ADR-0012 decisions 2(b) (visible fallback, now
  behind a flag) and 11 (config names; a third scheme), ADR-0014 decision 3 (the guard pin moves)
- **Changes:** `agentmailguard/` (package, configs, prompts, tests, docs), root
  `evaluation/mailguard_bench`, `tests/unit/test_mailguard_*`, `Makefile`, docs, `specs/`

## Context

The v2 guard has six numbered layers: L1 injection scanner, L2 intent extractor, L3 channel
isolation, L3b retrieved document scanner, L4 output scanner and L5 policy engine, which is called
twice (an inbound gate after L1/L2 and an outbound gate after L4). Three properties of that layout
were wrong for the owner's model of the guard:

1. L1 and L2 read the same email with the same cheap detectors; they are one inbound decision.
2. The document scanner ran *after* the inbound gate, so poisoned knowledge could never stop a job
   before the model call, and the gate never saw it.
3. Channel isolation builds the prompt; it decides nothing, so it is not a guard layer. The outbound
   gate re-evaluated every verdict and could not remember the inbound decision, so "strictest action
   wins" across the two gates held only by accident.

Two measured defects of v2 must not survive the refactor (benchmark v1, 2026-09):

4. The intent extractor acted as a second, uncalibrated detector: its `contains_assistant_instructions`
   raised severity on ordinary question emails (Llama-3.1-8B quarantined 86 of 100 harmless question
   emails, which invalidated its C0T/C3 rows).
5. The document scanner compared chunks with the whole email body. On the 100 pinned poisoned-document
   cases it caught every poisoned chunk in 32; a replay that compares against the sender's question
   sentences caught 85, with an ordered-prefix signal 87–89, and flagged 0 clean chunks. The guard
   stopped the poisoned-document route only from 79% to 32% (GPT-4o-mini) and 91% to 39% (Qwen2.5-7B).

## Decision

1. **Four guard layers, named by meaning.** Identifiers carry the meaning; "L1-L4" is a display label.

   | Label | Identifier | Was |
   |---|---|---|
   | L1 Inbound Guard | `inbound_guard` | L1 injection scanner + L2 intent extractor (one facade, two sub-stages) |
   | L2 Doc Scanner | `doc_scanner` | L3b retrieved document scanner, plus a set judge |
   | L3 Inbound Gate | `inbound_gate` | L5 `decide(stage="inbound")` |
   | L4 Output Guard | `output_guard` | L4 output scanner + L5 `decide(stage="outbound")` |
   | (not a layer) | `channel_isolation` | L3 channel isolation |

   The policy engine becomes a shared component (`mailguard.policy`), used by the gate and by L4.
2. **Order.** email → L1 → **L3 Inbound Gate pass 1** on L1 facts alone (documents marked "not
   scanned"); a block or quarantine stops here with no doc judge call, as in v2 → otherwise documents
   → L2 → **L3 Inbound Gate pass 2** with every fact, before any model call; block/quarantine →
   escalated draft with no model call; pass → channel isolation → one host generation → L4 → host
   review queue. The report keeps only the final inbound decision (pass 2, or pass 1 when it stopped)
   with `metadata.gate_passes` = 1 or 2; the audit log gets one record for that final decision, its id
   seeded with the gate pass so it stays deterministic; a pass-1 that passes is provisional and is not
   audited.
   An empty document set (template jobs) makes L2 a contractual no-op. The question sentences L2
   compares against are produced by a pure function the pipeline always runs, not by L1, so
   switching L1 off does not weaken L2.
3. **Inbound floor (strictest action wins).** L4 merges its outbound policy decision with the
   inbound decision. The inbound decision is carried only when a specific rule produced it (not the
   catch-all default) **and** its action is stricter than `auto_send`, so a clean inbound `draft_only`
   does not suppress a clean `auto_send`, and an inbound `auto_send` never counts as a floor (which keeps
   the invariant "auto_send only without a floor and on the allowlist"). The
   floor is code, not configuration. Invariants: the final action is at least the floor;
   `requires_human` is the OR of both; audit ids stay deterministic.
4. **Doc scanner signals and set judge.** The doc scanner's cheap stage compares each chunk with the
   email's question sentences (the pure function of decision 2; recorded on L1's verdict when L1 runs),
   not with the whole body, and
   adds an ordered-prefix signal and bait clusters (chunks that echo each other are quarantined as a
   cluster). Each chunk verdict carries `trust`, `cluster_id` and the raw `signals`. The set-level
   verdict is deterministic by default (aggregate of those signals). An optional LLM set judge
   (PoisonedRAG-style poisoning spread over several chunks) is behind
   `DOC_SCANNER__SET_JUDGE_LLM_ENABLED` (off), runs only when at least one chunk scores inside the
   suspicious band, and may only quarantine more chunks. Thresholds are chosen on a separate dev split,
   never on the 100 pinned poisoned-document cases, and that split includes at least one poisoning
   style the current generator does not produce (paraphrased echo, no prefix, payload mid-chunk), so
   the thresholds are not specific to the PoisonedRAG template.
5. **Fail-closed judges.** With `MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR=true` (default) an invalid or
   failed judge answer sets the verdict's `error` (`judge_invalid:<stage>`), which the fail-closed
   rule turns into human review. `false` keeps the v2 visible fallback (the benchmark uses it to
   compare with v2). A judge that is not configured is not an invalid answer: it still degrades.
6. **Hard rename, no aliases, one read-only exception.** Old env keys (`L1__*` … `L5__*`,
   `GUARD_MODELS__JUDGE|EXTRACTOR|DOC_SCANNER`) stop the process with a migration table. Reports carry
   `schema_version: 3`; every v3 code path (runs, scoring, ablations) refuses older reports. The only
   exception is a read-only legacy reader in the bench that regenerates the committed v1/v2 result
   folders' tables (`make mailguard-report`), so the Review 1 numbers stay reproducible at `HEAD`. It
   neither runs nor converts old data. The L1 classifier artifact keeps its name
   (`l1_injection_clf_v1.joblib`) and its pinned hash. The input contract `RetrievedChunk` is renamed
   `SourceDocument`; `ChunkVerdict` stays for the per-piece verdict.
7. **Contract to the core is additive.** `decision_to_job_result` keeps every field and adds
   `reason`, `decided_by` and `guard_schema_version`.
8. **Channel isolation does not grade.** It returns `IsolationInfo` (mode, nonce, counts). Forged
   channel markers are graded where they arrive: in the email by L1 and in documents by L2. Markers
   scrubbed from thread history are recorded in `IsolationInfo.forged_markers` (`forged_channel_marker`
   findings; `markers_scrubbed` stays the count). The inbound verdict is never modified after the gate,
   so the report shows exactly what the gate saw. Because isolation runs at prompt build, after the
   inbound gate, this is visible only to L4 (outbound fact `isolation_forged_markers`, used by no rule
   by default) and to the benchmark; it never changes an inbound decision.
   A failed prompt build still fails closed through the policy's `layer_error` fact.
9. **L1 severity comes from the scan sub-stage only.** The extractor contributes intent, the
   sanitized body, `removed_ratio` and findings tagged `stage="extract"`; those findings never raise
   severity or score. This closes context item 4. The extract sub-stage always runs deterministically;
   only its LLM call is optional (`INBOUND_GUARD__EXTRACT_LLM_ENABLED`).
10. **Prompt version.** Rendering chunks with their trust level changes the guarded prompt, so
    `GUARDED_PROMPT_VERSION` becomes `guarded.v3` and is written into every run meta.
11. **Host hooks are a separate ADR.** The host-side changes that consume `trust` (review-UI badge,
    `knowledge_document.trust_level`, `exclude_document_ids`, re-retrieval, ingestion scan) are
    ADR-0016 and start after this ADR's last task.
12. **Guard call budget.** At most five guard model calls per job (two in L1, one chunk judge, one
    set judge, one output judge); the production default enables two. The report counts them and a
    test enforces the ceiling over the parity matrix. The benchmark reports calls and latency per
    configuration.
13. **Acceptance is a live measurement, not parity.** The refactor is complete only after a v3
    benchmark run (Task 16) meets the recorded criteria: C3 ASR ≤ 5% on LLMail-Inject, ≥ 80/100
    poisoned-document cases with every poisoned chunk caught, ≤ 5% of benign question emails and
    ≤ 2% of clean documents escalated, and each single-layer ablation worse than C3 on its own route
    metric (C3-L1: inbound catch rate before generation on LLMail-Inject; C3-L2: every-poisoned-chunk
    rate on the poisoned-document cases). Final ASR is reported but is not the ablation criterion,
    because L4 can still catch an injection when L1 is off.
    A failed criterion reopens Task 5 or 7 and is recorded here; thresholds are never tuned on the
    pinned cases.
14. **Parity deltas are bounded.** Every `INTENDED_DELTAS` entry names the unit test that proves the
    new behaviour independently of the golden, and each task has a pre-declared maximum number of
    deltas (Task 4 ≤ 10, raised from 3 by the owner on 2026-10-14 for the isolation deltas only,
    plan_v3 F10; 5 ≤ 6, 7 ≤ 20, 8 ≤ 6, 11 ≤ 5; every other task 0). The golden is never
    regenerated.

## Intended behaviour changes (recorded in `agentmailguard/tests/parity.py`)

- Poisoned or critical knowledge is decided at the inbound gate: a quarantine stops the job before
  generation; a human-approval escalation is carried into L4 as the floor.
- Forged markers inside thread history (not email, not documents) are still removed by isolation
  but no longer graded; they are recorded in `IsolationInfo.forged_markers`.
- With the fail-closed flag on, an invalid judge answer is human review instead of the cheap verdict.
- An email whose scan is clean but whose extractor flags assistant-style instructions is no longer
  escalated by severity (decision 9); it keeps a finding only.
- Poisoned chunks that echo the question sentences, or form a bait cluster, are quarantined by the
  cheap stage where v2 kept them (decision 4).

## Consequences

- v1/v2 benchmark runs can be re-run only from a checkout of their own commit; the bench at `HEAD`
  runs and scores v3 only, and reads the committed v1/v2 folders through the read-only legacy reader.
  The guard pin moves to the v3 commit.
- Final acceptance runs `make ci` on a Linux checkout in addition to the Windows script, because the
  Windows baseline (63 environment failures) cannot prove the make/bash recipes still work. GitHub CI
  is the authoritative baseline; the Windows list is for local regression comparison only.
- The branch is pushed after every task (never merged, no PR) so the code is reviewable and safe
  before Task 15.
- The paper's v3 tables come from the Task 16 run; v2 tables stay labelled v2 until then.
- Every document naming the old layers is updated; the paper's ablation tables are marked as v2
  results until the v3 ablation is run.
