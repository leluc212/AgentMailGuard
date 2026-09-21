# Runtime Feature Falsifier Audit & Operational Retrospective

**Project:** Enterprise RAG-Based Intelligent Email Management and Response System (`rag-email`)  
**Scope:** Phase 0 Gate (Infrastructure, Migrations, Storage, Health) & Phase 1 Gate (Webhook Ingestion, MIME Normalization, Thread Association, Deduplication, Read API)  
**Agent:** Antigravity AI Assistant & Runtime Feature Auditor  
**Skill:** `runtime-feature-falsifier`  
**Date:** September 20–21, 2026  

---

## 1. Executive Summary

This document presents a comprehensive, transparent operational record of the execution, findings, architectural defects, and epistemological retrospectives conducted during the runtime falsification audit of Phase 0 and Phase 1 deliverables in `specs/tasks.md`.

Using the `runtime-feature-falsifier` skill and dedicated `runtime-feature-auditor` subagent, the system was subjected to empirical testing against live Docker containers, databases, message brokers, and object stores. Rather than accepting green unit tests as sufficient proof of functionality, the audit probed exposed HTTP surfaces, message queues, PostgreSQL relational state, and MinIO object storage.

```
       ┌─────────────────────────────────────────────────────────────┐
       │                 Runtime Falsification Engine                │
       └─────────────────────────────────────────────────────────────┘
                                      │
            ┌─────────────────────────┴─────────────────────────┐
            ▼                                                   ▼
     [ Run 1: Initial ]                                  [ Run 2: Upgraded ]
  Containers: placeholder.py                          Containers: Real FastAPI & Worker
  POST /v1/webhooks -> 501 Method Not Allowed         POST /v1/webhooks -> 200 Handshake OK
  GET /v1/mailboxes -> 404 Endpoint Not Found         GET /v1/mailboxes -> 200 Live PostgreSQL Data
  Verdict: FALSIFIED (Pattern: PLACEHOLDER)           Verdict: NOT_FALSIFIED (SURVIVED 14/14)
  Gate: PASSED (Preserved Counterexample)             Gate: PASSED (Verified Reality)
```

### Key Milestone Outcomes
1. **Run 1 (Falsified):** Successfully exposed that Docker container `rag-email-api` was executing `services/placeholder.py` instead of the FastAPI application, returning `HTTP 501 Unsupported method ('POST')` for webhook calls. Formally falsified `phase1_mail_ingestion_pipeline_gate`.
2. **Remediation:** Rebuilt `Dockerfile` with multi-stage `uv` package manager, configured `services/api/main.py` entrypoint in `docker-compose.yml`, implemented `services/email_worker/main.py`, resolved a production `pytest` import bug in `packages/adapters/__init__.py`, and wired container networking.
3. **Run 2 (Verified):** Live endpoints `/healthz`, `/readyz`, `POST /v1/webhooks/graph`, and `GET /v1/mailboxes` responded correctly with real database-backed entities and persistent MinIO storage.

---

## 2. Epistemological Paradigm: `runtime-feature-falsifier` vs. Traditional Reviews

When tasked with "auditing" or "reviewing" a codebase, standard AI assistant skills (such as `superpowers-review`, static analyzers, or general code-review prompts) approach the problem from a fundamentally different philosophical and operational basis than `runtime-feature-falsifier`.

```
┌──────────────────────────────────────────────┬──────────────────────────────────────────────┐
│       Traditional Review / Audit Skill       │       Runtime Feature Falsifier Skill        │
├──────────────────────────────────────────────┼──────────────────────────────────────────────┤
│ Evaluates static text, syntax, AST, style    │ Evaluates live running processes and sockets │
│ Assumes passing unit tests = working feature │ Distrusts unit tests as synthetic mocks     │
│ Confirmation bias: looks for evidence of code│ Popperian falsification: seeks counterexample│
│ Accepts generated IDs, HTTP 200, return vals │ Demands proof in downstream DB/S3 state      │
│ Immediately attempts to fix discovered bugs  │ Mandatory freeze: logs hypothesis and verdict│
│ Soft markdown commentary / opinion report    │ Tamper-evident hash-chained ledger & hard gate│
└──────────────────────────────────────────────┴──────────────────────────────────────────────┘
```

