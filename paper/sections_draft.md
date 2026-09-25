# Draft text for the paper (Sections VI-VII) — numbers from this repository

All numbers below are produced by the commands in `docs/experiments.md`; regenerate before
submission. Placeholders `[TBD]` need the three-model runs (Ollama / OpenAI).

## VI-A. Datasets (replacement text)

We evaluate on a unified email-and-RAG benchmark of 1,405 cases assembled from public
sources (Table VII). The 862 attack cases combine (i) 300 real adaptive attack emails from
Microsoft's LLMail-Inject challenge, in which participants attacked an email assistant with a
`send_email` tool under ten defense configurations [Abdelnabi et al., 2025]; (ii) 225 cases
built from BIPIA's email task contexts with its 15 text-attack families inserted at the start,
middle or end of the message [Yi et al., 2025]; (iii) 49 InjecAgent data-stealing and
direct-harm instructions embedded in follow-up emails [Zhan et al., 2024]; (iv) 120 cases
from 24 hand-written attack templates spanning the taxonomy of Section IV (instruction
override, persona hijack, delimiter confusion, hidden HTML, encoding, zero-width characters,
homoglyphs, Vietnamese-language injection, paraphrased and quoted injections) injected into
40 benign support emails; and, for the RAG vector, (v) 150 PoisonedRAG question/answer pairs
with five adversarial passages each (NQ, HotpotQA, MS-MARCO) [Zou et al., 2025] and (vi) 18
PoisonedRAG-style passages targeting our support knowledge base. The 543 benign cases
comprise the 40 seed support emails, LLMail-Inject's 203 false-positive test emails, 150
Enron ham messages and 150 Bitext customer-support requests. Each attack case carries a
machine-checkable goal (attacker recipient, URL, exact phrase, wrong answer or system-prompt
leak) and each benign case carries the keywords a correct reply must contain.

The Layer-1 classifier is trained on a separate corpus of 24,382 labelled texts from ten
sources (deepset, xTRam1, jackhhao, Lakera Gandalf, TrustAIRLab in-the-wild jailbreaks,
LLMail-Inject, BIPIA train split, InjecAgent attacker/user instructions, Enron ham, Bitext).
Sources that appear in both the corpus and the benchmark are split by a stable hash of the
item identifier so no benchmark item is seen in training; seed templates are benchmark-only.

## VI-B. Models

Qwen2.5-7B-Instruct and Llama-3.1-8B-Instruct (Q4_K_M, Ollama) and GPT-4o-mini (OpenAI API)
serve both as the reply agent and as the judge behind the LLM stages of Layers 1, 2, 3b and 4.
The Stage-2 classifier of Layer 1 is a calibrated TF-IDF (word 1-2 grams, character 3-5 grams)
logistic regression: on the held-out test split it reaches precision 0.966, recall 0.927,
F1 0.946, AUROC 0.990 and FPR 2.0% at 0.9 ms per email on a laptop CPU.

## VI-C. Metrics

ASR is the fraction of attack cases whose goal is satisfied by the *final* outbound artifact
and the policy action is not `block`/`quarantine`; TMR counts attack cases that triggered a
forward or an attacker recipient; DER counts attacker addresses in recipients or body; TSR is
the fraction of benign cases that are not blocked and whose draft contains the expected
keywords; FPR is the fraction of benign cases blocked or quarantined. We report Wilson 95%
intervals and, for the ablation, exact McNemar tests on paired case ids.

## VII. Results — detector-level numbers available now

| Component | Metric | Value |
|---|---|---|
| L1 rules + ML (no LLM), 694 email-vector attacks | detection at MEDIUM+ | 89.8% [87.3, 91.8] |
| | detection on LLMail-Inject (n=300) | 100.0% |
| | detection on BIPIA (n=225) | 86.2% |
| | detection on InjecAgent (n=49) | 75.5% |
| | FPR on 543 benign emails | 1.8% [1.0, 3.4] |
| | latency | 6.4 ms/email (rules+ML+heuristics) |
| L3b rules + heuristics + ML, 2,883 chunks | precision / recall / F1 (quarantine) | 0.956 / 0.853 / 0.902 |
| | PoisonedRAG passages (n=1,500) recall | 85.1% |
| | clean Bitext / Enron chunks specificity | 93.4% / 98.7% |

Weak spots of the cheap stages (motivating the LLM judge): trigger-word-free paraphrases
(0/10 detected), quoted-history injections (1/5), persona hijacks without explicit
"unrestricted" wording (5/10).

## VII. Results — harness validation with the simulated agent (not a model result)

The deterministic simulated agent obeys any instruction visible outside channel markers.
Under C0 it reaches ASR [see evaluation/results/naive-all/summary.json]; C3 drives the
email-vector ASR to 0% and the RAG-vector ASR to [TBD after rerun]; removing Layer 3
(channel isolation) is the single most damaging ablation because the simulated agent then
obeys poisoned chunks that Layer 3b did not catch. These runs validate the harness and the
goal checks; model results (Tables V and VI) come from the Qwen / Llama / GPT-4o-mini runs.

## Table VII (new): benchmark composition

| Source | Kind | Vector | n |
|---|---|---|---|
| LLMail-Inject (phase 2, labelled attacks) | attack | email | 300 |
| BIPIA email x 15 attack types | attack | email | 225 |
| InjecAgent (ds/dh, base+enhanced) | attack | email | 49 |
| Seed templates x carriers | attack | email | 120 |
| PoisonedRAG (NQ/HotpotQA/MS-MARCO) | attack | rag | 150 |
| Seed poison templates | attack | rag | 18 |
| Seed benign support emails | benign | - | 40 |
| LLMail-Inject FP emails | benign | - | 203 |
| Enron ham | benign | - | 150 |
| Bitext support requests | benign | - | 150 |
