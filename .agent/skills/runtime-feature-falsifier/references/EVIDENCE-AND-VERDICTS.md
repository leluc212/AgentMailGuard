# Evidence and Verdict Rules

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

## Evidence hierarchy

Prefer evidence closer to the claimed effect.

1. **End-effect evidence** — retrieved object, persisted state, rendered output, delivered artifact, completed external transaction in an authorized test environment.
2. **State-transition evidence** — before/after state via legitimate interface, DB/storage observation when authorized, filesystem/object existence, queue/job completion.
3. **Interaction evidence** — browser trace, network request/response, CLI execution, process output.
4. **Runtime logs** — useful corroboration, not sufficient alone when end effects are observable.
5. **Source/config inspection** — classification evidence after runtime probing.
6. **Tests** — claim-discovery hints only; never runtime proof for this skill.

## Runtime verdict thresholds

### FALSIFIED

Assign when at least one of the following is reproducible:

- a contract-valid input fails the advertised behavior,
- the advertised side effect is absent despite reported success,
- output is demonstrably unrelated to meaningful input where the contract requires dependence,
- the advertised entry point cannot complete its claimed workflow under the intended configured environment,
- state is lost contrary to a persistence claim,
- the feature depends exclusively on a mock/demo path rather than the intended runtime path.

### NOT_FALSIFIED

Assign only when:

- the contract is sufficiently known,
- the intended environment was used,
- the declared probe matrix was executed,
- required end effects were observed,
- no in-contract counterexample was found.

Always report the number and classes of probes. Never translate this status to "verified", "proven", "alive", "fully working", "confirmed working", "verified reality", or an equivalent success verdict.

### BLOCKED

Use when the intended probe cannot be executed because of missing credentials, unavailable real dependency, unsupported environment, or denied authorization.

Do not silently replace the blocker with a mock.

### INCONCLUSIVE

Use when the contract is ambiguous or evidence conflicts enough that a falsification judgment cannot be made responsibly.

## Persistent-investigation evidence

When an observed failure has plausible alternate explanations, use `hypothesis-ledger.jsonl` rather than jumping directly from suspicion to classification. A `CONFIRMED` hypothesis must be tied to a trigger or linked runtime attempt whose result is `FALSIFIED`; ledger prose alone cannot establish a product failure.

A hypothesis closed `REFUTED` should contain concrete evidence against it. `BLOCKED` means the real investigation path cannot continue because of access/environment/dependency constraints. `BUDGET_EXHAUSTED` means the declared bounded investigation budget was actually consumed without responsible resolution; it prevents `NOT_FALSIFIED` and should surface as `INCONCLUSIVE` unless another independent counterexample already makes the feature `FALSIFIED`.

Repeated attempts are useful only when they buy information. Preserve intermittent failures even when subsequent attempts survive; investigate session, timing, cache, state, dependency, and auditor-error explanations instead of erasing the earlier observation.

## Implementation-pattern evidence

### TODO

Strong evidence:
- runtime returns Not Implemented/TODO/501 or equivalent, AND/OR
- reachable path contains explicit TODO/not-implemented branch consistent with runtime result.

### PLACEHOLDER

Strong evidence:
- advertised feature reaches a skeletal stub/default response lacking required behavior,
- source/config shows placeholder implementation consistent with runtime evidence.

### FAKE_NOOP

Strong evidence:
- reports completion/success but no required effect occurs,
- repeated observations show the action is causally disconnected from state.

### BTTLP (be-there-to-look-pretty)

Strong evidence:
- visible control/status exists primarily cosmetically,
- interaction produces UI-only feedback or superficial wiring without completing the advertised workflow,
- no required network/state/downstream action is observed.

### HARDCODED

Strong evidence:
- materially distinct valid inputs produce the same canned output/state unexpectedly,
- fixture/demo values appear regardless of input,
- runtime observation is corroborated by fixed constants/branches in the reachable path.

### MOCK_ONLY

Strong evidence:
- feature succeeds only when using a mock/demo/fake provider or test flag,
- intended runtime provider/path is missing, unimplemented, or fails,
- mock adapter is incorrectly presented as feature completion.

### PARTIAL_IMPLEMENTATION

Use when some advertised obligations are genuinely implemented but at least one required contract-valid path or end effect is absent.

### REAL_BUT_BROKEN

Use when the implementation clearly attempts the actual end-to-end behavior through real dependencies/state, but a defect prevents correct operation. Do not use this label merely to be charitable; require evidence distinguishing it from a stub/fake.

### AUDIT_ENVIRONMENT_INTERFERENCE

Use only for audit/test contamination rather than a product implementation defect. Examples include a live worker consuming the same RabbitMQ queue as an integration test, another process sharing the same database schema, or tests and the live target writing the same bucket/prefix. Pair this pattern with `BLOCKED` or `INCONCLUSIVE` while isolation is unresolved; do not present it as a product `FALSIFIED` finding unless a separate in-contract runtime counterexample exists.

## Confidence

Add `confidence`:

- `HIGH`: direct reproducible runtime evidence plus corroborating state/source evidence.
- `MEDIUM`: direct runtime evidence but classification mechanism not fully confirmed.
- `LOW`: incomplete/ambiguous evidence; usually pair with `INCONCLUSIVE` or `UNKNOWN_PATTERN`.