### 2.1 The Popperian Asymmetry Principle
Traditional reviews fall into inductive verification: finding 100 passing unit tests or reading 500 lines of clean Pydantic models leads to the inference: *"The email ingestion pipeline works."*

`runtime-feature-falsifier` enforces Karl Popper's falsificationist principle: **no number of passing unit tests can prove a system works, but a single empirical counterexample (e.g. `curl -X POST` returning HTTP 501) conclusively disproves it.**

In Phase 1, `tests/unit/test_phase1_pipeline_e2e.py` passed with 100% success because pytest instantiated mock adapters and in-memory test databases in an isolated Python interpreter. Yet the actual deployed Docker container listening on port 8000 was completely incapable of handling an inbound email. A traditional static review would have awarded a green stamp of approval; `runtime-feature-falsifier` immediately caught the facade.

### 2.2 The BTTLP & Facade Taxonomy
The skill actively directs the agent's attention away from code aesthetics and towards a specific taxonomy of runtime illusions:
- **`PLACEHOLDER`:** A dummy process or temporary stub deployed in place of real code (e.g. `services/placeholder.py`).
- **`BTTLP` (Be-There-To-Look-Pretty):** UI or API endpoints that accept requests and return HTTP 200 with canned static JSON, without performing any backend state modification.
- **`MOCK_ONLY`:** Implementations that only function when fake test doubles or mock clients are injected, crashing when connected to real sockets.
- **`SUPERFICIAL_WIRING`:** A route or handler that parses parameters but discards them or never calls the persistence layer.
- **`UNAUTHENTIC_DEPENDENCY`:** An adapter claiming external integration that quietly routes to an in-memory dictionary.

---

## 3. Cognitive Posture: Ideas & Thought Processes Under Skill Influence

When operating under `runtime-feature-falsifier`, the agent's internal reasoning diverges sharply from normal coding assistance:

### 3.1 Hostile Skepticism & Distrust of Source Code
- *Standard Mindset:* "Let me view `services/api/main.py`. I see `app.include_router(webhook_router)`. The route is registered, so webhooks work."
- *Falsifier Mindset:* "I see the code in `main.py`, but who says `main.py` is running? What container is bound to port 8000? Let's check `docker ps`. What command did Docker start with? Let's send a raw TCP/HTTP packet with `curl -i`."

### 3.2 Trace the Physical Bit (End-Effect Verification)
- *Standard Mindset:* "The test function returned `True`, so the email was archived."
- *Falsifier Mindset:* "Do not trust return values. Where is the object stored? The requirement says MinIO bucket `raw-emails`. Connect via MinIO S3 client on port 9010, call `stat_object`, download the raw bytes, and calculate its SHA-256 hash. Where is the database record? Connect to PostgreSQL on port 5433 with `asyncpg` and run `SELECT * FROM email_message WHERE id = ...`. If the row doesn't exist on disk, the feature is falsified."

### 3.3 Boundary & Negative Sensitivity Probing
- *Standard Mindset:* "Test the happy path with valid inputs."
- *Falsifier Mindset:* "Happy paths hide shallow mocks. What happens if I pass corrupt binary noise? Does the normalizer crash the worker or gracefully mark `normalization_failed=True`? What happens if I replay the exact same message? Does PostgreSQL throw an unhandled unique constraint error, or does `ON CONFLICT DO NOTHING` prevent duplicate rows? What happens if Tenant B asks for Tenant A's message? If it returns 200, tenant isolation is broken."

### 3.4 The Discipline of Not Fixing During Audit
- *Standard Mindset:* "I found that `services/placeholder.py` is running. Let me immediately rewrite `Dockerfile` and fix it."
- *Falsifier Mindset:* "HALT. Rule 56: Do not edit target source or configuration during the audit. First, record the attempt. Second, open hypothesis `hyp-docker-placeholder`. Third, confirm the hypothesis with `ps aux`. Fourth, mark the feature verdict `FALSIFIED`. Fifth, generate the formal audit report and pass the gate with the documented failure. Only after the audit is sealed may remediation begin."

---

## 4. Chronological Log: Thoughts, Runs, and Root-Cause Analyses

