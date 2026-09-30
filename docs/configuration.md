# System Configuration & Settings Reference

**Spec Alignment:** `specs/requirements.md §R20.6, §R5.10, §R21.6` · `specs/design.md §13.3` · `CLAUDE.md`

All configuration in `rag-email` is read from environment variables (or local `.env` file) via `pydantic-settings`. The system enforces fail-fast validation: any missing required parameter, out-of-range value, or architectural constraint violation causes the application to terminate immediately on startup.

---

## 1. General Architecture & Conventions

- **Nested Variable Delimiter:** Double underscore (`__`) maps flat environment variables to nested configuration groups (e.g. `DATABASE__PORT` maps to `settings.database.port`).
- **Fail-Fast Validation:** Validated Pydantic V2 models assert type safety, range limits, port validity, and relationship ordering (such as retry delay ascension and triage threshold ordering).
- **Service Specialization:** All services share the base `AppSettings` model while individual workers extend it with service-specific defaults (e.g., `APISettings`, `TriageWorkerSettings`).

---

## 2. Configuration Groups

### 2.1 Database Settings (`DATABASE__*`)
*Authoritative store: PostgreSQL with `pgvector` (R5.1).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `DATABASE__HOST` | `string` | `localhost` | Non-empty | PostgreSQL server hostname |
| `DATABASE__PORT` | `integer` | `5432` | 1–65535 | PostgreSQL connection port |
| `DATABASE__USER` | `string` | `postgres` | Non-empty | Database role username |
| `DATABASE__PASSWORD` | `string` | `postgres` | - | Database role password |
| `DATABASE__NAME` | `string` | `rag_email` | Non-empty | Target database name |
| `DATABASE__POOL_MIN` | `integer` | `5` | $\ge 1$ | Minimum connection pool size |
| `DATABASE__POOL_MAX` | `integer` | `20` | $\ge 1$ | Maximum connection pool size |
| `DATABASE__SSL_MODE` | `string` | `prefer` | `disable, require, prefer` | SSL connection mode |

### 2.2 Broker Settings (`BROKER__*`)
*Asynchronous messaging topology: RabbitMQ (R3.1, R3.8, R7.1).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `BROKER__HOST` | `string` | `localhost` | Non-empty | RabbitMQ broker hostname |
| `BROKER__PORT` | `integer` | `5672` | 1–65535 | AMQP broker port |
| `BROKER__USER` | `string` | `guest` | Non-empty | RabbitMQ username |
| `BROKER__PASSWORD` | `string` | `guest` | - | RabbitMQ password |
| `BROKER__VHOST` | `string` | `/` | Non-empty | RabbitMQ virtual host |
| `BROKER__EXCHANGE_MAIL_INGEST` | `string` | `mail.ingest` | Non-empty | Direct exchange for mail sync requests |
| `BROKER__EXCHANGE_EMAIL_PROCESS` | `string` | `email.process` | Non-empty | Direct exchange for email normalization |
| `BROKER__EXCHANGE_EMAIL_TRIAGE` | `string` | `email.triage` | Non-empty | Direct exchange for triage classification |
| `BROKER__EXCHANGE_EMAIL_ROUTE` | `string` | `email.route` | Non-empty | Topic exchange for routing classified emails |
| `BROKER__EXCHANGE_EMAIL_DISPATCH` | `string` | `email.dispatch` | Non-empty | Direct exchange for email dispatch |
| `BROKER__EXCHANGE_KNOWLEDGE_INGEST` | `string` | `knowledge.ingest` | Non-empty | Direct exchange for knowledge document ingestion |
| `BROKER__EXCHANGE_RETRY` | `string` | `retry.email` | Non-empty | Direct exchange for retry delay ladder |
| `BROKER__EXCHANGE_DLX` | `string` | `dlx.email` | Non-empty | Topic exchange for terminal dead-lettering |
| `BROKER__EXCHANGE_RETRY_RETURN` | `string` | `retry.return` | Non-empty | Headers exchange that routes expired retries back to their origin exchange (design §7.2) |
| `BROKER__QUEUE_MAIL_SYNC` | `string` | `mail.sync.requested` | Non-empty | Mail sync requested queue |
| `BROKER__QUEUE_NORMALIZE` | `string` | `email.normalize` | Non-empty | Email normalization queue |
| `BROKER__QUEUE_TRIAGE` | `string` | `email.triage` | Non-empty | Email triage queue |
| `BROKER__QUEUE_DISPATCH` | `string` | `email.dispatch` | Non-empty | Email dispatch queue |
| `BROKER__QUEUE_KNOWLEDGE` | `string` | `knowledge.ingest` | Non-empty | Knowledge ingestion queue |
| `BROKER__QUEUE_DEAD_LETTER` | `string` | `email.dead_letter` | Non-empty | Dead-letter queue for terminal failures |
| `BROKER__USE_QUORUM_QUEUES` | `boolean` | `false` | `true/false` | Enable quorum queues for HA deployment (R3.9) |

### 2.3 Object Storage Settings (`OBJECT_STORAGE__*`)
*Object payload store: MinIO / S3 (R5.8).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `OBJECT_STORAGE__ENDPOINT` | `string` | `localhost:9000` | Host:Port | S3 endpoint URL |
| `OBJECT_STORAGE__ACCESS_KEY` | `string` | `minioadmin` | Non-empty | S3 access key |
| `OBJECT_STORAGE__SECRET_KEY` | `string` | `minioadmin` | Non-empty | S3 secret key |
| `OBJECT_STORAGE__BUCKET_RAW_MIME` | `string` | `raw-mime` | Bucket naming | Target bucket for immutable raw MIME payloads |
| `OBJECT_STORAGE__BUCKET_ATTACHMENTS` | `string` | `attachments` | Bucket naming | Target bucket for extracted file attachments |
| `OBJECT_STORAGE__BUCKET_KNOWLEDGE` | `string` | `knowledge-docs`| Bucket naming | Target bucket for raw uploaded knowledge files |
| `OBJECT_STORAGE__BUCKET_HTML` | `string` | `html` | Bucket naming | Target bucket for the HTML part of a message (R4.1). `python -m packages.core.storage_cli bootstrap` creates it with the others. Docker Compose forwards no bucket names, so the containers use the default |
| `OBJECT_STORAGE__SECURE` | `boolean` | `false` | `true/false` | Use TLS/HTTPS for object store operations |
| `OBJECT_STORAGE__REGION` | `string` | `us-east-1` | Non-empty | S3 region identifier |

