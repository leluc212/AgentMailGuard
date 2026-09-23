# Execution Log: Phase 0 Task 0.1 — Repository Scaffold

## Step 1: Scaffold Repository Directory Tree
- **Files Changed:**
  - `services/{api,mail_connector,email_worker,triage_worker,ai_worker,knowledge_worker,dispatch_worker}/__init__.py`
  - `packages/{core,domain,db,broker,adapters,llm,retrieval,knowledge,business,observability}/__init__.py`
  - `migrations/.gitkeep`, `frontend/.gitkeep`, `evaluation/{datasets,experiments,results}/.gitkeep`, `docs/adr/.gitkeep`
  - `tests/{unit,integration,e2e}/__init__.py`
- **What Changed:**
  - Created complete directory layout specified in `specs/design.md §4`.
  - Initialized Python package identifiers (`__init__.py`) across all service and package components.
  - Placed tracking placeholders (`.gitkeep`) in non-Python data directories.
- **Verification Command:**
  - `python3 -c "import os; dirs = ['services/api', 'packages/domain', 'migrations', 'evaluation/datasets', 'tests/unit', 'docs/adr']; assert all(os.path.isdir(d) for d in dirs), 'Missing dirs'"`
- **Result:** PASS

## Step 2: Configure Python 3.12 Monorepo Workspace via uv
- **Files Changed:**
  - `.python-version`
  - `pyproject.toml`
  - `.gitignore`
  - `README.md`
  - `uv.lock`
- **What Changed:**
  - Pinned Python version to 3.12.
  - Configured root `pyproject.toml` with project dependencies (FastAPI, Pydantic, asyncpg, aio-pika, OpenTelemetry, Prometheus, pgvector) and dev tools (pytest, ruff, mypy).
  - Generated frozen `uv.lock` and synced virtual environment under `.venv`.
- **Verification Command:**
  - `uv lock && uv sync`
- **Result:** PASS

## Step 3: Implement Automated Architectural Dependency Guard
- **Files Changed:**
  - `tests/unit/test_dependency_rules.py`
- **What Changed:**
  - Implemented AST-based tests enforcing the architectural boundary rules.
  - Asserted that `packages/*` never imports `services/*`.
  - Asserted that `packages/domain` imports only stdlib and `packages/core`.
  - Asserted that mail provider names (`gmail`, `graph`, `imap`) appear only within `packages/adapters/`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_dependency_rules.py -v`
- **Result:** PASS (3 passed in 0.04s)

## Step 4: Implement Makefile Task Targets
- **Files Changed:**
  - `Makefile`
- **What Changed:**
  - Configured all 8 required targets from R24.1 (`up`, `down`, `migrate`, `seed`, `test`, `lint`, `eval`, `load`).
  - Wired `lint` to `ruff` + `mypy` and `test` to `pytest tests/unit`.
  - Added placeholders for infrastructure and evaluation workflows.
- **Verification Command:**
  - `make lint && make test && make -n up down migrate seed eval load`
- **Result:** PASS

## Step 5: Verification & Task 0.1 Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
- **What Changed:**
  - Validated full test and lint suite cleanly passing.
  - Marked Task 0.1 as completed (`[x]`).
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS

# Execution Log: Phase 0 Task 0.2 — Configuration & Settings

## Step 1: Implement Core Settings Models & Groups
- **Files Changed:**
  - `packages/core/settings.py`
  - `packages/core/__init__.py`
- **What Changed:**
  - Implemented Pydantic V2 settings models for all 11 architectural groups: Database, Broker, Object Storage, Provider Credential Refs, Embedding, LLM Tiers & Price Table, Retrieval, Triage, Summarization, Retry Ladder, and Worker Concurrency.
  - Implemented top-level `AppSettings` and per-service subclasses (`APISettings`, `WorkerSettings`, etc.).
  - Added startup vector dimension assertion helper `assert_embedding_dimension()` (R5.10).
- **Verification Command:**
  - `uv run python -c "from packages.core.settings import AppSettings; s = AppSettings(); assert s.embedding.dimension == 1536"`
- **Result:** PASS

## Step 2: Implement Unit Tests for Settings & Fail-Fast Validations
- **Files Changed:**
  - `tests/unit/test_settings.py`
- **What Changed:**
  - Added unit test suite covering all 11 settings groups and specialized service classes.
  - Verified environment variable override behavior (`DATABASE__PORT`, `EMBEDDING__DIMENSION`, etc.).
  - Verified fail-fast validation for out-of-range ports, invalid confidence thresholds, invalid threshold order, illegal retry ladders, and negative prices.
  - Verified startup assertion for embedding vector dimensionality (R5.10).
- **Verification Command:**
  - `uv run pytest tests/unit/test_settings.py -v`
- **Result:** PASS (9/9 passed in 0.27s)

## Step 3: Generate .env.example
- **Files Changed:**
  - `.env.example`
- **What Changed:**
  - Created root `.env.example` documenting all configuration keys organized across the 11 architectural groups.
  - Supplied safe local development defaults aligned with local Docker Compose services.
- **Verification Command:**
  - `uv run python -c "from packages.core.settings import AppSettings; from dotenv import dotenv_values; s = AppSettings(**dotenv_values('.env.example'))"`
- **Result:** PASS

## Step 4: Author docs/configuration.md
- **Files Changed:**
  - `docs/configuration.md`
- **What Changed:**
  - Created complete documentation reference for all 11 configuration groups.
  - Documented environment variable mappings, types, defaults, validation rules, and consuming services.
  - Detailed startup embedding dimension assertion (R5.10) and per-model price table (R21.6).
- **Verification Command:**
  - `python3 -c "import os; assert os.path.exists('docs/configuration.md') and os.path.getsize('docs/configuration.md') > 1000"`
- **Result:** PASS (10,201 bytes written)

## Step 5: Verification, Lint & Task 0.2 Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
- **What Changed:**
  - Verified static formatting (`ruff`), type checking (`mypy` strict mode with `pydantic.mypy`), and unit test suite.
  - Marked Task 0.2 as completed (`[x]`) in `specs/tasks.md`.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (12/12 tests passed, 0 lint/mypy errors across 26 source files)

# Execution Log: Phase 0 Task 0.3 — Docker Compose Stack

## Step 1: Create Prometheus Configuration
- **Files Changed:**
  - `monitoring/prometheus/prometheus.yml`
- **What Changed:**
  - Configured Prometheus scraping intervals (15s).
  - Defined scrape jobs for Prometheus itself, `api`, and all 6 worker classes.
- **Verification Command:**
  - `python3 -c "import os; assert os.path.exists('monitoring/prometheus/prometheus.yml')"`
- **Result:** PASS

## Step 2: Create Application Dockerfile & Healthcheck Placeholder
- **Files Changed:**
  - `services/placeholder.py`
  - `.dockerignore`
  - `Dockerfile`
- **What Changed:**
  - Implemented lightweight HTTP server in `services/placeholder.py` serving `/healthz`, `/readyz`, and `/metrics`.
  - Added `.dockerignore` to streamline build context.
  - Built base Docker image (`rag-email-service:test`) packaging services and dependencies.
- **Verification Command:**
  - `docker build -t rag-email-service:test .`
- **Result:** PASS

## Step 3: Author docker-compose.yml with Healthchecks & Dependencies
- **Files Changed:**
  - `docker-compose.yml`
- **What Changed:**
  - Declared all 13 services defined in R20.1 across Infrastructure, Application, and Observability tiers.
  - Added container healthchecks with curl, wget, pg_isready, and rabbitmq-diagnostics.
  - Enforced strict startup dependency graphs (`depends_on: condition: service_healthy`).
- **Verification Command:**
  - `docker compose config`
- **Result:** PASS

## Step 4: Author docker-compose.scale.yml & Wire Makefile
- **Files Changed:**
  - `docker-compose.scale.yml`
  - `docker-compose.yml`
- **What Changed:**
  - Created `docker-compose.scale.yml` with replica scale configuration for workers (email-worker, triage-worker, ai-worker, etc.).
  - Configured worker containers with internal exposure for horizontal replica scaling without port collisions.
  - Validated both standard and scaled compose topologies.
- **Verification Command:**
  - `docker compose config && docker compose -f docker-compose.yml -f docker-compose.scale.yml config`
- **Result:** PASS

## Step 5: Verification, Healthcheck Validation & Task 0.3 Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
- **What Changed:**
  - Verified `make up` boots all 13 services (`postgres`, `rabbitmq`, `minio`, `prometheus`, `grafana`, `frontend`, `api`, and 6 worker classes) in healthy state.
  - Confirmed PostgreSQL `vector` and `pg_trgm` extensions active.
  - Confirmed RabbitMQ management UI accessible.
  - Confirmed Prometheus scraping targets and Grafana running.
  - Confirmed unit test suite (12/12 passing) and code quality (ruff + mypy 0 issues).
  - Marked Task 0.3 complete (`[x]`).
- **Verification Command:**
  - `docker compose ps && make lint && make test`
- **Result:** PASS (13/13 containers healthy, 12/12 unit tests passing, ruff & mypy clean)



# Execution Log: Phase 0 Task 0.4 — Database Migrations & Core Schema

## Step 1: Write Initial Core Schema SQL Migration (Up & Down)
- **Files Changed:**
  - `migrations/0001_core_schema.up.sql`
  - `migrations/0001_core_schema.down.sql`
- **What Changed:**
  - Authored `0001_core_schema.up.sql` implementing all 15 core entities from `specs/design.md §6.1` plus the 5 business subsystem entities from `§6.2`.
  - Added `organization_id UUID NOT NULL` to all tenant-scoped tables (R5.3).
  - Enforced composite uniqueness on `(organization_id, mailbox_id, provider_message_id)` and `(organization_id, mailbox_id, provider_thread_id)` (R5.4).
  - Created GIN indices on `email_message.search_tsv` and `knowledge_chunk.content_tsv` (R5.6).
  - Created HNSW index on `embedding_record.embedding` with `vector_cosine_ops` (R5.7).
  - Authored `0001_core_schema.down.sql` providing clean, reversible drops in topological dependency order.
- **Verification Command:**
  - `docker exec -i rag-email-postgres psql -U postgres -d rag_email < migrations/0001_core_schema.up.sql && docker exec -i rag-email-postgres psql -U postgres -d rag_email < migrations/0001_core_schema.down.sql`
- **Result:** PASS (Clean up and down execution, verified 0 tables remain after down)

## Step 2: Implement Async Database Pool & Migration Engine
- **Files Changed:**
  - `packages/db/connection.py`
  - `packages/db/migrator.py`
  - `packages/db/cli.py`
  - `packages/db/__init__.py`
  - `packages/core/settings.py`
- **What Changed:**
  - Implemented asyncpg connection pool factory with automatic `pgvector.asyncpg.register_vector` codec setup.
  - Built migration engine tracking versions and SHA256 checksums in `schema_migrations`.
  - Implemented transactional `apply_migrations()`, `rollback_migrations()`, and status reporting.
  - Added CLI runner supporting `up`, `down`, `status`, and `verify-dim`.
- **Verification Command:**
  - `uv run python -m packages.db.cli up && uv run python -m packages.db.cli status`
- **Result:** PASS (Applied migration 0001 recorded in schema_migrations)

## Step 3: Implement Startup Vector Dimension Verifier
- **Files Changed:**
  - `packages/db/migrator.py`
- **What Changed:**
  - Implemented `verify_database_vector_dimension()` querying PostgreSQL system catalog `atttypmod` for `embedding_record.embedding`.
  - Enforced R5.10 fail-fast constraint ensuring configured dimension (1536) matches the table column vector width.
- **Verification Command:**
  - `uv run python -m packages.db.cli verify-dim`
- **Result:** PASS (Verified configured=1536 matches database=1536)

## Step 4: Update Makefile & Run Full Up/Down Cycle against Live Postgres
- **Files Changed:**
  - `Makefile`
- **What Changed:**
  - Updated `Makefile` wiring `migrate` to `$(UV) run python -m packages.db.cli up` and `migrate-down` to `$(UV) run python -m packages.db.cli down`.
  - Added `migrate-down` to `.PHONY`.
  - Executed full cycle (`make migrate` -> `make migrate-down` -> `make migrate`) against PostgreSQL container.
- **Verification Command:**
  - `make migrate && make migrate-down && make migrate`
- **Result:** PASS (Applied 0001 -> Rolled back 0001 -> Reapplied 0001 cleanly)

## Step 5: Implement Automated Unit & Integration Tests
- **Files Changed:**
  - `tests/unit/test_migration_tooling.py`
  - `tests/integration/test_database_schema.py`
  - `Makefile`
- **What Changed:**
  - Implemented unit tests for migration discovery, SQL parsing, SHA256 checksumming, and error handling for missing down scripts and invalid names.
  - Implemented comprehensive integration tests covering:
    - Active `vector` and `pg_trgm` extensions (R5.1).
    - Creation of all 15 core entities and 5 business subsystem entities (R5.2, R13.1).
    - Mandatory `organization_id` on every tenant table (R5.3).
    - Composite uniqueness on `(org, mailbox, provider_message_id)` and `(org, mailbox, provider_thread_id)` (R5.4).
    - Reversibility of migrations (`down` rolls back tables, `up` restores) (R5.5).
    - GIN indices on `tsvector` columns (R5.6).
    - HNSW index on `embedding_record.embedding` with `vector_cosine_ops` (R5.7).
    - Startup vector dimension assertion (1536 PASS, 768 FAIL) (R5.10).
  - Updated `Makefile` `test` target to run the complete test suite.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (23/23 tests passed, 0 lint/type issues across 32 source files)

# Execution Log: Phase 0 Task 0.5 — Object Storage Client

## Step 1: Add minio Dependency to pyproject.toml
- **Files Changed:**
  - `pyproject.toml`
  - `uv.lock`
- **What Changed:**
  - Added `minio>=7.2.0` to `project.dependencies`.
  - Locked and synchronized virtual environment via `uv lock && uv sync`.
- **Verification Command:**
  - `uv run python -c "import minio; print('MinIO SDK version:', minio.__version__)"`
- **Result:** PASS (MinIO SDK version 7.2.20 installed and verified)

## Step 2: Implement Key Conventions & Storage Protocol
- **Files Changed:**
  - `packages/core/storage.py`
  - `packages/core/__init__.py`
- **What Changed:**
  - Implemented `ObjectKeyBuilder` generating standardized, tenant-isolated paths for `raw_mime`, `html_body`, `attachment`, and `knowledge_doc`. Added filename sanitization preventing directory traversal.
  - Defined `StorageProtocol` protocol and domain exceptions (`StorageError`, `ObjectNotFoundError`, `BucketBootstrapError`).
  - Implemented `FakeObjectStorageClient` for hermetic in-memory unit testing.
- **Verification Command:**
  - `uv run python -c "from packages.core.storage import ObjectKeyBuilder, FakeObjectStorageClient; print(ObjectKeyBuilder.raw_mime('org1', 'mbx1', 'msg1'))"`
- **Result:** PASS (`raw/org1/mbx1/msg1.eml`)

## Step 3: Implement MinioObjectStorageClient Wrapper with Bootstrap
- **Files Changed:**
  - `packages/core/storage.py`
  - `packages/core/storage_cli.py`
- **What Changed:**
  - Implemented `MinioObjectStorageClient` with async non-blocking wrappers around MinIO SDK via `asyncio.to_thread`.
  - Implemented bucket bootstrapping creating `raw-mime`, `attachments`, and `knowledge-docs` idempotently.
  - Implemented `put_bytes`, `get_bytes`, `delete_object`, `object_exists`, `get_object_metadata`, and `get_presigned_url`.
  - Added CLI tool `packages/core/storage_cli.py`.
- **Verification Command:**
  - `uv run python -m packages.core.storage_cli bootstrap`
- **Result:** PASS (All 3 buckets created on live MinIO container, verified idempotent on second run)

## Step 4: Implement Unit Tests for Key Conventions & Fake Client
- **Files Changed:**
  - `tests/unit/test_object_storage.py`
- **What Changed:**
  - Added unit test suite covering `ObjectKeyBuilder` path formatting across all 4 key categories (`raw_mime`, `html_body`, `attachment`, `knowledge_doc`).
  - Verified path traversal protection against `../../` and directory escapement.
  - Verified `FakeObjectStorageClient` in-memory CRUD operations, metadata extraction, presigned URL simulation, and `ObjectNotFoundError` handling.
- **Verification Command:**
  - `uv run pytest tests/unit/test_object_storage.py -v`
- **Result:** PASS (5/5 tests passed in 0.55s)

## Step 5: Implement Integration Tests against Live MinIO & Sign-Off
- **Files Changed:**
  - `tests/integration/test_minio_storage.py`
  - `specs/tasks.md`
- **What Changed:**
  - Implemented live integration tests verifying:
    - Bucket bootstrap creates `raw-mime`, `attachments`, `knowledge-docs` idempotently.
    - End-to-end raw MIME upload, existence check, bytes verification, metadata check, and deletion.
    - Attachment upload, presigned URL generation, and direct HTTP download via `httpx.AsyncClient`.
    - Knowledge document upload and round-trip verification.
    - Strict `ObjectNotFoundError` error handling on missing keys.
  - Verified full test suite (33/33 tests passing) and code quality (0 issues across 36 source files).
  - Marked Task 0.5 complete (`[x]`) in `specs/tasks.md`.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (33/33 tests passed in 3.73s)

# Execution Log: Phase 0 Task 0.6 — Domain Layer: Entities & Processing State Machine

## Step 1: Implement Pure Domain Entities
- **Files Changed:**
  - `packages/domain/entities.py`
- **What Changed:**
  - Implemented value objects `EmailAddress` and `AttachmentRef`.
  - Implemented pure dataclasses: `NormalizedMessage`, `Classification`, `Candidate`, `ContextPackage`, `Job`, and `ProcessingEvent`.
  - Built `ContextPackage.get_ordered_sections()` enforcing the 7-stage fixed prompt assembly order (R14.8) for LLM prefix caching.
  - Restricted all imports strictly to stdlib and `packages/core`.
- **Verification Command:**
  - `uv run python -c "from packages.domain.entities import NormalizedMessage, ContextPackage"`
- **Result:** PASS

## Step 2: Implement Processing State Machine & Transition Table
- **Files Changed:**
  - `packages/domain/state_machine.py`
- **What Changed:**
  - Implemented `JobState` enum with all 12 active, failure, and replay states (R18.1, R18.2).
  - Defined authoritative `TRANSITIONS` table implementing `specs/design.md §8`.
  - Implemented `IllegalStateTransitionError` and `validate_transition()` (R18.3).
  - Implemented `transition_job()` updating state and generating matching `ProcessingEvent` (R18.4, R18.5).
- **Verification Command:**
  - `uv run python -c "from packages.domain.state_machine import JobState, TRANSITIONS; assert len(JobState) == 12"`
- **Result:** PASS

## Step 3: Update Domain Package Exports
- **Files Changed:**
  - `packages/domain/__init__.py`
  - `tests/unit/test_dependency_rules.py`
- **What Changed:**
  - Re-exported all entities, enums, exceptions, and transition helpers from `packages.domain`.
  - Updated AST architectural boundary check to recognize intra-domain package imports.
- **Verification Command:**
  - `uv run pytest tests/unit/test_dependency_rules.py -v`
- **Result:** PASS (3/3 tests passed in 0.11s)

## Step 4: Implement Unit Tests for Domain Entities & Context Assembly
- **Files Changed:**
  - `tests/unit/test_domain_entities.py`
- **What Changed:**
  - Implemented unit tests covering:
    - Value object string formatting and immutability (`EmailAddress`, `AttachmentRef`).
    - `NormalizedMessage`, `Classification`, and `Candidate` initialization and scoring.
    - `ContextPackage` strictly asserting the 7-stage fixed prompt assembly order (R14.8), citation formatting, and `[BUSINESS DATA]` prefixing.
    - `Job` and `ProcessingEvent` defaults and attributes.
- **Verification Command:**
  - `uv run pytest tests/unit/test_domain_entities.py -v`
- **Result:** PASS (7/7 tests passed in 0.07s)

## Step 5: Implement Exhaustive Unit Tests for State Machine & Sign-Off
- **Files Changed:**
  - `packages/domain/state_machine.py`
  - `tests/unit/test_state_machine.py`
  - `specs/tasks.md`
- **What Changed:**
  - Added exhaustive unit test suite verifying:
    - Presence of all 12 job states (R18.1, R18.2).
    - 100% of all 24 declared legal transitions in `TRANSITIONS` table.
    - Illegal transitions raising `IllegalStateTransitionError` (R18.3).
    - Terminal `COMPLETED` state invariance (zero outgoing transitions).
    - Operator replay path from `DEAD_LETTER -> RETRY_PENDING` (R18.7).
    - `transition_job()` updating state and generating `ProcessingEvent` (R18.4, R18.5).
  - Verified full test suite (62/62 tests passing) and lint/type checks across 40 source files.
  - Marked Task 0.6 complete (`[x]`) in `specs/tasks.md`.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (62/62 tests passed in 5.95s, 0 issues found)

# Execution Log: Phase 0 Task 0.7 — Broker Foundation

## Step 1: Update Broker Configuration & Settings
- **Files Changed:**
  - `packages/core/settings.py`
  - `.env.example`
  - `docs/configuration.md`
  - `tests/unit/test_settings.py`
- **What Changed:**
  - Expanded `BrokerSettings` with explicit exchange names (`mail.ingest`, `email.process`, `email.triage`, `email.route`, `email.dispatch`, `knowledge.ingest`, `retry.email`, `dlx.email`) per `design.md §7.1`.
  - Added queue name settings (`mail.sync.requested`, `email.normalize`, `email.triage`, `email.dispatch`, `knowledge.ingest`, `email.dead_letter`) per R3.8.
  - Added `use_quorum_queues: bool = False` configuration field per R3.9.
  - Documented new broker configuration parameters in `.env.example` and `docs/configuration.md`.
  - Added assertions to `tests/unit/test_settings.py`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_settings.py -v`
