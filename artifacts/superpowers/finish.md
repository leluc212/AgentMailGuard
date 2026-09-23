# Final Summary: Task 2.14 — Job Timeline API & Replay

## Verification Commands Run & Results
1. `pytest tests/unit/test_job_timeline_and_replay.py -v`: **14 passed** (100%)
2. `pytest tests/integration/test_job_timeline_and_replay_integration.py -v`: **2 passed** (100%)
3. `pytest tests/unit/test_api_skeleton.py -v`: **16 passed** (100%)
4. `mypy packages/ services/ tests/`: **0 errors** across 200 source files
5. `ruff check packages/ services/ tests/`: **0 errors**
6. `pytest tests/unit tests/integration -q`: **629 passed** (100%)

## Summary of Changes
- **Atomic Job Store Replay Method (`packages/db/job.py`)**: Added `replay_job` to `JobStore` protocol, `PostgresJobStore`, and `InMemoryJobStore`. Enforces `DEAD_LETTER -> RETRY_PENDING` transition using `transition_job`, resets attempt counter to 0 (optional), clears `last_error`, and logs an atomic `ProcessingEvent` with `event_type = 'operator_replay'`.
- **Pydantic Schemas (`services/api/schemas/jobs.py`, `services/api/schemas/messages.py`, `services/api/schemas/__init__.py`)**: Created `ProcessingEventResponse`, `JobDetailResponse`, `JobTimelineResponse`, `JobReplayRequest`, and `JobReplayResponse`. Extended `MessageTimelineResponse` with chronological events and pagination.
- **REST API Endpoints & Routers (`services/api/dependencies.py`, `services/api/routers/messages.py`, `services/api/routers/jobs.py`, `services/api/routers/v1.py`)**:
  - Added `get_job_store` and `JobStoreDep` dependency injection.
  - Implemented `GET /v1/messages/{id}/timeline` returning ordered `processing_event` records with pagination (R18.6, R23.5, R23.6).
  - Implemented `jobs_router` with `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/timeline`, and `POST /v1/jobs/{id}/replay` (R18.7, R23.2).
  - Enforced 409 Conflict rejection when attempting to replay non-`DEAD_LETTER` jobs.
  - Re-published replayed jobs as `JobEnvelope` to RabbitMQ category queue (`job.queue_name`).
- **Publisher Setting Compatibility (`packages/broker/publisher.py`)**: Added `broker_settings` property alias to `MessagePublisher` for backward-compatible attribute access.
- **Automated Tests (`tests/unit/test_job_timeline_and_replay.py`, `tests/integration/test_job_timeline_and_replay_integration.py`)**: Authored 14 unit tests and 2 live PostgreSQL 16 + RabbitMQ 3.13 integration tests verifying event order, tenant scoping, replay transitions, and AMQP redelivery.

## Next Steps
- Mark Task 2.14 complete in `specs/tasks.md`.
- Proceed to Task 2.15: Queue metrics (`specs/tasks.md` lines 282–285).