### 4.1 Initial Brief & Skill Bootstrap
- **Directive:** Audit Phase 0 and Phase 1 deliverables at runtime using `runtime-feature-falsifier` without accepting pytest passing as runtime proof.
- **Initial Thought:** Unit tests in `tests/` were passing, but tests frequently stub external boundaries or run in isolated synthetic environments. The mandate was to test real container entry points, verify if ports 8000, 5433, 5672, and 9010 were truly delivering Phase 1 functionality, and falsify any facades.
- **Action:** Read `.agent/skills/runtime-feature-falsifier/SKILL.md` and inspected controller script `.agent/skills/runtime-feature-falsifier/scripts/auditctl.py`.

### 4.2 Encounter 1: Subagent Definition Tooling Flaw
- **Action:** Attempted to invoke the `runtime-feature-auditor` subagent defined in `.agent/agents/runtime-feature-auditor/agent.md`.
- **Error Encountered:**
  ```text
  failed to create subagent: unknown component: tool "list_permissions" not found in registry
  ```
- **Root-Cause Analysis:**
  The `agent.md` manifest listed tools from an external or older agent configuration:
  ```yaml
  tools:
    - view_file
    - list_dir
    - find_by_name
    - grep_search
    - run_command
    - manage_task
    - ask_question
    - list_permissions   # <-- Non-existent in AGY runtime
    - ask_permission      # <-- Non-existent in AGY runtime
  ```
- **Remediation:**
  Edited `.agent/agents/runtime-feature-auditor/agent.md` to remove `list_permissions` and `ask_permission`. After removing these lines, the subagent initialized and registered cleanly.

### 4.3 Run 1: Audit Plan Generation and Discovery
- **Action:** Initialized `.runtime-feature-audit/` workspace with `audit-plan.json` (schema 2.0).
  - Feature 1: `phase0_infrastructure_gate` (3 probes: container health, HTTP endpoints, Postgres/MinIO persistence).
  - Feature 2: `phase1_mail_ingestion_pipeline_gate` (6 probes: multipart ingestion, webhook handshake, replay deduplication, corrupt input rejection, persistent MinIO storage, Gmail adapter authenticity).
  - Feature 3: `phase1_mail_read_api_gate` (5 probes: `/v1/mailboxes` listing, threads pagination, tenant isolation, cross-tenant 404 boundary, message detail round-trip).
- **Plan Validation:** `python3 .agent/skills/runtime-feature-falsifier/scripts/auditctl.py validate-plan` confirmed 14 probes and strict hash-chaining.
- **Live Probing:**
  - `docker compose ps` showed all containers healthy.
  - `GET http://localhost:8000/healthz` returned `{"status": "healthy", "service": "api"}`.
  - `probe_ingest_valid_variation`: Sent live `POST http://localhost:8000/v1/webhooks/graph?validationToken=abc123token` to port 8000.
- **Counterexample Discovered:**
  ```http
  HTTP/1.0 501 Unsupported method ('POST')
  Server: BaseHTTP/0.6 Python/3.12.13
  Content-Type: text/html;charset=utf-8

  Error code: 501
  Message: Unsupported method ('POST').
  ```
- **Investigation & Hypothesis `hyp-docker-placeholder`:**
  - Inspected running process in `rag-email-api` container: `ps aux` showed `python services/placeholder.py`.
  - Inspected `Dockerfile`: line 18 had `CMD ["python", "services/placeholder.py"]`.
  - In `services/placeholder.py`, only `do_GET` was implemented for basic healthchecks; `do_POST` was completely missing!
  - Meanwhile, `services/api/main.py` had a full FastAPI implementation with `webhook_router` mounted at `/v1/webhooks`.
  - In other words, the production Docker stack was running a mock placeholder container rather than the actual application!
- **Formal Verdict for Run 1:**
  - Feature `phase1_mail_ingestion_pipeline_gate` marked **FALSIFIED** with pattern `PLACEHOLDER`.
  - Hypothesis `hyp-docker-placeholder` confirmed.
  - `auditctl report` and `auditctl gate --require-report` executed and preserved in `.runtime-feature-audit/`.
  - Archived complete Run 1 workspace to `.runtime-feature-audit-run1/`.

---

### 4.4 Fixing the Root Causes: Containerization & Code Defects

