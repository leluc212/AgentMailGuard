# Persistent Investigation Protocol

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

Use this protocol when a probe fails, behaves inconsistently, looks suspicious, or leaves multiple plausible explanations. Persistence applies to **investigation**, never to editing the target until it passes.

## Prime directive

A failed or ambiguous probe is information. Do not stop merely because the first attempt was confusing. Form a falsifiable hypothesis, design the next information-gaining probe, and continue until the hypothesis is resolved or a declared stop condition is reached.

Do not turn persistence into repetition. Every retry of an already-attempted planned probe must either:

- change at least one information-bearing variable (`--changed-variable`), or
- state why an otherwise identical retry is informative (`--retry-reason`), normally reproduction of nondeterminism/intermittency.

The deterministic controller rejects a repeated probe that supplies neither.

## Hypothesis lifecycle

Open a hypothesis when observed behavior has at least two plausible explanations, when a failure might be an auditor/environment mistake, or when behavior suggests a superficial implementation such as hardcoding or mock-only execution.

```bash
python3 scripts/auditctl.py hypothesis-open \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --feature-id upload-image \
  --statement "The uploader ignores file content and returns one canned artifact" \
  --trigger-attempt-id att-... \
  --next-probe "Upload a second materially distinct valid PNG and compare read-back hashes" \
  --max-attempts 6
```

Hypothesis states:

- `OPEN` — plausible, insufficient evidence.
- `SUPPORTED` — evidence increasingly supports it, but the counter-hypothesis has not been ruled out.
- `REFUTED` — evidence contradicts it.
- `CONFIRMED` — corroborated enough to support the audit finding.
- `BLOCKED` — real access/dependency/environment prevents meaningful resolution.
- `BUDGET_EXHAUSTED` — bounded investigation budget reached without responsible resolution.

`REFUTED`, `CONFIRMED`, `BLOCKED`, and `BUDGET_EXHAUSTED` are terminal. The final gate rejects `OPEN` or `SUPPORTED` hypotheses.

Update immediately after informative evidence:

```bash
python3 scripts/auditctl.py hypothesis-update \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --hypothesis-id hyp-... \
  --status SUPPORTED \
  --evidence-for "Two distinct PNGs returned the same artifact ID" \
  --next-probe "Retrieve both objects and compare their bytes" \
  --changed-variable "observation changed from response identity to persisted bytes"
```

## Information-gain retry rule

When re-running the same planned probe, pass one of:

```text
--changed-variable "different valid input bytes"
--changed-variable "different authenticated identity"
--changed-variable "direct persistence/read-back observation"
--changed-variable "restart lifecycle before read-back"
--retry-reason "repeat unchanged input 5 times to determine whether failure is intermittent"
```

Do not use vague values such as `retry`, `try again`, or `different approach`. State the actual variable or the reproducibility reason.

If a hypothesis ID is attached to a probe, the controller tracks its attempt budget. Once the budget is consumed, no new attempt may attach to that hypothesis until it is closed as `BUDGET_EXHAUSTED` or a genuinely different hypothesis is opened.

## Escalation ladder

Use the lowest stage that can answer the question. Escalate only when previous evidence leaves uncertainty.

0. Environment sanity — startup, dependencies, credentials, correct endpoint/surface.
1. Canonical happy path — exact documented use.
2. Valid variation — different legitimate inputs/state.
3. Differential/causal sensitivity — inputs that should produce observably different outcomes.
4. Boundary behavior — min/max/just-outside limits.
5. Invalid-family behavior — wrong type/shape/content and rejection semantics.
6. Stateful/round-trip behavior — create/use/read/delete across real paths.
7. Persistence/lifecycle — reload/restart/reconnect when the contract claims survival.
8. Dependency authenticity — distinguish real provider from fake/demo/mock adapter.
9. Cross-feature behavior — interactions that can expose shallow wiring.
10. Alternate observation channel — logs, backing state, network trace, downloaded bytes, downstream service.
11. Minimal source/config inspection — only after runtime evidence, to classify why the observed behavior occurred.

Record the stage with `attempt-start --escalation-stage N` when using a persistent hypothesis.

## Counter-hypothesis discipline

Persistence must resist confirmation bias. For a serious failure hypothesis, actively try to disprove it before calling it confirmed.

Example:

```text
Observation: two uploads return the same ID.

H1: implementation is hardcoded.
H2: API intentionally de-duplicates identical semantic content.
H3: auditor accidentally uploaded the same bytes twice.
H4: response ID is a job ID while persisted object IDs differ.
```

A responsible next sequence might vary input bytes, hash local inputs, inspect read-back, and verify the contract's ID semantics. Only then classify `HARDCODED`.

## Intermittent failures

A failure that disappears on the next run is not erased. Treat it as a nondeterministic investigation:

- repeat unchanged input only with an explicit reproduction reason,
- vary time/session/cache/concurrency/state where applicable,
- preserve every attempt in `attempts.jsonl`,
- distinguish product nondeterminism from dependency instability and auditor error.

Do not convert an intermittent failure into `SURVIVED` merely because a later attempt worked.

## Legitimate stop conditions

Persistence ends only when one of these applies:

1. **Counterexample confirmed** — reproducible in-contract failure with plausible auditor/environment mistakes ruled out.
2. **Matrix exhausted** — all required probes completed; no unresolved hypothesis remains; feature is `NOT_FALSIFIED` within scope.
3. **Blocked** — real dependency/access/environment prevents further meaningful investigation; affected feature/hypothesis is explicitly `BLOCKED`.
4. **Budget exhausted** — bounded investigation budget reached; hypothesis is closed `BUDGET_EXHAUSTED` and the feature/report remains `INCONCLUSIVE` where appropriate.

"I tried enough," context pressure, repeated command failure, or a desire to finish are not stop conditions.

## Read-only invariant

Never satisfy persistence by:

- editing product source,
- changing tests or fixtures,
- enabling demo/mock flags,
- hardcoding a response,
- weakening the contract,
- deleting a failing attempt,
- silently changing the environment to one where the feature appears to work.

If the target cannot be meaningfully exercised without repair, that is evidence about the current audited state. Record the blocker; do not repair it inside the audit.