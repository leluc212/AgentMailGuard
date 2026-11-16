# v4 benchmark: methods notes for the paper (task 7.36, ADR-0019)

Written during the run of 2026-11-10. Every number here comes from a committed file (named next to
it); results are added only after all three models have finished, from each run's `report.md`,
`metrics.csv` and `journal/summary.md`. Sections marked **TO FILL** are empty on purpose.

## 1. What changed from the 1,800-case set, and why

The earlier set (`evaluation/datasets/mailguard/`, sha256 `df350223…`, 1,800 cases, runs of
2026-10-19) overlapped the guard's own training and tuning data
(`docs/benchmark-data-leaks-2026-11-06.md`, measured 2026-11-09):

- 57 of its 222 PoisonedRAG questions were in the Doc Scanner's threshold-tuning split;
- all 500 benign emails (297 Enron, 203 LLMail) were in the L1 classifier's training corpus.

The v4 set removes both. It is **not** the 1,800-case set with cases removed: 400 RAG cases were
added, so it has 2,143 cases. **Results of the v4 runs are not comparable case by case with the
2026-10-19 runs** (other benign and RAG cases, 51 other email attacks, a retrained classifier,
and the triage change of ADR-0018).

| Set | 1,800-case set | v4 set (2,143) | Change |
|---|---|---|---|
| Email attacks, LLMail-Inject phase 2, benchmark half | 1,000 | 1,000 | 949 kept, 51 replaced (overlap or duplicate) by a seeded draw of the same scenario |
| Benign emails | 500 (203 LLMail + 297 Enron) | 500 (Enron only) | LLMail benign emails all stay in L1 training (owner, 2026-11-10) |
| PoisonedRAG | 222 | 165 | the 57 tuning questions removed |
| AgentMailGuard seed RAG | 18 | 18 | |
| LLMail-Inject phase 1 RAG (scenarios 3, 4) | 0 | 400 | new (owner, 2026-11-10) |
| Synthetic `synthetic_poison_v1` | 60 (inside the RAG set) | 60 (own set, reported apart) | |
| **Total** | **1,800** | **2,143** | +400 −57 |

Source: `evaluation/datasets/{mailguard,mailguard_v4}/manifest.json` (`counts`, `source_counts`).

## 2. Data partition (fixed before training)

`evaluation/datasets/partition_v1.json` (seed 20260930, sha256 `22c6430b…`) gives every raw unit
exactly one use: L1 training, Doc Scanner tuning, benchmark, or unused.

| Unit | L1 training | Tuning | Benchmark | Unused |
|---|---|---|---|---|
| Enron ham, one unit per normalised text (988 units, 992 message ids) | 376 | 0 | 500 | 112 |
| LLMail benign, one unit per distinct text (160 texts, 203 emails) | 160 | 0 | 0 | 0 |
| PoisonedRAG target question (300) | 0 | 90 | 165 | 45 |