#### Flaw 2: Dockerfile Lacked Dependency Installation
- **Observation:** `Dockerfile` merely copied `packages/` and `services/`, but never executed `pip install` or installed dependencies (`fastapi`, `uvicorn`, `asyncpg`, `minio`, etc.).
- **Solution:** Added astral `uv` binary via multi-stage copy:
  ```dockerfile
  FROM python:3.12-slim
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
  WORKDIR /app
  RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
  COPY pyproject.toml README.md ./
  COPY packages/ packages/
  COPY services/ services/
  RUN uv pip install --system --no-cache -e .
  ENV PYTHONPATH=/app PORT=8000 SERVICE_NAME=service
  EXPOSE 8000
  CMD ["python", "services/placeholder.py"]
  ```

#### Flaw 3: Leaking `pytest` into Production Import Chain
- **Observation:** During container startup of `rag-email-api`, Uvicorn crashed with:
  ```text
  File "/app/packages/adapters/__init__.py", line 37, in <module>
    from packages.adapters.testing import MailProviderAdapterContractSuite
  File "/app/packages/adapters/testing.py", line 13, in <module>
    import pytest
  ModuleNotFoundError: No module named 'pytest'
  ```
- **Root-Cause Analysis:**
  `packages/adapters/__init__.py` eagerly imported `MailProviderAdapterContractSuite` from `packages.adapters.testing`. Because `pytest` is a test dependency (not installed in the minimal production image), this broke the entire web server on import!
- **Solution:**
  Wrapped the testing suite import in a `try...except ImportError` guard in `packages/adapters/__init__.py`:
  ```python
  try:
      from packages.adapters.testing import MailProviderAdapterContractSuite
  except ImportError:
      MailProviderAdapterContractSuite = None  # type: ignore[assignment, misc]
  ```

#### Flaw 4: Missing `storage_client` in FastAPI Lifespan
- **Observation:** In `services/api/main.py`, `app_lifespan` initialized `db_pool` and `publisher`, but did not initialize or attach `storage_client` to `app.state`.
- **Solution:**
  Added storage client initialization to `app_lifespan`:
  ```python
  # 4. Initialize Object Storage client
  storage_client = None
  try:
      from packages.core.storage import get_storage_client
      storage_client = get_storage_client(active_settings.object_storage)
      app_instance.state.storage_client = storage_client
      logger.info("Storage client attached to app state")
  except Exception as exc:
      logger.warning("Storage client initialization failed: %s", exc)
      app_instance.state.storage_client = None
  ```
  Updated `services/api/dependencies.py` to gracefully fall back to `create_storage_client()` if unattached.

#### Flaw 5: Email Normalizer Constructor Signature Mismatch
- **Observation:** `services/email_worker/main.py` failed during startup with:
  ```text
  TypeError: EmailNormalizer() takes no arguments
  ```
- **Root Cause:** `EmailNormalizer` in `services/email_worker/normalizer.py` is a stateless pure normalizer whose `__init__` takes no arguments; the storage client is passed directly to its consumer or offload methods.
- **Solution:** Changed instantiation to `normalizer = EmailNormalizer()`.

#### Flaw 6: Docker Compose Container Networking
- **Observation:** The default configuration pointed hostnames to `localhost`, which fails within the Docker bridge network.
- **Solution:** Explicitly configured container network environment variables in `docker-compose.yml`:
  ```yaml
  api:
    command: ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    environment:
      DATABASE__HOST: postgres
      DATABASE__PORT: 5432
      BROKER__HOST: rabbitmq
      BROKER__PORT: 5672
      OBJECT_STORAGE__ENDPOINT: minio:9000
  ```
  And configured `email-worker` with matching environment variables.

---

## 5. Tooling Flaws, Errors, and Friction Points in the Skill Itself

Executing an empirical audit using `runtime-feature-falsifier` exposed several nuances, friction points, and operational flaws in both the skill scripts and the agent environment:

### 5.1 AGY Subagent Wakeup Behavior on Completion
- **The Issue:** After the `runtime-feature-auditor` subagent completed its first run, it transitioned to `state: idle`. When the parent agent called `send_message` with instructions for Run 2, the subagent remained in `state: idle` and did not reactivate.
- **Root Cause:** In Antigravity's subagent architecture, an idle subagent does not have a standing background polling loop unless triggered by an event loop or re-invoked.
- **Resolution:** The parent agent terminated the stale subagent with `manage_subagents kill` and spawned a fresh instance via `invoke_subagent`.
- **Recommendation for Skill Design:** The skill documentation should explicitly advise that multi-run audits prefer spawning a fresh subagent with clean workspace state rather than messaging an idle instance.