### 2.4 Provider Credential References (`PROVIDERS__*`)
*Credential pointers (R1.1 - Secrets stored by reference only, never in plaintext).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `PROVIDERS__MOCK_PROVIDERS` | `boolean` | `true` | `true/false` | Use deterministic FakeProviderAdapter in CI |
| `PROVIDERS__GMAIL_CREDENTIALS_REF` | `string` | `vault/gmail` | Non-empty | Vault secret pointer for Gmail OAuth |
| `PROVIDERS__GRAPH_CREDENTIALS_REF` | `string` | `vault/graph` | Non-empty | Vault secret pointer for Microsoft Graph OAuth |
| `PROVIDERS__IMAP_CREDENTIALS_REF` | `string` | `vault/imap` | Non-empty | Vault secret pointer for IMAP credentials |
| `GMAIL_ACCESS_TOKEN` | `string` (secret) | blank | A `ya29.` access token, about 1 hour lifetime | Token read at call time by the Gmail adapter for mailboxes with `credentials_ref=env:GMAIL_ACCESS_TOKEN` (set by `make connect-gmail`), and as the registry fallback for `gmail` mailboxes with no `credentials_ref`. Never refreshed by the stack (ADR-0009). Blank in CI; the test guard strips it (R24.5). |

- **Docker Compose.** `GMAIL_ACCESS_TOKEN` is forwarded from the host `.env` only to `mail-connector` (sync) and `dispatch-worker` (drafts and sends), not through `x-app-env`. Containers read it at start: after minting a new token, run `make up`.
- **Connecting the test account.** `make connect-gmail ADDRESS=<address>` checks that the token belongs to that address, then registers it in the demo tenant with `credentials_ref=env:GMAIL_ACCESS_TOKEN`, starting from the account's current `historyId` (`docs/demo-runbook.md` §3.4).
- **Dispatch mode** is not an environment variable: it is set per category as `dispatch_mode: create_draft | send_reply` in `config/categories.yaml` (default `create_draft`, task 6.4). The image copies `config/`, so a change needs `make up`.

### 2.5 Embedding Model & Dimension (`EMBEDDING__*`)
*Semantic indexing parameters and vector width enforcement (R5.10).*

The ai-worker and the API embed retrieval queries with this model; the knowledge worker embeds the corpus with it. Every service must use the same `EMBEDDING__MODEL_NAME` and `EMBEDDING__DIMENSION` (R5.10; the ai-worker and knowledge worker refuse to start on a dimension mismatch). Under Docker Compose, `EMBEDDING__MOCK`, `EMBEDDING__MODEL_NAME`, `EMBEDDING__BASE_URL`, `EMBEDDING__API_KEY` and `RETRIEVAL__RETRIEVAL_TIMEOUT_MS` are forwarded into the app containers, and `EMBEDDING__DIMENSION` when it is set. A hosted embedder needs more than the old 500 ms retrieval budget, so the default is now 3000 ms (§2.7): the vector branch spends that budget on the embedding call plus the ANN search, and a branch that runs out degrades retrieval to lexical. The ai-worker's query tokens and the knowledge worker's ingestion tokens are counted in `embedding_tokens_total{model}` (R9.11); `/v1/search/debug` queries are counted only with a real embedder.

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `EMBEDDING__MODEL_NAME` | `string` | `text-embedding-3-small` | Non-empty | Embedding model identifier |
| `EMBEDDING__DIMENSION` | `integer` | `1536` | 1–10000 | Vector dimension; **must equal DB column width** |
| `EMBEDDING__BATCH_SIZE` | `integer` | `64` | $\ge 1$ | Max chunks embedded per batch call |
| `EMBEDDING__MAX_TOKENS` | `integer` | `8191` | $\ge 1$ | Max context tokens supported by embedder |
| `EMBEDDING__BASE_URL` | `string` | `https://api.openai.com/v1` | URL | Base URL for OpenAI-compatible embedding API |
| `EMBEDDING__API_KEY` | `string` | `null` | Optional | API key for embedding service |
| `EMBEDDING__MOCK` | `boolean` | `true` | `true/false` | Enable FakeEmbedder for hermetic testing/CI (R24.5) |
| `EMBEDDING__TIMEOUT_S` | `float` | `10.0` | $\ge 0.1$ | Embedding request timeout in seconds |
| `EMBEDDING__MAX_RETRIES` | `integer` | `3` | $\ge 0$ | Maximum retry attempts on transient errors |
| `EMBEDDING__RETRY_DELAY_S` | `float` | `0.5` | $\ge 0.0$ | Initial retry delay in seconds |

> **Startup Dimension Assertion (R5.10):**
> On service startup, `assert_embedding_dimension(configured, db_column)` validates that `EMBEDDING__DIMENSION` matches the PostgreSQL `embedding_record.embedding VECTOR(n)` column definition. If there is any discrepancy, the service aborts immediately.

#### Gemini embeddings for the live benchmark (task 7.20)

The live v2 benchmark embeds the case knowledge and the retrieval queries with Google's `gemini-embedding-001` through its OpenAI-compatible endpoint, at 1536 dimensions, the same for every run (ADR-0011):

```dotenv
EMBEDDING__MOCK=false
EMBEDDING__MODEL_NAME=gemini-embedding-001
EMBEDDING__DIMENSION=1536
EMBEDDING__BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
EMBEDDING__API_KEY=<Gemini API key from Google AI Studio>
```

