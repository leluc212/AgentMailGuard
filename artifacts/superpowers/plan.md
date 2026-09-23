# Implementation Plan - Task 2.13: Lease Reaper for Stuck Jobs

Reclaim processing jobs stuck in a non-terminal state past `lease_expires_at` or timeout, transitioning them back to a retryable state (`RETRY_PENDING`) or dead-lettering (`DEAD_LETTER`) when maximum attempts are exceeded, with strict PostgreSQL row-level concurrency safety (`SKIP LOCKED`), atomic audit event persistence, and Prometheus metrics.

## User Review Required

> [!IMPORTANT]
> - **Concurrency Safety:** PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` guarantees multiple reaper replicas or concurrent workers never contend, deadlock, or double-reap the same stuck job (R20.3).
> - **State Transitions:**
>   - Jobs in `GENERATING` with attempts remaining transition `GENERATING -> RETRY_PENDING`.
>   - Jobs in other non-terminal states (e.g., `CONTEXT_READY`, `QUEUED`) transition `state -> FAILED -> RETRY_PENDING`.
>   - Jobs with exhausted attempts (`attempt >= max_attempts`) transition `state -> FAILED -> DEAD_LETTER`.
> - **AMQP Re-enqueueing:** If a `MessagePublisher` is attached to `LeaseReaper`, reclaimed retryable jobs are published to the retry ladder (or target queue), and exhausted jobs are published to the dead-letter queue with headers preserved. If running without a publisher, atomic database transitions and events are still recorded.

## Open Questions

None. The specifications in `specs/requirements.md` (R19.8, R19.6, R18.2, R18.4, R18.5) and `specs/design.md §9` are exact.

## Proposed Changes

### Configuration & Telemetry

#### [MODIFY] [packages/core/settings.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/core/settings.py)
- Define `LeaseReaperSettings`:
  - `enabled: bool = True`
  - `lease_timeout_s: int = 300` (5 minutes)
  - `reaper_interval_s: float = 30.0` (30 seconds sweep cycle)
  - `batch_size: int = 100`
  - `reap_stuck_unleased: bool = True` (reap unleased jobs stuck in active states past timeout)
- Add `lease_reaper: LeaseReaperSettings` to `AppSettings`.

#### [MODIFY] [packages/observability/metrics.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/observability/metrics.py)
- Add `reaped_leases_total: Counter` instrumented with labels `["action", "state"]` (`action` ∈ `{"reclaimed", "dead_letter"}`).

#### [MODIFY] [.env.example](file:///home/ple/Documents/antigravity/dazzling-bose/.env.example) & [docs/configuration.md](file:///home/ple/Documents/antigravity/dazzling-bose/docs/configuration.md)
- Add `LEASE_REAPER__*` environment variable entries with documentation.

---

### Database Store & Domain

#### [MODIFY] [packages/db/job.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/db/job.py)
- Extend `JobStore` protocol with lease operations:
  - `acquire_lease(organization_id, job_id, lease_timeout_s: int = 300) -> Job | None`
  - `renew_lease(organization_id, job_id, extension_s: int = 300) -> Job | None`
  - `reap_expired_jobs(now: datetime | None = None, batch_size: int = 100, unleased_timeout_s: int | None = None) -> list[tuple[Job, ProcessingEvent, str]]`
- Implement in `PostgresJobStore`:
  - `acquire_lease` and `renew_lease` using atomic SQL `UPDATE processing_job SET lease_expires_at = $1, updated_at = $2 WHERE id = $3 AND organization_id = $4 AND state NOT IN ('COMPLETED', 'DEAD_LETTER')`.
  - `reap_expired_jobs` using a single transaction with:
    - `SELECT ... FROM processing_job WHERE state NOT IN ('COMPLETED', 'DEAD_LETTER') AND ((lease_expires_at IS NOT NULL AND lease_expires_at <= $1) OR ($3::boolean AND lease_expires_at IS NULL AND state IN ('GENERATING', 'CONTEXT_READY') AND updated_at <= $2)) ORDER BY COALESCE(lease_expires_at, updated_at) ASC LIMIT $4 FOR UPDATE SKIP LOCKED;`
    - Transition state via domain state machine.
    - Increment `attempt`, clear `lease_expires_at = NULL`, set `last_error`.
    - Atomically insert `ProcessingEvent` in the same transaction.
- Implement matching in-memory locking semantics in `InMemoryJobStore`.

---

### Broker Lease Reaper & Consumer Integration

#### [NEW] [packages/broker/lease_reaper.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/lease_reaper.py)
- Implement `LeaseReaper` class:
  - `reap_once() -> list[tuple[Job, ProcessingEvent, str]]`:
    - Calls `job_store.reap_expired_jobs(...)`.
    - Increments `metrics.reaped_leases_total` per reaped job.
    - If `publisher` present: constructs `JobEnvelope` and publishes to retry exchange (if reclaimed) or DLQ (if dead-lettered).
  - `start() -> None`: starts background periodic sweep task.
  - `stop() -> None`: cancels and drains task cleanly.

#### [MODIFY] [packages/broker/consumer.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/consumer.py)
- In `_handle_message`: automatically take a lease on job claim via `job_store.acquire_lease` if `self.job_store` is present.

#### [MODIFY] [packages/broker/batch_consumer.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/batch_consumer.py)
- In batch processing: acquire lease for each message in the batch upon claim.

---

### Verification Plan

#### Automated Tests
- Unit tests in `tests/unit/test_lease_reaper.py`:
  - Test lease acquisition and renewal on active jobs.
  - Test lease expiry detection and transition `GENERATING -> RETRY_PENDING`.
  - Test non-expired leases remain untouched.
  - Test retry limit exhaustion transitioning to `DEAD_LETTER`.
  - Test unleased stuck job fallback detection.
  - Test `LeaseReaper` sweep with `MessagePublisher` for retry and DLQ routing.
  - Test `LeaseReaper` background start/stop lifecycle.
  - Test Prometheus counter `reaped_leases_total`.
- Live integration tests in `tests/integration/test_lease_reaper_integration.py`:
  - Live PostgreSQL test (`PostgresJobStore`):
    - Multi-worker concurrency with `SELECT ... FOR UPDATE SKIP LOCKED` verifying zero double-reaping.
    - State transitions and audit trail in real `processing_job` and `processing_event` tables.
  - Live RabbitMQ integration test:
    - Expired lease job reaped and republished to RabbitMQ retry exchange, then consumed after backoff.
- Full regression suite:
  - `.venv/bin/pytest tests/unit tests/integration -q`
- Static analysis & linting:
  - `.venv/bin/mypy packages/ tests/`
  - `.venv/bin/ruff check packages/ tests/`
