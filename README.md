# AgentMailGuard

AgentMailGuard is a layered guard against prompt injection for AI email assistants. It is evaluated inside rag-email, a production-style AI email system that hosts it.

Python 3.12+, MIT license. The guard lives in [`agentmailguard/`](agentmailguard/); the host system is the rest of the repository.

## How the guard works

One email goes through six layers (L1, L2, L3, L3b, L4, L5) around the single reply-writing model call. Only an inbound `block` or `quarantine` stops the run; every other decision is returned as a label for the dispatcher to act on.

```
                        BEFORE the reply call
 email ──▶ L1 injection scan ──▶ L2 intent extract ──▶ L5 inbound gate ──┐
                                                          │ block /      │ other
                                                          │ quarantine   │ decisions
                                                          ▼              ▼
                                               return (report, None,   retrieved chunks
                                               None): no reply call         │
                                                                            ▼
                                                                  L3b scan chunks
                                                                  (drop poisoned)
                                                                            │
                                                                            ▼
                                                                  L3 build prompt
                                                                  (isolated channels)
                                                                            │
                                                                            ▼
                                                          ┌──────── REPLY MODEL ────────┐
                                                          │   generate(bundle.messages) │
                                                          └──────────────┬──────────────┘
                                                                         ▼
                        AFTER the reply call              L4 output scan (redact, check)
                                                                         ▼
                                                           L5 outbound gate ──▶ decision
```

| Layer | Input | Decision | Output |
|---|---|---|---|
| L1 Email Injection Scanner | One email | Cascade by cost: regex rules (15 families, 54 patterns) and obfuscation checks; then a TF-IDF + logistic-regression classifier, fused with the rules; then an optional LLM judge for uncertain scores only. An error is a HIGH fail-closed verdict. | `LayerVerdict`: severity, score 0-1, findings, indicators |
| L2 User Intent Extractor | The body, without L1's verdict | Scores each segment alone with L1's rules and classifier, strips the injected ones, extracts the user's intent by regex (optional LLM paraphrase). | `SanitizedIntent` |
| L3 Channel Isolation | Trusted instructions and data, the intent, untrusted text | No model. A nonce, scrubbed channel markers, spotlighting (`delimit`, `datamark` default, `encode`) and nonce-tagged channels. | `SecurePrompt` (`.messages`) |
| L3b Retrieved Document Scanner | Retrieved chunks and the query | Each chunk alone: L1's rules and classifier plus PoisonedRAG heuristics, then an optional LLM check of uncertain chunks; poisoned chunks are dropped. | Kept chunks, one `ChunkVerdict` each |
| L4 Output Scanner | The draft and what L1, L2 and L3b flagged | Deterministic checks: secret redaction, system-prompt leak, citation integrity, injected-goal compliance, unsafe recipients and links; then an optional LLM judge of doubtful drafts. | `OutputVerdict` with `redacted_text` |
| L5 Policy Engine | L1-L4 verdicts as summary facts, never the text; stage `inbound` or `outbound` | No model. Every matching rule in `configs/policy.yaml` is collected and the strictest action wins: quarantine > block > human_approval > draft_only > auto_send. A layer error forces at least `human_approval`. Default is `draft_only`; `auto_send` needs category `acknowledgement` or `scheduling`, a reply action, the outbound gate and severity low or below. | `PolicyDecision` and a JSONL audit line |

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
| Direct use (Python, CLI) | `agentmailguard/artifacts/models/l1_injection_clf_v1.joblib`, or any path in `L1__ML_MODEL_PATH` |
| Benchmark | `evaluation/mailguard_bench/pinned/`; check with `cd evaluation/mailguard_bench/pinned && sha256sum -c SHA256SUMS` |

Without the owner's file, `make mailguard-prep` downloads the datasets and trains a classifier (network, no API key) into `../AgentMailGuard-bench-artifacts`. Use it for direct use through `L1__ML_MODEL_PATH`; its hash differs from the pin, so the pinned benchmark rejects it.

