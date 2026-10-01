# Restore a Green Lint Baseline (RA.14) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `make ci` (`fmt-check lint test-unit test-integration`) passes on the whole repository, so the tasks held at `[~]` only by "CI green" (RA.1–RA.13, 4.11, 4.12) can close as `[x]`.

**Architecture:** There are three independent failures, each fixed at its cause.
- **14 mypy errors** in 6 test files: each has a precise fix in the test, with no `# type: ignore` and no weakened assertion.
- **9 Markdown documents** that ruff 0.16 formats by default: they are taken out of the formatter's scope, because their code blocks are often paste-in fragments that formatting corrupts.
- **31 Python files:** reformatted in one formatting-only change, which the full test suites then prove is behaviour-neutral.

A close-out task runs `make ci` and only then updates `specs/tasks.md`.

**Tech Stack:** Python 3.12, uv, ruff 0.16.7 (formatter + linter), mypy strict with the pydantic plugin, pytest 9.

**Spec:** `specs/tasks.md` RA.14. The target is "Done when: `make ci` (`fmt-check lint test-unit test-integration`) passes. Then flip RA.1–RA.13 to `[x]`". Also `specs/requirements.md` §0.3 Definition of Done #6 ("CI green") and R24.2 ("enforce formatting, linting, and static type checking in CI"). Evidence: audit finding A09 in `artifacts/superpowers/2026-09-26-audit-register.md`. Tool behaviour was verified against ruff's own docs (`docs/formatter.md`, `docs/configuration.md`, 2026-09-27): Markdown formatting is on by default, and the documented opt-out is an exclude pattern.

## Global Constraints

- Measured baseline on 2026-09-27: `uv run mypy packages services tests evaluation` gives `Found 14 errors in 6 files`; `uv run ruff format --check .` gives `40 files would be reformatted`; `uv run ruff check .` is clean. Unit 1260 passed; integration 149 passed.
- **No behaviour changes.** Production code is touched only by `ruff format`. Type fixes live in tests, and each keeps the test asserting the same thing.
- No `# type: ignore`, no new mypy `exclude`, no loosening of `strict = true`, and no new ruff `ignore` rules. Fixing a check by switching it off defeats R24.2.
- Never run `make fmt`: it also runs `ruff check --fix`, which can rewrite code beyond formatting. Use `uv run ruff format <explicit files>` only.
- pytest `addopts` already has `-q`; never add another. Integration tests are isolated to `rag_email_test` by `tests/integration/conftest.py`. The live dev stack is not touched.
- **Standing user instruction:** no commits until Phase 4 is complete, so each Commit step becomes a controller snapshot. When the user commits, the Task 3 formatting change should be its own commit and should be added to `.git-blame-ignore-revs` (Task 4 Step 4 explains how).

## Review Focus

1. **Reformatting silently changes behaviour** (for example an implicit string concatenation reflowed, or a comment moved into a different branch). Expected: none; ruff's formatter is AST-preserving. Pinned by Task 3 Step 4: full unit and integration suites plus `ruff check .` after formatting, and an AST-equality check over all 31 files.
2. **A type fix weakens a test** (a `cast` that hides a real mismatch, or a helper that stops failing when the metric is absent). Expected: each fixed test still fails for its original reason. Pinned by Task 1 Step 5: a deliberate break for each fixed helper.
3. **Markdown exclusion hides real Python.** Expected: only `*.md` leaves the formatter's scope. Every `.py` and `.pyi` file is still formatted, and `ruff check` scope is unchanged. Pinned by Task 2 Step 4.
4. **`make ci` passes locally but not in GitHub CI** (different commands). Expected: the CI workflow runs the same four checks plus the OpenAPI check. Pinned by Task 4 Step 2, which runs the CI workflow's exact commands.
5. **Statuses flip before the evidence exists.** Expected: `[x]` only after a green `make ci` run in the same task, with its numbers written into the gate note. Pinned by Task 4's step order.

---

## Design decisions

