# AgentMailGuard benchmark: what reference evaluations report, and what to show the council on Wednesday

- **Date:** 2026-09-28 (all sources opened on this date)
- **Scope:** research brief only. It maps published practice onto the planned benchmark in `docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md` (C0 vs C3, 300 LLMail-Inject phase-2 attacks + 150 benign, ~100 RAG-vector cases, `gemma-4-26b-a4b-it`).
- **Sources:** primary only (arXiv papers, official model cards and dataset repos, OWASP, NIST, MITRE, MSRC). Every quote below is verbatim from the URL next to it.

---

## 0. The short answer

```
                    WHAT REVIEWERS EXPECT                          PLANNED BENCHMARK
 ┌──────────────────────────────────────────────┐   ┌──────────────────────────────────────────┐
 │ threat model (goals, knowledge, capability)  │──▶│ implicit in spec; needs one slide        │ MISSING (cheap)
 │ no-defence baseline                          │──▶│ C0                                        │ HAVE
 │ ≥1 published defence baseline                │──▶│ none                                      │ MISSING (cheap offline)
 │ per-layer ablation                           │──▶│ presets C1, C2, C3-Lx exist in code       │ NOT RUN (costly: API)
 │ security AND utility, side by side           │──▶│ ASR + FPR + benign "TSR"                  │ HAVE (rename TSR)
 │ held-out / unseen attacks, leakage control   │──▶│ sha1 split of phase 2                     │ PARTIAL (near-dups)
 │ adaptive attacks                             │──▶│ LLMail attacks are adaptive to OTHER guards│ MISSING (state as limit)
 │ CIs + paired test                            │──▶│ Wilson + McNemar                          │ HAVE
 │ per-category breakdown                       │──▶│ per scenario, per vector                  │ HAVE
 │ latency / tokens / cost                      │──▶│ p50/p95/p99, tokens, calls, cost          │ HAVE
 │ failure analysis with examples               │──▶│ not planned                               │ MISSING (cheap)
 │ limitations / threats to validity            │──▶│ partly in spec §7                         │ MISSING (cheap)
 └──────────────────────────────────────────────┘   └──────────────────────────────────────────┘
```

The plan already covers the *numbers* reviewers want (ASR, FPR, CIs, McNemar, per-scenario, overhead). What is missing is mostly *framing and honesty* (threat model, leakage check, adaptive-attack limitation, failure examples), which costs hours, not API quota. The two expensive gaps are the per-layer ablation and a published-defence baseline; both have cheap partial substitutes (Section 4).

---

## 1. How the reference evaluations report results

### 1.1 Summary table

| Work | Security metric | Utility / false-positive metric | Baselines & ablations | Adaptive / unseen attacks | Statistics | Cost / latency |
|---|---|---|---|---|---|---|
| **LLMail-Inject** (Abdelnabi et al., 2025) | "Tool Call" rate and "E2E Attack Success"; per-defence detection rate (recall); Team Success Rate | FPR on synthetic benign emails; thresholds set to 0 % FPR | per defence (Prompt Shield, TaskTracker, Spotlight, LLM Judge, All); ensembles of detectors | the whole dataset is adaptive human attacks; phase 2 adds a blocklist of phase-1 attacks | means ± std for submissions-before-success | not a focus |
| **AgentDojo** (Debenedetti et al., 2024) | Targeted ASR | Benign Utility; Utility Under Attack | no defence vs delimiters, BERT detector, sandwiching, tool filter | "any of a collection of attacks" = adaptive-attacker proxy | none reported as tests | suite cost in US$ |
| **InjecAgent** (Zhan et al., 2024) | ASR-valid and ASR-all; base vs enhanced (hacking-prompt) setting | valid-output rate | 30 agents; per attack type (direct harm vs data stealing, split into extraction and transmission) | enhanced setting | — | — |
| **BIPIA** (Yi et al., 2023/2025) | ASR | ROUGE of task output with/without defence | component ablation (remove explicit reminder / boundary awareness) | — | — | — |
| **Spotlighting** (Hines et al., Microsoft, 2024) | ASR | task performance on SQuAD, BoolQ, WiC, IMDB with/without transform | no defence → instructions only → delimiters → datamarking → encoding (incremental ablation) | notes delimiters can be subverted by an attacker who knows them | — | — |
| **StruQ / SecAlign** (Chen et al., 2024) | ASR, incl. optimisation-based GCG | AlpacaEval2 WinRate | undefended vs StruQ vs SecAlign | unseen, stronger attacks than training; GCG loss curves | std across samples | — |
| **CaMeL** (Debenedetti et al., Google DeepMind, 2025) | number of successful attacks (of 949) | utility vs native tool calling, utility under attack, policy trigger rate on benign runs | with vs without security policies; failure-mode categorisation | "provable" by construction | — | input/output token multipliers (2.82×/2.73×) |
| **Prompt Guard 2** (Meta, 2025) | AUC, Recall @ 1 % FPR | FPR fixed at 1 %; "APR @ 3 % utility reduction" on AgentDojo | vs Prompt Guard 1, ProtectAI, Deepset, LLM Warden | private OOD benchmark; admits adaptive-attack vulnerability | — | latency per classification (ms) |
| **PoisonedRAG** (Zou et al., 2024) | ASR (substring match, checked against human eval); retrieval precision/recall/F1 | — | defences: paraphrasing, perplexity, dedup, knowledge expansion | black-box vs white-box attacker | — | — |
| **Liu et al.** (USENIX Sec 2024) | ASV, MR | PNA; FPR/FNR for detection defences | 5 attacks × 10 defences × 10 LLMs × 7 tasks | combined attack | — | — |

