# Pinned benchmark inputs: sources and licenses

## v4 pinned dataset and classifier (2026-11-10, ADR-0019, task 7.36)

The pinned case set is now `evaluation/datasets/mailguard_v4/` (2,143 cases, sha256
`a65bde85fb11c81b7492a6ca63a2ff117ac20c01e85632cd6c5c30fe33af5768`), built from
`evaluation/datasets/partition_v1.json` so that no case is in the L1 corpus, the LLMail train half or
the Doc Scanner tuning split:

| Set | Cases | Sources |
|---|---|---|
| `llmail_attack` | 1,000 | LLMail-Inject phase 2, bench half (949 of the 1,800-case set's attacks, 51 replaced) |
| `llmail_benign` | 500 | Enron ham (SetFit/enron_spam, public domain), the partition's benchmark units |
| `rag_attack` | 583 | 165 PoisonedRAG, 18 AgentMailGuard seed, 400 LLMail-Inject **phase 1** retrieval attacks (scenarios 3 and 4) |
| `rag_synthetic` | 60 | `synthetic_poison_v1`, reported apart from `rag_attack` |

The LLMail-Inject benign emails (`emails_for_fp_tests.json`) are no longer benchmark cases: all
160 distinct texts stay in the L1 corpus (owner, 2026-11-10). The phase-1 cases come from the
same Microsoft dataset (MIT, notice below), processed by the owner (`eval_attack.jsonl`, not in git).
The PoisonedRAG question terms below now apply to 165 cases (HotpotQA, Natural Questions, MS MARCO).

The classifier `l1_injection_clf_v1.joblib` is retrained without the benchmark's 500 Enron emails
(19,069 train rows); its sha256 must be `9c3c6c469779cfc5038aa971a31021c5a56f963bd1606f48274053cbe5efbfb6`.
It is still not in git, for the reason in "Why the classifier is not in git". Its metrics are in
`l1_injection_clf_v1.metrics.json`; ADR-0019 compares them with the earlier classifier.

The sections below describe the 1,800-case set (`evaluation/datasets/mailguard/`, still in git,
no longer pinned) and the earlier classifier (`8fc1cbe7…`); their source and license notices apply
to the v4 set as well.

## Expanded v3 pinned dataset (2026-10-18)

Each config now runs 1,800 cases: 1,000 LLMail bench-half attacks, 500 benign
emails (all 203 LLMail FP emails plus 297 seeded Enron ham emails), and 300
poisoned-document cases (222 valid PoisonedRAG questions, 18 seed cases, and
60 explicitly labeled `synthetic_poison_v1` cases). Synthetic cases use six
fictional policies and ten instruction payload templates; they are not independent
real-world observations. Source labels remain available for separate reporting.
The historical manifest key `llmail_benign` now includes both benign sources.
The 100-case `ablation_attack` subset is legacy metadata; v3 runs the full set.

Nine default configs give 16,200 case runs per model; optional C0T raises this to
18,000. Earlier 550-case timing/cost estimates below describe the old dataset
and should not be used for this expansion. Existing run folders keep their own pins;
use a new RUN for this dataset. The Task 16 clean-document benign set remains separate.

That set was built by `build_cases.py` at `4957774`; the builder at HEAD builds the v4 set (ADR-0019).

The case file, its manifest and the classifier's metrics are committed, so nobody has to rebuild
them (`make mailguard-cases` is not needed to run the v2 benchmark). **The L1 classifier is not in
git and is not redistributed** (see "Why the classifier is not in git" below): the owner sends it to
the teammate privately, and it goes in this folder. `SHA256SUMS` lists every file below, the
classifier included; `cd evaluation/mailguard_bench/pinned && sha256sum -c SHA256SUMS` checks them
once the classifier is here, and `make bench-doctor` does the same and names what is missing.

| File | In git | What it is |
|---|---|---|
| `evaluation/datasets/mailguard/cases.jsonl` | yes | The 1,800 benchmark cases (1,000 LLMail attacks, 500 mixed-source benign emails and 300 RAG-poisoning cases), seed 20260930. sha256 `df35022389823454700c8ed0916189807320a8c1274ca0e5d1a2945685b64870`. |
| `evaluation/datasets/mailguard/manifest.json` | yes | Which case ids make up each set, and how they were drawn. |
| `evaluation/mailguard_bench/pinned/l1_injection_clf_v1.joblib` | **no** (git-ignored) | The Layer-1 injection classifier (TF-IDF word and character n-grams into a calibrated logistic regression), built with scikit-learn 1.9.1. Its sha256 must be `8fc1cbe74a599ab870a10ca5ff43f4a6d80b3e2273e36c7ed163c637a1d40103`; the kit refuses a file with any other. |
| `evaluation/mailguard_bench/pinned/l1_injection_clf_v1.metrics.json` | yes | Its validation and test metrics, written by the training script (numbers only, no training text). |

## Where the benchmark cases come from

All case text is taken from the public datasets below by the guard's own builder functions
(`mailguard.datasets.build_email_benchmark`, `build_rag_poison`, selected by
`evaluation/mailguard_bench/build_cases.py`); the licenses are those recorded in the guard's
dataset registry (`mailguard/datasets/sources.py`, `SOURCES`); the first two were checked against
their pages on 2026-09-30 and their license files were read again on 2026-10-01 (next section).

| Source | Used for | License | Link |
|---|---|---|---|
| LLMail-Inject challenge, phase 2 (Microsoft; Abdelnabi et al., 2025) | 1,000 attack emails, 203 benign emails | MIT | https://huggingface.co/datasets/microsoft/llmail-inject-challenge |
| PoisonedRAG (Zou et al., USENIX Security 2025) | 222 poisoned-passage cases | MIT for the attack passages; the questions inside them come from HotpotQA (CC BY-SA 4.0), Natural Questions (CC BY-SA 3.0) and MS MARCO (non-commercial research only), see "The question text" below | https://github.com/sleeepeer/PoisonedRAG |
| AgentMailGuard seed knowledge base and poison templates (written for this project) | 18 cases | the project's own (AgentMailGuard `pyproject.toml`: MIT) | https://github.com/leluc212/AgentMailGuard |

The attack emails are the attacks that Microsoft's challenge participants submitted; they are data
for measuring a defense and are never sent anywhere (the benchmark only drafts replies).

The expanded manifest records the v3 guard builder commit `d95e542c616ca9ef4fee067a0862816004fffac3`. Historical v1/v2 runs retain their own manifests.

The additional 297 Enron ham emails use the Enron source listed below. The 60
`synthetic_poison_v1` cases are project-authored fictional policies and instruction payloads.

## The L1 classifier (not in git)

Trained by the guard's `training.train_l1_classifier` from the corpus that
`mailguard.datasets.build_l1_corpus` builds (24,382 texts; 19,463 train rows). It was built on
2026-09-29 at the v1 guard commit 81df5d07b15b5bb3d1ecf3aae556df01e304cbe0: that day's Makefile
target `mailguard-prep` pinned that commit and ran exactly these two commands, and the metrics file
reports `train_rows` 19463 and version `l1_injection_clf_v1`. The training code
(`training/`, the classifier and `mailguard/datasets/`) is identical at the v2 guard commit
915cb1e2b86395e4389673deb9914308cc39627b (and at 1a3ef62, the v2 pin before ADR-0014), so either
commit builds the same kind of model.
The file is a joblib pickle: load it only with scikit-learn 1.9.1 (`make bench-doctor` refuses
any other version). Train and test are separate for the attack emails only: the LLMail-Inject
attacks in the corpus come from the half of the challenge data that the benchmark cases do not
use (disjoint by exact text; near-duplicates exist across the halves). The benign emails are not
separate: the corpus takes every email of the challenge's `emails_for_fp_tests.json` as a
negative (source `llmail_fp`), and the 150 benign benchmark cases are drawn from that same file.
A run's `analysis/leakage.json` counts the overlap at cosine 0.9 or more: in the published
2026-09-29-gpt4omini run, 138 of the 150 benign cases are near-duplicates of training rows, and
for the attacks 151 of 300 against the training half and 123 of 300 against the training rows the
classifier saw. The benign false-positive rate is therefore optimistic.

Training corpus sources (from the guard's registry):

| Source | License | Link |
|---|---|---|
| deepset/prompt-injections | Apache-2.0 | https://huggingface.co/datasets/deepset/prompt-injections |
| jackhhao/jailbreak-classification | Apache-2.0 | https://huggingface.co/datasets/jackhhao/jailbreak-classification |
| xTRam1/safe-guard-prompt-injection | **none declared** (see below) | https://huggingface.co/datasets/xTRam1/safe-guard-prompt-injection |
| Lakera/gandalf_ignore_instructions | MIT | https://huggingface.co/datasets/Lakera/gandalf_ignore_instructions |
| TrustAIRLab/in-the-wild-jailbreak-prompts | MIT | https://huggingface.co/datasets/TrustAIRLab/in-the-wild-jailbreak-prompts |
| LLMail-Inject phase 2 (attacks: training half; benign negatives: `emails_for_fp_tests.json`, see above) | MIT | https://huggingface.co/datasets/microsoft/llmail-inject-challenge |
| BIPIA (Microsoft) | MIT | https://github.com/microsoft/BIPIA |
| InjecAgent (UIUC Kang Lab) | MIT | https://github.com/uiuc-kang-lab/InjecAgent |
| SetFit/enron_spam (ham half; Enron corpus) | public domain (FERC release) | https://huggingface.co/datasets/SetFit/enron_spam |
| Bitext customer-support dataset | CDLA-Sharing-1.0 | https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset |

## Why the classifier is not in git

**xTRam1/safe-guard-prompt-injection declares no license** (checked 2026-09-30); its 10,127 rows
are 42% of the training corpus. The guard's registry says it is "used for training only, not
redistributed". The training texts are not shipped here, and the classifier is a statistical model
whose vocabulary was learned partly from them. ADR-0012 decision 15 (2026-10-01) therefore keeps
the classifier out of git and **not redistributed**: no commit, branch, release or public link holds
it. The owner sends `l1_injection_clf_v1.joblib` to the teammate privately; the teammate puts it in
`evaluation/mailguard_bench/pinned/` (the path is git-ignored, so `git add .` cannot pick it up).
This repository records only its sha256 (above and in `SHA256SUMS`), and `kit/pinned.py`, the
doctor and `make bench-setup` refuse a missing file or a file with another sha256.

The raw datasets are not needed at run time: the benchmark reads only `cases.jsonl` and the
classifier (see the guide, "What the run needs").

## License notices of the committed case file

`cases.jsonl` is committed. Its attack and benign emails come from Microsoft's LLMail-Inject
challenge data and its RAG-poisoning cases from PoisonedRAG; both are MIT-licensed (the question
text in the poisoning cases has its own terms, see the section after the two notices), and MIT asks that
the copyright and permission notice be included with copies. Both texts below were fetched from the
repositories' `LICENSE` files on 2026-10-01 and are reproduced verbatim (only the code-block
indentation of this Markdown is added).

### LLMail-Inject challenge (Microsoft)

Source of the text: https://github.com/microsoft/llmail-inject-challenge/blob/main/LICENSE (file
`LICENSE` of the `main` branch, git blob `9e841e7a26e4eb057b24511e7b92d42b257a80e5`). The attack
and benign emails themselves are read from the dataset
https://huggingface.co/datasets/microsoft/llmail-inject-challenge; its card declares `license: mit`
in its metadata and carries no copyright line of its own (the card points to the challenge
repository above as the accompanying code), so the repository's notice is the only one that exists
for it.