See it work, from `agentmailguard/` (an injected email; with the rules alone L1 and L2 report critical and L5 quarantines it before any reply call):

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
- `GuardConfig.preset(name)` takes `C0` (no layers), `C1` (L1+L5), `C2` (L1+L2+L3+L5), `C3` (all six), and `C3-L1` to `C3-L5` or `C3-L3B` (all minus one). Other names raise `ValueError`. For a custom layer set, build `GuardConfig` directly with the booleans `l1`, `l2`, `l3`, `l3b`, `l4`, `l5`. These are the guard's own presets; the benchmark's C0-C7 below are a different set that reuses the names.
- `report.inbound_decision` is the gate before generation. `report.decision` is the final gate after L4. A custom provider can be passed as `judge=`, `extractor_llm=`, `doc_llm=` or `output_llm=`.
- This cheap-only setup does not stop soft attacks: without the classifier or a judge, an attack that matches no rule falls to the default `draft_only`. To enable the LLM stages, run `cp agentmailguard/.env.example agentmailguard/.env` (the guard reads `.env` from the current directory, so run from `agentmailguard/`) or export `GUARD_MODELS__JUDGE`, `GUARD_MODELS__EXTRACTOR`, `GUARD_MODELS__DOC_SCANNER` and `GUARD_MODELS__OUTPUT_JUDGE`, each a name from `configs/models.yaml` (`gpt-4o-mini`, `qwen2.5-7b-instruct`, `llama-3.1-8b-instruct`). `gpt-4o-mini` also needs `OPENAI_API_KEY`; the Ollama models need `OLLAMA__BASE_URL`. The root `.env` does not carry these keys, and the benchmark ignores them (it takes `MODEL=<profile>`).
- A failed LLM stage (timeout, non-JSON output, schema mismatch) keeps the cheap result and sets `metadata.llm_fallback=true`.
- Step-wise API: `inspect_inbound`, `build_prompt`, `inspect_outbound`. Adapters for a host: `GuardedReplyAgent`, `decision_to_job_result`, `dispatch_allowed` in `mailguard.integration.adapters`.
- Unit tests of the guard: `python -m pytest tests/unit -q` from `agentmailguard/`.

## Run the benchmark

The benchmark runs 550 pinned cases through the real rag-email pipeline, once per config. The cases are 300 LLMail-Inject attack emails, 150 benign emails and 100 RAG-poisoning cases, drawn with seed 20260930 (each draw on its own derived seed) from public datasets. Sources and licenses: [pinned/NOTICE.md](evaluation/mailguard_bench/pinned/NOTICE.md); case ids per set: `evaluation/datasets/mailguard/manifest.json`. The case file `evaluation/datasets/mailguard/cases.jsonl` has sha256 `c00dddca6336df91bcf80de7904ad5a8335564ababd8618c7b1d953a23811d19`. After `make mailguard-prep`, `make mailguard-cases` rebuilds the set and fails unless it reproduces the manifest's ids and hash.

Needs Docker Engine with Compose v2, make, and the classifier in `pinned/`. Linux is the tested route; on Windows 11 Home use WSL2 with Ubuntu and Docker Engine inside it ([docs/BENCHMARK.md](docs/BENCHMARK.md)). Keys, set in `.env` (names only; never print or commit them):

| Key | Needed for |
|---|---|
| `LLM__OPENAI_API_KEY` and the same value in `EMBEDDING__API_KEY` (a Gemini key), with `EMBEDDING__MOCK=false` | Embeddings, every live run; also the `gemma-4-26b` profile |
| `BENCH_OPENAI_API_KEY` | `gpt-4o-mini` |
| `BENCH_OLLAMA_BASE_URL` (optional) | `qwen2.5-7b`, `llama-3.1-8b-local`: Ollama serves them, no key; the default host is `localhost:11434`, moved by this key |

The `BENCH_*` keys are not in `.env.example`; the exact block is in section 9.9 of the runbook. Plan for at least 25 GB of free disk (the doctor warns below it) and hours per model: 4 to 6 for `gpt-4o-mini` and 12 to 15 for a local model, extrapolated from a 7-case smoke run ([docs/BENCHMARK.md](docs/BENCHMARK.md)). Run the kit from the repository root, in this order:

```bash
make bench-doctor                                   # check Docker, Python, disk, keys in .env, classifier sha256
make bench-setup                                    # once per machine: pin check, guard smoke, stack up and healthy
make bench-run MODEL=gpt-4o-mini RUN=<id>           # one model through every config; a rerun with the same RUN resumes
make bench-report RUN=<id>                          # rebuild report.md, summary.json, manifest.json, metrics.csv
make bench-package RUN=<id>                         # bench-results-<id>.zip and how to commit it to branch bench/<id>
```

A small trial first: `make bench-run MODEL=gpt-4o-mini RUN=trial-gpt CONFIGS=C0,C0T,C7 LIMIT=5 CONCURRENCY=1`. Options of `bench-run`: `CONFIGS=`, `LIMIT=n`, `CONCURRENCY=1|2`, `DRY_RUN=1`. Model profiles: `gemma-4-26b`, `gpt-4o-mini`, `llama-3.1-8b-local`, `qwen2.5-7b`. Do not run `make up`, `docker compose` or edit `.env` while a run is going.

Benchmark configs (scheme v2, the default). These names are not the Python `GuardConfig.preset` names above; scheme v1 used the preset meanings (`SCHEME=v1` with `make mailguard-bench` reproduces the published runs on the earlier guard pin), and a run folder never mixes schemes.

