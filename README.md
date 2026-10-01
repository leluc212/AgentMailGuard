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

| Layer | Input | How it decides | Output |
|---|---|---|---|
| L1 Email Injection Scanner | One email (rules see subject + sender + body; classifier and judge see the first 12000 chars) | Cascade by cost. Stage 1: 15 regex rule families (54 patterns) plus obfuscation heuristics. If the rule score is below 0.90, stage 2 runs a TF-IDF + calibrated logistic regression classifier, fused with the rules by noisy-OR. Stage 3, an LLM judge, runs only for fused scores in 0.20 to 0.85 and only if a judge is configured. An exception gives a HIGH fail-closed verdict. | `LayerVerdict`: severity, score 0-1, findings, indicators (every address and URL in the email) |
| L2 User Intent Extractor | The clean body (first 6000 chars); it does not see L1's verdict | Splits the body into segments, scores each alone with L1's rules and classifier, strips every segment scoring 0.50 or more, extracts entities, actions and intent by regex. An optional LLM step paraphrases the intent. | `SanitizedIntent`: sanitized body, user intent, requested actions, entities, stripped segments |
| L3 Channel Isolation | Trusted instructions and business data; the semi-trusted intent; the untrusted body, thread summary, recent messages and surviving chunks | No model. A random nonce, forged channel markers and chat-template tokens scrubbed, untrusted text cut to 6000 tokens, spotlighting (`delimit`, `datamark` default, or `encode`), nonce-tagged channels, a security-rules preamble. | `SecurePrompt` (`.messages`) |
| L3b Retrieved Document Scanner | The retrieved chunks and the retrieval query | Each chunk alone: L1's rules, obfuscation checks, PoisonedRAG heuristics (answer-forcing, instruction-override, link-insertion, query-echo) and the L1 classifier. Quarantine at fused score 0.70 or more. The LLM judge for 0.30 to 0.70 is off by default. | Kept chunks in original order, plus one `ChunkVerdict` per chunk |
| L4 Output Scanner | The draft, the email, the L2 intent, allowed citation ids, protected prompts, kept chunk texts, and what L1, L2 and L3b flagged | Deterministic stages: secret redaction (Luhn check), system-prompt leak (8-word n-gram), citation integrity, compliance with an injected goal, unsafe forward or recipient, external links. Score is the highest finding. The LLM judge is off by default and can only raise the score. | `OutputVerdict` with `redacted_text`; `run()` swaps it into the draft |
| L5 Policy Engine | The L1-L4 verdicts as summary facts (never the text); stage `inbound` or `outbound` | No model. Every matching rule in `configs/policy.yaml` (policy `2026.09-v2`) is collected and the strictest action wins: quarantine > block > human_approval > draft_only > auto_send. Any layer error gives human_approval (fail-closed). `auto_send` needs category `acknowledgement` or `scheduling`, the outbound gate and severity low or below. Default is `draft_only`. | `PolicyDecision` (action, risk tier, matched rules, reasons) and a JSONL audit line in `logs/mailguard_audit.jsonl` |

## Repository layout

```
agentmailguard/            the guard (git subtree, pinned): mailguard/ package, configs/, training/, tests/
services/                  rag-email services: api, frontend, mail_connector, and five workers
packages/                  rag-email shared libraries (core, domain, db, broker, adapters, retrieval, llm, ...)
evaluation/mailguard_bench/  the benchmark runner, kit, scheme definitions and pinned inputs
config/                    rag-email runtime config (triage rules, categories, templates, agent profiles)
prompts/                   reply prompt templates (*.v1-v3.j2)
migrations/                versioned SQL migrations
docs/                      benchmark guides, runbook, ADRs, overview, configuration
specs/                     requirements, design, task list (the source of truth for rag-email)
tests/                     unit, integration and e2e tests of rag-email
```

## Quick start

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), make, git, Docker Engine with Compose v2. Linux is the tested route. On Windows 11 Home use WSL2 with Ubuntu and Docker Engine inside it (see [docs/BENCHMARK.md](docs/BENCHMARK.md)).

```bash
git clone https://github.com/leluc212/AgentMailGuard.git
cd AgentMailGuard
git checkout main            # the guard subtree is on main, not on RAG_Email_System
uv sync
cp .env.example .env         # the defaults run offline: fake LLM, mock embedder
```

The L1 classifier is not in git. The owner sends the file `l1_injection_clf_v1.joblib` privately; it is not redistributed (ADR-0012, decision 15). Put it in `evaluation/mailguard_bench/pinned/` and check it:

```bash
cd evaluation/mailguard_bench/pinned && sha256sum -c SHA256SUMS
```

