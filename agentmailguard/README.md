# AgentMailGuard

Multi-layer prompt-injection defense for LLM agents that read customer email and answer
with retrieval-augmented generation (RAG). AgentMailGuard is the security subsystem of the
*Enterprise RAG-Based Intelligent Email Management and Response System* (branch
`RAG_Email_System`); it is training-free at inference time, runs on local 7B/8B models via
Ollama or on GPT-4o-mini, and is evaluated on public benchmarks (InjecAgent, BIPIA,
LLMail-Inject, PoisonedRAG).

```
Customer email ──> L1 Inbound Guard ──> L3 Inbound Gate, pass 1 ──> stop: block / quarantine
                   (scan: rules -> ML ->      (policy on L1's facts)
                    LLM judge; extract:             │ otherwise
                    strip + intent)                 v
Knowledge base ──> RAG retrieval ──> L2 Doc Scanner ──> L3 Inbound Gate, pass 2 ──> stop, or go on
                                     (quarantine poisoned chunks)   (policy on every fact)
                                                                          │
                                   channel isolation (spotlighting; builds the prompt, not a layer)
                                                                          v
                                                                 LLM reply agent
                                                                          │
                                   L4 Output Guard (redact / verify, outbound policy, inbound floor)
                                                                          │
                                   final decision: draft / block / human approval / auto-send
```

| Layer | Module | Purpose | Cost |
|---|---|---|---|
| L1 Inbound Guard | `mailguard/layers/inbound_guard` | One verdict from two sub-stages. **scan:** cascade of 15 regex rule families + obfuscation heuristics → calibrated TF-IDF/LR → LLM judge (uncertain band only); severity comes from here. **extract:** strip instruction-carrying segments, entities/actions, optional neutral paraphrase; never raises severity | 1-3 ms (+LLM on ~10-20 % of emails) |
| L2 Doc Scanner | `mailguard/layers/doc_scanner` | Per chunk: rules + PoisonedRAG heuristics + ML; echo of the email's question sentences (`echo_vs_question`, `ordered_prefix`), bait clusters, a trust level per chunk; optional chunk judge (uncertain chunks only) and set judge (adds quarantines only) | 1-3 ms/chunk |
| L3 Inbound Gate | `mailguard/layers/inbound_gate` | The shared policy engine at stage `inbound`, run twice: pass 1 on L1's facts (a block or quarantine stops the job before the Doc Scanner), pass 2 on every fact (the audited decision) | < 1 ms |
| L4 Output Guard | `mailguard/layers/output_guard` | PII/secret redaction (Luhn), system-prompt leak (n-gram), citation integrity, injected-goal compliance, unsafe recipients/links, optional judge; then the policy engine at stage `outbound`, then the inbound decision carried in as a floor | 1-5 ms |
| (not a layer) channel isolation | `mailguard/prompting/channel_isolation.py` | Nonce-tagged channel markers, datamark/encode spotlighting, marker scrubbing, token budget; returns the prompt and an `IsolationInfo`, never a verdict | < 1 ms |

The policy engine (`mailguard/policy/engine.py`, `configs/policy.yaml`, version `2026.10-v3`) is shared by L3 and L4: all matching rules are collected and the strictest action wins (`quarantine > block > human_approval > draft_only > auto_send`, ties to the lowest priority number), with an idempotent audit log. Reports carry `schema_version: 3`. Design decisions: [ADR-0015](../docs/adr/0015-guard-architecture-v3.md); a deeper reference per layer: [`../docs/overview/layers/`](../docs/overview/layers/).

## Quick start

```bash
pip install -e ".[eval,dev]"          # core + datasets + tests
python -m pytest tests/unit -q         # the unit tests, no network, no LLM

# scan one email through the full stack (rules + ML only, no LLM required)
python scripts/scan_email.py tests/fixtures/emails.json --pick attacks:0 --config C3 --kb datasets/seed/support_kb -v
```

LLM-backed stages (the L1 scan judge and extractor, the L2 chunk and set judges, the L4 output judge) are
enabled by setting `GUARD_MODELS__INBOUND_JUDGE`, `__INBOUND_EXTRACTOR`, `__DOC_JUDGE`, `__DOC_SET_JUDGE` and
`__OUTPUT_JUDGE` in `.env` (see `.env.example`) to a model from `configs/models.yaml`:
`qwen2.5-7b-instruct` / `llama-3.1-8b-instruct` (Ollama), `gpt-4o-mini` (OpenAI API), or the same
two open models through OpenRouter with one provider pinned (`qwen/qwen-2.5-7b-instruct`,
`meta-llama/llama-3.1-8b-instruct`; see "Served-provider provenance" in `docs/architecture.md`).
The chunk judge, the set judge and the output judge also need their switch
(`DOC_SCANNER__LLM_ENABLED`, `DOC_SCANNER__SET_JUDGE_LLM_ENABLED`, `OUTPUT_GUARD__LLM_ENABLED`, default off).
A judge that is not configured degrades to the cheap stages. A configured judge that fails at run time
(timeout, non-JSON, missing/invalid fields, transport error) keeps the cheap result, marks the verdict
`metadata.llm_fallback = true` with `llm_fallback_reason`
(`timeout | non_json | schema_missing | invalid_fields | error`) and, by default
(`MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR=true`), sets the verdict's `error` to `judge_invalid:<stage>`
(`inbound_scan`, `inbound_extract`, `doc_chunk`, `doc_set`, `output`), which the policy turns into human
review: the guard fails closed. With the flag set to `false` the error stays empty and the email is
processed normally (the v2 visible fallback). At most five guard model calls are made per job (the
production default enables two), counted in `report.metadata["guard_calls"]`. v2 environment keys
(`L1__*` to `L5__*`, `GUARD_MODELS__JUDGE|EXTRACTOR|DOC_SCANNER`) stop the process with a migration
table (`LegacyConfigError`); the mapping is in `docs/configuration.md` section 2.23 of the host repository.

