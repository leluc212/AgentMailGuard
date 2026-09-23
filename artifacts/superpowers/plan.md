# Implementation Plan — Phase 2 Task 2.14: Job Timeline API & Replay

**Spec Alignment:** `specs/requirements.md` (R18.6, R18.7, R23.2, R23.5, R23.6) · `specs/tasks.md` Task 2.14 · `specs/design.md §8, §9` · `GEMINI.md`

---

## 1. Goal & Architecture Overview

Expose REST API endpoints to:
1. Retrieve ordered `processing_event` audit history for any message (`GET /v1/messages/{id}/timeline`) per R18.6 and R23.5.
2. Replay a `DEAD_LETTER` job from its last good state via an operator endpoint (`POST /v1/jobs/{id}/replay`) per R18.7 and R23.2.
3. Provide single job inspection (`GET /v1/jobs/{id}`) and job event timeline (`GET /v1/jobs/{id}/timeline`) under tenant scoping per R23.2 and R23.6.

```
                           OPERATOR / UI
                             │         │
       GET /v1/messages/{id}/timeline  POST /v1/jobs/{id}/replay
                             │         │
                             ▼         ▼
                      ┌──────────────────────┐
                      │    FastAPI Server    │
                      │  (mandatory org_id)  │
                      └───────┬──────┬───────┘
                              │      │
           ┌──────────────────┘      └──────────────────┐
           │ list_events_for_message                    │ replay_job (atomic)
           ▼                                            ▼
┌─────────────────────────┐                  ┌─────────────────────────┐
│     PostgreSQL 16       │                  │   JobStore.replay_job   │
│ - processing_event      │                  │ - verify DEAD_LETTER    │
│   (ordered created_at)  │                  │ - state -> RETRY_PENDING│
│ - email_message         │                  │ - reset attempt count   │
└─────────────────────────┘                  │ - insert replay event   │
                                             └────────────┬────────────┘
                                                          │
                                                          ▼ publish envelope
                                             ┌─────────────────────────┐
                                             │     RabbitMQ 3.13       │
                                             │ - exchange_email_route  │
                                             │ - routing_key:          │
                                             │   job.queue_name        │
                                             └─────────────────────────┘
```

---

## 2. User Review Required

> [!IMPORTANT]
> - `POST /v1/jobs/{id}/replay` enforces strict state validation: only jobs in `DEAD_LETTER` state may be replayed. Attempting to replay an active (`GENERATING`, `CONTEXT_READY`) or final successful (`COMPLETED`) job returns HTTP `409 Conflict`.
> - Replay atomically transitions `DEAD_LETTER -> RETRY_PENDING` (explicitly declared in `packages/domain/state_machine.py`) and resets `attempt = 0` and `last_error = None` so the replayed job enjoys a fresh retry budget.
> - When `MessagePublisher` is attached to the API application state, the replayed job is immediately packaged into a `JobEnvelope` and published to the destination queue (`job.queue_name`), where consumers transition it back to `GENERATING` via `handle_job_recovery`.

---

## 3. Step-by-Step Implementation Steps

### Step 1: Add Replay and JobStore API Methods
- **Files to Modify:**
  - `packages/db/job.py`
- **Actions:**
  - Add `replay_job` to `JobStore` protocol:
    ```python
    async def replay_job(
        self,
        organization_id: UUID | str,
        job_id: UUID | str,
        payload: dict[str, Any] | None = None,
        reset_attempts: bool = True,
    ) -> tuple[Job, ProcessingEvent]: ...
    ```
  - Implement atomic `replay_job` in `PostgresJobStore`:
    - Row-level lock: `SELECT ... FROM processing_job WHERE id = $1 AND organization_id = $2 FOR UPDATE`.
    - Enforce current state is `DEAD_LETTER`; if not, raise `IllegalStateTransitionError`.
    - Transition state to `RETRY_PENDING` using `packages.domain.state_machine.transition_job`.
    - Update `state = RETRY_PENDING`, `attempt = 0` (if reset_attempts), `last_error = None`, `lease_expires_at = None`, `next_retry_at = None`, `updated_at = now()`.
    - Insert `processing_event` with `event_type = 'operator_replay'`, `state_from = 'DEAD_LETTER'`, `state_to = 'RETRY_PENDING'`.
  - Implement thread-safe `replay_job` in `InMemoryJobStore`.
- **Verification:**
  - `.venv/bin/pytest tests/unit/test_lease_reaper.py -v`

---

### Step 2: Define Pydantic Schemas for Jobs and Timelines
- **Files to Create / Modify:**
  - `services/api/schemas/jobs.py` (NEW)
  - `services/api/schemas/messages.py`
  - `services/api/schemas/__init__.py`
