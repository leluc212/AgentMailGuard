# Guard v3 — execution plan (re-grounded 2026-10-13, review amendments applied 2026-10-13)

Review amendments (plan_v3 Q1=b', R-h, R-i, Q7–Q10, design decisions 8–14) touch Tasks 4, 5, 7, 10,
11, 13, 15c and add Task 16 below; they are marked **[review]**. Tasks 1–2 (done) are unchanged.
Second review (2026-10-14, plan_v3 F1–F9) touches Tasks 4, 5, 7, 8, 15, 16 and sections (b), (d);
marked **[review 10-14]**.

Source of truth, in order: the owner's messages, `plan_v3/plan_v3.md` (decisions R-a…R-g, Q1–Q6,
D1, design decisions 1–8, task list 1–15), `docs/adr/0015-guard-architecture-v3.md` (Proposed),
`CLAUDE.md` (§3 DoD, §7 divergence protocol, never renumber requirement IDs). This file only
sequences that work and fixes the choices the plan left open. If something here contradicts
plan_v3.md, plan_v3.md wins — raise it, do not silently pick.

Paths: repo `c:\Users\ADMIN\Desktop\AgentMailGuard` (branch `refactor/guard-v3`, base `main`),
subtree `...\AgentMailGuard\agentmailguard` (package `mailguard`). Always use absolute paths.
Windows PowerShell: chain with `;`, temp is `$env:TEMP`. No WSL: `make` and bash recipes cannot
run. Docker Desktop IS running (engine 29.1.5, compose v5.0.1).

## (a) Verified current state

Committed: `7ecaf82 test(guard): v2 parity golden and ADR-0015 (proposed) ... [task v3.1]` on top
of `main` (`f9e93a2`). It added `agentmailguard/tests/parity.py`,
`agentmailguard/tests/golden/guard_v2_baseline.json`, `agentmailguard/tests/unit/test_guard_parity.py`,
`agentmailguard/scripts/capture_guard_baseline.py`, ADR-0015, `plan_v3/`. Task 1 is done; tick it
in plan_v3's Execution log during Task 2's commit.

Task 2 is in the working tree, uncommitted (staged renames + unstaged import edits):