- **Dimension.** `gemini-embedding-001` returns 3072 numbers unless asked for fewer (Google documents 128 to 3072). The embedder sends `dimensions: 1536` with every request, which is the width of the vector column. Google notes that a caller must normalise vectors of any size other than 3072; retrieval ranks by cosine distance (`vector_cosine_ops`), which ignores vector length, so no normalisation step is added.
- **Response shape.** Google's endpoint may omit `index` on the items and `usage` on the response; the embedder keeps the response order and counts no tokens in that case.
- **Where the settings live.** Host tools (the guard-worker, the live runner) read these lines from `.env`. The containers get them from `.env.stack`, rendered per model by `stack_env` (§2.22), with the key read at run time from `.env`'s `LLM__OPENAI_API_KEY`: the Gemini key, which is not the LLM key of a profile such as GPT-4o-mini. Google AI Studio shows the request limits of `gemini-embedding-001` for the project.

### 2.6 LLM Providers, Tiers & Price Table (`LLM__*`)
*Model routing, provider configuration, and inference cost accounting (R14.5, R14.7, R15.1, R21.6, R24.5).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `LLM__PROVIDER` | `string` | `fake` | `fake`, `openai`, `anthropic`, `local` | Active LLMProvider implementation (R14.7, R24.5) |
| `LLM__FAST_MODEL` | `string` | `gpt-4o-mini` | Blank = default | Tier 1 (routine) model for triage, summarization and routine drafts (R15.1) |
| `LLM__STRONG_MODEL` | `string` | `gpt-4o` | Blank = default | Tier 2 (high-capability) model for escalated drafts (R15.1) |
| `LLM__FALLBACK_MODEL` | `string` | `claude-3-haiku`| Blank = default | Tier 3 model for retry recovery |
| `LLM__FORCE_SINGLE_TIER` | `boolean` | `false` | `true/false` | Force strong model only (ablation study R15.6) |
| `LLM__TIMEOUT_S` | `float` | `15.0` | $\ge 0.1$ | Request timeout for LLM inference calls in seconds. Docker Compose forwards it when set; the live benchmark's stack env (§2.22) gives the containers 60, the timeout the v1 benchmark used |
| `LLM__OPENAI_API_KEY` | `string` | `null` | Optional | OpenAI API secret key |
| `LLM__OPENAI_BASE_URL` | `string` | `https://api.openai.com/v1` | URL; blank = default | Base URL of any OpenAI-compatible `/chat/completions` endpoint, e.g. the Gemini API. With `LLM__PROVIDER=openai`, a Gemini/Gemma model on this default fails startup validation (R14.7, R20.6) |
| `LLM__ANTHROPIC_API_KEY` | `string` | `null` | Optional | Anthropic Claude API key |
| `LLM__ANTHROPIC_BASE_URL` | `string` | `https://api.anthropic.com/v1` | URL | Anthropic Claude API base endpoint |
| `LLM__LOCAL_BASE_URL` | `string` | `http://localhost:11434/v1` | URL | OpenAI-compatible local model server base endpoint |
| `LLM__LOCAL_API_KEY` | `string` | `ollama` | Non-empty | API key for local endpoint |
| `LLM__PRICE_TABLE` | `JSON object` | the four models below | `{model: {input_per_m, output_per_m}}`, USD per 1M tokens, each `≥ 0`; blank = default | Replaces the **whole** price table (not merged). Keys must match the model names providers report (e.g. `LLM__FAST_MODEL`); a local model (`LLM__PROVIDER=local`) needs its own entry (R21.6) |

**Cost Accounting Table (R21.6):**
The system maintains a configurable per-model price table to convert token usage to estimated inference costs per draft:
```python
price_table = {
    "gpt-4o-mini": {"input_per_m": 0.15, "output_per_m": 0.60},
    "gpt-4o": {"input_per_m": 5.00, "output_per_m": 15.00},
    "claude-3-haiku": {"input_per_m": 0.25, "output_per_m": 1.25},
    "text-embedding-3-small": {"input_per_m": 0.02, "output_per_m": 0.00},
}
```

A model with no entry in the table has an **unknown** cost, not a free one: its tokens are still counted, `estimated_ai_cost_total` and `generated_draft_cost_total` are not incremented, `generated_draft.cost_estimate` is stored as `NULL`, and a warning is logged per request. Add every model you route to, including local ones.

#### Hosted OpenAI-compatible endpoint: Google Gemini API (R14.7, R20.6, R21.6)

The `openai` provider talks to any OpenAI-compatible `/chat/completions` endpoint. The project's live runs use the Google Gemini API this way:

```dotenv
LLM__PROVIDER=openai
LLM__OPENAI_API_KEY=<Gemini API key from Google AI Studio>
LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM__FAST_MODEL=gemma-4-26b-a4b-it
LLM__STRONG_MODEL=gemma-4-31b-it
LLM__FALLBACK_MODEL=gemini-3.1-flash-lite
LLM__PRICE_TABLE={"gemma-4-26b-a4b-it":{"input_per_m":0,"output_per_m":0},"gemma-4-31b-it":{"input_per_m":0,"output_per_m":0},"gemini-3.1-flash-lite":{"input_per_m":0.25,"output_per_m":1.50},"text-embedding-3-small":{"input_per_m":0.02,"output_per_m":0}}
```

- **Prices.** Gemma is free of charge (free tier only); `gemini-3.1-flash-lite` costs $0.25 / $1.50 per 1M input / output tokens (Google pricing page, updated 2026-09-24). Because `LLM__PRICE_TABLE` replaces the whole table, keep every model you route to in it, or its cost is recorded as unknown.
- **Fail-fast check.** With `LLM__PROVIDER=openai`, settings validation refuses a Gemini or Gemma model name while `LLM__OPENAI_BASE_URL` is still `https://api.openai.com/v1` (with or without a trailing slash), so the key is never sent to the wrong host.
- **Docker Compose.** `LLM__PROVIDER`, `LLM__OPENAI_API_KEY`, `LLM__OPENAI_BASE_URL`, `LLM__FAST_MODEL`, `LLM__STRONG_MODEL`, `LLM__FALLBACK_MODEL` and `LLM__PRICE_TABLE` are forwarded from the host `.env` into every service that uses the shared `x-app-env` block (init, api, mail-connector, email-worker, triage-worker, knowledge-worker, ai-worker). An unset host value arrives blank, and a blank value means "use the default" for the base URL, the three model names and the price table. `LLM__TIMEOUT_S` is forwarded only when it is set (§2.22).
- **Live smoke check.** `make llm-smoke` sends one triage request and one draft request through the configured provider and reports, per request, whether the response parsed and validated, the `finish_reason` and the token counts. It never prints the key. It is run by hand and is not part of CI (R24.5); tests always run on the fake provider.