- **Actions:**
  - In `services/api/schemas/jobs.py`:
    - `ProcessingEventResponse`: schema representing `ProcessingEvent` records (`id`, `job_id`, `message_id`, `organization_id`, `event_type`, `state_from`, `state_to`, `payload`, `trace_id`, `created_at`).
    - `JobDetailResponse`: detailed job metadata (`id`, `organization_id`, `message_id`, `thread_id`, `job_type`, `state`, `attempt`, `max_attempts`, `idempotency_key`, `queue_name`, `priority`, `lease_expires_at`, `last_error`, `created_at`, `updated_at`).
    - `JobTimelineResponse`: ordered event history for a job.
    - `JobReplayRequest`: payload schema (`reset_attempts: bool = True`, `reason: str | None = None`).
    - `JobReplayResponse`: replay response (`job_id: UUID`, `organization_id: UUID`, `previous_state: str`, `new_state: str`, `attempt: int`, `republished: bool`, `routing_key: str | None`, `replayed_at: datetime`).
  - In `services/api/schemas/messages.py`:
    - `MessageTimelineResponse`: (`message_id: UUID`, `organization_id: UUID`, `current_state: str | None`, `total_events: int`, `limit: int`, `offset: int`, `events: list[ProcessingEventResponse]`).
- **Verification:**
  - `.venv/bin/mypy services/api/schemas/`

---

### Step 3: Implement Dependencies & Endpoints
- **Files to Create / Modify:**
  - `services/api/dependencies.py`
  - `services/api/routers/messages.py`
  - `services/api/routers/jobs.py` (NEW)
  - `services/api/routers/v1.py`
- **Actions:**
  - In `services/api/dependencies.py`:
    - Add `get_job_store(request: Request) -> Any` and `JobStoreDep`.
  - In `services/api/routers/messages.py`:
    - Add `GET /v1/messages/{id}/timeline`:
      - Verify message exists and matches `org_id` (404 if not found).
      - Fetch ordered events via `job_store.list_events_for_message(org_id, id)`.
      - Paginate using `PaginationParamsDep` (R23.6).
      - Return `MessageTimelineResponse`.
  - In `services/api/routers/jobs.py`:
    - Create `jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])`.
    - `GET /v1/jobs/{id}`: Fetch job details (404 if not found).
    - `GET /v1/jobs/{id}/timeline`: Return ordered event history for a job.
    - `POST /v1/jobs/{id}/replay`:
      - Validate job exists (404 if not found).
      - Validate job state is `DEAD_LETTER` (409 Conflict if not replayable).
      - Call `job_store.replay_job(...)`.
      - If `publisher` in `app.state`: construct `JobEnvelope` and publish to `exchange_email_route` with `routing_key = job.queue_name or "email.triage"`.
      - Return `JobReplayResponse`.
  - In `services/api/routers/v1.py`:
    - Include `jobs_router`.
- **Verification:**
  - `.venv/bin/mypy services/api/`

---

### Step 4: Unit Test Suite for Timeline & Replay
- **Files to Create:**
  - `tests/unit/test_job_timeline_and_replay.py`
- **Actions:**
  - Test `GET /v1/messages/{id}/timeline`:
    - Chronological ordering of events.
    - Offset / limit pagination.
    - 404 for unknown message ID.
    - Tenant isolation (message belongs to org A, org B request gets 404).
    - Missing `X-Organization-ID` returns 400.
  - Test `POST /v1/jobs/{id}/replay`:
    - Successful replay from `DEAD_LETTER` to `RETRY_PENDING`.
    - Attempt counter reset to 0.
    - Event written to timeline with `event_type = 'operator_replay'`.
    - Message republished via mock publisher.
    - Replay rejected with 409 Conflict for non-DEAD_LETTER job (e.g. `COMPLETED`, `GENERATING`).
    - 404 for non-existent job or foreign tenant.
  - Test `GET /v1/jobs/{id}` and `GET /v1/jobs/{id}/timeline`.
- **Verification:**
  - `.venv/bin/pytest tests/unit/test_job_timeline_and_replay.py -v`

---

### Step 5: Live Integration Tests (PostgreSQL 16 & RabbitMQ 3.13)
- **Files to Create:**
  - `tests/integration/test_job_timeline_and_replay_integration.py`
- **Actions:**
  - Test live `GET /v1/messages/{id}/timeline` against PostgreSQL database with real seeded events.
  - Test live `POST /v1/jobs/{id}/replay`:
    - Transitions real PostgreSQL `processing_job` from `DEAD_LETTER` to `RETRY_PENDING`.
    - Publishes real AMQP envelope to RabbitMQ exchange.
    - Verifies message appears in destination queue ready for worker consumption.
- **Verification:**
  - `.venv/bin/pytest tests/integration/test_job_timeline_and_replay_integration.py -v`
  - `.venv/bin/pytest tests/unit tests/integration -q`
  - `.venv/bin/mypy packages/ services/ tests/`
  - `.venv/bin/ruff check packages/ services/ tests/`

---

## 4. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Replaying a job that is already running or succeeded | Enforce explicit check: only `DEAD_LETTER` jobs can be replayed; raise HTTP 409 Conflict otherwise. |
| Duplicate message publication if publisher fails mid-operation | Database transition and event persistence commit first. If publishing fails, log error and return `republished: false` so operator can retry. |
| Cross-tenant leakage of message events or job replay | Strict mandatory `get_organization_id` dependency on all routes and query parameters; queries filter by `organization_id`. |
