---
name: runtime-feature-falsifier
description: Use this skill when the user wants a runtime feature audit, smoke/exploratory verification of real product behavior, or a hunt for fake, TODO, placeholder, be-there-to-look-pretty (BTTLP), hardcoded, mock-only, no-op, or superficially wired functionality. Exercise real UI/API/CLI entry points, persistently investigate ambiguous or intermittent failures with bounded hypotheses, and distrust source presence or test-suite success. Use it for whole-project feature reality checks and individual capabilities such as upload, auth, CRUD, export, search, payment, persistence, and workflows; do not use it merely to run or repair tests.
---

# Runtime Feature Falsifier

**Normative authority:** this `SKILL.md` defines verdict semantics, retry rules, hard invariants, and completion requirements. Files under `references/` elaborate with examples and procedures but must not redefine or override these rules.

## Mission

Try to **falsify claimed software behavior at runtime**.

Do not try to prove a feature is "real" or "alive." A finite set of successful probes only supports `NOT_FALSIFIED` within the executed scope. Actively search for concrete counterexamples showing that a claimed feature is:

- `TODO` or unimplemented,
- `PLACEHOLDER`,
- `FAKE_NOOP`,
- `BTTLP` (**be-there-to-look-pretty**) — present/wired enough to look implemented without completing the advertised behavior,
- `HARDCODED` to fixtures, demos, narrow inputs, or canned outputs,
- `MOCK_ONLY`,
- `PARTIAL_IMPLEMENTATION`,
- `REAL_BUT_BROKEN`.

## Hard invariants

1. **Runtime behavior outranks source appearance.** A function, route, button, handler, schema, or test is not proof.
2. **Do not use target-project test success as runtime evidence.** Existing tests may be read only as last-resort claim discovery.
3. **Do not edit implementation, tests, fixtures, or configuration during an audit.** Audit first; repairs require a separate user request.
4. **Do not enable mock/demo/fake modes to make a feature pass.** If the intended dependency is unavailable, record `BLOCKED`.
5. **Use the real supported entry point.** Do not bypass a UI/API/CLI workflow by calling an internal helper because it is easier.
6. **Verify the advertised end effect.** A toast, 2xx, generated ID, exit code 0, or "success" string is insufficient.
7. **Log before execution and after execution.** Every runtime attempt has a `STARTED` event before the action and a `FINISHED` event afterward.
8. **Never erase inconvenient attempts.** `attempts.jsonl` is append-only and hash-chained.
9. **Persist on uncertainty, not on making the product pass.** A suspicious, failed, or intermittent result starts a bounded hypothesis-driven investigation; do not repair target code.
10. **Do not repeat blindly.** Re-running an already-attempted probe must change an information-bearing variable or state an explicit reproduction/nondeterminism reason.
11. **Do not finalise until the deterministic audit gate passes.** Findings may be `FALSIFIED`; incompleteness or unresolved hypotheses may not be hidden.
12. **Runtime preflight is three-stage and reproducible.** Record the real startup path and expected runtime identity; complete required `environment_start`, `runtime_identity`, and `environment_collision` probes. Preflight and dependency-sensitive attempts require `repro_command` metadata. A healthy container/port is not proof that the intended application is what is serving it.
13. **Audit-environment interference is not a product counterexample.** Before ordinary feature probes, check for shared mutable queues/topics, DB schemas/databases, object-store buckets/prefixes, Redis namespaces, ports/processes, tenants/accounts, filesystem paths, or test fixtures that can race with the live target. If isolation cannot be established, record `BLOCKED`/`INCONCLUSIVE`; do not mislabel contamination as a product defect.
14. **Parallel workers never write audit JSONL directly.** They may execute pre-registered probes concurrently, but only `auditctl.py` may serialize canonical attempt/hypothesis events. Canonical JSONL readers take the same cross-platform lock as writers, so summary/report/gate cannot observe a partial append.
15. **Reporting language preserves epistemic scope.** `NOT_FALSIFIED` is the verdict. Do not rename it to “verified”, “proven”, “alive”, “fully working”, “verified reality”, or equivalent in reports, retrospectives, summaries, or chat conclusions.
16. **One sealed audit run is immutable history.** After remediation, start a new audit workspace and, on harnesses with dedicated subagents such as Antigravity, spawn a fresh auditor instance. Do not append a post-fix run to a sealed pre-fix ledger.

