# System Configuration & Settings Reference

**Spec Alignment:** `specs/requirements.md §R20.6, §R5.10, §R21.6` · `specs/design.md §13.3` · `GEMINI.md`

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

### 2.5 Embedding Model & Dimension (`EMBEDDING__*`)
*Semantic indexing parameters and vector width enforcement (R5.10).*

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

### 2.6 LLM Providers, Tiers & Price Table (`LLM__*`)
*Model routing, provider configuration, and inference cost accounting (R14.5, R14.7, R15.1, R21.6, R24.5).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `LLM__PROVIDER` | `string` | `fake` | `fake`, `openai`, `anthropic`, `local` | Active LLMProvider implementation (R14.7, R24.5) |
| `LLM__FAST_MODEL` | `string` | `gpt-4o-mini` | Non-empty | Tier 1 model for triage & summarization |
| `LLM__STRONG_MODEL` | `string` | `gpt-4o` | Non-empty | Tier 2 model for complex draft generation |
| `LLM__FALLBACK_MODEL` | `string` | `claude-3-haiku`| Non-empty | Tier 3 model for retry recovery |
| `LLM__FORCE_SINGLE_TIER` | `boolean` | `false` | `true/false` | Force strong model only (ablation study R15.6) |
| `LLM__TIMEOUT_S` | `float` | `15.0` | $\ge 0.1$ | Request timeout for LLM inference calls in seconds |
| `LLM__OPENAI_API_KEY` | `string` | `null` | Optional | OpenAI API secret key |
| `LLM__OPENAI_BASE_URL` | `string` | `https://api.openai.com/v1` | URL | OpenAI API base endpoint |
| `LLM__ANTHROPIC_API_KEY` | `string` | `null` | Optional | Anthropic Claude API key |
| `LLM__ANTHROPIC_BASE_URL` | `string` | `https://api.anthropic.com/v1` | URL | Anthropic Claude API base endpoint |
| `LLM__LOCAL_BASE_URL` | `string` | `http://localhost:11434/v1` | URL | OpenAI-compatible local model server base endpoint |
| `LLM__LOCAL_API_KEY` | `string` | `ollama` | Non-empty | API key for local endpoint |

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

### 2.7 Hybrid Retrieval Parameters (`RETRIEVAL__*`)
*Reciprocal Rank Fusion and cross-encoder reranking (R10, R11).*

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `RETRIEVAL__TOP_N` | `integer` | `20` | 1–100 | Candidates retrieved per search branch |
| `RETRIEVAL__RRF_K` | `integer` | `60` | $\ge 1$ | Reciprocal Rank Fusion constant ($k$) |
| `RETRIEVAL__TOP_K` | `integer` | `5` | 1–50 | Final chunk count passed to LLM context |
| `RETRIEVAL__RERANK_ENABLED` | `boolean` | `true` | `true/false` | Enable cross-encoder reranker stage |
| `RETRIEVAL__RELEVANCE_FLOOR` | `float` | `0.70` | 0.0–1.0 | Minimum reranker relevance score |
| `RETRIEVAL__RETRIEVAL_TIMEOUT_MS` | `integer` | `500` | $\ge 10$ | Retrieval timeout SLA in milliseconds |

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
| `SUMMARIZATION__SUMMARIZER_MODEL` | `string` | `gpt-4o-mini` | Non-empty | Model used for generating summaries |

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
| `ROUTING__CONFIGURED_CONSUMERS` | `list[string]` | `["email.support.*", ...]` | Valid patterns | Glob patterns defining queues with active consumers. Unconsumed queues trigger warning (R7.6) |

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
| `ROUTER_MIN_RELEVANCE_SCORE` | `float` | `0.50` | 0.0–1.0 | Minimum relevance score threshold for retrieved chunks (R15.3) |
| `ROUTER_MULTIPLE_ACTIONS_THRESHOLD` | `integer` | `2` | $\ge 1$ | Count of detected requested actions triggering escalation (R15.3) |
| `ROUTER_CONTEXT_TOKENS_THRESHOLD` | `integer` | `3500` | $\ge 1$ | Total context token estimate triggering escalation (R15.3) |
| `ROUTER_MAX_ESCALATIONS_PER_JOB` | `integer` | `1` | $\ge 0$ | Maximum allowed escalations per job to prevent retry loops (R15.5) |
| `ROUTER_SINGLE_TIER_OVERRIDE` | `string` | `high_capability` | Non-empty | Model tier to use when `force_single_tier` is true (R15.6) |

