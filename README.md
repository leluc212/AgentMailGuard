# AgentMailGuard

Multi-layer prompt-injection defense for LLM agents that read customer email and answer
with retrieval-augmented generation (RAG). AgentMailGuard is the security subsystem of the
*Enterprise RAG-Based Intelligent Email Management and Response System* (branch
`RAG_Email_System`); it is training-free at inference time, runs on local 7B/8B models via
Ollama or on GPT-4o-mini, and is evaluated on public benchmarks (InjecAgent, BIPIA,
LLMail-Inject, PoisonedRAG).

```
Customer email ──> L1 Email Injection Scanner ──> L2 User Intent Extractor ──┐
                        (rules -> ML -> LLM judge)      (strip + paraphrase)   │
Knowledge base ──> RAG retrieval ──> L3b Retrieved Document Scanner ──────────┤
                                        (quarantine poisoned chunks)          v
                                   L3 Channel Isolation (spotlighting) ──> LLM reply agent
                                                                              │
                                   L4 Output Scanner (redact / verify) <──────┘
                                                                              │
                                   L5 Email Policy Engine ──> draft / block / human approval / auto-send
```

| Layer | Module | Purpose | Cost |
|---|---|---|---|
| L1 | `mailguard/layers/l1_injection_scanner` | Cascade: 13 regex rule families + obfuscation heuristics → calibrated TF-IDF/LR → LLM judge (uncertain band only) | 1-3 ms (+LLM on ~10-20 % of emails) |
| L2 | `mailguard/layers/l2_intent_extractor` | Strip instruction-carrying segments, extract entities/actions, optional neutral paraphrase | 2-10 ms |
| L3 | `mailguard/layers/l3_channel_isolation` | Nonce-tagged channel markers, datamark/encode spotlighting, marker scrubbing, token budget | < 1 ms |
| L3b | `mailguard/layers/l3b_document_scanner` | PoisonedRAG heuristics (answer-forcing, query-echo, "ignore other documents") + rules + ML per chunk | 1-3 ms/chunk |
| L4 | `mailguard/layers/l4_output_scanner` | PII/secret redaction (Luhn), system-prompt leak (n-gram), citation integrity, injected-goal compliance, unsafe recipients/links | 1-5 ms |
| L5 | `mailguard/layers/l5_policy_engine` | YAML risk-tiered gating (`quarantine > block > human_approval > draft_only > auto_send`), idempotent audit log | < 1 ms |

## Quick start

```bash
pip install -e ".[eval,dev]"          # core + datasets + tests
python -m pytest tests/unit -q         # 68 unit tests, no network, no LLM

# scan one email through the full stack (rules + ML only, no LLM required)
python scripts/scan_email.py tests/fixtures/emails.json --pick attacks:0 --config C3 --kb datasets/seed/support_kb -v
```

LLM-backed stages (L1 judge, L2 paraphrase, optional L3b/L4 judges) are enabled by setting
`GUARD_MODELS__*` in `.env` (see `.env.example`) to a model from `configs/models.yaml`:
`qwen2.5-7b-instruct` / `llama-3.1-8b-instruct` (Ollama) or `gpt-4o-mini` (OpenAI API).
Without a model the stack degrades to the cheap stages and never fails open.

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

Configurations: `C0` no defense, `C1` L1 only, `C2` L1+L2+L3, `C3` all layers, `C3-L1` ... `C3-L5`
one-layer ablations. Metrics: ASR, TMR, DER, TSR, FPR, latency, with Wilson 95 % CIs and
McNemar tests for ablations (`evaluation/metrics.py`).

## Repository layout

```
configs/            injection_rules.yaml, channels.yaml, pii_patterns.yaml, policy.yaml, models.yaml
mailguard/          the guard package (contracts, layers, llm providers, pipeline, datasets, integration)
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
