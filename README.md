# AgentMailGuard

AgentMailGuard is a layered guard against prompt injection for AI email assistants. It is evaluated inside rag-email, a production-style AI email system that hosts it.

Python 3.12+, MIT license. The guard lives in [`agentmailguard/`](agentmailguard/); the host system is the rest of the repository.

## How the guard works

One email goes through four layers (L1 Inbound Guard, L2 Doc Scanner, L3 Inbound Gate, L4 Output Guard) and a prompt-building switch (channel isolation, not a layer) around the single reply-writing model call. Only an inbound `block` or `quarantine` stops the run before the call; every other decision is returned as a label for the dispatcher to act on. The policy engine is shared infrastructure (stage `inbound` for L3, stage `outbound` for L4), not a layer.

```
                        BEFORE the reply call
 email ──▶ L1 Inbound Guard ──▶ L3 Inbound Gate, pass 1 ──┐
           (scan + extract)      (L1's facts only)         │ other
                                      │ block /            │ decisions
                                      │ quarantine         ▼
                                      ▼             retrieved chunks
                           return (report, None,          │
                           None): no reply call,          ▼
                           no Doc Scanner run      L2 Doc Scanner ──▶ L3 Inbound Gate, pass 2
                                                   (trust per chunk)   (every fact; final, audited)
                                                                              │ block / quarantine: stop
                                                                              ▼ other decisions
                                                                  channel isolation (build prompt)
                                                                              │
                                                                              ▼
                                                          ┌──────── REPLY MODEL ────────┐
                                                          │   generate(bundle.messages) │
                                                          └──────────────┬──────────────┘
                                                                         ▼
                        AFTER the reply call              L4 Output Guard (scan + redact, outbound
                                                          policy, inbound decision as a floor)
                                                                         ▼
                                                                  final decision
```

| Layer | Input | Decision | Output |
|---|---|---|---|
| L1 Inbound Guard (`inbound_guard`) | One email | Two sub-stages, one verdict. **scan:** cascade by cost: regex rules (15 families, 54 patterns) and obfuscation checks; then a TF-IDF + logistic-regression classifier, fused with the rules; then an optional LLM judge for uncertain scores only. **extract:** scores each segment alone, strips the injected ones, extracts the user's intent by regex (optional LLM paraphrase). Severity and score come from the scan only. A judge that fails or answers invalidly is human review (`judge_invalid:<stage>`); an error is a HIGH fail-closed verdict. | `InboundVerdict`: severity, score 0-1, findings, indicators, sanitized body, intent |
| L2 Doc Scanner (`doc_scanner`) | Retrieved chunks and the email's question sentences | Each chunk: the L1 rules and classifier plus PoisonedRAG heuristics, then signals against the question sentences (`echo_vs_question`, `ordered_prefix`) and bait clusters; an optional LLM check of uncertain chunks and an optional LLM set judge (adds quarantines only). Skipped when gate pass 1 stops the job. | `DocumentVerdict`: one `ChunkVerdict` each, with `trust` (trusted, untrusted, quarantined) |
| L3 Inbound Gate (`inbound_gate`) | L1 and L2 verdicts as summary facts, never the text | No model. The shared policy engine at stage `inbound`, run twice: pass 1 on L1's facts, pass 2 on every fact. Every matching rule in `configs/policy.yaml` is collected and the strictest action wins: quarantine > block > human_approval > draft_only > auto_send. A layer error forces at least `human_approval`. | `PolicyDecision` (the final pass is audited, JSONL) |
| Channel isolation (a switch, not a layer) | Trusted instructions and data, the intent, untrusted text, the kept chunks with their trust | No model, no verdict. A nonce, scrubbed channel markers, spotlighting (`delimit`, `datamark` default, `encode`) and nonce-tagged channels. | `SecurePrompt` (`.messages`) and `IsolationInfo` |
| L4 Output Guard (`output_guard`) | The draft and what L1 and L2 flagged | Deterministic checks: secret redaction, system-prompt leak, citation integrity, injected-goal compliance, unsafe recipients and links; an optional LLM judge of doubtful drafts; then the policy engine at stage `outbound`; then the inbound decision is carried in as a floor (only a specific rule stricter than `auto_send`). Default is `draft_only`; `auto_send` needs category `acknowledgement` or `scheduling`, a reply action, severity low or below, and no floor. | `OutputVerdict` with `redacted_text`, and the final `PolicyDecision` |

All five judges (L1 scan, L1 extract, L2 chunk, L2 set, L4 output) share one call budget of five per job (the production default enables two) and fail closed by default (`MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR`). Reports carry `schema_version: 3`. The v2 names (L3b, L5, `C3-L3B`, `C3-L5`, `L1__*` to `L5__*` keys) are gone; the migration table is in [docs/configuration.md](docs/configuration.md) section 2.23, and the decisions are in [ADR-0015](docs/adr/0015-guard-architecture-v3.md).

Thresholds, caps and rules per layer: [docs/overview/layers/](docs/overview/layers/) and [docs/overview/04-agentmailguard-layers.md](docs/overview/04-agentmailguard-layers.md).

## Install and use the guard

No Docker is needed. Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), git. If the repository is private, ask the owner for access.

```bash
git clone https://github.com/leluc212/AgentMailGuard.git
cd AgentMailGuard
git checkout main            # the guard subtree is on main, not on RAG_Email_System
uv sync
source .venv/bin/activate
uv pip install -e agentmailguard
```

The L1 classifier `l1_injection_clf_v1.joblib` is not in git; the owner sends it privately and it is not redistributed (ADR-0012, decision 15). Its sha256 is `8fc1cbe74a599ab870a10ca5ff43f4a6d80b3e2273e36c7ed163c637a1d40103` (scikit-learn 1.9.1 pickle). Without it the guard still runs, with L1 stage 2 off. There are two routes, each reading its own location:

| Route | Classifier location |
|---|---|
| Direct use (Python, CLI) | `agentmailguard/artifacts/models/l1_injection_clf_v1.joblib`, or any path in `INBOUND_GUARD__ML_MODEL_PATH` |
| Benchmark | `evaluation/mailguard_bench/pinned/`; check with `cd evaluation/mailguard_bench/pinned && sha256sum -c SHA256SUMS` |

