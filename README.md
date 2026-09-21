# AgentMailGuard (`rag-email`)

> **Enterprise RAG-Based Intelligent Email Management and Response System**
>
> *Classify first. Retrieve only when required. Generate only when necessary.*

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791.svg)](https://github.com/pgvector/pgvector)
[![RabbitMQ](https://img.shields.io/badge/RabbitMQ-3.13-FF6600.svg)](https://www.rabbitmq.com/)
[![OpenTelemetry](https://img.shields.io/badge/Observability-OpenTelemetry%20%2B%20Prometheus-F5A800.svg)](https://opentelemetry.io/)
[![Tests](https://img.shields.io/badge/Tests-395%20Passing-brightgreen.svg)]()
[![Code Style](https://img.shields.io/badge/Code%20Style-Ruff%20%2B%20Mypy%20Strict-black.svg)]()

---

## 1. Overview & Governing Architecture

**AgentMailGuard** is a specification-driven, enterprise-grade email management and autonomous response pipeline. It bridges inbound enterprise mailboxes (Gmail, Microsoft Graph, IMAP) with intelligent routing, thread-aware conversation tracking, hybrid knowledge retrieval (FTS + vector ANN), and controlled LLM drafting.

### The Governing Principle

Most enterprise email automation systems fail economically because they run naive LLM inference and vector embedding across **every** incoming email. AgentMailGuard enforces strict cost discipline:

```
100,000 Inbound Emails/Day
  │
  ├── 45,000 Early Exits (45%) ──────────▶ Zero AI Inference Spend
  │   (newsletters, alerts, bounces, OOO)
  │
  ├── 20,000 Deterministic Replies (20%) ─▶ Zero Retrieval, Zero Generation
  │   (templated acknowledgements, forms)
  │
  └── 35,000 Actionable Inquiries (35%) ──▶ AI Generation (~24,500 trigger Hybrid RAG)
```

Inference compute is budgeted strictly where it creates business value.

---

## 2. End-to-End Pipeline Architecture

```
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│ Gmail / Graph   │       │  Sync           │       │  RabbitMQ       │
│ Webhook/PubSub  ├──────▶│  Orchestrator   ├──────▶│  email.normalize│
└─────────────────┘       └────────┬────────┘       └────────┬────────┘
                                   │                         │
                                   ▼                         ▼
                          ┌─────────────────┐       ┌─────────────────┐
                          │ MinIO (Raw MIME)│       │ Email Normalizer│
                          │ PostgreSQL (Job)│       │ Consumer Worker │
                          └─────────────────┘       └────────┬────────┘
                                                             │ (persist + thread associate)
                                                             ▼
                                                    ┌─────────────────┐
                                                    │  RabbitMQ       │
                                                    │  email.triage   │
                                                    └────────┬────────┘
                                                             │
                                                             ▼
                                                    ┌─────────────────┐
                                                    │ Cascading Triage│
                                                    │  (Worker Node)  │
                                                    └────────┬────────┘
                                                             │
                  ┌──────────────────────────────────────────┴──────────────────────────────────────────┐
                  ▼                                          ▼                                          ▼
     ┌──────────────────────────┐               ┌──────────────────────────┐               ┌──────────────────────────┐
     │ Stage 1: Rules Engine    │               │ Stage 2: ML Classifier   │               │ Stage 3: Small-LLM       │
     │ (~1ms, declarative YAML) │──[uncertain]─▶│ (~20-50ms, TF-IDF/Head)  │──[uncertain]─▶│ (~200-300ms fallback)    │
     └────────────┬─────────────┘               └────────────┬─────────────┘               └────────────┬─────────────┘
                  │                                          │                                          │
                  └──────────────────────────────────────────┼──────────────────────────────────────────┘
                                                             ▼
                                              Classification Result Emitted
                                                             │
                   ┌─────────────────────────────────────────┴─────────────────────────────────────────┐
                   ▼                                         ▼                                         ▼
         [reply_required=False]                  [workflow_hint='template']                 [workflow_hint='ai']
                   │                                         │                                         │
                   ▼                                         ▼                                         ▼
            Early Exit Gate                          Deterministic Path                        Actionable Queue
        (COMPLETED in DB, no AI)               (DRAFTED via Approved Template)              (email.<cat>.<priority>)
                                                                                                       │
                                                                                                       ▼
                                                                                              ┌─────────────────┐
                                                                                              │ Hybrid RAG &    │
                                                                                              │ Context Builder │
                                                                                              └────────┬────────┘
                                                                                                       │
                                                                                                       ▼
                                                                                              ┌─────────────────┐
                                                                                              │ AI Generation   │
                                                                                              │ & Dispatch      │
                                                                                              └─────────────────┘
```

---

## 3. Key System Features

### Multi-Provider Ingestion & Sync Checkpoints
- Native adapters for **Gmail** (Cloud Pub/Sub push + `historyId` sync), **Microsoft Graph** (`@odata.deltaLink` change notifications), and **IMAP** (`UIDVALIDITY` sync).
- Pluggable `MailProviderAdapter` protocol isolated in `packages/adapters/`.
- Deterministic, fixture-driven `FakeProviderAdapter` for offline CI and load testing.

### Canonical Email Normalization & MinIO Offload
- RFC 822 MIME parsing with fallback charset cascades.
- Quoted reply history separation (`body_text` vs `body_text_clean`).
- Heuristic signature block detection and stripping.
- High-volume binary attachment offloading to MinIO object storage with checksum validation.

### Distributed Broker & Atomic State Machine
- RabbitMQ messaging with durable exchanges, category/priority routing lanes, retry ladders (`.30s`, `.5m`, `.30m`), and dead-letter exchanges (`email.dlx`).
- JSON `JobEnvelope` carrying W3C trace context, deterministic idempotency keys, and classification snapshots.
- Atomic state machine: transitions (`RECEIVED` $\rightarrow$ `NORMALIZED` $\rightarrow$ `CLASSIFIED` $\rightarrow$ `ROUTED` $\rightarrow$ `CONTEXT_BUILT` $\rightarrow$ `DRAFTED` $\rightarrow$ `DISPATCHED`) and telemetry events commit in the same database transaction.

### Cascading Triage Engine (Stage 1 Live)
- Declarative, hot-reloadable rules configuration (`config/triage_rules.yaml`).
- Sub-2ms evaluation latency (measured `<0.1ms`).
- Dynamic `mtime` change detection without worker restarts and fail-safe syntax error isolation.
- Built-in detection for `Auto-Submitted`, `List-Unsubscribe`, bulk precedence, robot senders, bounces (DSN), out-of-office autoreplies, invoice references (`INV-YYYY-NNNNN`), and calendar invites.

### Hybrid RAG & Structured Storage
- PostgreSQL 16 with `pgvector` for HNSW vector search (`vector_cosine_ops`) and GIN indexes for full-text search (`search_tsv`).
- Reciprocal Rank Fusion (RRF) combining keyword and semantic recall with cross-encoder relevance reranking.
- Strict multi-tenant isolation: every query carries `organization_id`.

---

## 4. Tech Stack

| Component | Technology | Description |
|---|---|---|
| **Language** | Python 3.12+ | Monorepo managed via `uv` |
| **API Framework** | FastAPI + Pydantic V2 | Async REST endpoints, OpenAPI 3.1 specs |
| **Database** | PostgreSQL 16 + `pgvector` | Authoritative operational data & hybrid search |
| **Broker** | RabbitMQ 3.13 | AMQP 0-9-1 async messaging, retries, and DLX |
| **Object Storage** | MinIO (S3 compatible) | Raw MIME payloads and extracted attachments |
| **Observability** | OpenTelemetry, Prometheus, Grafana | W3C distributed tracing, latency histograms, metrics |
| **Tooling** | Docker Compose, Ruff, Mypy | Strict static analysis, zero lint errors |

---

## 5. Repository Structure

```
.
├── config/                  # Runtime configurations (e.g. triage_rules.yaml)
├── docs/                    # Configuration, ADRs, architecture proposals
├── evaluation/              # RAG and triage benchmark datasets and harnesses
├── migrations/              # Up/Down versioned SQL schema migrations
├── monitoring/              # Prometheus scrapers and Grafana dashboards
├── packages/                # Modular shared libraries
│   ├── core/                # Settings, object storage, idempotency, logging
│   ├── domain/              # Pure domain entities, state machine, rules (stdlib only)
│   ├── db/                  # Asyncpg pools, migration runner, entity stores
│   ├── broker/              # RabbitMQ topology, envelope, publisher, base consumer
│   ├── adapters/            # Mail adapters: gmail, graph, imap, fake
│   ├── observability/       # Metrics registry, trace span helpers
│   ├── retrieval/           # Hybrid search, RRF fusion, query builder
│   ├── llm/                 # LLMProvider protocol, structured outputs
│   ├── knowledge/           # Document parsing, chunking, embedding
│   └── business/            # Business context integrations
├── services/                # Deployable services and worker nodes
│   ├── api/                 # FastAPI read/write REST service
│   ├── mail_connector/      # Mailbox synchronization orchestrator
│   ├── email_worker/        # MIME normalization and attachment offload
│   ├── triage_worker/       # Cascading classification engine
│   ├── ai_worker/           # Hybrid RAG and LLM response drafting
│   ├── knowledge_worker/    # Knowledge base ingestion worker
│   └── dispatch_worker/     # Outbound mail delivery worker
├── specs/                   # Authoritative specification documents
│   ├── requirements.md      # Acceptance criteria (R1.1 – R24.7)
│   ├── design.md            # Architectural blueprint and contracts
│   └── tasks.md             # Work queue across Phases 0–8
└── tests/                   # Automated test suite
    ├── unit/                # Pure component unit tests (336 tests)
    ├── integration/         # Multi-tenant PostgreSQL and RabbitMQ tests (59 tests)
    └── fixtures/            # Test emails, MIME fixtures, and triage regression suite
```

---

## 6. Quickstart & Local Setup

### Prerequisites
- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- [Docker](https://docs.docker.com/get-docker/) & Docker Compose

### 1. Clone and Install Dependencies
```bash
git clone https://github.com/leluc212/AgentMailGuard.git
cd AgentMailGuard
uv sync
```

### 2. Boot Infrastructure Stack
Boot PostgreSQL (with `pgvector`), RabbitMQ, MinIO, Prometheus, and Grafana:
```bash
make up
```

### 3. Run Migrations & Seed Reference Data
```bash
make migrate
make seed
```

### 4. Run Automated Test Suites
Run all unit and multi-tenant integration tests:
```bash
make test
```
*Current test status: 395 passed, 0 failures, 0 regressions.*

### 5. Access Management & Telemetry Consoles
- **API Documentation (OpenAPI / Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **RabbitMQ Management UI**: [http://localhost:15672](http://localhost:15672) (guest / guest)
- **MinIO Storage Console**: [http://localhost:9011](http://localhost:9011) (minioadmin / minioadmin)
- **Grafana Dashboard**: [http://localhost:3002](http://localhost:3002) (admin / admin)
- **Prometheus Metrics**: [http://localhost:9090](http://localhost:9090)

---

## 7. Configuration Reference

All settings are configured through environment variables or `.env` files using Pydantic Settings validation. Refer to [docs/configuration.md](docs/configuration.md) and [.env.example](.env.example) for comprehensive options.

Key settings groups:
- `DATABASE__*`: PostgreSQL connection parameters and pool size.
- `BROKER__*`: RabbitMQ URL, exchange topology, and queue names.
- `STORAGE__*`: MinIO S3 endpoint, bucket names, and credentials.
- `TRIAGE__*`: Cascade thresholds (`rule_confidence_threshold=0.95`, `ml_confidence_threshold=0.80`, `llm_confidence_threshold=0.70`) and `TRIAGE__RULES_PATH`.
- `CONCURRENCY__*`: Worker prefetch and task concurrency limits.

---

## 8. License & Status

Developed under the **Enterprise RAG-Based Intelligent Email Management and Response System** specification.
Architectural compliance: Clean Architecture, Domain-Driven Design, strict tenant data isolation, zero mock-only stubs.