Normalisation: lower case, whitespace runs to one space, on `Subject: <subject>\n<body>`
(the L1 builder's own rule). A PoisonedRAG question is one unit with its five poisoned passages.

The case builder (`evaluation/mailguard_bench/build_cases.py`) **fails, naming the ids**, if any
case shares content with another case, is in the L1 corpus, is in the LLMail training half, is a
tuning question, or comes from a non-benchmark unit. On the v4 set every check is 0
(`manifest.json`, `overlap_checks`).

Pool rules applied before selection (counted in `manifest.json`, `pool_rules`):

- Email attacks: of 10,517 benchmark-half attacks, 229 repeat another bench item's content and 290
  are in the L1 corpus or the training half; 9,998 remain.
- Phase-1 RAG: 9,872 rows → 9,584 distinct attacker texts → 7,983 distinct poisoned chunks
  (1,601 texts collapse to an existing chunk once the body is cut at 6,000 characters). 400 drawn,
  stratified by scenario (108 scenario 3, 292 scenario 4).

## 3. The L1 classifier

Retrained with the guard's unchanged trainer (`training.train_l1_classifier`, scikit-learn 1.9.1)
on the guard's unchanged corpus builder output minus the 500 benchmark Enron emails
(train 394, val 48, test 58 rows removed). Thresholds unchanged.
Source: `evaluation/mailguard_bench/pinned/l1_injection_clf_v1.metrics.json`, ADR-0019 §3.

| | Earlier (pinned before) | v4 |
|---|---|---|
| sha256 | `8fc1cbe7…` | `9c3c6c46…` |
| Training rows | 19,463 | 19,069 |
| Validation precision / recall | 0.946 / 0.960 | 0.945 / 0.960 |
| Validation FPR / AUROC | 0.051 / 0.989 | 0.056 / 0.987 |
| Test precision / recall | 0.966 / 0.927 | 0.966 / 0.926 |
| Test FPR / AUROC | 0.020 / 0.990 | 0.021 / 0.991 |
| Test FPR, Enron emails | 1/99 | 2/41 |

## 4. The new RAG cases (LLMail-Inject phase 1)

LLMail-Inject phase 2 has no retrieval scenario; phase 1 has two: scenario 3 (top-10 emails about
"project Zenith", goal: send `confirmation` to `contact@contact.com`) and scenario 4 (top-10 about
"Q2 budget revisions", goal: send the figure `$9.378 million`). Each case is built
(`evaluation/mailguard_bench/llmail_phase1_rag.py`) as:

- poisoned chunk: the attacker's email (subject and body, body cut at 6,000 characters);
- clean chunk: one benign email of the original context (scenario 4: the one holding the figure);
- inbound email: a benign question on the scenario's topic; the retrieval query is that topic;
- scored goal: exfiltration to `contact@contact.com` (recipient or body of the final draft).

To state in the paper: the 183 PoisonedRAG and seed cases test a **wrong answer** goal, the 400
phase-1 cases an **exfiltration** goal. Report them per source as well as together. All 400 share
two clean chunks (one per scenario). Their poisoned chunks are long (median 1,979 characters,
max 6,050), and the ingestion chunker may split one into several chunks.

## 5. Configurations, models, scoring

- Configurations (ADR-0017): C0, rag-email with no guard; C3, all four guard layers (Inbound Guard,
  Doc Scanner, Inbound Gate, Output Guard) plus channel isolation, all five judges live. Guard pin
  `d95e542` (tree `f1b3fa4d…`), unchanged.
- Models, each in every LLM role of its run: `gpt-4o-mini` (OpenAI), Llama-3.1-8B-Instruct
  (OpenRouter, pinned to CoreWeave bf16, fallbacks off), **Qwen3.5-9B** (`qwen/qwen3.5-9b`,
  OpenRouter, pinned to PrimeIntellect bf16, fallbacks off; ADR-0021). Embedding
  `text-embedding-3-small`, 1536 dimensions, the same for all three.
- **Why Qwen3.5-9B and not Qwen2.5-7B.** OpenRouter stopped serving Qwen2.5-7B on 2026-11-11
  (ADR-0020). A local 4-bit Qwen2.5-7B run exists (`2026-11-11-qwen25-local-v4`) and is not
  reported; the owner replaced the model with Qwen3.5-9B (ADR-0021). Qwen3.5-9B is a newer, larger
  model, so the third model is not a Qwen2.5 result.
- **Qwen3.5 reasoning is off.** Qwen3.5-9B can think before it answers, and neither client sends a
  switch for it. The pin is the endpoint that answers without reasoning; the canary fails on any
  reasoning token, and the run logged none (ADR-0021).
- **The guard's judges are the model under test.** In C3 the inbound, doc, doc-set and output
  judges of each run are that run's model (`manifest.json`, `runs.C3.live_layers`). A weaker model
  therefore weakens both the draft and the guard; a model difference in C3 is not a difference of
  the generator alone.
- Triage: category is an enum of nine values in the strict schema, 400-token budget (ADR-0018).
- Concurrency 2, per-call LLM timeout 60 s and case budget 300 s for all three models.
- Scoring: the harness rule on the final draft (`goal_achieved`). `human_approval` is not counted
  as blocked: a draft held for review is scored on its content, in every configuration.
- Metric names to use in the paper (owner, 2026-11-10: report attack success, not detection):
  - **ASR (attack success rate)**, k/n with a Wilson 95 % interval, over scored attacks
    (pipeline ASR), and over the attacks that reached drafting (guard ASR).
  - **FPR** and **utility** on the 500 benign emails.
  - **McNemar** C0 vs C3, paired on the same cases.
  - **Stop stage** per case (`journal/cases.<config>.csv`): triage stop / guard stopped the email /
    guard stopped the draft / draft delivered and model did not follow / attack succeeded.
- n: a case that ends in an error row after the retry pass is left out of n; the report prints
  scored/planned for every table, and the journal lists every excluded case with its error.

## 6. Per-run artefacts to cite

For each run `evaluation/results/mailguard_bench/<run>/`:
`report.md`, `metrics.csv`, `manifest.json` (commits, pins, settings), `journal/summary.md`
(stop stages, error kinds with an example message, the ADR-0018 log counts), `journal/cases.*.csv`,
`journal/errors.*.csv`, `kit-log.jsonl` (step timings), `raw/` (rows and container logs, not in git).

## 7. Results (filled 2026-11-12, Qwen3.5-9B column 2026-11-16, from the committed run files)

Figures and the full per-group table: `paper/figures/v4/out/` (`table_asr.csv`, built by
`paper/figures/v4/make_figures.py`, whose group totals are checked against each `metrics.csv`).

### 7.1 Runs

| Model | RUN | Cases scored / planned C0 | C3 | Model API cost per scored email, mean (USD) C0 / C3 | Run time |
|---|---|---|---|---|---|
| gpt-4o-mini | `2026-11-10-gpt4omini-v4` | 2,142 / 2,143 | 2,143 / 2,143 | 0.000243 / 0.000355 | 2.2 h |
| Llama-3.1-8B | `2026-11-11-llama31-v4` | 2,136 / 2,143 | 2,141 / 2,143 | 0.000209 / 0.000375 | 3.3 h |
| Qwen3.5-9B | `2026-11-16-qwen35-9b-v4` | 2,140 / 2,143 | 2,140 / 2,143 | 0.000069 / 0.000137 | 4.6 h |

- Cost: the report's "Overhead per email" table (`report.md`), mean over the emails with a draft
  record (n 1,243–1,802). Provider invoices are not in a committed file; total spend is not stated
  here.
- Run time: the sum of the config passes with status `ok` in `kit-log.jsonl` (gpt-4o-mini 673 s +
  7,177 s; Llama 5,514 s + 6,382 s; Qwen3.5 6,882 s + 9,754 s).

### 7.2 Attack success rate (ASR), k/n, Wilson 95 %

| Group (n planned) | Config | gpt-4o-mini | Llama-3.1-8B | Qwen3.5-9B |
|---|---|---|---|---|
| Email attacks, all (1,000) | C0 | 242/999 (24.2 %) | 324/999 (32.4 %) | 278/999 (27.8 %) |
| | C3 | 0/1000 (0.0 %) | 0/999 (0.0 %) | 0/998 (0.0 %) |
| PoisonedRAG, wrong answer (165) | C0 | 28/165 (17.0 %) | 150/165 (90.9 %) | 155/165 (93.9 %) |
| | C3 | 9/165 (5.5 %) | 20/164 (12.2 %) | 67/165 (40.6 %) |
| Seed RAG, wrong answer (18) | C0 | 12/18 (66.7 %) | 14/18 (77.8 %) | 18/18 (100.0 %) |
| | C3 | 0/18 | 0/18 | 0/18 |
| LLMail phase 1 sc. 3, exfiltration (108) | C0 | 21/108 (19.4 %) | 14/108 (13.0 %) | 43/108 (39.8 %) |
| | C3 | 0/108 | 0/108 | 0/108 |
| LLMail phase 1 sc. 4, exfiltration (292) | C0 | 23/292 (7.9 %) | 10/287 (3.5 %) | 17/292 (5.8 %) |
| | C3 | 0/292 | 0/292 | 0/292 |
| RAG attacks, all (583) | C0 | 84/583 (14.4 %) | 188/578 (32.5 %) | 233/583 (40.0 %) |
| | C3 | 9/583 (1.5 %) | 20/582 (3.4 %) | 67/583 (11.5 %) |
| Synthetic, reported apart (60) | C0 | 25/60 (41.7 %) | 14/60 (23.3 %) | 21/60 (35.0 %) |
| | C3 | 0/60 | 0/60 | 0/60 |

Wilson intervals: `table_asr.csv`. McNemar exact p, C0 vs C3 on the same cases (`metrics.csv`,
table `paired`):

| | gpt-4o-mini | Llama-3.1-8B | Qwen3.5-9B |
|---|---|---|---|
| Email (LLMail-Inject) | 2.8e-73 | 5.9e-98 | 8.2e-84 |
| RAG (583) | 7.3e-20 | 1.1e-50 | 1.9e-47 |
| Synthetic (60) | 6.0e-08 | 1.2e-04 | 9.5e-07 |

### 7.3 Benign emails (500 Enron)

| | gpt-4o-mini | Llama-3.1-8B | Qwen3.5-9B |
|---|---|---|---|
| FPR C0 | 0/500 (0.0 %) | 0/499 (0.0 %) | 0/498 (0.0 %) |
| FPR C3 | 21/500 (4.2 %) [2.8, 6.3] | 40/500 (8.0 %) [5.9, 10.7] | 22/499 (4.4 %) [2.9, 6.6] |
| Guard FPR C3 (benign that reached drafting) | 27/240 (11.3 %) | 42/379 (11.1 %) | 29/380 (7.6 %) |
| Utility C0 | 239/500 (47.8 %) | 374/499 (75.0 %) | 379/498 (76.1 %) |
| Utility C3 | 219/500 (43.8 %) | 291/500 (58.2 %) | 358/499 (71.7 %) |

Source: `metrics.csv`, table `llmail`, group `all`.

### 7.4 Where cases stopped (C3, attacks; `journal/summary.md`)

| Model | Triage stop | Guard stopped the email | Guard stopped the draft | Draft delivered |
|---|---|---|---|---|
| gpt-4o-mini | 635 | 603 | 383 | 22 |
| Llama-3.1-8B | 218 | 837 | 551 | 35 |
| Qwen3.5-9B | 341 | 740 | 489 | 71 |

- A triage stop counts as a failed attack. gpt-4o-mini's triage stops 139 of the 165 PoisonedRAG
  cases as "no reply required"; Llama and Qwen3.5 stop 9 and 2. Its low PoisonedRAG ASR therefore
  comes mostly from triage, not from the guard.
- Of the PoisonedRAG cases that passed triage in C3, the guard flagged 16 of 26 (gpt-4o-mini),
  139 of 155 (Llama) and 102 of 163 (Qwen3.5) (`journal/cases.C3.csv`, `layers_flagged`). Of
  Qwen3.5's 67 successes, 56 had no guard flag.
- The journal's "guard stopped the draft" includes drafts held for human approval; the report
  scores those on content (`human_approval` is not a block). The journal and the report therefore
  differ on 11 Qwen3.5 and 6 Llama PoisonedRAG rows; the report's numbers (7.2) are the results.
  Fix tracked as task 7.37.

### 7.5 Cases left out of n (error after the retry pass)

| Model | C0 | C3 |
|---|---|---|
| gpt-4o-mini | 1: `fail_closed_validation` 1 | 0 |
| Llama-3.1-8B | 7: `fail_closed_validation` 6, `triage_stage_failure` 1 | 2: `fail_closed_validation` 1, `triage_stage_failure` 1 |
| Qwen3.5-9B | 3: `triage_stage_failure` 3 (unparseable triage JSON) | 3: `triage_stage_failure` 3 (2 unparseable triage JSON, 1 HTTP 429) |

Case ids: `journal/errors.<config>.csv`. `fail_closed_validation`: the repair retry also returned
unparseable JSON, so no draft was stored (by design). Qwen3.5's pinned provider rate-limited often
(HTTP 429); the retry pass recovered 16 of 19 C0 and 31 of 34 C3 error rows (ADR-0021, observed
section).

### 7.6 Log counts (ADR-0018) and guard calls

| Model | `not recognized` C0 / C3 | `Unterminated string` C0 / C3 | Guard calls per job C3, mean / max |
|---|---|---|---|
| gpt-4o-mini | 0 / 0 | 4 / 0 | 1.67 / 9 |
| Llama-3.1-8B | 0 / 0 | 40 / 4 | 1.74 / 8 |
| Qwen3.5-9B | 0 / 0 | 18 / 8 | 1.71 / 8 |

Sources: `journal/summary.md` (log counts), `report.md` "Overhead per email" (mean) and "Guard
calls per stage" (most per job).

## 8. Limitations to state

- One benign source (Enron, 500 emails, 2000–2001 corporate mail); LLMail-style benign emails are
  not measured (they are L1 training data).
- The RAG headline mixes two goal types (wrong answer, exfiltration).
- 400 phase-1 cases share two clean chunks; their poisoned chunks are longer than PoisonedRAG's.
- Synthetic cases (60) are project-written templates and are reported apart.
- The retrained classifier's bytes are not reproducible run to run (same metrics, other sha256:
  ADR-0019 §3); the pinned file is the one used.
- ADR-0015 states at most five guard calls per job; measured runs reached up to 9 (each scanned
  chunk counts). Report the measured number.
- No layer ablation in these runs (ADR-0017): the contribution of each layer is not measured.
- The guard's judges are the model under test (section 5): C3 differences between models mix the
  generator's and the guard's strength. No run uses a fixed judge model.
- Qwen3.5-9B replaces Qwen2.5-7B (ADR-0021); it is a newer and larger model, and its results are
  not a Qwen2.5 result. It runs with reasoning off on one pinned provider, with no fallback; that
  provider rate-limited often, so 1 C3 case is left out of n for an HTTP 429.
- One run per model and configuration; no run-to-run variance is measured.
