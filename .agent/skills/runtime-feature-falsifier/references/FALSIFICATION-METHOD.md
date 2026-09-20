# Falsification Method

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

## 1. Audit the claim, not the implementation shape

A feature claim is an observable proposition about the running product. Translate vague nouns into falsifiable behavior.

Bad claim:

> Image upload exists.

Better claim:

> From the profile editor, an authenticated user can select a supported image, submit it, receive success only after storage succeeds, reload the profile, and see the uploaded image associated with the same account.

This exposes several independently falsifiable obligations: entry point, acceptance, storage, association, retrieval/rendering, and persistence.

## 2. Build a causal chain

For every feature, model:

`input -> entry point -> processing -> side effect/state -> downstream observation`

Probe the chain at the user-visible boundaries. A feature can look healthy at the first boundary while failing later.

Examples of false positives:

- UI toast says "uploaded" but no request was sent.
- API returns an object ID but no object was persisted.
- save command exits 0 but file is unchanged.
- payment UI says success but transaction is a fixture.
- search returns plausible data independent of the query.

## 3. Use counterexample-oriented probe families

### A. Valid variation

Use at least two semantically different valid inputs. This is the simplest defense against fixture-specific behavior.

### B. Metamorphic / causal sensitivity

Change one meaningful input dimension while holding other conditions constant. Ask what output/state must change if the implementation is genuine.

Examples:

- uploaded bytes A != B -> read-back content/hash should reflect A vs B,
- create name `alpha` vs `beta` -> subsequent reads should preserve the selected name,
- query `cats` vs `ships` -> result set should respond to query semantics,
- user A vs B -> ownership/authorization should differ where required.

### C. Negative-type probes

Use data outside the accepted family. A fake implementation often says success without parsing, validating, or performing work.

The expected result may be a clean rejection. A rejection is evidence of validation, not a failure, when the contract excludes the input.

### D. Boundary probes

Prefer values around documented boundaries: zero/empty, one, max-1, max, max+1. Do not generate harmful loads merely to test limits.

### E. Stateful probes

Observe before and after. For create/update/delete/upload features, verify both visible output and backing state when accessible through legitimate product interfaces.

### F. Round-trip probes

Create through one real path and consume through another. Round trips catch fake success and disconnected implementations.

Examples:

- upload -> list -> download,
- create -> read -> update -> read,
- CLI write -> app read,
- API mutation -> UI refresh.

### G. Restart/persistence probes

Only when persistence is claimed and restarting is safe: create state, restart the service, then retrieve it.

### H. Identity/authorization probes

Only with authorized test identities. Verify behavior follows the current identity rather than a fixed demo account.

### I. Dependency authenticity

A feature that only works with `MOCK=true`, hardcoded fixtures, fake adapters, or bypassed dependencies is not evidence that the production path works.

Compare behavior under the intended runtime configuration. Record any environment/config change made to execute a probe.

## 4. Differential triage patterns

| Observation | Suspicion | Follow-up |
|---|---|---|
| Different inputs -> identical output | Hardcoded/fixture | Hash/compare outputs, inspect downstream state |
| Success response -> no state change | Fake/no-op/be-there-to-look-pretty (BTTLP) | Round-trip/read-back, inspect logs/config |
| UI changes -> no network/state effect | be-there-to-look-pretty (BTTLP) | Capture network and persistence evidence |
| Works only under mock/demo flag | Mock-only | Run intended config, inspect adapter/provider wiring |
| 501/NotImplemented/TODO marker | TODO | Capture runtime + source marker |
| Only one canned input works | Hardcoded/narrow fixture | Vary valid inputs systematically |
| Happy path works, valid variant fails | Partial/broken | Identify contract boundary and minimal reproducer |
| Unsupported data gets success | Fake validation/type confusion | Verify resulting side effect and retrieval |

## 5. Do not overclaim

`NOT_FALSIFIED` means only:

> No counterexample was found within the executed probe set under this environment.

It does not mean correct, secure, complete, production-ready, or fully implemented.

## Runtime-surface evidence boundary

The controller can require a documented startup path, an `environment_start` probe, and reproduction metadata for dependency-sensitive attempts. Those controls improve auditability but cannot cryptographically prove that a browser/API/CLI action used the intended public surface instead of an internal helper. Treat `entry_point`, `repro_command`, network/browser captures, and downstream evidence together. If the distinction materially affects a conclusion, capture an observable transport/UI artifact rather than relying on self-reported log text alone.