Without the owner's file, `make mailguard-prep` downloads the datasets and trains a classifier (network, no API key) into `../AgentMailGuard-bench-artifacts`. Use it for direct use through `INBOUND_GUARD__ML_MODEL_PATH`; its hash differs from the pin, so the pinned benchmark rejects it.

See it work, from `agentmailguard/` (an injected email; with the rules alone L1 reports critical and the Inbound Gate quarantines it before any reply call):

```bash
cd agentmailguard
python scripts/scan_email.py tests/fixtures/emails.json --pick attacks:0 --config C3 --kb datasets/seed/support_kb -v
```

Integrate it in Python (run from `agentmailguard/`, or set `PYTHONPATH=.` to run it as a file elsewhere). With default settings every guard model name is `fake`, so no LLM stage is built.

```python
import asyncio
from mailguard.contracts.email import DraftCandidate
from mailguard.pipeline import GuardConfig, MailGuardPipeline

SYSTEM = "You are the Acme support assistant."

async def reply_agent(messages):  # replace with your real reply-model call
    return DraftCandidate(body="Your X200 has a 24-month warranty [kb-warranty-1].")

async def main():
    guard = MailGuardPipeline(config=GuardConfig.preset("C3"), audit=False)
    email = {"message_id": "m2", "sender": {"email": "a@b.com"},
             "subject": "hi", "body": "Where is order ORD-1?"}
    chunks = [{"chunk_id": "kb-warranty-1", "document_id": "faq",
               "content": "Every X200 has a 24-month warranty."}]
    report, draft, bundle = await guard.run(
        email, chunks, reply_agent, system_instructions=SYSTEM, category="support")
    if draft is None:  # inbound block/quarantine: the reply model was never called
        print("stopped:", report.inbound_decision.action)
    else:
        print(report.decision.action, draft.body, [c.chunk_id for c in bundle.kept_chunks])

asyncio.run(main())
```

It prints `draft_only Your X200 has a 24-month warranty [kb-warranty-1]. ['kb-warranty-1']`.

Notes:
- `GuardConfig.preset(name)` takes the guard v3 presets: `C0` (nothing), `C1` (Inbound Guard + Inbound Gate), `C2` (C1 + Doc Scanner + channel isolation), `C3` (everything), `C3-L1` to `C3-L4` (C3 minus one of the four layers) and `C3-ISO` (C3 without channel isolation). Other names, including the v2 `C3-L3B` and `C3-L5`, raise `ValueError`. For a custom set, build `GuardConfig` directly with the booleans `inbound_guard`, `doc_scanner`, `inbound_gate`, `output_guard` and `channel_isolation`. The benchmark uses these same names (plus `C0T`, the guard's template with no layer, which is `GuardConfig.preset("C0")`).
- `report.inbound_decision` is the Inbound Gate's decision before generation. `report.decision` is the final decision (L4's, which carries the inbound decision as a floor; the inbound decision when L4 is off). A custom provider can be passed as `inbound_judge=`, `inbound_extractor=`, `doc_judge=`, `doc_set_judge=` or `output_judge=` (`doc_set_judge` only has an effect with `DOC_SCANNER__SET_JUDGE_LLM_ENABLED=true`, and a provider is ignored for a layer object passed ready-made).
- This cheap-only setup does not stop soft attacks: without the classifier or a judge, an attack that matches no rule falls to the default `draft_only`. To enable the LLM stages, name a registered model for each stage; [Configure the models](#configure-the-models) has every setting and three worked examples. The benchmark ignores these settings (it takes `MODEL=<profile>`).
- A configured LLM stage that fails (timeout, non-JSON output, schema mismatch) keeps the cheap result, sets `metadata.llm_fallback=true` and, by default (`MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR=true`), sets the verdict's `error` to `judge_invalid:<stage>`, which the policy turns into human review. A stage that is not configured degrades silently. `false` restores the v2 visible fallback (the benchmark's `--judge-fallback` / `JUDGE_FALLBACK=1`).
- Step-wise API: `inspect_inbound`, `build_prompt`, `inspect_outbound`. Adapters for a host: `GuardedReplyAgent`, `decision_to_job_result`, `dispatch_allowed` in `mailguard.integration.adapters`.
- Unit tests of the guard: `python -m pytest tests/unit -q` from `agentmailguard/`.

## Run the benchmark

The benchmark runs 550 pinned cases through the real rag-email pipeline, once per config. The cases are 300 LLMail-Inject attack emails, 150 benign emails and 100 RAG-poisoning cases, drawn with seed 20260930 (each draw on its own derived seed) from public datasets. Sources and licenses: [pinned/NOTICE.md](evaluation/mailguard_bench/pinned/NOTICE.md); case ids per set: `evaluation/datasets/mailguard/manifest.json`. The case file `evaluation/datasets/mailguard/cases.jsonl` has sha256 `c00dddca6336df91bcf80de7904ad5a8335564ababd8618c7b1d953a23811d19`. After `make mailguard-prep`, `make mailguard-cases` rebuilds the set and fails unless it reproduces the manifest's ids and hash.

Needs Docker Engine with Compose v2, make, and the classifier in `pinned/`. Linux is the tested route; on Windows 11 Home use WSL2 with Ubuntu and Docker Engine inside it ([docs/BENCHMARK.md](docs/BENCHMARK.md)). The benchmark is full cloud (owner decision 2026-10-01, ADR-0014): `gpt-4o-mini` through the OpenAI API, Qwen2.5-7B and Llama-3.1-8B through OpenRouter, each pinned to one provider. Keys, set in `.env` (names only; never print or commit them):