### 2.7 Hybrid Retrieval Parameters (`RETRIEVAL__*`)
*Reciprocal Rank Fusion and cross-encoder reranking (R10, R11).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `RETRIEVAL__TOP_N` | `integer` | `20` | 1–100 | Candidates retrieved per search branch |
| `RETRIEVAL__RRF_K` | `integer` | `60` | $\ge 1$ | Reciprocal Rank Fusion constant ($k$) |
| `RETRIEVAL__TOP_K` | `integer` | `5` | 1–50 | Final chunk count passed to LLM context |
| `RETRIEVAL__RERANK_ENABLED` | `boolean` | `true` | `true/false` | Enable the cross-encoder reranker stage. Honoured by the ai-worker's context builder (R11.2); off keeps the RRF order |
| `RETRIEVAL__RERANK_MODEL` | `string` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Non-empty | Cross-encoder that reranks the fused candidates (R11.1) |
| `RETRIEVAL__RERANK_MODEL_DIR` | `string` | empty | Directory, or empty | Where the cross-encoder's files are read from. Empty means the Hugging Face cache (a host run). The runtime image downloads the model at build time into a path it sets here itself, so there is no network access at runtime. Docker Compose never forwards this key: a forwarded key that nothing sets reaches the container as a bare name, which unsets the image's value. Set it only for host processes, such as the benchmark's guard-worker: the teammate kit copies the image's model folder to the git-ignored `.cache/reranker` (`docker compose cp ai-worker:/app/.cache/reranker`) and starts the guard-worker with this key pointing there, in that one process's environment (`docs/demo-runbook.md` §9.9 step 4), so the guard-worker reranks with the image's own weights and no download |
| `RETRIEVAL__RERANK_TIMEOUT_MS` | `integer` | `1000` | $\ge 10$ | Deadline for one rerank call in milliseconds. A reranker that is unavailable or slower keeps the RRF order and records the fallback (R11.5) |
| `RETRIEVAL__RELEVANCE_FLOOR` | `float` | `0.70` | 0.0–1.0 | Minimum reranker relevance score |
| `RETRIEVAL__RETRIEVAL_TIMEOUT_MS` | `integer` | `3000` | $\ge 10$ | Per-branch retrieval timeout in milliseconds (R10.9). The vector branch's budget covers query embedding plus the ANN search; a branch that exceeds it is dropped and retrieval continues on the other branch (`retrieval_degraded=true`, R10.6). 3000 leaves room for a hosted embedding call, which the earlier 500 ms default could not always fit. Used by the ai-worker and `/v1/search/debug`. |

Under Docker Compose, `RETRIEVAL__RETRIEVAL_TIMEOUT_MS` is forwarded with the 3000 default, and `RETRIEVAL__RERANK_ENABLED`, `RETRIEVAL__RERANK_MODEL` and `RETRIEVAL__RERANK_TIMEOUT_MS` only when they are set (§2.22); `RETRIEVAL__RERANK_MODEL_DIR` is never forwarded, because the image sets it. The image carries the one model named by `RETRIEVAL__RERANK_MODEL`'s default: naming another model in a container makes the reranker unavailable there, which falls back to the RRF order.

### 2.8 Cascading Triage Thresholds (`TRIAGE__*`)
*Three-stage triage cascade early exit rules (R6.1, R6.2, R6.11).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `TRIAGE__RULE_CONFIDENCE_THRESHOLD` | `float` | `0.95` | 0.0–1.0 | Rule cutoff (must be $\ge$ ML threshold) |
| `TRIAGE__ML_CONFIDENCE_THRESHOLD` | `float` | `0.80` | 0.0–1.0 | Lightweight ML classifier confidence cutoff |
| `TRIAGE__LLM_CONFIDENCE_THRESHOLD` | `float` | `0.70` | 0.0–1.0 | Small-LLM classifier confidence cutoff |
| `TRIAGE__SAFE_DEFAULT_CATEGORY` | `string` | `general_inquiry` | Non-empty | Fallback category when all stages fail |
| `TRIAGE__SAFE_DEFAULT_PRIORITY` | `string` | `normal` | Non-empty | Fallback priority when all stages fail |
| `TRIAGE__RULES_PATH` | `string` | `config/triage_rules.yaml` | Valid file path | Path to declarative triage rules YAML (R6.8) |
| `TRIAGE__ML_MODEL_PATH` | `string` | `artifacts/models/triage_ml_v1.joblib` | Valid file path | Path to trained Stage 2 ML classifier artifact (R6.1) |
| `TRIAGE__TEMPLATES_PATH` | `string` | `config/templates.yaml` | Valid file path | Deterministic reply templates; the triage worker refuses to start if a template's body file cannot be resolved (R6.13, R6.14) |