The target application may naturally create runtime state while being exercised. That is allowed when it is part of the feature under audit. Do not mutate source/config merely to make the audit possible.

## Default audit workspace

Use a project-local workspace unless the user specifies another location:

```text
.runtime-feature-audit/
├── audit-plan.json
├── feature-inventory.json      # SYSTEM mode
├── attempts.jsonl
├── hypothesis-ledger.jsonl     # persistent investigation; hash-chained
├── evidence/
├── tracked-source-baseline.json
├── feature-matrix.md
├── system-coverage.md          # SYSTEM mode
├── .active.json                # audit lifecycle marker; created by init
├── .report-state.json          # fingerprints canonical state used for generated reports
├── .complete.json              # written only after a successful final gate
└── audit-report.md
```

Do not put audit probes into the project's own test directories. `auditctl init` also snapshots hashes of Git-tracked files when Git is available; the final gate rejects tracked source/test mutation introduced during the audit.

## Deterministic controller

Use the bundled `scripts/auditctl.py` rather than manually inventing log formats or completion checks. Bundled script paths are relative to the skill root. Keep the **target project root explicit** because skill scripts may execute from the skill directory rather than the project directory.

In the commands below, `<project-root>` means the absolute target-project root. Always use an audit directory under that project. Examples use `python3` on POSIX; on default Windows Python installs substitute `py -3`.

```bash
python3 scripts/auditctl.py --help
# Windows: py -3 scripts/auditctl.py --help
```

The controller has these mandatory phases:

```text
init -> build plan -> validate-plan
     -> attempt-start/attempt-batch START -> REAL RUNTIME ACTION -> attempt-finish/attempt-batch FINISH
     -> suspicious/ambiguous? hypothesis-open/update -> information-gaining next probe
     -> repeat until hypothesis terminal + required matrix complete
     -> report -> gate --require-report
```

If the gate exits nonzero, continue the audit or report a genuine blocker. Do not work around the gate.

## Audit modes — requested scope is binding

Choose the mode from the user's request:

- `FEATURE` — one named feature or an explicitly bounded set of features.
- `SYSTEM` — all/every features, whole-project, whole-system, full feature-reality audit, or equivalent exhaustive wording.

**Never silently downgrade `SYSTEM` to a representative sample.** Auditing one "important" feature, one subsystem, or a handful of examples does not satisfy a whole-project request.

For `SYSTEM`, first read [references/SYSTEM-AUDIT.md](references/SYSTEM-AUDIT.md). Build `.runtime-feature-audit/feature-inventory.json` from multiple discovery surfaces and map every `IN_SCOPE` inventory item into `audit-plan.json`. The deterministic gate rejects unmapped in-scope items.

The feature universe is runtime/product oriented: user-facing capabilities, public APIs/CLI/SDK operations, workflows, jobs/events that are product behavior, and public functions when a library/SDK exposes functions as its supported interface. Do not inflate the inventory with private helpers merely because they exist in source. If the user explicitly asks to audit every exported/public function, include those functions as inventory items.

### Plan Mode recommendation for whole-system audits

`SYSTEM` audits are inherently multi-phase. Use the host's strongest planning mode before execution when available.