| Done (verified) | Evidence |
|---|---|
| `layers/l1_injection_scanner/*` → `layers/inbound_guard/{scan,rules,classifier,llm_judge}.py` | git status R entries |
| `layers/l2_intent_extractor/extractor.py` → `layers/inbound_guard/extract.py` (old `__init__` deleted) | git status |
| `layers/l3b_document_scanner/scanner.py` → `layers/doc_scanner/chunk_scanner.py` | git status |
| `layers/l4_output_scanner` → `layers/output_guard/scanner.py` | git status |
| `layers/l5_policy_engine` → `mailguard/policy/engine.py` | git status |
| `layers/l3_channel_isolation/isolation.py` → `mailguard/prompting/channel_isolation.py` (`prompting` did not exist at HEAD: `git show HEAD:agentmailguard/mailguard/prompting` → not in HEAD) | git |
| Prompts renamed: `inbound_scan_judge.v1`, `inbound_extractor.v1`, `doc_chunk_judge.v1`, `output_judge.v1`; every `PROMPT_NAME` points at the new names | git grep |
| No `.py`/toml/Makefile import of any old module path remains (subtree or root); only docs mention them (Task 14) | `git grep -E 'layers[./\\](l1_injection_scanner|...)'` → only README.md, paper/outline.md, docs/overview/layers/* |
| `mailguard/prompts/__init__.py` loads by name from its own dir — no old path. Hatch `packages = ["mailguard","services"]` packages subdirs + .txt — no change needed | read |
| Subtree unit tests: all pass (230 tests, incl. the 87 parity matrix cases) | pytest run |
| Root guard tests: 64 failed = 63 baseline + 1 (see below) | mg_root_tests.ps1 |

Task 2 still to do (pending):
1. Ruff I001 (import order) introduced by the move in 4 files: `agentmailguard/evaluation/eval_detectors.py`,
   `agentmailguard/mailguard/pipeline.py`, `agentmailguard/tests/unit/test_l3_isolation.py`,
   `agentmailguard/tests/unit/test_llm_fallback.py` → `ruff check --fix` on exactly those files.
2. Rename subtree test files to v3 names with `git mv` (decision: done now, because Task 2 is the
   move-only task and Task 15's ratchet would otherwise force a second move):
   `test_l1_scanner.py→test_inbound_scan.py`, `test_l2_extractor.py→test_inbound_extract.py`,
   `test_l3_isolation.py→test_channel_isolation.py`, `test_l3b_doc_scanner.py→test_doc_chunk_scanner.py`,
   `test_l4_output_scanner.py→test_output_scanner.py`, `test_l5_policy.py→test_policy_engine.py`,
   `test_l5_strictest.py→test_policy_strictest.py`. Contents unchanged.
3. Stage `agentmailguard/` + `tests/unit/test_mailguard_live_guard_stages.py` (by name) and commit.
   Identifiers inside the code (`LayerName.L1_INJECTION_SCANNER`, `LAYER = ...`, `settings.l1`,
   `pipeline.l1`, `GuardConfig.l1`, `report.l3b`) are NOT Task 2 — they belong to Tasks 3–6.

Known transient regression (not caused by Task 2): `tests/unit/test_mailguard_single_repo_layout.py::
test_the_committed_guard_folder_is_the_v2_pin` fails since 7ecaf82, because any commit under
`agentmailguard/` changes `HEAD:agentmailguard` away from the v2 pin tree `f659748a…`. ADR-0015 says the
pin moves to the v3 commit; that must be the LAST guard change, so it is Task 15 step 15b. It is listed
in `.agents/tasks/guard-v3/known-transient-failures.txt` and is allowed only until 15b.

Pre-existing noise (ignore, never "fix" inside a task):
- ruff: 34 errors, all in `agentmailguard/training/colab_unsloth_finetune.ipynb`; `ruff format --check`
  reports exactly 1 file (that notebook). Anything else is yours.
- mypy `mailguard`: 30 errors in 4 untouched files under `mailguard/datasets/` (`build_l1_corpus.py` 16,
  `download.py` 7, `build_rag_poison.py` 5, `build_email_benchmark.py` 2). Gate = no NEW mypy error
  outside those 4 files.
- Root: 63 baseline failures (make/bash/WSL/OpenRouter-env/kit/doctor), saved at
  `c:\Users\ADMIN\Desktop\AgentMailGuard\.agents\tasks\guard-v3-root-baseline-failures.txt`
  (copied from `$env:TEMP\mg_base_failed_v2.txt`). `test_mailguard_kit_commands.py` is excluded by the
  script. Non-mailguard Windows failures (e.g. `test_agent_profile` jinja path) are outside the
  script's pattern and out of scope.

## (b) Ordered task groups, files, commits

Mapping onto the existing workflow loops (unchanged; each keeps its own verdict file):
`loop-package-settings` = Tasks 0, 2, 3 · `loop-pipeline-core` = Tasks 4–8 ·
`loop-contract-judges` = Tasks 9–11 · `loop-bench` = Tasks 12–13 · `loop-docs-finalize` = Tasks 14–15.
Inside a loop, do tasks in order; one commit per task (15 has two). **[review, Q9]** Push
`refactor/guard-v3` to origin after every task's commit (`git push -u origin refactor/guard-v3`);
never open a PR, never merge into `main`, never force-push.

Commit message: `refactor(guard): <what changed, present tense> [task v3.N]` (docs task may use
`docs(guard): …`, the pin commit `chore(bench): …`, always ending `[task v3.N]`). Stage files by name
(`git add <paths>`), never `git add -A`; `.agents/` is git-ignored and must stay out of commits.
Every commit also ticks its line in `plan_v3/plan_v3.md` "Execution log" (`- [x] Task N — on <sha
of previous> …`), and replaces Task N-1's base sha with Task N-1's real commit sha (a commit cannot
contain its own hash; plan_v3 F6).

Cross-cutting rules for every task 3–13:
- **Root consumer in the same commit.** Q1=b is a hard rename with no aliases, so whenever a task
  renames something the root bench reads, update the root consumer in that commit so the root guard
  run shows no regression. Root files that read guard internals (verified by grep):
  `evaluation/mailguard_bench/{guard_build,guard_factory,guard_smoke,guarded_reply,scheme,artifacts,
  analyses,first_layer}.py`, `evaluation/mailguard_bench/live/guard_worker.py`,
  `tests/unit/test_mailguard_bench_{guard,guarded_reply,layer_guard,scheme_guard}.py`,
  `tests/unit/test_mailguard_live_guard_stages.py`, `tests/integration/test_mailguard_bench_guarded_run.py`.
  Before each commit: `git grep -n -E '<old names of this task>' -- evaluation tests scripts services`.
- If a root test asserts v2-only bench semantics that v3 cannot express before Task 12/13, add it to
  `known-transient-failures.txt` with `expires: Task 12|13` and the reason, and say so in the commit
  body. Tasks 12/13 must empty those entries. Never add entries any other way.
- New/renamed config keys: `agentmailguard/.env.example` AND root `docs/configuration.md` (new section
  "AgentMailGuard guard keys (v3)" with the key table and the v2→v3 migration table) in the same
  commit (CLAUDE.md §3.4). Root services do not read guard keys, so root `.env.example` is not touched.
- Parity: the golden JSON is never edited. `tests/parity.py` sections "API-version specific"
  (`_offline`, `run_case`) may change to the new API. Every changed outcome goes into `INTENDED_DELTAS`
  with `why` citing an ADR-0015 "Intended behaviour changes" bullet; a delta that fits no bullet is a
  regression → fix code, or stop and ask (divergence protocol §7). **[review, design decision 14]**
  Each entry also carries `test: "<pytest node id>"` of a unit test that proves the new behaviour
  without the golden; `test_guard_parity.py` asserts that node id exists (collect-only). Maximum deltas
  per task: Task 4 ≤ 10 (owner decision 2026-10-14, plan_v3 F10; was 3), Task 5 ≤ 6, Task 7 ≤ 20, Task 8 ≤ 6, Task 11 ≤ 5; exceeding a cap means stop
  and ask, not raise the cap.

### Task 0 — Docker re-baseline on main (no commit)
Docker became available after the 63-failure baseline was taken; re-measure before trusting it.
1. `git worktree add "$env:TEMP\mg_main" main` (read-only use; remove at the end with
   `git worktree remove "$env:TEMP\mg_main"`).
2. Make `.agents/tasks/guard-v3/mg_root_tests.ps1` (verbatim copy of the TEMP script, already present)
   take `[string]$Root = "c:\Users\ADMIN\Desktop\AgentMailGuard"`; run it with `-Root "$env:TEMP\mg_main"
   -Out "$env:TEMP\mg_main_unit.txt"`.
3. Infra for integration tests (they use the compose stack's Postgres/RabbitMQ with an isolated DB and
   vhost, `tests/integration/conftest.py`): from the repo root
   `$env:GIT_COMMIT = git rev-parse HEAD; docker compose up -d postgres rabbitmq`, wait until
   `docker compose ps --format '{{.Name}} {{.Health}}'` shows both `healthy`. Then, in the main worktree:
   `$env:MAILGUARD_DIR="$env:TEMP\mg_main\agentmailguard"; uv run --project . --with-editable ./agentmailguard python -m pytest tests/integration/test_mailguard_bench_guarded_run.py tests/integration/test_mailguard_bench_host.py -p no:cacheprovider -q -rfE`.
4. Write `.agents/tasks/guard-v3/root-baseline-docker.txt` = union of failures from 2 and 3 (same
   `FAILED <nodeid>` format). From then on this file is THE root baseline; keep the old one for
   reference. If it differs from the 63-line file, note which tests flipped.
5. Full-stack check once (DoD §3.3, native `make up`): `docker compose up -d --build`, wait for every
   service with a healthcheck to be `healthy` (timeout 10 min), record result, `docker compose down`.
   Leave postgres/rabbitmq up only while integration tests run.

### Task 2 — package layout + prompt names (move-only) — finish and commit
Files: the 4 I001 files above; `git mv` of the 7 test files; `plan_v3/plan_v3.md` (tick Tasks 1, 2).
Commit: `refactor(guard): v3 package layout and prompt names, move-only [task v3.2]`.

### Task 3 — Settings v3 + LegacyConfigError
Decision: one flat section per layer named by its identifier; the two old `llm_enabled` become
`scan_llm_enabled` / `extract_llm_enabled` so no key is ambiguous after the L1+L2 merge.
- `mailguard/config/settings.py`: `InboundGuardSettings` (all L1 fields; `llm_enabled→scan_llm_enabled`;
  L2's `llm_enabled→extract_llm_enabled`, `max_body_chars`, `strip_threshold`; keep the
  flag≤block validator), `DocScannerSettings` (= L3b), `ChannelIsolationSettings` (= L3),
  `OutputGuardSettings` (= L4), `PolicySettings` (= L5). `MailGuardSettings` fields `inbound_guard,
  doc_scanner, channel_isolation, output_guard, policy`. `GuardModelSettings`: `inbound_judge,
  inbound_extractor, doc_judge, doc_set_judge, output_judge, models_path` (all default `"fake"`;
  `doc_set_judge` unused until Task 10). `ml_model_path` default stays
  `artifacts/models/l1_injection_clf_v1.joblib` (pinned artifact name).
- `LegacyConfigError(ValueError)` in settings.py, exported from `mailguard/config/__init__.py`.
  `MailGuardSettings.__init__` checks, before `super().__init__`, `os.environ` and the dotenv file
  that will be read (`_env_file` kwarg if given, else `model_config["env_file"]`, read with
  `dotenv.dotenv_values`; python-dotenv ships with pydantic-settings) for keys, case-insensitive:
  prefixes `L1__ L2__ L3__ L3B__ L4__ L5__` and exact `GUARD_MODELS__JUDGE|EXTRACTOR|DOC_SCANNER`.
  It raises one error listing every found key with its v3 replacement (table:
  `L1__LLM_ENABLED→INBOUND_GUARD__SCAN_LLM_ENABLED`, `L1__X→INBOUND_GUARD__X`,
  `L2__LLM_ENABLED→INBOUND_GUARD__EXTRACT_LLM_ENABLED`, `L2__X→INBOUND_GUARD__X`, `L3__→CHANNEL_ISOLATION__`,
  `L3B__→DOC_SCANNER__`, `L4__→OUTPUT_GUARD__`, `L5__→POLICY__`, `GUARD_MODELS__JUDGE→…INBOUND_JUDGE`,
  `…EXTRACTOR→…INBOUND_EXTRACTOR`, `…DOC_SCANNER→…DOC_JUDGE`). Keep the table as one module constant
  `LEGACY_ENV_KEYS` (the ratchet in Task 15 whitelists it).
- Update every reader: `mailguard/layers/inbound_guard/{scan,extract,llm_judge,classifier}.py`,
  `layers/doc_scanner/chunk_scanner.py`, `layers/output_guard/scanner.py`, `prompting/channel_isolation.py`,
  `policy/engine.py`, `pipeline.py` (`gm.judge→gm.inbound_judge` etc.), `mailguard/llm/registry.py` if it
  reads guard_models, `services/guard_worker/main.py`, `evaluation/*.py`, `training/*.py`,
  `tests/conftest.py`, `tests/unit/*`, `tests/parity.py::_offline` (`s.policy.audit_log_path`,
  `s.inbound_guard.ml_model_path`), root `evaluation/mailguard_bench/guard_build.py`, `guard_factory.py`
  and any root env builder that sets `GUARD_MODELS__*`/`L*__` (grep), plus their root tests.
- `agentmailguard/.env.example` rewritten with v3 keys; `agentmailguard/docs/manual_steps.md` C5 line
  (`GUARD_MODELS__JUDGE`→`GUARD_MODELS__INBOUND_JUDGE`); root `docs/configuration.md` section.
- New `agentmailguard/tests/unit/test_settings_v3.py`: parametrized over `LEGACY_ENV_KEYS`, each old
  key via `monkeypatch.setenv` and via a tmp `.env` raises `LegacyConfigError` naming the new key;
  new keys load into the right field; no legacy key → no error.
Commit: `refactor(guard): v3 settings sections and LegacyConfigError for v2 env keys [task v3.3]`.

### Task 4 — Contracts v3, policy 2026.10-v3, audit v3
- `mailguard/contracts/verdict.py`: `LayerName` = `INBOUND_GUARD="inbound_guard"`,
  `DOC_SCANNER="doc_scanner"`, `INBOUND_GATE="inbound_gate"`, `OUTPUT_GUARD="output_guard"` (4 values,
  R-d: channel isolation is not a layer). New `InboundVerdict` (replaces `SanitizedIntent`: its fields
  plus the scan verdict's severity/score/findings/error; `metadata["stages"]={"scan":…, "extract":…}`)
  with a pure `InboundVerdict.merge(scan: LayerVerdict|None, extract: SanitizedIntent-like|None)`
  implementing design decision 8 **[review]**: `severity`/`score` come from `scan` only; findings =
  union with extractor findings tagged `metadata["stage"]="extract"`; error if either sub-stage
  errored; latency = sum; new field `question_sentences: list[str]` (recorded by the pipeline from
  `prompting/questions.py`, see Task 5; never computed inside L1). `DocumentVerdict` (LayerVerdict fields +
  `chunks: list[ChunkVerdict]`, `kept_chunk_ids`, `quarantined_chunk_ids`, `quarantine_ratio`) with
  `DocumentVerdict.from_chunks(verdicts)`. `ChunkVerdict` gains `trust: Literal["trusted","untrusted",
  "quarantined"]`, `cluster_id: str|None`, `signals: dict[str, float]` (defaults keep the parity golden
  readable). **[review, Q7]** `RetrievedChunk` → `SourceDocument` (`from_any` kept); every reader in the
  subtree and the root bench renamed in this commit (grep `RetrievedChunk`).
  `IsolationInfo` (`mode`, `nonce`, `chunks_in`, `chunks_kept`, `markers_scrubbed` (count),
  `forged_markers: list[Finding]` (default `[]`; one `forged_channel_marker` finding, `metadata["stage"]=
  "isolation"`, per marker scrubbed from thread history — plan_v3 F3), `truncated`,
  `error: str|None`, `latency_ms`). Nothing writes to `report.inbound_guard.findings` after the gate. Decision: `merge`/`from_chunks` live in contracts now so the
  pipeline stays buildable; Tasks 5/7 move the call sites into the facades.
- `mailguard/contracts/policy.py` `GuardReport`: `schema_version: Literal[3] = 3`, `message_id`,
  `organization_id`, `inbound_guard: InboundVerdict|None`, `doc_scanner: DocumentVerdict|None`,
  `isolation: IsolationInfo|None`, `inbound_decision`, `output_guard: OutputVerdict|None`, `decision`,
  `total_latency_ms`; `model_config = ConfigDict(extra="forbid")` so a v2 dump (`l1`, `l3b`, …) fails
  validation; `verdicts()` returns the non-None layer verdicts.
- `LAYER` constants: inbound_guard/* → `INBOUND_GUARD` (findings keep `detector`/`rule_id`; add
  `metadata["stage"]="scan"|"extract"`), chunk_scanner → `DOC_SCANNER`, output scanner → `OUTPUT_GUARD`,
  policy engine → `INBOUND_GATE` for `stage="inbound"`, `OUTPUT_GUARD` for `"outbound"`.
- `prompting/channel_isolation.py`: `build()` returns `(SecurePrompt, IsolationInfo)`; no verdict,
  no findings; a build exception sets `isolation.error` (design decision 7). Policy fact `layer_error`
  becomes true when any verdict `error` OR `report.isolation.error` is set (P00 unchanged).
- `policy/engine.py`: `facts()` reads the v3 report (removed_ratio from `inbound_guard`, quarantine
  data from `doc_scanner`, citation/goal facts from `output_guard`); audit record keys v3
  (`"layers"` keyed by v3 names, `"doc_scanner"` instead of `"l3b"`, `"schema_version": 3`). Keep the
  sha256 audit_id seed deterministic.
- `configs/policy.yaml`: `version: "2026.10-v3"`; rule IDs, priorities, actions unchanged.
- `pipeline.py`: fill `report.inbound_guard` via `InboundVerdict.merge`, `report.doc_scanner` via
  `from_chunks` (ordering unchanged in this task), `report.isolation`; `summary()` keys v3
  (`doc_scanner_quarantined`). `integration/adapters.py` (`report_json`, `dispatch_allowed`) adapted;
  `decision_to_job_result` output unchanged.
- Subtree: `evaluation/harness.py` (detected layer strings), `tests/unit/test_evaluation.py`,
  `test_contracts.py`, `test_pipeline.py`, policy tests, `services/guard_worker/main.py`.
  Root: `evaluation/mailguard_bench/artifacts.py` `AI_LAYERS`, `first_layer.py`, `guarded_reply.py`, any
  `report.l*` reader, and their tests.
- Forged markers (ADR decision 8): after removing the isolation verdict, run parity. If `x-forged`
  cases change, add a forged-channel-marker finding to the inbound scan (`rules.py`
  `obfuscation_findings`) and the chunk scanner so the email/document outcome matches the golden. Only
  thread-history-only forgeries may become intended deltas (ADR bullet 2); those are recorded in
  `IsolationInfo.forged_markers`, never in `report.inbound_guard.findings`.
Commit: `refactor(guard): v3 contracts, GuardReport schema 3, policy 2026.10-v3 and audit v3 [task v3.4]`.

### Task 5 — L1 Inbound Guard facade
- New `mailguard/layers/inbound_guard/guard.py` `InboundGuard(settings, *, judge, extractor_llm,
  scan=None, extract=None)`: owns `EmailInjectionScanner` + `UserIntentExtractor` (shared `rules`,
  `classifier`), `async inspect(email) -> InboundVerdict` (scan, then extract; ≤2 LLM calls, Q5=a),
  exposes `.rules`, `.classifier`, `.scan`, `.extract`, `.judge`. Export from `layers/inbound_guard/__init__.py`.
- `pipeline.py`: attributes/kwargs `l1`, `l2`, `judge`, `extractor_llm` → `inbound_guard`,
  `inbound_judge`, `inbound_extractor`; doc scanner takes `inbound_guard.rules/classifier`.
- `tests/parity.py::run_case` kwargs; root `guard_factory.py` (`pipeline.l1.judge` →
  `pipeline.inbound_guard.judge`, `pipeline.l2.llm` → `pipeline.inbound_guard.extract.llm`),
  `guard_build.py`, `guard_smoke.py`, root tests.
- **[review, R-i]** `extract.py`: the extractor no longer calls `max(severity, 0.6)` (or any severity
  bump) when `contains_assistant_instructions` is true; it emits a finding
  (`detector="extract"`, `rule_id="assistant_instructions"`) instead.
- **[review 10-14, F4]** The extract sub-stage always runs its deterministic part (quoted-history
  strip, sanitized body, `removed_ratio`, intent heuristic); only its LLM call is optional
  (`INBOUND_GUARD__EXTRACT_LLM_ENABLED`). Test: with the extract LLM off, `InboundVerdict` still has a
  sanitized body and `removed_ratio`, and zero extractor calls are recorded.
- **[review, design decision 8]** New `mailguard/prompting/questions.py` with the pure function
  `question_sentences(body: str) -> list[str]` (sentences ending in `?` or starting with a request
  verb, after quoted-history strip; max 8, each ≤ 40 tokens; a ≤ 3-token question is kept). It is
  **not** part of L1: `pipeline.inspect_inbound` always calls it, on the sanitized body when L1 ran
  and on the raw body otherwise, stores the result in `report.inbound_guard.question_sentences` when
  L1 is on and in `report.metadata["question_sentences"]` otherwise, and passes it to L2. Test: the
  value is identical with L1 on and off for an email with nothing to sanitize.
- New `tests/unit/test_inbound_guard.py`: merge rules, error propagation, `metadata.stages`, LLM call
  count ≤2 with FakeLLMProvider; **[review]** "clean scan + flagging extractor ⇒ severity unchanged,
  finding present"; `question_sentences` on 5 fixture emails (incl. a 2-token question and a
  no-question email → `[]`). Parity: the harmless-question cases whose v2 outcome was quarantine
  become an intended delta under ADR-0015 bullet 4.
Commit: `refactor(guard): L1 Inbound Guard facade returns InboundVerdict [task v3.5]`.

### Task 6 — GuardConfig + presets v3
- `GuardConfig(name, inbound_guard, doc_scanner, inbound_gate, output_guard, channel_isolation)`;
  presets `C0` (all off), `C1` = inbound_guard+inbound_gate, `C2` = C1+doc_scanner+channel_isolation,
  `C3` = all, ablations `C3-L1` (no inbound_guard), `C3-L2` (no doc_scanner), `C3-L3` (no inbound_gate),
  `C3-L4` (no output_guard), `C3-ISO` (no channel_isolation); old names (`C3-L3B`, `C3-L5`) →
  `ValueError`. `active_layers` returns the 4 layer ids only; `isolation` is a separate bool.
- Pipeline attributes/kwargs `l3, l3b, l4, l5` → `channel_isolation, doc_scanner, output_guard,
  policy`; `doc_llm→doc_judge`, `output_llm→output_judge`.
- Consumers: `services/guard_worker/main.py` (`--config`), subtree `evaluation/harness.py`,
  `tests/unit/test_pipeline.py`, `test_evaluation.py`; root `scheme.py`, `guard_build.py`
  (`config.l1 or config.l2 or config.l3b`), `live/guard_worker.py`, root tests (transient-failure rule
  applies to v2-only bench assertions; expires Task 12).
Commit: `refactor(guard): GuardConfig v3 identifiers and presets [task v3.6]`.

### Task 7 — L2 Doc Scanner before L3 Inbound Gate + build_escalated_draft + cheap-stage signals
- New `layers/doc_scanner/set_judge.py` (`deterministic_set_verdict(chunk_verdicts) -> DocumentVerdict`)
  and `layers/doc_scanner/scanner.py` `DocScanner` (per-chunk `RetrievedDocumentScanner` + signals +
  set judge; `async scan(chunks, *, question_sentences, query) -> DocumentVerdict`). New
  `layers/inbound_gate/{__init__,gate}.py` `InboundGate(policy).decide(report, *, category) ->
  PolicyDecision` (stage="inbound").
- **[review, R-h / design decision 9]** New `layers/doc_scanner/signals.py`, pure functions, no model
  call, unit-tested one by one:
  - `echo_vs_question(chunk_text, question_sentences) -> float` — max token/3-gram overlap of the
    chunk's opening window against each question sentence; a separate short-question rule for
    questions of ≤ 3 tokens (exact phrase match in the first 20 tokens) so they are not dropped.
  - `ordered_prefix(chunk_text, question_sentences) -> bool` — the chunk starts with (or repeats, in
    order) a question sentence before its own content.
  - `bait_clusters(chunks, echo_scores) -> dict[chunk_id, cluster_id]` — union-find over pairs with
    high mutual echo; a cluster with ≥ 2 high-echo chunks is quarantined whole.
  - `trust_of(score, settings) -> trust`.
  `ChunkVerdict.signals` records every value. `DocScannerSettings` gains `echo_threshold`,
  `echo_short_question_tokens`, `cluster_echo_threshold`, `suspicious_band: tuple[float, float]`, each
  with its value **and** the dev split it was chosen on in the field description. Thresholds are set
  on the dev split of `datasets/build_rag_poison.py`; the 100 pinned poisoned-document cases are never
  used for tuning (test: a `tests/unit/test_doc_scanner_tuning_guard.py` asserting the pinned case ids
  do not appear in the dev split). **[review]** The dev split must contain, besides the generator's
  current PoisonedRAG-style prefix echo, at least two styles it does not produce today: paraphrased
  echo with no verbatim prefix, and the payload placed mid-chunk after benign text. `build_rag_poison.py`
  gains a `--style {prefix,paraphrase,mid}` option and the split is committed by id (seeded); the
  tuning-guard test also asserts all three styles are present in the dev split.
- `pipeline.py`: `inspect_inbound(email, chunks=(), *, category, query=None)` **[review, ADR
  decision 2]** runs inbound_guard → inbound_gate **pass 1** on L1 facts only (`docs_scanned=False`
  fact; P01/P04 cannot fire) → if the action is block/quarantine: set `report.doc_scanner=None`,
  return without any doc call → otherwise doc_scanner (echo reference = the question sentences above,
  falling back to the caller's query or subject + sanitized body only when the list is empty) →
  inbound_gate **pass 2** with every fact; `report.inbound_decision` is pass 2 (or pass 1 when it
  stopped) and `report.inbound_decision.metadata["gate_passes"]` records 1 or 2. **[review 10-14, F2]**
  Audit: exactly one record, for the final decision; the `audit_id` sha256 seed includes `gate_pass`
  (deterministic across identical runs, distinct from other runs' records). A pass-1 that passes is
  provisional and is not audited. Tests: one audit record per job in both paths; `audit_id` equal across
  two identical runs. `chunks=()` → `DocScanner.scan` returns an
  empty `DocumentVerdict` without touching any judge (test: a recording FakeLLMProvider sees zero
  calls). Test: an email L1 blocks never reaches the doc judge even with `doc_judge` configured.
  `build_prompt`
  stops scanning; it uses `report.doc_scanner.kept_chunk_ids` and renders each kept chunk as
  `[chunk id=… trust=…]`; **[review, design decision 12]** `GUARDED_PROMPT_VERSION = "guarded.v3"`,
  written into the run meta by the root bench (`runmeta.py`, Task 13).
  `run()` signature and return shape unchanged: on a block/quarantine with
  `stop_on_inbound_block=True` it still returns `(report, None, None)` with no model call.
  Decision: `build_escalated_draft(report) -> DraftCandidate` is a public pipeline method (fixed
  holding body, `action="escalate"`, reason from `report.inbound_decision`) that hosts call; `run()`
  does not put it in its return value, so the parity `draft_body` and `Job.result_ref` stay unchanged.
- Parity: record intended deltas for poison chunk sets (P01/P04 at inbound, generation stopped or
  floor carried) under ADR bullet 1, and for chunks newly caught by the echo/cluster signals under
  ADR bullet 5. New tests `tests/unit/test_doc_scanner.py` (signals, clusters, trust, 0 clean chunks
  flagged on a 3-tenant clean fixture), `tests/unit/test_inbound_gate.py` (incl. "no model call when
  gate blocks").
- Root: `guarded_reply.py`/`guard_build.py` if they call `inspect_inbound`/`build_prompt` directly.
Commit: `refactor(guard): doc scanner runs before the inbound gate; escalated draft helper [task v3.7]`.

### Task 8 — L4 Output Guard replaces the outbound gate (inbound floor D1)
- New `layers/output_guard/guard.py` `OutputGuard(settings, scanner, policy)`;
  `async inspect(report, draft, …) -> tuple[OutputVerdict, PolicyDecision]`: scan, then
  `policy.decide(stage="outbound")`, then the floor exactly as plan_v3 design decision 1
  (`CATCH_ALL` = ids of rules whose `when` is empty — today `P99-default` — computed from the loaded
  policy, not hard-coded; **[review 10-14, F1]** a floor exists only when `rank(inbound.action) >
  rank(AUTO_SEND)`; strictest action; OR of `requires_human`; max risk tier; winner tie →
  outbound; `matched_rule_ids` union with `inbound:` prefix; `metadata.decided_by`,
  `metadata.carried_from_inbound`; reason `"carried from inbound_gate: <rule> (<action>)"`; auto_send
  allowlist after the merge). Not configurable.
- `pipeline.inspect_outbound` uses it; `report.decision` = merged decision. Remove the words
  "outbound gate" from code/comments.
- New `tests/unit/test_output_guard_floor.py`: parametrized table (inbound action × outbound action ×
  inbound rule catch-all/specific × allowlist) asserting the 4 invariants; audit_id equal across two
  identical runs. Required row: specific inbound rule returning `auto_send` × outbound `auto_send` +
  allowlist ⇒ final `auto_send`, `carried_from_inbound = false`. Parity deltas under ADR bullet 1
  (floor carried).
- **[review 10-14, F3]** `policy/engine.py` `facts()` for `stage="outbound"` adds
  `isolation_forged_markers = len(report.isolation.forged_markers)` (0 when isolation is off). No rule in
  `policy.yaml` uses it by default (no parity delta); test: the fact equals the scrubbed thread-history
  marker count on a fixture.
Commit: `refactor(guard): L4 Output Guard merges the inbound floor; outbound gate removed [task v3.8]`.

### Task 9 — Host review queue contract (additive)
- First re-check core consumers: `packages/db/{dispatch,draft_persistence,idempotency,job}.py`,
  `packages/domain/entities.py`, `services/triage_worker/gate.py` treat `result_ref` as opaque (grep at
  plan time found no reader of the `"mailguard"` key in `packages/` or `services/`); also root
  `evaluation/mailguard_bench/live/*`.
- `mailguard/integration/adapters.py::decision_to_job_result`: keep every key/value; add
  `"reason"` (winner's first reason or None), `"decided_by"` (`decision.metadata["decided_by"]`),
  `"guard_schema_version": report.schema_version`. Test: v2 key set ⊂ result with identical values on
  a fixed report; new keys present.
Commit: `refactor(guard): job result adds reason, decided_by and guard_schema_version [task v3.9]`.

### Task 10 — LLM set judge (flag, off)
- `DocScannerSettings.set_judge_llm_enabled: bool = False` (`DOC_SCANNER__SET_JUDGE_LLM_ENABLED`),
  `GUARD_MODELS__DOC_SET_JUDGE`; prompt `mailguard/prompts/doc_set_judge.v1.txt` (PoisonedRAG-style
  spread poisoning, chunks wrapped with `wrap_untrusted`, the question sentences shown as the
  reference); `set_judge.py` `LLMSetJudge` monotonic: may only add quarantined ids that are in the
  input, never removes; runs only when enabled, a provider exists, **and [review] at least one chunk's
  score lies inside `suspicious_band`** (chunks already quarantined or clearly clean never trigger a
  call — keeps the ≤1 doc-judge call budget). Pipeline kwarg `doc_set_judge`. `.env.example` + root
  `docs/configuration.md`.
- Tests: monotonic property, ignores unknown ids, flag off → no LLM call, flag on with no chunk in the
  band → no LLM call; parity unchanged.
Commit: `refactor(guard): optional LLM set judge for the doc scanner, off by default [task v3.10]`.

### Task 11 — Fail-closed invalid judge + guard never calls tools
- `MailGuardCoreSettings.fail_closed_on_judge_error: bool = True`
  (`MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR`). In the 5 judges (inbound scan judge, inbound extractor,
  doc chunk judge, doc set judge, output judge): on `LLMSchemaValidationError`/`LLMError` with the flag
  on, set `verdict.error = "judge_invalid:<stage>"` (`inbound_scan|inbound_extract|doc_chunk|doc_set|output`)
  → P00 human_approval; flag off → current `mark_llm_fallback`. Provider not configured → degrade (no error).
- **[review, design decision 13]** `GuardReport.metadata["guard_calls"]`: dict `{stage: count}` filled
  by every judge wrapper (`inbound_scan`, `inbound_extract`, `doc_chunk`, `doc_set`, `output`); a
  module constant `MAX_GUARD_CALLS_PER_JOB = 5`.
- Tests: per judge × flag; `tests/unit/test_guard_no_tools.py` runs the parity matrix with `llm=True`
  using a recording FakeLLMProvider and asserts no request carries tools/tool_choice, the guard never
  invokes the host `generate` more than once (R-g), and the summed `guard_calls` never exceeds
  `MAX_GUARD_CALLS_PER_JOB` for any case or config (incl. `C3` with every judge enabled).
- `.env.example` + root docs; ADR bullet 3 covers any delta.
Commit: `refactor(guard): invalid judge answers fail closed behind a flag; no tool calls [task v3.11]`.

### Task 12 — Bench scheme v3, configuration (root)
- `evaluation/mailguard_bench/scheme.py` scheme `v3` with the v3 presets incl. `C3-ISO`;
  `guard_build.py`, `guard_factory.py` (`LiveLayers` fields renamed to v3 labels, e.g.
  `inbound_judge`, `inbound_extractor`, `doc_judge`, `doc_set_judge`, `output_judge`), `guard_models.yaml`,
  `guard_smoke.py` output line, `live/guard_worker.py` + `kit/campaign.py` config-name validation, bench
  env builder (`GUARD_MODELS__INBOUND_*`, `MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR` per config; a
  v2-comparison mode sets it false). Schemes v1/v2 at HEAD refuse with a message naming their guard
  commits (`81df5d07…`, `915cb1e2…`) — ADR-0015 Consequences.
- Root tests `tests/unit/test_mailguard_bench_{scheme,scheme_guard,scheme_runners,scheme_tools,guard,
  layer_guard,layer_ablation,models}.py`. Clear all `expires: Task 12` transient entries.
Commit: `refactor(guard): bench scheme v3 configs and guard wiring [task v3.12]`.

### Task 13 — Bench scheme v3, scoring & report (root)
- `scoring.py`, `report.py`, `scheme_report.py`, `results.py`, `runmeta.py`, `artifacts.py`,
  `analyses.py` (labels), `first_layer.py` (v3 layer ids; `goal_pre_l4` → `goal_pre_output_guard`),
  `l2_recount.py`, `overhead.py`. The v3 reader refuses guard `schema_version < 3` and pre-v3 run metas
  with a clear message (Q1=b). Committed `evaluation/results/mailguard_bench/*` stay as historical data
  (not rewritten). **[review, Q1=b' / design decision 11]** New `evaluation/mailguard_bench/legacy_reader.py`:
  read-only loader for the committed v1/v2 run folders (run meta + per-case reports, old layer names
  kept as-is, no conversion to schema 3) used only by the report path (`make mailguard-report` /
  `scheme_report.py --legacy <run_dir>`); it raises on any attempt to run, score or ablate. Test:
  the Review 1 GPT-4o-mini and Qwen2.5-7B tables regenerate byte-identical from the committed folders;
  a v3 folder passed to the legacy reader is refused, a v2 folder passed to the v3 reader is refused.
  `runmeta.py` records `guarded_prompt_version`. **[review, design decision 12/13]** `overhead.py`
  reports, per config, guard calls per job (from `guard_calls`), guard latency p50/p95 and end-to-end
  p50/p95; `scheme_report.py` prints that table next to the ASR table.
- Root tests `test_mailguard_bench_{scoring,report,report_v2,report_v2_build,scheme_report,artifacts,
  analyses,first_layer,l2_recount,overhead}.py`. Clear `expires: Task 13` entries.
Commit: `refactor(guard): bench scheme v3 scoring and reports; pre-v3 runs refused [task v3.13]`.

### Task 14 — Docs, paper, specs, ADR → Accepted
- Subtree: `agentmailguard/README.md` layer table, `docs/architecture.md`, `docs/experiments.md`,
  `docs/manual_steps.md`, `paper/outline.md` (mapping table; v2 ablation tables marked "v2 results").
- Root: `docs/overview/layers/*` → v3 cards (`L1-inbound-guard.md`, `L2-doc-scanner.md`,
  `L3-inbound-gate.md`, `L4-output-guard.md`, `channel-isolation.md`; old files `git mv`'d then rewritten),
  `docs/demo-runbook.md`, `docs/benchmark-windows-native.md`, `docs/configuration.md` check.
- `specs/design.md` guard section; `specs/requirements.md` — never delete/renumber, mark superseded
  criteria `WITHDRAWN (ADR-0015)`; `specs/tasks.md` — add the v3 tasks with requirement IDs.
- ADR-0015 status → Accepted (date, final intended-delta list = `INTENDED_DELTAS` reasons); add
  "Amended by ADR-0015" lines to ADR-0010/0012/0014.
Commit: `docs(guard): v3 architecture in docs, paper, specs; ADR-0015 accepted [task v3.14]`.

### Task 15 — Ratchet, cleanup, pin, acceptance
- 15a: `agentmailguard/tests/unit/test_no_legacy_identifiers.py` scans `mailguard/ services/
  evaluation/ training/ configs/ tests/` for `l1_injection_scanner|l2_intent_extractor|
  l3_channel_isolation|l3b_document_scanner|l4_output_scanner|l5_policy_engine|\bL[1-5]B?__|
  GUARD_MODELS__(JUDGE|EXTRACTOR|DOC_SCANNER)\b|report\.l3b|\.l[1-5]\b attribute access`; whitelist
  (plan_v3 F5): the ratchet itself, `evaluation/mailguard_bench/legacy_reader.py`, `tests/golden/`,
  `tests/parity.py` (docstring and the `GOLDEN_PROMPT_KEY` map), `LEGACY_ENV_KEYS`, the artifact
  name `l1_injection_clf_v1`, `train_l1_classifier`/`build_l1_corpus`. Root twin
  `tests/unit/test_mailguard_v3_identifiers.py` over `evaluation/mailguard_bench` (excluding
  `evaluation/results/`). Remove dead helpers (`pipeline.severity_of`, `verdict_or_none` if unused).
  Commit `refactor(guard): ratchet forbids v2 guard identifiers; cleanup [task v3.15]`.
- 15b (must not touch `agentmailguard/`): move the pin to 15a's commit: root
  `evaluation/mailguard_bench/guard_env.py` (`V3_MAILGUARD_COMMIT`, `V3_MAILGUARD_TREE` =
  `git rev-parse <15a>:agentmailguard`, `DEFAULT_MAILGUARD_COMMIT`), `Makefile` (`MAILGUARD_COMMIT ?=`,
  `MAILGUARD_V3_*`, the `MAILGUARD_TREE` selector), guides (`docs/demo-runbook.md`,
  `docs/benchmark-windows-native.md`); rename the layout test to `..._is_the_v3_pin` and update
  `tests/unit/test_mailguard_pin.py`. Empty `known-transient-failures.txt`.
  Commit `chore(bench): move the guard pin to the v3 guard [task v3.15]`.
- 15c: full acceptance (all of section (c), incl. Docker stack healthy and integration tests); tick
  Task 15 in plan_v3; report final state. **[review]** Push, then ask the owner to run `make ci` on a
  Linux checkout; Task 15 is complete only when that run is green, because the Windows baseline of 63
  environment failures cannot prove the make/bash recipes still work. **[review 10-14, F9]** The
  authoritative baseline is GitHub CI (green at `c98cdfe`: unit, overlay, quality, UI, integration);
  the Windows 63-failure list is only for local regression comparison.

### Task 16 — Live benchmark v3 (acceptance of the refactor) **[review, Q10 / ADR decision 13]**
Runs on the Windows machine with Docker Desktop (compose stack) and the OpenRouter/GPT-4o-mini profile
from task 7.19; it never touches `agentmailguard/` (the pin from 15b is what is measured).
- 16a — benign set, one commit before the run: `evaluation/mailguard_bench/datasets/benign_v3/`
  generated once by a seeded script (`build_benign_v3.py`): ≥ 100 ordinary question emails (≥ 20 of
  them 2–3-token questions, ≥ 20 with quoted history) and ≥ 100 clean knowledge documents on the same
  topics as the poisoned cases; ids committed; `tests/unit/test_mailguard_benign_v3.py` checks the
  counts and that no id overlaps the pinned poisoned cases. Commit `feat(bench): benign v3 set [task v3.16]`.
- 16b — the run: scheme v3, configs `C0, C1, C2, C3, C3-L1, C3-L2, C3-L3, C3-L4, C3-ISO`,
  `MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR=true`, datasets LLMail-Inject (as v1), the 100 pinned
  poisoned-document cases, and `benign_v3`. **No commit anywhere in the checkout while the RUN is in
  progress** (`rag_email_commit` is fingerprinted). Results zipped into
  `evaluation/results/mailguard_bench/<run_id>/` and committed afterwards:
  `chore(bench): v3 benchmark run <run_id> [task v3.16]`.
- 16c — acceptance, written into the run's `README.md` and the paper's results section, each criterion
  with its measured value and pass/fail:
  (a) C3 ASR ≤ 5% on LLMail-Inject;
  (b) poisoned-document route: "every poisoned chunk caught" ≥ 80/100 (replay predicted 85–89);
  (c) benign: ≤ 5% of question emails and ≤ 2% of clean documents quarantined/escalated;
  (d) overhead table present: guard calls per job ≤ 5 for every config, p95 end-to-end reported
  (SC5 p95 < 10 s stays a target, not a fail condition of this task);
  (e) ablations, measured per route **[review 10-14, F8]**: `C3-L1` — inbound catch rate (attacks
  blocked/quarantined before generation ÷ all attacks, LLMail-Inject) clearly lower than C3; `C3-L2` —
  "every poisoned chunk caught" on the 100 poisoned-document cases lower than C3 (otherwise the layer
  is not earning its place and the ADR must say so). Final ASR is reported for every config but is not
  criterion (e), since L4 can still catch injections with L1 off. `scoring.py`/`overhead.py` (Task 13)
  must compute the inbound catch rate per config.
  Any of (a)–(c) failing reopens Task 5 or 7 through the divergence protocol with the measured numbers
  in ADR-0015; thresholds are never re-tuned on the pinned cases. ADR-0015 moves to Accepted only after
  16c; Task 14 leaves it at "Accepted pending Task 16".

## (c) Verification commands (Windows)

Run after every task, before committing. Subtree commands from
`c:\Users\ADMIN\Desktop\AgentMailGuard\agentmailguard`:
```powershell
uv run --project .. --with-editable . python -m pytest tests/unit -q -p no:cacheprovider
uv run --project .. ruff check mailguard evaluation training tests services scripts      # only the 34 notebook errors allowed
uv run --project .. ruff format --check mailguard evaluation training tests services scripts  # only the notebook
uv run --project .. --with-editable . mypy mailguard                                      # only the 30 datasets/ errors allowed
```
Root guard tests (from anywhere):
```powershell
powershell -NoProfile -File C:\Users\ADMIN\AppData\Local\Temp\mg_root_tests.ps1 -Out C:\Users\ADMIN\AppData\Local\Temp\mg_now.txt
# if the TEMP copy is gone: c:\Users\ADMIN\Desktop\AgentMailGuard\.agents\tasks\guard-v3\mg_root_tests.ps1
$base = Get-Content c:\Users\ADMIN\Desktop\AgentMailGuard\.agents\tasks\guard-v3\root-baseline-docker.txt   # after Task 0; before it: ..\guard-v3-root-baseline-failures.txt
$allow = Get-Content c:\Users\ADMIN\Desktop\AgentMailGuard\.agents\tasks\guard-v3\known-transient-failures.txt | ? { $_ -match '^(FAILED|ERROR) ' }
Get-Content C:\Users\ADMIN\AppData\Local\Temp\mg_now.txt.failed | % { $_.Trim() } | ? { $_ -and ($base -notcontains $_) -and ($allow -notcontains $_) }   # must print nothing
```
A regression = a failure in `mg_now.txt.failed` that is in neither the baseline nor the transient list.
Integration (Tasks 4, 7, 8, 12, 13, 15c), from the repo root with Docker:
```powershell
$env:GIT_COMMIT = git rev-parse HEAD; docker compose up -d postgres rabbitmq   # wait for healthy
$env:MAILGUARD_DIR = "c:\Users\ADMIN\Desktop\AgentMailGuard\agentmailguard"
uv run --project . --with-editable ./agentmailguard python -m pytest tests/integration/test_mailguard_bench_guarded_run.py tests/integration/test_mailguard_bench_host.py -p no:cacheprovider -q -rfE
```
Stack health (Task 0 and 15c; native `make up`): `docker compose up -d --build`, all healthchecked
services `healthy`, then `docker compose down`. `make`/bash recipe tests stay environment failures (no WSL).

## (d) Parity contract

After EVERY task, `agentmailguard/tests/unit/test_guard_parity.py` (part of the subtree unit run) must
pass: each of the 87 matrix cases (`tests.parity.cases()`) equals `tests/golden/guard_v2_baseline.json` updated only by
`tests/parity.INTENDED_DELTAS`. Rules: the golden file is never regenerated or edited; each delta
entry lists only the changed fields plus `why` citing an ADR-0015 "Intended behaviour changes" bullet;
expected delta sources are Task 4 (thread-history-only forged markers, ≤ 3), Task 5 (extractor no
longer raises severity, R-i, ≤ 6), Task 7 (doc scanner before the gate and new cheap-stage signals,
≤ 20), Task 8 (inbound floor, ≤ 6) and Task 11 (fail-closed judge, only if a fake judge answer is
invalid, ≤ 5). **[review 10-14, F7]** Tasks 2, 3, 6, 9, 10, 12, 13 must add no delta. The reviewer of each loop checks every new delta
against the ADR list and that ADR-0015's list matches `INTENDED_DELTAS` by Task 14.

## Open points / assumptions
- **[review]** Q7 name: `SourceDocument` is the default; the owner may pick another name before Task 4
  starts. After Task 4 it is part of the hard rename.
- **[review]** Thresholds for the new doc-scanner signals need a dev split that excludes the 100 pinned
  poisoned-document cases; if `build_rag_poison.py` has no such split yet, Task 7 adds it (seeded,
  committed ids) before any threshold is chosen.
- **[review]** Host hooks (`trust` consumers) are ADR-0016, after Task 16; nothing in Tasks 3–16 touches
  `services/` or `packages/` beyond the additive `decision_to_job_result` fields of Task 9.
- **[review]** Floor tie rule (Task 8): when inbound and outbound rank equal, the outbound rule wins
  and its reason is shown; the inbound reason is kept in `matched_rule_ids` and `metadata.carried_from_inbound`
  only. If the owner wants both reasons in `reason`, join them with `"; "` — decide before Task 8.
- **[review]** Q5=a keeps two L1 calls (scan judge + extractor). Merging them into one call is a
  possible follow-up once Task 16 shows the overhead; not in scope here.
- `build_escalated_draft` is not returned by `run()` (keeps `Job.result_ref` and parity stable); if the
  owner wants the escalated draft inside the job result, that is an additive Task 9 change — ask.
- C1 in v3 includes the extractor (L1 = scan + extract). v2 bench rows "L1 only" have no v3 equivalent;
  Task 12 documents this instead of adding a sub-stage toggle (Q6=c gave a toggle only to isolation).