| Config | Active layers | Live AI stages |
|---|---|---|
| C0 | none; rag-email's own path, no guard code | none |
| C0T | guard prompt template, no layer | none |
| C1 | L1 + L5 | l1.judge |
| C2 | L2 + L5 | l2.llm |
| C3 | L3 + L5 | none |
| C4 | L3b + L5 | l3b.llm |
| C5 | L4 + L5 | l4.llm |
| C6 | L5 alone (control) | none |
| C7 | L1, L2, L3, L3b, L4, L5 (all) | all four |

Results go to `evaluation/results/mailguard_bench/<RUN>/`; `raw/` is git-ignored. Guides: [docs/BENCHMARK.md](docs/BENCHMARK.md) (Windows/WSL2, local models), owner runbook [docs/demo-runbook.md](docs/demo-runbook.md) section 9.9.

## AI and ML components

A full live run uses four fixed ML models, one LLM under test that fills up to seven roles, and a separate reader LLM after the run. The model under test is the only thing that changes between runs (ADR-0011, decision 1). The drawing is C7, with every layer on; the other configs switch layers off as in the table above.

```
[ ] fixed model, the same in every run          ( ) LLM role, filled by the run's one model

triage-worker     rules ─▶ [triage classifier] ─▶ (triage LLM) ─▶ gate ─▶ AI path
ai-worker or      (summarizer) ─▶ hybrid search with [Gemini embeddings] ─▶ [reranker]
guard-worker
  guard, before   L1   rules ─▶ [L1 classifier] ─▶ (L1 judge)
                  L2   rules + [L1 classifier] per segment ─▶ (L2 extractor)
                  L5   inbound gate, no model
                  L3b  rules + [L1 classifier] per chunk ─▶ (L3b scanner)
                  L3   prompt build, no model
  the call        (reply writer)
  guard, after    L4   deterministic checks ─▶ (L4 judge)
                  L5   outbound gate, no model
after the run     (meaning reader): another model reads each attack draft
```

Fixed models:

| Model | Role | Source |
|---|---|---|
| Triage classifier: TF-IDF + logistic regression | Triage stage 2, after the rules | `artifacts/models/triage_ml_v1.joblib`, in git |
| L1 injection classifier: TF-IDF + logistic regression, sigmoid-calibrated | L1 stage 2; L2 reuses it per segment, L3b per chunk | `l1_injection_clf_v1.joblib`, private, sha256-checked in `pinned/` |
| Gemini `gemini-embedding-001`, 1536 dimensions | Embeddings for hybrid search, hosted | set by the kit; needs the Gemini key |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranks the retrieved chunks, on CPU | baked into the image (`Dockerfile`, `RERANK_MODEL`); the kit copies it to `.cache/reranker` for the guard-worker |

LLM roles, all on the model under test:

| Role | Calls the model when | Configs |
|---|---|---|
| Triage stage 3 | the rules and the classifier are not confident enough | all |
| Thread summarizer | the thread has more than 4 messages or more than an estimated 1,500 tokens | all |
| Reply writer | the gate sends the email down the AI path: one call, at most one repair | all, unless L5's inbound gate stops the email |
| L1 judge | L1's cheap fused score is in [0.20, 0.85) | C1, C7 |
| L2 extractor | every email: a neutral paraphrase of the request | C2, C7 |
| L3b scanner | a retrieved chunk scores in [0.30, 0.70) | C4, C7 |
| L4 judge | the draft is below high severity, and an injected goal was flagged or its score is at least 0.2 | C5, C7 |

How the models were chosen:
- The guard's four model roles (`judge`, `extractor`, `doc_scanner`, `output_judge`) default to `fake`; a host names a registered model for each. The benchmark names the run's model for all four ([guard_factory.py](evaluation/mailguard_bench/guard_factory.py)) and points rag-email's fast, strong and fallback tiers and its summarizer at the same model ([model_profiles.py](evaluation/mailguard_bench/model_profiles.py), [stack_env.py](evaluation/mailguard_bench/live/stack_env.py)).
- The three models under test are the ones AgentMailGuard's own plan compares, each as reply agent and guard LLM stages ([agentmailguard/docs/experiments.md](agentmailguard/docs/experiments.md)): one hosted model, `gpt-4o-mini` (OpenAI API), and two open-weight 7-8B models run locally in 4-bit builds on Ollama, `qwen2.5:7b-instruct` and `llama3.1:8b`. Owner decision 2026-09-29, recorded in [guard_models.yaml](evaluation/mailguard_bench/guard_models.yaml). The fourth profile, `gemma-4-26b` (Gemini API), served the first test run and is not one of the three.
- The meaning reader must be independent of the models under test: [meaning.py](evaluation/mailguard_bench/meaning.py) refuses a benchmarked model, any variant of one (another tag, date, quantization or provider prefix) and the run's own generation model. Choose it and write it down before the first run (ADR-0012, decision 7).