### 1.2 Evidence per work

**LLMail-Inject** — https://arxiv.org/abs/2506.09956 (submitted 2025-06-11); full text https://arxiv.org/html/2506.09956
- Scenario: "participants adaptively attempted to inject malicious instructions into emails in order to trigger unauthorized tool calls in an LLM-based email assistant … 208,095 unique attack submissions from 839 participants."
- Success definition: "A submission was deemed to be _successful_ if the submitted email was retrieved … and the email was undetected by the relevant defense(s); and the tool was invoked with the appropriate arguments." (§2.4)
- Two reported rates: "the fraction of submissions that correctly called the send_email tool (denoted as "Tool Call") … [and] the fraction of submissions that correctly called the tool and were not detected, denoted as "E2E Attack Success"." (§4.1)
- **TSR in LLMail-Inject means Team Success Rate**: "the number of teams who solved a given sub-level as a fraction of the number of teams who attempted that sub-level" (§4.3). Table 1: Prompt Shield 0.600, TaskTracker 0.440, Spotlight 0.529, LLM Judge 0.318, All 0.166.
- False positives: "For Prompt Shield and TaskTracker, we used thresholds of 0.99, which had 0% false positive rates on the synthetic emails. LLM Judge also had a 0% false positive rate." (§4.5)
- Layering: "such combinations of defenses always outperform any of the individual component defenses." (§4.5)
- Phase 2 (the half this project uses): "We added a blocklist to block known attacks (and paraphrases thereof) from the main phase … We replaced spotlighting with defensive system prompt instructions and delimiters." (§2.5)
- Dataset files (HF `microsoft/llmail-inject-challenge`, API listing opened 2026-09-28): `emails_for_fp_tests.json`, `labelled_unique_submissions_phase1.json`, `labelled_unique_submissions_phase2.json`, `raw_submissions_phase{1,2}.jsonl`, `scenarios.json`, `system_prompt.json`.
- Lesson for this thesis: "We need benchmarks for end-to-end attacks." (§7)

**AgentDojo** — https://arxiv.org/abs/2406.13352 (2024-06-19, rev. 2024-11-24); https://arxiv.org/html/2406.13352
- §3.4: "**Benign Utility**: the fraction of user tasks that the model solves in the absence of any attacks. **Utility Under Attack**: the fraction of security cases … where the agent solves the user task correctly, without any adversarial side effects … **Targeted Attack Success Rate (ASR)**: the fraction of security cases where the attacker's goal is met."
- Adaptive proxy: "successful on a given security case if _any_ of the attacks in the collection succeeds. This metric models an adaptive attacker."
- Baseline defences (§4.3): "Data delimiters … Prompt injection detection which uses a BERT classifier … Prompt sandwiching … Tool filter."
- Cost (App. D): "running the full suite of 629 security test cases on GPT-4o costs around US$35."