## Datasets (reputable public sources only)

```bash
python -m mailguard.datasets.download --all --max-mb 400     # -> datasets/raw/ + MANIFEST.json
python -m mailguard.datasets.download --manifest             # provenance table
python -m mailguard.datasets.build_l1_corpus                 # -> datasets/processed/l1_injection
python -m mailguard.datasets.build_email_benchmark           # -> datasets/processed/email_bench/cases.jsonl
python -m mailguard.datasets.build_rag_poison                # -> datasets/processed/rag_poison/chunks.jsonl
```

| Source | Venue / owner | Used for |
|---|---|---|
| InjecAgent | Findings of ACL 2024 | email-vector attacks (data stealing / direct harm) |
| BIPIA | ACM SIGKDD 2025 (Microsoft) | email contexts x 15 text-attack types; L1 training |
| LLMail-Inject | Microsoft (2025 challenge) | 370k real adaptive attack emails; benign FP emails |
| PoisonedRAG | USENIX Security 2025 | poisoned passages for the RAG vector |
| TrustAIRLab in-the-wild jailbreaks | ACM CCS 2024 | jailbreak / regular prompts for L1 |
| deepset, xTRam1, jackhhao, Lakera Gandalf | Hugging Face (Apache/MIT) | L1 classifier training |
| Enron-Spam (ham), Bitext support | CEAS 2006 / Bitext | benign email traffic (TSR, FPR) |
| seed corpus | this repo | 40 benign support emails, 24 attack templates, 6 poison templates, 5 KB docs |

Leakage control: sources that feed both the L1 classifier and the benchmark are split by a
stable hash of the item id (`bench_side`), and seed attack templates are benchmark-only.

## Training

```bash
python -m training.train_l1_classifier                 # ~20 s on CPU; writes artifacts/models/l1_injection_clf_v1.joblib (+ metrics)
python -m training.build_sft_dataset                   # chat-format SFT data for the judge
python -m training.finetune_llm_judge --config training/configs/qwen2.5-7b_lora.yaml     # QLoRA, needs a 16 GB GPU
python -m training.finetune_llm_judge --config training/configs/llama-3.1-8b_lora.yaml
```
See `training/EXPORT_OLLAMA.md` to register a fine-tuned judge in Ollama.

## Evaluation

```bash
python -m evaluation.run_benchmark --agent naive --config all                       # offline sanity (simulated agent)
python -m evaluation.run_benchmark --agent qwen2.5-7b-instruct --guard-model qwen2.5-7b-instruct --config all
python -m evaluation.run_benchmark --agent llama-3.1-8b-instruct --guard-model llama-3.1-8b-instruct --config all
python -m evaluation.run_benchmark --agent gpt-4o-mini --guard-model gpt-4o-mini --config all
python -m evaluation.report evaluation/results/<run-dir> --out paper/tables        # Table V / VI (md + LaTeX)
```

Configurations (guard v3; the presets of `GuardConfig.preset`): `C0` no defense, `C1` Inbound Guard (L1) +
Inbound Gate (L3), `C2` C1 + Doc Scanner (L2) + channel isolation, `C3` everything, `C3-L1` ... `C3-L4`
(C3 minus one layer) and `C3-ISO` (C3 without channel isolation). The root live benchmark
(`evaluation/mailguard_bench`) adds `C0T`, the guard's prompt template with no layer active, as a baseline,
and is where the v3 numbers will come from (plan_v3 Task 16); this folder's harness runs the nine presets
above on the offline simulated and local agents. Results of the earlier six-layer guard are labelled "v2 results".
Metrics: ASR, TMR, DER, TSR, FPR, latency, with Wilson 95 % CIs and
McNemar tests for ablations (`evaluation/metrics.py`).

## Repository layout

```
configs/            injection_rules.yaml, channels.yaml, pii_patterns.yaml, policy.yaml, models.yaml
mailguard/          the guard package (contracts, layers/{inbound_guard,doc_scanner,inbound_gate,output_guard},
                    prompting (channel isolation), policy (shared engine), prompts, llm providers, pipeline,
                    datasets, integration)
evaluation/         taxonomy, metrics, harness, run_benchmark, report
training/           L1 classifier, SFT data builder, LoRA fine-tuning, export guide
datasets/           seed/ (hand-written), raw/ (downloaded, git-ignored), processed/ (built, git-ignored)
services/           guard_worker (RabbitMQ / stdin) for the core system
scripts/            scan_email.py demo CLI
tests/              unit tests + fixtures
docs/, paper/       architecture notes, dataset cards, experiment protocol, paper outline
WORKLOG.md          session-by-session progress log (read this first when resuming)
```

## Integration with the core

`mailguard/integration/adapters.py` wraps the core's `ContextPackage` and `LLMProvider`
without importing them (`GuardedReplyAgent`, `decision_to_job_result`, `dispatch_allowed`).
The dispatcher must only send when the policy decision is `auto_send`; `draft_only` and
`human_approval` create provider drafts for a reviewer; `block`/`quarantine` create nothing.