Expected sha256: `8fc1cbe74a599ab870a10ca5ff43f4a6d80b3e2273e36c7ed163c637a1d40103`. It is a scikit-learn 1.9.1 pickle. Without it the guard still runs, but L1 stage 2 is off and the benchmark refuses to start. `make bench-doctor` names what is missing.

Start the stack and check it:

```bash
make up                      # builds images, then init runs migrations, buckets, broker topology
docker compose ps --format '{{.Service}} {{.Status}}'
make smoke                   # end-to-end check on the running stack
make down                    # stop it
```

All app services and infrastructure should show `Up (healthy)`. `init` is a one-shot and exits. The review UI is at <http://localhost:3001> (127.0.0.1 only, no login). It needs `FRONTEND__ORGANIZATION_ID` in `.env`; the demo tenant is `00000000-0000-0000-0000-000000000001`. The API docs are at <http://localhost:8000/docs>.

### Use the guard from Python

Install the guard from its own folder (the `eval` and `dev` extras are optional):

```bash
cd agentmailguard && pip install -e ".[eval,dev]"
```

Then run this from `agentmailguard/` (it also works with `PYTHONPATH=.`). With default settings all guard model names are `fake`, so no LLM stage is built.

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
- `GuardConfig.preset(name)` takes `C0` (no layers), `C1` (L1+L5), `C2` (L1+L2+L3+L5), `C3` (all six), and `C3-L1` to `C3-L5` or `C3-L3B` (all minus one). Other names raise `ValueError`. For a custom layer set, build `GuardConfig` directly with the booleans `l1`, `l2`, `l3`, `l3b`, `l4`, `l5`.
- `report.inbound_decision` is the gate before generation. `report.decision` is the final gate after L4. A custom provider can be passed as `judge=`, `extractor_llm=`, `doc_llm=` or `output_llm=`.
- This cheap-only setup does not stop soft attacks: the snippet's inputs are benign, and without the classifier or a judge an attack that matches no rule falls to the default `draft_only`. Enable LLM stages with `GUARD_MODELS__JUDGE`, `GUARD_MODELS__EXTRACTOR`, `GUARD_MODELS__DOC_SCANNER`, `GUARD_MODELS__OUTPUT_JUDGE` in `.env`, set to a name from `configs/models.yaml` (`gpt-4o-mini`, `qwen2.5-7b-instruct`, `llama-3.1-8b-instruct`).
- A failed LLM stage (timeout, non-JSON output, schema mismatch) keeps the cheap result and sets `metadata.llm_fallback=true`.
- Step-wise API: `inspect_inbound`, `build_prompt`, `inspect_outbound`. Adapters for a host: `GuardedReplyAgent`, `decision_to_job_result`, `dispatch_allowed` in `mailguard.integration.adapters`.
- Demo CLI, from `agentmailguard/`:

```bash
python scripts/scan_email.py tests/fixtures/emails.json --pick attacks:0 --config C3 --kb datasets/seed/support_kb -v
```

## Run the benchmark

The benchmark runs 550 pinned cases through the real rag-email pipeline, once per config. Run the kit from the repository root, in this order:

```bash
make bench-doctor                                   # check Docker, Python, disk, keys in .env, classifier sha256
make bench-setup                                    # once per machine: pin check, guard smoke, stack up and healthy
make bench-run MODEL=gpt-4o-mini RUN=<id>           # one model through every config; a rerun with the same RUN resumes
make bench-report RUN=<id>                          # rebuild report.md, summary.json, manifest.json, metrics.csv
make bench-package RUN=<id>                         # bench-results-<id>.zip and how to commit it to branch bench/<id>
```

A small trial first:

```bash
make bench-run MODEL=gpt-4o-mini RUN=trial-gpt CONFIGS=C0,C0T,C7 LIMIT=5 CONCURRENCY=1
```

Options of `bench-run`: `CONFIGS=`, `LIMIT=n`, `CONCURRENCY=1|2`, `DRY_RUN=1`. Model profiles: `gemma-4-26b`, `gpt-4o-mini`, `llama-3.1-8b-local`, `qwen2.5-7b` (the last two run on a local Ollama, no key). Do not run `make up`, `docker compose` or edit `.env` while a run is going.

Scheme v2 (the default) configs:

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

Scheme v1 reproduces the published runs on the earlier guard pin (`SCHEME=v1` with `make mailguard-bench`); a run folder never mixes schemes.

Results go to `evaluation/results/mailguard_bench/<RUN>/`; `raw/` is git-ignored. The case set is `evaluation/datasets/mailguard/cases.jsonl` (sha256 `c00dddca6336df91bcf80de7904ad5a8335564ababd8618c7b1d953a23811d19`), drawn with a fixed seed.