| Key | Needed for |
|---|---|
| `EMBEDDING__API_KEY`, with `EMBEDDING__MODEL_NAME`, `EMBEDDING__BASE_URL`, `EMBEDDING__DIMENSION=1536` and `EMBEDDING__MOCK=false` | Embeddings, every live run: an OpenAI-compatible endpoint of your choice that accepts the `dimensions` parameter (not `text-embedding-ada-002`), the same for every model of a comparison |
| `BENCH_OPENAI_API_KEY` | `gpt-4o-mini` |
| `BENCH_OPENROUTER_API_KEY` (and optionally `BENCH_OPENROUTER_BASE_URL`) | `qwen2.5-7b-openrouter` (pinned to Phala), `llama-3.1-8b-openrouter` (pinned to CoreWeave, `bf16`) |
| `BENCH_OLLAMA_BASE_URL` (optional) | the local profiles `qwen2.5-7b`, `llama-3.1-8b-local` (not used on 2026-10-02): Ollama serves them, no key |
| `LLM__OPENAI_API_KEY` | the meaning reader's key (its provider and URL go on the `bench-report` command line); also the `gemma-4-26b` test profile |

The exact block is in [docs/BENCHMARK.md](docs/BENCHMARK.md) part C and section 9.9 of the runbook. Plan for at least 25 GB of free disk (the doctor warns below it) and hours per model: 4 to 6 for `gpt-4o-mini`, extrapolated from a 7-case smoke run; the OpenRouter models were not measured (plan 4 to 8 each). Run the kit from the repository root, in this order:

```bash
make bench-doctor MODEL=<profile>                       # check Docker, Python, disk, keys and embedding in .env, classifier sha256
make bench-setup                                        # once per machine: pin check, guard smoke, stack up and healthy
make bench-canary MODEL=<openrouter profile>            # OpenRouter only: one live call checks the pinned provider (under a cent)
make bench-run MODEL=gpt-4o-mini RUN=<id> CONCURRENCY=2 # one embedding check, then one model through every config; a rerun resumes
make bench-report RUN=<id>                              # rebuild report.md, summary.json, manifest.json, metrics.csv
make bench-package RUN=<id>                             # bench-results-<id>.zip and how to commit it to branch bench/<id>
```