- **D1. Fix types in the tests, where they are wrong.** The 14 errors fall into four kinds, each with a precise fix:
  - A `UUID | str` value passed where `UUID | None` is required: convert with `UUID(str(...))`.
  - Duck-typed metrics doubles passed as `PipelineMetrics`: wrap them in `typing.cast`. The production code is deliberately defensive about partial metrics objects, and the doubles exist to test exactly that.
  - Helpers returning `Any` from `sample.value`: wrap the value in `float(...)`.
  - An attribute assigned on a value typed `AbstractIncomingMessage`: set it through the mock factory instead.
- **D2. Exclude Markdown from the formatter, not from ruff.** `[tool.ruff.format] exclude = ["*.md"]` follows ruff's documented format-scoped exclude. Ruff never lints Markdown, and this setting leaves Python formatting untouched. Formatting prose documents' code blocks corrupts intentional fragments. Two formatting passes over plan files today had to fence fragments as `text` to stop ruff rewriting `name=Counter(...)` into `name = (Counter(...),)`.
- **D3. One formatting-only change.** The 31 files are reformatted with nothing else in the change, so a reviewer can verify it mechanically and `git blame` can skip it later.
- **D4. Close out on evidence.** RA.1–RA.13 and RA.14 flip to `[x]`. 4.11 and 4.12 also flip, because their only stated blocker for `[x]` is RA.14. Their other "Left" bullets name work owned by 4.13 and 7.x. 4.9 stays `[~]`, because its own retry/DLQ hop is still open.

## Not in this plan

- A pre-commit hook or editor settings to keep the baseline green. CI already fails on regressions. A pre-commit config is a separate, optional convenience.
- The 4.10 `draft_citation_mismatch` log call that loses its fields (noted in the 4.12 plan).
- Any change to CI triggers (`.github/workflows/ci.yml`).

## File structure

| File | Change |
|---|---|
| `tests/unit/test_thread_context_assembly.py`, `tests/integration/test_thread_context_assembly_postgres.py` | `UUID(str(...))` for `summarized_through_message_id` |
| `tests/unit/test_draft_repair_orchestration.py`, `tests/unit/test_citation_verification_generation.py` | `cast(PipelineMetrics, …)` at 5 call sites |
| `tests/unit/test_queue_metrics.py`, `tests/integration/test_queue_metrics_integration.py` | `float(sample.value)`; `routing_key` parameter on the mock factory |
| `pyproject.toml` | `[tool.ruff.format] exclude = ["*.md"]` |
| 31 Python files (Task 3 list) | `ruff format` only |
| `specs/tasks.md` | statuses and gate evidence |

---

### Task 1: Fix the 14 mypy errors in tests

**Files:**
- Modify: `tests/unit/test_thread_context_assembly.py:133`, `tests/integration/test_thread_context_assembly_postgres.py:177`
- Modify: `tests/unit/test_draft_repair_orchestration.py:488,507,760`, `tests/unit/test_citation_verification_generation.py:264,282`
- Modify: `tests/unit/test_queue_metrics.py` (helpers ~lines 80-114, `_make_mock_message` line 65, line 176), `tests/integration/test_queue_metrics_integration.py` (helpers ~lines 64-96)

**Interfaces:**
- Produces: `_make_mock_message(envelope: JobEnvelope, routing_key: str = "test.queue") -> AbstractIncomingMessage` in `tests/unit/test_queue_metrics.py`.

- [ ] **Step 1: Record the failing check (RED)**

Run: `uv run mypy packages services tests evaluation 2>&1 | tail -15`
Expected: the 14 errors listed in this plan's Global Constraints baseline, ending `Found 14 errors in 6 files`.

- [ ] **Step 2: Thread-state message id (2 errors)**

`ThreadState.summarized_through_message_id` is `UUID | None`, while `NormalizedMessage.message_id` is `UUID | str`. These tests build their messages with `uuid4()`, so the conversion is exact.

In `tests/unit/test_thread_context_assembly.py`, line ~133, change:

```text
        summarized_through_message_id=messages[-1].message_id,
```

to:

```text
        summarized_through_message_id=UUID(str(messages[-1].message_id)),
```

In `tests/integration/test_thread_context_assembly_postgres.py`, line ~177, change `summarized_through_message_id=hist_messages[-1].message_id,` to `summarized_through_message_id=uuid.UUID(str(hist_messages[-1].message_id)),`. That file uses `import uuid` (module style), so no import change is needed. The unit file already imports `UUID`.

- [ ] **Step 3: Duck-typed metrics doubles (5 errors)**

`SinglePassGenerator(metrics=…)` is typed `PipelineMetrics | None`. These doubles wrap a real `PipelineMetrics` and replace one collector with one that raises, to prove telemetry never fails a job. Tell the type checker what the test intends.

In `tests/unit/test_draft_repair_orchestration.py`, add `from typing import cast` if absent. Ensure `PipelineMetrics` is imported from `packages.observability.metrics` next to `create_pipeline_metrics`. At each of the three call sites (lines ~488, ~507, ~760), change:

```text
        metrics=metrics,
```

to:

```text
        metrics=cast(PipelineMetrics, metrics),
```

Change only the `SinglePassGenerator(...)` calls whose `metrics` is a `_MetricsWithBrokenCounter`. Read each site; do not touch calls that pass a real `PipelineMetrics`.

In `tests/unit/test_citation_verification_generation.py`, do the same at lines ~264 and ~282, where the variable holds a `_MetricsWithBrokenVerifiedCounter` or `_MetricsWithBrokenMismatchCounter`. Add `cast` to the `typing` import, and import `PipelineMetrics` from `packages.observability.metrics`.

- [ ] **Step 4: Metric-reading helpers and the mock message (7 errors)**

In `tests/unit/test_queue_metrics.py` and `tests/integration/test_queue_metrics_integration.py`, every helper `_get_histogram_sum`, `_get_histogram_count` and `_get_gauge_value` ends its inner loop with `return sample.value`. Change each of those lines to:

```text
                    return float(sample.value)
```

That is 3 lines per file. The fallback `return 0.0` / `return -1.0` lines stay as they are.

In `tests/unit/test_queue_metrics.py`, give the mock factory a parameter. Change:

```text
def _make_mock_message(envelope: JobEnvelope) -> AbstractIncomingMessage:
```

to:

```text
def _make_mock_message(
    envelope: JobEnvelope, routing_key: str = "test.queue"
) -> AbstractIncomingMessage:
```

In its body, change `msg.routing_key = "test.queue"` to `msg.routing_key = routing_key`. At line ~175, replace the two lines:

```text
    mock_msg = _make_mock_message(envelope)
    mock_msg.routing_key = "email.billing.priority"
```

with:

```text
    mock_msg = _make_mock_message(envelope, routing_key="email.billing.priority")
```

- [ ] **Step 5: Prove the tests still bite, then GREEN**

Run: `uv run mypy packages services tests evaluation 2>&1 | tail -1`
Expected: `Success: no issues found in <N> source files`.

Run: `uv run pytest tests/unit/test_thread_context_assembly.py tests/unit/test_draft_repair_orchestration.py tests/unit/test_citation_verification_generation.py tests/unit/test_queue_metrics.py && uv run pytest tests/integration/test_thread_context_assembly_postgres.py tests/integration/test_queue_metrics_integration.py`
Expected: all pass, with the same counts as before this task.