- **Result:** PASS (9/9 tests passed in 0.63s)

## Step 2: Implement Topology Declaration
- **Files Changed:**
  - `packages/broker/topology.py`
- **What Changed:**
  - Created `packages/broker/topology.py` implementing `setup_topology()` and `BrokerTopology`.
  - Declared 8 durable primary exchanges (`mail.ingest`, `email.process`, `email.triage`, `email.dispatch`, `knowledge.ingest`, `retry.email`, `email.route`, `dlx.email`) and 3 companion fanout retry exchanges (`retry.email.30s`, `retry.email.5m`, `retry.email.30m`).
  - Declared 6 durable stage queues and 3 retry queues (`email.retry.30s`, `email.retry.5m`, `email.retry.30m`) configured with `x-message-ttl` (30s, 300s, 1800s) and `x-dead-letter-exchange` routing back to `email.route`.
  - Configured `email.dead_letter` queue bound to `dlx.email` with `#` wildcard for terminal dead-letter inspection.
  - Verified idempotent re-declaration against live RabbitMQ container.
- **Verification Command:**
  - `uv run ruff check packages/broker/topology.py && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS

## Step 3: Implement Job Envelope & Persistent Publisher
- **Files Changed:**
  - `packages/broker/envelope.py`
  - `packages/broker/publisher.py`
- **What Changed:**
  - Created `JobEnvelope` Pydantic model (`job_id`, `idempotency_key`, `job_type`, `organization_id`, `message_id`, `thread_id`, `trace_id`, `attempt`, `classification`, `enqueued_at`) strictly implementing `design.md §7.3`.
  - Implemented `to_message()` and `from_message()` enforcing `delivery_mode=PERSISTENT`, JSON payload, and standard headers.
  - Implemented `MessagePublisher` supporting persistent exchange publishing, retry queue publishing via companion fanout exchanges (preserving origin routing keys for TTL-based DLX redelivery), and terminal dead-lettering to `dlx.email` with failure headers (`x-failure-reason`, `x-original-routing-key`, `x-original-exchange`, `x-attempt`).
- **Verification Command:**
  - `uv run ruff check packages/broker/envelope.py packages/broker/publisher.py && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS

## Step 4: Implement Base Consumer with Manual Ack & Prefetch QoS
- **Files Changed:**
  - `packages/broker/consumer.py`
  - `packages/broker/__init__.py`
- **What Changed:**
  - Created `BaseConsumer` base class setting bounded QoS `prefetch_count` (R3.4) and consuming with `no_ack=False` (manual acknowledgement, R3.3).
  - Ensured messages are manually ACKed only after `process_job()` completes and side-effects commit.
  - Implemented automatic retry routing for transient failures (`TransientError` / unexpected exceptions): calculates next delay tier (`30s`, `5m`, `30m`), republishes via retry fanout exchange, and ACKs original message.
  - Implemented terminal dead-lettering for unrecoverable failures (`FatalError` or `attempt >= max_retries`): routes to `dlx.email` with failure headers and ACKs original message to prevent poison-message blocking (R3.5, R19.6).
  - Re-exported all public components from `packages.broker`.
- **Verification Command:**
  - `uv run ruff check packages/broker && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS

## Step 5: Unit Tests, Integration Tests & Task Sign-Off
- **Files Changed:**
  - `tests/unit/test_broker.py`
  - `tests/integration/test_broker_rabbitmq.py`
  - `specs/tasks.md`
- **What Changed:**
  - Implemented unit tests covering `JobEnvelope` defaults, JSON serialization/deserialization, AMQP persistent message formatting, correlation/trace context preservation, and retry delay calculation.
  - Implemented live RabbitMQ integration tests:
    - Idempotent topology declaration verifying all 11 exchanges and 9 queues.
    - Persistent publishing and manual ACK consumer processing.
    - Transient failure retry ladder routing to `email.retry.30s` with attempt increment and failure headers.
    - Terminal failure routing to `dlx.email` / `email.dead_letter` on `FatalError`.
    - Retry ladder queue TTL expiration (500ms) with automated dead-letter redelivery to worker queue preserving original topic routing key.
  - Verified full test and lint suite (72/72 tests passing, 0 ruff errors, 0 mypy errors across 46 files).
  - Marked Task 0.7 complete (`[x]`) in `specs/tasks.md`.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (72/72 tests passed in 8.55s, 0 issues found)

# Execution Log: Phase 0 Task 0.8 — Idempotency Utility

## Step 1: Implement Pure Idempotency Core & Key Derivation
- **Files Changed:**
  - `packages/core/idempotency.py`
- **What Changed:**
  - Implemented `derive_idempotency_key(organization_id, mailbox_id, provider_message_id, operation_type)` computing deterministic 64-character SHA-256 digest per R19.2 and `design.md §9`.
  - Defined `IdempotencyRecord`, `IdempotencyConflictError`, and `IdempotencyBackend` abstract protocol.
  - Implemented `InMemoryIdempotencyBackend` supporting async-safe lock and duplicate key conflict simulation.
  - Implemented `execute_once[T]()` orchestrating the two-layer algorithm (fast-path app short-circuit, execution, completion persistence, and polling on concurrent race conflict).
- **Verification Command:**
  - `uv run ruff check packages/core/idempotency.py && uv run python -c "from packages.core.idempotency import derive_idempotency_key; assert len(derive_idempotency_key('org', 'box', 'msg', 'norm')) == 64"`
- **Result:** PASS

## Step 2: Implement Postgres Idempotency Backend
- **Files Changed:**
  - `packages/db/idempotency.py`
- **What Changed:**
  - Implemented `PostgresIdempotencyBackend` backed by `asyncpg.Pool` connection pool.
  - Implemented `find_completed(key)` querying `processing_job` table for `idempotency_key = $1 AND state = 'COMPLETED'` and deserializing JSONB `result_ref`.
  - Implemented `mark_completed(key, result, metadata)` inserting completed job state into `processing_job` and catching `asyncpg.UniqueViolationError` to raise `IdempotencyConflictError` for concurrent race handling (R19.4).
- **Verification Command:**
  - `uv run ruff check packages/db/idempotency.py && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS

## Step 3: Update Package Exports
- **Files Changed:**
  - `packages/core/__init__.py`
  - `packages/db/__init__.py`
- **What Changed:**
  - Re-exported `derive_idempotency_key`, `execute_once`, `IdempotencyBackend`, `IdempotencyRecord`, `InMemoryIdempotencyBackend`, and `IdempotencyConflictError` from `packages.core`.
  - Re-exported `PostgresIdempotencyBackend` from `packages.db`.
