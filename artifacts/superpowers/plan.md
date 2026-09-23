# Implementation Plan — Phase 2 Task 2.15: Queue Metrics

**Spec Alignment:** `specs/requirements.md` (R7.5, R21.4, R20.5) · `specs/tasks.md` Task 2.15 · `specs/design.md §7, §10` · `GEMINI.md`

---

## 1. Goal & Architecture Overview

Export per-queue depth (pending message count) and wait time (latency from enqueue to consumer claim) as Prometheus metrics exposed at `/metrics`, satisfying requirements for pipeline telemetry (R7.5, R21.4) and infrastructure autoscaling signals (R20.5).

```
               PROMETHEUS SCRAPER / AUTOSCALER (KEDA)
                               │
                      GET /metrics (HTTP)
                               │
                               ▼
                    ┌──────────────────────┐
                    │  FastAPI /metrics    │
                    │  CollectorRegistry   │
                    └──────────▲───────────┘
                               │
            ┌──────────────────┴──────────────────┐
            │                                     │
   queue_depth (Gauge)                   queue_wait_ms (Histogram)
            │                                     │
            │ set(message_count)                  │ observe(wait_ms)
            │                                     │
 ┌───────────────────────┐             ┌─────────────────────────┐
 │     QueueMonitor      │             │ BaseConsumer / Batch    │
 │  (async AMQP poller)  │             │ (upon message claim)    │
 └──────────┬────────────┘             └────────────┬────────────┘
            │                                       │
            │ passive declare                       │ (now - enqueued_at)
            ▼                                       ▼
 ┌───────────────────────────────────────────────────────────────┐
 │                         RabbitMQ 3.13                         │
 │ - email.sync, email.normalize, email.triage, email.dispatch   │
 │ - email.<category>.<priority> (e.g. email.support.normal)     │
 │ - email.retry.30s, email.retry.5m, email.retry.30m            │
 │ - email.dead_letter                                           │
 └───────────────────────────────────────────────────────────────┘
```

---

## 2. User Review Required

> [!IMPORTANT]
> - **AMQP Passive Declaration:** Queue depths are sampled passively via `aio_pika.Channel.declare_queue(queue_name, passive=True)`. This queries RabbitMQ broker metadata without altering queues, consuming messages, or requiring management HTTP credentials/ports.
> - **Error Resilience:** In AMQP 0-9-1, passively declaring a non-existent queue raises `ChannelNotFoundEntity` and closes the channel. The `QueueMonitor` explicitly catches this, logs a debug note, sets `queue_depth` to 0, and reopens the channel so other queues continue sampling without interruption.
> - **Autoscaling Metric Conformity:** The gauge is named `queue_depth` with label `["queue"]`, matching `R21.4` and `specs/design.md §10`, allowing autoscalers (KEDA / Prometheus Adapter) to query `queue_depth{queue="..."}` directly.

---

## 3. Proposed Changes

### Component 1: Observability Metrics (`packages/observability`)

#### [MODIFY] [packages/observability/metrics.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/observability/metrics.py)
- Expand `QUEUE_WAIT_BUCKETS` to include high-backlog ranges up to 60s:
  `(5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0, 30000.0, 60000.0)`.
- Ensure `queue_depth` (Gauge) and `queue_wait_ms` (Histogram) are properly configured with label `["queue"]`.
- Add `queue_consumers` (Gauge with label `["queue"]`) to capture active consumer counts.

---

### Component 2: Broker Consumers (`packages/broker`)

#### [MODIFY] [packages/broker/consumer.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/consumer.py)
- Accept optional `metrics: PipelineMetrics | None = None` in `BaseConsumer.__init__`, defaulting to `get_metrics()`.
- In `BaseConsumer._handle_message`, after successfully parsing `JobEnvelope`:
  - Calculate `wait_ms = max(0.0, (datetime.now(UTC) - enqueued_at).total_seconds() * 1000.0)`.
  - Observe `self.metrics.queue_wait_ms.labels(queue=self.queue_name).observe(wait_ms)`.

#### [MODIFY] [packages/broker/batch_consumer.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/batch_consumer.py)
- Accept optional `metrics: PipelineMetrics | None = None` in `BaseBatchConsumer.__init__`.
- In `BaseBatchConsumer._handle_single_item(item)`:
  - Calculate `wait_ms = max(0.0, (datetime.now(UTC) - item.envelope.enqueued_at).total_seconds() * 1000.0)`.
  - Observe `self.metrics.queue_wait_ms.labels(queue=self.queue_name).observe(wait_ms)`.

