# Pinned benchmark inputs: sources and licenses

These files are committed so that nobody has to rebuild them (`make mailguard-prep` and
`make mailguard-cases` are not needed to run the v2 benchmark). `SHA256SUMS` lists every
file below; `cd evaluation/mailguard_bench/pinned && sha256sum -c SHA256SUMS` checks them, and
`make bench-doctor` does the same.

| File | What it is |
|---|---|
| `evaluation/datasets/mailguard/cases.jsonl` | The 550 benchmark cases (300 LLMail-Inject attack emails, 150 benign emails and 100 RAG-poisoning cases), seed 20260930. |
| `evaluation/datasets/mailguard/manifest.json` | Which case ids make up each set, and how they were drawn. |
| `evaluation/mailguard_bench/pinned/l1_injection_clf_v1.joblib` | The Layer-1 injection classifier (TF-IDF word and character n-grams into a calibrated logistic regression), built with scikit-learn 1.9.1. |
| `evaluation/mailguard_bench/pinned/l1_injection_clf_v1.metrics.json` | Its validation and test metrics, written by the training script. |

## Where the benchmark cases come from

All case text is taken from the public datasets below by the guard's own builder functions
(`mailguard.datasets.build_email_benchmark`, `build_rag_poison`, selected by
`evaluation/mailguard_bench/build_cases.py`); the licenses are those recorded in the guard's
dataset registry (`mailguard/datasets/sources.py`, `SOURCES`); the first two were checked against
their pages on 2026-09-30.

| Source | Used for | License | Link |
|---|---|---|---|
| LLMail-Inject challenge, phase 2 (Microsoft; Abdelnabi et al., 2025) | 300 attack emails, 150 benign emails | MIT | https://huggingface.co/datasets/microsoft/llmail-inject-challenge |
| PoisonedRAG (Zou et al., USENIX Security 2025) | 89 poisoned-passage cases | MIT | https://github.com/sleeepeer/PoisonedRAG |
| AgentMailGuard seed knowledge base and poison templates (written for this project) | 11 cases | the project's own (AgentMailGuard `pyproject.toml`: MIT) | https://github.com/leluc212/AgentMailGuard |

The attack emails are the attacks that Microsoft's challenge participants submitted; they are data
for measuring a defense and are never sent anywhere (the benchmark only drafts replies).

`manifest.json` records `mailguard_commit` 81df5d07b15b5bb3d1ecf3aae556df01e304cbe0: the guard
commit whose builder drew the cases (the v1 guard). The case set is the same for v1 and v2.

## The L1 classifier

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

**xTRam1/safe-guard-prompt-injection declares no license** (checked 2026-09-30); its 10,127 rows
are 42% of the training corpus. The guard's registry says it is "used for training only, not
redistributed". The training texts are not shipped here; the classifier is a statistical model
whose vocabulary was learned partly from them. Whether shipping that model is acceptable is the
owner's decision, not something this file settles.

The raw datasets are not needed at run time: the benchmark reads only `cases.jsonl` and the
classifier (see the guide, "What the run needs").