- **Verification Command:**
  - `uv run ruff check packages/core packages/db && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS

## Step 4: Implement Unit Test Suite
- **Files Changed:**
  - `tests/unit/test_idempotency.py`
- **What Changed:**
  - Tested `derive_idempotency_key` determinism across strings and UUIDs, and sensitivity to any input difference (R19.2).
  - Tested repeat execution fast-path short-circuiting returning cached results without re-running operations (R19.3).
  - Tested concurrent execution races across 10 overlapping coroutines: verified exactly one winner is persisted, runner-ups resolve via polling, and all coroutines receive identical results with zero duplicate errors (R19.4).
  - Tested partial/transient failure handling: verified exceptions prevent completion recording and permit subsequent retry attempts.
  - Tested in-memory duplicate key rejection.
- **Verification Command:**
  - `uv run ruff check tests/unit/test_idempotency.py && uv run pytest tests/unit/test_idempotency.py -v`
- **Result:** PASS (5/5 tests passed in 0.60s)

## Step 5: Implement PostgreSQL Integration Test Suite
- **Files Changed:**
  - `tests/integration/test_idempotency_postgres.py`
- **What Changed:**
  - Tested `PostgresIdempotencyBackend` against live PostgreSQL instance (`rag-email-postgres` on port 5433).
  - Verified `mark_completed` and `find_completed` persistence with JSONB deserialization.
  - Verified `execute_once()` short-circuiting against database.
  - Verified concurrent race condition against real DB-level `UNIQUE` constraint across 5 simultaneous workers: exactly 1 worker executed operation and inserted the row; 4 caught `UniqueViolationError` / `IdempotencyConflictError` and resolved the winner's result via polling.
  - Verified transient operation failure leaves no record and permits subsequent execution.
- **Verification Command:**
  - `uv run pytest tests/integration/test_idempotency_postgres.py -v`
- **Result:** PASS (4/4 passed in 0.53s)

## Step 6: Full Verification & Task 0.8 Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
- **What Changed:**
  - Verified full test suite (81 tests across all unit and integration suites passing).
  - Verified code formatting with `ruff check` and strict static typing with `mypy` (0 errors across 50 files).
  - Marked Task 0.8 as completed in `specs/tasks.md`.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (81 passed in 9.77s)

# Execution Log: Phase 0 Task 0.9 — Observability Skeleton

## Step 1: Configuration & Settings Extension
- **Files Changed:**
  - `packages/core/settings.py`
  - `packages/core/__init__.py`
  - `.env.example`
  - `docs/configuration.md`
- **What Changed:**
  - Defined `ObservabilitySettings` Pydantic model (`log_level`, `log_format`, `otlp_endpoint`, `prometheus_enabled`, `metrics_port`, `health_port`, `drain_timeout_s`).
  - Added `telemetry: ObservabilitySettings` field to `AppSettings`.
  - Re-exported `ObservabilitySettings` from `packages.core`.
  - Documented new configuration keys in `.env.example` and section 2.12 of `docs/configuration.md`.
- **Verification Command:**
  - `uv run python -c "from packages.core.settings import AppSettings; s = AppSettings(); assert s.telemetry.metrics_port == 9090; assert s.telemetry.log_format == 'json'" && uv run ruff check packages/core && uv run mypy packages/core`
- **Result:** PASS

## Step 2: Structured JSON Logging & Correlation Context
- **Files Changed:**
  - `packages/observability/context.py`
  - `packages/observability/logging.py`
- **What Changed:**
  - Implemented async-safe `contextvars` correlation context tracking `trace_id, message_id, thread_id, job_id, organization_id` (R21.3).
  - Implemented `bind_log_context()` context manager for scoped contextual execution.
  - Implemented `StructuredJSONFormatter` emitting single-line JSON with ISO-8601 UTC timestamps, caller locations, error stack traces, and the 5 mandatory correlation fields.
  - Implemented `setup_logging()` configuring the root logger with `CorrelationFilter` and quiet defaults for noisy third-party loggers.
- **Verification Command:**
  - `uv run python -c "import logging, json; from packages.observability.logging import setup_logging, StructuredJSONFormatter; from packages.observability.context import bind_log_context; setup_logging(level='INFO', json_format=True); f = StructuredJSONFormatter(); rec = logging.LogRecord('test', logging.INFO, 'path.py', 10, 'Hello %s', ('World',), None); out = f.format(rec); d = json.loads(out); assert d['message'] == 'Hello World'; assert 'trace_id' in d; assert 'job_id' in d; assert 'timestamp' in d" && uv run ruff check packages/observability && uv run mypy packages/observability`
- **Result:** PASS

## Step 3: OpenTelemetry Tracing & AMQP Context Propagation
- **Files Changed:**
  - `packages/observability/tracing.py`
- **What Changed:**
  - Implemented OpenTelemetry initialization (`init_tracer`, `get_tracer`) supporting service naming and OTLP exporters.
  - Implemented W3C carrier injection and extraction (`inject_trace_context`, `extract_trace_context`) attaching `traceparent`, `trace_id`, and `span_id` across AMQP hops (R21.1, R21.2).
  - Implemented `trace_span()` context manager and attribute recording conforming to the stage hierarchy in `design.md §10`.
- **Verification Command:**
  - `uv run python -c "from packages.observability.tracing import init_tracer, trace_span, inject_trace_context, extract_trace_context, get_current_trace_id; tracer = init_tracer('test-svc'); headers = {}; ... print('Step 3 verified successfully!')" && uv run ruff check packages/observability && uv run mypy packages/observability`
- **Result:** PASS

## Step 4: Prometheus Metrics Registry & Cost Tracking
- **Files Changed:**
  - `packages/observability/metrics.py`
- **What Changed:**
  - Implemented `create_pipeline_metrics()` declaring all R21.4 counters, latency histograms with p50/p95/p99 buckets, and gauges.
  - Implemented monotonic cumulative cost accounting via `record_ai_cost()` per R21.6 and design.md §10.
  - Implemented `generate_metrics_payload()` formatting metrics for the `/metrics` endpoint using standard Prometheus exposition.
- **Verification Command:**
  - `uv run python -c "from packages.observability.metrics import create_pipeline_metrics, record_ai_cost, generate_metrics_payload; from packages.core.settings import ModelPricing; ... print(f'Step 4 verified! Cost: {cost:.6f}')" && uv run ruff check packages/observability && uv run mypy packages/observability`
- **Result:** PASS

## Step 5: Health & Readiness Framework
- **Files Changed:**
  - `packages/observability/health.py`
- **What Changed:**
  - Implemented `HealthRegistry` supporting dynamic registration of asynchronous dependency checks.
  - Implemented `/healthz` (liveness returning 200 OK) and `/readyz` (returning 200 OK if all dependencies are ready or 503 if failing / draining).
  - Implemented drain mode transition for graceful shutdowns (R20.7, R20.8).
  - Implemented `create_health_router()` generating a reusable FastAPI/Starlette router exposing `/healthz`, `/readyz`, and `/metrics`.
- **Verification Command:**
  - `uv run python -c "import asyncio; from packages.observability.health import HealthRegistry, create_health_router; ... print('Step 5 verified!')" && uv run ruff check packages/observability && uv run mypy packages/observability`
- **Result:** PASS

## Step 6: Graceful Shutdown Coordinator & Standalone Server
- **Files Changed:**
  - `packages/observability/shutdown.py`
  - `packages/observability/server.py`
- **What Changed:**
  - Implemented `GracefulShutdownCoordinator` managing OS signals (SIGTERM/SIGINT), drain callbacks, and in-flight job accounting via `track_job()` with a bounded `drain_timeout_s` (R20.8).
  - Implemented `ObservabilityServer` and `start_observability_server()` running a lightweight background HTTP server hosting `/healthz`, `/readyz`, and `/metrics` for headless worker processes (R20.7, R21.4).
- **Verification Command:**
  - `uv run python -c "import asyncio, httpx; from packages.observability.shutdown import GracefulShutdownCoordinator; ... print('Step 6 verified successfully!')" && uv run ruff check packages/observability && uv run mypy packages/observability`
- **Result:** PASS

## Step 7: Broker Consumer Tracing & Context Propagation Integration
- **Files Changed:**
  - `packages/broker/envelope.py`
  - `packages/broker/consumer.py`
- **What Changed:**
  - Updated `JobEnvelope.to_message()` to automatically inject W3C trace context headers into outgoing AMQP messages (R21.1).
  - Updated `BaseConsumer._handle_message()` to extract trace context from AMQP headers and start child span `broker.consume` (R21.2).
  - Wired `bind_log_context()` with `trace_id, message_id, thread_id, job_id, organization_id` around consumer processing (R21.3).
  - Wired `GracefulShutdownCoordinator.track_job()` to manage in-flight consumer draining (R20.8).
  - Recorded retry and failure counts to Prometheus metrics (`retry_jobs_total`, `failed_jobs_total`) (R21.4).
- **Verification Command:**
  - `uv run ruff check packages/broker packages/observability && uv run mypy packages/broker packages/observability && uv run pytest tests/unit/test_broker.py -v`
- **Result:** PASS (5/5 unit tests passed in 0.96s)

## Step 8: Package Re-Exports & Wiring
- **Files Changed:**
  - `packages/observability/__init__.py`
- **What Changed:**
  - Cleanly re-exported public observability symbols (`setup_logging`, `bind_log_context`, `get_correlation_context`, `init_tracer`, `get_tracer`, `trace_span`, `inject_trace_context`, `extract_trace_context`, `get_metrics`, `create_pipeline_metrics`, `record_ai_cost`, `generate_metrics_payload`, `HealthRegistry`, `create_health_router`, `GracefulShutdownCoordinator`, `ObservabilityServer`, `start_observability_server`).
  - Verified architectural boundaries via AST dependency rules.
- **Verification Command:**
  - `uv run ruff check packages/observability && uv run mypy packages/observability && uv run pytest tests/unit/test_dependency_rules.py -v`
- **Result:** PASS (3/3 boundary tests passed)

## Step 9: Unit Test Suite
- **Files Changed:**
  - `tests/unit/test_observability_logging.py`
  - `tests/unit/test_observability_tracing.py`
  - `tests/unit/test_observability_metrics.py`
  - `tests/unit/test_observability_health_shutdown.py`
- **What Changed:**
  - Tested structured JSON logging, ISO-8601 timestamps, stacktrace extraction, and `bind_log_context()` scope isolation for all 5 mandatory fields (`trace_id, message_id, thread_id, job_id, organization_id`) (R21.3).
  - Tested OpenTelemetry tracer setup, span generation, status codes, and AMQP W3C carrier injection/extraction preserving trace ID across queue hops (R21.1, R21.2).
  - Tested Prometheus metrics instantiation across all R21.4 counters, latency histograms with p50/p95/p99 buckets, gauges, monotonic AI cost accumulation with price table (R21.6), and Prometheus exposition generation.
  - Tested `/healthz` (200), `/readyz` (200 ready, 503 unready, 503 draining), in-flight job tracking via `track_job()`, and graceful shutdown coordinator execution sequence (R20.7, R20.8).
- **Verification Command:**
  - `uv run ruff check tests/unit/test_observability_*.py && uv run mypy tests/unit/test_observability_*.py && uv run pytest tests/unit/test_observability_*.py -v`
- **Result:** PASS (16/16 unit tests passed in 1.12s)

## Step 10: Live Integration Test Suite
- **Files Changed:**
  - `tests/integration/test_observability_e2e.py`
- **What Changed:**
  - Implemented end-to-end integration test connecting live PostgreSQL (port 5433) and RabbitMQ (port 5672) containers.
  - Verified `/healthz` and `/readyz` endpoints over HTTP return 200 OK with real database query and broker connection checks.
  - Verified OpenTelemetry W3C trace context injected at publish is extracted by consumer across AMQP hop, preserving identical distributed trace IDs (R21.1, R21.2).
  - Verified async correlation context captures `message_id, thread_id, job_id, organization_id` inside worker execution (R21.3).
  - Verified Prometheus `/metrics` exposition over HTTP scrapes pipeline counters (R21.4).
  - Verified graceful shutdown signal coordinates immediate transition of `/readyz` to 503 draining without message loss (R20.8).
- **Verification Command:**
  - `uv run ruff check tests/integration/test_observability_e2e.py && uv run mypy tests/integration/test_observability_e2e.py && uv run pytest tests/integration/test_observability_e2e.py -v`
- **Result:** PASS (1 passed in 1.79s)

## Step 11: Verification, Lint, Architectural Guard & Task 0.9 Sign-off
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
- **What Changed:**
  - Verified full test suite (98 tests across all unit and integration suites passing in 10.88s).
  - Verified code formatting with `ruff check .` (0 warnings/errors) and strict static typing with `mypy` (0 errors across 62 files).
  - Verified architectural boundary rules (`test_dependency_rules.py`).
  - Marked Task 0.9 as completed in `specs/tasks.md`.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (98 passed in 10.88s)

# Execution Log: Phase 0 Task 0.10 — API Skeleton

## Step 1: Core Pagination Abstractions & API Settings
- **Files Changed:**
  - `packages/core/pagination.py`
  - `packages/core/settings.py`
  - `packages/core/__init__.py`
  - `.env.example`
  - `docs/configuration.md`
- **What Changed:**
  - Implemented `PageParams` (`limit: int = 50` [1..100], `offset: int = 0` [ge=0]) and generic `PaginatedResponse[T]` per R23.6.
  - Implemented `paginate(items, total_count, params) -> PaginatedResponse[T]`.
  - Extended `APISettings` with `cors_origins`, `title`, `version`, and `description`.
  - Documented new configuration keys in `.env.example` and `docs/configuration.md`.
- **Verification Command:**
  - `uv run python -c "from packages.core.pagination import PageParams, PaginatedResponse, paginate; from packages.core.settings import APISettings; p = paginate(['a', 'b'], 10, PageParams(limit=2, offset=0)); assert p.has_more is True; assert p.total_count == 10" && uv run ruff check packages/core && uv run mypy packages/core`
- **Result:** PASS

## Step 2: Tenant Scoping Dependencies & Error Schemas
- **Files Changed:**
  - `services/api/dependencies.py`
  - `services/api/errors.py`
- **What Changed:**
  - Implemented `get_organization_id()` extracting and validating UUID from `X-Organization-ID` header or `organization_id` query param (R23.6, R5.3).
  - Wired `bind_log_context(organization_id=str(org_id))` inside dependency so all logs carry tenant correlation context (R21.3).
  - Implemented `TenantContext` and `get_tenant_context()` dependency.
  - Implemented `APIErrorResponse` schema and custom exception handlers for `StarletteHTTPException`, `HTTPException`, `RequestValidationError`, and unhandled 500 exceptions.
- **Verification Command:**
  - `uv run ruff check services/api && uv run mypy services/api`
- **Result:** PASS

## Step 3: API Pagination Helpers & Versioned /v1 Router
- **Files Changed:**
  - `services/api/pagination.py`
  - `services/api/routers/__init__.py`
  - `services/api/routers/v1.py`
- **What Changed:**
  - Implemented `get_pagination_params()` as a FastAPI dependency validating `limit` (1..100) and `offset` ($\ge 0$).
  - Built `v1_router = APIRouter(prefix="/v1", dependencies=[Depends(get_organization_id)])` enforcing mandatory tenant scoping across all child endpoints.
  - Added `/v1/ping` and `/v1/sample-items` endpoints demonstrating tenant validation and paginated response envelope.
- **Verification Command:**
  - `uv run ruff check services/api && uv run mypy services/api`
- **Result:** PASS

## Step 4: FastAPI Application Factory & OpenAPI Export
- **Files Changed:**
  - `services/api/main.py`
  - `services/api/openapi.py`
  - `services/api/__init__.py`
- **What Changed:**
  - Implemented `create_app()` mounting root health/readiness router (`/healthz`, `/readyz`, `/metrics`), `/v1` router, CORS middleware, and custom exception handlers.
  - Implemented `TraceContextMiddleware` extracting W3C `traceparent` headers and binding trace context.
  - Implemented application lifespan connecting asyncpg database pool and registering database readiness check with `HealthRegistry`.
  - Implemented `services/api/openapi.py` CLI generating and validating OpenAPI 3.1 schema.
- **Verification Command:**
  - `uv run python -m services.api.openapi --check && uv run ruff check services/api && uv run mypy services/api`
- **Result:** PASS (OpenAPI schema valid v3.1.0, 5 paths)

## Step 5: Unit Test Suite
- **Files Changed:**
  - `tests/unit/test_api_skeleton.py`
- **What Changed:**
  - Tested OpenAPI 3.1 schema generation and documentation endpoints (`/docs`, `/redoc`, `/openapi.json`) (R23.1).
  - Tested un-scoped root health endpoints (`/healthz`, `/readyz`, `/metrics`) (R20.7, R21.4).
  - Tested mandatory `organization_id` scoping: missing header rejected with 400 `ORGANIZATION_ID_REQUIRED`, invalid UUID rejected with 400 `INVALID_ORGANIZATION_ID`, valid header/query succeeds (R23.6, R5.3).
  - Tested pagination helper: defaults, boundary limits, invalid params rejected with 422, `has_more` calculation (R23.6).
  - Tested W3C `traceparent` propagation and structured error formats on 404/422.
- **Verification Command:**
  - `uv run pytest tests/unit/test_api_skeleton.py -v`
- **Result:** PASS (16/16 passed in 2.08s)

## Step 6: Live Integration Test, Full Verification & Task 0.10 Sign-Off
- **Files Changed:**
  - `tests/integration/test_api_integration.py`
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
- **What Changed:**
  - Implemented live integration tests against live PostgreSQL container (port 5433).
  - Verified full application lifespan with real database connection pool and `/readyz` health reporting.
  - Verified full test suite (117 tests across all unit and integration suites passing in 12.49s).
  - Verified code formatting (`ruff check`) and strict static typing (`mypy`) with 0 errors across 72 source files.
  - Marked Task 0.10 as completed in `specs/tasks.md`.
- **Verification Command:**
  - `make lint && make test`
- **Result:** PASS (117 passed in 12.49s)

# Execution Log: Phase 0 Task 0.11 — CI Pipeline & Test Harness

## Step 1: External Stubbing Harness (R24.5)
- **Files Changed:**
  - `tests/stubs/mail_provider.py`
  - `tests/stubs/llm.py`
  - `tests/stubs/embedding.py`
  - `tests/stubs/__init__.py`
  - `tests/conftest.py`
- **What Changed:**
  - Implemented `FakeMailProviderAdapter` supporting in-memory message storage, checkpoint syncing, raw MIME retrieval, message dispatch, and draft creation with zero network requests.
  - Implemented `StubLLMProvider` delivering deterministic raw and structured JSON generations, token count simulation, and failure injection without API keys.
  - Implemented `StubEmbedder` generating deterministic, L2-normalized 1536-dimensional unit vectors with cosine similarity calculation.
  - Configured global pytest fixtures and autouse credential guard fixture `guard_live_credentials` in `tests/conftest.py`.
- **Verification Command:**
  - `uv run ruff check tests/stubs tests/conftest.py && uv run mypy tests/stubs tests/conftest.py`
- **Result:** PASS

## Step 2: Credential Guard & Stub Unit Tests (R24.5)
- **Files Changed:**
  - `tests/unit/test_ci_credential_guard.py`
- **What Changed:**
  - Added unit test suite covering:
    - Fake mail provider connection lifecycle, message addition, sync streaming, MIME extraction, and draft recording.
    - Stub LLM deterministic generation, canned responses, and failure injection.
    - Stub embedder 1536-dimension assertion, exact determinism, unit normalization, and cosine similarity.
    - Credential isolation asserting no live API keys exist or leak into test environments.
- **Verification Command:**
  - `uv run pytest tests/unit/test_ci_credential_guard.py -v`
- **Result:** PASS (10/10 passed in 0.09s)

## Step 3: Local CI Makefile Targets & Format Checks (R24.2)
- **Files Changed:**
  - `Makefile`
- **What Changed:**
  - Added `fmt-check` (`ruff format --check .`).
  - Added `test-unit` (`pytest tests/unit -v`).
  - Added `test-integration` (`pytest tests/integration -v`).
  - Added composite `ci` target running `fmt-check lint test-unit test-integration`.
- **Verification Command:**
  - `make fmt-check`
- **Result:** PASS (90 files verified formatted)

## Step 4: GitHub Actions CI Workflow Definition (R24.2, R24.4, R24.5)
- **Files Changed:**
  - `.github/workflows/ci.yml`
- **What Changed:**
  - Defined `ci.yml` GitHub Actions workflow triggered on push/PR to master and main with concurrency cancellation.
  - Defined `quality` job running `ruff format --check`, `ruff check`, `mypy strict`, and OpenAPI schema validation.
  - Defined `unit-tests` job running `pytest tests/unit -v` with coverage and explicit credential nullification.
  - Defined `integration-tests` job spinning up ephemeral `pgvector/pgvector:pg16`, `rabbitmq:3.13-management`, and `quay.io/minio/minio:latest` service containers with healthchecks, running database migrations, MinIO bucket bootstrap, and `pytest tests/integration -v`.
- **Verification Command:**
  - `uv run python -c "import yaml; from pathlib import Path; doc = yaml.safe_load(Path('.github/workflows/ci.yml').read_text()); assert 'quality' in doc['jobs']; assert 'unit-tests' in doc['jobs']; assert 'integration-tests' in doc['jobs']"`
- **Result:** PASS

## Step 5: Full Verification & Task 0.11 Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
- **What Changed:**
  - Executed full local CI suite via `make ci`.
  - Verified 102 unit tests and 25 integration tests (127 total tests passed in 14.03s).
  - Verified formatting (90 files), linting (0 errors), and strict typing (0 errors across 78 files).
  - Marked Task 0.11 complete in `specs/tasks.md`.
- **Verification Command:**
  - `make ci`
- **Result:** PASS (127 passed in 14.03s)









# Execution Log: Phase 1 Task 1.1 — Provider Adapter Interface & Registry

## Step 1: Add Adapter Domain Entities
- **Files Changed:**
  - `packages/domain/entities.py`
  - `packages/domain/__init__.py`
  - `tests/unit/test_domain_entities.py`
- **What Changed:**
  - Added pure dataclass entities: `Mailbox`, `Checkpoint`, `Subscription`, `RawMessage`, `RawThread`, `OutboundReply`, `DraftRef`, `SentRef`, `ThreadRef`, and `SyncResult`.
  - Re-exported new domain entities in `packages.domain.__all__`.
  - Added unit tests covering initialization, immutability, and defaults for all new entities.
- **Verification Command:**
  - `uv run pytest tests/unit/test_domain_entities.py tests/unit/test_dependency_rules.py`
- **Result:** PASS (13 passed in 0.37s)

## Step 2: Implement Common Error Taxonomy
- **Files Changed:**
  - `packages/adapters/exceptions.py`
  - `tests/unit/test_adapter_exceptions.py`
- **What Changed:**
  - Implemented common error taxonomy: `ProviderError`, `RateLimited` (with `retry_after: float | None`), `AuthExpired`, `NotFound`, `Transient`, and `Permanent`.
  - Added structured metadata storage for `provider`, `mailbox_id`, and `raw_error`.
  - Added unit tests verifying inheritance hierarchy, `retry_after` parameter handling, and error distinction.
- **Verification Command:**
  - `uv run pytest tests/unit/test_adapter_exceptions.py`
- **Result:** PASS (5 passed in 0.04s)

## Step 3: Define MailProviderAdapter Protocol & Registry
- **Files Changed:**
  - `packages/adapters/protocol.py`
  - `packages/adapters/registry.py`
  - `packages/adapters/__init__.py`
  - `tests/unit/test_adapter_registry.py`
- **What Changed:**
  - Defined `@runtime_checkable` `MailProviderAdapter(Protocol)` exposing all 7 async methods: `subscribe`, `renew_subscription`, `synchronize`, `get_message`, `get_thread`, `create_draft`, and `send_reply` (R1.1, R1.4).
  - Implemented provider registry keyed by `mailbox.provider` with lookup, registration, case-insensitive keying, and error handling (R1.3).
  - Re-exported protocol, errors, and registry utilities in `packages.adapters`.
  - Added unit tests validating runtime protocol conformance and registry resolution.
- **Verification Command:**
  - `uv run pytest tests/unit/test_adapter_registry.py`
- **Result:** PASS (5 passed in 0.06s)

## Step 4: Implement Reusable Contract Test Suite
- **Files Changed:**
  - `packages/adapters/testing.py`
  - `tests/unit/test_mail_adapter_contract.py`
  - `packages/adapters/__init__.py`
- **What Changed:**
  - Created `MailProviderAdapterContractSuite` providing automated contract checks for all 7 protocol methods (`subscribe`, `renew_subscription`, `synchronize`, `get_message`, `get_thread`, `create_draft`, and `send_reply`).
  - Verified contract suite with `ConformingMockAdapter` and tested negative protocol checks for incomplete classes.
  - Verified error taxonomy base catching for all 5 error types.
- **Verification Command:**
  - `uv run pytest tests/unit/test_mail_adapter_contract.py`
- **Result:** PASS (10 passed in 0.11s)

## Step 5: Verify Architecture Confinement & Full Test Suite
- **Files Changed:**
  - `tests/unit/test_dependency_rules.py`
- **What Changed:**
  - Added `test_services_never_reference_provider_literals` enforcing that `services/*` never references provider string literals directly (`gmail`, `graph`, `imap`), preserving R1.3 provider isolation.
  - Ran static analysis (`ruff`) and static type checks (`mypy`) on `packages/adapters/` and `packages/domain/`.
  - Executed full unit test suite (138 passed).
- **Verification Command:**
  - `uv run pytest tests/unit/test_dependency_rules.py && uv run ruff check packages/adapters packages/domain && uv run mypy packages/adapters packages/domain && uv run pytest tests/unit`
- **Result:** PASS (All checks and 138 tests passed)

# Execution Log: Phase 1 Task 1.2 — FakeProviderAdapter

## Step 1: Implement FakeProviderAdapter Core & Protocol Conformance
- **Files Changed:**
  - `packages/adapters/fake.py`
  - `packages/adapters/__init__.py`
  - `tests/unit/test_fake_adapter.py`
- **What Changed:**
  - Implemented `FakeProviderAdapter` class conforming to `MailProviderAdapter` protocol with in-memory stores for messages, threads, subscriptions, drafts, and outbound deliveries.
  - Auto-registered `FakeProviderAdapter` under provider key `"fake"`.
  - Added `tests/unit/test_fake_adapter.py` inheriting `MailProviderAdapterContractSuite` and verified all 8 protocol contract tests pass.
- **Verification Command:**
  - `uv run pytest tests/unit/test_fake_adapter.py`
- **Result:** PASS (9 passed in 0.08s)

## Step 2: Add Fixture Loading, Sync Windows & Pagination
- **Files Changed:**
  - `packages/adapters/fake.py`
  - `tests/fixtures/mail/sample_messages.json`
  - `tests/unit/test_fake_adapter.py`
- **What Changed:**
  - Added sample JSON mail fixtures in `tests/fixtures/mail/sample_messages.json`.
  - Implemented `load_fixtures_from_json` and `load_fixtures_from_dict` supporting RFC822 MIME generation, metadata parsing, and thread association.
  - Implemented sync windows with configurable `batch_size`, monotonic `history_id` sequences, and `has_more` multi-page pagination.
- **Verification Command:**
  - `uv run pytest tests/unit/test_fake_adapter.py`
- **Result:** PASS (11 passed in 0.11s)

## Step 3: Implement Expired Checkpoints & Failure Injection
- **Files Changed:**
  - `packages/adapters/fake.py`
  - `tests/unit/test_fake_adapter.py`
- **What Changed:**
  - Implemented expired checkpoint detection (`expire_checkpoint`) returning `requires_full_resync=True` to trigger bounded full resync flow.
  - Implemented programmable fault injection (`inject_rate_limit`, `inject_auth_expired`, `inject_transient_failure`, `inject_permanent_failure`, `inject_not_found`, and `clear_injected_faults`).
  - Added decremental call counters allowing fault simulation followed by automated recovery.
  - Verified all error taxonomy cases and recovery transitions in unit tests.
- **Verification Command:**
  - `uv run pytest tests/unit/test_fake_adapter.py`
- **Result:** PASS (15 passed in 0.12s)

## Step 4: Ensure Stub Compatibility, Linting & Regression Suite
- **Files Changed:**
  - `tests/stubs/__init__.py`
  - `packages/adapters/registry.py`
  - `packages/adapters/__init__.py`
  - `tests/unit/test_adapter_registry.py`
- **What Changed:**
  - Re-exported `FakeProviderAdapter` in `tests/stubs` and preserved full compatibility with `FakeMailProviderAdapter`.
  - Added `register_default_adapters` to `packages/adapters/registry.py` to ensure default built-ins remain available across all test modules.
  - Verified static linting (`ruff`), strict type checking (`mypy`), and ran full unit test suite (153 passed with 0 regressions).
- **Verification Command:**
  - `uv run ruff check packages/adapters && uv run mypy packages/adapters && uv run pytest tests/unit`
- **Result:** PASS (153 passed in 2.46s)

# Execution Log: Phase 1 Task 1.3 — Gmail Adapter

## Step 1: Implement MIME Serialization & Gmail HTTP Client Core
- **Files Changed:**
  - `packages/adapters/gmail.py`
  - `tests/unit/test_gmail_adapter.py`
- **What Changed:**
  - Implemented URL-safe base64 encoding and decoding helpers (`encode_urlsafe_b64`, `decode_urlsafe_b64`).
  - Implemented RFC822 MIME builder (`build_rfc822_mime`) converting `OutboundReply` to valid MIME bytes with HTML alternative support.
  - Implemented async HTTP request dispatcher `_request()` translating HTTP 429 (`RateLimited` with `Retry-After`), 401/403 (`AuthExpired`), 404 (`NotFound`), 5xx/timeouts (`Transient`), and 400 (`Permanent`).
- **Verification Command:**
  - `uv run pytest tests/unit/test_gmail_adapter.py`
- **Result:** PASS (5 passed in 0.09s)

## Step 2: Implement 7 Protocol Methods & Contract Test Suite
- **Files Changed:**
  - `packages/adapters/gmail.py`
  - `packages/adapters/registry.py`
  - `packages/adapters/__init__.py`
  - `tests/unit/test_gmail_adapter.py`
- **What Changed:**
  - Implemented all 7 protocol methods on `GmailProviderAdapter`: `subscribe`, `renew_subscription`, `synchronize`, `get_message`, `get_thread`, `create_draft`, and `send_reply`.
  - Registered `GmailProviderAdapter` under provider key `"gmail"` in `registry.py` and exported from `packages.adapters`.
  - Added `TestGmailProviderAdapterContract(MailProviderAdapterContractSuite)` in `test_gmail_adapter.py` using `create_mock_gmail_transport()`.
  - Fixed mock transport route matching to correctly handle `GET /messages?maxResults=50` list endpoint.
- **Verification Command:**
  - `uv run pytest tests/unit/test_gmail_adapter.py`
- **Result:** PASS (14 passed in 0.16s)

## Step 3: Implement Pub/Sub Parsing, Expired History & Rate Limiting
- **Files Changed:**
  - `packages/adapters/gmail.py`
  - `packages/adapters/__init__.py`
  - `tests/unit/test_gmail_adapter.py`
- **What Changed:**
  - Implemented `GmailPushNotification` dataclass and `parse_pubsub_notification()` decoding nested Google Cloud Pub/Sub base64 `message.data` JSON envelopes (extracting `emailAddress` and `historyId`).
  - Added multi-page `history.list()` pagination walking `nextPageToken` and accumulating unique message IDs.
  - Added robust detection of expired/invalid `historyId` (handling both HTTP 404 and HTTP 400 history-expired responses) returning `requires_full_resync=True` with `sync_state="full_resync"`.
  - Added unit tests covering standard Pub/Sub envelope, direct payload dictionary, JSON str, UTF-8 bytes, invalid format validation, 404/400 expired history resync fallback, and multi-page history pagination.
- **Verification Command:**
  - `uv run pytest tests/unit/test_gmail_adapter.py`
- **Result:** PASS (21 passed in 0.20s)

## Step 4: Ensure Architectural Isolation, Linting & Full Test Suite
- **Files Changed:**
  - `tests/unit/test_adapter_registry.py`
  - `tests/unit/test_gmail_adapter.py`
- **What Changed:**
  - Verified architectural boundary rules in `tests/unit/test_dependency_rules.py` ensuring mail provider identifier `"gmail"` is strictly confined to `packages/adapters/`.
  - Formatted files with `ruff format`, fixed all import ordering, and resolved fixture type hint in `tests/unit/test_adapter_registry.py`.
  - Ran `ruff check` (0 errors) and strict `mypy` static type checking (0 issues across 103 source files).
  - Executed full test suite via `make ci`: 174 unit tests and 31 integration tests (205 total tests passed in 14.82s).
- **Verification Command:**
  - `make ci`
- **Result:** PASS (205 passed in 14.82s)

# Execution Log: Phase 1 Task 1.4 — Microsoft Graph Adapter

## Step 1: Implement Graph HTTP Client Core, Error Translation & Notification Parsing
- **Files Changed:**
  - `packages/adapters/graph.py`
  - `tests/unit/test_graph_adapter.py`
- **What Changed:**
  - Implemented `GraphProviderAdapter` HTTP dispatcher `_request()` translating Microsoft Graph API errors: HTTP 429 (`RateLimited` with `Retry-After`), HTTP 401/403 (`AuthExpired`), HTTP 404 (`NotFound`), HTTP 410 (`Permanent` with delta token expired / gone), 5xx/network errors (`Transient`), and HTTP 400 (`Permanent`).
  - Implemented `GraphChangeNotification` dataclass and `parse_graph_notification()` extracting items from the `"value"` array (decoding dicts, JSON strings, and bytes, and capturing `subscriptionId`, `changeType`, `resource`, `resourceData.id`, `clientState`, `subscriptionExpirationDateTime`).
  - Added unit tests in `tests/unit/test_graph_adapter.py` covering rate limiting with `Retry-After`, auth expiration, resource not found, server/transient errors, bad request, and notification parsing.
- **Verification Command:**
  - `uv run pytest tests/unit/test_graph_adapter.py -k "test_error_translation or test_parse_notification"`
- **Result:** PASS (8 passed)

## Step 2: Implement 7 Protocol Methods & Contract Test Suite
- **Files Changed:**
  - `packages/adapters/graph.py`
  - `packages/adapters/registry.py`
  - `packages/adapters/__init__.py`
  - `tests/unit/test_graph_adapter.py`
- **What Changed:**
  - Implemented all 7 protocol methods on `GraphProviderAdapter`: `subscribe` (creates Graph webhook subscription), `renew_subscription` (patches expiration date), `synchronize` (executes delta query), `get_message` (fetches RFC822 MIME byte stream from `/$value` and metadata), `get_thread` (filters messages by `conversationId`), `create_draft` (creates draft in `/messages`), and `send_reply` (dispatches message via `/sendMail`).
  - Added alias `MicrosoftGraphProviderAdapter = GraphProviderAdapter` for spec compliance (R1.2).
  - Registered `GraphProviderAdapter` under provider key `"graph"` in `packages/adapters/registry.py` and exported from `packages.adapters`.
  - Added `TestGraphProviderAdapterContract(MailProviderAdapterContractSuite)` in `tests/unit/test_graph_adapter.py` using `create_mock_graph_transport()`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_graph_adapter.py -k "TestGraphProviderAdapterContract or test_graph_adapter_registered"`
- **Result:** PASS (9 passed)

## Step 3: Implement Delta Query Traversal & Invalid Delta Token Detection
- **Files Changed:**
  - `packages/adapters/graph.py`
  - `tests/unit/test_graph_adapter.py`
- **What Changed:**
  - Implemented multi-page delta traversal in `synchronize()` following `@odata.nextLink` pages, accumulating unique message IDs, and saving the terminal `@odata.deltaLink` into `Checkpoint.delta_link` (R2.6).
  - Implemented expired/invalid delta token detection (detecting HTTP 410 Gone, `ResyncRequired`, and `InvalidDeltaToken`), safely returning `SyncResult(requires_full_resync=True, has_more=False)` with `Checkpoint(sync_state="full_resync", delta_link=None)` (R2.7).
  - Added unit tests: `test_delta_pagination_follows_next_link_to_delta_link`, `test_delta_token_expired_triggers_full_resync_410`, and `test_delta_token_resync_required_error_code`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_graph_adapter.py -k "delta"`
- **Result:** PASS (3 passed)

## Step 4: Ensure Architectural Isolation, Formatting, Linting & Full CI Suite
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
  - `artifacts/superpowers/finish.md`
- **What Changed:**
  - Verified architectural boundary rules in `tests/unit/test_dependency_rules.py` ensuring provider name `"graph"` is strictly confined to `packages/adapters/`.
  - Formatted all files with `ruff format`, resolved all `ruff check` lint warnings and unused imports.
  - Validated strict type checking with `mypy` across 105 source files (0 issues).
  - Executed full test suite via `make ci`: 194 unit tests and 31 integration tests (225 total tests passed in 14.54s).
  - Marked Task 1.4 completed in `specs/tasks.md`.
- **Verification Command:**
  - `make ci`
- **Result:** PASS (225 passed in 14.54s)

# Execution Log: Phase 1 Task 1.5 — Webhook Receivers

## Step 1: Enhance JobEnvelope for Mailbox Sync Jobs
- **Files Changed:**
  - `packages/broker/envelope.py`
  - `tests/unit/test_broker.py`
- **What Changed:**
  - Updated `JobEnvelope` to make `message_id` and `thread_id` default to `""` for mailbox-level pipeline jobs (`job_type="sync_mailbox"`).
  - Added `mailbox_id: str | None = None` and `payload: dict[str, Any] = Field(default_factory=dict)` to `JobEnvelope`.
  - Added `mailbox_id` propagation into AMQP message headers in `to_message()`.
  - Added `test_job_envelope_sync_mailbox_defaults()` verifying serialization/deserialization and header assignment.
- **Verification Command:**
  - `uv run pytest tests/unit/test_broker.py`
- **Result:** PASS (6 passed in 0.85s)

## Step 2: Implement Webhook Router & Handshakes in packages/adapters/webhooks.py
- **Files Changed:**
  - `packages/adapters/webhooks.py`
  - `packages/adapters/__init__.py`
  - `services/api/main.py`
- **What Changed:**
  - Created `packages/adapters/webhooks.py` implementing `webhook_router`:
    - Microsoft Graph endpoints (`GET/POST /v1/webhooks/graph` and `/v1/webhooks/graph/{mailbox_id}`): completes validation handshake when `validationToken` query parameter is present (returns 200 `text/plain` with exact token string, R2.1); handles change notification JSON payloads, parsing signals with `parse_graph_notification()`, enqueuing `sync_mailbox` jobs to `mail.sync` via RabbitMQ exchange `mail.ingest`, and acknowledging with 202 Accepted within 5s with zero provider fetches (R2.2, R2.3).
    - Gmail / Google Cloud Pub/Sub endpoints (`GET/POST /v1/webhooks/gmail` and `/v1/webhooks/gmail/{mailbox_id}`): completes verification handshake probes when challenge/token parameters are present (returns 200 `text/plain`, R2.1); handles push notification envelopes, parsing signals with `parse_pubsub_notification()`, enqueuing `sync_mailbox` jobs to `mail.sync`, and acknowledging with 200 OK within 5s with zero provider fetches (R2.2, R2.3).
  - Exported `webhook_router` from `packages/adapters/__init__.py` and mounted in `services/api/main.py`.
  - Initialized `app.state.publisher` in `app_lifespan` in `services/api/main.py`.
  - Confined all provider string literals and parser imports strictly within `packages/adapters/` (GEMINI.md §4, R1.3).
- **Verification Command:**
  - `uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS (4 passed in 0.29s)

## Step 3: Implement Comprehensive Webhook Receiver Unit Tests
- **Files Changed:**
  - `tests/unit/test_webhook_receivers.py`
- **What Changed:**
  - Created unit test suite covering Microsoft Graph and Gmail / Pub/Sub webhook receivers:
    - Verified Graph validation handshake on GET and POST responding with exact `validationToken` in `text/plain; charset=utf-8` (R2.1).
    - Verified Graph change notification ingestion, decoding `"value"` notification items, treating payloads as signals only, enqueuing `JobEnvelope(job_type="sync_mailbox")` to `mail.ingest`/`mail.sync.requested`, performing no synchronous provider fetches, and returning 202 Accepted (R2.2, R2.3).
    - Verified Gmail / Google Cloud Pub/Sub validation probe and challenge query echo (R2.1).
    - Verified Gmail Pub/Sub push envelope decoding, signal extraction (`email_address`, `history_id`), `sync_mailbox` enqueuing to `mail.sync.requested`, and immediate 200 OK acknowledgment (R2.2, R2.3).
    - Verified mailbox path parameter overrides and graceful error handling for empty/ping payloads and malformed request bodies.
- **Verification Command:**
  - `uv run pytest tests/unit/test_webhook_receivers.py`
- **Result:** PASS (10 passed in 0.70s)

## Step 4: Formatting, Linting, Type Checking & Full CI Suite
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
  - `artifacts/superpowers/finish.md`
- **What Changed:**
  - Formatted all files with `ruff format`, resolved all `ruff check` lint warnings and unused imports.
  - Verified architectural boundary rules in `tests/unit/test_dependency_rules.py` ensuring provider names are strictly confined to `packages/adapters/`.
  - Validated strict type checking with `mypy` across 107 source files (0 issues).
  - Executed full test suite via `make ci`: 205 unit tests and 31 integration tests (236 total tests passed in 13.54s).
  - Marked Task 1.5 completed in `specs/tasks.md`.
- **Verification Command:**
  - `make ci`
- **Result:** PASS (236 passed in 13.54s)

# Execution Log: Phase 1 Task 1.6 — Checkpoint Store & Sync Orchestration

## Batch 1: Domain & Store Interfaces and Backends
- **Files Changed:**
  - `packages/domain/entities.py`
  - `packages/db/checkpoint.py`
  - `packages/db/mailbox.py`
  - `packages/db/__init__.py`
- **What Changed:**
  - Added optional `organization_id: UUID | str | None = None` to `Checkpoint` dataclass.
  - Implemented `CheckpointStore` protocol with `PostgresCheckpointStore` and `InMemoryCheckpointStore`. Supported atomic locking via conditional SQL update (`sync_state = 'syncing'`), pending follow-up toggling, and multi-tenant persistence.
  - Implemented `MailboxStore` protocol with `PostgresMailboxStore` and `InMemoryMailboxStore` for mailbox lookups and status lifecycle management (`needs_reauth`).
- **Verification Command:**
  - `uv run pytest tests/unit/test_domain_entities.py`
- **Result:** PASS (10 passed in 0.05s)

## Batch 2: Sync Orchestration Loop in services/mail_connector
- **Files Changed:**
  - `services/mail_connector/orchestrator.py`
  - `services/mail_connector/__init__.py`
- **What Changed:**
  - Implemented `SyncOrchestrator` driving the canonical sync loop from `specs/design.md §5.1`.
  - Enforced in-flight coalescing via `checkpoint_store.try_acquire_lock`: concurrent sync requests atomically set `pending_followup = true` and exit with coalesced outcome (R2.9).
  - Bounded full re-sync: on `requires_full_resync=True`, records `sync_state='full_resync'`, resets cursors, updates `last_full_sync_at`, and executes full bounded sync (R2.7).
  - Stored raw MIME payloads in object storage (`raw/{org_id}/{mailbox_id}/{msg_id}.eml`) (R4.10, R5.8).
  - Published persistent `JobEnvelope(job_type="normalize_email")` with deterministic SHA-256 idempotency key to `email.process` / `email.normalize` (R3.1, R19.2).
  - Checkpoint advancement ordering: saved checkpoint **only after** all fetched messages from window are durably stored in object storage and published to broker (R2.8).
  - Handled `AuthExpired`: marked mailbox status to `needs_reauth`, recorded error state, and halted without spinning (R1.5, R2.10).
- **Verification Command:**
  - `uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS (4 passed in 0.38s)

## Batch 3 (Parallel Execution): Unit and Integration Tests
- **Files Changed:**
  - `tests/unit/test_sync_orchestrator.py`
  - `tests/integration/test_checkpoint_postgres.py`
- **What Changed:**
  - Added 8 unit tests in `test_sync_orchestrator.py` covering initial sync, incremental sync, failure during side-effects preventing checkpoint advance (R2.8), full resync on expired checkpoint (R2.7), in-flight coalescing (R2.9), multi-page pagination, and auth expiration (R1.5).
  - Added 4 integration tests in `test_checkpoint_postgres.py` verifying real PostgreSQL operations: CRUD round-trip, atomic in-flight locking, atomic pending follow-up toggling, and mailbox status updates.
- **Verification Commands:**
  - `uv run pytest tests/unit/test_sync_orchestrator.py -v`
  - `uv run pytest tests/integration/test_checkpoint_postgres.py -v`
- **Results:**
  - `test_sync_orchestrator.py`: PASS (8 passed in 0.61s)
  - `test_checkpoint_postgres.py`: PASS (4 passed in 1.30s)

## Batch 4: Formatting, Static Typing, Full CI & Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
  - `artifacts/superpowers/finish.md`
- **What Changed:**
  - Formatted codebase and fixed line lengths with `ruff`.
  - Validated strict type checking with `mypy` across 112 source files (0 errors).
  - Executed complete CI suite via `make ci`: 213 unit tests + 35 integration tests (248 total passed in 13.00s).
  - Marked Task 1.6 completed in `specs/tasks.md`.
- **Verification Command:**
  - `make ci`
- **Result:** PASS (248 passed)

# Execution Log: Phase 1 Task 1.7 — Subscription Renewal Job

## Batch 1 (Parallel Execution): Schema Migration, Observability & Configuration
- **Files Changed:**
  - `migrations/0002_mailbox_subscription.up.sql`
  - `migrations/0002_mailbox_subscription.down.sql`
  - `tests/integration/test_database_schema.py`
  - `packages/observability/metrics.py`
  - `packages/core/settings.py`
  - `packages/core/__init__.py`
  - `.env.example`
  - `docs/configuration.md`
  - `tests/unit/test_observability_metrics.py`
- **What Changed:**
  - Created `0002_mailbox_subscription.up.sql` creating table `mailbox_subscription` with mandatory `organization_id` foreign key, uniqueness constraints, and indexes on `expires_at` and `mailbox_id` (R2.10, R5.3).
  - Created reversible rollback `0002_mailbox_subscription.down.sql` (R5.5).
  - Updated `test_database_schema.py` to assert `mailbox_subscription` tenant column scoping and enhanced `test_migration_reversibility` to roll back all discovered migrations.
  - Added `subscription_renewals_total` Counter with `["provider", "status"]` labels to `PipelineMetrics` (R21.4).
  - Added `SubscriptionRenewalSettings` (`renewal_threshold_hours`, `check_interval_seconds`, `batch_size`) to `AppSettings` (R20.6).
  - Documented new configuration parameters in `.env.example` and Section 2.14 of `docs/configuration.md`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_observability_metrics.py tests/integration/test_database_schema.py`
- **Result:** PASS (10 passed in 3.10s)

## Batch 2: Domain Entity & SubscriptionStore Implementations
- **Files Changed:**
  - `packages/domain/entities.py`
  - `packages/db/subscription.py`
  - `packages/db/__init__.py`
  - `tests/unit/test_subscription_store.py`
- **What Changed:**
  - Extended `Subscription` domain entity with `organization_id`, `last_renewed_at`, `last_renewal_status`, and `last_error` (default `None`).
  - Defined `SubscriptionStore` protocol (`get`, `get_by_mailbox`, `list_expiring`, `save`, `record_renewal_outcome`).
  - Implemented `PostgresSubscriptionStore` with asyncpg parameterized queries carrying `organization_id` on every query (R5.3).
  - Implemented in-memory test double `InMemorySubscriptionStore` with thread-safe `asyncio.Lock`.
  - Re-exported stores from `packages.db`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_subscription_store.py -v`
- **Result:** PASS (4 passed in 0.19s)

## Batch 3: Subscription Renewal Job & Error Taxonomy Orchestration
- **Files Changed:**
  - `services/mail_connector/renewal.py`
  - `services/mail_connector/__init__.py`
  - `tests/unit/test_subscription_renewal.py`
- **What Changed:**
  - Implemented `SubscriptionRenewalJob` driving scheduled renewal cycles (`run_once`, `run_loop`).
  - Enforced provider name isolation: `services/mail_connector` interacts strictly with `MailProviderAdapter` protocol and neutral adapter resolver (GEMINI.md §4).
  - Evaluated expiring subscriptions (`expires_at <= now() + renewal_threshold_hours`).
  - Handled `AuthExpired`: marked mailbox operational status as `'needs_reauth'` via `MailboxStore.update_status()`, recorded outcome with error message, incremented telemetry counter, and halted without spinning (R1.5, R2.10).
  - Handled `RateLimited` (preserving `retry_after`), `Transient`, and `Permanent` errors per common taxonomy.
  - Skipped mailboxes already in `'needs_reauth'` status to prevent spinning.
- **Verification Command:**
  - `uv run pytest tests/unit/test_subscription_renewal.py -v`
- **Result:** PASS (6 passed in 0.44s)

## Batch 4: PostgreSQL Integration, Multi-Tenant Assertions & Full CI
- **Files Changed:**
  - `tests/integration/test_subscription_renewal_postgres.py`
  - `specs/tasks.md`
  - `artifacts/superpowers/finish.md`
- **What Changed:**
  - Added live integration tests connecting to PostgreSQL container:
    - Multi-tenant CRUD operations across $\ge 3$ tenants with strict isolation (R5.3).
    - End-to-end renewal cycle verifying `status="success"` and updated `expires_at` persisted to database.
    - End-to-end `AuthExpired` transition verifying `mailbox.status = 'needs_reauth'` committed to database and second pass skipped without spinning.
  - Verified architectural boundary rules (`test_dependency_rules.py`).
  - Formatted and verified static typing: 0 lint errors, 0 mypy issues across 117 source files.
  - Executed full test suite: 223 unit tests + 37 integration tests = 260 tests passed.
  - Marked Task 1.7 complete (`[x]`) in `specs/tasks.md`.
- **Verification Command:**
  - `uv run ruff check . && uv run mypy . && uv run pytest tests/unit/ && uv run pytest tests/integration/`
- **Result:** PASS (260 passed, 0 lint/type issues)

# Execution Log: Phase 1 Task 1.8 — Manual Re-Sync Endpoint

## Step 1: Request/Response Schemas & Mailbox Router (R2.11, R23.2, R23.6, R5.3)
- **Files Changed:**
  - `services/api/schemas/mailboxes.py` [NEW]
  - `services/api/schemas/__init__.py` [NEW]
  - `services/api/routers/mailboxes.py` [NEW]
  - `services/api/routers/v1.py` [MODIFY]
- **What Changed:**
  - Defined Pydantic V2 models `TimeWindow`, `ResyncRequest`, `ResyncResponse`, and `MailboxResponse` with model validator enforcing `since <= until`.
  - Implemented `POST /v1/mailboxes/{id}/resync` accepting `since`, `until`, `full_resync`, and `force`.
  - Enforced tenant verification against `X-Organization-ID`: returns 404 for unknown mailboxes or mailboxes belonging to different tenants (R23.6, R5.3).
  - Implemented guard against spinning on invalid auth: returns 409 Conflict if mailbox is in `needs_reauth` or `paused` status unless `force=True` (R1.5, R2.10).
  - Implemented `GET /v1/mailboxes/{id}` returning mailbox status and metadata (R23.2).
  - Mounted mailbox router onto `v1_router` under prefix `/mailboxes`.
- **Verification Command:**
  - `uv run ruff check services/api && uv run mypy services/api`
- **Result:** PASS

## Step 2: Dependency Injection & Error Serialization (R3.1, R21.1)
- **Files Changed:**
  - `services/api/dependencies.py` [MODIFY]
  - `services/api/errors.py` [MODIFY]
  - `services/api/main.py` [MODIFY]
- **What Changed:**
  - Added `get_mailbox_store` falling back from `app.state.mailbox_store` to `PostgresMailboxStore(db_pool)` or `InMemoryMailboxStore`.
  - Added `get_job_publisher` resolving `app.state.publisher`.
  - Wired `jsonable_encoder` in `validation_exception_handler` and `http_exception_handler` to guarantee JSON serializability of Pydantic V2 validation contexts.
  - Bound OpenTelemetry span `api.mailbox.resync` and correlation context variables (`mailbox_id`, `organization_id`).
- **Verification Command:**
  - `uv run python -m services.api.openapi --check`
- **Result:** PASS (OpenAPI schema valid v3.1.0, 11 paths)

## Step 3: Comprehensive Unit Test Suite (R2.11, R23.6, R5.3)
- **Files Changed:**
  - `tests/unit/test_api_resync.py` [NEW]
- **What Changed:**
  - Implemented 13 unit tests covering:
    - Successful 202 Accepted response with `JobEnvelope` enqueued to `mail.ingest` / `mail.sync.requested`.
    - Time window filtering (`since`, `until`) and `full_resync` flags preserved in AMQP payload.
    - 422 Unprocessable Content when `since > until`.
    - 404 Not Found on missing mailbox or cross-tenant request.
    - 400 Bad Request when `X-Organization-ID` is omitted.
    - 409 Conflict for `needs_reauth` or `paused` mailbox without `force`, and 202 with status reset to active when `force=True`.
    - `GET /v1/mailboxes/{id}` returning `MailboxResponse` and enforcing tenant scoping.
- **Verification Command:**
  - `uv run pytest tests/unit/test_api_resync.py -v`
- **Result:** PASS (13 passed in 1.23s)

## Step 4: End-to-End Integration Tests (R2.11, R20.1)
- **Files Changed:**
  - `tests/integration/test_api_resync_e2e.py` [NEW]
- **What Changed:**
  - Implemented live integration tests connecting to PostgreSQL (port 5433) and RabbitMQ (port 5672) containers.
  - Seeded $\ge 3$ tenants with real mailboxes to verify multi-tenant isolation.
  - Exercised `GET /v1/mailboxes/{id}` and `POST /v1/mailboxes/{id}/resync` via `httpx.AsyncClient` inside `app.router.lifespan_context(app)`.
  - Verified message arrival on RabbitMQ queue `mail.sync.requested` with `delivery_mode=PERSISTENT`, correlation headers, and full payload.
  - Verified operational override updating `mailbox.status` in PostgreSQL from `needs_reauth` to `active` upon `force=True`.
  - Verified OpenAPI 3.1 schema includes resync and mailbox endpoints.
- **Verification Command:**
  - `uv run pytest tests/integration/test_api_resync_e2e.py -v`
- **Result:** PASS (2 passed in 2.31s)

## Step 5: Full Verification & Sign-Off
- **Files Changed:**
  - `specs/tasks.md` [MODIFY]
  - `artifacts/superpowers/execution.md` [MODIFY]
  - `artifacts/superpowers/finish.md` [MODIFY]
- **What Changed:**
  - Marked Task 1.8 complete (`[x]`) in `specs/tasks.md`.
  - Validated zero lint issues (`ruff check`), zero static typing errors (`mypy`), and clean architectural boundary checks.
  - Verified full test suite across 275 unit and integration tests.
- **Verification Command:**
  - `uv run ruff check . && uv run mypy . && uv run pytest tests/unit/ tests/integration/`
- **Result:** PASS (275 passed in 22.57s)



# Execution Log: Phase 1 Task 1.9 — Raw Payload Archival

## Step 1: Raw Payload Archiver Core & Replay Abstractions (R4.10, R5.8)
- **Files Changed:**
  - `packages/core/archive.py` [NEW]
  - `packages/core/__init__.py` [MODIFY]
- **What Changed:**
  - Implemented `ArchivedPayloadRef` dataclass capturing `object_key`, `bucket`, `sha256`, `size_bytes`, `content_type`, and `metadata`.
  - Implemented `RawPayloadArchiver` with `archive()`, `retrieve()`, and `create_replay_envelope()`.
  - Implemented `compute_payload_digest()` and `ChecksumMismatchError` for data integrity validation.
  - Re-exported new symbols from `packages.core`.
- **Verification Command:**
  - `uv run ruff check packages/core && uv run mypy packages/core`
- **Result:** PASS (0 errors across 7 files)

## Step 2: Observability & Metric Instruments (R21.4)
- **Files Changed:**
  - `packages/observability/metrics.py` [MODIFY]
  - `tests/unit/test_observability_metrics.py` [MODIFY]
- **What Changed:**
  - Defined `PAYLOAD_SIZE_BUCKETS` (1KB, 10KB, 50KB, 250KB, 1MB, 5MB, 25MB).
  - Added `raw_payloads_archived_total` Counter with `["provider", "status"]` labels.
  - Added `raw_payload_size_bytes` Histogram with `["provider"]` labels and payload size buckets.
  - Updated unit tests verifying registration of the new metric instruments.
- **Verification Command:**
  - `uv run pytest tests/unit/test_observability_metrics.py -v && uv run ruff check packages/observability && uv run mypy packages/observability`
- **Result:** PASS (3/3 tests passed, 0 lint/mypy issues)

## Step 3: Integrate Archiver into Sync Orchestrator (R4.10, R5.8, R3.1)
- **Files Changed:**
  - `services/mail_connector/orchestrator.py` [MODIFY]
- **What Changed:**
  - Injected `RawPayloadArchiver` into `SyncOrchestrator` with fallback to default settings.
  - Wrapped raw message archival with OpenTelemetry span `mail.archive_raw` and telemetry recording (`raw_payloads_archived_total`, `raw_payload_size_bytes`).
  - Enriched published `JobEnvelope(job_type="normalize_email")` payload with `sha256`, `size_bytes`, and `raw_bucket` alongside `raw_object_key`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_sync_orchestrator.py -v && uv run ruff check services/mail_connector && uv run mypy services/mail_connector`
- **Result:** PASS (8/8 tests passed, 0 lint/mypy issues)

## Step 4: Unit Test Suite for Raw Payload Archival (R4.10, R5.8)
- **Files Changed:**
  - `tests/unit/test_raw_payload_archival.py` [NEW]
- **What Changed:**
  - Tested binary RFC 822 MIME byte archival and string payload archival.
  - Verified deterministic SHA-256 calculation and key formatting `raw/{org_id}/{mbx_id}/{msg_id}.eml`.
  - Verified S3 metadata headers (`organization_id`, `mailbox_id`, `provider_message_id`, `sha256`, `size_bytes`, `archived_at`).
  - Tested retrieval with checksum verification and `ChecksumMismatchError` on corruption.
  - Tested `create_replay_envelope()` generating persistent `JobEnvelope(job_type="normalize_email")` ready for replay.
- **Verification Command:**
  - `uv run pytest tests/unit/test_raw_payload_archival.py -v && uv run ruff check tests/unit/test_raw_payload_archival.py && uv run mypy tests/unit/test_raw_payload_archival.py`
- **Result:** PASS (7/7 tests passed, 0 lint/mypy issues)

## Step 5: Live MinIO Integration Tests & Multi-Tenant Scoping (R4.10, R5.8)
- **Files Changed:**
  - `tests/integration/test_raw_payload_archival_minio.py` [NEW]
- **What Changed:**
  - Added live integration tests running against MinIO container on port 9000.
  - Validated multi-tenant raw MIME archival across >=3 tenants with tenant-isolated object keys.
  - Verified byte-for-byte retrieval and SHA-256 integrity verification.
  - Verified S3 metadata persistence (`x-amz-meta-organization_id`, `x-amz-meta-sha256`, `x-amz-meta-provider_message_id`).
  - Tested replay envelope reconstitution and AMQP serialization.
- **Verification Command:**
  - `uv run pytest tests/integration/test_raw_payload_archival_minio.py -v && uv run ruff check tests/integration/test_raw_payload_archival_minio.py && uv run mypy tests/integration/test_raw_payload_archival_minio.py`
- **Result:** PASS (1 passed in 0.21s, 0 lint/mypy issues)

## Step 6: Full Verification, Documentation & Sign-Off (DoD)
- **Files Changed:**
  - `specs/tasks.md` [MODIFY]
  - `artifacts/superpowers/execution.md` [MODIFY]
  - `artifacts/superpowers/finish.md` [MODIFY]
- **What Changed:**
  - Marked Task 1.9 complete (`[x]`) in `specs/tasks.md`.
  - Validated full test suite: 283 passed (243 unit + 40 integration).
  - Validated 0 ruff errors and 0 mypy issues across all 125 source files.
  - Verified architectural boundaries strictly preserved.
- **Verification Command:**
  - `uv run ruff check . && uv run mypy packages services tests evaluation && uv run pytest tests/unit/ tests/integration/`
- **Result:** PASS (283 passed in 27.30s)

# Execution Log: Phase 1 Task 1.10 — MIME Normalization

## Step 1: HTML-to-Text Converter (R4.2)
- **Files Changed:**
  - `services/email_worker/html.py` [NEW]
  - `services/email_worker/__init__.py` [MODIFY]
- **What Changed:**
  - Implemented `HTMLToTextConverter` and `html_to_text()` using Python stdlib `HTMLParser`.
  - Preserves hyperlinks in `anchor text (url)` format, strips 1x1 / 0x0 / hidden tracking pixels, and drops `<script>`, `<style>`, `<meta>`, and `<noscript>` elements.
  - Normalizes block elements (`p`, `div`, `h1`-`h6`, `table`, `li`) into structured plain text without excessive empty lines.
- **Verification Command:**
  - `uv run ruff check services/email_worker/html.py services/email_worker/__init__.py && uv run mypy services/email_worker/html.py services/email_worker/__init__.py`
- **Result:** PASS (0 errors, 0 mypy issues)

## Step 2: Quoted-History Separation & Signature Detection (R4.3, R4.4)
- **Files Changed:**
  - `services/email_worker/history.py` [NEW]
  - `services/email_worker/__init__.py` [MODIFY]
- **What Changed:**
  - Implemented `separate_quoted_history()` recognizing international and multi-client reply headers (`On ... wrote:`, `-----Original Message-----`, `Le ... a écrit :`, Outlook blocks, trailing `>` quote lines).
  - Implemented `detect_and_strip_signature()` detecting RFC 3676 delimiters (`-- `), mobile signatures (`Sent from my iPhone/iPad/Galaxy/Android`), and standard sign-offs (`Best regards,`, `Thanks,`, `Sincerely,`).
  - Implemented `clean_email_body()` combining history separation and signature stripping to return `(body_text, body_text_clean, signature_stripped)`.
- **Verification Command:**
  - `uv run ruff check services/email_worker/history.py services/email_worker/__init__.py && uv run mypy services/email_worker/history.py services/email_worker/__init__.py`
- **Result:** PASS (0 errors, 0 mypy issues)

## Step 3: Attachment Metadata Extraction (R4.7, R5.8)
- **Files Changed:**
  - `services/email_worker/attachments.py` [NEW]
  - `services/email_worker/__init__.py` [MODIFY]
- **What Changed:**
  - Implemented `extract_attachments_from_message()` traversing MIME tree to identify attachments and inline media with filenames or Content-IDs.
  - Computed size, SHA-256 digests, and deterministic object storage keys via `ObjectKeyBuilder.attachment(org_id, msg_id, att_id, filename)`.
  - Implemented async `offload_attachments()` offloading binary payloads to MinIO `bucket_attachments` with metadata headers.
- **Verification Command:**
  - `uv run ruff check services/email_worker/attachments.py services/email_worker/__init__.py && uv run mypy services/email_worker/attachments.py services/email_worker/__init__.py`
- **Result:** PASS (0 errors, 0 mypy issues)

## Step 4: MIME Parser & Email Normalizer Orchestrator (R4.1, R4.2, R4.9, design.md §5.2)
- **Files Changed:**
  - `packages/domain/entities.py` [MODIFY]
  - `services/email_worker/parser.py` [NEW]
  - `services/email_worker/normalizer.py` [NEW]
  - `services/email_worker/__init__.py` [MODIFY]
  - `tests/unit/test_domain_entities.py` [MODIFY]
- **What Changed:**
  - Extended `NormalizedMessage` with `signature_stripped: bool`, `.flags` dict, and `.to_contract_dict()` matching `design.md §5.2`.
  - Implemented `parse_mime_bytes()`, `extract_email_headers()`, and `select_message_body()` with RFC 2047 decoding, subject normalization, and HTML-to-text fallback when no plain text part exists (R4.2).
  - Implemented `EmailNormalizer` coordinating headers, cleaned body (`body_text` and `body_text_clean`), snippet, attachments, and flags.
  - Implemented `normalize_and_offload()` for asynchronous storage offload of attachments and raw HTML bodies.
- **Verification Command:**
  - `uv run ruff check services/email_worker packages/domain && uv run mypy services/email_worker packages/domain && uv run pytest tests/unit/test_domain_entities.py tests/unit/test_dependency_rules.py`
- **Result:** PASS (14/14 tests passed, 0 errors, 0 mypy issues)

## Step 5: Unit Tests for Pure Normalization Components (R4.1–R4.4, R4.7, R24.3)
- **Files Changed:**
  - `tests/unit/test_email_normalization.py` [NEW]
- **What Changed:**
  - Added 30 comprehensive unit tests covering HTML parsing (tracking pixel removal, hyperlink formatting, entity unescaping, block spacing), quoted-history splitting, signature detection (RFC 3676, mobile, sign-offs), subject normalization, and attachment extraction with SHA-256 digests.
  - Tested `EmailNormalizer` end-to-end for `multipart/alternative`, HTML-only fallback, and corrupted payload failure handling.
- **Verification Command:**
  - `uv run ruff check tests/unit/test_email_normalization.py && uv run mypy tests/unit/test_email_normalization.py && uv run pytest tests/unit/test_email_normalization.py -v`
- **Result:** PASS (30/30 passed in 0.21s, 0 lint/mypy issues)

## Step 6: Awkward Real-World MIME Corpus Test Suite (R4.1–R4.4, R4.7, R24.3)
- **Files Changed:**
  - `tests/fixtures/mime/*.eml` [NEW] (10 awkward real-world MIME fixture files)
  - `tests/unit/test_mime_corpus.py` [NEW]
  - `services/email_worker/parser.py` [MODIFY]
  - `services/email_worker/attachments.py` [MODIFY]
- **What Changed:**
  - Created 10 real-world awkward MIME fixtures: `multipart/alternative`, HTML-only fallback, plain-text-only, ISO-8859-1 (Latin-1 8-bit), Windows-1252, Shift-JIS, RFC 2047 encoded-word headers, inline image media with Content-ID, multi-tier nested quote replies, and nested forwarded `message/rfc822` attachment.
  - Enhanced `parser.py` with raw surrogateescape recovery and charset fallback cascade to handle unencoded 8-bit headers.
  - Enhanced `attachments.py` to extract attached `message/rfc822` email files while ignoring recursive internal subparts.
  - Validated all 10 fixtures through `EmailNormalizer` with complete assertions on canonical entity fields and flags.
- **Verification Command:**
  - `uv run ruff check tests/unit/test_mime_corpus.py && uv run mypy tests/unit/test_mime_corpus.py && uv run pytest tests/unit/test_mime_corpus.py -v`
- **Result:** PASS (10/10 passed in 0.16s, 0 lint/mypy issues)

## Step 7: Full Verification, Documentation & Sign-Off (DoD)
- **Files Changed:**
  - `specs/tasks.md` [MODIFY]
  - `artifacts/superpowers/execution.md` [MODIFY]
  - `artifacts/superpowers/finish.md` [NEW]
- **What Changed:**
  - Marked Task 1.10 as completed (`[x]`) in `specs/tasks.md`.
  - Executed full lint check (`uv run ruff check .`): 0 errors across 132 source files.
  - Executed strict type checking (`uv run mypy packages services tests evaluation`): 0 issues across 132 source files.
  - Executed full test suite (`uv run pytest tests/unit/ tests/integration/`): 323 passed in 21.69s (283 previous + 40 new).
  - Verified architectural boundaries strictly preserved.
- **Verification Command:**
  - `uv run ruff check . && uv run mypy packages services tests evaluation && uv run pytest tests/unit/ tests/integration/`
- **Result:** PASS (323 passed, 0 lint/mypy issues)

# Execution Log: Phase 1 Task 1.11 — Subject Normalization & Thread Association

## Step 1: Domain Entity & Configuration (R4.5, R4.6)
- **Files Changed:**
  - `packages/domain/entities.py` [MODIFY]
  - `packages/domain/__init__.py` [MODIFY]
  - `packages/core/settings.py` [MODIFY]
  - `packages/core/__init__.py` [MODIFY]
  - `.env.example` [MODIFY]
  - `docs/configuration.md` [MODIFY]
- **What Changed:**
  - Defined `EmailThread` domain entity (`id`, `organization_id`, `mailbox_id`, `provider_thread_id`, `subject_normalized`, `participants`, `first_message_at`, `last_message_at`, `message_count`, `status`).
  - Added `ThreadAssociationSettings` (`window_days: int = 14`) to `AppSettings`.
  - Added `THREAD_ASSOCIATION__WINDOW_DAYS=14` to `.env.example` and documented it in `docs/configuration.md`.
- **Verification Command:**
  - `uv run ruff check packages/domain packages/core && uv run mypy packages/domain packages/core`
- **Result:** PASS (0 errors, 0 mypy issues across 10 source files)

## Step 2: Database Thread Store (R4.6, design.md §5.2)
- **Files Changed:**
  - `packages/db/thread.py` [NEW]
  - `packages/db/__init__.py` [MODIFY]
- **What Changed:**
  - Implemented `ThreadStore` protocol defining thread lookup (`find_by_provider_thread_id`, `find_by_rfc822_message_id`, `find_by_references`, `find_by_subject_and_participants`), creation, and atomic update.
  - Implemented `InMemoryThreadStore` for isolated pure unit tests with subject matching and participant overlap logic.
  - Implemented `PostgresThreadStore` utilizing PostgreSQL array overlap operator (`participants && $4`), atomic `LEAST`/`GREATEST` timestamp progression, and `array_agg(DISTINCT p)` participant expansion.
- **Verification Command:**
  - `uv run ruff check packages/db && uv run mypy packages/db`
- **Result:** PASS (0 errors, 0 mypy issues across 14 source files)

## Step 3: Thread Associator Service & Normalization Helpers (R4.5, R4.6)
- **Files Changed:**
  - `services/email_worker/threading.py` [NEW]
  - `services/email_worker/__init__.py` [MODIFY]
- **What Changed:**
  - Implemented `extract_participant_emails()` parsing and deduplicating sender, recipient, and cc email addresses into normalized lowercase sorted sets.
  - Defined `ThreadAssociationReason` enum (`provider_thread_id`, `in_reply_to`, `references`, `subject_participants`, `new_thread`) and `ThreadAssociationResult`.
  - Implemented `ThreadAssociator` orchestrating the 4-tier decision cascade: provider thread ID -> In-Reply-To -> References -> normalized subject + overlapping participants within time window -> new thread, while advancing message counts and timestamps.
- **Verification Command:**
  - `uv run ruff check services/email_worker/threading.py services/email_worker/__init__.py && uv run mypy services/email_worker/threading.py services/email_worker/__init__.py`
- **Result:** PASS (0 errors, 0 mypy issues)

## Step 4: Pure Unit Tests for Thread Association (R4.5, R4.6)
- **Files Changed:**
  - `tests/unit/test_thread_association.py` [NEW]
  - `services/email_worker/threading.py` [MODIFY]
- **What Changed:**
  - Added 26 unit tests covering subject prefix normalization (`Re:`, `Fwd:`, `Aw:`, `Re[2]:`, stacked, whitespace), participant extraction/deduplication, and the full 4-tier association cascade.
  - Verified Tier 1 provider thread ID matching, Tier 2a In-Reply-To matching, Tier 2b References matching (prioritizing latest reference), Tier 3 subject + participant overlap within window, failure cases when outside window or disjoint participants, and Tier 4 new thread fallback.
- **Verification Command:**
  - `uv run ruff check tests/unit/test_thread_association.py && uv run mypy tests/unit/test_thread_association.py && uv run pytest tests/unit/test_thread_association.py -v`
- **Result:** PASS (26/26 passed in 0.16s, 0 lint/mypy issues)

## Step 5: PostgreSQL Integration Tests with Multi-Tenant Fixtures (R4.5, R4.6, GEMINI.md §8)
- **Files Changed:**
  - `migrations/0003_email_thread_nullable_provider_thread_id.up.sql` [NEW]
  - `migrations/0003_email_thread_nullable_provider_thread_id.down.sql` [NEW]
  - `tests/integration/test_thread_postgres.py` [NEW]
- **What Changed:**
  - Added migration `0003_email_thread_nullable_provider_thread_id` to allow nullable `provider_thread_id` per `design.md §6.1` (for non-provider / IMAP threads).
  - Implemented 5 integration tests against ephemeral PostgreSQL:
    - Multi-tenant isolation verified across $\ge 3$ tenants with overlapping `provider_thread_id` values.
    - PostgreSQL array overlap querying (`participants && $4`) and sliding time-window logic.
    - Atomic thread counter increment (`message_count + 1`), `first_message_at` / `last_message_at` timestamp progression, and array deduplication.
    - In-Reply-To and References lookups against `email_message` with cross-tenant isolation.
    - Full `ThreadAssociator` pipeline execution with live PostgreSQL storage.
- **Verification Command:**
  - `uv run ruff check tests/integration/test_thread_postgres.py && uv run mypy tests/integration/test_thread_postgres.py && uv run pytest tests/integration/test_thread_postgres.py -v`
- **Result:** PASS (5/5 passed in 1.42s, 0 lint/mypy issues)

## Step 6: Full Verification, Documentation & Sign-Off (DoD)
- **Files Changed:**
  - `specs/tasks.md` [MODIFY]
  - `artifacts/superpowers/execution.md` [MODIFY]
  - `artifacts/superpowers/finish.md` [NEW]
- **What Changed:**
  - Marked Task 1.11 complete (`[x]`) in `specs/tasks.md`.
  - Executed static analysis across 136 files: 0 ruff errors, 0 mypy issues.
  - Executed full test suite: 354 passed in 22.29s (+31 tests: 26 unit in `test_thread_association.py` + 5 integration in `test_thread_postgres.py`).
  - Strict multi-tenant isolation, architectural boundaries, and schema integrity verified.
- **Verification Command:**
  - `uv run ruff check . && uv run mypy packages services tests evaluation && uv run pytest tests/unit/ tests/integration/`
- **Result:** PASS (354 passed, 0 lint/mypy issues)

## Step 1: Message & Attachment Persistence Stores (`packages/db/message.py`, `packages/db/__init__.py`)
- **Files Changed:**
  - `packages/db/message.py`
  - `packages/db/__init__.py`
- **What Changed:**
  - Implemented `MessageInsertResult` and `AttachmentRecord` data models.
  - Defined `MessageStore` protocol for atomic message and attachment operations.
  - Implemented `InMemoryMessageStore` for pure unit testing and isolated mocking.
  - Implemented `PostgresMessageStore` using `asyncpg` supporting:
    - Atomic `INSERT INTO email_message` with `ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO NOTHING`.
    - Write-time full-text search vector generation: `setweight(to_tsvector('english', coalesce(subject, '')), 'A') || setweight(to_tsvector('english', coalesce(body_text_clean, '')), 'B')`.
    - Same-transaction attachment metadata insertion into the `attachment` table.
    - Full-text search with `search_tsv @@ plainto_tsquery('english', ...)` and thread-scoped retrieval.
  - Exported components from `packages/db/__init__.py`.
- **Verification Command:**
  - `uv run ruff check packages/db/ && uv run mypy packages/db/`
- **Result:** PASS (15 source files checked cleanly)

## Step 2: Email Worker Persister Integration (`services/email_worker/persister.py`, `services/email_worker/__init__.py`)
- **Files Changed:**
  - `services/email_worker/persister.py`
  - `services/email_worker/threading.py`
  - `services/email_worker/__init__.py`
- **What Changed:**
  - Implemented `EmailPersistenceResult` dataclass recording operation status, deduplication state, resolved thread, and downstream dispatch eligibility flag (`should_dispatch`).
  - Implemented `EmailPersister` pipeline service coordinating:
    - Fast check against `MessageStore.get_message_by_provider_id` to exit early on replayed payloads.
    - Multi-tier thread association via `ThreadAssociator.associate_normalized_message`.
    - Atomic persistence of `email_message` and `attachment` records.
    - Duplicate detection via `ON CONFLICT DO NOTHING` with automatic suppression of downstream jobs (`should_dispatch=False` per R4.8).
  - Exported `EmailPersister` and `EmailPersistenceResult` in `services/email_worker/__init__.py`.
- **Verification Command:**
  - `uv run ruff check services/email_worker/ && uv run mypy services/email_worker/`
- **Result:** PASS (8 source files checked cleanly)

## Step 3: Unit Tests (`tests/unit/test_message_persistence.py`)
- **Files Changed:**
  - `tests/unit/test_message_persistence.py`
- **What Changed:**
  - Added unit test suite covering:
    - `test_in_memory_store_insert_and_get`: Validates message insertion, ID lookups, provider message ID index, and attachment records.
    - `test_in_memory_store_deduplication_on_conflict`: Validates idempotent rejection of duplicate `(organization_id, mailbox_id, provider_message_id)` with `inserted=False, is_duplicate=True`.
    - `test_in_memory_store_tenant_isolation`: Validates strict tenant isolation across organizations.
    - `test_in_memory_store_thread_messages_and_search`: Validates thread message ordering and in-memory full-text search.
    - `test_persister_pipeline_first_time_message`: Validates persister pipeline creating thread and returning `should_dispatch=True`.
    - `test_persister_pipeline_replayed_message_suppresses_dispatch`: Validates R4.8 suppression of duplicate jobs (`should_dispatch=False`) and preservation of thread counters.
- **Verification Command:**
  - `uv run ruff check tests/unit/test_message_persistence.py && uv run mypy tests/unit/test_message_persistence.py && uv run pytest tests/unit/test_message_persistence.py -v`
- **Result:** PASS (6 tests passed in 0.16s)

## Step 4: Multi-Tenant PostgreSQL Integration Tests (`tests/integration/test_message_postgres.py`)
- **Files Changed:**
  - `tests/integration/test_message_postgres.py`
- **What Changed:**
  - Added live PostgreSQL integration test suite:
    - `test_postgres_message_store_multi_tenant_isolation`: Validates 3 distinct tenants storing identical provider message IDs and subjects in total isolation per GEMINI.md §8 mandate.
    - `test_postgres_message_store_deduplication_on_conflict`: Validates `ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO NOTHING` suppressing replayed deliveries with `inserted=False, is_duplicate=True`.
    - `test_postgres_message_store_attachments_persistence`: Validates atomic transactional persistence of attachments in the `attachment` table with FK linking to `email_message.id`.
    - `test_postgres_message_store_search_tsv_gin`: Validates write-time full-text search vector generation with 'A' (subject) and 'B' (clean body) weights and tenant-isolated GIN queries.
    - `test_postgres_message_store_thread_ordered_messages`: Validates thread messages retrieved in chronological order (`received_at ASC`).
- **Verification Command:**
  - `uv run ruff check tests/integration/test_message_postgres.py && uv run mypy tests/integration/test_message_postgres.py && uv run pytest tests/integration/test_message_postgres.py -v`
- **Result:** PASS (5 integration tests passed in 1.47s)

## Step 1: Update Persister for Normalization Failures (`services/email_worker/persister.py`)
- **Files Changed:**
  - `services/email_worker/persister.py`
- **What Changed:**
  - Updated `EmailPersister.persist()` to explicitly check `message.normalization_failed`.
  - When `normalization_failed=True`, message is persisted in database (preserving `raw_object_key` per R4.9), and `should_dispatch` is returned as `False` to suppress downstream triage job emission.
- **Verification Command:**
  - `uv run ruff check services/email_worker/persister.py && uv run mypy services/email_worker/persister.py`
- **Result:** PASS

## Step 2: Implement Email Normalization Consumer (`services/email_worker/consumer.py`, `services/email_worker/__init__.py`)
- **Files Changed:**
  - `services/email_worker/consumer.py`
  - `services/email_worker/__init__.py`
- **What Changed:**
  - Implemented `EmailNormalizationConsumer(BaseConsumer)` to consume jobs from `email.normalize`.
  - Implemented `process_job`:
    - Fetches raw MIME payload from MinIO/S3 object storage via `storage_client.get_bytes`.
    - Normalizes payload via `EmailNormalizer.normalize_and_offload`.
    - Persists message and attachments using `EmailPersister.persist` with `ON CONFLICT DO NOTHING` (never discarding messages per R4.9).
    - If `normalization_failed=True`: raises `FatalError` causing `BaseConsumer` to route the job to `dlx.email` exchange preserving `x-original-routing-key`, `x-failure-reason`, `x-attempt` headers and acknowledging message.
    - If duplicate: returns early without dispatching.
    - If successful: emits downstream triage job envelope to `email.triage`.
  - Re-exported `EmailNormalizationConsumer` in `services/email_worker/__init__.py`.
- **Verification Command:**
  - `uv run ruff check services/email_worker/ && uv run mypy services/email_worker/`
- **Result:** PASS (9 source files checked cleanly)

## Step 3: Unit Tests for Normalization Failure Handling (`tests/unit/test_normalization_failure.py`)
- **Files Changed:**
  - `services/email_worker/normalizer.py`
  - `tests/unit/test_normalization_failure.py`
- **What Changed:**
  - Updated `EmailNormalizer.normalize()` to validate non-empty MIME payloads and detect total header absence, triggering fallback message generation with `normalization_failed=True` and `raw_object_key` retention per R4.9.
  - Added unit test suite covering:
    - `test_normalizer_corrupted_mime_produces_failed_message`: Validates corrupted non-email bytes produce `normalization_failed=True` while retaining `raw_object_key` and fallback text.
    - `test_persister_suppresses_dispatch_on_normalization_failed`: Validates `EmailPersister` stores failed message in DB (never discarding per R4.9) and suppresses downstream dispatch (`should_dispatch=False`).
    - `test_consumer_persists_and_raises_fatal_error_on_normalization_failure`: Validates `EmailNormalizationConsumer` persists failed message to store and raises `FatalError` for dead-letter routing, with zero triage jobs dispatched.
    - `test_consumer_success_path_publishes_triage_job`: Validates successful normalization dispatches triage job envelope to `email.triage`.
    - `test_consumer_missing_raw_key_raises_fatal_error`: Validates missing payload keys raise `FatalError`.
- **Verification Command:**
  - `uv run ruff check tests/unit/test_normalization_failure.py && uv run mypy tests/unit/test_normalization_failure.py && uv run pytest tests/unit/test_normalization_failure.py -v`
- **Result:** PASS (5 unit tests passed in 0.13s)

## Step 4: Live RabbitMQ + PostgreSQL Integration Tests (`tests/integration/test_normalization_failure_integration.py`)
- **Files Changed:**
  - `tests/integration/test_normalization_failure_integration.py`
- **What Changed:**
  - Implemented end-to-end integration test suite running against live PostgreSQL (port 5433) and RabbitMQ (port 5672) containers:
    - `test_normalization_failure_persists_and_dead_letters`: Ingests corrupted non-MIME binary payload, processes via `EmailNormalizationConsumer`, asserts PostgreSQL row is durably stored in `email_message` with `normalization_failed=True` and `raw_object_key` intact (R4.9). Asserts message is routed to `email.dead_letter` queue with `x-original-routing-key=email.normalize` and `x-failure-reason` in AMQP headers (R3.5). Confirms downstream `email.triage` queue receives 0 jobs.
    - `test_multi_tenant_normalization_mixed_scenarios`: Tests $\ge 3$ distinct tenants handling a mix of corrupted, valid, and replayed emails simultaneously. Validates complete tenant isolation, corrupt payload dead-lettering, and successful payload dispatch to `email.triage`.
- **Verification Command:**
  - `uv run ruff check tests/integration/test_normalization_failure_integration.py && uv run mypy tests/integration/test_normalization_failure_integration.py && uv run pytest tests/integration/test_normalization_failure_integration.py -v`
- **Result:** PASS (2 integration tests passed in 2.01s)

## Step 5: Full CI Verification & Task Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
- **What Changed:**
  - Executed complete CI validation gate (`make ci`):
    - `ruff format --check .`: 156 files verified clean.
    - `ruff check .`: 0 lint errors.
    - `mypy packages services tests evaluation`: 0 type errors across 143 source files.
    - `pytest tests/unit -v`: 320 unit tests passed cleanly.
    - `pytest tests/integration -v`: 52 integration tests passed against live PostgreSQL and RabbitMQ containers.
  - Marked Task 1.13 as completed (`[x]`) in `specs/tasks.md`.
- **Verification Command:**
  - `make ci`
- **Result:** PASS (372 total tests passing, 0 lint/format/type issues)

# Execution Log: Phase 1 Task 1.14 — Read API for Mail Data & Real Gmail Support

## Step 1: Database Store Enhancements for Read Operations (`packages/db/`)
- **Files Changed:**
  - `packages/db/mailbox.py`
  - `packages/db/thread.py`
- **What Changed:**
  - Added `list_mailboxes(organization_id, limit, offset, status, provider)` to `MailboxStore` protocol, `PostgresMailboxStore`, and `InMemoryMailboxStore` supporting pagination and tenant filtering.
  - Added `list_threads(organization_id, limit, offset, mailbox_id, status)` to `ThreadStore` protocol, `PostgresThreadStore`, and `InMemoryThreadStore` supporting pagination, ordering by `last_message_at DESC NULLS LAST`, and tenant filtering.
- **Verification Command:**
  - `uv run ruff check packages/db/ && uv run mypy packages/db/ && uv run pytest tests/unit/test_thread_association.py tests/integration/test_thread_postgres.py -v`
- **Result:** PASS (31 tests passed, 0 lint/type issues across 15 source files)

## Step 2: Real Gmail Credentials Reference Resolver & Webhook Bug Fix (`packages/adapters/`)
- **Files Changed:**
  - `packages/adapters/registry.py`
  - `packages/adapters/webhooks.py`
- **What Changed:**
  - Implemented `resolve_provider_credentials(credentials_ref, provider)` supporting `env:<VAR>`, `file:<PATH>`, raw token strings, and fallback to `GMAIL_ACCESS_TOKEN`.
  - Updated `get_adapter_for_mailbox` to resolve credentials and pass `access_token` to `GmailProviderAdapter` when instantiating real Gmail adapters.
  - Fixed database table name query bug in `packages/adapters/webhooks.py`: changed `FROM mailboxes` to `FROM mailbox` to match PostgreSQL schema.
- **Verification Command:**
  - `uv run ruff check packages/adapters/ && uv run mypy packages/adapters/ && uv run pytest tests/unit/test_adapter_registry.py tests/unit/test_gmail_adapter.py tests/unit/test_webhook_receivers.py -v`
- **Result:** PASS (36 tests passed, 0 lint/type issues across 9 source files)

## Step 3: Read API Schemas, Dependencies & Routers (`services/api/`)
- **Files Changed:**
  - `services/api/schemas/threads.py`
  - `services/api/schemas/messages.py`
  - `services/api/schemas/__init__.py`
  - `services/api/dependencies.py`
  - `services/api/routers/mailboxes.py`
  - `services/api/routers/threads.py`
  - `services/api/routers/messages.py`
  - `services/api/routers/v1.py`
- **What Changed:**
  - Created Pydantic V2 schemas for threads (`ThreadSummaryResponse`, `ThreadDetailResponse`, `MessageSummaryInThread`) and messages (`EmailAddressResponse`, `AttachmentSummaryResponse`, `MessageDetailResponse`).
  - Added `ThreadStoreDep`, `MessageStoreDep`, and `StorageClientDep` dependencies to `services/api/dependencies.py`.
  - Implemented `GET /v1/mailboxes`: paginated list with optional `status` and `provider` filters, scoped by `organization_id`.
  - Implemented `GET /v1/threads`: paginated list of threads with optional `mailbox_id` and `status` filters, scoped by `organization_id`.
  - Implemented `GET /v1/threads/{id}`: detailed view including chronological thread messages.
  - Implemented `GET /v1/messages/{id}`: detailed view with recipient lists, attachments metadata, clean text, and presigned download URLs.
  - Mounted `thread_router` and `message_router` under `/v1` in `services/api/routers/v1.py`.
- **Verification Command:**
  - `uv run ruff check services/api/ && uv run mypy services/api/ && uv run python -m services.api.openapi --check && uv run pytest tests/unit/test_api_skeleton.py tests/unit/test_api_resync.py -v`
- **Result:** PASS (OpenAPI schema valid with 15 paths, 29 tests passed, 0 lint/type issues)

## Step 4: Pure Unit Tests for Mail Read API & Credentials (`tests/unit/test_mail_read_api.py`)
- **Files Changed:**
  - `tests/unit/test_mail_read_api.py`
- **What Changed:**
  - Implemented unit test suite covering:
    - `test_list_mailboxes_paginated_and_filtered`: validates tenant isolation, pagination limits/offsets, provider filters, and status filters for `GET /v1/mailboxes`.
    - `test_list_threads_paginated_and_ordered`: validates tenant scoping, recency ordering (`last_message_at DESC`), and pagination for `GET /v1/threads`.
    - `test_get_thread_detail_with_chronological_messages`: validates thread metadata, ordered messages (`received_at ASC`), and 404 on cross-tenant requests.
    - `test_get_message_detail_with_attachments`: validates recipient resolution, attachment metadata, presigned download URLs, and cross-tenant 404 rejection.
    - `test_resolve_provider_credentials_and_adapter_instantiation`: validates `env:VAR`, raw OAuth tokens, and environment fallbacks passing `access_token` to `GmailProviderAdapter`.
- **Verification Command:**
  - `uv run ruff check tests/unit/test_mail_read_api.py && uv run mypy tests/unit/test_mail_read_api.py && uv run pytest tests/unit/test_mail_read_api.py -v`
- **Result:** PASS (5 tests passed in 0.83s, 0 lint/type issues)

## Step 5: Multi-Tenant PostgreSQL Integration Tests (`tests/integration/test_mail_read_api_integration.py`)
- **Files Changed:**
  - `tests/integration/test_mail_read_api_integration.py`
- **What Changed:**
  - Implemented multi-tenant integration test suite against live PostgreSQL container conforming to GEMINI.md §8:
    - `test_mail_read_api_multi_tenant_isolation`: seeds 3 distinct tenants (`org1`, `org2`, `org3`) with overlapping provider IDs and normalized subjects, verifying strict cross-tenant 404 rejection on threads and messages, and tenant isolation on mailbox/thread listings.
    - `test_mail_read_api_pagination_and_filters`: tests database pagination (`limit`, `offset`), filtering by `provider`, `status`, and `mailbox_id`, recency ordering (`last_message_at DESC NULLS LAST`), and chronological message ordering (`received_at ASC`).
- **Verification Command:**
  - `uv run ruff check tests/integration/test_mail_read_api_integration.py && uv run mypy tests/integration/test_mail_read_api_integration.py && uv run pytest tests/integration/test_mail_read_api_integration.py -v`
- **Result:** PASS (2 integration tests passed in 1.02s, 0 lint/type issues)

## Step 6: Phase 1 End-to-End Pipeline & Real Gmail Integration Test (`tests/integration/test_phase1_pipeline_e2e.py`)
- **Files Changed:**
  - `tests/integration/test_phase1_pipeline_e2e.py`
- **What Changed:**
  - Implemented end-to-end integration test suite exercising the complete Phase 1 pipeline across all active infrastructure components:
    - `test_real_gmail_mailbox_adapter_capability`: tests real Gmail mailbox credentials resolution from `credentials_ref` (`env:VAR` and direct OAuth tokens), instantiating `GmailProviderAdapter` configured for `https://gmail.googleapis.com` without mock doubles.
    - `test_phase1_pipeline_full_e2e`: runs full lifecycle: Ingest -> MinIO (`raw-emails` bucket) -> checkpoint advancement in PostgreSQL -> RabbitMQ (`email.normalize` queue) -> `EmailNormalizationConsumer` -> MIME normalization & attachment offload to MinIO -> Thread association & message insertion in PostgreSQL (`email_message`, `email_thread`) -> RabbitMQ downstream triage job dispatch (`email.triage` queue) -> Read API verification via HTTP (`GET /v1/mailboxes`, `GET /v1/threads`, `GET /v1/threads/{id}`, `GET /v1/messages/{id}`) -> Idempotency duplicate suppression check.
- **Verification Command:**
  - `uv run ruff check tests/integration/test_phase1_pipeline_e2e.py && uv run mypy tests/integration/test_phase1_pipeline_e2e.py && uv run pytest tests/integration/test_phase1_pipeline_e2e.py -v`
- **Result:** PASS (2 integration tests passed in 0.98s, 0 lint/type issues)

## Step 7: Full CI Verification Gate & Task Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
- **What Changed:**
  - Executed full formatting, linting, type-checking, unit test suite, and live integration test suite via `make ci`.
  - Verified 381 automated tests (325 unit + 56 integration) passed with zero regressions.
  - Marked Task 1.14 as completed (`[x]`) in `specs/tasks.md`, completing Phase 1: Core Mail Pipeline.
- **Verification Command:**
  - `make ci`
- **Result:** PASS (163 files formatted, 150 source files checked by mypy with 0 errors, 381 tests passed in 27.82s)



---

# Execution Log: Phase 2 Task 2.1 — Job Envelope & Publication

## Step 1: Job Store Protocol and Backends (packages/db/job.py, packages/db/__init__.py)
- **Files Changed:**
  - packages/db/job.py (new)
  - packages/db/__init__.py (modified)
- **What Changed:**
  - Implemented JobStore protocol, PostgresJobStore, and InMemoryJobStore.
  - Implemented create_job() creating processing_job rows with initial state RECEIVED (R18.1), handling idempotency conflict via ON CONFLICT (idempotency_key) DO NOTHING (R19.4), and recording initial processing_event in the same transaction (R18.4, R18.5).
  - Implemented transition_job_state() validating legal transitions via packages/domain/state_machine.py (R18.3), and atomically updating processing_job and inserting processing_event in the same transaction (R18.4, R18.5).
  - Implemented get_job(), get_job_by_idempotency_key(), list_events_for_message(), and list_events_for_job() with mandatory tenant scoping (organization_id).
  - Exported JobStore, PostgresJobStore, and InMemoryJobStore from packages/db.
- **Verification Command:**
  - uv run ruff check packages/db/ && uv run mypy packages/db/
- **Result:** PASS (0 lint issues, 16 source files checked with 0 errors)

## Step 2: Job Envelope Specification Refinement (packages/broker/envelope.py)
- **Files Changed:**
  - packages/broker/envelope.py (modified)
- **What Changed:**
  - Refined JobEnvelope strictly matching specs/design.md §7.3 and R7.3.
  - Added classification snapshot helper methods set_classification() and with_classification() supporting dict, dataclasses, and Pydantic models.
  - Exposed convenience snapshot accessors category, priority, reply_required, workflow_hint, and retrieval_required.
  - Added AMQP header routing tags (category, priority) and trace context propagation to to_message().
- **Verification Command:**
  - uv run ruff check packages/broker/ && uv run mypy packages/broker/
- **Result:** PASS (0 lint issues, 5 source files checked with 0 errors)

## Step 3: Mail Ingestion Job Creation (services/mail_connector/orchestrator.py)
- **Files Changed:**
  - services/mail_connector/orchestrator.py (modified)
- **What Changed:**
  - Added optional JobStore injection to SyncOrchestrator.__init__().
  - Integrated deterministic idempotency key derivation (R19.2) and Job creation in state RECEIVED (R18.1).
  - Persisted processing_job row and initial processing_event telemetry prior to publishing the AMQP message.
  - Linked the generated/persisted job ID into JobEnvelope.job_id and envelope payload ensuring end-to-end trace correlation.
- **Verification Command:**
  - uv run ruff check services/mail_connector/ && uv run mypy services/mail_connector/ && uv run pytest tests/unit/test_sync_orchestrator.py -v
- **Result:** PASS (8 unit tests passed, 0 lint/type issues)

## Step 4: Downstream Normalization State Transition (services/email_worker/consumer.py)
- **Files Changed:**
  - services/email_worker/consumer.py (modified)
  - services/email_worker/main.py (modified)
- **What Changed:**
  - Added optional JobStore injection to EmailNormalizationConsumer.__init__() and wired PostgresJobStore in services/email_worker/main.py.
  - Implemented atomic state transition from RECEIVED to NORMALIZED on successful normalization and database persistence (R18.1, R18.4, R18.5).
  - Implemented state transition from RECEIVED to FAILED on MIME normalization errors (R4.9, R3.5).
  - Forwarded job_id from JobEnvelope into downstream triage envelope, preserving full pipeline job correlation.
- **Verification Command:**
  - uv run ruff check services/email_worker/ && uv run mypy services/email_worker/ && uv run pytest tests/unit/test_email_normalization.py tests/unit/test_normalization_failure.py -v
- **Result:** PASS (35 unit tests passed, 0 lint/type issues)

## Step 5: Pure Unit Tests for Job Envelope, Store & State Transitions (tests/unit/test_job_envelope_and_store.py)
- **Files Changed:**
  - tests/unit/test_job_envelope_and_store.py (new)
- **What Changed:**
  - Added test_job_envelope_serialization_and_classification_snapshot: tests JobEnvelope properties, Classification entity attachment (R7.3), AMQP persistent headers, and round-trip deserialization.
  - Added test_in_memory_job_store_creation_and_idempotency: verifies Job insertion in state RECEIVED (R18.1), initial ProcessingEvent recording (R18.4), and idempotent deduplication returning existing job without duplicate events (R19.2, R19.4).
  - Added test_job_store_state_machine_transitions: tests legal state progression (RECEIVED -> NORMALIZED -> CLASSIFIED -> QUEUED) and asserts IllegalStateTransitionError is raised on undeclared state jumps (R18.3).
  - Added test_sync_orchestrator_creates_job_in_received_state: verifies SyncOrchestrator creates a Job in state RECEIVED at ingestion and sets JobEnvelope.job_id = str(job.id).
  - Added test_email_normalization_consumer_transitions_to_normalized: verifies consumer transitions job from RECEIVED to NORMALIZED, sets message_id/thread_id, emits chronological ProcessingEvents, and forwards job_id into downstream triage envelope.
- **Verification Command:**
  - uv run ruff check tests/unit/test_job_envelope_and_store.py && uv run mypy tests/unit/test_job_envelope_and_store.py && uv run pytest tests/unit/test_job_envelope_and_store.py -v
- **Result:** PASS (5 unit tests passed in 0.56s, 0 lint/type issues; 330/330 total unit tests passing)

## Step 6: Multi-Tenant PostgreSQL & Broker Integration Tests (tests/integration/test_job_publication_integration.py)
- **Files Changed:**
  - `tests/integration/test_job_publication_integration.py` (new)
- **What Changed:**
  - Implemented `test_job_persistence_and_event_recording_multi_tenant`: seeds 3 distinct organizations with overlapping provider message IDs, creates jobs in `RECEIVED` state via `PostgresJobStore`, and verifies `processing_job` and initial `processing_event` rows are atomically committed in the same transaction (R18.1, R18.4, R18.5).
  - Verified tenant isolation: queries with foreign `organization_id` return `None`.
  - Implemented `test_job_store_idempotency_conflict`: verifies concurrent or duplicate job ingestion with identical `idempotency_key` returns `is_new=False` without inserting duplicate records or events (R19.2, R19.4).
  - Implemented `test_job_publication_and_retrieval_with_classification_snapshot`: verifies RabbitMQ persistent publication of `JobEnvelope` with classification snapshot, headers, correlation IDs, and retrieval from queue matching specifications (R7.3).
- **Verification Command:**
  - `uv run pytest tests/integration/test_job_publication_integration.py -v`
- **Result:** PASS (3 tests passed in 1.16s, 0 lint/type issues)

## Step 7: Full Verification Gate & Task Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
- **What Changed:**
  - Executed full linting (`ruff`), type checking (`mypy`), unit test suite (330 tests), and live integration test suite (59 tests).
  - Verified 389 automated tests passed cleanly with 0 regressions.
  - Marked Task 2.1 as completed (`[x]`) in `specs/tasks.md`.
- **Verification Command:**
  - `uv run ruff check packages/ services/ tests/ && uv run mypy packages/ services/ tests/ && uv run pytest tests/unit/ -q && uv run pytest tests/integration/ -q`
- **Result:** PASS (All lint & type checks passed, 330 unit tests passed, 59 integration tests passed)


---

# Execution Log: Phase 2 Task 2.2 — Rule Engine (Triage Stage 1)

## Step 1: Header Capture in Email Normalization (packages/domain/entities.py, services/email_worker/)
- **Files Changed:**
  - `packages/domain/entities.py` (modified)
  - `services/email_worker/parser.py` (modified)
  - `services/email_worker/normalizer.py` (modified)
- **What Changed:**
  - Added `headers: dict[str, str] = field(default_factory=dict)` to `NormalizedMessage` and `ParsedHeaders`.
  - In `parser.py`, captured all RFC 822 MIME headers into a case-insensitive dictionary with lowercase keys, decoding encoded words while preserving headers such as `Auto-Submitted`, `List-Unsubscribe`, and `Precedence`.
  - Forwarded extracted headers through `EmailNormalizer.normalize()` into `NormalizedMessage.headers`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_email_normalization.py -v`
- **Result:** PASS (30/30 unit tests passed in 0.30s)

## Step 2: Pure Domain Rule Engine (packages/domain/rules.py, packages/domain/__init__.py)
- **Files Changed:**
  - `packages/domain/rules.py` (new)
  - `packages/domain/__init__.py` (modified)
- **What Changed:**
  - Implemented pure domain rule engine models and evaluators: `EmailContext`, `FieldPredicate`, `CompositeCondition`, `RuleAction`, `Rule`, and `RuleEngine`.
  - Implemented field-level condition evaluation for headers (`header.<name>`), sender email/name, subject, normalized subject, clean body text, recipients, CC, and attachments.
  - Implemented operators: `exists`, `equals`, `contains`, `starts_with`, `ends_with`, pre-compiled `matches` regex, and boolean composites `any`, `all`, `not`.
  - Implemented `RuleEngine.evaluate()` producing `Classification` with `decided_by='rule'`, latency in milliseconds, and audit rule payload, or `None` on fall-through (R6.1, R6.8).
  - Preserved strict domain package boundary (stdlib-only imports in `packages/domain`).
- **Verification Command:**
  - `uv run ruff check packages/domain/ && uv run mypy packages/domain/ && uv run pytest tests/unit/test_dependency_rules.py -v`
- **Result:** PASS (0 lint issues, 4 source files checked by mypy with 0 errors, 4 boundary tests passed)

## Step 3: Hot-Reloadable Engine & YAML Loader (services/triage_worker/rules.py, services/triage_worker/__init__.py)
- **Files Changed:**
  - `services/triage_worker/rules.py` (new)
  - `services/triage_worker/__init__.py` (modified)
- **What Changed:**
  - Implemented `load_rules_from_yaml()` and `load_rules_from_file()` using PyYAML `yaml.safe_load`.
  - Implemented `HotReloadableRuleEngine` wrapping the pure domain `RuleEngine`.
  - Implemented `mtime`-based dynamic hot-reloading on access and explicit `reload(force=True)`.
  - Added fail-safe error isolation: malformed YAML or invalid regex patterns log error warnings and gracefully retain the active ruleset without crashing.
  - Exported `HotReloadableRuleEngine`, `load_rules_from_file`, and `load_rules_from_yaml` from `services/triage_worker`.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/ && uv run pytest tests/unit/test_dependency_rules.py -v`
- **Result:** PASS (0 lint issues, 2 source files checked by mypy with 0 errors, 4 boundary tests passed)

## Step 4: Author Declarative Ruleset & Settings Integration (config/triage_rules.yaml, packages/core/settings.py, .env.example, docs/configuration.md)
- **Files Changed:**
  - `config/triage_rules.yaml` (new)
  - `packages/core/settings.py` (modified)
  - `.env.example` (modified)
  - `docs/configuration.md` (modified)
- **What Changed:**
  - Authored authoritative declarative triage rules in `config/triage_rules.yaml` defining 10 rules: `auto-submitted`, `list-unsubscribe`, `precedence-bulk`, `no-reply-sender`, `out-of-office`, `delivery-status-notification`, `invoice-reference`, `urgent-billing`, `calendar-invite`, and `receipt-acknowledgement`.
  - Added `rules_path: str = Field(default="config/triage_rules.yaml")` to `TriageSettings` in `packages/core/settings.py`.
  - Documented `TRIAGE__RULES_PATH` in `.env.example` and `docs/configuration.md` conforming to Definition of Done §3.4.
  - Verified loading 10 compiled rules from `config/triage_rules.yaml`.
- **Verification Command:**
  - `uv run python -c "from packages.core.settings import AppSettings; s = AppSettings(); assert s.triage.rules_path == 'config/triage_rules.yaml'" && uv run pytest tests/unit/test_settings.py -v`
- **Result:** PASS (9/9 settings unit tests passed, rules_path verified)

## Step 5: Fixture Email Regression Suite (tests/fixtures/triage/*.eml)
- **Files Changed:**
  - `tests/fixtures/triage/01_auto_submitted.eml` (new)
  - `tests/fixtures/triage/02_newsletter.eml` (new)
  - `tests/fixtures/triage/03_no_reply.eml` (new)
  - `tests/fixtures/triage/04_out_of_office.eml` (new)
  - `tests/fixtures/triage/05_delivery_status_notification.eml` (new)
  - `tests/fixtures/triage/06_invoice_inquiry.eml` (new)
  - `tests/fixtures/triage/07_urgent_billing.eml` (new)
  - `tests/fixtures/triage/08_calendar_invite.eml` (new)
  - `tests/fixtures/triage/09_actionable_support.eml` (new)
- **What Changed:**
  - Created 9 realistic RFC 822 MIME fixture emails matching real-world enterprise patterns.
  - Fixtures cover Auto-Submitted headers, List-Unsubscribe headers, robot sender domains, out-of-office autoreplies, DSN bounce notices, invoice inquiries (`INV-YYYY-NNNNN`), urgent collections notices, calendar meeting invitations, and an actionable support inquiry designed to test fall-through behavior.
- **Verification Command:**
  - `python3 -c "import os; files = sorted(os.listdir('tests/fixtures/triage')); assert len(files) == 9"`
- **Result:** PASS (9 fixture emails verified)

## Step 6: Comprehensive Unit Tests & Benchmarks (tests/unit/test_rule_engine.py)
- **Files Changed:**
  - `tests/unit/test_rule_engine.py` (new)
- **What Changed:**
  - Implemented unit tests validating field predicates (headers, sender, subject, body, attachments) and operators (exists, equals, contains, starts_with, ends_with, regex matches).
  - Implemented composite condition tests (`any`, `all`, `not`).
  - Validated `RuleAction` and `Classification` output contracts (`decided_by='rule'`, category, intent, priority, flags, latency).
  - Validated dynamic hot-reloading: file updates on disk, `mtime` detection, automatic compilation, and fail-safe recovery on malformed YAML edits without crashing.
  - Executed regression suite over all 9 fixture emails in `tests/fixtures/triage/` asserting expected categories, flags, and fall-through with `None` for actionable inquiries.
  - Benchmarked evaluation execution: verified average latency well under the 2ms budget (<0.1ms).
- **Verification Command:**
  - `uv run ruff check tests/unit/test_rule_engine.py && uv run mypy tests/unit/test_rule_engine.py && uv run pytest tests/unit/test_rule_engine.py -v`
- **Result:** PASS (0 lint issues, 0 type issues, 6/6 tests passed in 0.61s)

# Execution Log: Phase 2 Task 2.3 — Lightweight ML Classifier (Triage Stage 2)

## Step 1: Add scikit-learn dependency & update environment
- **Files Changed:**
  - `pyproject.toml`
  - `uv.lock`
- **What Changed:**
  - Added `scikit-learn>=1.4.0` to project dependencies in `pyproject.toml`.
  - Synced virtualenv via `uv sync`, installing `scikit-learn 1.9.1`, `joblib 1.6.0`, `scipy 1.18.1`, `numpy 2.5.3`.
- **Verification Command:**
  - `uv run python -c "import sklearn; print(sklearn.__version__)"`
- **Result:** PASS (1.9.1 printed)
## Step 2: Add ML model configuration settings
- **Files Changed:**
  - `packages/core/settings.py`
  - `.env.example`
  - `docs/configuration.md`
- **What Changed:**
  - Added `ml_model_path: str = Field(default="artifacts/models/triage_ml_v1.joblib", description="Path to trained ML classifier model artifact (R6.1)")` to `TriageSettings`.
  - Added `TRIAGE__ML_MODEL_PATH=artifacts/models/triage_ml_v1.joblib` to `.env.example`.
  - Documented `TRIAGE__ML_MODEL_PATH` in `docs/configuration.md`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_settings.py -v && uv run ruff check packages/core/settings.py && uv run mypy packages/core/settings.py`
- **Result:** PASS (9/9 settings tests passed, 0 lint issues, 0 mypy issues)

## Step 3: Implement training & evaluation pipeline
- **Files Changed:**
  - `services/triage_worker/training.py` (new)
  - `artifacts/models/triage_ml_v1.joblib` (new)
  - `artifacts/models/triage_ml_v1_metrics.json` (new)
- **What Changed:**
  - Implemented `train_triage_model` and `evaluate_model` using scikit-learn `TfidfVectorizer` (sublinear TF, n-grams 1-2) + `LogisticRegression` (balanced class weights, L-BFGS solver).
  - Trained on 252 items from `evaluation/datasets/classification/train.jsonl` and evaluated on 64 held-out items from `test.jsonl`.
  - Achieved 100% Accuracy and 100% Macro-F1 across all 9 canonical categories (`R6.4`).
  - Serialized model artifact with metadata (git SHA, timestamp, classes) to `artifacts/models/triage_ml_v1.joblib` and metrics JSON to `artifacts/models/triage_ml_v1_metrics.json` (`R22.1, R22.12`).
- **Verification Command:**
  - `uv run ruff check services/triage_worker/training.py && uv run mypy services/triage_worker/training.py && uv run python -m services.triage_worker.training`
- **Result:** PASS (0 lint issues, 0 type issues, trained in 1.77s, 100% macro-F1 on test set)

## Step 4: Implement MLClassifier inference engine
- **Files Changed:**
  - `services/triage_worker/classifier.py` (new)
- **What Changed:**
  - Implemented `MLClassifier` with `load_from_artifact` and `classify(context)` adhering to `design.md §5.3` and `R6.1, NFR3`.
  - Executes feature extraction, prediction, and calibrated probability calculation.
  - Emits full `Classification` entity: category, confidence, priority (detecting urgency cues), reply requirement, workflow hint, knowledge retrieval requirement, `decided_by='ml'`, and full class probability distribution in `raw`.
  - Tuned `C=50.0` in training pipeline to produce well-calibrated confidence scores that clear the `TRIAGE__ML_CONFIDENCE_THRESHOLD=0.80` threshold on confident samples while leaving ambiguous queries for Stage 3 LLM fallback.
  - Verified measured inference latency of 8–10 ms, comfortably below the 20–50 ms NFR3 budget.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`
- **Result:** PASS (0 lint issues, 0 mypy issues across all 4 files in services/triage_worker)

## Step 5: Author comprehensive automated test suite
- **Files Changed:**
  - `tests/unit/test_triage_ml.py` (new)
- **What Changed:**
  - Authored 18 comprehensive automated unit tests covering:
    - Model artifact loading, version/name inspection, missing file handling, corrupted artifact detection.
    - Structured `Classification` contract compliance (`decided_by='ml'`, calibrated probabilities summing to 1.0, latency measurement).
    - Multi-type context input coercion (`EmailContext`, `NormalizedMessage`, `dict`, and rejection of invalid types).
    - Priority determination (urgent regex matching) and reply/retrieval gates.
    - Automated notification and acknowledgement template gates.
    - 50-iteration inference latency benchmarking asserting mean latency < 20 ms and p95 < 50 ms (`NFR3`).
    - Held-out evaluation metrics computation (`R22.1`: accuracy, precision, recall, macro-F1, confusion matrix shape 9x9).
    - Edge cases: empty subject/body, massive 50,000-word payload, and foreign Unicode / emojis.
- **Verification Command:**
  - `uv run pytest tests/unit/test_triage_ml.py -v && uv run pytest tests/unit -v && uv run pytest tests/integration -v`
- **Result:** PASS (18/18 new triage ML tests passed; full test suite: 354 unit + 59 integration = 413 passed, 0 failures)

# Execution Log: Phase 2 Task 2.4 — Small-LLM Fallback (Triage Stage 3)

## Step 1: Define LLMProvider protocol and types
- **Files Changed:**
  - `packages/llm/protocol.py` (new)
  - `packages/llm/__init__.py`
- **What Changed:**
  - Defined `ModelTier` enum (`FAST`, `ROUTINE`, `STRONG`, `HIGH_CAPABILITY`, `FALLBACK`) per `R15` and `design.md §5.7`.
  - Defined `ChatMessage` dataclass representing conversational messages.
  - Defined `LLMResult` dataclass carrying schema-validated content, model ID, tier, token counts, and latency.
  - Defined `LLMProvider` runtime-checkable protocol with `async def generate(self, *, messages, schema, tier, max_tokens, temperature) -> LLMResult`.
  - Exported core types in `packages/llm/__init__.py`.
- **Verification Command:**
  - `uv run ruff check packages/llm/ && uv run mypy packages/llm/`
- **Result:** PASS (0 lint errors, 0 mypy errors)

## Step 2: Implement FakeLLMProvider for offline testing
- **Files Changed:**
  - `packages/llm/fake.py` (new)
  - `packages/llm/__init__.py`
- **What Changed:**
  - Implemented `FakeLLMProvider` complying with `LLMProvider` protocol (GEMINI.md §8).
  - Supports configurable default responses, FIFO queued canned responses, dynamic responder callbacks, failure injection, latency simulation, and call inspection (`recorded_calls`).
- **Verification Command:**
  - `uv run ruff check packages/llm/ && uv run mypy packages/llm/`
- **Result:** PASS (0 lint errors, 0 mypy errors)

## Step 3: Implement HttpLLMProvider (OpenAI-compatible)
- **Files Changed:**
  - `packages/llm/client.py` (new)
  - `packages/llm/__init__.py`
  - `packages/llm/protocol.py`
- **What Changed:**
  - Implemented `HttpLLMProvider` using `httpx.AsyncClient` supporting OpenAI / LiteLLM-compatible `/chat/completions` API.
  - Implemented tier-to-model resolution (`FAST` / `ROUTINE` -> `gpt-4o-mini`, `STRONG` / `HIGH_CAPABILITY` -> `gpt-4o`, `FALLBACK` -> `claude-3-haiku`).
  - Added structured JSON schema output support via `response_format` and JSON validation.
  - Added latency measurement, token usage extraction, and error hierarchy (`LLMError`, `LLMTimeoutError`, `LLMResponseError`, `LLMSchemaValidationError`).
- **Verification Command:**
  - `uv run ruff check packages/llm/ && uv run mypy packages/llm/ && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS (0 lint errors, 0 mypy errors, all dependency boundaries pass)

## Step 4: Implement Stage 3 LLMTriageClassifier
- **Files Changed:**
  - `services/triage_worker/llm_classifier.py` (new)
  - `services/triage_worker/__init__.py`
- **What Changed:**
  - Defined Pydantic schema `LLMTriageOutput` enforcing the 9 canonical categories, priority cues, reply_required, workflow_hint (`ai`, `template`, `none`), retrieval_required, and confidence bounds.
  - Authored concise system prompt and `prepare_triage_prompt(ctx)` handling body truncation to 2000 chars and header extraction (`Auto-Submitted`, `List-Unsubscribe`).
  - Implemented `LLMTriageClassifier` with `classify(context)` and `classify_sync(context)` mapping outputs to domain `Classification` entity with `decided_by='llm'`.
  - Implemented `safe_default(error_message)` helper meeting R6.11 safe fallback with review flag.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`
- **Result:** PASS (0 lint errors, 0 mypy errors)

## Step 5: Author comprehensive automated test suite
- **Files Changed:**
  - `tests/unit/test_llm_provider.py` (new)
  - `tests/unit/test_triage_stage3.py` (new)
- **What Changed:**
  - Created `tests/unit/test_llm_provider.py` testing protocol conformance, FakeLLMProvider mock behaviors, and HttpLLMProvider with `httpx.MockTransport` covering success, timeout, HTTP 429, and invalid JSON.
  - Created `tests/unit/test_triage_stage3.py` testing `LLMTriageOutput` validation, category synonym normalization, prompt formatting, end-to-end `LLMTriageClassifier` async and sync invocation, multi-type context coercion, exception handling, and safe default fallback.
- **Verification Command:**
  - `uv run pytest tests/unit/test_llm_provider.py tests/unit/test_triage_stage3.py -v && uv run pytest tests/unit tests/integration -q`
- **Result:** PASS (23/23 new tests passed; full test suite: 436 tests passed, 0 failures)

## Step 6: Mark Task Complete
- **Files Changed:**
  - `specs/tasks.md`
- **What Changed:**
  - Marked Task 2.4 complete (`[x]`).
- **Result:** PASS

# Execution Log: Phase 2 Task 2.5 — Cascade Orchestration & Thresholds

## Step 1: Implement Classification Database Store
- **Files Changed:**
  - `packages/db/classification.py` (new)
  - `packages/db/__init__.py`
- **What Changed:**
  - Defined `ClassificationResultRow` dataclass matching table `classification_result` with conversions to/from pure domain `Classification` entities.
  - Defined runtime-checkable `ClassificationStore` Protocol (`save_classification`, `get_classification`, `get_latest_classification_by_message`, `list_classifications_by_message`).
  - Implemented `PostgresClassificationStore` using parameterized queries with strict multi-tenant `organization_id` scoping per R5.3 and R6.7.
  - Implemented `InMemoryClassificationStore` for hermetic offline testing per GEMINI.md §8.
  - Exported all symbols from `packages.db`.
- **Verification Command:**
  - `uv run ruff check packages/db/ && uv run mypy packages/db/ && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS (0 lint errors, 0 mypy errors, all dependency boundary rules passed)

## Step 2: Implement Configurable Threshold Manager
- **Files Changed:**
  - `services/triage_worker/thresholds.py` (new)
  - `services/triage_worker/__init__.py`
- **What Changed:**
  - Implemented `ThresholdManager` providing hierarchical confidence threshold lookups with strict precedence: `Org + Category > Org default > Global Category > Global default` (`R6.9`).
  - Added runtime re-configuration methods (`set_organization_threshold`, `set_organization_category_threshold`, `set_global_category_threshold`, `load_organization_settings`) allowing per-org and per-category tuning without redeploying code.
  - Added validation ensuring thresholds fall strictly within `[0.0, 1.0]` and restricting stages to `rule`, `ml`, and `llm`.
  - Exported `ThresholdManager` and `OrganizationThresholdOverrides` in `services.triage_worker`.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`
- **Result:** PASS (0 lint errors, 0 mypy errors)

## Step 3: Implement Cascading Triage Engine
- **Files Changed:**
  - `services/triage_worker/cascade.py` (new)
  - `services/triage_worker/__init__.py`
- **What Changed:**
  - Implemented `CascadingTriageEngine` orchestrating Stage 1 (Deterministic Rules) -> Stage 2 (Lightweight ML) -> Stage 3 (Small LLM) per R6.1.
  - Enforced strict early short-circuiting: when any stage meets its confidence threshold, downstream stages are never executed (R6.2).
  - Implemented safe default fallback (`category='general_inquiry'`, `priority='normal'`, `reply_required=True`, `confidence=0.0`, `decided_by='default'`) with `review_flag=True` if all stages abstain or fail (R6.11).
  - Wired optional persistence to `ClassificationStore` saving stage, latency, model, and raw output (R6.7).
  - Added `StageExecutionRecord` telemetry capturing full audit trail across all evaluated stages.
  - Exported `CascadingTriageEngine`, `CascadeResult`, and `StageExecutionRecord` in `services.triage_worker`.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/ && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS (0 lint errors, 0 mypy errors, all boundary tests passed)

## Step 4: Author Cascade Unit Test Suite
- **Files Changed:**
  - `tests/unit/test_triage_cascade.py` (new)
- **What Changed:**
  - Authored 12 automated unit tests validating:
    - Stage 1 rule short-circuiting ensuring ML and LLM are never invoked (R6.2).
    - Stage 2 ML short-circuiting on low rule confidence ensuring LLM is never called.
    - Stage 3 LLM fallback invocation when rules and ML abstain or miss thresholds.
    - Safe default assignment (`category='general_inquiry'`, `priority='normal'`, `reply_required=True`, `confidence=0.0`, `decided_by='default'`, `review_flag=True`) with `stages_attempted` telemetry when all stages fail (R6.11).
    - Hierarchical threshold resolution precedence (`Org+Category > Org default > Global Category > Global default`) and dynamic JSONB settings loading without restart (R6.9).
    - Audit trail and persistence to `InMemoryClassificationStore` (R6.7).
    - Synchronous wrapper `triage_sync` and invalid input rejection (`TypeError`).
- **Verification Command:**
  - `uv run pytest tests/unit/test_triage_cascade.py -v`
- **Result:** PASS (12/12 passed in 2.63s)

## Step 5: Author PostgreSQL Integration Test Suite
- **Files Changed:**
  - `tests/integration/test_classification_store_postgres.py` (new)
- **What Changed:**
  - Authored 4 integration tests against live PostgreSQL container on port 5433 verifying:
    - End-to-end insertion and retrieval of classification results in `classification_result` table.
    - Tenant isolation ensuring queries with different `organization_id` return None (R5.3).
    - Latest classification resolution and chronological listing for multi-attempt messages.
    - Database CHECK constraint verification on `decided_by IN ('rule', 'ml', 'llm', 'default')`.
- **Verification Command:**
  - `uv run pytest tests/integration/test_classification_store_postgres.py -v`
- **Result:** PASS (4/4 passed in 1.25s)

## Step 6: Full Regression Verification & Task 2.5 Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
  - `artifacts/superpowers/finish.md`
- **What Changed:**
  - Validated full test suite (452 unit and integration tests passing).
  - Validated strict static type checking with mypy and code formatting with ruff.
  - Validated architectural boundary rules (AST dependency rules).
  - Marked Task 2.5 complete in `specs/tasks.md`.
- **Verification Command:**
  - `uv run pytest tests/unit tests/integration -q && uv run ruff check . && uv run mypy packages services tests`
- **Result:** PASS (452 passed, 0 lint errors, 0 type errors)

# Execution Log: Phase 2 Task 2.6 — Category Taxonomy

## Step 1: Implement Domain Category Taxonomy
- **Files Changed:**
  - `packages/domain/taxonomy.py` (new)
  - `packages/domain/__init__.py`
- **What Changed:**
  - Implemented `Category(StrEnum)` defining the 9 mandatory categories per R6.4: `support`, `sales`, `billing`, `administration`, `scheduling`, `general_inquiry`, `automated_notification`, `acknowledgement`, `no_response`.
  - Implemented `CategoryDefinition` dataclass specifying category descriptions, default reply requirements, default retrieval requirements, workflow hints, priorities, intent taxonomies, auto-send eligibility, and aliases.
  - Implemented `TaxonomyRegistry` allowing tenant-level customization and extensions while guaranteeing the 9 canonical baseline categories remain intact.
  - Implemented normalization and validation functions (`normalize_category`, `validate_category`, `is_valid_category`, `get_category_definition`).
  - Exported all taxonomy symbols in `packages.domain`.
- **Verification Command:**
  - `uv run ruff check packages/domain/ && uv run mypy packages/domain/ && uv run pytest tests/unit/test_dependency_rules.py`
- **Result:** PASS (0 lint errors, 0 type errors, all boundary tests passed)

## Step 2: Align Triage Worker Classifiers & Evaluation Schemas
- **Files Changed:**
  - `services/triage_worker/classifier.py`
  - `services/triage_worker/llm_classifier.py`
  - `evaluation/datasets/schemas.py`
- **What Changed:**
  - Updated Stage 2 ML classifier to import `NO_REPLY_CATEGORIES`, `RETRIEVAL_CATEGORIES`, and `normalize_category` directly from `packages.domain.taxonomy`.
  - Updated Stage 3 LLM fallback classifier to use `CANONICAL_CATEGORIES`, `normalize_category`, and `is_valid_category` from `packages.domain.taxonomy` in its Pydantic validator.
  - Updated evaluation dataset schemas to alias `ClassificationCategory = Category` from `packages.domain.taxonomy`.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ evaluation/ && uv run mypy services/triage_worker/ evaluation/ && uv run pytest tests/unit/test_triage_ml.py tests/unit/test_triage_stage3.py tests/unit/test_triage_cascade.py`
- **Result:** PASS (42/42 tests passed, 0 lint/mypy errors)

## Step 3: Author Category Taxonomy Unit Test Suite
- **Files Changed:**
  - `tests/unit/test_category_taxonomy.py` (new)
- **What Changed:**
  - Authored 50 unit tests covering:
    - R6.4 mandatory category conformance and string enum behavior.
    - Category definition completeness and metadata consistency.
    - Default routing rules: no-reply categories, actionable categories, retrieval categories, workflow hints.
    - Normalization of aliases and synonyms (e.g. `technical_support` -> `support`, `out_of_office` -> `no_response`, etc.).
    - Strict category validation and error handling.
    - Tenant-level custom category registration via `TaxonomyRegistry`.
    - Strict architectural domain boundary compliance (stdlib + packages.core only).
- **Verification Command:**
  - `uv run pytest tests/unit/test_category_taxonomy.py -v`
- **Result:** PASS (50/50 passed in 0.23s)

## Step 4: Regression Verification & Task 2.6 Sign-Off
- **Files Changed:**
  - `specs/tasks.md`
  - `artifacts/superpowers/execution.md`
  - `artifacts/superpowers/finish.md`
- **What Changed:**
  - Validated full test suite (502 unit and integration tests passing).
  - Validated strict static type checking with mypy and code formatting with ruff across 173 source files.
  - Marked Task 2.6 complete in `specs/tasks.md`.
- **Verification Command:**
  - `uv run pytest tests/unit tests/integration -q && uv run ruff check . && uv run mypy packages services tests evaluation`
- **Result:** PASS (502 passed, 0 lint errors, 0 type errors)

# Execution Log: Phase 2 Task 2.7 — Early-Exit Gate — The Cost Lever

## Step 1: Implement Early-Exit Gate Engine
- **Files Changed:**
  - `services/triage_worker/gate.py` (new)
  - `services/triage_worker/__init__.py`
- **What Changed:**
  - Implemented `GateAction(StrEnum)` (`EARLY_EXIT`, `PROCEED_NO_RAG`, `PROCEED_RAG`).
  - Implemented `GateDecision` dataclass recording action, job, event, and explicit execution flags (`should_embed`, `should_retrieve`, `should_rerank`, `should_generate`).
  - Implemented `EarlyExitGate` providing pure in-memory `evaluate_decision` and database-persisted `evaluate_and_persist` (via `JobStore`).
  - Implemented legal state transitions:
    - If `reply_required == false`: transitions directly from `CLASSIFIED` to `COMPLETED` (`R6.5`). Sets all AI execution flags to `False`.
    - If `retrieval_required == false`: transitions to `QUEUED` with `should_retrieve=False` (`R6.6`).
    - If `retrieval_required == true`: transitions to `QUEUED` with `should_retrieve=True`.
  - Implemented `DownstreamPipelineHooks(Protocol)` and `GatedPipelineRunner` asserting zero calls on early exit and selective retrieval bypass.
  - Exported all gate symbols in `services.triage_worker`.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`
- **Result:** PASS (0 lint errors, 0 type errors)

## Step 2: Wire Gate into Cascading Triage Engine
- **Files Changed:**
  - `services/triage_worker/cascade.py`
- **What Changed:**
  - Added optional `gate: EarlyExitGate` to `CascadingTriageEngine.__init__`.
  - Implemented `triage_and_gate` (async) and `triage_and_gate_sync` (sync wrapper) methods, coupling classification output with immediate gate evaluation.
- **Verification Command:**
  - `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`
- **Result:** PASS (0 lint errors, 0 type errors)

## Step 3: Author Gate Unit Test Suite
- **Files Changed:**
  - `tests/unit/test_early_exit_gate.py` (new)
- **What Changed:**
  - Authored 10 comprehensive unit tests validating:
    - Early exit across all no-reply categories (`automated_notification`, `acknowledgement`, `no_response`) with direct transition to `COMPLETED` (`R6.5`).
    - Downstream execution barrier asserting 0 calls (call count == 0) to `embed_query`, `retrieve_knowledge`, `rerank_candidates`, and `generate_reply` via `GatedPipelineRunner`.
    - Legal state progression from `NORMALIZED -> CLASSIFIED -> COMPLETED`.
    - Selective retrieval bypass when `retrieval_required=False`: 0 calls to embedding, retrieval, and reranking, while generation is executed (`R6.6`).
    - Full RAG path when `retrieval_required=True`: all stages executed.
    - Prevention of illegal state transitions (raising `IllegalStateTransitionError`).
    - Store-backed persistence with `InMemoryJobStore`.
    - End-to-end integration via `engine.triage_and_gate` and `engine.triage_and_gate_sync`.
- **Verification Command:**
  - `uv run pytest tests/unit/test_early_exit_gate.py -v`
- **Result:** PASS (10/10 passed in 1.70s)

## Step 4: Author PostgreSQL Gate Integration Test Suite
- **Files Changed:**
  - `tests/integration/test_early_exit_gate_postgres.py` (new)
- **What Changed:**
  - Authored 3 integration tests against live PostgreSQL container on port 5433 verifying:
    - Early exit transitions job to `COMPLETED` and atomically writes `processing_event` audit trail (`state_from='CLASSIFIED'`, `state_to='COMPLETED'`).
    - Actionable job transitions to `QUEUED` with correct retrieval flags.
    - Tenant isolation: cross-tenant access is rejected with `KeyError`.
- **Verification Command:**
  - `uv run pytest tests/integration/test_early_exit_gate_postgres.py -v`
- **Result:** PASS (3/3 passed in 2.43s)

# Execution Log: Phase 2 Task 2.8 — Deterministic Template Reply Path

## Step 1: Update Domain State Machine & Entities
- **Files Changed:**
  - `packages/domain/state_machine.py`
  - `packages/domain/entities.py`
  - `packages/domain/__init__.py`
  - `tests/unit/test_state_machine.py`
- **What Changed:**
  - Added `JobState.DRAFTED` to allowed target states in `TRANSITIONS[JobState.CLASSIFIED]` to support deterministic template replies transitioning directly to `DRAFTED`.
  - Added `GeneratedDraft` dataclass entity in `packages/domain/entities.py` matching the `generated_draft` database schema.
  - Updated `tests/unit/test_state_machine.py` (25 total legal transitions, verified `CLASSIFIED -> DRAFTED`, replaced illegal transition test).
- **Verification Command:**
  - `uv run pytest tests/unit/test_state_machine.py -v`
- **Result:** PASS (23/23 passed in 0.12s)

## Step 2: Implement Template Domain & Variable Substitution Engine
- **Files Changed:**
  - `packages/domain/templates.py` (new)
  - `packages/domain/__init__.py`
  - `tests/unit/test_templates_domain.py` (new)
- **What Changed:**
  - Implemented `TemplateDefinition`, `TemplateRenderResult`, `substitute_variables`, `build_template_context`, and `TemplateRegistry` in pure domain stdlib.
  - Supported mustache-style variable substitution (`{{ variable }}` and `{{ object.field }}`) for message fields and business data fields.
  - Authored unit test suite covering variable extraction, dot-notation traversal, fallback defaults, and registry operations.
- **Verification Command:**
  - `uv run pytest tests/unit/test_templates_domain.py -v && uv run pytest tests/unit/test_dependency_rules.py -v`
- **Result:** PASS (10/10 passed in 0.10s, 4/4 dependency rules passed in 0.67s)

## Step 3: Approved Versioned Template Files & Default Configuration
- **Files Changed:**
  - `prompts/templates/acknowledgement.v1.txt` (new)
  - `prompts/templates/scheduling_ack.v1.txt` (new)
  - `config/templates.yaml` (new)
  - `services/triage_worker/template_loader.py` (new)
  - `services/triage_worker/__init__.py`
- **What Changed:**
  - Created version 1 approved text template files for `(acknowledgement, receipt_confirmation)` and `(scheduling, meeting_accepted)`.
  - Created declarative `config/templates.yaml` registering these templates.
  - Implemented `HotReloadableTemplateRegistry` and YAML loaders in `services/triage_worker/template_loader.py`.
- **Verification Command:**
  - `uv run python -c "from services.triage_worker.template_loader import load_templates_from_file; r = load_templates_from_file('config/templates.yaml'); assert len(r.templates) == 2"`
- **Result:** PASS

## Step 4: Implement Draft Persistence Store
- **Files Changed:**
  - `packages/db/draft.py` (new)
  - `packages/db/__init__.py`
  - `tests/unit/test_draft_store.py` (new)
- **What Changed:**
  - Defined `DraftStore` protocol exposing `create_draft`, `get_draft`, `list_drafts_for_job`, `list_drafts_for_thread`.
  - Implemented `InMemoryDraftStore` for isolated unit testing.
  - Implemented `PostgresDraftStore` using tenant-scoped parameterized SQL queries against `generated_draft` table.
- **Verification Command:**
  - `uv run pytest tests/unit/test_draft_store.py -v`
- **Result:** PASS (1/1 passed in 0.10s)

## Step 5: Gate Integration & Downstream Zero-Call Assertions
- **Files Changed:**
  - `services/triage_worker/gate.py`
  - `services/triage_worker/cascade.py`
  - `tests/unit/test_template_gate.py` (new)
- **What Changed:**
  - Added `GateAction.TEMPLATE_REPLY` to `GateAction`.
  - Extended `EarlyExitGate` to evaluate and render deterministic template replies when `workflow_hint == 'template'`, setting `JobState.DRAFTED` and zero-AI flags (`should_retrieve=False`, `should_generate=False`).
  - Added seamless fallback to `workflow_hint='ai'` when template is missing in registry, transitioning to `QUEUED` without blocking.
  - Extended `GatedPipelineRunner` to verify zero AI calls on `TEMPLATE_REPLY`.
  - Verified mutual exclusivity and exhaustiveness of all three funnel outcomes (early exit, template reply, AI generation).
- **Verification Command:**
  - `uv run pytest tests/unit/test_template_gate.py -v`
- **Result:** PASS (4/4 passed in 2.34s)

## Step 6: PostgreSQL Integration Tests & Full Suite Regression Verification
- **Files Changed:**
  - `tests/integration/test_template_gate_postgres.py` (new)
  - `specs/tasks.md`
  - `artifacts/superpowers/finish.md`
- **What Changed:**
  - Authored 3 integration tests against live PostgreSQL container verifying:
    - Atomically transitioning job to `DRAFTED`, recording `processing_event`, and persisting draft into `generated_draft`.
    - Fallback from missing template to `QUEUED` AI generation.
    - Multi-tenant draft isolation across organizations.
  - Validated full test suite (534/534 tests green).
  - Validated static type checking (`mypy --strict`) and code style (`ruff`).
  - Marked Task 2.8 complete in `specs/tasks.md`.
- **Verification Command:**
  - `uv run pytest tests/unit tests/integration -q && uv run ruff check . && uv run mypy packages services tests evaluation`
- **Result:** PASS (534 passed, 0 lint errors, 0 type errors)

# Execution Log: Phase 2 Task 2.9 — Funnel Instrumentation

## Step 1: Metrics Extension & Instrumentation
- **Files Changed:**
  - `packages/observability/metrics.py`
  - `tests/unit/test_observability_metrics.py`
- **What Changed:**
  - Added `emails_early_exit_total` Counter (`["organization", "category", "reason"]`) to `PipelineMetrics`.
  - Added `triage_funnel_outcomes_total` Counter (`["organization", "category", "outcome", "rag_mode"]`) for complete funnel accounting.
  - Verified `emails_templated_total` and `emails_generated_total` are exported side by side on `/metrics`.
- **Verification Command:**
  - `.venv/bin/pytest tests/unit/test_observability_metrics.py -v`
- **Result:** PASS (3/3 tests passed)

## Step 2: Funnel Reconciliation Module
- **Files Changed:**
  - `packages/observability/funnel.py` (new)
  - `packages/observability/__init__.py`
- **What Changed:**
  - Defined `FunnelOutcome` (`early_exit`, `template`, `ai_generation`) and `RAGMode` (`none`, `rag`, `no_rag`) enums.
  - Implemented `FunnelReport` and `compute_funnel_reconciliation(metrics, organization=None)`.
  - Enforced zero residual invariant: $\text{residual} = \text{total\_triaged} - (\text{early\_exit} + \text{template} + \text{ai\_generation}) = 0$.
  - Re-exported funnel symbols from `packages.observability`.
- **Verification Command:**
  - `.venv/bin/python -c "from packages.observability import FunnelOutcome, FunnelReport, RAGMode, compute_funnel_reconciliation; print('OK')"`
- **Result:** PASS

## Step 3: Triage Worker & Early-Exit Gate Instrumentation
- **Files Changed:**
  - `services/triage_worker/gate.py`
  - `services/triage_worker/cascade.py`
- **What Changed:**
  - Injected `metrics: PipelineMetrics | None = None` into `EarlyExitGate` and `CascadingTriageEngine`.
  - Implemented `_record_metrics` in `EarlyExitGate` to atomically increment `triage_funnel_outcomes_total`, `emails_early_exit_total`, and `emails_templated_total` on every gate transition.
  - Recorded `emails_classified_total` and `classification_latency_ms` on cascade completion.
- **Verification Command:**
  - `.venv/bin/pytest tests/unit/test_early_exit_gate.py tests/unit/test_template_gate.py -v`
- **Result:** PASS (14/14 tests passed)

## Step 4: Comprehensive Unit & Integration Tests
- **Files Changed:**
  - `tests/unit/test_funnel_metrics.py` (new)
  - `tests/integration/test_funnel_metrics_integration.py` (new)
- **What Changed:**
  - Authored 7 unit tests verifying counter registrations, gate action metric emissions, multi-tenant isolation, zero-traffic edge cases, and 100k-email reference dataset simulation (45k early exit, 20k template, 35k AI generation with 24.5k RAG and 10.5k no-RAG) proving zero residual and exact percentages.
  - Authored live PostgreSQL integration test verifying database state persistence (`COMPLETED`, `DRAFTED`, `QUEUED`), Prometheus text exposition format, and live reconciliation.
- **Verification Command:**
  - `.venv/bin/pytest tests/unit/test_funnel_metrics.py tests/integration/test_funnel_metrics_integration.py -v`
- **Result:** PASS (8/8 tests passed)

## Step 5: Observability Documentation & Full Verification
- **Files Changed:**
  - `docs/observability.md` (new)
  - `specs/tasks.md`
- **What Changed:**
  - Created `docs/observability.md` detailing the funnel architecture, metric definitions, and PromQL queries for the Grafana Funnel Dashboard.
  - Marked Task 2.9 complete (`[x]`) in `specs/tasks.md`.
  - Validated full test suite (542/542 tests green), ruff clean, mypy clean.
- **Verification Command:**
  - `.venv/bin/ruff check . && .venv/bin/mypy packages services && .venv/bin/pytest tests/unit tests/integration`
- **Result:** PASS (542/542 passed)

## Task 2.12: Retry, Backoff & Dead-Letter

### Step 1: Exponential Backoff & Jitter Engine
- **Files Changed:**
  - `packages/broker/backoff.py` (new)
  - `packages/core/settings.py`
  - `packages/broker/__init__.py`
  - `tests/unit/test_backoff.py` (new)
- **What Changed:**
  - Implemented `calculate_exponential_backoff` with support for 'full', 'equal', 'decorrelated', and 'none' jitter modes.
  - Implemented `resolve_retry_tier_delay` mapping attempt counts to discrete RabbitMQ retry tiers (30s, 300s, 1800s).
  - Added backoff settings (`backoff_base_s`, `backoff_factor`, `max_backoff_s`, `jitter_mode`) to `RetryLadderSettings`.
- **Verification Command:**
  - `.venv/bin/pytest tests/unit/test_backoff.py -v`
- **Result:** PASS (6/6 passed)

### Step 2: Retry, Recovery & Dead-Letter Coordinator
- **Files Changed:**
  - `packages/broker/retry.py` (new)
  - `packages/broker/publisher.py`
  - `packages/broker/__init__.py`
  - `packages/db/job.py`
- **What Changed:**
  - Implemented `handle_job_transient_failure` transitioning `GENERATING -> RETRY_PENDING` and publishing to retry ladder queue.
  - Implemented `handle_job_recovery` transitioning `RETRY_PENDING -> GENERATING` on redelivery.
  - Implemented `handle_job_terminal_failure` transitioning `FAILED -> DEAD_LETTER` and publishing to `dlx.email`.
  - Added `x-failed-at` ISO timestamp header and allowed custom headers in `MessagePublisher.publish_to_dead_letter`.
- **Verification Command:**
  - `.venv/bin/pytest tests/unit/test_retry_and_dead_letter.py -v`
- **Result:** PASS (6/6 passed)

### Step 3: Consumer and Batch Consumer Integration
- **Files Changed:**
  - `packages/broker/consumer.py`
  - `packages/broker/batch_consumer.py`
- **What Changed:**
  - Injected optional `job_store` into `BaseConsumer` and `BaseBatchConsumer`.
  - Automated recovery and failure handling on message consumption.
  - Enforced manual ack per message and batch item fault isolation.
- **Verification Command:**
  - `.venv/bin/pytest tests/unit/test_batch_consumer.py tests/unit/test_retry_and_dead_letter.py -v`
- **Result:** PASS (17/17 passed)

### Step 4: Configuration and Live Integration Verification
- **Files Changed:**
  - `.env.example`
  - `docs/configuration.md`
  - `tests/unit/test_settings.py`
  - `tests/integration/test_retry_dead_letter_integration.py` (new)
  - `specs/tasks.md`
- **What Changed:**
  - Added retry backoff documentation and example keys.
  - Authored live integration tests with PostgreSQL 16 and RabbitMQ 3.13 for recovery and DLQ exhaustion.
  - Marked Task 2.12 complete in `specs/tasks.md`.
- **Verification Command:**
  - `.venv/bin/pytest tests/unit/test_settings.py tests/integration/test_retry_dead_letter_integration.py -v`
  - `.venv/bin/pytest tests/unit tests/integration -q`
- **Result:** PASS (602/602 passed)
