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
- Models, each in every LLM role of its run: `gpt-4o-mini` (OpenAI), Qwen2.5-7B-Instruct (OpenRouter,
  pinned to Phala), Llama-3.1-8B-Instruct (OpenRouter, pinned to CoreWeave bf16, fallbacks off).
  Embedding `text-embedding-3-small`, 1536 dimensions, the same for all three.
- Triage: category is an enum of nine values in the strict schema, 400-token budget (ADR-0018).
- Concurrency 2.
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

## 7. Results — TO FILL after all three runs

| Model | RUN | Cases scored / planned (C0, C3) | Cost (USD) | Wall time |
|---|---|---|---|---|
| gpt-4o-mini | `2026-11-10-gpt4omini-v4` | | | |
| Qwen2.5-7B | | | | |
| Llama-3.1-8B | | | | |

Per model: email ASR C0 / C3, RAG ASR C0 / C3 (headline 583, and per source), synthetic ASR apart,
FPR and utility on Enron, McNemar p, stop-stage bars, cases excluded from n by error kind,
`not recognized` and `Unterminated string` counts (ADR-0018), guard calls per job (mean, max).

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