---

### Component 3: Queue Depth Monitor (`packages/broker`)

#### [NEW] [packages/broker/queue_monitor.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/queue_monitor.py)
- Implement `get_monitored_queues(broker_settings=None, routing_settings=None) -> list[str]`:
  - Discovers core queues (`email.sync`, `email.normalize`, `email.triage`, `email.dispatch`, `knowledge.ingest`, `email.dead_letter`).
  - Discovers retry queues (`email.retry.30s`, `email.retry.5m`, `email.retry.30m`).
  - Discovers category priority lanes (`email.<category>.<lane>` for all taxonomy categories and lanes).
- Implement `QueueMonitor`:
  - `__init__(connection=None, broker_settings=None, metrics=None, queues=None, shutdown_coordinator=None)`
  - `async def sample_queue_depths() -> dict[str, int]`:
    - Iterates over all queues using a dedicated channel.
    - Runs `declare_queue(queue_name, passive=True)`.
    - Updates `self.metrics.queue_depth.labels(queue=queue_name).set(q.declaration_result.message_count)`.
    - Updates `self.metrics.queue_consumers.labels(queue=queue_name).set(q.declaration_result.consumer_count)`.
    - Re-establishes channel if closed due to non-existent queue.
    - Returns `{queue_name: message_count}` mapping.
  - `async def start(interval_seconds: float = 10.0)` / `async def stop()`:
    - Runs background async polling task.
    - Clean shutdown via cancellation or `shutdown_coordinator`.

#### [MODIFY] [packages/broker/__init__.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/__init__.py)
- Export `QueueMonitor` and `get_monitored_queues`.

---

### Component 4: Test Suite

#### [NEW] [tests/unit/test_queue_metrics.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_queue_metrics.py)
- Unit tests:
  1. `test_consumer_records_queue_wait_ms`: Verify `BaseConsumer` observes `queue_wait_ms` upon receiving a message with an earlier `enqueued_at`.
  2. `test_batch_consumer_records_queue_wait_ms`: Verify `BaseBatchConsumer` observes `queue_wait_ms` on individual batch item processing.
  3. `test_queue_monitor_samples_depths`: Mock channel passive declaration and verify gauges are updated.
  4. `test_queue_monitor_recovers_missing_queue`: Ensure `ChannelNotFoundEntity` sets depth to 0 and reopens the channel safely.
  5. `test_get_monitored_queues_completeness`: Verify core, retry, and category queues are included.

#### [NEW] [tests/integration/test_queue_metrics_integration.py](file:///home/ple/Documents/antigravity/dazzling-bose/tests/integration/test_queue_metrics_integration.py)
- Live integration tests against RabbitMQ 3.13:
  1. `test_live_queue_depth_sampling`:
     - Declare topology idempotently.
     - Publish 3 messages to `email.support.normal` and 2 messages to `email.dead_letter`.
     - Execute `QueueMonitor.sample_queue_depths()`.
     - Assert `metrics.queue_depth.labels(queue="email.support.normal") == 3`.
     - Assert `metrics.queue_depth.labels(queue="email.dead_letter") == 2`.
  2. `test_live_queue_wait_time_recording_and_metrics_endpoint`:
     - Publish message with `enqueued_at` set 500ms in the past.
     - Run a test consumer to consume the message.
     - Verify `queue_wait_ms` has count >= 1 and sum >= 500ms.
     - Fetch Prometheus `/metrics` payload and verify `queue_depth` and `queue_wait_ms` lines exist with expected labels.

---

## 4. Verification Plan

### Automated Tests
1. `uv run pytest tests/unit/test_queue_metrics.py tests/unit/test_observability_metrics.py -v`
2. `uv run pytest tests/integration/test_queue_metrics_integration.py -v`
3. `uv run ruff check packages/broker packages/observability tests/unit/test_queue_metrics.py tests/integration/test_queue_metrics_integration.py`
4. `uv run mypy packages/broker packages/observability`
5. Full Phase 2 verification run:
   `uv run pytest tests/unit tests/integration -m "not slow" -v`