Review Focus 2 check: temporarily change the `float(sample.value)` helpers in `tests/unit/test_queue_metrics.py` so they look up a misspelled metric name, for example `metric_name + "_nope"`. Run `uv run pytest tests/unit/test_queue_metrics.py`, and expect failures: the helpers fall back to `0.0` / `-1.0` and the assertions notice. Revert. Then temporarily make the metrics doubles' failing collector in `tests/unit/test_draft_repair_orchestration.py` (`_ExplodingCounter`) stop raising, and confirm that `uv run pytest tests/unit/test_draft_repair_orchestration.py -k metric_failure` still passes. The `cast` changed only the type checker's view, not what the doubles do. Revert, and record both observations in the report.

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff check tests && uv run ruff format --check tests/unit/test_thread_context_assembly.py tests/integration/test_thread_context_assembly_postgres.py tests/unit/test_draft_repair_orchestration.py tests/unit/test_citation_verification_generation.py`
Expected: clean. `tests/unit/test_queue_metrics.py` and `tests/integration/test_queue_metrics_integration.py` are on the formatter list and are formatted in Task 3, not here.

```bash
git add tests/unit/test_thread_context_assembly.py tests/integration/test_thread_context_assembly_postgres.py \
  tests/unit/test_draft_repair_orchestration.py tests/unit/test_citation_verification_generation.py \
  tests/unit/test_queue_metrics.py tests/integration/test_queue_metrics_integration.py
git commit -m "test(types): fix the 14 strict-mypy errors in test files without ignores [task RA.14] [R24.2]"
```

---

### Task 2: Keep Markdown out of the formatter

**Files:**
- Modify: `pyproject.toml` (`[tool.ruff]` section, lines ~48-62)

**Interfaces:**
- Produces: `[tool.ruff.format] exclude = ["*.md"]`.

- [ ] **Step 1: Record the failing check (RED)**

Run: `uv run ruff format --check . 2>&1 | grep -E "^ +--> " | sed -E 's/ +--> //; s/:[0-9]+:[0-9]+$//' | sort -u | grep -c "\.md$"`
Expected: `9`.

- [ ] **Step 2: Scope the formatter**

In `pyproject.toml`, directly after the `[tool.ruff]` table (after its `exclude = [...]` list) and before `[tool.ruff.lint]`, add:

```toml
[tool.ruff.format]
# Markdown documents (plans, specs, audits) often hold paste-in code fragments that the
# formatter would rewrite into different code; ruff formats *.md by default since 0.16.
exclude = ["*.md"]
```

- [ ] **Step 3: GREEN**

Run: `uv run ruff format --check . 2>&1 | tail -1`
Expected: `31 files would be reformatted, …`. Only the Python files remain.

- [ ] **Step 4: Prove the scope is narrow (Review Focus 3)**

Run: `uv run ruff format --check . 2>&1 | grep -E "^ +--> " | sed -E 's/ +--> //; s/:[0-9]+:[0-9]+$//' | sort -u | grep -vc "\.py$"`
Expected: `0`. No non-Python file is flagged.

Run: `printf 'x=1\n' > /tmp/ruff_scope_probe.py && uv run ruff format --check /tmp/ruff_scope_probe.py; echo "exit $?"; rm -f /tmp/ruff_scope_probe.py`
Expected: ruff marks the probe file as unformatted (a block beginning `unformatted: File would be reformatted`) and prints `exit 1`. Python files are still formatted.

Run: `uv run ruff check .`
Expected: `All checks passed!`, which shows lint scope is unchanged.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build(ruff): keep Markdown documents out of the formatter [task RA.14] [R24.2]"
```

---

### Task 3: Formatting-only pass over the 31 Python files

**Files:** exactly these 31, and nothing else.

```text
packages/broker/queue_monitor.py packages/context/assembly.py packages/context/builder.py
packages/context/policy.py packages/db/thread_state.py packages/domain/knowledge.py
packages/knowledge/chunker.py packages/knowledge/embedder.py packages/knowledge/parsers/pdf.py
packages/retrieval/fake.py packages/retrieval/postgres.py packages/retrieval/query_builder.py
packages/retrieval/rerank.py packages/retrieval/retriever.py packages/retrieval/rrf.py
services/api/dependencies.py tests/integration/test_job_timeline_and_replay_integration.py
tests/integration/test_queue_metrics_integration.py tests/integration/test_summarization_postgres.py
tests/stubs/__init__.py tests/unit/test_document_parsers.py tests/unit/test_embedding_service.py
tests/unit/test_job_timeline_and_replay.py tests/unit/test_queue_metrics.py tests/unit/test_rerank.py
tests/unit/test_retrieval_debug_api.py tests/unit/test_retrieval_metrics.py tests/unit/test_rrf.py
tests/unit/test_structural_chunker.py tests/unit/test_summarization_policy.py tests/unit/test_thread_state.py
```

