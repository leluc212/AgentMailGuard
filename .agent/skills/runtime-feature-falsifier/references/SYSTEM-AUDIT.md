# Whole-Project / System Audit Mode

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

Use this workflow when the user asks to audit **all**, **every**, the **whole project**, the **whole system**, or otherwise requests exhaustive project-level runtime feature falsification.

## Core scope rule

A system audit is not a representative sample.

Do not stop after auditing one feature, one subsystem, one happy path, or a few "important" examples. The audit may finish only when every capability in the declared discovered feature universe is either:

1. `IN_SCOPE` and mapped to a planned feature with completed required probes, or
2. explicitly `EXCLUDED` with a concrete scope reason, or
3. `IN_SCOPE` but its required runtime probes terminate as `BLOCKED` / `INCONCLUSIVE` with evidence.

`auditctl gate` enforces the inventory-to-plan mapping. It cannot prove that discovery found literally every unknown feature, so the report must describe the **discovered feature universe** and discovery sources rather than claiming mathematical completeness.

## Recommended Plan Mode workflow

Whole-system audits are long, multi-phase tasks. Use the host's strongest planning mode before execution when available.

### Codex

Prefer starting with `/plan` for a whole-project audit. During Plan Mode:

- inspect the repository read-only,
- identify runtime surfaces and claimed capability families,
- establish how the application starts,
- enumerate discovery sources,
- propose the feature inventory and audit phases,
- identify required credentials/dependencies and likely blockers,
- decide how to partition the execution into coherent batches.

Do **not** execute the runtime falsification matrix while still in Plan Mode. After the plan is approved / Plan Mode ends, create `.runtime-feature-audit/`, materialize the inventory and audit plan, validate them, then execute.

If Plan Mode is unavailable, use the host's planning/TODO facility and perform the same read-only discovery phase before runtime execution.

## Phase S0 — Define system boundary

Record:

- project/system name,
- environment,
- included applications/services/packages,
- user roles/personas where relevant,
- interfaces considered public/user-facing,
- explicit exclusions from the user,
- authorized external dependencies.

For an unqualified "audit the whole project" request, default to every claimed user-facing/public runtime capability reachable from the checked-out project. Do not silently narrow to one package or subsystem.

## Phase S1 — Discover the feature universe

Use multiple independent discovery sources. Source inspection is allowed here for **claim discovery**, not runtime proof.

Useful sources include:

- user requirements / acceptance criteria,
- README/product documentation,
- UI navigation, routes, menus, buttons, forms, pages,
- API/OpenAPI/GraphQL/RPC schemas and public routes,
- CLI `--help`, subcommands, documented flags,
- public SDK exports,
- background jobs/events/webhooks/schedulers that are product capabilities,
- feature flags/configuration that expose product capabilities,
- source route/command registration when documentation is incomplete,
- existing tests only as last-resort claim discovery.

Do not count internal helpers as separate product features merely because functions exist in source.

Use at least two materially independent discovery sources in `SYSTEM` mode when the repository provides them. If discovery is constrained, record the blocked discovery source instead of pretending coverage is exhaustive.

## Phase S2 — Normalize and inventory

Create `.runtime-feature-audit/feature-inventory.json`.

Each discovered item should represent a falsifiable runtime capability or workflow, not an implementation symbol. Give it:

- stable `feature_id`,
- `kind`: `CAPABILITY`, `FUNCTION`, `WORKFLOW`, or `SYSTEM`,
- concise claim,
- claim/discovery sources,
- expected runtime surface(s),
- disposition: `IN_SCOPE` or `EXCLUDED`,
- `exclusion_basis` plus a concrete exclusion reason when excluded.

Deduplicate aliases. Example: a UI "Upload image" button and `POST /images` may be two entry points for one capability rather than two features.

For an "audit everything" request, do not use `EXCLUDED` merely to reduce work. Use only `USER_SCOPE`, `NOT_RUNTIME_CAPABILITY`, `DUPLICATE_ALIAS`, or `OUTSIDE_TARGET_SYSTEM` as the exclusion basis. A dependency/environment failure is **not** an exclusion: keep the feature `IN_SCOPE` and terminate the affected probe as `BLOCKED`.

Set inventory `discovery_status`:

- `COMPLETE` when the declared discovery pass is complete,
- `PARTIAL_BLOCKED` when part of feature discovery cannot be completed because a required source/environment is unavailable; add blockers.

## Phase S3 — Build system coverage plan

Every `IN_SCOPE` inventory feature must appear in `audit-plan.json` with the same `feature_id`.

In addition to per-feature probes, model meaningful end-to-end workflows as inventory features of `kind=WORKFLOW`, for example:

- register -> login -> upload -> retrieve -> delete,
- create record -> search -> update -> export,
- ingest event -> process -> persist -> expose result,
- CLI create -> list -> inspect -> remove.

If multiple capabilities form a user-visible workflow but the plan audits them only in isolation, the system audit is incomplete.

Use `workflow_coverage.applicable=true` when cross-feature workflows matter and list at least one workflow item. If they genuinely do not apply, set `applicable=false` with a reason.

## Phase S4 — Execute in batches, not samples

Partition the inventory by surface/subsystem to control context, for example:

1. startup/health and navigation,
2. authentication/authorization,
3. CRUD/stateful domain capabilities,
4. file/import/export capabilities,
5. search/query/filter capabilities,
6. integrations/dependencies,
7. background/event workflows,
8. cross-feature end-to-end workflows.

A batch boundary is an execution-management device, not permission to omit later batches.

For each feature, apply its own conditional probe matrix. Do not force file-upload probes onto unrelated functionality.

## Phase S5 — Reconcile inventory before reporting

Before final report:

- compare inventory `IN_SCOPE` IDs with plan IDs,
- ensure no discovered in-scope feature is unmapped,
- ensure all required probes have terminal attempts,
- ensure exclusions have reasons,
- ensure cross-feature workflows are represented when applicable,
- regenerate `system-coverage.md`, `feature-matrix.md`, and `audit-report.md`,
- run the final gate.

The system report should state:

- discovered feature count,
- in-scope count,
- excluded count,
- planned count,
- unmapped count,
- completed runtime verdict counts,
- discovery status/blockers,
- system/workflow counterexamples.

Never summarize a system audit as "all features work". Use the falsification verdicts and exact discovered scope.