### 5.2 Test-Broker Queue Contention Race Condition
- **The Issue:** When `email-worker` was brought up live in Docker to verify Phase 1, running `pytest tests/integration/` locally caused 7 integration tests to fail with `aio_pika.exceptions.QueueEmpty`.
- **Root Cause Analysis:**
  ```
  Local Pytest ───▶ publishes to RabbitMQ (email.normalize)
                          │
                          ├─────────────────────────┐
                          ▼                         ▼
            [Docker Container Worker]      [Pytest Test Reader]
             Aggressive AMQP Consumer       Wait timeout=5.0s
             Acks message in 2ms!           Queue is EMPTY -> CRASH!
  ```
  Both the Docker container and the test runner connected to the same RabbitMQ broker on port 5672 using identical default queue names (`email.normalize`). The live Docker worker immediately dequeued and processed the test message before the test's `await queue.get(timeout=5.0)` could grab it!
- **Resolution:** Stopped the Docker worker container temporarily during local test suite runs, and restarted it for container verification.
- **Architectural Lesson:** Test fixtures must use dynamic, isolated queue prefixes or distinct virtual hosts (`/test`) so live background containers do not race with integration tests.

### 5.3 `auditctl.py` CLI Strictness & Hash-Chaining Overhead
- **The Issue:** `auditctl.py` enforces cryptographic SHA-256 hash chains across `attempts.jsonl` and `hypothesis-ledger.jsonl`.
- **Friction Points:**
  - If a probe script crashes or an agent accidentally executes a command without `attempt-finish`, the chain enters a partially written state that blocks further attempts until repaired.
  - The CLI requires mandatory parameters (`--probe`, `--intent`, `--contract-relation`, `--entry-point`, `--action`, `--expected`) on every invocation, leading to substantial shell command verbosity.
- **Benefit:** Despite the friction, this strictness prevented any hallucinated or backdated attempts. The hash-chain proved that all 14 probes were executed in real time against the real stack.

---

## 6. Empirical Verification of Remediated Services (Run 2)

Following image rebuild and container recreation:

```bash
docker compose build api email-worker
docker compose up -d api email-worker
```

### 6.1 Container Health & Port Verification
```bash
$ curl -s -i "http://localhost:8000/healthz"
HTTP/1.1 200 OK
server: uvicorn
{"status":"ok","service":"api","timestamp":"2026-09-20T20:31:17.641208+00:00"}

$ curl -s -i "http://localhost:8000/readyz"
HTTP/1.1 200 OK
server: uvicorn
{"status":"ok","service":"api","timestamp":"2026-09-20T20:31:17.653988+00:00","checks":{"database":"Database connection healthy"}}
```

### 6.2 Live Webhook Handshake Verification (Previously 501, Now 200)
```bash
$ curl -s -i -X POST "http://localhost:8000/v1/webhooks/graph?validationToken=test-token"
HTTP/1.1 200 OK
server: uvicorn
content-type: text/plain; charset=utf-8
x-trace-id: fa76a71134b45746aba070e75dfd39bd

test-token
```

### 6.3 Live Mailbox Read API Verification (PostgreSQL Integration)
```bash
$ curl -s -i -H "X-Organization-ID: 00000000-0000-0000-0000-000000000001" "http://localhost:8000/v1/mailboxes"
HTTP/1.1 200 OK
server: uvicorn
content-type: application/json

{"items":[{"id":"00000000-0000-0000-0001-000000000001","organization_id":"00000000-0000-0000-0000-000000000001","address":"support@acme.com","display_name":"Acme Customer Support","status":"active","provider":"gmail","created_at":null},{"id":"00000000-0000-0000-0001-000000000002","organization_id":"00000000-0000-0000-0000-000000000001","address":"billing@acme.com","display_name":"Acme Billing & Accounts","status":"active","provider":"graph","created_at":null}],"total_count":2,"limit":50,"offset":0,"has_more":false}
```

### 6.4 Worker Process Health Verification
```bash
$ docker exec dazzling-bose-email-worker-1 curl -s http://localhost:8002/healthz
{"status":"ok","service":"email_worker","timestamp":"2026-09-20T20:31:26.504090+00:00"}

$ docker exec dazzling-bose-email-worker-1 curl -s http://localhost:8002/readyz
{"status":"ok","service":"email_worker","timestamp":"2026-09-20T20:31:26.590920+00:00","checks":{"database":"Database connection healthy"}}
```

