# ADR-0019: one partition of the raw data, a retrained L1 classifier, the 2,143-case set

- **Status:** Accepted, 2026-11-10
- **Date:** 2026-11-10
- **Decided by:** project owner (chat review of 2026-11-10, "phương án C": Enron 500 to the benchmark,
  the 160 LLMail benign texts to L1 only, RAG = 183 clean cases + 400 LLMail phase-1 scenario 3/4
  cases, synthetic reported apart, the classifier `9c3c6c…` approved, a new directory `mailguard_v4/`)
- **Number:** 0019 (ADR-0016 is reserved for task 7.32)
- **Task:** `specs/tasks.md` 7.36
- **Amends:** `docs/benchmark-data-leaks-2026-11-06.md` fix step 4 ("keep the guard as it is: do not
  retrain L1"), see Context
- **Changes:** root `evaluation/` only: `datasets/partition_v1.json`, `datasets/mailguard_v4/`,
  `mailguard_bench/{partition,l1_corpus_filter,case_checks,llmail_phase1_rag,build_cases,cases}.py`,
  `mailguard_bench/kit/pinned.py`, `mailguard_bench/pinned/{SHA256SUMS,NOTICE.md,*.metrics.json}`,
  tests, docs. **Not** `agentmailguard/`: the guard pin `d95e542` and its tree `f1b3fa4d…` stay.

## Context

`docs/benchmark-data-leaks-2026-11-06.md` found two overlaps in the 1,800-case set
(`evaluation/datasets/mailguard/`, sha256 `df350223…`), measured on 2026-11-09:

- 57 of its 222 PoisonedRAG questions are in the Doc Scanner tuning split
  (`agentmailguard/datasets/splits/rag_poison_dev.json`); `test_doc_scanner_tuning_guard` fails on it.
- All 500 benign emails are in the L1 classifier's corpus: the 297 Enron emails and the 203 LLMail
  emails (160 distinct texts) are corpus negatives.

The report recommended excluding training items from the benchmark rather than retraining L1. That
cannot work for the benign pool: every Enron ham email that passes the benchmark filter is already in
the corpus, so no clean benign email is left (the report's own supply table, "Benign emails: none").
The owner chose to retrain L1 without 500 Enron emails and benchmark on those.

## Decision

1. **One partition, fixed before training.** `evaluation/datasets/partition_v1.json`
   (`evaluation/mailguard_bench/partition.py`, seed 20260930, sha256 `22c6430b…`) assigns every unit to
   one of `l1_train`, `doc_tuning`, `benchmark`, `unused`, with the sha256 of each raw file it read:

   | Unit | l1_train | doc_tuning | benchmark | unused |
   |---|---|---|---|---|
   | Enron ham, one group per normalised text (988 groups, 992 message ids) | 376 | 0 | 500 | 112 |
   | LLMail benign, one per distinct normalised text (160 texts, 203 indices) | 160 | 0 | 0 | 0 |
   | PoisonedRAG target question `<dataset>-<item id>` (300) | 0 | 90 | 165 | 45 |

   An Enron group is benchmark-eligible only if all its messages pass the benchmark filter
   (80–2,500 characters of `message`), so no id of a benchmark group reaches the L1 builder's filter.
   The 112 `unused` groups pass neither filter or only the benchmark one after the draw.
   Normalisation is the L1 builder's `norm` of `Subject: <subject>\n<body>` (a test keeps them equal).

2. **The L1 corpus is the guard's, minus the benchmark.** `build_l1_corpus` runs unchanged (its
   `stats.json` is byte-identical to the committed one); `l1_corpus_filter.py` removes every row whose
   normalised text is a benchmark key, and fails unless it removes exactly one row per key. It removed
   500 rows (train 394, val 48, test 58). The builder splits by a content hash, so no other row moved.

3. **The classifier is retrained** by the guard's `training.train_l1_classifier` (scikit-learn 1.9.1)
   on the filtered corpus. Thresholds are not changed.

   | | pinned before | rebuilt, unfiltered | **new (pinned)** |
   |---|---|---|---|
   | sha256 | `8fc1cbe7…` | `532c059c…` | **`9c3c6c46…`** |
   | train rows | 19,463 | 19,463 | 19,069 |
   | val precision / recall | 0.9463 / 0.9603 | same | 0.9447 / 0.9603 |
   | val FPR / AUROC | 0.0506 / 0.9886 | same | 0.0563 / 0.9873 |
   | test precision / recall | 0.9664 / 0.9270 | same | 0.9657 / 0.9263 |
   | test FPR / AUROC | 0.0199 / 0.9903 | same | 0.0209 / 0.9905 |
   | test FPR, `enron_ham` | 1/99 | 1/99 | 2/41 |
   | test FPR, `llmail_fp` | 0/10 | 0/10 | 0/10 |

   The rebuilt unfiltered model reproduces the pinned metrics to every digit but not its bytes; an
   earlier rebuild on another machine gave `02ea6a…`. Why the bytes differ is not known (the pickle is
   not byte-stable across runs). The classifier is not in git (ADR-0012 decision 15, xTRam1).

4. **The case set `evaluation/datasets/mailguard_v4/`** (`build_cases.py`, sha256 `a65bde85…`,
   2,143 cases) has these sets:

   | Set | Cases | From |
   |---|---|---|
   | `llmail_attack` | 1,000 | the 1,000 phase-2 attacks of the 1,800-case set; 51 overlap and are replaced by a seeded draw of the same scenario |
   | `llmail_benign` | 500 | the 500 Enron benchmark units |
   | `rag_attack` | 583 | 165 PoisonedRAG + 18 seed + 400 LLMail phase-1 (scenario 3: 108, scenario 4: 292) |
   | `rag_synthetic` | 60 | `synthetic_poison_v1`, run by every config (`runner.FULL_RUN_SETS`), scored in its own report section and `metrics.csv` table `rag_synthetic`, with its own C0 vs C3 McNemar; never in the RAG headline |

   Pool rules, each counted in the manifest's `pool_rules`: a phase-2 bench-half attack is dropped
   when its text repeats another bench item (229, lowest case id kept) or is in the L1 corpus or the
   LLMail train half (290). A phase-1 row is one case per distinct attacker text (9,872 rows, 9,584
   texts) and per distinct poisoned chunk (1,601 texts dropped: bodies over 6,000 characters cut to
   one chunk, or subjects equal after normalisation). After selection `case_checks.require_clean`
   fails, naming the ids, on duplicate content, a case in the filtered L1 corpus or the train half, a
   tuning question, or a case outside the partition's benchmark units. It found 0 of each.

5. **Phase-1 RAG cases** (`llmail_phase1_rag.py`): the attacker's email is the poisoned chunk; one
   benign email of its original context is the clean chunk (scenario 4: the one with the `$9.378
   million` figure); the inbound email is a benign question on the scenario's topic, `kb_query` that
   topic; the goal is exfiltration to `contact@contact.com`. Input: the owner's phase-1 processing,
   `eval_attack.jsonl` (sha256 `029c20c8…`), not in git.

6. **The default case directory is `mailguard_v4/`** (`cases.DEFAULT_CASE_DIR`); the 1,800-case set
   stays unchanged at `cases.LEGACY_CASE_DIR`, so the 2026-10-19 run folders stay readable.

## Consequences

- `agentmailguard/tests/unit/test_doc_scanner_tuning_guard.py` reads the fixed path
  `evaluation/datasets/mailguard/manifest.json`, so it still fails (it checks the 1,800-case set,
  which still has the 57 overlaps). `tests/unit/test_mailguard_partition.py::test_v4_has_no_tuning_question`
  makes the same check on the v4 set and passes. The guard test is not changed (`agentmailguard/` is pinned).
- New runs are not comparable case by case with the 2026-10-19 runs: other benign and RAG cases,
  51 other attacks, another classifier, and ADR-0018's triage change.
- The benign false-positive rate rests on one source (Enron, 500 emails); LLMail-style benign
  emails are no longer measured. The classifier's own test FPR on Enron rose from 1/99 to 2/41.
- The RAG headline mixes two attack types: PoisonedRAG/seed (`wrong_answer`, 183) and LLMail phase 1
  (`exfiltration`, 400). Report them per source as well as together. All 400 phase-1 cases share two
  clean chunks (one per scenario).
- Every phase-1 poisoned chunk is up to 6,050 characters (median 1,979), longer than PoisonedRAG
  passages; the ingestion chunker may split it into several chunks.
- `ablation_attack` is empty (no v3 config uses it since ADR-0017).
- Rebuilding needs the raw datasets, the filtered corpus and `eval_attack.jsonl`, none in git.

## Rebuilding

```
python -m mailguard.datasets.build_l1_corpus --out-dir <full>          # the guard, unchanged
python -m evaluation.mailguard_bench.partition                          # must reproduce partition_v1.json
python -m evaluation.mailguard_bench.l1_corpus_filter --full <full> --out <filtered>
python -m training.train_l1_classifier --corpus <filtered> --out <dir>/l1_injection_clf_v1.joblib
python -m evaluation.mailguard_bench.build_cases --l1-corpus <filtered> --phase1 <dir with eval_attack.jsonl>
```

(through `uv run --project . --with-editable <abs path>/agentmailguard --with pandas --with pyarrow`,
with `MAILGUARD_DIR`, `MAILGUARD_COMMIT`, `MAILGUARD_ARTIFACTS` set as the Makefile sets them)