### 2.9 Thread Summarization Thresholds (`SUMMARIZATION__*`)
*Threshold-triggered conversation context compression (R8.3, R8.4).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `SUMMARIZATION__MIN_MESSAGES_THRESHOLD` | `integer` | `4` | $\ge 1$ | Message count threshold triggering summarization |
| `SUMMARIZATION__CONTEXT_TOKEN_THRESHOLD` | `integer` | `1500` | $\ge 100$ | Token threshold triggering summarization |
| `SUMMARIZATION__KEEP_LATEST_MESSAGES` | `integer` | `2` | $\ge 1$ | Verbatim messages preserved with summary |
| `SUMMARIZATION__RESUMMARIZE_LAG_MESSAGES` | `integer` | `2` | $\ge 0$ | An existing summary is refreshed only after more than this many new messages arrive (R8.4, design §5.4 LAG); `0` re-summarizes on every new message above the threshold |
| `SUMMARIZATION__SUMMARIZER_MODEL` | `string` | unset | Non-empty when set | Model that writes thread summaries (R8.3). Unset: the FAST tier model (`LLM__FAST_MODEL`) writes them. Honoured when set, so name only a model the configured `LLM__PROVIDER` endpoint serves; a model from another provider fails every summary. **An `.env` copied from `.env.example` before task 7.20 has `SUMMARIZATION__SUMMARIZER_MODEL=gpt-4o-mini`: delete that line.** Docker Compose never reads this name from `.env`: the containers get the setting only from `BENCH_SUMMARIZER_MODEL`, which the live benchmark's stack env sets to the benchmarked model (§2.22), so an old line cannot reach them. Host processes (the benchmark's guard-worker) read `.env` and do honour the line |

### 2.10 Retry Ladder Intervals & Backoff (`RETRY__*`)
*Exponential backoff with jitter and dead-lettering (R3.4, R19.5, R19.6).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `RETRY__TIER_1_DELAY_S` | `integer` | `30` | $\ge 1$ | First retry interval (30 seconds) |
| `RETRY__TIER_2_DELAY_S` | `integer` | `300` | > tier_1 | Second retry interval (5 minutes) |
| `RETRY__TIER_3_DELAY_S` | `integer` | `1800` | > tier_2 | Third retry interval (30 minutes) |
| `RETRY__MAX_RETRIES` | `integer` | `3` | 1–10 | Maximum delivery retries before DLX routing |
| `RETRY__BACKOFF_BASE_S` | `float` | `1.0` | $\ge 0.01$ | Base duration in seconds for exponential backoff |
| `RETRY__BACKOFF_FACTOR` | `float` | `2.0` | $\ge 1.0$ | Exponential growth multiplier per attempt |
| `RETRY__MAX_BACKOFF_S` | `float` | `1800.0` | $\ge 1.0$ | Ceiling cap for backoff calculation |
| `RETRY__JITTER_MODE` | `string` | `full` | full, equal, decorrelated, none | Jitter randomization strategy (R19.5) |

### 2.11 Worker Concurrency & Prefetch (`CONCURRENCY__*`)
*Independent worker scaling signals and priority lane sizing (R20.3, R7.2).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `CONCURRENCY__DEFAULT_PREFETCH` | `integer` | `10` | $\ge 1$ | AMQP message prefetch limit |
| `CONCURRENCY__MAIL_CONNECTOR_CONCURRENCY` | `integer` | `5` | $\ge 1$ | Mail connector worker concurrency |
| `CONCURRENCY__EMAIL_WORKER_CONCURRENCY` | `integer` | `10` | $\ge 1$ | Email processor worker concurrency |
| `CONCURRENCY__TRIAGE_WORKER_CONCURRENCY` | `integer` | `10` | $\ge 1$ | Triage worker concurrency |
| `CONCURRENCY__AI_WORKER_CONCURRENCY` | `integer` | `4` | $\ge 1$ | Default AI draft generation worker concurrency |
| `CONCURRENCY__AI_WORKER_NORMAL_CONCURRENCY` | `integer` | `4` | $\ge 1$ | Normal priority lane worker concurrency (R7.2) |
| `CONCURRENCY__AI_WORKER_PRIORITY_CONCURRENCY` | `integer` | `8` | $\ge 1$ | Priority lane worker concurrency (2x normal scaling) |
| `CONCURRENCY__AI_WORKER_NORMAL_PREFETCH` | `integer` | `10` | $\ge 1$ | Normal priority lane consumer prefetch QoS |
| `CONCURRENCY__AI_WORKER_PRIORITY_PREFETCH` | `integer` | `5` | $\ge 1$ | Priority lane consumer prefetch QoS (bounded latency) |
| `CONCURRENCY__KNOWLEDGE_WORKER_CONCURRENCY`| `integer` | `2` | $\ge 1$ | Knowledge document ingestion concurrency |
| `CONCURRENCY__DISPATCH_WORKER_CONCURRENCY` | `integer` | `5` | $\ge 1$ | Outbound mail dispatch concurrency |
| `CONCURRENCY__MICRO_BATCH_SIZE` | `integer` | `5` | $\ge 1$, $\le$ prefetch | Number of jobs pulled together in a micro-batch (R3.6) |
| `CONCURRENCY__MICRO_BATCH_TIMEOUT_MS` | `integer` | `50` | 1–5000 | Max wait time in ms before processing partial micro-batch (R3.6) |

### 2.12 Observability & Telemetry (`TELEMETRY__*`)
*Logging, OpenTelemetry tracing, Prometheus metrics, and service health (R21, R20.7, R20.8).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `TELEMETRY__LOG_LEVEL` | `string` | `INFO` | DEBUG, INFO, WARNING, ERROR | Standard Python logging level |
| `TELEMETRY__LOG_FORMAT` | `string` | `json` | json, text | Output format for structured log emission |
| `TELEMETRY__OTLP_ENDPOINT` | `string` | *None* | Valid URL / host:port | OpenTelemetry collector OTLP gRPC/HTTP endpoint |
| `TELEMETRY__PROMETHEUS_ENABLED` | `boolean` | `true` | Boolean | Enable Prometheus metric collection & `/metrics` |
| `TELEMETRY__METRICS_PORT` | `integer` | `9090` | 1–65535 | Dedicated port for worker metrics HTTP endpoint |
| `TELEMETRY__HEALTH_PORT` | `integer` | `8080` | 1–65535 | Dedicated port for worker health probes (`/healthz`, `/readyz`) |
| `TELEMETRY__DRAIN_TIMEOUT_S` | `float` | `15.0` | $\ge 1.0$ | Graceful shutdown drain timeout in seconds |