For current Codex CLI, **prefer `/plan` first** for a whole-project/system audit. While in Plan Mode, perform read-only discovery and produce a decision-complete audit strategy: system boundary, startup path, discovery sources, candidate inventory, execution batches, dependencies/credentials, and likely blockers. Do not run the falsification probes while still in Plan Mode. After the plan is approved / Plan Mode ends, initialize the deterministic `SYSTEM` audit, materialize the inventory and probe plan, validate, then execute.

If dedicated Plan Mode is unavailable, use the host's planning/TODO mechanism and perform the same discovery/planning phase before runtime execution. Do not require this overhead for a simple single-feature `FEATURE` audit.

When running under Google Antigravity 2.0, read [references/GOOGLE-ANTIGRAVITY.md](references/GOOGLE-ANTIGRAVITY.md). When running under Antigravity CLI (`agy`), also read [references/ANTIGRAVITY-CLI.md](references/ANTIGRAVITY-CLI.md); use its native plan execution mode for `SYSTEM` discovery/planning before probes. Gemini CLI uses the same portable skill methodology but is a separate host integration.

## Workflow checklist

### Phase 1 — Establish contract

- [ ] Identify the feature claim from sources in this priority order:
  1. explicit user requirement / acceptance criteria,
  2. product/API documentation,
  3. UI copy / CLI help / public schema,
  4. implementation-facing docs/config,
  5. existing tests only as last-resort claim discovery.
- [ ] Record real entry point(s), preconditions, accepted inputs/limits, expected outputs, expected end effects, rejection behavior, and dependencies.
- [ ] Record `target.runtime_identity_expectation`: what process/server/build/container command should actually be serving the product.
- [ ] Identify collision surfaces that could make the audit race with tests or other live consumers.
- [ ] Mark uncertain obligations `CONTRACT_UNKNOWN`; do not invent support requirements.

Read [references/FALSIFICATION-METHOD.md](references/FALSIFICATION-METHOD.md) before planning a non-trivial feature and [references/AUDIT-ENVIRONMENT.md](references/AUDIT-ENVIRONMENT.md) for runtime identity/isolation preflight.

### Phase 2 — Create and validate the falsification plan

Initialize once:

```bash
python3 scripts/auditctl.py init \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --target-root "<project-root>" \
  --project-name "<project>" \
  --environment "<local-or-authorized-staging>" \
  --scope "<requested scope>" \
  --mode <feature|system>
```

Populate `<project-root>/.runtime-feature-audit/audit-plan.json` using [assets/audit-plan.schema.json](assets/audit-plan.schema.json).

For `SYSTEM` mode, first populate `feature-inventory.json` using [assets/feature-inventory.schema.json](assets/feature-inventory.schema.json). Every `IN_SCOPE` inventory ID must map 1:1 to a planned feature ID. Do not mark a discovered feature `EXCLUDED` merely to shorten the audit.

For every feature declare these shape flags because the gate uses them to require relevant probe families:

- `input_sensitive`: behavior materially depends on input values/types,
- `stateful`: behavior claims a state transition, persistence, creation, update, deletion, delivery, or downstream result,
- `dependency_sensitive`: behavior depends on a real external/internal provider whose authenticity matters.

The plan must set both `target.startup_path` and `target.runtime_identity_expectation`, and contain three required `ENVIRONMENT` probes: `environment_start`, `runtime_identity`, and `environment_collision`. `runtime_identity` must establish what executable/server/container entrypoint is actually serving the target; `environment_collision` must examine applicable shared mutable resources before ordinary feature probes.

Then validate:

```bash
python3 scripts/auditctl.py validate-plan --audit-dir "<project-root>/.runtime-feature-audit"
```

Do not execute feature probes until this passes.

### Phase 3 — Start the product as intended

Use the documented local/development/authorized staging startup path. Do not substitute mocks.

Preflight sequence:

1. `environment_start` — start/reach the target runtime and record the reproducible startup command/path. It must `SURVIVED` before later preflight.
2. `runtime_identity` — verify the running process/container/server matches `target.runtime_identity_expectation` using runtime evidence such as process command line, container entrypoint/CMD, server signature, build/version endpoint, OpenAPI identity, executable path, or equivalent. A health endpoint alone is insufficient. Identity mismatch may be `FALSIFIED` (for example `PLACEHOLDER`) and should be preserved; later probes characterize the deployed runtime, not the intended source tree.
3. `environment_collision` — inspect applicable shared queues/topics, DB schemas/databases, buckets/prefixes, caches/namespaces, ports/processes, tenants/accounts, filesystem paths, and test fixtures. This probe must `SURVIVED` before ordinary feature probes; otherwise results may be contaminated.

All three preflight attempts require `--repro-command`. Features marked `dependency_sensitive: true` also require reproduction metadata on every attempt.

If credentials/dependencies are unavailable, record affected attempts `BLOCKED`. Do not silently downgrade to a fake provider.

### Phase 4 — Execute probes with two-phase logging

**Before the runtime action:**

```bash
python3 scripts/auditctl.py attempt-start \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --feature-id <feature-id> \
  --probe-id <probe-id> \
  --action "<exact user-visible action>" \
  --repro-command "<command if applicable>"
```

Capture the returned `attempt_id`.

Then perform the actual UI/API/CLI/runtime action.

**Immediately afterward:**

```bash
python3 scripts/auditctl.py attempt-finish \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --attempt-id <attempt-id> \
  --observed "<what actually happened>" \
  --side-effect-check "<downstream/state observation>" \
  --evidence <relative-evidence-path> \
  --result <SURVIVED|FALSIFIED|BLOCKED|INCONCLUSIVE> \
  --failure-pattern <pattern> \
  --confidence <HIGH|MEDIUM|LOW>
```

If a probe crashes or times out, still finish it as `BLOCKED` or `INCONCLUSIVE` with the observed failure. An open `STARTED` event causes the final gate to fail.

For large `SYSTEM` audits, prefer **two-phase batch logging** instead of hundreds of per-probe CLI calls. Prepare one JSON document for a group of `STARTED` events, register them atomically, execute those already-registered probes (sequentially or in parallel), then ingest one `FINISH` batch.

```bash
python3 scripts/auditctl.py attempt-batch \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --input starts.json

# execute the registered runtime probes

python3 scripts/auditctl.py attempt-batch \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --input finishes.json
```

Batch documents use [assets/attempt-batch.schema.json](assets/attempt-batch.schema.json). `phase` is `START` or `FINISH`. The controller validates the whole batch first and serializes all accepted events under one cross-platform lock, preserving the same hash chain as single-attempt commands. **Never batch-fake observations:** batching reduces logging ceremony; it does not permit logging actions that were not actually executed.

Parallel execution is allowed only after each probe has a canonical `STARTED` event. Workers must use unique evidence filenames and must not write `attempts.jsonl` or `hypothesis-ledger.jsonl` directly.

Read [references/ATTEMPT-LOGGING.md](references/ATTEMPT-LOGGING.md) before the first execution attempt.

### Phase 5 — Persist on suspicious, ambiguous, or intermittent behavior

When an attempt produces a failure or suspicious result that has multiple plausible explanations, do not stop at the first interpretation. Read [references/PERSISTENT-INVESTIGATION.md](references/PERSISTENT-INVESTIGATION.md) and open a bounded hypothesis.

```bash
python3 scripts/auditctl.py hypothesis-open \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --feature-id <feature-id> \
  --statement "<falsifiable explanation>" \
  --trigger-attempt-id <attempt-id> \
  --next-probe "<next information-gaining probe>" \
  --max-attempts 6
```

Attach subsequent attempts to the hypothesis. If the same planned probe is repeated, state what changed or why an unchanged retry is informative:

```bash
python3 scripts/auditctl.py attempt-start \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --feature-id <feature-id> \
  --probe-id <probe-id> \
  --hypothesis-id <hypothesis-id> \
  --changed-variable "<actual information-bearing change>" \
  --escalation-stage <0-11>

For intermittent behavior where an identical retry is itself the experiment, use `--retry-reason` instead of inventing a fake changed variable. Update the hypothesis after each informative step and resolve it to `REFUTED`, `CONFIRMED`, `BLOCKED`, or `BUDGET_EXHAUSTED`. `OPEN` and `SUPPORTED` hypotheses block final completion.

Persistence means **changing the investigation strategy**, not changing the target implementation. A failed probe is evidence that chooses the next probe.

### Phase 6 — Prefer probes that expose superficial implementations

For non-trivial applicable features, prefer these lenses:

1. `baseline_valid` — canonical claimed use.
2. `valid_variation` / `format_variation` — different legitimate inputs.
3. `causal_sensitivity` — alter meaningful input and verify output/state responds; primary hardcode detector.
4. `invalid_type_or_shape` / `corrupt_input` — excluded data should reject safely, not fake success.
5. `boundary_*` — empty/min/max/just-outside documented limits.
6. `state_transition` — compare state before/after.
7. `round_trip` — create/upload/save, then retrieve/use/download through another real path.
8. `persistence` — verify documented lifecycle survival.
9. `repeat_or_duplicate` — retry, duplicate, idempotency, repeated action.
10. `dependency_authenticity` — distinguish intended provider from mock/demo adapter.
11. `workflow_completion` — follow the feature to its advertised end result.
12. `hardcode_discrimination`, `mock_discrimination`, `bttlp_discrimination` when earlier behavior is suspicious.

Do not mechanically run irrelevant lenses. The plan validator applies conditional coverage based on feature shape.

For file/upload features, read [references/INPUT-MATRICES.md](references/INPUT-MATRICES.md).

### Phase 7 — Falsify first; classify second

When runtime behavior produces a counterexample, inspect only enough source/config/logging afterward to distinguish the implementation pattern.

Runtime counterexample first:

```text
input -> real entry point -> observed output -> required effect absent/wrong
```

Then classification evidence:

```text
TODO | PLACEHOLDER | FAKE_NOOP | BTTLP | HARDCODED | MOCK_ONLY
| PARTIAL_IMPLEMENTATION | REAL_BUT_BROKEN | UNKNOWN_PATTERN
```

Do not infer `HARDCODED`, `MOCK_ONLY`, or `TODO` from source appearance alone.

Read [references/EVIDENCE-AND-VERDICTS.md](references/EVIDENCE-AND-VERDICTS.md) before assigning final labels.

### Phase 8 — Generate report and pass final gate

Generate machine-grounded outputs from the plan and append-only log:

```bash
python3 scripts/auditctl.py report --audit-dir "<project-root>/.runtime-feature-audit"
```

Then run the completion/evidence gate:

```bash
python3 scripts/auditctl.py gate \
  --audit-dir "<project-root>/.runtime-feature-audit" \
  --require-report