- [ ] **Step 1: Confirm the list matches reality**

Run: `uv run ruff format --check . 2>&1 | grep -E "^ +--> " | sed -E 's/ +--> //; s/:[0-9]+:[0-9]+$//' | sort -u > /tmp/ra14_fmt_list.txt && wc -l < /tmp/ra14_fmt_list.txt`
Expected: `31`, and the file lists exactly the paths above. If a path differs (a file changed since this plan was measured), format the list the command prints, and name every difference in the report.

- [ ] **Step 2: Snapshot the ASTs before formatting**

Run:

```bash
uv run python - <<'EOF'
import ast, json, pathlib
paths = pathlib.Path("/tmp/ra14_fmt_list.txt").read_text().split()
dumps = {p: ast.dump(ast.parse(pathlib.Path(p).read_text())) for p in paths}
pathlib.Path("/tmp/ra14_ast_before.json").write_text(json.dumps(dumps))
print(len(dumps), "ASTs recorded")
EOF
```

Expected: `31 ASTs recorded`.

- [ ] **Step 3: Format exactly those files**

Run: `uv run ruff format $(cat /tmp/ra14_fmt_list.txt)`
Expected: `31 files reformatted`.

- [ ] **Step 4: Prove the change is behaviour-neutral (Review Focus 1)**

Run:

```bash
uv run python - <<'EOF'
import ast, json, pathlib
before = json.loads(pathlib.Path("/tmp/ra14_ast_before.json").read_text())
changed = [p for p, d in before.items() if ast.dump(ast.parse(pathlib.Path(p).read_text())) != d]
print("AST changes:", changed or "none")
raise SystemExit(1 if changed else 0)
EOF
```

Expected: `AST changes: none`. The formatter touched only layout. Comments are not in the AST, so also read the diff of any file where comments moved; `git diff --stat` narrows it down.

Run: `uv run ruff format --check . 2>&1 | tail -1 && uv run ruff check .`
Expected: `… files already formatted` with no "would be reformatted", then `All checks passed!`.

Run: `uv run pytest tests/unit && uv run pytest tests/integration`
Expected: unit 1260 passed, integration 149 passed (same counts as the baseline).

- [ ] **Step 5: Commit, formatting only**

```bash
git add $(cat /tmp/ra14_fmt_list.txt)
git commit -m "style: ruff format the 31 files left unformatted before RA.14 [task RA.14] [R24.2]"
```

---

### Task 4: `make ci` green, then close the statuses

**Files:**
- Modify: `specs/tasks.md` (RA status note ~line 378, RA.1–RA.14 checkboxes ~lines 380-424, gate evidence ~line 434, 4.11 ~line 505, 4.12 ~line 512)

- [ ] **Step 1: Run `make ci` exactly as defined**

Run: `make ci > /tmp/ra14_make_ci.log 2>&1; echo "exit $?"; grep -E "passed|failed|error|Success|All checks|already formatted" /tmp/ra14_make_ci.log | tail -8`
Expected: `exit 0`. The log shows the formatter check clean, `All checks passed!`, `Success: no issues found`, then the unit and integration pass lines. Record the unit and integration counts; the next steps use them.

- [ ] **Step 2: Run the GitHub CI workflow's own commands (Review Focus 4)**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy packages services tests evaluation && uv run python -m services.api.openapi --check && echo CI-STATIC-OK`
Expected: `CI-STATIC-OK`. These are the static steps of `.github/workflows/ci.yml`. The test steps are covered by Step 1.

- [ ] **Step 3: Update `specs/tasks.md`**