Step-by-step guide, including Windows/WSL2 and local models: [docs/BENCHMARK.md](docs/BENCHMARK.md). Owner runbook: [docs/demo-runbook.md](docs/demo-runbook.md), section 9.9.

## rag-email: the host system

rag-email is a multi-tenant system that reads mailboxes, decides which emails need a reply, and drafts replies grounded in a knowledge base. It classifies first, retrieves only when required and generates only when necessary: one generation call per job at most. Every approved reply becomes a provider draft; nothing is auto-sent.

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
                                                         │       ▲
          benchmark: guard-worker replaces the           │       └── full design: L1/L2/L5 before,
          drafting step only (L1..L5 around the call)    ▼             L3b/L3 on the retrieved chunks,
                                                    DRAFTED            L4/L5 on the draft
                                                         ▼
                              review UI (approve / edit / reject) ──▶ email.dispatch
                                                         ▼
                                       dispatch-worker ──▶ provider draft ──▶ COMPLETED
```

Where the guard attaches today: in the benchmark, `guard-worker` is the ai-worker process with `GuardedDraftingService` in place of `DraftingService`. Summary, retrieval, rerank and routing stay unchanged; the guard wraps the drafting step only, so emails that triage stops or answers by template never reach it. It is a host process (`python -m evaluation.mailguard_bench.live.guard_worker --config C0T|C1..C7 --run RUN --model-profile M [--scheme v2|v1]`), evaluation-only, and rag-email adds no defence logic of its own (ADR-0010, ADR-0011). Only one guard-worker or the ai-worker container may consume the lane queues at a time. In the full design the layers sit as drawn above. Wiring the outbound decision into the dispatch-worker is not done.

| Service | Role | Host port |
|---|---|---|
| api | FastAPI `/v1`, provider webhooks, `/healthz`, `/readyz`, `/metrics` | 127.0.0.1:8000 |
| frontend | Review UI (FastAPI + Jinja2 + htmx); calls only the API | 127.0.0.1:3001 |
| mail-connector | Mailbox sync, subscription renewal | 8001 (internal) |
| email-worker | MIME parse, quoted-history split, attachments to MinIO | 8002 (internal) |
| triage-worker | Rules, ML, LLM cascade and the gate | 8003 (internal) |
| ai-worker | Context, hybrid retrieval, reply agent | 8004 (internal) |
| knowledge-worker | Parse, chunk, embed, persist documents | 8005 (internal) |
| dispatch-worker | Create the provider draft (default) or send | 8006 (internal) |
| postgres (pgvector, pg16) | Data, full-text and HNSW vector search | 5433 |
| rabbitmq 3.13 | Broker; management UI | 5672, 15672 |
| minio | Object storage; console | 9010, 9011 |
| prometheus, grafana | Metrics; Grafana has the Prometheus datasource only | 9090, 3002 |

Postgres, RabbitMQ, MinIO, Prometheus and Grafana publish on all interfaces and use default credentials (`postgres/postgres`, `guest/guest`, `minioadmin/minioadmin`, `admin/admin`). Do not expose them beyond a development machine.

Details: [docs/rag-email.md](docs/rag-email.md), [specs/](specs/) (requirements, design, tasks) and [docs/configuration.md](docs/configuration.md).

## Configuration

Settings are environment variables read through Pydantic Settings, from the shell or `.env`. The template is [.env.example](.env.example); every key is described in [docs/configuration.md](docs/configuration.md). Main groups: `DATABASE__*`, `BROKER__*`, `OBJECT_STORAGE__*`, `LLM__*`, `EMBEDDING__*`, `RETRIEVAL__*`, `TRIAGE__*`, `CONCURRENCY__*`, `FRONTEND__*`. The guard reads `GUARD_MODELS__*`.

The defaults are `LLM__PROVIDER=fake` and `EMBEDDING__MOCK=true`, so a fresh stack needs no key. A live benchmark needs real keys and `EMBEDDING__MOCK=false`; the exact `.env` block is in section 9.9 of the runbook.

## Tests

```bash
make ci               # fmt-check, lint, test-unit, test-integration, test-e2e
make mailguard-unit   # the whole unit suite with the guard on the import path
```

`make ci` needs Docker for the integration tests, which use their own `rag_email_test` database and vhost. The e2e tests need `uv run playwright install chromium` once. Plain `uv run pytest` skips the tests that need the guard; `make mailguard-unit` runs them with fake models, no network and no classifier. The guard's own unit tests run from `agentmailguard/` with `python -m pytest tests/unit -q`.

CI (`.github/workflows/ci.yml`) has these jobs: code quality (ruff format, ruff check, mypy, OpenAPI schema check), unit tests, unit tests with the guard overlay, integration tests on ephemeral services, and Playwright browser tests of the review UI. No test needs live credentials.

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
