# Finish Summary — Phase 2 Task 2.15: Queue Metrics

**Spec Alignment:** `specs/requirements.md` (R7.5, R21.4, R20.5) · `specs/tasks.md` Task 2.15 · `specs/design.md §7, §10` · `GEMINI.md`

---

## 1. Code Review (Blocker / Major / Minor / Nit)

- **Blocker:** None.
- **Major:** None.
- **Minor:** None.
- **Nit:** None.
  - All metrics conform strictly to R21.4 specification names (`queue_depth`, `queue_wait_ms`).
  - AMQP passive inspection does not alter queue states or consume in-flight messages.
  - Non-existent queue errors (`ChannelNotFoundEntity`) are handled cleanly with zeroed gauge values and automatic channel re-establishment.
  - Negative wait times from potential clock skew are safely clamped to `0.0ms`.
  - Type hints and linting pass with zero errors across all modified and newly created files.

---

## 2. Summary of Changes

### Observability Metrics
- **`packages/observability/metrics.py`**:
  - Expanded `QUEUE_WAIT_BUCKETS` with latency buckets up to 60,000ms: `(5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0, 30000.0, 60000.0)`.
  - Added `queue_consumers` Gauge labeled with `["queue"]`.
  - Configured `queue_depth` Gauge and `queue_wait_ms` Histogram with `["queue"]` labels per R7.5, R20.5, and R21.4.

### Broker Consumer Infrastructure
- **`packages/broker/consumer.py`**:
  - Added `metrics: PipelineMetrics | None = None` parameter to `BaseConsumer.__init__` (defaults to global singleton `get_metrics()`).
  - In `_handle_message`, computes message wait time in milliseconds (`(now - enqueued_at) * 1000`) and observes it into `queue_wait_ms`.
- **`packages/broker/batch_consumer.py`**:
  - Added `metrics` parameter to `BaseBatchConsumer.__init__`, forwarding to `super().__init__`.
  - In `_handle_single_item`, records `queue_wait_ms` for each independently processed item.

### Background Queue Depth Monitor
- **`packages/broker/queue_monitor.py`** [NEW]:
  - `get_monitored_queues()`: Automatically discovers core stage queues (`email.sync`, `email.normalize`, `email.triage`, `email.dispatch`, `knowledge.ingest`, `email.dead_letter`), retry tier queues (`email.retry.30s`, `email.retry.5m`, `email.retry.30m`), and taxonomy category priority lanes (`email.<category>.<lane>`).
  - `QueueMonitor`: Passively queries RabbitMQ message and consumer counts via `aio_pika.Channel.declare_queue(queue_name, passive=True)` without consuming messages. Safely handles missing queues by zeroing metrics and reopening channels. Supports background async polling and graceful shutdown.
- **`packages/broker/__init__.py`**:
  - Exported `QueueMonitor` and `get_monitored_queues` in `__all__`.

### Test Suite
- **`tests/unit/test_queue_metrics.py`** [NEW]:
  - 7 unit tests verifying wait time recording in standard and batch consumers, depth gauge updates, missing queue channel recovery, queue discovery completeness, monitor lifecycle, and clock skew clamping.
- **`tests/integration/test_queue_metrics_integration.py`** [NEW]:
  - 2 live integration tests against RabbitMQ 3.13 verifying actual queue depths across multiple queues (`email.support.normal`, `email.dead_letter`), consumer wait time recording, and `/metrics` Prometheus exposition formatting.

### Task Tracking
- **`specs/tasks.md`**:
  - Marked Task 2.15 complete `[x]`.

---

## 3. Verification Commands Run & Results

1. **Unit Tests:**
   - `uv run pytest tests/unit/test_queue_metrics.py tests/unit/test_observability_metrics.py -v`
   - **Result:** 10/10 passed in 0.38s.
2. **Integration Tests (Live RabbitMQ 3.13):**
   - `uv run pytest tests/integration/test_queue_metrics_integration.py -v`
   - **Result:** 2/2 passed in 0.84s.
3. **Static Type Checking (mypy):**
   - `uv run mypy packages/broker packages/observability`
   - **Result:** 0 issues across 21 source files.
4. **Code Quality / Linting (ruff):**
   - `uv run ruff check packages/broker packages/observability tests/unit/test_queue_metrics.py tests/integration/test_queue_metrics_integration.py`
   - **Result:** All checks passed.
5. **Phase 2 Gate & Complete Regression Suite:**
   - `uv run pytest tests/unit tests/integration -m "not slow" -q`
   - **Result:** 638/638 passed (100% green).

---

## 4. Phase 2 Gate Status

All criteria for the **Phase 2 gate** in `specs/tasks.md` are satisfied and verified by automated tests:
1. Newsletter fixture terminates at `COMPLETED` with zero AI calls (`test_triage_engine.py`, `test_funnel_metrics.py`).
2. Acknowledgement fixture produces a template reply with zero retrieval and zero generation calls (`test_early_exit_gate.py`).
3. Support fixture lands in `email.support.normal` (`test_routing.py`, `test_cascading_triage_integration.py`).
4. Killing a worker mid-job results in redelivery and exactly one logical result (`test_lease_reaper_integration.py`).
5. A poisoned job reaches the DLQ with its reason intact and can be replayed (`test_retry_dead_letter_integration.py`, `test_job_timeline_and_replay_integration.py`).
6. Per-queue depth and wait time exported to Prometheus at `/metrics` (`test_queue_metrics_integration.py`).

Phase 2 is now **100% complete**.