**InjecAgent** — https://arxiv.org/abs/2403.02691 (2024-03-05); https://arxiv.org/html/2403.02691
- "1,054 test cases … We categorize attack intentions into two primary types: direct harm to users and exfiltration of private data … ReAct-prompted GPT-4 vulnerable to attacks 24% of the time … enhanced setting … nearly doubling the attack success rate."
- §3.2: "For data stealing attacks specifically, we provide a detailed breakdown of the success rates across two steps: data extraction and subsequent transmission." (Directly analogous to this project's ASR vs DER split.)

**BIPIA** — https://arxiv.org/abs/2312.14197 (2023-12-21, rev. 2025-01-27); https://arxiv.org/html/2312.14197
- §7.3: "We conduct an ablation study to evaluate the impact of our defense's two core components … the ASR of the black-box defenses increases when either of the two components is removed."
- §7.2 utility: "when examining the ROUGE score … with and without these defenses … the performance remains comparable to the original model."

**Spotlighting** — https://arxiv.org/abs/2403.14720 (2024-03-20); https://arxiv.org/html/2403.14720
- Abstract: "spotlighting reduces the attack success rate from greater than 50% to below 2% in our experiments with minimal impact on task efficacy."
- §V-A (adaptive caveat): "this kind of defense could be easily subverted by an attacker who gains knowledge of our system prompt and inserts their own delimiting."
- §V-B utility: "we quantified model performance … across a number of benchmark datasets, in the presence and absence of the datamarking transformation."

**StruQ** — https://arxiv.org/abs/2402.06363 (2024-02-09): "Our system significantly improves resistance to prompt injection attacks, with little or no impact on utility."
**SecAlign** — https://arxiv.org/abs/2410.05451 (2024-10-07, rev. 2025-07-03): "the first known method that reduces the success rates of various prompt injections to <10%, even against attacks much more sophisticated than ones seen during training." Full text §4.2: "The utility (WinRate) and security (ASR) of SecAlign compared to StruQ" and GCG "loss … shaded region shows standard deviation across samples."

**CaMeL** — https://arxiv.org/abs/2503.18813 (2025-03-24, rev. 2025-06-24); https://arxiv.org/html/2503.18813
- Abstract: "solving 77% of tasks with provable security (compared to 84% with an undefended system) in AgentDojo."
- Fig. 9: "the number of successful attacks (out of 949 attacks in total) … with the Tool Calling API and with CaMeL (both enforcing and not enforcing security policies)."
- §6.5: "CaMeL requires only 2.82× more input and 2.73× more output tokens than native tool-calling."
- §6.1.2 failure analysis: "we analyze the failure modes … We categorize failures and show the amount for each category."
- Fig. 10: policies "triggered during the benign and adversarial evaluations" (an over-blocking metric).

**Llama Prompt Guard 2** — https://github.com/meta-llama/PurpleLlama/blob/main/Llama-Prompt-Guard-2/86M/MODEL_CARD.md
- Metrics: "AUC (English) | Recall @ 1% FPR (English) | … | Latency per classification (A100 GPU, 512 tokens)"; 86M: ".998 | 97.5% | … | 92.4 ms".
- Held-out: "a private benchmark built with datasets distinct from those used in training Prompt Guard."
- Agentic: "APR @ 3% utility reduction": Prompt Guard 2 86M 81.2 %, ProtectAI 22.2 %.
- Limitation: "Vulnerability to Adaptive Attacks: … adversaries may develop sophisticated attacks specifically to bypass detection."

**ProtectAI deberta-v3-base-prompt-injection-v2** — https://huggingface.co/protectai/deberta-v3-base-prompt-injection-v2 (README): "Tested on 20,000 prompts from untrained datasets — Accuracy: 95.25% … Recall: 99.74%"; limitation: "It does not detect jailbreak attacks or handle non-English prompts." Apache-2.0 and ungated, so it can be run today.

**Llama Guard** — https://arxiv.org/abs/2312.06674 (2023-12-07): a content-safety classifier ("safety risk taxonomy … prompt classification … response classification"), **not** a prompt-injection detector. Cite it only to say why it is not the right baseline.

**PoisonedRAG** — https://arxiv.org/abs/2402.07867 (2024-02-12, rev. 2024-08-13); https://arxiv.org/html/2402.07867
- "PoisonedRAG could achieve a 90% attack success rate when injecting five malicious texts for each target question … several defenses … are insufficient."
- §7.1: "We report the ASR and F1-Score … F1-Score is higher when more malicious texts designed for a target question are retrieved."
- App. J: a "minor difference between human evaluation and substring matching in calculating ASRs" — i.e. string-match scoring needs a manual audit.

**Liu et al., Formalizing and Benchmarking Prompt Injection** — https://arxiv.org/abs/2310.12815 (2023-10-19, rev. 2025-11-12); https://arxiv.org/html/2310.12815
- §6.1: "Performance under No Attacks (PNA), Attack Success Value (ASV), and Matching Rate (MR) … To measure the performance of detection-based defenses, we further use False Positive Rate (FPR) and False Negative Rate (FNR)."

---

## 2. What a credible layered-defence evaluation must include

| # | Requirement | Primary source (quote) |
|---|---|---|
| R1 | **Threat model**: goals, knowledge, capability; assume attacker knows the defence | Carlini et al. 2019, https://arxiv.org/abs/1902.06705 (checklist §3.1): "State a precise threat model … The threat model assumes the attacker knows how the defense works. The threat model states attacker's goals, knowledge and capabilities." |
| R2 | **No-defence baseline and clean utility** | Carlini: "Report clean model accuracy when not under attack. For defenses that abstain or reject inputs, generate a ROC curve." AgentDojo "Benign Utility". |
| R3 | **Compare with published defences** | Carlini: "Compare against prior work and explain important differences … ensure it has not been broken." AgentDojo §4.3 and Prompt Guard 2 card both compare against named detectors. |
| R4 | **Per-component ablation** | BIPIA §7.3 (remove each component); Spotlighting incremental ablation; LLMail-Inject per-defence and ensemble tables. |
| R5 | **Security and utility together (trade-off)** | NIST AI 100-2 E2025, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-2e2025.pdf: "they should be evaluated simultaneously for any new mitigations, and mitigations should be compared on a Pareto plot." Prompt Guard 2: "APR @ 3% utility reduction". |
| R6 | **Adaptive / unseen attacks; no train–test leakage** | Nasr, Carlini et al. 2025, https://arxiv.org/abs/2510.09023: "we bypass 12 recent defenses … with attack success rate above 90% for most; importantly, the majority of defenses originally reported near-zero attack success rates." Lesson #1: "Small static evals can be misleading! … existing static datasets are extremely limited, creating a high risk of overfitting." Zhan et al. 2025, https://arxiv.org/abs/2503.00061: "we evaluate eight different defenses and bypass all of them using adaptive attacks, consistently achieving an attack success rate of over 50%." NIST E2025: "Mitigations need to be evaluated against strong adaptive attacks" and "new mitigations should be tested adversarially, with the researchers proposing the mitigation also trying to break it." |
| R7 | **Confidence intervals, paired significance test** | Brown, Cai & DasGupta 2001, Statistical Science 16(2), https://projecteuclid.org/journals/statistical-science/volume-16/issue-2/Interval-Estimation-for-a-Binomial-Proportion/10.1214/ss/1009213286.full: "We recommend the Wilson interval or the equal-tailed Jeffreys prior interval for small n." Dietterich 1998, Neural Computation 10(7), https://mlanthology.org/neco/1998/dietterich1998neco-approximate/: "McNemar's test, is shown to have low type I error." (None of the PI papers above report significance tests — doing so is a genuine plus.) |
| R8 | **Per-category breakdown** | InjecAgent (direct harm vs data stealing; extraction vs transmission); LLMail-Inject per retrieval level and LLM; CaMeL per suite. |
| R9 | **Latency, tokens, cost** | CaMeL §6.5 token multipliers; Prompt Guard 2 ms per classification; AgentDojo US$ per suite. |
| R10 | **Failure analysis with examples** | CaMeL §6.1.2 failure categories; LLMail-Inject §7 quotes a successful "declarative sentence" attack; PoisonedRAG App. J worked examples. |
| R11 | **Limitations / threats to validity** | Prompt Guard 2 "Limitations"; Spotlighting's own subversion caveat; Carlini: "Evaluations which intentionally deviate from the advice here may wish to justify the decision to do so." |
| R12 | **Describe attacks fully and release code** | Carlini: "Describe the attacks applied, including all hyperparameters" and "Release pre-trained models and source code." |

---

## 3. Framing documents and exact identifiers

| Framework | Identifier | Source and quote |
|---|---|---|
| **OWASP Top 10 for LLM Applications 2025** | **LLM01:2025 Prompt Injection** | https://genai.owasp.org/llmrisk/llm01-prompt-injection/ — "Indirect prompt injections occur when an LLM accepts input from external sources, such as websites or files." Mitigations map one-to-one onto the layers: "Implement input and output filtering" (L1, L4), "Segregate and identify external content" (L3), "Require human approval for high-risk actions" (L5 + rag-email's human review), "Enforce privilege control and least privilege access" (L5), "Conduct adversarial testing and attack simulations" (the benchmark). Also: "RAG … and fine-tuning … do not fully mitigate prompt injection vulnerabilities." |
| **NIST AI 100-2 E2025** "Adversarial Machine Learning: A Taxonomy and Terminology of Attacks and Mitigations" | **NISTAML.018** Prompt Injection; **NISTAML.015** Indirect Prompt Injection; **NISTAML.013** Data Poisoning (for poisoned KB) | https://csrc.nist.gov/pubs/ai/100/2/e2025/final — "Date Published: March 2025"; authors "Apostol Vassilev (NIST), Alina Oprea (Northeastern University), Alie Fordyce (Cisco), Hyrum Anderson (Cisco), Xander Davies (U.K. AI Security Institute)…". IDs from the PDF index. |
| **MITRE ATLAS** (data v5.6.0, `ATLAS.yaml`) | **AML.T0051** LLM Prompt Injection, **AML.T0051.000** Direct, **AML.T0051.001** Indirect; **AML.T0070** RAG Poisoning; **AML.T0066** Retrieval Content Crafting; **AML.T0057** LLM Data Leakage. Mitigations: **AML.M0020** Generative AI Guardrails, **AML.M0015** Adversarial Input Detection, **AML.M0029** Human In-the-Loop for AI Agent Actions, **AML.M0033** Input and Output Validation for AI Agent Components | https://raw.githubusercontent.com/mitre-atlas/atlas-data/main/dist/ATLAS.yaml — AML.T0051.001: "An adversary may inject prompts indirectly via separate data channel ingested by the LLM such as include text or multimedia pulled from databases or websites." AML.T0070: "Adversaries may inject malicious content into data indexed by a retrieval augmented generation (RAG) system." |
| **Industry practice (defence in depth)** | MSRC blog, 2025-07-29 | https://www.microsoft.com/en-us/msrc/blog/2025/07/how-microsoft-defends-against-indirect-prompt-injection-attacks — "Microsoft therefore takes a defense-in-depth approach spanning prevention, detection, and impact mitigation." and "The user must explicitly approve the generated text and send the email themselves." This is the strongest one-line justification for a five-layer design plus human review. |

---

## 4. "Must show on Wednesday" checklist, mapped to the planned benchmark

Cost column assumes the free Gemini tier. Per the spec, C3 makes up to one generation call plus guard LLM calls (L1 judge if enabled, L2 intent extraction, L3b doc scan, L4 output scan); L1 blocks skip the model. The Gemini API rate-limit page says limits are per project and to "View your active rate limits in AI Studio" (https://ai.google.dev/gemini-api/docs/rate-limits); check the Gemma 4 row there before planning extra runs. `gemma-4-26b-a4b-it` is listed as a supported model at https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api.

### MUST have

| ☐ | Item | Planned? | Gap and cheapest fix | Cost |
|---|---|---|---|---|
| ☐ | **Threat-model slide** (attacker = external email sender or KB-document author; knows the product exists; black-box to the guard; goal = exfiltrate to `contact@contact.com` / make draft forward; cannot touch system prompt or code; human reviewer present). Tag with LLM01:2025, NISTAML.015/.018, AML.T0051.001, AML.T0070 | implicit | write it | 30 min, no API |
| ☐ | **C0 vs C3 ASR with Wilson 95 % CI**, McNemar exact p, on the same 300 cases | yes | none | planned runs |
| ☐ | **FPR and benign success next to ASR** (a blocking guard trivially wins) | yes | **rename "TSR"**: in LLMail-Inject TSR means Team Success Rate; call it "benign utility" (AgentDojo term) or footnote it | 5 min |
| ☐ | **State the claim the CI supports.** With n=300, the Wilson upper bound is ≤5 % only if ≤7 attacks succeed (8/300 → [1.4 %, 5.2 %]). With 150 benign, 0 FP gives FPR ≤2.5 %; 3 FP gives up to 5.7 % | not in spec | say "point estimate ≤5 %" vs "upper bound ≤5 %" explicitly | 0 |
| ☐ | **Per-category table**: per LLMail scenario, and email vector vs RAG vector (RAG table separate, as planned) | yes | none | planned |
| ☐ | **Overhead**: latency p50/p95, model calls and tokens per email, C0 vs C3 | yes | show as ×-multiplier like CaMeL | planned |
| ☐ | **Leakage control statement**: L1 classifier trained on the other sha1-half of phase 2 | partial | exact dedup only; phase 2 has many paraphrases by the same teams. Compute max TF-IDF cosine of each test attack to the training half and report the share above e.g. 0.9, or re-score ASR excluding near-duplicates | 30–60 min, CPU only |
| ☐ | **Layer attribution from the C3 run** (which layer first blocked/sanitised each defended case) | no | read it from the stored GuardReport per case; it is a zero-API substitute for a full ablation. Label it "first catching layer", not "ablation" | 30 min, no API |
| ☐ | **Two or three worked examples**: one attack blocked by L1, one that passed L1 but was caught later (L3b/L4/L5), one that **succeeded** at C3 (or a false positive) | no | pick from results JSONL | 30 min |
| ☐ | **Limitations slide** (Section 6 below) — especially "no adaptive attack against MailGuard itself" | no | write it | 20 min |
| ☐ | **Reproducibility line**: manifest with git SHAs, seed 20260930, config hash; "nothing tuned on eval cases" | yes | show it | 0 |

### NICE to have (do in this order if time remains)

| ☐ | Item | How | Cost |
|---|---|---|---|
| ☐ | **Published-classifier baseline**, offline | Run ProtectAI deberta-v3-base-prompt-injection-v2 (Apache, ungated) — and Prompt Guard 2 86M if the Llama licence is already accepted — as an L1 replacement on the same 450 emails; report detection rate and FPR next to MailGuard L1-only. Mirrors LLMail-Inject §4.5 and the Prompt Guard 2 card | 1–2 h, CPU, **no Gemini calls** |
| ☐ | **Spotlighting-only baseline end-to-end** (`GuardConfig(l3=True, others False)`) | one extra run on attacks only | ≈ C0 cost (1 call/case) |
| ☐ | **Reduced ablation**: C1 (L1+L5) and C2 (L1+L2+L3+L5) presets on the 300 attacks, or leave-one-out `C3-Lx` on a stratified 100-attack subset | presets already exist in `mailguard/pipeline.py`; report with wide CIs and label as subset | each config ≈ one C3-sized run; subset cuts it by 3× |
| ☐ | **Small adaptive red-team**: team members spend ~2 h writing 20–30 attacks with full knowledge of the layers | report separately as "informal white-box, small n", never pooled with the headline | 2 h + ≤30 C3 cases |
| ☐ | **Cross-phase generalisation** of L1: score L1 (classifier+regex only) on a sample of `labelled_unique_submissions_phase1.json` | offline | 30 min, no API |
| ☐ | **Draft-quality check on benign**: 20–30 benign drafts C0 vs C3 rated blind by a teammate | shows L2/L3 do not degrade replies | 45 min |
| ☐ | **Trade-off plot** (ASR vs FPR; one point per config, or L1 threshold sweep) | NIST's Pareto-plot advice | 30 min if ablation/threshold data exist |

---

## 5. Recommended slide outline (11 slides)

```
 1 Problem ─▶ 2 Threat model ─▶ 3 Architecture ─▶ 4 Method ─▶ 5 Headline ─▶ 6 Breakdown
                                                                              │
 11 Next ◀─ 10 Limits ◀─ 9 Failures ◀─ 8 Overhead ◀─ 7 Which layer catches what ◀┘
```

1. **Problem and contribution.** Indirect prompt injection in email assistants; OWASP LLM01:2025; one sentence on AgentMailGuard and on why rag-email exists (host system, no company system available).
2. **Threat model diagram.** Two entry points (inbound email; poisoned KB document), attacker goal (exfiltrate / forward), what the attacker cannot do, human reviewer at the end. Tag NISTAML.015, AML.T0051.001, AML.T0070.
   ```
   attacker email ──▶ [L1 scan] ─▶ [L2 intent] ─▶┐
                                                  ├─▶ [L3 isolate] ─▶ LLM ─▶ [L4 scan] ─▶ [L5 policy] ─▶ human review
   poisoned KB doc ─▶ retrieval ─▶ [L3b scan] ───┘
   ```
3. **Architecture with the five layers** mapped to OWASP mitigations and MSRC "prevention, detection, impact mitigation".
4. **Evaluation method.** Dataset (LLMail-Inject phase-2 held-out half, 300+150; ~100 RAG cases), C0 vs C3, same cases, same model, seed; success rule; metrics; "nothing tuned on eval cases".
5. **Headline result.** Bar chart ASR C0 vs C3 with Wilson CI error bars, FPR beside it, McNemar p, and the target line at 5 %.
6. **Breakdown.** ASR per scenario and email vs RAG vector (small multiples or a table).
7. **Which layer catches what.** Stacked bar of first-catching layer from the C3 run; plus ablation/baseline rows if run.
8. **Overhead.** Latency p50/p95 and calls/tokens per email, C0 vs C3, with the SC4/SC5 targets.
9. **Failure analysis.** One successful attack at C3 and one false positive, annotated with which layer should have acted.
10. **Limitations and threats to validity.**
11. **Next steps.** Adaptive red-team, full ablation, published-defence baselines, second model.

---

## 6. Pitfalls reviewers attack, and the answer to prepare

| Pitfall | Why it is attacked (source) | Status here | Prepared answer |
|---|---|---|---|
| Evaluating on training data | Nasr et al. 2025 Lesson #1 | split by sha1 half; near-duplicates possible | show the near-duplicate check; report ASR on the de-duplicated subset |
| Only static attacks | Nasr et al.; Zhan et al.; NIST E2025 | LLMail attacks were adaptive to Microsoft's defences, not to MailGuard | say so plainly; show the small red-team if done; "the attacker moves second" |
| Over-blocking | NIST Pareto advice; Prompt Guard 2 "APR @ 3 % utility reduction"; Carlini ROC advice | FPR + benign utility planned | show FPR CI beside ASR on the same slide |
| Simulated agent / no real host | LLMail-Inject: "We need benchmarks for end-to-end attacks" | real rag-email pipeline | emphasise it runs through the real ContextBuilder and generator |
| Success rule is a proxy | PoisonedRAG App. J substring-vs-human gap | address-in-body or `forward` (no tools in rag-email) | manual audit of a random 30 scored cases; report agreement |
| Cherry-picked samples | Carlini "compare … on a per-example basis" | stratified, seeded manifest | show seed, manifest hash, and that errors are counted separately |
| No utility metric | AgentDojo, BIPIA, SecAlign all report one | benign utility planned | plus blind draft-quality check if time |
| No comparison to prior defences | Carlini "Compare against prior work" | none yet | offline ProtectAI/Prompt Guard 2 baseline; spotlighting-only run |
| No ablation for a layered claim | BIPIA §7.3; LLMail-Inject ensemble table | presets exist, not run | first-catching-layer chart now; full ablation as next step |
| Single model | AgentDojo/CaMeL report many models | Gemma 4 only | state as limitation |
| Human reviewer counted as defence | OWASP/MSRC treat HITL as mitigation | ASR measured before human review | say ASR is measured on the draft before the human, so it is conservative |

---

## 7. Reviewer questions to prepare for

1. What exactly counts as a successful attack when rag-email has no send tool? Is that weaker than LLMail-Inject's tool-call success?
2. Was the L1 classifier trained on anything similar to the test emails? How did you check near-duplicates across the sha1 split?
3. Your target is 5 % ASR. Is that the point estimate or the upper confidence bound?
4. What is the false-positive rate, and how many real benign emails would a company lose per day at that rate?
5. Which layer does most of the work? Would L1 alone give the same result? (Have the first-catching-layer chart.)
6. How does your L1 compare with Prompt Guard 2 or ProtectAI's classifier on the same emails?
7. What happens if the attacker knows your layers (adaptive attack)? Why should we trust near-zero numbers when Nasr et al. broke 12 defences that reported near-zero?
8. Show one attack that got through. Why did every layer miss it?
9. How much latency and how many extra model calls does the guard add? Does it meet SC4/SC5?
10. The guard uses an LLM judge — can the judge itself be injected?
11. Why Gemma 4 26B? Would results transfer to GPT/Claude/Gemini?
12. How were the poisoned-KB cases built, and are they PoisonedRAG's optimised texts or templates?
13. Is the human reviewer part of your defence or not? Is ASR measured before or after review?
14. How does this map to OWASP LLM01 / NIST / MITRE ATLAS?
15. Why a layered design instead of a design-level defence like CaMeL? (Answer: CaMeL targets tool-using agents with a planner; rag-email makes one generation call with no tools; CaMeL still loses utility, 77 % vs 84 %.)

---

## 8. Limitations and threats to validity (draft text for slide 10)

- **Internal:** success is scored by a string/action rule, not by tool execution; a manual audit sample bounds the error. Rate-limit errors are excluded and reported, not counted as defended.
- **Leakage:** training and test halves come from the same competition phase; near-duplicate paraphrases may inflate L1 detection.
- **Adaptivity:** LLMail-Inject attacks were adapted against Microsoft's phase-2 defences, not against AgentMailGuard; results are a transfer evaluation, not a worst-case bound (Carlini 2019; Nasr et al. 2025).
- **External:** one model (Gemma 4 26B via Gemini API), one host system, English emails, one attacker goal (exfiltration to one address).
- **Statistical:** n=300 attacks and 150 benign give CI half-widths of roughly ±2–3 points near 5 %; small per-scenario cells have wide intervals.

---

## References (all opened 2026-09-28)

1. Abdelnabi et al., *LLMail-Inject: A Dataset from a Realistic Adaptive Prompt Injection Challenge*, arXiv:2506.09956, 2025-06-11. https://arxiv.org/abs/2506.09956 ; dataset https://huggingface.co/datasets/microsoft/llmail-inject-challenge
2. Debenedetti et al., *AgentDojo*, arXiv:2406.13352, 2024. https://arxiv.org/abs/2406.13352
3. Zhan et al., *InjecAgent*, arXiv:2403.02691, Findings of ACL 2024 (per arXiv comment). https://arxiv.org/abs/2403.02691
4. Yi et al., *Benchmarking and Defending Against Indirect Prompt Injection Attacks on LLMs (BIPIA)*, arXiv:2312.14197, KDD 2025 (per arXiv comment). https://arxiv.org/abs/2312.14197
5. Hines et al., *Defending Against Indirect Prompt Injection Attacks With Spotlighting*, arXiv:2403.14720, 2024-03-20. https://arxiv.org/abs/2403.14720
6. Chen et al., *StruQ*, arXiv:2402.06363, USENIX Security 2025 (per arXiv comment). https://arxiv.org/abs/2402.06363
7. Chen et al., *SecAlign*, arXiv:2410.05451, ACM CCS 2025 (per arXiv comment). https://arxiv.org/abs/2410.05451
8. Debenedetti et al., *Defeating Prompt Injections by Design (CaMeL)*, arXiv:2503.18813, 2025. https://arxiv.org/abs/2503.18813
9. Meta, *Llama Prompt Guard 2 86M model card*. https://github.com/meta-llama/PurpleLlama/blob/main/Llama-Prompt-Guard-2/86M/MODEL_CARD.md
10. Inan et al., *Llama Guard*, arXiv:2312.06674. https://arxiv.org/abs/2312.06674
11. ProtectAI, *deberta-v3-base-prompt-injection-v2* model card. https://huggingface.co/protectai/deberta-v3-base-prompt-injection-v2
12. Zou et al., *PoisonedRAG*, arXiv:2402.07867, USENIX Security 2025 (per arXiv comment). https://arxiv.org/abs/2402.07867
13. Liu et al., *Formalizing and Benchmarking Prompt Injection Attacks and Defenses*, arXiv:2310.12815, USENIX Security 2024 (per arXiv comment). https://arxiv.org/abs/2310.12815
14. Zhan et al., *Adaptive Attacks Break Defenses Against Indirect Prompt Injection Attacks on LLM Agents*, arXiv:2503.00061, Findings of NAACL 2025 (per arXiv comment). https://arxiv.org/abs/2503.00061
15. Nasr, Carlini et al., *The Attacker Moves Second*, arXiv:2510.09023, 2025-10-10. https://arxiv.org/abs/2510.09023
16. Carlini, Athalye, Papernot et al., *On Evaluating Adversarial Robustness*, arXiv:1902.06705, 2019. https://arxiv.org/abs/1902.06705
17. OWASP GenAI Security Project, *LLM01:2025 Prompt Injection*. https://genai.owasp.org/llmrisk/llm01-prompt-injection/
18. NIST AI 100-2 E2025, *Adversarial Machine Learning: A Taxonomy and Terminology of Attacks and Mitigations*, March 2025. https://csrc.nist.gov/pubs/ai/100/2/e2025/final ; PDF https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-2e2025.pdf
19. MITRE ATLAS data v5.6.0. https://raw.githubusercontent.com/mitre-atlas/atlas-data/main/dist/ATLAS.yaml (browse: https://atlas.mitre.org/techniques/AML.T0051)
20. MSRC, *How Microsoft defends against indirect prompt injection attacks*, 2025-07-29. https://www.microsoft.com/en-us/msrc/blog/2025/07/how-microsoft-defends-against-indirect-prompt-injection-attacks
21. Brown, Cai, DasGupta, *Interval Estimation for a Binomial Proportion*, Statistical Science 16(2), 2001. https://projecteuclid.org/journals/statistical-science/volume-16/issue-2/Interval-Estimation-for-a-Binomial-Proportion/10.1214/ss/1009213286.full
22. Dietterich, *Approximate Statistical Tests for Comparing Supervised Classification Learning Algorithms*, Neural Computation 10(7), 1998. https://mlanthology.org/neco/1998/dietterich1998neco-approximate/
23. Google, *Gemma on the Gemini API* (lists `gemma-4-26b-a4b-it`). https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api ; rate limits https://ai.google.dev/gemini-api/docs/rate-limits