```

**Only finish when this command exits 0.** A completed audit can contain many `FALSIFIED` findings. The gate checks integrity and completeness, not feature health. It also rejects reports generated before the latest attempt, hypothesis update, plan change, or SYSTEM inventory change; regenerate the report whenever canonical audit state changes.

If you write a separate retrospective or human-facing summary, run `auditctl terminology-check --input <file>` before publishing it. This catches common verdict inflation such as `Run 2 (Verified)` or `Verified Reality` when the machine verdict is only `NOT_FALSIFIED`.

After a sealed audit is followed by remediation, **do not reuse the sealed workspace**. Use a new directory such as `.runtime-feature-audit-run2`; in Antigravity/other subagent harnesses, spawn a fresh auditor instance rather than messaging an idle/completed auditor.

## Verdict semantics

These definitions are normative. References may illustrate them but must not redefine them. Use runtime verdict and implementation pattern independently.

Runtime verdict:

- `FALSIFIED` — reproducible counterexample to a claimed obligation.
- `NOT_FALSIFIED` — all required planned probes completed without a counterexample; never rewrite this as “verified”, “proven”, “alive”, “fully working”, “confirmed working”, “verified reality”, or an equivalent success verdict.
- `BLOCKED` — required real environment/dependency/access unavailable.
- `INCONCLUSIVE` — evidence or contract is too ambiguous.

Per-attempt `SURVIVED` means only that one probe did not falsify its proposition.

## Gotchas

- A correct rejection of an unsupported type is **not** a feature failure.
- An invalid input that gets a success response **is suspicious** when rejection is part of the claim.
- A UI control can be be-there-to-look-pretty (`BTTLP`) even when its click handler runs.
- A 2xx can be fake/no-op when persistence or downstream state never changes.
- Different inputs returning the same result are not automatically hardcoded; require a contract-implied causal relationship.
- Seeded/demo data is not proof of newly created state.
- A restart test is meaningful only when persistence across that lifecycle is actually claimed.
- Source inspection is useful for classification, not as a substitute for runtime execution.
- Do not weaken or delete a planned required probe because earlier probes failed.
- Do not retry an already-attempted probe unchanged unless reproduction of nondeterminism is the stated experiment.
- Do not leave a serious failure hypothesis `OPEN`/`SUPPORTED` just because later work is inconvenient.
- Do not repair the implementation and then report the repaired state as the audited baseline.
- A healthy container, open port, or `/healthz` response does not establish runtime identity; verify what executable/application is actually serving it.
- Shared RabbitMQ/Kafka queues, DB schemas, buckets, Redis namespaces, or tenants can make a live worker race with a test/audit consumer; isolate or block the audit before interpreting results.
- After remediation, start a fresh audit run and auditor instance; do not merge pre-fix and post-fix evidence into one sealed ledger.

## Upload-image minimum pattern

When the contract claims image upload, do not stop at one JPEG/PNG.

Classify candidate inputs against the actual contract, then cover applicable families:

- accepted image variants: JPEG/JPG, PNG, GIF, WebP, TIFF/TIF, SVG, EPS, or product-specific formats,
- structural variants: size/dimensions/aspect ratio, transparency/animation/metadata, extension case, spaces/unicode,
- MIME/extension mismatches and renamed non-image content,
- representative non-images: TXT, JSON, source code, PDF, DOCX, PPTX, ZIP,
- empty/truncated/corrupt files,
- two or more materially distinct valid images for causal-sensitivity testing,
- upload -> list/read/download/render round trip,
- persistence/backing-state observation when claimed.


## Completion invariant

Before returning a final audit result, all of the following must be true:

- the plan validates,
- in `SYSTEM` mode, the feature inventory validates and every discovered `IN_SCOPE` item maps to the plan,
- in `SYSTEM` mode, meaningful cross-feature workflows are inventoried/planned when applicable,
- the three preflight probes are complete: startup health `SURVIVED`, runtime identity was observed against the declared expectation, and environment collision/isolation `SURVIVED`; preflight/dependency-sensitive attempts carry reproduction metadata,
- every required probe has a terminal attempt,
- no attempt remains open,
- bundled Draft 7 JSON schemas are enforced by the vendored standards-compliant `fastjsonschema` engine, and RFF semantic validators accept the canonical plan/inventory/events,
- the attempt and hypothesis hash chains validate,
- no hypothesis remains `OPEN` or `SUPPORTED`,
- falsified findings have effect/evidence corroboration,
- survived stateful probes have downstream evidence where required,
- reports were generated from the latest canonical plan/log/hypothesis/inventory state (report freshness gate passes),
- Git-tracked source/test files are unchanged from the audit baseline when that gate is available,
- `auditctl gate --require-report` exits 0.

If any item is false, the audit is incomplete rather than successful.