### 2.13 REST API Configuration (`API__*`)
*FastAPI application and operator REST API settings (R23.1, R23.6).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `API__SERVICE_NAME` | `string` | `api` | Non-empty | Service identifier for the API component |
| `API__API_PREFIX` | `string` | `/v1` | Starts with `/` | Versioned route prefix for tenant endpoints |
| `API__HOST` | `string` | `0.0.0.0` | Valid IP / hostname | Bind interface for the API server |
| `API__PORT` | `integer` | `8000` | 1–65535 | Bind port for the API server |
| `API__CORS_ORIGINS` | `list[string]` | `["http://localhost:3000", ...]` | Valid origin URLs | Allowed origins for Cross-Origin Resource Sharing |
| `API__TITLE` | `string` | `Enterprise RAG Email API` | Non-empty | OpenAPI title in generated documentation |
| `API__VERSION` | `string` | `0.1.0` | Semver | OpenAPI version string in documentation |

### 2.14 Subscription Renewal Job (`SUBSCRIPTION_RENEWAL__*`)
*Scheduled renewal parameters for mail provider push notifications (R2.10).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `SUBSCRIPTION_RENEWAL__RENEWAL_THRESHOLD_HOURS` | `integer` | `24` | $\ge 1$ | Subscriptions expiring within this window will be renewed |
| `SUBSCRIPTION_RENEWAL__CHECK_INTERVAL_SECONDS` | `integer` | `3600` | $\ge 10$ | Interval between renewal check runs in seconds |
| `SUBSCRIPTION_RENEWAL__BATCH_SIZE` | `integer` | `100` | $\ge 1$ | Maximum number of subscriptions evaluated per renewal batch |

### 2.15 Thread Association (`THREAD_ASSOCIATION__*`)
*Message-to-thread association window parameters (R4.6, design.md §5.2).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `THREAD_ASSOCIATION__WINDOW_DAYS` | `integer` | `14` | 1–365 | Time window in days for matching threads by normalized subject and overlapping participants |

### 2.16 Category Routing & Priority Lanes (`ROUTING__*`)
*Declarative category topology and priority lane configuration (R7.1, R7.2, R7.4, R7.6).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `ROUTING__CATEGORIES_CONFIG_PATH` | `string` | `config/categories.yaml` | Non-empty | Path to declarative category taxonomy YAML file (R7.4) |
| `ROUTING__PRIORITY_LANES` | `list[string]` | `["normal", "priority"]` | Non-empty | Set of priority lanes declared for each category (R7.2) |
| `ROUTING__CONFIGURED_CONSUMERS` | `list[string]` | unset (derived: every category of the taxonomy on every lane) | Valid patterns | Queue names or glob patterns defining queues with active consumers. Unset, the consumers are derived from the category taxonomy (the canonical nine plus `config/categories.yaml`, crossed with `ROUTING__PRIORITY_LANES`), so no lane is left without a consumer and a category added by configuration is consumed at once (R7.4, R7.6, v2 Amendment 1 G.2). A value, even `[]`, is used as it is; queues it does not cover trigger a startup warning (R7.6). Leave it unset (see below) |

Docker Compose forwards no `ROUTING__*` value, so the containers always run the derived default of `ROUTING__CONFIGURED_CONSUMERS` (`packages.broker.routing.resolve_configured_consumers`, which the `ai-worker`, the guard-worker and the topology check all call). Host processes read `.env`: a value there gives them other lanes than the ai-worker container consumes. For the live benchmark that is a silent fault, because C0 drafts in the ai-worker container and C0T to C3 in the guard-worker (a host process): a lane the guard-worker does not consume, such as `email.administration.priority` (the `.env.example` of before task 7.20 listed only `email.administration.normal`), leaves that lane's emails unconsumed in the guarded configs. Keep the setting out of `.env`; `stack_env` refuses an `.env` that sets it (§2.22).

#### Per-category dispatch mode (`config/categories.yaml`)
*How an approved draft leaves the system (R17.1, R17.6, R16.8, design.md §5.8, ADR-0009). These are YAML keys on each category entry, not environment variables.*

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `dispatch_mode` | `string` | `create_draft` | `create_draft` or `send_reply` (case-insensitive); any other value stops the service at startup | `create_draft`: the approved reply becomes a draft in the provider mailbox and a person sends it. `send_reply`: the approved reply is sent. Every shipped category uses `create_draft`. |
| `auto_send_eligible` | `boolean` | `false` | Boolean | Sending without an approval (R17.6). `false` for every category; dispatch starts only from an approval (`POST /v1/drafts/{id}/approve`). |

A category the classifier returns that is not registered dispatches as `create_draft`. Workers read the file at startup when they declare the broker topology (`ROUTING__CATEGORIES_CONFIG_PATH`), so a change needs a restart of the dispatch-worker.

### 2.17 Lease Reaper Configuration (`LEASE_REAPER__*`)
*Configuration for detecting and reclaiming stuck jobs past lease expiration (R19.8, design.md §9).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `LEASE_REAPER__ENABLED` | `boolean` | `false` | Boolean | Whether background periodic lease reaper is active. Off by default until leases are cleared on completion and `queue_name` is recorded (W3). |
| `LEASE_REAPER__LEASE_TIMEOUT_S` | `integer` | `300` | $\ge 10$ | Lease timeout in seconds before an active job is considered stuck |
| `LEASE_REAPER__REAPER_INTERVAL_S` | `float` | `30.0` | $\ge 1.0$ | Interval between periodic reaper sweeps in seconds |
| `LEASE_REAPER__BATCH_SIZE` | `integer` | `100` | 1–1000 | Maximum number of stuck jobs reclaimed in a single sweep |
| `LEASE_REAPER__REAP_STUCK_UNLEASED` | `boolean` | `true` | Boolean | Whether to also reclaim active jobs with NULL lease exceeding timeout |