1. Change `- [~] **RA.1` through `- [~] **RA.13` to `- [x] …`, which is 13 lines, and `- [ ] **RA.14` to `- [x] **RA.14`.
2. Replace the status note that begins `> **Status (2026-09-27): RA.1–RA.13 held at \`[~]\`.**` with:

```markdown
> **Status (2026-09-27): RA.1–RA.14 done.** `make ci` is green (evidence below); RA.14 removed the pre-existing lint/format baseline that had held RA.1–RA.13 at `[~]`.
```

3. Append this sentence to the end of the `> **Gate evidence (2026-09-27):**` paragraph, with `<U>` and `<I>` replaced by the counts measured in Step 1:

```markdown
**CI green (2026-09-27, RA.14):** `make ci` → exit 0 (`ruff format --check .` clean with `*.md` out of the formatter's scope; `ruff check .` clean; strict mypy clean on packages, services, tests and evaluation; unit <U> passed; integration <I> passed).
```

4. Change `- [~] **4.11 Draft persistence**` and `- [~] **4.12 Generation metrics**` to `[x]`. In each task's `Left:` bullet, delete the clause `held at \`[~]\` until RA.14 turns \`make ci\` green (DoD #6).` for 4.11, and `held at \`[~]\` until RA.14 turns \`make ci\` green.` for 4.12. Keep the rest of each bullet, which names work owned by 4.13 and 7.x. Leave 4.9 at `[~]`.

Run: `grep -c "^- \[x\] \*\*RA\." specs/tasks.md && grep -n "4.11 Draft persistence\|4.12 Generation metrics\|4.9 Structured" specs/tasks.md && grep -c "held at \`\[~\]\` until RA.14" specs/tasks.md`
Expected: `14`; the 4.11 and 4.12 lines show `[x]`; 4.9 shows `[~]`; the last count is `0`.

- [ ] **Step 4: Blame-ignore note for the formatting commit**

When the user commits Task 3's change on its own, its commit hash should go into `.git-blame-ignore-revs` (`git config blame.ignoreRevsFile .git-blame-ignore-revs`), so `git blame` skips it. The hash does not exist during no-commit execution. Add this line to the report's hand-back instead of creating the file:

```text
After committing Task 3 alone: echo "<sha>  # ruff format baseline (RA.14)" >> .git-blame-ignore-revs
```

- [ ] **Step 5: Commit**

```bash
git add specs/tasks.md
git commit -m "docs(tasks): make ci green — close RA.1-RA.14, 4.11 and 4.12 [task RA.14] [R24.2]"
```

---

## Dry-run verification (2026-09-27)

All four tasks were applied verbatim in a scratch copy. Two wording defects were corrected above: the integration test's `uuid` import style, and ruff's literal "unformatted" message. Measured: mypy `Success: no issues found in 340 source files`; `ruff format --check .` reports all files formatted with Markdown out of scope; `ruff check .` clean; `make ci` exit 0 with unit 1260 and integration 149, the same as the baseline; the OpenAPI check passed; and the Task 4 greps matched. Both deliberate-break checks in Task 1 Step 5 behaved as described.

## Self-review record

1. **Spec coverage.**
   - RA.14 "done when `make ci` passes": Task 4 Step 1.
   - "Fix type errors at their cause (no blanket `# type: ignore`)": Task 1, one precise fix per error kind.
   - "formatting changes must not alter behaviour": Task 3 Step 4, with an AST check and both full suites.
   - "Decide whether `docs/**/*.md` belongs in ruff's scope": Task 2 and D2.
   - "Then flip RA.1–RA.13 to `[x]` and add the date": Task 4 Step 3.
   - R24.2: nothing is switched off.
2. **Placeholder scan.** No TBD. `<U>`, `<I>` and `<sha>` are values measured or created during execution, and each step says where they come from.
3. **Type consistency.** `_make_mock_message(envelope, routing_key="test.queue")` is defined and used in the same task. `cast(PipelineMetrics, …)` uses the existing `PipelineMetrics` import path.
4. **Review Focus.** Each of the five lines names its pinning step.