What a full live benchmark needs (3 models × 9 configs × 550 cases, one model at a time):

1. The Gemini key in `.env` for the embeddings, and `BENCH_OPENAI_API_KEY` for `gpt-4o-mini` (the key table above).
2. The settings the host processes share with the containers, in `.env`: the `EMBEDDING__*` lines, `LLM__TIMEOUT_S=60`, `RETRIEVAL__RETRIEVAL_TIMEOUT_MS=3000` and `RETRIEVAL__CATEGORY_FILTER_ENABLED=false` (the block in runbook section 9.9). The kit writes the containers' copy to `.env.stack`, with the tiers, the summarizer and the reranker, and refuses to start while `.env` or the shell disagrees.
3. For the local models: `ollama pull qwen2.5:7b-instruct` and `ollama pull llama3.1:8b`, then load each one before its run, for example `ollama run qwen2.5:7b-instruct "Reply with OK" < /dev/null`. The live runner refuses a model that is not loaded.
4. The private L1 classifier in `pinned/`.
5. The checks for one model and the reader: `uv run python -m evaluation.mailguard_bench.kit.doctor --model-profile qwen2.5-7b --reader <reader model>` (`make bench-doctor` runs the checks without either).
6. One `make bench-run MODEL=<profile> RUN=<id>` per model, then the meaning column, with the reader's endpoint set for that one command: `LLM__PROVIDER=openai LLM__OPENAI_BASE_URL=<reader endpoint> make bench-report RUN=<id> READER=<reader model>` ([docs/BENCHMARK.md](docs/BENCHMARK.md), section D7).

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

Where the guard attaches today: in the benchmark, `guard-worker` is the ai-worker process with `GuardedDraftingService` in place of `DraftingService`. Summary, retrieval, rerank and routing stay unchanged; the guard wraps the drafting step only, so emails that triage stops or answers by template never reach it (ADR-0010, ADR-0011). It is evaluation-only: `python -m evaluation.mailguard_bench.live.guard_worker --config C0T|C1..C7 --run RUN --model-profile M [--scheme v2|v1]`, and only one guard-worker or the ai-worker container may consume the lane queues at a time. In the full design L1/L2/L5 run before the call, L3b/L3 on the retrieved chunks, L4/L5 on the draft; wiring the outbound decision into the dispatch-worker is not done.

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

Settings are environment variables read through Pydantic Settings, from the shell or `.env`. The template is [.env.example](.env.example); every key is described in [docs/configuration.md](docs/configuration.md). Main groups: `DATABASE__*`, `BROKER__*`, `OBJECT_STORAGE__*`, `LLM__*`, `EMBEDDING__*`, `RETRIEVAL__*`, `TRIAGE__*`, `CONCURRENCY__*`, `FRONTEND__*`. The guard's own settings (`GUARD_MODELS__*`, `L1__*`, `OLLAMA__*`) are read from `agentmailguard/.env`, see above.

## Tests

```bash
make ci               # fmt-check, lint, test-unit, test-integration, test-e2e
make mailguard-unit   # the whole unit suite with the guard on the import path
```

`make ci` needs Docker for the integration tests, which use their own `rag_email_test` database and vhost. The e2e tests need `uv run playwright install chromium` once. Plain `uv run pytest` skips the tests that need the guard; `make mailguard-unit` runs them with fake models, no network and no classifier. CI (`.github/workflows/ci.yml`) runs code quality (ruff, mypy, OpenAPI check), unit tests with and without the guard overlay, integration tests on ephemeral services and Playwright tests of the review UI. No test needs live credentials.

## Documentation

- [docs/rag-email.md](docs/rag-email.md): detailed README of the host system.
- [docs/BENCHMARK.md](docs/BENCHMARK.md): benchmark guide for a teammate (WSL2, local models, sending results back).
- [docs/benchmark-windows-native.md](docs/benchmark-windows-native.md): native Windows route with Docker Desktop.
- [docs/demo-runbook.md](docs/demo-runbook.md): owner runbook; section 9 is the benchmark.
- [docs/overview/](docs/overview/): project overview, rag-email, AgentMailGuard and its layers.
- [docs/adr/](docs/adr/): architecture decisions (0008 to 0013).
- [docs/configuration.md](docs/configuration.md), [docs/observability.md](docs/observability.md).
- [specs/](specs/): `requirements.md`, `design.md`, `tasks.md`.
- [agentmailguard/README.md](agentmailguard/README.md): the guard's own README and its own evaluation harness.

## License

MIT, see [LICENSE](LICENSE). It covers rag-email and the AgentMailGuard code under `agentmailguard/`. Third-party benchmark data keeps its own terms, listed in [evaluation/mailguard_bench/pinned/NOTICE.md](evaluation/mailguard_bench/pinned/NOTICE.md). The L1 classifier is not redistributed (ADR-0012, decision 15).