### 2.18 Agent Profile Registry (`AGENT_PROFILES__*`)
*Declarative agent profile specialization, prompt templates, and output schemas (R14.1, R14.2, R14.6).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `AGENT_PROFILES__CONFIG_PATH` | `string` | `config/agent_profiles.yaml` | Non-empty | Path to declarative YAML profile definitions (R14.1) |
| `AGENT_PROFILES__DEFAULT_PROFILE` | `string` | `general_inquiry` | Non-empty | Fallback profile when classification category is unmapped (R14.2) |
| `AGENT_PROFILES__PROMPTS_DIR` | `string` | `prompts` | Directory | Base directory for versioned Jinja2 prompt templates (R14.6) |
| `AGENT_PROFILES__SCHEMAS_DIR` | `string` | `schemas` | Directory | Base directory for structured JSON response schemas (R14.1) |

### 2.19 Complexity Router & Model Cascade (`ROUTER_*` / `COMPLEXITY_ROUTER__*`)
*Complexity-based model cascading, escalation thresholds, and single-tier ablation evaluation (R15.1–R15.6, design.md §5.7).*

Under Docker Compose only `ROUTER_FORCE_SINGLE_TIER` and `ROUTER_CONFIDENCE_THRESHOLD` (with `LLM__PROVIDER`, the API keys, `LLM__OPENAI_BASE_URL`, the three `LLM__*_MODEL` names and `LLM__PRICE_TABLE`, see §2.6) are forwarded from the host `.env` into the app containers; set another router variable in `docker-compose.yml` before relying on it there. The per-job escalation cap (`ROUTER_MAX_ESCALATIONS_PER_JOB`, R15.5) counts escalations recorded on the job's earlier `GENERATING` transitions, so a redelivered job does not escalate again once the cap is reached.

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `ROUTER_ENABLED` | `boolean` | `true` | Boolean | Enable complexity-based model cascade routing (R15.1) |
| `ROUTER_FORCE_SINGLE_TIER` | `boolean` | `false` | Boolean | Force single tier for H4 empirical ablation study (R15.6) |
| `ROUTER_DEFAULT_TIER` | `string` | `routine` | Non-empty | Default routine tier for incoming jobs (R15.1, R15.2) |
| `ROUTER_ESCALATED_TIER` | `string` | `high_capability` | Non-empty | Target escalated tier when complexity triggers fire (R15.1, R15.3) |
| `ROUTER_CONFIDENCE_THRESHOLD` | `float` | `0.75` | 0.0–1.0 | Triage confidence score threshold below which generation escalates (R15.3) |
| `ROUTER_THREAD_MESSAGES_THRESHOLD` | `integer` | `5` | $\ge 1$ | Thread message count threshold triggering escalation (R15.3) |
| `ROUTER_THREAD_TOKENS_THRESHOLD` | `integer` | `2000` | $\ge 1$ | Thread token estimate threshold triggering escalation (R15.3) |
| `ROUTER_MIN_RETRIEVED_CHUNKS` | `integer` | `2` | $\ge 0$ | Minimum relevant chunks required to avoid escalation (R15.3) |
| `ROUTER_MIN_RELEVANCE_SCORE` | `float` | `0.50` | 0.0–1.0 | Minimum relevance score (a probability) a reranked chunk must reach to count as evidence (R15.3). Applied only when the cross-encoder rerank ran (`ContextPackage.rerank_applied`); chunks that kept their RRF order are only counted against `ROUTER_MIN_RETRIEVED_CHUNKS`, because an RRF score (at most 2/61) is not a probability (task 7.21) |
| `ROUTER_MULTIPLE_ACTIONS_THRESHOLD` | `integer` | `2` | $\ge 1$ | Count of detected requested actions triggering escalation (R15.3) |
| `ROUTER_CONTEXT_TOKENS_THRESHOLD` | `integer` | `3500` | $\ge 1$ | Total context token estimate triggering escalation (R15.3) |
| `ROUTER_MAX_ESCALATIONS_PER_JOB` | `integer` | `1` | $\ge 0$ | Maximum allowed escalations per job to prevent retry loops (R15.5) |
| `ROUTER_SINGLE_TIER_OVERRIDE` | `string` | `high_capability` | Non-empty | Model tier to use when `force_single_tier` is true (R15.6) |

### 2.20 Business Data Lookups (`BUSINESS_DATA__*`)
*Deadline and snapshot sizes for the transactional lookups the Context Builder plans in code (R13.4, R13.7, design.md §5.4, ADR-0008).*

The ai-worker decides in code which business facts to fetch before the one generation call: typed order and ticket numbers in the email are always looked up, and the routed profile's `context_policy` (`thread_plus_rag_plus_business`, set per profile in `config/agent_profiles.yaml`) or a mapped intent adds a snapshot of the sender's recent orders and open tickets. The provider call runs under `BUSINESS_DATA__TIMEOUT_MS`, and the Postgres provider also sets it as the transaction's `statement_timeout`. On timeout or error every planned fact becomes `UNAVAILABLE`, `business_data_degraded=true` is recorded on the `CONTEXT_READY` event, and the draft is still written. Under Docker Compose the three keys are forwarded into the app containers.

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `BUSINESS_DATA__TIMEOUT_MS` | `integer` | `500` | $\ge 10$ | Deadline for one business-data provider call in milliseconds; also the Postgres `statement_timeout` (R13.7) |
| `BUSINESS_DATA__SNAPSHOT_ORDERS` | `integer` | `3` | $\ge 0$ | Most recent orders (by `placed_at`) in a customer snapshot (design.md §5.4) |
| `BUSINESS_DATA__SNAPSHOT_TICKETS` | `integer` | `3` | $\ge 0$ | Newest tickets whose status is not `closed` or `resolved` in a customer snapshot (design.md §5.4) |

### 2.21 Review UI (`FRONTEND__*`)
*How the review UI (the `frontend` service) reaches the `/v1` API (R23.4–R23.7, design.md §5.8, ADR-0009).*

