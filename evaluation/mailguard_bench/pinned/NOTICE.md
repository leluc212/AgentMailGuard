# Pinned benchmark inputs: sources and licenses

The case file, its manifest and the classifier's metrics are committed, so nobody has to rebuild
them (`make mailguard-cases` is not needed to run the v2 benchmark). **The L1 classifier is not in
git and is not redistributed** (see "Why the classifier is not in git" below): the owner sends it to
the teammate privately, and it goes in this folder. `SHA256SUMS` lists every file below, the
classifier included; `cd evaluation/mailguard_bench/pinned && sha256sum -c SHA256SUMS` checks them
once the classifier is here, and `make bench-doctor` does the same and names what is missing.

| File | In git | What it is |
|---|---|---|
| `evaluation/datasets/mailguard/cases.jsonl` | yes | The 550 benchmark cases (300 LLMail-Inject attack emails, 150 benign emails and 100 RAG-poisoning cases), seed 20260930. sha256 `c00dddca6336df91bcf80de7904ad5a8335564ababd8618c7b1d953a23811d19`. |
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
| LLMail-Inject challenge, phase 2 (Microsoft; Abdelnabi et al., 2025) | 300 attack emails, 150 benign emails | MIT | https://huggingface.co/datasets/microsoft/llmail-inject-challenge |
| PoisonedRAG (Zou et al., USENIX Security 2025) | 89 poisoned-passage cases | MIT for the attack passages; the questions inside them come from HotpotQA (CC BY-SA 4.0), Natural Questions (CC BY-SA 3.0) and MS MARCO (non-commercial research only), see "The question text" below | https://github.com/sleeepeer/PoisonedRAG |
| AgentMailGuard seed knowledge base and poison templates (written for this project) | 11 cases | the project's own (AgentMailGuard `pyproject.toml`: MIT) | https://github.com/leluc212/AgentMailGuard |

The attack emails are the attacks that Microsoft's challenge participants submitted; they are data
for measuring a defense and are never sent anywhere (the benchmark only drafts replies).

`manifest.json` records `mailguard_commit` 81df5d07b15b5bb3d1ecf3aae556df01e304cbe0: the guard
commit whose builder drew the cases (the v1 guard). The case set is the same for v1 and v2.

## The L1 classifier (not in git)

Trained by the guard's `training.train_l1_classifier` from the corpus that
`mailguard.datasets.build_l1_corpus` builds (24,382 texts; 19,463 train rows). It was built on
2026-09-29 at the v1 guard commit 81df5d07b15b5bb3d1ecf3aae556df01e304cbe0: that day's Makefile
target `mailguard-prep` pinned that commit and ran exactly these two commands, and the metrics file
reports `train_rows` 19463 and version `l1_injection_clf_v1`. The training code
(`training/`, the classifier and `mailguard/datasets/`) is identical at the v2 guard commit
1a3ef62b7368703c22c3f90111abdde0678d5617, so either commit builds the same kind of model.
The file is a joblib pickle: load it only with scikit-learn 1.9.1 (`make bench-doctor` refuses
any other version). The LLMail-Inject texts in its corpus come from the half of the challenge
data that is not used by the benchmark cases.

Training corpus sources (from the guard's registry):

| Source | License | Link |
|---|---|---|
| deepset/prompt-injections | Apache-2.0 | https://huggingface.co/datasets/deepset/prompt-injections |
| jackhhao/jailbreak-classification | Apache-2.0 | https://huggingface.co/datasets/jackhhao/jailbreak-classification |
| xTRam1/safe-guard-prompt-injection | **none declared** (see below) | https://huggingface.co/datasets/xTRam1/safe-guard-prompt-injection |
| Lakera/gandalf_ignore_instructions | MIT | https://huggingface.co/datasets/Lakera/gandalf_ignore_instructions |
| TrustAIRLab/in-the-wild-jailbreak-prompts | MIT | https://huggingface.co/datasets/TrustAIRLab/in-the-wild-jailbreak-prompts |
| LLMail-Inject phase 2 (training half) | MIT | https://huggingface.co/datasets/microsoft/llmail-inject-challenge |
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
`attack-prag-hotpotqa-*` (30 cases), `attack-prag-nq-*` (29) and `attack-prag-msmarco-*` (30), and
each poisoned passage in them starts with the question text (for example "what day is groundhog's
day?"). The PoisonedRAG MIT license above covers the attack passages; it does not cover the question
text, which keeps the terms of its own dataset. Read on 2026-10-01 from the datasets' own pages:

| Dataset | Questions in the cases | Terms | Where read |
|---|---|---|---|
| HotpotQA (Yang et al., EMNLP 2018) | 30 | CC BY-SA 4.0: "HotpotQA is distributed under a CC BY-SA 4.0 License." Attribution: cite Yang et al. (2018), *HotpotQA: A Dataset for Diverse, Explainable Multi-hop Question Answering*, EMNLP 2018. | https://hotpotqa.github.io/ |
| Natural Questions (Kwiatkowski et al., TACL 2019) | 29 | CC BY-SA 3.0 (the Hugging Face dataset card's Licensing Information: "Creative Commons Attribution-ShareAlike 3.0 Unported"; the Google code repository itself is Apache-2.0, which covers the code and not the data). Attribution: cite Kwiatkowski et al. (2019), *Natural Questions: a Benchmark for Question Answering Research*, TACL. | https://huggingface.co/datasets/google-research-datasets/natural_questions |
| MS MARCO (Microsoft) | 30 | Non-commercial research only: "The MS MARCO datasets are intended for non-commercial research purposes only to promote advancement in the field of artificial intelligence and related areas, and is made available free of charge without extending any license or other intellectual property rights." | https://microsoft.github.io/msmarco/ |

What this means for the repository: the CC BY-SA terms ask for attribution (above) and, if the text
is redistributed in an adapted form, for the same license on the adaptation; the MS MARCO terms
allow non-commercial research use only. This benchmark is a research measurement, which is the use
MS MARCO's terms allow, but **a public repository, or any use beyond research, is not covered by
what was read**. The cases are short questions plus generated passages, not the datasets' documents,
but this notice does not assess whether that is a substantial part of the source. **The owner has to
decide** whether the 89 cases stay in a repository that may become public; if they do not, the
owner ships them in the private bundle like the classifier and the kit's case check is pointed at
that file (recorded as an open item in `specs/tasks.md`, task 7.25). Until then they stay committed
as before, for research use by the owner's team.

The third source, the AgentMailGuard seed knowledge base and poison templates, was written for this
project (MIT, AgentMailGuard's `pyproject.toml`).
