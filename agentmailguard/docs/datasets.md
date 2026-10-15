# Dataset cards

All sources are registered in `mailguard/datasets/sources.py`; `python -m
mailguard.datasets.download --manifest` prints the provenance table and
`datasets/raw/MANIFEST.json` records sha256 / size / URL / license / fetch time of every file.

| Name | Kind | Ref | License | Roles | Adaptation |
|---|---|---|---|---|---|
| deepset_prompt_injections | HF | deepset/prompt-injections | Apache-2.0 | l1_train | text/label as-is; official test split kept |
| jackhhao_jailbreak | HF | jackhhao/jailbreak-classification | Apache-2.0 | l1_train | prompt/type -> label |
| xtram1_safeguard | HF | xTRam1/safe-guard-prompt-injection | unspecified | l1_train | text/label as-is |
| lakera_gandalf | HF | Lakera/gandalf_ignore_instructions | MIT | l1_train | all positive |
| trustairlab_itw_jailbreak | HF | TrustAIRLab/in-the-wild-jailbreak-prompts (CCS'24) | MIT | l1_train | jailbreak -> 1, regular -> 0 (capped) |
| llmail_inject | HF | microsoft/llmail-inject-challenge | MIT | email_bench, l1_train | raw phase-2 submissions joined with labels; `attack_attempt == True`; hash-split train/bench; FP emails -> benign |
| injecagent | GitHub | uiuc-kang-lab/InjecAgent (ACL'24) | MIT | email_bench, l1_train | attacker instruction embedded in a follow-up email; data-stealing cases -> exfiltration goal (attacker email parsed) |
| bipia | GitHub | microsoft/BIPIA (KDD'25) | MIT | email_bench, l1_train | email contexts x text attacks at start/middle/end; train split -> L1 corpus, test split -> benchmark |
| poisonedrag | GitHub | sleeepeer/PoisonedRAG (USENIX Sec'25) | MIT | rag_poison | adversarial passages prefixed with the target question; goal = incorrect answer |
| enron_ham | HF | SetFit/enron_spam | public domain | benign | ham messages as benign emails / clean chunks |
| bitext_support | HF | bitext/Bitext-customer-support-llm-chatbot-training-dataset | CDLA-Sharing-1.0 | benign | instructions as benign requests; responses as clean KB chunks |
| seed | this repo | datasets/seed | MIT | all | 5 KB docs (26 chunks), 40 benign emails, 24 attack templates, 6 poison templates |

## Processed sets

* `datasets/processed/l1_injection/{train,val,test}.jsonl` (~20k rows): id, text, label, source, technique, split.
* `datasets/processed/email_bench/cases.jsonl`: `BenchCase` records (email, chunks, goal, expected keywords,
  attacker identity, technique, vector, source); `stats.json` has counts per source/kind/vector.
* `datasets/processed/rag_poison/chunks.jsonl`: chunk-level poison labels for L2 Doc Scanner evaluation (the v2 name of the layer was L3b).

## Leakage and licensing notes

* Items that could appear in both the L1 corpus and the benchmark (LLMail-Inject, InjecAgent)
  are split by `sha1(id) % 2`; BIPIA uses its own train/test files; seed attack templates are
  benchmark-only.
* xTRam1 has no declared license: it is used for training only and never redistributed.
  Raw data is git-ignored; only the download manifest and build scripts are versioned.
* LLMail-Inject phase-1 files (> 1 GB) are skipped; phase-2 (263 MB raw + 68 MB labels) is used.
* PoisonedRAG passages are used in the black-box form of the paper, `S = question + adversarial
  text`; the retrieval bait (question prefix) is what the L2 Doc Scanner's `echo_vs_question` and
  `ordered_prefix` signals key on (against the email's question sentences), so its recall on this
  source should be read as "detects the bait", not the misinformation itself. The ~85 % recall and the
  chunk-level numbers in `evaluation/results/detectors/l3b.json` are **v2 results** of the old L3b
  scanner (whole-body query echo, scheme v2); they are not recomputed here and are not v3 numbers. The
  v3 thresholds were chosen on `datasets/splits/rag_poison_dev.json` (schema v3, seed 20261014: 90
  PoisonedRAG items outside the benchmark pool and 600 clean Bitext chunks), never on the benchmark's
  pinned cases.