The review UI is server-rendered (FastAPI + Jinja2 + htmx) and calls only the `/v1` API; it holds no database or broker connection. Every request carries `FRONTEND__ORGANIZATION_ID` as the `X-Organization-Id` header (R23.6); while it is blank the pages show a setup error and call nothing. The UI has no login (ADR-0009, CLAUDE.md §6), so Docker Compose publishes it and the API on `127.0.0.1` only (`http://localhost:3001`, `http://localhost:8000`); do not publish either port on a network interface. Inside Compose the UI reaches the API at `http://api:8000`, fixed in `docker-compose.yml`; `FRONTEND__API_BASE_URL` from `.env` applies when the UI runs on the host (`uv run uvicorn services.frontend.main:app --port 3001`). Compose forwards `FRONTEND__ORGANIZATION_ID` from the host `.env` into the `frontend` container only.

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `FRONTEND__API_BASE_URL` | `string` | `http://localhost:8000` | Starts with `http://` or `https://`; a trailing `/` is dropped | Base URL of the API the review UI calls; only `/v1` paths are used |
| `FRONTEND__ORGANIZATION_ID` | `UUID` | unset | UUID, or blank for unset | Tenant whose drafts are reviewed, sent as `X-Organization-Id` (R23.6). `.env.example` sets the seeded demo tenant `00000000-0000-0000-0000-000000000001` |

### 2.22 Live benchmark stack environment (`.env.stack`)
*Per-model container settings of the live v2 benchmark (task 7.20, ADR-0011, `docs/demo-runbook.md` §9.9). Not part of the application's own settings: a generated file, read only by `docker compose`.*

The v2 benchmark runs rag-email's own services for real, with one benchmarked model per run in every LLM role. `python -m evaluation.mailguard_bench.live.stack_env --model-profile <profile>` renders the settings the containers that call a model (`api`, `triage-worker`, `knowledge-worker`, `ai-worker`) need into `.env.stack`, and prints the command that applies them:

```bash
docker compose --env-file .env --env-file .env.stack up -d --no-deps api triage-worker knowledge-worker ai-worker
```

It never runs the command; Postgres, RabbitMQ and MinIO are never restarted by it.

| Setting | Value | Source |
|---|---|---|
| `LLM__PROVIDER`, `LLM__OPENAI_BASE_URL`, `LLM__OPENAI_API_KEY`, `LLM__FAST_MODEL`, `LLM__STRONG_MODEL`, `LLM__FALLBACK_MODEL`, `LLM__PRICE_TABLE` | the model profile (`evaluation/mailguard_bench/model_profiles.py`); an endpoint on `localhost` becomes `host.docker.internal` | the profile; its key is read from `.env` |
| `LLM__TIMEOUT_S` | `60.0` (`--llm-timeout-s`) | the timeout the v1 benchmark used |
| `BENCH_SUMMARIZER_MODEL` | the profile's model | one model in every LLM role. `docker-compose.yml` maps this stack-only name to the containers' `SUMMARIZATION__SUMMARIZER_MODEL` (blank when unset: the FAST tier writes summaries), so a `SUMMARIZATION__SUMMARIZER_MODEL` line in `.env`, which an older `.env` has as `gpt-4o-mini`, is never read by a container (§2.9) |
| `EMBEDDING__MOCK`, `EMBEDDING__MODEL_NAME`, `EMBEDDING__DIMENSION`, `EMBEDDING__BASE_URL`, `EMBEDDING__API_KEY` | `false`, `gemini-embedding-001`, `1536`, Google's OpenAI-compatible endpoint, the Gemini key | the same for every run; the key is `.env`'s `LLM__OPENAI_API_KEY` |
| `RETRIEVAL__RETRIEVAL_TIMEOUT_MS`, `RETRIEVAL__RERANK_ENABLED`, `RETRIEVAL__RERANK_MODEL`, `RETRIEVAL__RERANK_TIMEOUT_MS` | `3000`, `true`, `cross-encoder/ms-marco-MiniLM-L-6-v2`, `1000` | the same for every run |

- **Keys.** The file holds API keys, so it is written owner-only (`0600`), only where git ignores the path (`.env.stack` is in `.gitignore`, and `.dockerignore` keeps it out of every build), and its keys are never printed. The tool refuses a path git could commit. Delete the file after the last run.
- **Precedence.** Compose reads the files named by `--env-file` in order, the later one winning, and `--env-file` replaces the default `.env`, which is why the printed command names both. A variable exported in the shell wins over every file, so the tool refuses to write when the shell sets a rendered setting to another value (it names the setting, never the value); keep the keys in `.env` and out of the shell.
- **Optional keys.** Compose forwards `LLM__TIMEOUT_S`, `EMBEDDING__DIMENSION` and the four `RETRIEVAL__RERANK_*` keys only when they are set: they are value-less in `docker-compose.yml`, which Compose drops when nothing resolves them, so the settings default or the image's own value applies. Do not write them into `.env` as `NAME=`: a blank value would be forwarded and would override that.
- **Host processes.** C0 drafts in the `ai-worker` container, but C0T to C3 draft in the guard-worker, a host process, and the live runner is one too. They take the model profile's `LLM__*` keys from the profile and every other setting from the shell and `.env`, never from `.env.stack`. `stack_env` therefore refuses to write while `.env` (or the shell) would give them other settings than the containers: `.env` must hold the Gemini embedding lines, `RETRIEVAL__RETRIEVAL_TIMEOUT_MS=3000` and `LLM__TIMEOUT_S=60` (the value `--llm-timeout-s` sets; `60` and `60.0` are the same number) as `docs/demo-runbook.md` §9.9 step 1 lists them; it may hold the three `RETRIEVAL__RERANK_*` lines only with the values above; `SUMMARIZATION__SUMMARIZER_MODEL` must be unset or the profile's model; and `ROUTING__CONFIGURED_CONSUMERS` must be unset (§2.16). The message names each setting and what it must be, never the value it found, which may be a key. The preflight in §9.9 step 5 prints the same settings from an `ai-worker` container and from the host, so a difference shows before any quota is spent.
- **Host alias.** `api`, `triage-worker`, `knowledge-worker` and `ai-worker` get `extra_hosts: ["host.docker.internal:host-gateway"]`. On Linux, Docker Engine resolves `host-gateway` to the host's address on the default bridge (`docker0`), so a local Ollama must listen there (`docs/demo-runbook.md` §9.9).