```text
    MIT License

    Copyright (c) Microsoft Corporation.

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE
```

### PoisonedRAG (Zou et al.; repository of Runpeng Geng)

Source of the text: https://github.com/sleeepeer/PoisonedRAG/blob/main/LICENSE (file `LICENSE` of the
`main` branch, git blob `445b053a0e2c2827732eb8ea8f16cbde4f92f597`). The poisoned passages come from
that repository's `results/adv_targeted_results/` files.

```text
MIT License

Copyright (c) 2024 Runpeng Geng

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### The question text inside the 89 PoisonedRAG cases (HotpotQA, Natural Questions, MS MARCO)

PoisonedRAG builds its poisoned passages for questions taken from three public question-answering
datasets, and every one of the 89 committed cases carries that question: the case ids are
`attack-prag-hotpotqa-*` (68 cases), `attack-prag-nq-*` (89) and `attack-prag-msmarco-*` (65), and
each poisoned passage in them starts with the question text (for example "what day is groundhog's
day?"). The PoisonedRAG MIT license above covers the attack passages; it does not cover the question
text, which keeps the terms of its own dataset. Read on 2026-10-01 from the datasets' own pages:

| Dataset | Questions in the cases | Terms | Where read |
|---|---|---|---|
| HotpotQA (Yang et al., EMNLP 2018) | 68 | CC BY-SA 4.0: "HotpotQA is distributed under a CC BY-SA 4.0 License." Attribution: cite Yang et al. (2018), *HotpotQA: A Dataset for Diverse, Explainable Multi-hop Question Answering*, EMNLP 2018. | https://hotpotqa.github.io/ |
| Natural Questions (Kwiatkowski et al., TACL 2019) | 89 | CC BY-SA 3.0 (the Hugging Face dataset card's Licensing Information: "Creative Commons Attribution-ShareAlike 3.0 Unported"; the Google code repository itself is Apache-2.0, which covers the code and not the data). Attribution: cite Kwiatkowski et al. (2019), *Natural Questions: a Benchmark for Question Answering Research*, TACL. | https://huggingface.co/datasets/google-research-datasets/natural_questions |
| MS MARCO (Microsoft) | 65 | Non-commercial research only: "The MS MARCO datasets are intended for non-commercial research purposes only to promote advancement in the field of artificial intelligence and related areas, and is made available free of charge without extending any license or other intellectual property rights." | https://microsoft.github.io/msmarco/ |

What this means for the repository: the CC BY-SA terms ask for attribution (above) and, if the text
is redistributed in an adapted form, for the same license on the adaptation; the MS MARCO terms
allow non-commercial research use only. This benchmark is a research measurement, which is the use
MS MARCO's terms allow; a public repository beyond that use is not covered by what was read. The
cases are short questions plus generated passages, not the datasets' documents, and the same text is
already public in the PoisonedRAG repository. The owner decided on 2026-10-01 that the 89 cases are
published in this repository, with the attribution above, for non-commercial research use
(ADR-0012 decision 17); the classifier's metrics file, numbers only, is published with them.

The third source, the AgentMailGuard seed knowledge base and poison templates, was written for this
project (MIT, AgentMailGuard's `pyproject.toml`).