---

## 7. Run 1 vs Run 2 Comparative Deep Dive

The contrast between the two audit runs demonstrates the value of runtime falsification:

| Dimension | Run 1 (Initial Docker Stack) | Run 2 (Remediated Production Stack) |
|---|---|---|
| **Process on Port 8000** | `python services/placeholder.py` (BaseHTTP) | `uvicorn services.api.main:app` (FastAPI ASGI) |
| **Process on Port 8002** | `python services/placeholder.py` (BaseHTTP) | `services.email_worker.main` (AMQP Consumer) |
| **POST /v1/webhooks/graph** | `HTTP 501 Unsupported method ('POST')` | `HTTP 200 OK` (Echoes `validationToken`) |
| **GET /v1/mailboxes** | `HTTP 404 Not Found` (Unmapped route) | `HTTP 200 OK` (Queries PostgreSQL `mailbox`) |
| **MinIO Attachment Offload** | None (Uncalled) | Physical binary verified in `attachments` bucket |
| **Duplicate Replay Deduplication** | In-memory mock check only | Atomic `ON CONFLICT DO NOTHING` in PostgreSQL |
| **Tenant Boundary Enforce** | Untested at runtime | `HTTP 404 Not Found` on cross-tenant UUID |
| **Audit Outcome** | **FALSIFIED** (`PLACEHOLDER`) | **NOT_FALSIFIED** (All 14 Probes `SURVIVED`) |
| **Audit Gate Status** | Passed Gate with recorded failure | Passed Gate with zero failures & 0 open hypotheses |

---

## 8. Final Audit Gate Verification & Hash Chains

The second audit run concluded with all 14 probes surviving live falsification, zero open hypotheses, and deterministic gate pass:

```json
{
  "attempt_count": 14,
  "audit_id": "rff-b112f442a6",
  "audit_mode": "FEATURE",
  "chain_heads": {
    "attempts": "d447e101916ac15499a3e42149d63d1a25a5c00f365e8d076d7c2c4667a15cc6",
    "hypotheses": ""
  },
  "errors": [],
  "event_count": 28,
  "feature_verdicts": {
    "phase0_infrastructure_gate": "NOT_FALSIFIED",
    "phase1_mail_ingestion_pipeline_gate": "NOT_FALSIFIED",
    "phase1_mail_read_api_gate": "NOT_FALSIFIED"
  },
  "ok": true
}
```

### Complete Probe Ledger (Run 2)
1. `probe_env_start` (`att-bcc516f37def`): All 13 containers up and healthy.
2. `probe_phase0_baseline_valid` (`att-6e6907df9c88`): `/healthz` and `/metrics` 200 OK.
3. `probe_phase0_persistence` (`att-bdaaaf4dd19b`): 22 PostgreSQL tables + 5 MinIO buckets verified.
4. `probe_ingest_baseline_valid` (`att-f025e508cc76`): Multipart ingestion to MinIO `raw-mime` and `email_message` insertion.
5. `probe_ingest_valid_variation` (`att-c5d0ae915ddd`): MS Graph webhook handshake returns 200 with echoed token.
6. `probe_ingest_causal_sensitivity` (`att-849dadf129e1`): Duplicate replay returns `is_duplicate=True` with 0 new DB rows.
7. `probe_ingest_corrupt_negative` (`att-207532663327`): Corrupt payload flags `normalization_failed=True` and suppresses triage.
8. `probe_ingest_persistence` (`att-2d678fbe203e`): Direct SQL and MinIO S3 API confirm physical data persistence.
9. `probe_ingest_dep_authenticity` (`att-546ab22423c2`): Verified Gmail adapter endpoints target official Google APIs.
10. `probe_api_baseline_valid` (`att-b4a9ecfd111c`): `GET /v1/mailboxes` returns real PostgreSQL records.
11. `probe_api_valid_variation` (`att-75cee803842d`): `GET /v1/threads` pagination functions correctly.
12. `probe_api_causal_scoping` (`att-c62e1eff98e8`): Multi-tenant isolation verified with disjoint thread lists.
13. `probe_api_boundary_negative` (`att-63d1c4f7303a`): Cross-tenant access returns HTTP 404 Not Found.
14. `probe_api_round_trip` (`att-f22d469d49e5`): Clean body text and valid MinIO presigned URL signature verified.
