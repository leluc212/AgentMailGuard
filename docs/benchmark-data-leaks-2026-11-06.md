# Benchmark data leaks in the 1,800-case set (2026-11-06)

**Status:** open. Found on `refactor/guard-v4` at `4c7f4dd`; nothing in this report is fixed yet.
**Affects:** the pinned case set `evaluation/datasets/mailguard/` (built by `790accb`, 2026-10-18) and
every result folder built on it (`evaluation/results/mailguard_bench/2026-10-19-*-v3-1800`).
**Does not affect:** the 550-case set and its results (`2026-10-17/18-*-v3-retrainedL1`) for the
first leak; the guard code (`agentmailguard/`, pin `d95e542`).

## Summary

The benchmark is meant to test AgentMailGuard on data it never learned from. Two pools of the
1,800-case set break that rule:

| # | Benchmark pool | Overlaps with | Size | How sure |
|---|---|---|---|---|
| 1 | PoisonedRAG RAG attacks | the Doc Scanner threshold tuning set | 57 of 222 cases | measured, and a guard test fails on it |
| 2 | Enron benign emails | the L1 classifier's training corpus | up to all 297 cases | inferred from the builders; the corpus is not committed |

Both make the reported numbers look better than the guard would do on unseen data: leak 1 the
RAG attack success rate, leak 2 the false-positive rate. Neither changes the email-attack headline
(LLMail-Inject attacks come from the half of LLMail that the classifier did not train on).

```
 raw sources                 used by the guard to learn       used by the benchmark to score
 ───────────                 ──────────────────────────       ──────────────────────────────
 PoisonedRAG (300 targets) ─▶ tuning set: 90 targets   ◀──57──▶ RAG attacks: 222 targets
 Enron ham (one file)      ─▶ L1 corpus: 876 emails    ◀─≤297─▶ benign: 297 emails
 LLMail benign (203)       ─▶ L1 corpus: 160 emails    ◀─known─▶ benign: 203 emails (caveat already in every report)
 LLMail attacks            ─▶ train half               ─────────  bench half: no overlap
```

## Leak 1: PoisonedRAG cases in the tuning set

**What.** The Doc Scanner quarantines a retrieved chunk when its score crosses thresholds. Those
thresholds were chosen on a tuning set of PoisonedRAG items,
`agentmailguard/datasets/splits/rag_poison_dev.json` (90 target questions, 3 styles: prefix,
paraphrase, mid). ADR-0015 decision 4 requires the benchmark to use different items.

**Cause.** The old builder took 50 PoisonedRAG questions per source dataset and the tuning split was
built to avoid exactly those (`excluded_pinned_items`: 89). Commit `790accb` set
`PRAG_PER_DATASET = 10**9` in `evaluation/mailguard_bench/build_cases.py` to take every valid
question. That pulled 57 tuning items into the benchmark.

**Evidence.**

- `agentmailguard/tests/unit/test_doc_scanner_tuning_guard.py::test_no_pinned_benchmark_case_is_in_the_dev_split`
  fails from `790accb` onward. The 1,800-case runs were made while it failed.
- Overlap count, reproducible from committed files:

  ```python
  import json
  m = json.load(open("evaluation/datasets/mailguard/manifest.json"))
  s = json.load(open("agentmailguard/datasets/splits/rag_poison_dev.json"))
  bench = {c.removeprefix("attack-prag-") for c in m["sets"]["rag_attack"] if c.startswith("attack-prag-")}
  dev = {f"{r['dataset']}-{r['item_id']}" for r in s["poisoned"]}
  print(len(bench), len(dev), len(bench & dev))   # 222 90 57
  ```

- On the 550-case set the same check gives 89 PoisonedRAG items and an overlap of 0.

**Effect on the published numbers.** The raw rows are not committed, so the effect was measured
with the per-case verdicts of the meaning-based reader
(`analysis/meaning__<config>.jsonl` in `2026-10-19-gpt4omini-v3-1800`). That reader is a different
scoring rule from the keyword rule of the headline, so treat the split as indicative:

| gpt-4o-mini, PoisonedRAG | Cases | C0 attack success | C3 attack success |
|---|---|---|---|
| Also in the tuning set | 57 | 5.3 % | 0.0 % |
| Never seen in tuning | 165 | 15.2 % | 5.5 % |
| All, as reported | 222 | 12.6 % | 4.1 % |

Every C3 failure on PoisonedRAG is among the unseen cases. The same split for Llama-3.1-8B and
Qwen2.5-7B has not been computed yet; their C3 RAG attack success (11.7 % and 40.3 % over all 300
RAG cases) is high enough that the leak is unlikely to change the conclusion for them.

## Leak 2: Enron benign emails in the L1 classifier corpus

**What.** `790accb` added 297 Enron ham emails to the benign pool so the false-positive rate would
no longer rest only on LLMail's 203 benign emails, which are known L1 training negatives (the
caveat printed in every report).

**Cause.** Both builders read the same file, `datasets/raw/enron_ham/test.jsonl`:

- `agentmailguard/mailguard/datasets/build_l1_corpus.py::load_enron` keeps ham of 60–4,000
  characters, asks for up to 3,000, and the corpus stats record 876 Enron rows
  (`datasets/processed/l1_injection/stats.json`). Asking for 3,000 and getting 876 means it took
  every email that passed its filter.
- `agentmailguard/mailguard/datasets/build_email_benchmark.py::enron_benign` keeps ham of 80–2,500
  characters, a subset of the same filter, and the benchmark takes 297 of them.

So every benchmark Enron email was in the classifier corpus unless deduplication removed it. About
80 % of that corpus is the training split.

**Evidence status.** Inferred from the two builders and the corpus stats; not counted row by row,
because the processed corpus is not committed and was not on the machine this was found on.
Confirm it on a machine with `datasets/processed/l1_injection/*.jsonl` by matching normalised email
text.

**Effect.** The C3 false-positive rate on benign emails (4.1 % to 8.3 % at the drafting step across
the three models) is likely optimistic on both benign sources. The classifier has seen every benign
email the benchmark scores it on.

## Recommended fix

Fix the method, not the 57 cases. The dataset is about to grow to 2,000 / 1,000 / 600, and every new
source will raise the same question.

1. **One partition file, fixed before any training or tuning.** A committed file assigns every raw
   item, by its source id, to exactly one use: L1 classifier training, Doc Scanner tuning,
   benchmark, or unused. Split by the unit an attack is built from (a PoisonedRAG target question,
   an email thread, an Enron message id), never by case: PoisonedRAG has five poisoned texts per
   question, and two of them in different buckets are still a leak.
2. **The case builder refuses overlap.** `build_cases.py` drops nothing silently: it fails, naming
   the ids, when a benchmark case is in another bucket. A test after the fact is not enough. The
   existing test failed for weeks while runs went ahead.
3. **One test checks every pair.** Extend the tuning-guard test to classifier-vs-benchmark and
   tuning-vs-benchmark for every source, not only PoisonedRAG.
4. **Keep the guard as it is.** Exclude tuning and training items from the benchmark rather than
   re-tuning the Doc Scanner or retraining L1. Re-tuning would free at most 57–90 PoisonedRAG
   questions. In exchange it changes the thresholds and the guard pin, makes every committed v3
   result describe an older guard, and still needs another independent source to tune on.
5. **Label the old results.** Keep the 1,800-case folders unchanged. In the paper and the slides,
   state that their RAG and false-positive numbers include the overlaps above, or quote the
   unseen-only figures.

### Supply for the planned 2,000 / 1,000 / 600 set

| Pool | Planned | Independent supply today | Gap |
|---|---|---|---|
| Email attacks | 2,000 | 10,517 on LLMail's benchmark half | none |
| Benign emails | 1,000 | none: LLMail benign and Enron ham are both in the L1 corpus | about 1,000 |
| RAG attacks | 600 | 165 PoisonedRAG not in tuning, 18 seed | about 400 |

PoisonedRAG has 300 target questions in total, 90 of them in tuning, so no arrangement of it reaches
600. Options, each to be checked for licence and overlap before use:

- **Benign:** rows of a support-request dataset the classifier did not use. The L1 corpus used
  2,902 Bitext rows; whether the dataset has enough other rows, and how realistic they are as
  emails, still needs checking.
- **RAG:** new PoisonedRAG-style targets generated with the paper's method on more questions (an
  API cost, no local model); or another public indirect-injection dataset. BIPIA is already in the
  L1 corpus (145 rows) and would need the same exclusion.
- **Synthetic (`synthetic_poison_v1`):** cap it and report it separately, outside the headline. Its
  60 cases are 6 policies × 10 fixed templates with phrases the guard's rules target.
- **Fallback:** if no new source is acceptable, keep the RAG pool near 200 independent cases and say
  so in the report.

## Decisions needed

1. Where the extra benign and RAG cases come from, or whether to accept smaller independent pools.
2. Whether the published 1,800-case numbers are restated with the unseen-only figures or labelled.

The partition file, the builder check and the test (fix steps 1–3) do not depend on these
decisions. Building them needs the raw datasets (`agentmailguard/datasets/raw/`), which are not
committed.

## Other issues at `4c7f4dd` (found in the same scan)

- `tests/unit/test_kit_docs.py::test_the_stated_run_times_follow_from_the_smoke_measurements`
  still expects 550 cases.
- `make fmt-check` fails on `evaluation/mailguard_bench/cases.py` and two scripts under
  `paper/figures/audit-2026-10-26/`; `make lint` fails on the same two scripts (99 ruff errors,
  mostly in `build.py`).
- mypy reports three missing return annotations in `tests/unit/test_mailguard_expanded_cases.py`.
- ADR-0015 decision 12 states at most five guard model calls per job; the live runs record up to 9,
  because each scanned chunk counts as one call (`guard_calls_max` in `metrics.csv`).