A small trial first, over one LLMail attack, one benign email and one RAG case (so it also embeds knowledge documents): `make bench-run MODEL=gpt-4o-mini RUN=trial-gpt CONFIGS=C0,C0T,C3 CASE_IDS=attack-llmail-01d16d4e4af9,benign-llmailfp-0,attack-prag-hotpotqa-5a76e88555429972597f13f4 CONCURRENCY=1`. It passes only if every `ok <config>:` line says `0 error` ([docs/BENCHMARK.md](docs/BENCHMARK.md) D4); a trial folder (`RUN=trial-...`) is never a result. Options of `bench-run`: `CONFIGS=`, `LIMIT=n`, `CASE_IDS=id,id,...`, `CONCURRENCY=1|2`, `DRY_RUN=1`. Model profiles: `gpt-4o-mini`, `qwen2.5-7b-openrouter` and `llama-3.1-8b-openrouter` (the Friday run), `qwen2.5-7b` and `llama-3.1-8b-local` (local), `gemma-4-26b` (test). Before the stack is touched, `bench-run` makes one embedding call with the `EMBEDDING__*` settings and refuses to start unless it returns a 1536-wide vector. A guard model call that fails on the route or the service (HTTP 402 (no credit), 404, 408, 409 or 5xx, a timeout, a connection error, a router error in an HTTP 200 body, an HTTP 200 body that is not JSON) makes its case an error row (`guard_route_failure`) that the retry pass reruns; a per-minute 429 is retried, and a used-up quota stops the run (`quota_exhausted`). A used-up quota, balance, spend limit or daily cap of any provider (or OpenRouter credit), and an OpenRouter route that stops serving, print `STOP` and the kit stops cleanly; run the same command again later (the next day for a daily cap): it resumes, and other models may run in between, unless they use the limit that stopped it (the embedding's quota, or the OpenAI account of an OpenAI embedding: their embedding check then stops with the same message). Do not run `make up`, `docker compose` or edit `.env` while a run is going.

What one model run needs (v2 estimates from ADR-0014, owner decision 2026-10-01, computed for the v2 stage list and not re-measured for v3, which runs up to five guard judges per job; Task 16 re-measures; check that your accounts cover it): about 8,000 to 11,000 model requests (`gpt-4o-mini` on OpenAI, or the OpenRouter model), about 4 to 8 million input and 1 to 1.5 million output tokens, and about $1.5 to $2 for `gpt-4o-mini`, $1 for Qwen2.5-7B, $2 for Llama-3.1-8B; about 6,000 to 10,000 embedding requests (about 1 to 1.5 million tokens) per model run; up to about 3,600 reads of the meaning reader per model.

Benchmark configs (scheme v3, the only scheme a run at HEAD takes). They are the guard's own preset names above, plus `C0` (no guard code) and `C0T`; the runner refuses `--scheme v1` and `--scheme v2` (`make mailguard-bench SCHEME=v1` passes them through) and names the guard commit to check out for them, and a run folder never mixes schemes. The default campaign is every config except `C0T`, which runs only when asked for (`CONFIGS=C0T`). Every judge of every layer a config runs is live.

| Config | Active layers | Live AI stages |
|---|---|---|
| C0 | none; rag-email's own path, no guard code | none |
| C0T | guard prompt template, no layer | none |
| C1 | L1 Inbound Guard + L3 Inbound Gate | inbound_judge, inbound_extractor |
| C2 | C1 + L2 Doc Scanner + channel isolation | the above + doc_judge, doc_set_judge |
| C3 | all four layers + channel isolation (the target) | all five judges |
| C3-L1 | C3 without L1 | doc_judge, doc_set_judge, output_judge |
| C3-L2 | C3 without L2 | inbound_judge, inbound_extractor, output_judge |
| C3-L3 | C3 without L3 | all five judges |
| C3-L4 | C3 without L4 | the four inbound and document judges |
| C3-ISO | C3 without channel isolation | all five judges |

A judge that fails fails closed (human review) unless the run passes `--judge-fallback` (`JUDGE_FALLBACK=1`), which restores the v2 visible fallback. The report adds per-config exit points (`gate_pass_1`, `gate_pass_2`, `output_guard`, `delivered`), the inbound floor, guard calls per stage and fail-closed events. Committed v1/v2 run folders are read, never re-run, with `make mailguard-report RUN=<id> LEGACY=1`. The Makefile's `MAILGUARD_COMMIT` pin names the v3 guard (`1835a6e`, plan_v3 Task 15b).

Results go to `evaluation/results/mailguard_bench/<RUN>/`; `raw/` is git-ignored. Each report's "Run setup" section names the model, the pinned provider and the providers that served the calls, and the embedding. Guides: [docs/BENCHMARK.md](docs/BENCHMARK.md) (Windows/WSL2, cloud models), owner runbook [docs/demo-runbook.md](docs/demo-runbook.md) sections 9.9 and 9.10.

## AI and ML components

A full live run uses four fixed ML models, one LLM under test that fills up to eight roles, and a separate reader LLM after the run. The model under test is the only thing that changes between runs (ADR-0011, decision 1). The drawing is C3, with every layer on; the other configs switch layers off as in the table above.

```
[ ] fixed model, the same in every run          ( ) LLM role, filled by the run's one model

triage-worker     rules ─▶ [triage classifier] ─▶ (triage LLM) ─▶ gate ─▶ AI path
ai-worker or      (summarizer) ─▶ hybrid search with [embedding model] ─▶ [reranker]
guard-worker
  guard, before   L1   scan: rules ─▶ [L1 classifier] ─▶ (scan judge)
                       extract: rules + [L1 classifier] per segment ─▶ (extractor)
                  L3   inbound gate pass 1, no model
                  L2   rules + [L1 classifier] per chunk ─▶ (chunk judge) ─▶ (set judge)
                  L3   inbound gate pass 2, no model
                       channel isolation: prompt build, no model
  the call        (reply writer)
  guard, after    L4   deterministic checks ─▶ (output judge) ─▶ outbound policy + floor, no model
after the run     (meaning reader): another model reads each attack draft
```

Fixed models:

| Model | Role | Source |
|---|---|---|
| Triage classifier: TF-IDF + logistic regression | Triage stage 2, after the rules | `artifacts/models/triage_ml_v1.joblib`, in git |
| L1 injection classifier: TF-IDF + logistic regression, sigmoid-calibrated | L1 scan stage 2; the extract sub-stage reuses it per segment, the Doc Scanner per chunk | `l1_injection_clf_v1.joblib`, private, sha256-checked in `pinned/` |
| The runner's embedding model, 1536 dimensions (for example OpenAI `text-embedding-3-small` or Gemini `gemini-embedding-001`) | Embeddings for hybrid search, hosted | the runner's choice (ADR-0014), the same for every model of a comparison, recorded with each run; `EMBEDDING__*` in `.env` |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranks the retrieved chunks, on CPU | baked into the image (`Dockerfile`, `RERANK_MODEL`); the kit copies it to `.cache/reranker` for the guard-worker |

LLM roles, all on the model under test:

| Role | Calls the model when | Configs |
|---|---|---|
| Triage stage 3 | the rules and the classifier are not confident enough | all |
| Thread summarizer | the thread has more than 4 messages or more than an estimated 1,500 tokens | all |
| Reply writer | the gate sends the email down the AI path: one call, at most one repair | all, unless the Inbound Gate stops the email |
| L1 scan judge (`inbound_judge`) | L1's cheap fused score is in [0.20, 0.85) | C1, C2, C3 and the ablations that keep L1 |
| L1 extractor (`inbound_extractor`) | every email: a neutral paraphrase of the request | the same |
| L2 chunk judge (`doc_judge`) | a retrieved chunk scores in [0.30, 0.70) | configs with L2 |
| L2 set judge (`doc_set_judge`) | a kept chunk is in the suspicious band; one call, adds quarantines only | configs with L2 |
| L4 output judge (`output_judge`) | the draft is below high severity, and an injected goal was flagged or its score is at least 0.2 | configs with L4 |

How the models were chosen:
- The guard's five model roles (`inbound_judge`, `inbound_extractor`, `doc_judge`, `doc_set_judge`, `output_judge`) default to `fake`; a host names a registered model for each. The benchmark names the run's model for all five ([guard_factory.py](evaluation/mailguard_bench/guard_factory.py)) and points rag-email's fast, strong and fallback tiers and its summarizer at the same model ([model_profiles.py](evaluation/mailguard_bench/model_profiles.py), [stack_env.py](evaluation/mailguard_bench/live/stack_env.py)).
- The three models under test are the ones AgentMailGuard's own plan compares, each as reply agent and guard LLM stages ([agentmailguard/docs/experiments.md](agentmailguard/docs/experiments.md)): one hosted model, `gpt-4o-mini` (OpenAI API), and two open-weight 7-8B models, Qwen2.5-7B-Instruct and Llama-3.1-8B-Instruct. The v1 runs served the two open models locally in 4-bit builds on Ollama (owner decision 2026-09-29). The benchmark serves them through OpenRouter, each pinned to one provider with fallbacks off: `qwen/qwen-2.5-7b-instruct` on Phala (precision not stated by the provider) and `meta-llama/llama-3.1-8b-instruct` on CoreWeave at `bf16` (owner decision 2026-10-01, ADR-0014; [model_profiles.py](evaluation/mailguard_bench/model_profiles.py), [guard_models.yaml](evaluation/mailguard_bench/guard_models.yaml)). Every call's served provider is checked and recorded, and the numbers for these two since v2 are not comparable with v1's on serving. The profile `gemma-4-26b` (Gemini API) served the first test run and is not one of the three.
- The meaning reader must be independent of the models under test: [meaning.py](evaluation/mailguard_bench/meaning.py) refuses a benchmarked model, any variant of one (another tag, date, quantization or provider prefix) and the run's own generation model. Choose it and write it down before the first run (ADR-0012, decision 7).

What a full live benchmark needs (3 models × 9 configs × 550 cases, one model at a time; the 9 are the default campaign above, without C0T):

1. The keys in `.env`: `BENCH_OPENAI_API_KEY` for `gpt-4o-mini`, `BENCH_OPENROUTER_API_KEY` (with credit and a per-key limit) for the two OpenRouter models, and the embedding of your choice (the key table above).
2. The settings the host processes share with the containers, in `.env`: the `EMBEDDING__*` lines, `LLM__TIMEOUT_S=60`, `RETRIEVAL__RETRIEVAL_TIMEOUT_MS=3000` and `RETRIEVAL__CATEGORY_FILTER_ENABLED=false` (the block in [docs/BENCHMARK.md](docs/BENCHMARK.md) part C). The kit writes the containers' copy to `.env.stack`, with the tiers, the summarizer, the provider pin and the reranker, and refuses to start while `.env` or the shell disagrees.
3. For the OpenRouter models: OpenRouter privacy settings that allow Phala and CoreWeave, and before each run a credit check and `make bench-canary MODEL=<profile>` plus `make mailguard-probe MODEL=<profile>` ([docs/BENCHMARK.md](docs/BENCHMARK.md) part E).
4. The private L1 classifier in `pinned/`.
5. The checks for one model and the reader: `make bench-doctor MODEL=<profile> READER=<reader model>`.
6. One `make bench-run MODEL=<profile> RUN=<id> CONCURRENCY=2` per model (the kit's default is 1, about twice the time), then the meaning column, with the reader's endpoint set for that one command: `LLM__PROVIDER=openai LLM__OPENAI_BASE_URL=<reader endpoint> make bench-report RUN=<id> READER=<reader model>` ([docs/BENCHMARK.md](docs/BENCHMARK.md), section D7).

## Configure the models

Every LLM role in both systems, and where its model is set. Values in `<...>` are placeholders; never commit a key.

### rag-email

rag-email reads its settings through Pydantic Settings from the environment and `.env` (`packages/core/settings.py`; nested names use `__`). The app containers get them from `.env` through `docker-compose.yml`; run `make up` after a change.

| Role | Runs in | Model | Default |
|---|---|---|---|
| Triage stage 3 (only when the rules and the classifier are not confident; `TRIAGE__*` thresholds) | triage-worker | the `fast` tier: `LLM__FAST_MODEL` | `gpt-4o-mini` |
| Thread summarizer (only above `SUMMARIZATION__MIN_MESSAGES_THRESHOLD`, default 4, or `SUMMARIZATION__CONTEXT_TOKEN_THRESHOLD`, default 1500) | ai-worker | `SUMMARIZATION__SUMMARIZER_MODEL`; unset means `LLM__FAST_MODEL` | unset |
| Reply writer, routine | ai-worker | `ROUTER__DEFAULT_TIER` (`routine`), i.e. `LLM__FAST_MODEL` | `gpt-4o-mini` |
| Reply writer, escalated (low triage confidence, long thread, weak retrieval, several actions, oversized context) | ai-worker | `ROUTER__ESCALATED_TIER` (`high_capability`), i.e. `LLM__STRONG_MODEL` | `gpt-4o` |
| Fallback tier | ai-worker | `LLM__FALLBACK_MODEL`; used only when a router tier or an agent profile's `model_tier` names `fallback` (there is no automatic failover) | `claude-3-haiku` |
| Repair (at most one per job, when the reply fails schema validation) | ai-worker | the same tier and model as the reply it repairs | |
| Embeddings: the corpus, and each retrieval query | knowledge-worker; ai-worker and api | `EMBEDDING__MODEL_NAME` | `text-embedding-3-small` |

`LLM__FORCE_SINGLE_TIER=true` maps every tier to `LLM__STRONG_MODEL`. The provider:

| `LLM__PROVIDER` | Endpoint | Key | Tier models |
|---|---|---|---|
| `fake` (default) | none: canned answers, offline | none | not used |
| `openai` | `LLM__OPENAI_BASE_URL`, default `https://api.openai.com/v1`; any OpenAI-compatible `/chat/completions`, for example `https://openrouter.ai/api/v1`, `https://generativelanguage.googleapis.com/v1beta/openai`, or Ollama's `http://localhost:11434/v1` | `LLM__OPENAI_API_KEY` | `LLM__FAST_MODEL`, `LLM__STRONG_MODEL`, `LLM__FALLBACK_MODEL` |
| `anthropic` | `LLM__ANTHROPIC_BASE_URL`, default `https://api.anthropic.com/v1` | `LLM__ANTHROPIC_API_KEY` | the tier settings when `LLM__FAST_MODEL` names a `claude` model, else built-in Claude defaults |
| `local` (also `ollama`, `vllm`) | `LLM__LOCAL_BASE_URL`, default `http://localhost:11434/v1` | `LLM__LOCAL_API_KEY`, default `ollama` | fixed built-in names (`llama3.2:3b`, `llama3.3:70b`, `llama3.2:1b`); the tier settings are ignored, so for a local model of your choice use `openai` with Ollama's URL |

| Setting | Default | What it does |
|---|---|---|
| `LLM__TIMEOUT_S` | `15.0` | per-call timeout, every provider |
| `LLM__PRICE_TABLE` | gpt-4o-mini, gpt-4o, claude-3-haiku, text-embedding-3-small | JSON `{"<model>": {"input_per_m": <USD>, "output_per_m": <USD>}}`; replaces the whole table; an unpriced model's cost is recorded as unknown, never 0 |
| `LLM__OPENAI_PROVIDER_ROUTING` | unset | OpenRouter only: JSON `{"order": ["<provider slug>"], "allow_fallbacks": false, "require_parameters": true, "quantizations": ["bf16"]}` (only these keys), sent as the request's `provider` object; every call another provider served, or served after a fallback, is an error |
| `LLM__OPENAI_RESPONSE_METADATA` | `false` | asks OpenRouter which provider served each call; required (`true`) with a pin |
| `EMBEDDING__MOCK` | `true` | `true` is the offline fake embedder; `false` calls the endpoint below |
| `EMBEDDING__BASE_URL`, `EMBEDDING__API_KEY` | `https://api.openai.com/v1`, none | any OpenAI-compatible `/embeddings` endpoint, and its own key |
| `EMBEDDING__DIMENSION` | `1536` | sent as `dimensions` with every request, so the endpoint must accept that parameter (OpenAI `text-embedding-3-*` and Gemini `gemini-embedding-001` do; `text-embedding-ada-002` does not and fails every call); must equal the `VECTOR(1536)` column, or every service refuses to start (R5.10) |
| `EMBEDDING__TIMEOUT_S` | `10.0` | per-call embedding timeout |

With `LLM__PROVIDER=openai`, a Gemini or Gemma model name on the default OpenAI URL is refused at startup. Compose forwards to every app container `LLM__PROVIDER`, `LLM__OPENAI_API_KEY`, `LLM__ANTHROPIC_API_KEY`, `LLM__OPENAI_BASE_URL`, the three tier models, `LLM__PRICE_TABLE`, `EMBEDDING__MOCK`, `EMBEDDING__MODEL_NAME`, `EMBEDDING__BASE_URL`, `EMBEDDING__API_KEY`, and, only when set, `LLM__TIMEOUT_S`, `LLM__OPENAI_PROVIDER_ROUTING`, `LLM__OPENAI_RESPONSE_METADATA` and `EMBEDDING__DIMENSION`. It does not forward `LLM__LOCAL_*` or `LLM__ANTHROPIC_BASE_URL`, and the summarizer model reaches the containers only as `BENCH_SUMMARIZER_MODEL` (an older `.env`'s `SUMMARIZATION__SUMMARIZER_MODEL` line is never read by a container). Inside a container `localhost` is the container: an endpoint on the host is `host.docker.internal` (mapped for `api`, `triage-worker`, `knowledge-worker` and `ai-worker`), and a host Ollama must listen on the Docker bridge (runbook section 9.9 step 2). Every key and default: [docs/configuration.md](docs/configuration.md).

### AgentMailGuard

The guard reads its settings from the environment and from the `.env` of its working directory (`mailguard/config/settings.py`; run from `agentmailguard/` to use `agentmailguard/.env`). Each LLM stage is named by a registry entry. `fake` (the default) is the offline canned double, not an off switch: a stage left at `fake` has no LLM only while no stage built before it (inbound judge, inbound extractor, document judge, document set judge, output judge, in that order) names a real model and the host passes no `registry=`. Once one does, the guard builds its registry and every later `fake` stage answers with canned replies. To turn an LLM stage off, set its switch below to `false`, or name a real model for every stage you run. In direct use an unknown name logs a warning and turns the stage off.

| Stage | Model setting | Stage switch |
|---|---|---|
| L1 scan judge (uncertain L1 scores only) | `GUARD_MODELS__INBOUND_JUDGE` | `INBOUND_GUARD__SCAN_LLM_ENABLED`, default `true` |
| L1 extractor | `GUARD_MODELS__INBOUND_EXTRACTOR` | `INBOUND_GUARD__EXTRACT_LLM_ENABLED`, default `true` |
| L2 chunk judge (uncertain chunks only) | `GUARD_MODELS__DOC_JUDGE` | `DOC_SCANNER__LLM_ENABLED`, default **`false`**: set `true` |
| L2 set judge (adds quarantines only; one call, when a kept chunk is in the suspicious band) | `GUARD_MODELS__DOC_SET_JUDGE` | `DOC_SCANNER__SET_JUDGE_LLM_ENABLED`, default **`false`**: set `true` |
| L4 output judge (doubtful drafts only) | `GUARD_MODELS__OUTPUT_JUDGE` | `OUTPUT_GUARD__LLM_ENABLED`, default **`false`**: set `true` |

The registry is `configs/models.yaml` (`GUARD_MODELS__MODELS_PATH`, relative to `agentmailguard/`). It ships `gpt-4o-mini`, `qwen2.5-7b-instruct` and `llama-3.1-8b-instruct` (Ollama, 4-bit tags), `qwen/qwen-2.5-7b-instruct` and `meta-llama/llama-3.1-8b-instruct` (OpenRouter, pinned), and `fake`. Entry formats:

```yaml
models:
  gpt-4o-mini:                         # OpenAI
    backend: openai
    model: gpt-4o-mini
    json_mode: json_object             # json_object | json_schema | none
    timeout_s: 30                      # default 30
  meta-llama/llama-3.1-8b-instruct:    # OpenRouter, one provider pinned
    backend: openai
    model: meta-llama/llama-3.1-8b-instruct
    json_mode: json_object
    timeout_s: 120
    provider_routing: {order: [coreweave], allow_fallbacks: false, require_parameters: true, quantizations: [bf16]}
    response_metadata: true            # report the provider that served each call
  qwen2.5-7b-instruct:                 # Ollama
    backend: ollama
    tag: qwen2.5:7b-instruct           # the model as Ollama names it
    num_ctx: 8192
    timeout_s: 90                      # default OLLAMA__TIMEOUT_S (60)
```

| Backend | Endpoint | Key |
|---|---|---|
| `openai` | the entry's `base_url`, else the environment variable `OPENAI_BASE_URL`, else `https://api.openai.com/v1` | the entry's `api_key`, else the environment variable `OPENAI_API_KEY` |
| `ollama` | the entry's `base_url`, else `OLLAMA__BASE_URL` (default `http://localhost:11434`, without `/v1`) | none |
| `fake` | none | none |

`OPENAI_BASE_URL` and `OPENAI_API_KEY` are read from the process environment only (`os.getenv`), not from `.env`: export them, or put `base_url` and `api_key` in a private copy of the registry. Keep keys out of the committed `configs/models.yaml`.

### The benchmark

`MODEL=<profile>` (`--model-profile`) puts one model in every LLM role of both systems: rag-email's three tiers, the summarizer and `LLM__PRICE_TABLE`, its endpoint and key, the OpenRouter pin when there is one; and the guard's five roles from the benchmark's own registry, [evaluation/mailguard_bench/guard_models.yaml](evaluation/mailguard_bench/guard_models.yaml) (not `agentmailguard/configs/models.yaml`), with `OPENAI_BASE_URL` and `OPENAI_API_KEY` set from the profile. The configs turn the guard's layers, and with them their judges, on and off (`C3` runs all five). The `.env` lines `LLM__*` and `GUARD_MODELS__*` do not pick the benchmarked model.

| Profile | Model | Endpoint | Key |
|---|---|---|---|
| `gpt-4o-mini` | `gpt-4o-mini` | `https://api.openai.com/v1` | `BENCH_OPENAI_API_KEY` |
| `qwen2.5-7b-openrouter` | `qwen/qwen-2.5-7b-instruct`, pinned to Phala | `https://openrouter.ai/api/v1` (`BENCH_OPENROUTER_BASE_URL`) | `BENCH_OPENROUTER_API_KEY` |
| `llama-3.1-8b-openrouter` | `meta-llama/llama-3.1-8b-instruct`, pinned to CoreWeave, `bf16` | `https://openrouter.ai/api/v1` (`BENCH_OPENROUTER_BASE_URL`) | `BENCH_OPENROUTER_API_KEY` |
| `qwen2.5-7b` | `qwen2.5:7b-instruct` | Ollama, `http://localhost:11434/v1` (`BENCH_OLLAMA_BASE_URL`) | none |
| `llama-3.1-8b-local` | `llama3.1:8b` | Ollama, as above | none |
| `gemma-4-26b` (test) | `gemma-4-26b-a4b-it` | `https://generativelanguage.googleapis.com/v1beta/openai` | `LLM__OPENAI_API_KEY` |

The embedding is the runner's choice (`EMBEDDING__*` in `.env`, 1536 dimensions, the same for every model of a comparison). The meaning reader is set for one command: `LLM__PROVIDER=openai LLM__OPENAI_BASE_URL=<reader endpoint> make bench-report RUN=<id> READER=<reader model>`, with the reader's key as `LLM__OPENAI_API_KEY` in `.env`; it may not be a benchmarked model.

### Three worked examples

**OpenAI, `gpt-4o-mini`.** rag-email `.env`:

```dotenv
LLM__PROVIDER=openai
LLM__OPENAI_API_KEY=<your-openai-key>
LLM__FAST_MODEL=gpt-4o-mini
LLM__STRONG_MODEL=gpt-4o-mini
LLM__FALLBACK_MODEL=gpt-4o-mini
EMBEDDING__MOCK=false
EMBEDDING__MODEL_NAME=text-embedding-3-small
EMBEDDING__BASE_URL=https://api.openai.com/v1
EMBEDDING__API_KEY=<your-openai-key>
EMBEDDING__DIMENSION=1536
```

Guard, `agentmailguard/.env`, plus `export OPENAI_API_KEY=<your-openai-key>` in the shell:

```dotenv
GUARD_MODELS__INBOUND_JUDGE=gpt-4o-mini
GUARD_MODELS__INBOUND_EXTRACTOR=gpt-4o-mini
GUARD_MODELS__DOC_JUDGE=gpt-4o-mini
GUARD_MODELS__OUTPUT_JUDGE=gpt-4o-mini
DOC_SCANNER__LLM_ENABLED=true
OUTPUT_GUARD__LLM_ENABLED=true
```

**OpenRouter, Llama-3.1-8B pinned to CoreWeave.** rag-email `.env` (the embedding lines as in the first example):

```dotenv
LLM__PROVIDER=openai
LLM__OPENAI_BASE_URL=https://openrouter.ai/api/v1
LLM__OPENAI_API_KEY=<your-openrouter-key>
LLM__FAST_MODEL=meta-llama/llama-3.1-8b-instruct
LLM__STRONG_MODEL=meta-llama/llama-3.1-8b-instruct
LLM__FALLBACK_MODEL=meta-llama/llama-3.1-8b-instruct
LLM__OPENAI_PROVIDER_ROUTING='{"order":["coreweave"],"allow_fallbacks":false,"require_parameters":true,"quantizations":["bf16"]}'
LLM__OPENAI_RESPONSE_METADATA=true
LLM__PRICE_TABLE='{"meta-llama/llama-3.1-8b-instruct":{"input_per_m":0.22,"output_per_m":0.22}}'
```

Guard, `agentmailguard/.env` (the registry entry carries the same pin), plus `export OPENAI_BASE_URL=https://openrouter.ai/api/v1 OPENAI_API_KEY=<your-openrouter-key>`:

```dotenv
GUARD_MODELS__INBOUND_JUDGE=meta-llama/llama-3.1-8b-instruct
GUARD_MODELS__INBOUND_EXTRACTOR=meta-llama/llama-3.1-8b-instruct
GUARD_MODELS__DOC_JUDGE=meta-llama/llama-3.1-8b-instruct
GUARD_MODELS__OUTPUT_JUDGE=meta-llama/llama-3.1-8b-instruct
DOC_SCANNER__LLM_ENABLED=true
OUTPUT_GUARD__LLM_ENABLED=true
```

**Local Ollama, Qwen2.5-7B** (`ollama pull qwen2.5:7b-instruct`). rag-email `.env` (the containers reach the host's Ollama as `host.docker.internal`; a host process uses `http://localhost:11434/v1`):

```dotenv
LLM__PROVIDER=openai
LLM__OPENAI_BASE_URL=http://host.docker.internal:11434/v1
LLM__OPENAI_API_KEY=ollama
LLM__FAST_MODEL=qwen2.5:7b-instruct
LLM__STRONG_MODEL=qwen2.5:7b-instruct
LLM__FALLBACK_MODEL=qwen2.5:7b-instruct
LLM__PRICE_TABLE='{"qwen2.5:7b-instruct":{"input_per_m":0,"output_per_m":0}}'
```

Guard, `agentmailguard/.env` (the `ollama` entry `qwen2.5-7b-instruct`; no key):

```dotenv
GUARD_MODELS__INBOUND_JUDGE=qwen2.5-7b-instruct
GUARD_MODELS__INBOUND_EXTRACTOR=qwen2.5-7b-instruct
GUARD_MODELS__DOC_JUDGE=qwen2.5-7b-instruct
GUARD_MODELS__OUTPUT_JUDGE=qwen2.5-7b-instruct
DOC_SCANNER__LLM_ENABLED=true
OUTPUT_GUARD__LLM_ENABLED=true
OLLAMA__BASE_URL=http://localhost:11434
```

## rag-email: the host system

rag-email is a multi-tenant system that reads mailboxes, decides which emails need a reply, and drafts replies grounded in a knowledge base. It classifies first, retrieves only when required and generates only when necessary: one generation call per job at most. By default every approved reply becomes a provider draft; sending is a per-category `dispatch_mode: send_reply` in `config/categories.yaml`.

```
provider ──▶ mail-connector ──▶ email.normalize ──▶ email-worker ──▶ email.triage
                                                                          │
                                                                    triage-worker
                                                       rules ▶ ML ▶ LLM, then the gate
                                    ┌────────────────────────┬────────────┴────────────┐
                                    ▼                        ▼                         ▼
                           reply_required=false      workflow_hint=template        AI path
                           COMPLETED (no AI)         template, DRAFTED       email.<category>.<priority>
                                                     (no retrieval, no gen)            │
                                                                                       ▼
              ai-worker: thread context ▶ hybrid retrieval ▶ rerank ▶ router ▶ ONE generation call
                                                         ▲
                                  guard-worker takes this slot in the benchmark
                                                         ▼
                                                    DRAFTED
                                                         ▼
                              review UI (approve / edit / reject) ──▶ email.dispatch
                                                         ▼
                                       dispatch-worker ──▶ provider draft ──▶ COMPLETED
```

Where the guard attaches today: in the benchmark, `guard-worker` is the ai-worker process with `GuardedDraftingService` in place of `DraftingService`. Summary, retrieval, rerank and routing stay unchanged; the guard wraps the drafting step only, so emails that triage stops or answers by template never reach it (ADR-0010, ADR-0011). It is evaluation-only: `python -m evaluation.mailguard_bench.live.guard_worker --config C0T|C1|C2|C3|C3-L1..C3-L4|C3-ISO --run RUN --model-profile M [--scheme v3] [--judge-fallback]`, and only one guard-worker or the ai-worker container may consume the lane queues at a time. In the full design L1 and the Inbound Gate run before the call, the Doc Scanner and channel isolation on the retrieved chunks, the Output Guard on the draft; wiring the final decision into the dispatch-worker is not done.

Run the stack (needs Docker; defaults are `LLM__PROVIDER=fake` and `EMBEDDING__MOCK=true`, so no key):

```bash
cp .env.example .env         # offline defaults: fake LLM, mock embedder
make up                      # builds images, then init runs migrations, buckets, broker topology
make seed                    # optional: demo tenants (Acme, Beta, Gamma), customers, knowledge, emails
make smoke                   # end-to-end check on the running stack
make down                    # stop it
```

All app services and infrastructure should show `Up (healthy)` in `docker compose ps`; `init` is a one-shot and exits. The review UI is at <http://localhost:3001> (127.0.0.1, no login; `.env.example` sets `FRONTEND__ORGANIZATION_ID` to the demo tenant `00000000-0000-0000-0000-000000000001`, which exists after `make seed`). The API docs are at <http://localhost:8000/docs>. Postgres (5433), RabbitMQ (5672, 15672), MinIO (9010, 9011), Prometheus (9090) and Grafana (3002) publish on all interfaces with default credentials (`postgres/postgres`, `guest/guest`, `minioadmin/minioadmin`, `admin/admin`): keep them on a development machine.

Services, workers, schema and the full quick start: [docs/rag-email.md](docs/rag-email.md), [specs/](specs/) and [docs/configuration.md](docs/configuration.md).

## Repository layout

```
agentmailguard/              the guard (git subtree, pinned): mailguard/ package, configs/, training/, tests/
services/                    rag-email services: api, frontend, mail_connector, and five workers
packages/                    rag-email shared libraries (core, domain, db, broker, adapters, retrieval, llm, ...)
evaluation/mailguard_bench/  the benchmark runner, kit, scheme definitions and pinned inputs
config/  prompts/            rag-email runtime config (triage rules, categories, agent profiles) and reply prompts
migrations/                  versioned SQL migrations
docs/  specs/  tests/        guides, ADRs and runbook; requirements, design, tasks; rag-email tests
```

## Configuration

Settings are environment variables read through Pydantic Settings, from the shell or `.env`. The template is [.env.example](.env.example); every key is described in [docs/configuration.md](docs/configuration.md). Main groups: `DATABASE__*`, `BROKER__*`, `OBJECT_STORAGE__*`, `LLM__*`, `EMBEDDING__*`, `RETRIEVAL__*`, `TRIAGE__*`, `CONCURRENCY__*`, `FRONTEND__*`. The guard's own settings (`GUARD_MODELS__*`, `INBOUND_GUARD__*`, `OLLAMA__*`) are read from the `.env` of the guard's working directory (`agentmailguard/.env` when run from there); see [Configure the models](#configure-the-models).

## Tests

```bash
make ci               # fmt-check, lint, test-unit, test-integration, test-e2e
make mailguard-unit   # the whole unit suite with the guard on the import path
```

`make ci` needs Docker for the integration tests, which use their own `rag_email_test` database and vhost. The e2e tests need `uv run playwright install chromium` once. Plain `uv run pytest` skips the tests that need the guard; `make mailguard-unit` runs them with fake models, no network and no classifier. CI (`.github/workflows/ci.yml`) runs code quality (ruff, mypy, OpenAPI check), unit tests with and without the guard overlay, integration tests on ephemeral services and Playwright tests of the review UI. No test needs live credentials.

## Documentation

- [docs/rag-email.md](docs/rag-email.md): detailed README of the host system.
- [docs/BENCHMARK.md](docs/BENCHMARK.md): benchmark guide for a teammate (WSL2, cloud models, sending results back).
- [docs/benchmark-windows-native.md](docs/benchmark-windows-native.md): native Windows route with Docker Desktop.
- [docs/demo-runbook.md](docs/demo-runbook.md): owner runbook; section 9 is the benchmark.
- [docs/overview/](docs/overview/): project overview, rag-email, AgentMailGuard and its layers.
- [docs/adr/](docs/adr/): architecture decisions (0008 to 0015).
- [docs/configuration.md](docs/configuration.md), [docs/observability.md](docs/observability.md).
- [specs/](specs/): `requirements.md`, `design.md`, `tasks.md`.
- [agentmailguard/README.md](agentmailguard/README.md): the guard's own README and its own evaluation harness.

## License

MIT, see [LICENSE](LICENSE). It covers rag-email and the AgentMailGuard code under `agentmailguard/`. Third-party benchmark data keeps its own terms, listed in [evaluation/mailguard_bench/pinned/NOTICE.md](evaluation/mailguard_bench/pinned/NOTICE.md). The L1 classifier is not redistributed (ADR-0012, decision 15).
