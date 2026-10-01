# Runtime Assembly & Delivery Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every service that already has code actually run in `docker compose`, and close every path where the broker or an API endpoint silently loses a message.

**Architecture:** A shared `WorkerRuntime` owns each worker process's lifecycle: settings, logging, database pool, robust broker connection, topology declaration, publisher, health server, and graceful drain. Each service contributes only a `build_components` function. The retry ladder returns messages to their origin exchange through a new `retry.return` headers exchange, restoring `specs/design.md` §7.2. Consumers dead-letter unparseable bodies verbatim, publishers raise on unroutable messages, and shutdown stops consuming before it closes channels. Integration tests move to a dedicated vhost and database, so the now-running workers never compete with them.

**Tech Stack:** Python 3.12, uv 0.12.19, aio-pika 10.0.1 / aiormq 7.0.0, RabbitMQ 3.13.7, asyncpg 0.31, FastAPI 0.141, pydantic-settings 2.15, pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode = "auto"`), Docker Compose 5.5 with the classic builder (no buildx on this host).

**Spec:** There is no separate design spec. This plan implements the "W1 make it runnable" and "W2 stop message loss" workstreams of `artifacts/superpowers/2026-09-26-project-scouting-audit.md`. It argues from these sources:
- `specs/requirements.md`: R3.1–R3.5, R5.10, R7.1, R7.5, R7.6, R18.7, R19.8, R20.1, R20.7, R20.8, R23.7, R24.4, R24.5.
- `specs/design.md`: §3.2 (runtime processes), §7.1–§7.2 (topology and retry ladder), §9 (recovery), §13 (deployment).
- The eight research briefs verified at `1ecf7b1`.

Every task cites the requirement IDs it closes.

---

## Global Constraints

- **Binding rules.** `CLAUDE.md` is binding. `services/*` may import `packages/*`, never the reverse. `packages/domain` imports stdlib only. Provider literals `gmail`, `graph` and `imap` must not appear as string literals in `services/**`; `tests/unit/test_dependency_rules.py` enforces this.
- **Commit messages.** Use `type(scope): summary [task RA.n] [R<id>, ...]`, one commit per task step group. End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **pytest.** `--strict-markers` is on and no custom markers are registered. Do not add markers. Plain `async def` tests run in auto mode; `@pytest.mark.asyncio` is allowed because the plugin registers it.
- **Types and style.** mypy is `strict = true` and covers `tests/`, so annotate every test function and fake. Ruff line length is 100.
- **Pytest verbosity.** `pyproject.toml` `addopts` already contains `-q`. Run pytest without adding another `-q`, or the pass-count summary line disappears.
- **Verify every task with these commands.** The repository-wide `make lint` target is red *before* this plan starts: 14 mypy errors in 6 test files, and 40 files fail `ruff format --check`. Fixing that is out of scope. Do not "fix" unrelated files, and do not let touched files add new errors.
  - `uv run pytest tests/unit`
  - `uv run ruff check .`
  - `uv run mypy packages services`
  - `uv run ruff format --check <files you touched>`
  - `uv run mypy <test files you touched>`
- **Pinned versions.** aio-pika is **10.0.1**, not 9.x. Use only the APIs quoted in this plan. They were verified against the installed source.
- **The live stack is not a test target.** Integration tests must never touch RabbitMQ vhost `/` or database `rag_email`. After Task 3 they run in vhost `rag_email_test` and database `rag_email_test`, and a guard raises `IsolationError` otherwise.
- **Image layout.** The image's working directory is `/app`. Config, prompt, schema and model paths are relative to the working directory, and migrations resolve via `packages/db/migrator.py` `__file__.parents[2]`. Keep the editable install.
- **Metrics registry.** Runtime code must use `packages.observability.metrics.get_metrics()`. Tests use `create_pipeline_metrics(registry=CollectorRegistry())`.
- **Actions that touch the running dev stack.** Ask the user before running any of these: `make up`, `make broker-migrate-retry`, `docker compose up|down|build|restart`. They restart or alter the live dev stack.

## Review Focus

These five conditions follow from the requirements, but no task's normal tests would exercise them. Each one is pinned by a test in the task named.

1. **A dev broker that still has the old retry queues.** Today's `email.retry.*` queues were declared with `x-dead-letter-exchange=email.route`. Redeclaring them with new arguments raises 406 PRECONDITION_FAILED. A user expects a clear error that names the fix, not a crash loop. Pinned by `test_setup_topology_explains_stale_retry_queue_arguments` (Task 4).
2. **An actionable email classified into a category with no bound queue.** A user expects the job to end up in the dead-letter queue with a reason, never to vanish. Pinned by `test_unroutable_publish_raises` (Task 6).
3. **SIGTERM while a job is mid-flight.** A user expects the job to finish and be acked if it completes within the drain window, and to be requeued (never half-acked) if it does not. Pinned by `test_in_flight_job_finishing_after_shutdown_is_acked` and `test_cleanup_closes_channel_when_drain_times_out` (Task 7).
4. **A sync request whose mailbox is unresolvable or belongs to another tenant.** The webhook falls back to a nil organization. A user expects a dead-letter with the reason, not a retry loop and not a cross-tenant sync. Pinned by `test_tenant_mismatch_is_fatal` and `test_unresolvable_mailbox_id_is_fatal` (Task 11).
5. **An ack that fails because the channel was lost.** A user expects the broker to redeliver, and no second copy to be published through the retry ladder. Pinned by `test_failed_ack_does_not_publish_a_retry` (Task 6).

---

## Design decisions

| # | Decision | Why |
|---|---|---|
| D1 | Remove the three `testing.py` re-exports from package `__init__` files, and lazy-import `evaluation` inside the triage `train_triage_model` | 93 of 156 production modules crash without pytest (measured). Tests keep importing `packages.x.testing` directly. |
| D2 | Package-scoped autouse fixture moves integration tests to vhost/database `rag_email_test`, resets both each session, and installs connection guards. `scratch_vhost()` gives tests needing non-default retry TTLs their own vhost | Workers in compose consume vhost `/` and database `rag_email`. One test drops every table in the live database. |
| D3 | Retry queues dead-letter into a `retry.return` **headers** exchange (alternate-exchange `dlx.email`). One exchange-to-exchange binding per origin exchange matches header `retry-origin-exchange` | RabbitMQ keeps the routing key and headers on dead-lettering. The `x-*` headers are ignored by headers matching, so the header has no `x-` prefix. This restores design §7.2, "dead-letters back to the origin exchange". |
| D4 | The one-time migration of existing retry queues is `make broker-migrate-retry` (`rabbitmqctl delete_queue --if-empty`). `setup_topology` raises `RetryTopologyMigrationError` that names it | Queue arguments are immutable, and a policy cannot override an argument. `--if-empty` refuses to drop messages. |
| D5 | Channels created by `MessagePublisher.connect()` and consumers use `on_return_raises=True`. Consumers take the queue with `declare_queue(passive=True)` | With the aio-pika defaults, unroutable mandatory publishes succeed silently, and `get_queue(ensure=False)` queues are not re-consumed after a reconnect. |
| D6 | `BaseConsumer` registers `stop_consuming` as its drain callback and `close` as its cleanup callback. The success ack moves out of the `try` | Closing the channel during drain cancels in-flight callbacks. An ack failure inside the `try` published a duplicate retry. |
| D7 | Replay and knowledge upload refuse (409/503) rather than report success when they cannot publish. The destination comes from `BrokerSettings.exchange_for_queue` | Replay published to an unbound key and returned `republished=True`. Upload returned 202 when publish failed. |
| D8 | New `packages/broker/worker_runtime.py`, plus `python -m packages.broker.cli declare` for the compose `init` job | Four worker mains would otherwise duplicate the same ~80 lines, and none of them declared topology. |
| D9 | Mail-connector hosts the sync consumer, subscription renewal, queue monitor and the lease reaper. The reaper stays **off by default** (settings default flips to `false`) | The reaper today re-drives parked jobs wrongly: leases are never cleared and `queue_name` is never written. Enabling it is W3 work. |
| D10 | Missing triage rules, templates or model files stop the triage worker at startup (`TriageWorkerConfigError`) | Each of those fails *silently* today: empty rule engine, template body = path string, stage 2 skipped. |

**Explicitly not in this plan** (tracked for W3/W4; each is a recorded audit finding):
- **Retry jitter (R19.5).** Jitter through per-message expiry cannot work, because messages expire only at the head of the queue. It needs per-tier slot queues or an ADR accepting fixed tiers. The dead `jittered_delay_s` computation stays untouched. *Your decision.*
- **Recording `processing_job.queue_name` and carrying the original payload in replay.** After this plan, replay refuses jobs without a routable `queue_name`, which is every production job today. That refusal is honest, but it means replay is unusable until W3.
- **Redelivery idempotency** in the email and triage consumers, and a transaction seam across stores.
- **Ingestion correctness:** R2.9 follow-up wipe, Gmail `history_id`, stale sync locks. **Knowledge versioning.** **Retrieval query shape.** **Tenant scoping** in mailbox and checkpoint stores.
- **A working "mock providers" mode in compose.** `PROVIDERS__MOCK_PROVIDERS` is read by nothing, and the DB forbids `provider='fake'`. The end-to-end check therefore injects mail after the provider step.
- **CI hygiene.** CI still triggers only on `master` and `main`. The 14 pre-existing mypy errors and 40 unformatted files remain.

---

## File structure

```
packages/
  llm/__init__.py, retrieval/__init__.py, adapters/__init__.py   M  drop testing re-exports (T2)
  core/settings.py        M  BrokerSettings.exchange_retry_return + exchange_for_queue (T4),
                             TriageSettings.templates_path (T10), LeaseReaperSettings.enabled=False (T11)
  broker/publisher.py     M  retry-origin header, tier mapping from settings, raw DLQ publish,
                             channel(on_return_raises=True) (T4, T5, T6)
  broker/topology.py      M  retry.return exchange + bindings, RetryTopologyMigrationError (T4)
  broker/retry.py         M  origin fallback via exchange_for_queue (T4)
  broker/consumer.py      M  origin resolution, raw DLQ, passive declare, ack placement,
                             stop_consuming/close (T4–T7)
  broker/batch_consumer.py M same as consumer (T4–T7)
  broker/worker_runtime.py C  WorkerRuntime, WorkerResources, declare_topology (T9)
  broker/cli.py           C  `python -m packages.broker.cli declare` (T9)
services/
  triage_worker/training.py  M lazy evaluation import (T2)
  triage_worker/main.py      C entrypoint (T10)
  email_worker/main.py       M rewritten on WorkerRuntime (T9)
  knowledge_worker/main.py   M rewritten on WorkerRuntime + R5.10 check (T9)
  mail_connector/consumer.py   C MailSyncConsumer (T11)
  mail_connector/background.py C BackgroundLoop (T11)
  mail_connector/orchestrator.py M full_resync + resolver-inside-try (T11)
  mail_connector/main.py       C entrypoint (T11)
  api/routers/jobs.py        M replay routing + failure handling (T8)
  api/routers/knowledge.py   M upload enqueue failure → 503 + compensation (T8)
tests/integration/isolation.py, conftest.py   C  test isolation (T3)
Dockerfile, .dockerignore, pyproject.toml, uv.lock   M  runtime image (T12)
docker-compose.yml, Makefile, .env.example            M  wiring (T4, T13)
scripts/image_smoke.py, scripts/stack_smoke.py        C  real-image and live-stack checks (T12, T14)
specs/tasks.md, specs/design.md, docs/configuration.md, README.md   M  tracking and docs
```

---

### Task 1: Record the inserted work block in the task queue

`CLAUDE.md` §7 says: "When you discover missing work: add a task to `specs/tasks.md` with requirement IDs." The block goes **before** `# Phase 4 — Context & Generation`, so "next unchecked task in order" picks it up before 4.11.

**Files:**
- Modify: `specs/tasks.md` (insert between the `---` on line 372 and `# Phase 4 — Context & Generation` on line 374)

**Interfaces:**
- Produces: task IDs `RA.1`–`RA.13` that every later commit cites.

- [ ] **Step 1: Insert the block**

Insert this text immediately before the line `# Phase 4 — Context & Generation`, followed by a blank line and a `---` line:

```markdown
# Runtime Assembly & Delivery Safety (inserted 2026-09-26, before resuming Phase 4)

*Discovered missing work (CLAUDE.md §7). Phases 0–4 built components that no running process hosts, and several broker and API paths drop messages silently. Evidence: `artifacts/superpowers/2026-09-26-project-scouting-audit.md`. Plan: `docs/superpowers/plans/2026-09-26-runtime-assembly-and-delivery-safety.md`.*

- [ ] **RA.1 Production import path free of dev-only modules**
  - No production module imports pytest, `tests`, or `evaluation`; a subprocess import walk proves it.
  - _Requirements: R20.1_

- [ ] **RA.2 Integration tests isolated from the running stack**
  - Dedicated vhost and database reset per session; guards reject vhost `/` and database `rag_email`.
  - _Requirements: R24.4 (partial: dedicated vhost/database on the shared dev servers, not ephemeral containers)_

- [ ] **RA.3 Retry ladder returns messages to their origin exchange**
  - `retry.return` headers exchange with alternate exchange `dlx.email`; one-time migration target for existing retry queues.
  - _Requirements: R3.5, R19.5 (partial: fixed retry tiers, no jitter), R19.6_

- [ ] **RA.4 Unparseable deliveries dead-lettered verbatim**
  - _Requirements: R3.5, R3.3_

- [ ] **RA.5 Unroutable publishes raise; consumers resume after reconnect; failed acks never duplicate**
  - _Requirements: R3.1, R3.3, R7.1_

- [ ] **RA.6 Graceful drain: stop consuming → drain in-flight → close**
  - _Requirements: R20.8, R3.3_

- [ ] **RA.7 API publish paths never report lost work as success**
  - Replay routes via the queue→exchange resolver or refuses; upload returns 503 and compensates when it cannot enqueue.
  - _Requirements: R18.7, R23.7, R9.1_

- [ ] **RA.8 Shared worker runtime; topology declared at startup; R5.10 check hosted**
  - _Requirements: R3.2, R20.7, R20.8, R5.10_

- [ ] **RA.9 Triage worker entrypoint**
  - _Requirements: R6.1, R6.2, R6.5, R7.1, R3.4_

- [ ] **RA.10 Mail connector entrypoint and background jobs**
  - Sync consumer, subscription renewal, queue monitor; lease reaper hosted but disabled by default.
  - _Requirements: R2.1, R2.10, R2.11, R7.5, R19.8 (partial: hosted but disabled by default), R23.6_

- [ ] **RA.11 Production image from the lockfile with every runtime asset**
  - _Requirements: R20.1, R24.1_

- [ ] **RA.12 Compose wiring: commands, init job, readiness healthchecks, drain grace**
  - _Requirements: R20.1, R20.7, R20.8, R3.2_

- [ ] **RA.13 Live-stack smoke check and documentation**
  - _Requirements: R24.7 (partial: ingestion → triage), R20.1_

> **RA gate:** `make up` builds images from HEAD, and api, mail-connector, email-worker, triage-worker and knowledge-worker all report ready. `mail.sync.requested`, `email.normalize`, `email.triage` and `knowledge.ingest` each have ≥1 consumer. `make smoke` does three things: it drives a billing email through normalize → triage into `email.billing.*`; it drives a no-reply newsletter to `COMPLETED` with zero AI work; and it sends a cross-tenant sync request to `email.dead_letter` with its reason. `uv run pytest tests/integration` passes without touching vhost `/` or database `rag_email`.
```

- [ ] **Step 2: Verify the file still parses as the queue expects**

Run: `grep -cE '^- \[ \] \*\*RA\.' specs/tasks.md`
Expected: `13`

- [ ] **Step 3: Commit**

```bash
git add specs/tasks.md artifacts/superpowers/2026-09-26-project-scouting-audit.md docs/superpowers/plans/2026-09-26-runtime-assembly-and-delivery-safety.md
git commit -m "docs(tasks): insert runtime assembly and delivery safety block [task RA] [R3.2, R20.1]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Production import path free of dev-only modules (RA.1)

Today, 93 of 156 production modules raise `ModuleNotFoundError: pytest` in the image. The chain is `packages/broker/__init__.py:12` → `prompt_safety.py:15` → `packages/llm/__init__.py:55` → `packages/llm/testing.py:13 import pytest`. After that is fixed, `services.triage_worker.*` still fail, via `training.py:29-30` importing `evaluation`, which is not in the image.

**Files:**
- Create: `tests/unit/test_production_imports.py`
- Modify: `packages/llm/__init__.py` (line 55 and the `"LLMProviderContractSuite",` entry in `__all__`, line 93)
- Modify: `packages/retrieval/__init__.py` (line 43 and the `"SearchBackendContractSuite",` entry in `__all__`, line 69)
- Modify: `packages/adapters/__init__.py` (lines 38-41, the blank line after them, and the `"MailProviderAdapterContractSuite",` entry in `__all__`, line 52)
- Modify: `tests/unit/test_search_backend_contract.py:13-18`
- Modify: `tests/integration/test_postgres_search_backend.py:29-35`
- Modify: `services/triage_worker/training.py` (lines 17, 29-30, 180-183, 193)

**Interfaces:**
- Produces: the guarantee that `import services.<x>.main` works without dev dependencies. Later tasks add entrypoints; the walk picks them up automatically.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_production_imports.py`:

```python
"""Production import-path guard (RA.1, R20.1).

Every module shipped in the runtime image (``packages/`` and ``services/``) must import
without dev-only dependencies (pytest and its plugins are never installed in the image)
and without repo-root trees the image does not contain (``evaluation/``, ``tests/``).

The probe runs in a fresh interpreter so the pytest already loaded in this process
cannot mask a missing import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_ROOTS = ("packages", "services")
DEV_ONLY_ROOTS = ("pytest", "_pytest", "pytest_asyncio", "pytest_cov", "pytest_mock", "mypy")
TEST_SUPPORT_MODULE = "testing"  # packages/*/testing.py: contract suites, test-only by design

ENTRYPOINTS = (
    "services.api.main",
    "services.email_worker.main",
    "services.knowledge_worker.main",
    "services.triage_worker.consumer",
    "services.mail_connector.orchestrator",
    "packages.context.builder",
)

_PROBE = textwrap.dedent(
    """
    import importlib
    import importlib.abc
    import json
    import sys
    import traceback

    blocked = frozenset(json.loads(sys.argv[1]))
    modules = json.loads(sys.argv[2])
    first_party = tuple(json.loads(sys.argv[3]))


    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.partition(".")[0] in blocked:
                raise ModuleNotFoundError(
                    f"No module named {fullname!r} (not available in the production image)",
                    name=fullname,
                )
            return None


    sys.meta_path.insert(0, _Blocker())
    failures = {}
    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:
            tb = traceback.extract_tb(exc.__traceback__)
            frame = next((f for f in reversed(tb) if f.filename != "<string>"), tb[-1])
            where = f"{frame.filename}:{frame.lineno}"
            failures[name] = f"{type(exc).__name__}: {exc} (raised at {where})"
            # A failed package __init__ leaves its already-imported submodules cached, which
            # would let later imports of the same chain succeed; start the next one clean.
            for key in [k for k in sys.modules if k.partition(".")[0] in first_party]:
                del sys.modules[key]
    print(json.dumps(failures))
    """
)


def _unshipped_repo_packages() -> list[str]:
    return sorted(
        p.name
        for p in REPO_ROOT.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file() and p.name not in SHIPPED_ROOTS
    )


def _production_modules() -> list[str]:
    names: list[str] = []
    for root in SHIPPED_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts[-1] == TEST_SUPPORT_MODULE:
                continue
            names.append(".".join(parts))
    return names


def _probe(modules: list[str], blocked: list[str], cwd: Path) -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}  # mirrors Dockerfile ENV PYTHONPATH=/app
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            json.dumps(blocked),
            json.dumps(modules),
            json.dumps(list(SHIPPED_ROOTS)),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, str] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def test_probe_blocks_dev_only_and_unshipped_modules(tmp_path: Path) -> None:
    """The probe itself must be able to fail; otherwise the guard below is vacuous."""
    blocked = [*DEV_ONLY_ROOTS, *_unshipped_repo_packages()]
    failures = _probe(["pytest", "evaluation"], blocked, tmp_path)
    assert set(failures) == {"pytest", "evaluation"}


def test_entrypoints_are_covered_by_module_walk() -> None:
    """Renaming an entrypoint must not silently drop it from the import guard."""
    missing = sorted(set(ENTRYPOINTS) - set(_production_modules()))
    assert not missing, f"entrypoints not found under {SHIPPED_ROOTS}: {missing}"


def test_production_modules_import_without_dev_or_unshipped_dependencies(
    tmp_path: Path,
) -> None:
    blocked = [*DEV_ONLY_ROOTS, *_unshipped_repo_packages()]
    modules = [*ENTRYPOINTS, *(m for m in _production_modules() if m not in ENTRYPOINTS)]
    failures = _probe(modules, blocked, tmp_path)
    report = "\n".join(f"  {name}: {reason}" for name, reason in sorted(failures.items()))
    assert not failures, f"{len(failures)} production module(s) fail to import:\n{report}"
```

`cwd=tmp_path` keeps the untracked `.env` out of the probe. The editable `.pth` in `.venv` puts the repo root on `sys.path`, which is why `evaluation` and `tests` must be blocked explicitly.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_production_imports.py -v`
Expected: 2 passed, 1 failed with `93 production module(s) fail to import`. 68 lines name `packages/llm/testing.py:13`, 15 name `packages/retrieval/testing.py:18`, and the 10 `services.triage_worker.*` modules name `services/triage_worker/training.py:29`. This takes about 17 s.

- [ ] **Step 3: Remove the three re-exports**

`packages/llm/__init__.py`: delete line 55, `from packages.llm.testing import LLMProviderContractSuite`, and the `    "LLMProviderContractSuite",` line in `__all__`.

`packages/retrieval/__init__.py`: delete line 43, `from packages.retrieval.testing import SearchBackendContractSuite`, and the `    "SearchBackendContractSuite",` line in `__all__`. Also remove the trailing blank line at the end of the file; it is a pre-existing `ruff format` failure in a file you are now touching.

`packages/adapters/__init__.py`: delete this block (lines 38-41):

```python
try:
    from packages.adapters.testing import MailProviderAdapterContractSuite
except ImportError:
    MailProviderAdapterContractSuite = None  # type: ignore[assignment, misc]
```

Also delete the blank line left between the registry import's closing `)` and `from packages.adapters.webhooks import webhook_router`; otherwise ruff raises `I001`. Delete the `    "MailProviderAdapterContractSuite",` line in `__all__`.

- [ ] **Step 4: Point the two namespace importers at the testing module**

`tests/unit/test_search_backend_contract.py`, replacing lines 13-18:

```python
from packages.retrieval import (
    FakeSearchBackend,
    RetrievalQuery,
    SearchBackend,
)
from packages.retrieval.testing import SearchBackendContractSuite
```

`tests/integration/test_postgres_search_backend.py`, replacing lines 29-35:

```python
from packages.retrieval import (
    Candidate,
    PostgresSearchBackend,
    RetrievalQuery,
    SearchBackend,
)
from packages.retrieval.testing import SearchBackendContractSuite
```

- [ ] **Step 5: Run the guard again; expect the triage residue**

Run: `uv run pytest tests/unit/test_production_imports.py -v`
Expected: 1 failed with `10 production module(s) fail to import`, all `services.triage_worker.*`, raised at `services/triage_worker/training.py:29`.

- [ ] **Step 6: Make training import `evaluation` only when it actually trains**

In `services/triage_worker/training.py`:

Line 17: `from typing import Any` → `from typing import TYPE_CHECKING, Any`

Replace lines 29-30:

```python
from evaluation.datasets.loader import load_classification_dataset
from evaluation.datasets.schemas import ClassificationCategory, ClassificationDatasetItem
```

with:

```python
from packages.domain.taxonomy import Category

if TYPE_CHECKING:
    from evaluation.datasets.schemas import ClassificationDatasetItem
```

In `train_triage_model`, replace:

```python
    if train_items is None:
        train_items = load_classification_dataset(split="train")
    if test_items is None:
        test_items = load_classification_dataset(split="test")
```

with:

```python
    if train_items is None or test_items is None:
        # Offline-only dependency: evaluation/ ships with the repo, not the runtime image.
        from evaluation.datasets.loader import load_classification_dataset

        if train_items is None:
            train_items = load_classification_dataset(split="train")
        if test_items is None:
            test_items = load_classification_dataset(split="test")
```

Replace `all_categories = {c.value for c in ClassificationCategory}` with `all_categories = {c.value for c in Category}`. `ClassificationCategory` is an alias of `packages.domain.taxonomy.Category` (`evaluation/datasets/schemas.py:14`).

- [ ] **Step 7: Run the guard and the full unit suite**

Run: `uv run pytest tests/unit/test_production_imports.py -v`
Expected: 3 passed in about 3.5 s.

Run: `uv run pytest tests/unit`
Expected: 1044 passed (1041 existing plus 3 new).

Run: `uv run ruff check . && uv run mypy packages services tests/unit/test_production_imports.py tests/unit/test_search_backend_contract.py && uv run ruff format --check packages/llm/__init__.py packages/retrieval/__init__.py packages/adapters/__init__.py services/triage_worker/training.py tests/unit/test_production_imports.py`
Expected: all clean.

- [ ] **Step 8: Commit**

```bash
git add packages/llm/__init__.py packages/retrieval/__init__.py packages/adapters/__init__.py \
  services/triage_worker/training.py tests/unit/test_production_imports.py \
  tests/unit/test_search_backend_contract.py tests/integration/test_postgres_search_backend.py
git commit -m "fix(runtime): keep pytest and evaluation off the production import path [task RA.1] [R20.1]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Integration tests isolated from the running stack (RA.2)

Once Tasks 9-13 start real workers on vhost `/` and database `rag_email`, those workers would consume test messages. The tests would purge the workers' queues, and `test_migration_reversibility` would drop the live schema. This task moves every integration test to `rag_email_test`, and it adds `scratch_vhost()` for tests that need their own topology arguments (Task 4).

**Files:**
- Create: `tests/integration/isolation.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/unit/test_integration_isolation_helpers.py`
- Create: `tests/integration/test_isolation_integration.py`
- Modify these 8 files that hard-code `RABBITMQ_URL`:
  - `tests/integration/test_broker_rabbitmq.py`
  - `tests/integration/test_batch_consumer_integration.py`
  - `tests/integration/test_queue_metrics_integration.py`
  - `tests/integration/test_job_timeline_and_replay_integration.py`
  - `tests/integration/test_lease_reaper_integration.py`
  - `tests/integration/test_retry_dead_letter_integration.py`
  - `tests/integration/test_job_publication_integration.py`
  - `tests/integration/test_category_routing_integration.py`
- Modify: `tests/integration/test_normalization_failure_integration.py`
- Modify: `tests/integration/test_api_integration.py`
- Modify: `.github/workflows/ci.yml` (rabbitmq service ports)

**Interfaces:**
- Produces: `tests.integration.isolation.scratch_vhost(broker: BrokerSettings, prefix: str) -> AsyncContextManager[BrokerSettings]` (used by Tasks 4, 5, 6 and 9), and `IsolationError`.
- Produces: the invariant that inside `tests/integration`, `AppSettings().broker.vhost == "rag_email_test"` and `AppSettings().database.name == "rag_email_test"`.

- [ ] **Step 1: Write the failing unit tests for the helpers**

Create `tests/unit/test_integration_isolation_helpers.py`:

```python
"""Unit tests for integration-test isolation helpers (RA.2)."""

import pytest

from packages.core.settings import BrokerSettings, DatabaseSettings
from tests.integration.isolation import (
    IsolationError,
    database_from_dsn,
    maintenance_dsn,
    validate_test_name,
    vhost_from_amqp_url,
)


@pytest.mark.parametrize("name", ["rag_email_test", "a_test", "x1_test", "retry_ab12_test"])
def test_validate_test_name_accepts_safe_names(name: str) -> None:
    assert validate_test_name(name) == name


@pytest.mark.parametrize("name", ["rag_email", "/", "", "rag-email_test", "Rag_test", "a/b_test"])
def test_validate_test_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(IsolationError):
        validate_test_name(name)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("amqp://guest:guest@localhost:5672/", "/"),
        ("amqp://guest:guest@localhost:5672", "/"),
        ("amqp://guest:guest@localhost:5672/%2F", "/"),
        ("amqp://guest:guest@localhost:5672/rag_email_test", "rag_email_test"),
    ],
)
def test_vhost_from_amqp_url_matches_aiormq(url: str, expected: str) -> None:
    assert vhost_from_amqp_url(url) == expected


def test_settings_url_round_trips_test_vhost() -> None:
    assert vhost_from_amqp_url(BrokerSettings(vhost="rag_email_test").url) == "rag_email_test"
    assert vhost_from_amqp_url(BrokerSettings(vhost="/").url) == "/"


def test_database_from_dsn_and_maintenance_dsn() -> None:
    db = DatabaseSettings(name="rag_email_test", port=5433)
    assert database_from_dsn(db.asyncpg_dsn) == "rag_email_test"
    assert database_from_dsn(maintenance_dsn(db)) == "postgres"
    assert db.name == "rag_email_test"  # model_copy did not mutate the original
```

Run: `uv run pytest tests/unit/test_integration_isolation_helpers.py`
Expected: collection error, `ModuleNotFoundError: No module named 'tests.integration.isolation'`.

- [ ] **Step 2: Create the isolation module**

Create `tests/integration/isolation.py`. Do not name any function `test_*`; pytest would collect it when a test module imports it.

```python
"""Isolate integration tests from the running docker compose stack (RA.2, R24.4).

Integration tests share the RabbitMQ and PostgreSQL *servers* with the compose workers.
They are redirected to a dedicated vhost and database so workers never consume test
messages and tests never roll back or purge live state. Connection guards turn any
attempt to reach a non-test vhost or database into IsolationError.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import aio_pika
import asyncpg
import httpx
import pytest

from packages.core.settings import BrokerSettings, DatabaseSettings

OPT_OUT_ENV = "RAG_EMAIL_TEST_USE_CONFIGURED_ENV"
VHOST_ENV = "RAG_EMAIL_TEST_BROKER_VHOST"
DATABASE_ENV = "RAG_EMAIL_TEST_DATABASE_NAME"
MGMT_URL_ENV = "RAG_EMAIL_TEST_RABBITMQ_MGMT_URL"

DEFAULT_TEST_VHOST = "rag_email_test"
DEFAULT_TEST_DATABASE = "rag_email_test"
MAINTENANCE_DATABASE = "postgres"

_SAFE_TEST_NAME = re.compile(r"[a-z0-9_]+_test")

# Vhosts/databases the guards currently accept. scratch_vhost() adds and removes entries.
_allowed_vhosts: set[str] = set()
_allowed_databases: set[str] = set()


class IsolationError(RuntimeError):
    """Raised when an integration test reaches a non-test vhost or database."""


def isolation_enabled() -> bool:
    """Return False only when the caller explicitly opts out of isolation."""
    return os.environ.get(OPT_OUT_ENV, "") != "1"


def validate_test_name(name: str) -> str:
    """Accept only lowercase names ending in _test (safe to drop, URL-safe)."""
    if not _SAFE_TEST_NAME.fullmatch(name):
        raise IsolationError(
            f"Refusing to use {name!r}: test vhost/database names must match "
            f"{_SAFE_TEST_NAME.pattern!r}"
        )
    return name


def resolve_test_vhost() -> str:
    return validate_test_name(os.environ.get(VHOST_ENV, DEFAULT_TEST_VHOST))


def resolve_test_database() -> str:
    return validate_test_name(os.environ.get(DATABASE_ENV, DEFAULT_TEST_DATABASE))


def vhost_from_amqp_url(url: object) -> str:
    """Resolve the vhost exactly as aiormq does: empty path or '/' means '/'."""
    path = urlsplit(str(url)).path
    if path in ("", "/"):
        return "/"
    return unquote(path[1:])


def database_from_dsn(dsn: object) -> str:
    return unquote(urlsplit(str(dsn)).path.lstrip("/"))


def management_url(broker: BrokerSettings) -> str:
    return os.environ.get(MGMT_URL_ENV) or f"http://{broker.host}:15672"


def maintenance_dsn(database: DatabaseSettings) -> str:
    return database.model_copy(update={"name": MAINTENANCE_DATABASE}).asyncpg_dsn


def reset_vhost(broker: BrokerSettings, vhost: str) -> None:
    """Drop and recreate a test vhost, then grant the broker user full rights."""
    validate_test_name(vhost)
    name = quote(vhost, safe="")
    user = quote(broker.user, safe="")
    with httpx.Client(
        base_url=management_url(broker),
        auth=(broker.user, broker.password),
        timeout=10.0,
    ) as client:
        deleted = client.delete(f"/api/vhosts/{name}")
        if deleted.status_code not in (204, 404):
            deleted.raise_for_status()
        client.put(
            f"/api/vhosts/{name}", json={"description": "rag-email integration tests"}
        ).raise_for_status()
        client.put(
            f"/api/permissions/{name}/{user}",
            json={"configure": ".*", "write": ".*", "read": ".*"},
        ).raise_for_status()


def delete_vhost(broker: BrokerSettings, vhost: str) -> None:
    """Delete a test vhost; a missing vhost is not an error."""
    validate_test_name(vhost)
    with httpx.Client(
        base_url=management_url(broker),
        auth=(broker.user, broker.password),
        timeout=10.0,
    ) as client:
        deleted = client.delete(f"/api/vhosts/{quote(vhost, safe='')}")
        if deleted.status_code not in (204, 404):
            deleted.raise_for_status()


async def reset_database(database: DatabaseSettings, name: str) -> None:
    """Drop and recreate the test database via the maintenance database."""
    validate_test_name(name)
    conn = await asyncpg.connect(maintenance_dsn(database))
    try:
        # CREATE/DROP DATABASE cannot run inside a transaction block; asyncpg
        # autocommits statements executed outside conn.transaction().
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


def install_connection_guards(mp: pytest.MonkeyPatch, *, vhost: str, database: str) -> None:
    """Make any connection to a non-test vhost or database fail loudly."""
    _allowed_vhosts.clear()
    _allowed_vhosts.add(vhost)
    _allowed_databases.clear()
    _allowed_databases.add(database)

    real_connect_robust: Callable[..., Coroutine[Any, Any, Any]] = aio_pika.connect_robust
    real_connect: Callable[..., Coroutine[Any, Any, Any]] = asyncpg.connect
    real_create_pool: Callable[..., Any] = asyncpg.create_pool

    def check_vhost(url: object, kwargs: dict[str, Any]) -> None:
        actual = vhost_from_amqp_url(url) if url is not None else kwargs.get("virtualhost", "/")
        if actual not in _allowed_vhosts:
            raise IsolationError(
                f"Integration test tried to use RabbitMQ vhost {actual!r}; allowed: "
                f"{sorted(_allowed_vhosts)}. Build broker settings with AppSettings().broker."
            )

    def check_database(dsn: object, kwargs: dict[str, Any]) -> None:
        actual = database_from_dsn(dsn) if dsn is not None else kwargs.get("database")
        if actual not in _allowed_databases:
            raise IsolationError(
                f"Integration test tried to use database {actual!r}; allowed: "
                f"{sorted(_allowed_databases)}. Build settings with AppSettings().database."
            )

    async def guarded_connect_robust(url: Any = None, *args: Any, **kwargs: Any) -> Any:
        check_vhost(url, kwargs)
        return await real_connect_robust(url, *args, **kwargs)

    async def guarded_connect(dsn: Any = None, *args: Any, **kwargs: Any) -> Any:
        check_database(dsn, kwargs)
        return await real_connect(dsn, *args, **kwargs)

    def guarded_create_pool(dsn: Any = None, *args: Any, **kwargs: Any) -> Any:
        check_database(dsn, kwargs)
        return real_create_pool(dsn, *args, **kwargs)

    mp.setattr(aio_pika, "connect_robust", guarded_connect_robust)
    mp.setattr(asyncpg, "connect", guarded_connect)
    mp.setattr(asyncpg, "create_pool", guarded_create_pool)


@asynccontextmanager
async def scratch_vhost(broker: BrokerSettings, prefix: str) -> AsyncIterator[BrokerSettings]:
    """Create a throwaway vhost for tests that need their own topology arguments.

    Yields a copy of ``broker`` pointing at the new vhost. The guards accept it only
    while the context is open; the vhost is deleted on exit.
    """
    name = validate_test_name(f"{prefix}_{uuid4().hex[:10]}_test")
    await asyncio.to_thread(reset_vhost, broker, name)
    _allowed_vhosts.add(name)
    try:
        yield broker.model_copy(update={"vhost": name})
    finally:
        _allowed_vhosts.discard(name)
        await asyncio.to_thread(delete_vhost, broker, name)
```

Run: `uv run pytest tests/unit/test_integration_isolation_helpers.py`
Expected: 16 passed.

- [ ] **Step 3: Write the failing isolation integration test**

Create `tests/integration/test_isolation_integration.py`:

```python
"""Prove integration tests are isolated from the compose stack's vhost and database (RA.2)."""

from urllib.parse import quote
from uuid import uuid4

import aio_pika
import asyncpg
import httpx
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from tests.integration.isolation import (
    IsolationError,
    isolation_enabled,
    management_url,
    resolve_test_database,
    resolve_test_vhost,
    scratch_vhost,
)

pytestmark = pytest.mark.skipif(
    not isolation_enabled(), reason="isolation disabled via RAG_EMAIL_TEST_USE_CONFIGURED_ENV=1"
)


def test_settings_resolve_to_test_vhost_and_database() -> None:
    settings = AppSettings()
    assert settings.broker.vhost == resolve_test_vhost()
    assert settings.database.name == resolve_test_database()
    assert settings.broker.url.endswith(f"/{resolve_test_vhost()}")


async def test_database_pool_lands_in_migrated_test_database() -> None:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        assert await pool.fetchval("SELECT current_database()") == resolve_test_database()
        applied = await pool.fetchval("SELECT count(*) FROM schema_migrations")
        assert applied >= 3
    finally:
        await pool.close()


async def test_queue_declared_by_tests_exists_only_in_test_vhost() -> None:
    broker = AppSettings().broker
    queue_name = f"isolation.probe.{uuid4().hex[:8]}"
    conn = await aio_pika.connect_robust(broker.url)
    try:
        channel = await conn.channel()
        # exclusive: the broker drops the probe with the connection, even if an assert fails
        await channel.declare_queue(queue_name, exclusive=True)
        async with httpx.AsyncClient(
            base_url=management_url(broker), auth=(broker.user, broker.password)
        ) as client:
            in_test = await client.get(
                f"/api/queues/{quote(resolve_test_vhost(), safe='')}/{queue_name}"
            )
            in_default = await client.get(f"/api/queues/%2F/{queue_name}")
        assert in_test.status_code == 200
        assert in_default.status_code == 404
    finally:
        await conn.close()


async def test_guard_rejects_default_vhost() -> None:
    with pytest.raises(IsolationError):
        await aio_pika.connect_robust("amqp://guest:guest@localhost:5672/")


async def test_guard_rejects_live_database() -> None:
    live = AppSettings().database.model_copy(update={"name": "rag_email"})
    with pytest.raises(IsolationError):
        await asyncpg.connect(live.asyncpg_dsn)
    with pytest.raises(IsolationError):
        await create_pool_from_settings(live)


async def test_scratch_vhost_is_usable_only_while_open() -> None:
    async with scratch_vhost(AppSettings().broker, "probe") as broker:
        assert broker.vhost.startswith("probe_") and broker.vhost.endswith("_test")
        conn = await aio_pika.connect_robust(broker.url)
        await conn.close()
    with pytest.raises(IsolationError):
        await aio_pika.connect_robust(broker.url)
```

Run: `uv run pytest tests/integration/test_isolation_integration.py`
Expected: 6 failed. `test_settings_resolve_to_test_vhost_and_database` fails with `'/' == 'rag_email_test'`, the pool test with `'rag_email' == 'rag_email_test'`, the probe test with `404 == 200`, the two guard tests with `DID NOT RAISE`, and the scratch-vhost test with `AMQPInternalError` (the deleted vhost refuses the connection).

- [ ] **Step 4: Add the package fixture**

Create `tests/integration/conftest.py`:

```python
"""Integration-test isolation from the live docker compose stack (see isolation.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from packages.core.settings import AppSettings
from packages.db.migrator import apply_migrations
from tests.integration.isolation import (
    install_connection_guards,
    isolation_enabled,
    reset_database,
    reset_vhost,
    resolve_test_database,
    resolve_test_vhost,
)


@pytest.fixture(scope="package", autouse=True)
def isolated_infrastructure() -> Iterator[None]:
    """Point every integration test at a fresh, migrated test vhost and database."""
    if not isolation_enabled():
        yield
        return

    vhost = resolve_test_vhost()
    database = resolve_test_database()
    mp = pytest.MonkeyPatch()
    try:
        # Env vars beat .env in pydantic-settings; AppSettings() resolves lazily inside
        # fixtures and tests, so this redirects every settings instance created later.
        mp.setenv("BROKER__VHOST", vhost)
        mp.setenv("DATABASE__NAME", database)
        settings = AppSettings()

        reset_vhost(settings.broker, vhost)
        asyncio.run(reset_database(settings.database, database))
        asyncio.run(apply_migrations(dsn=settings.database.asyncpg_dsn))

        install_connection_guards(mp, vhost=vhost, database=database)
        yield
    finally:
        mp.undo()
```

A package-scoped, synchronous fixture is used rather than `pytest_configure`. The environment is restored after `tests/integration`, so `tests/unit` never sees it. `asyncio.run` avoids pytest-asyncio loop-scope configuration.

- [ ] **Step 5: Run the integration suite to see the red list**

Run: `uv run pytest tests/integration`
Expected: `test_isolation_integration.py` now passes. About ten files fail with `IsolationError`: the 8 `RABBITMQ_URL` files, `test_normalization_failure_integration.py` (a bare `BrokerSettings().url`), and `test_api_integration.py` (hard-coded `name="rag_email"`).

- [ ] **Step 6: Route the failing files through `AppSettings()`**

Apply exactly these edits:

1. **In each of the 8 files listed under Files:**
   - Delete the `RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"` line together with the blank line that follows it. Otherwise ruff reports I001 in all 8 files; `uv run ruff check --fix tests/integration` repairs it.
   - Replace every `aio_pika.connect_robust(RABBITMQ_URL)` with `aio_pika.connect_robust(AppSettings().broker.url)`.
   - Replace every bare `BrokerSettings()` call with `AppSettings().broker`, including `broker_settings or BrokerSettings()` in `test_batch_consumer_integration.py:57`.
   - Where `AppSettings` is not yet imported, extend the existing `from packages.core.settings import ...` line. It is missing today in `test_broker_rabbitmq.py:19`, `test_batch_consumer_integration.py:26`, `test_queue_metrics_integration.py:27` and `test_category_routing_integration.py:27-30`.
   - Remove `BrokerSettings` from the import where it becomes unused (ruff F401). Keep it where it is still a type annotation.
   - Exact call sites to change:
     - `test_broker_rabbitmq.py`: lines 27, 39, 88, 148, 219, 279
     - `test_batch_consumer_integration.py`: lines 36, 57, 107, 199
     - `test_queue_metrics_integration.py`: lines 44, 113, 215
     - `test_job_timeline_and_replay_integration.py`: lines 53, 67, 241
     - `test_lease_reaper_integration.py`: lines 52, 149, 217
     - `test_retry_dead_letter_integration.py`: lines 48, 75, 212
     - `test_job_publication_integration.py`: lines 45, 181
     - `test_category_routing_integration.py`: lines 41, 55, 91, 191, 236, 275
2. **`tests/integration/test_broker_rabbitmq.py`:** pass `broker_settings=settings` to the `NormalizerConsumer(...)`, `FailingConsumer(...)` and `FatalConsumer(...)` constructors (about lines 109, 171 and 234). Without it, `BaseConsumer` falls back to a bare `BrokerSettings()`, which means vhost `/`.
3. **`tests/integration/test_normalization_failure_integration.py`:** change lines 94, 132 and 224 from `BrokerSettings()` to `AppSettings().broker`, then drop the unused `BrokerSettings` import.
4. **`tests/integration/test_api_integration.py`:** in `live_api_client`, delete the comment and these five override lines, keeping `settings = APISettings()`:

   ```python
       settings.database.host = "localhost"
       settings.database.port = 5433
       settings.database.name = "rag_email"
       settings.database.user = "postgres"
       settings.database.password = "postgres"
   ```

Run: `uv run pytest tests/integration`
Expected: all pass (117 existing plus 6 new).

- [ ] **Step 7: Prove the live stack was untouched**

Run:

```bash
curl -s -u guest:guest 'http://localhost:15672/api/queues/%2F?columns=name,messages' | python3 -c "import json,sys; print({q['name']:q['messages'] for q in json.load(sys.stdin) if q['messages']})"
docker exec rag-email-postgres psql -U postgres -d rag_email -Atc "select count(*) from schema_migrations"
```

Expected: `{'email.general_inquiry.normal': 2}` (unchanged from before the run) and `3`.

- [ ] **Step 8: Let CI reach the management API**

In `.github/workflows/ci.yml`, change the rabbitmq service `ports:` block (around line 102-103) to:

```yaml
        ports:
          - 5672:5672
          - 15672:15672          # management API used by tests/integration/conftest.py
```

- [ ] **Step 9: Lint, type-check, commit**

Run:
- `uv run ruff check .`
- `uv run mypy tests/integration/isolation.py tests/integration/conftest.py tests/integration/test_isolation_integration.py tests/unit/test_integration_isolation_helpers.py`
- `uv run ruff format --check tests/integration/isolation.py tests/integration/conftest.py tests/integration/test_isolation_integration.py tests/unit/test_integration_isolation_helpers.py`

Expected: clean.

```bash
git add tests/integration tests/unit/test_integration_isolation_helpers.py .github/workflows/ci.yml
git commit -m "test(integration): isolate integration tests in a dedicated vhost and database [task RA.2] [R24.4]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 4: Retry ladder returns messages to their origin exchange (RA.3)

Today the three retry queues declare `x-dead-letter-exchange=email.route`, a topic exchange bound only to `email.<category>.<lane>`. A retry from `email.normalize`, `email.triage`, `knowledge.ingest` or `mail.sync.requested` is dropped when its TTL expires; `topology.py:190-193` is the cause. The fix returns every retry through a new headers exchange to its origin exchange. It also resolves the origin on redelivery, because dead-lettering replaces `message.exchange` with the DLX name.

```
consumer ─fail─▶ retry.email.<tier> (fanout, rk = origin rk) ─▶ email.retry.<tier> (TTL, DLX = retry.return)
   header retry-origin-exchange = <origin>                        │ expire: rk + headers kept
                                                                  ▼
                        retry.return (headers, alternate-exchange = dlx.email)
                          ├─ retry-origin-exchange=mail.ingest ──▶ mail.ingest ──▶ mail.sync.requested
                          ├─ …=email.process ──▶ email.process ──▶ email.normalize
                          ├─ …=email.triage / email.dispatch / knowledge.ingest ──▶ same-named queue
                          ├─ …=email.route ──▶ email.route ──▶ email.<category>.<lane>
                          └─ no match ──▶ dlx.email ──▶ email.dead_letter   (never dropped)
```

**Files:**
- Modify: `packages/core/settings.py` (import `re`; `_CATEGORY_QUEUE_RE`; `BrokerSettings.exchange_retry_return`; `BrokerSettings.exchange_for_queue`)
- Modify: `packages/broker/publisher.py`
- Modify: `packages/broker/topology.py`
- Modify: `packages/broker/consumer.py:139-143, 182`
- Modify: `packages/broker/batch_consumer.py:123-127, 254`
- Modify: `packages/broker/retry.py:177`
- Modify: `Makefile`, `specs/design.md` §7.1–§7.2, `docs/configuration.md` §2.2, `.env.example`
- Create: `tests/unit/test_retry_return_routing.py`
- Create: `tests/unit/test_broker_exchange_resolver.py`
- Create: `tests/integration/test_retry_return_routing_integration.py`

**Interfaces:**
- Produces: `BrokerSettings.exchange_retry_return: str` (default `"retry.return"`)
- Produces: `BrokerSettings.exchange_for_queue(queue_name: str | None) -> str | None`
- Produces: `packages.broker.publisher.RETRY_ORIGIN_EXCHANGE_HEADER = "retry-origin-exchange"`
- Produces: `packages.broker.publisher.RETRY_TIER_SUFFIXES: tuple[str, str, str]`
- Produces: `packages.broker.publisher.resolve_origin_exchange(message: AbstractIncomingMessage, broker_settings: BrokerSettings) -> str`
- Produces: `MessagePublisher(broker_settings=None, connection=None, channel=None, retry_settings: RetryLadderSettings | None = None)`
- Produces: `MessagePublisher.retry_tier_suffix(tier_delay_s: int) -> str`
- Produces: `packages.broker.topology.RetryTopologyMigrationError(RuntimeError)`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_broker_exchange_resolver.py`:

```python
"""BrokerSettings.exchange_for_queue mirrors setup_topology bindings (RA.3, R3.1, R18.7)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.broker.topology import setup_topology
from packages.core.settings import BrokerSettings


@pytest.mark.parametrize(
    ("queue", "exchange"),
    [
        ("mail.sync.requested", "mail.ingest"),
        ("email.normalize", "email.process"),
        ("email.triage", "email.triage"),
        ("email.dispatch", "email.dispatch"),
        ("knowledge.ingest", "knowledge.ingest"),
        ("email.support.normal", "email.route"),
        ("email.billing.priority", "email.route"),
        ("email.general_inquiry.normal", "email.route"),
    ],
)
def test_known_queues_resolve_to_their_bound_exchange(queue: str, exchange: str) -> None:
    assert BrokerSettings().exchange_for_queue(queue) == exchange


@pytest.mark.parametrize(
    "queue",
    [None, "", "email.retry.30s", "email.dead_letter", "email.support.urgent", "unknown.queue"],
)
def test_unroutable_queues_resolve_to_none(queue: str | None) -> None:
    assert BrokerSettings().exchange_for_queue(queue) is None


def test_resolver_follows_overridden_names() -> None:
    cfg = BrokerSettings(queue_normalize="n.q", exchange_email_process="n.ex")
    assert cfg.exchange_for_queue("n.q") == "n.ex"


class _RecordingChannel:
    """Minimal aio-pika channel double recording (queue, exchange, routing_key) bindings."""

    def __init__(self) -> None:
        self.bindings: list[tuple[str, str, str | None]] = []

    async def declare_exchange(self, name: str, *_: Any, **__: Any) -> MagicMock:
        ex = MagicMock()
        ex.name = name
        ex.bind = AsyncMock()
        return ex

    async def declare_queue(self, name: str, **_: Any) -> MagicMock:
        q = MagicMock()

        async def bind(exchange: Any, routing_key: str | None = None) -> None:
            self.bindings.append((name, exchange.name, routing_key))

        q.bind = bind
        return q


async def test_resolver_agrees_with_every_direct_topology_binding() -> None:
    cfg = BrokerSettings()
    channel = _RecordingChannel()
    await setup_topology(channel, cfg)  # type: ignore[arg-type]
    checked = 0
    for queue, exchange, key in channel.bindings:
        if key != queue or queue.startswith("email.retry.") or exchange == cfg.exchange_dlx:
            continue
        assert cfg.exchange_for_queue(queue) == exchange, (queue, exchange)
        checked += 1
    assert checked >= 5 + 2  # 5 stage queues + at least one category x 2 lanes
```

Create `tests/unit/test_retry_return_routing.py`:

```python
"""Retry return routing: origin header, tier mapping, topology (RA.3, R3.5, R19.5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelPreconditionFailed

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import (
    RETRY_ORIGIN_EXCHANGE_HEADER,
    MessagePublisher,
    resolve_origin_exchange,
)
from packages.broker.topology import RetryTopologyMigrationError, setup_topology
from packages.core.settings import BrokerSettings, RetryLadderSettings

ORIGINS = (
    "mail.ingest",
    "email.process",
    "email.triage",
    "email.route",
    "email.dispatch",
    "knowledge.ingest",
)


def _envelope(attempt: int = 1) -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"k-{uuid4()}",
        job_type="normalize",
        organization_id=str(uuid4()),
        attempt=attempt,
    )


def _message(exchange: str, headers: dict[str, Any] | None = None) -> AbstractIncomingMessage:
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.exchange = exchange
    msg.headers = headers or {}
    return msg


async def test_publish_to_retry_sets_retry_origin_header() -> None:
    publisher = MessagePublisher()
    exchange = AsyncMock()
    publisher._get_exchange = AsyncMock(return_value=exchange)  # type: ignore[method-assign]

    await publisher.publish_to_retry(
        envelope=_envelope(),
        tier_delay_s=30,
        origin_exchange="email.process",
        origin_routing_key="email.normalize",
        failure_reason="TransientError: x",
    )

    publisher._get_exchange.assert_awaited_once_with("retry.email.30s")
    msg = exchange.publish.call_args[0][0]
    assert exchange.publish.call_args[1]["routing_key"] == "email.normalize"
    assert msg.headers[RETRY_ORIGIN_EXCHANGE_HEADER] == "email.process"
    assert not RETRY_ORIGIN_EXCHANGE_HEADER.startswith("x-")  # headers exchange skips x-*
    assert msg.headers["x-original-exchange"] == "email.process"


@pytest.mark.parametrize(("delay", "suffix"), [(30, "30s"), (300, "5m"), (1800, "30m")])
def test_retry_tier_mapping_default_settings(delay: int, suffix: str) -> None:
    assert MessagePublisher().retry_tier_suffix(delay) == suffix


@pytest.mark.parametrize(("delay", "suffix"), [(1, "30s"), (2, "5m"), (3, "30m")])
def test_retry_tier_mapping_follows_retry_settings(delay: int, suffix: str) -> None:
    fast = RetryLadderSettings(tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3)
    assert MessagePublisher(retry_settings=fast).retry_tier_suffix(delay) == suffix


def test_resolve_origin_exchange_prefers_header_after_retry_return() -> None:
    s = BrokerSettings()
    via_return = _message("retry.return", {RETRY_ORIGIN_EXCHANGE_HEADER: "email.process"})
    assert resolve_origin_exchange(via_return, s) == "email.process"
    via_bytes = _message("retry.return", {RETRY_ORIGIN_EXCHANGE_HEADER: b"email.triage"})
    assert resolve_origin_exchange(via_bytes, s) == "email.triage"
    direct = _message("email.triage", {RETRY_ORIGIN_EXCHANGE_HEADER: "email.process"})
    assert resolve_origin_exchange(direct, s) == "email.triage"  # header trusted only via DLX


def _recording_channel() -> tuple[MagicMock, dict[str, AsyncMock], dict[str, dict[str, Any]]]:
    exchanges: dict[str, AsyncMock] = {}
    queue_calls: dict[str, dict[str, Any]] = {}

    async def declare_exchange(name: str, type_: Any = None, **kw: Any) -> AsyncMock:
        ex = exchanges.setdefault(name, AsyncMock())
        ex.declared_type, ex.declared_kwargs = type_, kw
        return ex

    async def declare_queue(name: str, **kw: Any) -> AsyncMock:
        queue_calls[name] = kw
        return AsyncMock()

    channel = MagicMock()
    channel.declare_exchange = AsyncMock(side_effect=declare_exchange)
    channel.declare_queue = AsyncMock(side_effect=declare_queue)
    return channel, exchanges, queue_calls


async def test_setup_topology_declares_retry_return_routing() -> None:
    channel, exchanges, queue_calls = _recording_channel()

    await setup_topology(channel, BrokerSettings(), RetryLadderSettings())

    rr = exchanges["retry.return"]
    assert rr.declared_type == aio_pika.ExchangeType.HEADERS
    assert rr.declared_kwargs["arguments"] == {"alternate-exchange": "dlx.email"}
    for suffix, ttl in (("30s", 30_000), ("5m", 300_000), ("30m", 1_800_000)):
        assert queue_calls[f"email.retry.{suffix}"]["arguments"] == {
            "x-message-ttl": ttl,
            "x-dead-letter-exchange": "retry.return",
        }
    for origin in ORIGINS:
        exchanges[origin].bind.assert_any_await(
            rr,
            routing_key="",
            arguments={"x-match": "all", RETRY_ORIGIN_EXCHANGE_HEADER: origin},
        )


async def test_setup_topology_explains_stale_retry_queue_arguments() -> None:
    channel, _, _ = _recording_channel()

    async def declare_queue(name: str, **kw: Any) -> AsyncMock:
        if name == "email.retry.30s":
            raise ChannelPreconditionFailed(
                "PRECONDITION_FAILED - inequivalent arg 'x-dead-letter-exchange'"
            )
        return AsyncMock()

    channel.declare_queue = AsyncMock(side_effect=declare_queue)

    with pytest.raises(RetryTopologyMigrationError, match="make broker-migrate-retry"):
        await setup_topology(channel, BrokerSettings(), RetryLadderSettings())
```

Run: `uv run pytest tests/unit/test_broker_exchange_resolver.py tests/unit/test_retry_return_routing.py`
Expected: a collection error, `ImportError: cannot import name 'RETRY_ORIGIN_EXCHANGE_HEADER'`, which interrupts the run. Running `tests/unit/test_broker_exchange_resolver.py` alone gives 16 failures with `AttributeError: 'BrokerSettings' object has no attribute 'exchange_for_queue'`.

- [ ] **Step 2: Add the settings field and the queue→exchange resolver**

In `packages/core/settings.py`, add `import re` to the stdlib imports at the top. Add this at module level, directly above `class BrokerSettings`:

```python
# Category routing queues bound on the topic exchange: email.<category>.<lane>
# (packages/broker/topology.py category bindings; lanes per CategoryRoutingSettings defaults).
_CATEGORY_QUEUE_RE = re.compile(r"email\.[a-z0-9_]+\.(?:normal|priority)")
```

In `BrokerSettings`, add this field directly after `exchange_dlx`:

```python
    exchange_retry_return: str = Field(
        default="retry.return",
        description=(
            "Headers exchange that expired retry messages dead-letter into; it routes each "
            "message back to its origin exchange via the retry-origin-exchange header "
            "(design.md §7.2)"
        ),
    )
```

In `BrokerSettings`, add this method directly after the `url` property:

```python
    def exchange_for_queue(self, queue_name: str | None) -> str | None:
        """Return the exchange whose binding delivers ``queue_name`` as routing key.

        Mirrors the bindings declared by ``packages.broker.topology.setup_topology``.
        Returns ``None`` for unknown, retry, or dead-letter queues, so callers refuse to
        publish instead of emitting a message the broker would drop as unroutable.
        """
        if not queue_name:
            return None
        direct_bindings = {
            self.queue_mail_sync: self.exchange_mail_ingest,
            self.queue_normalize: self.exchange_email_process,
            self.queue_triage: self.exchange_email_triage,
            self.queue_dispatch: self.exchange_email_dispatch,
            self.queue_knowledge: self.exchange_knowledge_ingest,
        }
        if queue_name in direct_bindings:
            return direct_bindings[queue_name]
        if _CATEGORY_QUEUE_RE.fullmatch(queue_name):
            return self.exchange_email_route
        return None
```

The resolver lives on `BrokerSettings` because `packages/core` must not import `packages/broker`.

- [ ] **Step 3: Publisher: origin header, settings-driven tiers, origin resolver**

In `packages/broker/publisher.py`, replace the import block with:

```python
import logging
from datetime import UTC, datetime
from typing import Any

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
)

from packages.broker.envelope import JobEnvelope
from packages.core.settings import BrokerSettings, RetryLadderSettings

logger = logging.getLogger(__name__)

RETRY_ORIGIN_EXCHANGE_HEADER = "retry-origin-exchange"
"""Header matched by the retry.return headers exchange.

Deliberately NOT ``x-`` prefixed: with ``x-match: all`` RabbitMQ skips ``x-*`` headers when
matching (rabbit_exchange_type_headers: ``match({<<"x-", _/binary>>, _, _}, _) -> skip``).
"""

RETRY_TIER_SUFFIXES: tuple[str, str, str] = ("30s", "5m", "30m")


def resolve_origin_exchange(
    message: AbstractIncomingMessage, broker_settings: BrokerSettings
) -> str:
    """Return the exchange a delivery logically belongs to.

    Dead-lettering replaces the message's exchange with the DLX name, so a retried delivery
    arrives with ``exchange == retry.return``; its true origin travels in the
    ``retry-origin-exchange`` header set by ``publish_to_retry``.
    """
    exchange = message.exchange or ""
    if exchange == broker_settings.exchange_retry_return:
        origin = (message.headers or {}).get(RETRY_ORIGIN_EXCHANGE_HEADER)
        if isinstance(origin, (bytes, bytearray)):
            origin = origin.decode("utf-8", errors="replace")
        if origin:
            return str(origin)
    return exchange
```

Replace `MessagePublisher.__init__` with this version, which appends the keyword at the end so positional callers keep working:

```python
    def __init__(
        self,
        broker_settings: BrokerSettings | None = None,
        connection: AbstractRobustConnection | None = None,
        channel: AbstractChannel | None = None,
        retry_settings: RetryLadderSettings | None = None,
    ) -> None:
        self.settings = broker_settings or BrokerSettings()
        self.retry_settings = retry_settings or RetryLadderSettings()
        self._external_conn = connection is not None
        self._external_channel = channel is not None
        self._connection = connection
        self._channel = channel
        self._exchanges: dict[str, AbstractExchange] = {}

    def retry_tier_suffix(self, tier_delay_s: int) -> str:
        """Map a tier delay to the retry queue whose TTL was declared from the same settings."""
        r = self.retry_settings
        if tier_delay_s <= r.tier_1_delay_s:
            return RETRY_TIER_SUFFIXES[0]
        if tier_delay_s <= r.tier_2_delay_s:
            return RETRY_TIER_SUFFIXES[1]
        return RETRY_TIER_SUFFIXES[2]
```

In `publish_to_retry`, replace the `if tier_delay_s <= 30 … else …` block and the `headers = {...}` dict with the following. With default settings the thresholds are still 30 and 300.

```python
        tier_suffix = self.retry_tier_suffix(tier_delay_s)
        retry_exchange = f"{self.settings.exchange_retry}.{tier_suffix}"

        headers = {
            "x-original-exchange": origin_exchange,
            "x-original-routing-key": origin_routing_key,
            "x-failure-reason": failure_reason,
            "x-attempt": envelope.attempt,
            # Matched by retry.return (headers exchange) after TTL dead-lettering.
            RETRY_ORIGIN_EXCHANGE_HEADER: origin_exchange,
        }
```

- [ ] **Step 4: Topology: the return exchange, its bindings, and a clear migration error**

In `packages/broker/topology.py`, add the first import directly below the `from aio_pika.abc import (...)` block, and the second directly above `from packages.broker.routing import (` (same first-party group, no blank line between them):

```python
from aio_pika.exceptions import ChannelPreconditionFailed

from packages.broker.publisher import RETRY_ORIGIN_EXCHANGE_HEADER, RETRY_TIER_SUFFIXES
```

There is no cycle: `publisher.py` imports only `envelope` and `settings`. Add this class below `logger = ...`:

```python
class RetryTopologyMigrationError(RuntimeError):
    """A retry queue exists on the broker with outdated arguments (RabbitMQ 406)."""

    def __init__(self, queue_name: str) -> None:
        super().__init__(
            f"Retry queue '{queue_name}' exists with outdated arguments (queue arguments are "
            "immutable in RabbitMQ). Run `make broker-migrate-retry` once to delete the empty "
            "old retry queues, then start the stack again."
        )
        self.queue_name = queue_name
```

Replace the fanout block (lines 114-120) so it uses the shared suffixes, and add the return exchange directly after it:

```python
    retry_tier_fanout_exchanges: dict[str, AbstractExchange] = {}
    for tier_suffix in RETRY_TIER_SUFFIXES:
        fanout_name = f"{b_cfg.exchange_retry}.{tier_suffix}"
        ex = await channel.declare_exchange(fanout_name, aio_pika.ExchangeType.FANOUT, durable=True)
        exchanges[fanout_name] = ex
        retry_tier_fanout_exchanges[tier_suffix] = ex

    # Retry return exchange (design.md §7.2): expired retry messages are dead-lettered here,
    # keeping their original routing key and headers, and routed back to the exchange named
    # in the retry-origin-exchange header. Unmatched messages go to the alternate exchange
    # (dlx.email) instead of being dropped.
    retry_return = await channel.declare_exchange(
        b_cfg.exchange_retry_return,
        aio_pika.ExchangeType.HEADERS,
        durable=True,
        arguments={"alternate-exchange": b_cfg.exchange_dlx},
    )
    exchanges[b_cfg.exchange_retry_return] = retry_return

    retry_origin_exchanges = (
        b_cfg.exchange_mail_ingest,
        b_cfg.exchange_email_process,
        b_cfg.exchange_email_triage,
        b_cfg.exchange_email_route,
        b_cfg.exchange_email_dispatch,
        b_cfg.exchange_knowledge_ingest,
    )
    for origin_name in retry_origin_exchanges:
        # destination.bind(source): messages flow retry.return -> origin exchange
        await exchanges[origin_name].bind(
            retry_return,
            routing_key="",
            arguments={"x-match": "all", RETRY_ORIGIN_EXCHANGE_HEADER: origin_name},
        )
```

Replace the retry-tier list and the start of the loop (lines 182-197) with:

```python
    retry_tiers = list(
        zip(
            RETRY_TIER_SUFFIXES,
            (
                r_cfg.tier_1_delay_s * 1000,
                r_cfg.tier_2_delay_s * 1000,
                r_cfg.tier_3_delay_s * 1000,
            ),
            strict=True,
        )
    )

    for tier_suffix, ttl_ms in retry_tiers:
        queue_name = f"email.retry.{tier_suffix}"
        retry_args: dict[str, Any] = {
            "x-message-ttl": ttl_ms,
            "x-dead-letter-exchange": b_cfg.exchange_retry_return,
        }
        if b_cfg.use_quorum_queues:
            retry_args["x-queue-type"] = "quorum"

        try:
            q_retry = await channel.declare_queue(queue_name, durable=True, arguments=retry_args)
        except ChannelPreconditionFailed as exc:
            raise RetryTopologyMigrationError(queue_name) from exc
```

The three `await q_retry.bind(...)` lines and `queues[queue_name] = q_retry` that follow stay unchanged.

- [ ] **Step 5: Consumers resolve the origin; retry fallback uses the resolver**

In `packages/broker/consumer.py`:
- Change `from packages.broker.publisher import MessagePublisher` to `from packages.broker.publisher import MessagePublisher, resolve_origin_exchange`.
- In `start()`, add `retry_settings=self.retry_settings,` to the `MessagePublisher(...)` call.
- Replace line 182, `origin_exchange = message.exchange or ""`, with `origin_exchange = resolve_origin_exchange(message, self.broker_settings)`.

In `packages/broker/batch_consumer.py`, make the same three changes: the import, `retry_settings=self.retry_settings,` in the `MessagePublisher(...)` inside `start()`, and line 254 in `_handle_single_item`.

In `packages/broker/retry.py`, replace:

```python
    effective_exchange = origin_exchange or publisher.settings.exchange_email_route
```

with:

```python
    # An empty origin (e.g. a publish through the default exchange) is resolved from the
    # queue bindings; if still unknown, "" matches no retry.return binding, so the message
    # is dead-lettered via the alternate exchange rather than dropped.
    effective_exchange = (
        origin_exchange
        or publisher.settings.exchange_for_queue(origin_routing_key)
        or publisher.settings.exchange_for_queue(queue_name)
        or ""
    )
```

- [ ] **Step 6: Run the unit tests**

Run: `uv run pytest tests/unit/test_broker_exchange_resolver.py tests/unit/test_retry_return_routing.py tests/unit/test_retry_and_dead_letter.py tests/unit/test_batch_consumer.py tests/unit/test_lease_reaper.py tests/unit/test_queue_metrics.py`
Expected: all pass.

- [ ] **Step 7: Write the live routing tests (scratch vhost with 1/2/3 s tiers)**

Create `tests/integration/test_retry_return_routing_integration.py`:

```python
"""Live retry-return routing on a throwaway vhost (RA.3, R3.5, R19.5).

A scratch vhost is required: the tests declare retry queues with 1/2/3 s TTLs, which
would conflict (406) with the default-TTL queues in the shared test vhost.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractQueue

from packages.broker.consumer import BaseConsumer, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import RETRY_ORIGIN_EXCHANGE_HEADER, MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings, BrokerSettings, RetryLadderSettings
from tests.integration.isolation import management_url, scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
ORIGIN_QUEUES = [
    ("mail.ingest", "mail.sync.requested"),
    ("email.process", "email.normalize"),
    ("email.triage", "email.triage"),
    ("email.route", "email.billing.priority"),
    ("email.dispatch", "email.dispatch"),
    ("knowledge.ingest", "knowledge.ingest"),
]


@pytest.fixture
async def fast_broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "retry") as broker:
        yield broker


@pytest.fixture
async def topo_channel(fast_broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(fast_broker.url)
    channel = await conn.channel()
    await setup_topology(channel, fast_broker, FAST_RETRY)
    try:
        yield channel
    finally:
        await conn.close()  # before the vhost fixture deletes the vhost


async def wait_for_message(queue: AbstractQueue, timeout_s: float) -> AbstractIncomingMessage:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        msg = await queue.get(no_ack=True, fail=False)
        if msg is not None:
            return msg
        await asyncio.sleep(0.1)
    raise AssertionError(f"no message on {queue.name} within {timeout_s}s")


def make_envelope(attempt: int) -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"it:{uuid4()}",
        job_type="normalize",
        organization_id=str(uuid4()),
        message_id=str(uuid4()),
        attempt=attempt,
    )


async def test_retry_queues_dead_letter_into_retry_return(
    fast_broker: BrokerSettings, topo_channel: AbstractChannel
) -> None:
    vh = quote(fast_broker.vhost, safe="")
    async with httpx.AsyncClient(
        base_url=management_url(fast_broker),
        auth=(fast_broker.user, fast_broker.password),
        timeout=10.0,
    ) as http:
        for suffix, ttl in (("30s", 1000), ("5m", 2000), ("30m", 3000)):
            q = (await http.get(f"/api/queues/{vh}/email.retry.{suffix}")).json()
            assert q["arguments"] == {
                "x-message-ttl": ttl,
                "x-dead-letter-exchange": "retry.return",
            }
        ex = (await http.get(f"/api/exchanges/{vh}/retry.return")).json()
        assert ex["type"] == "headers"
        assert ex["arguments"] == {"alternate-exchange": "dlx.email"}
        binds = (await http.get(f"/api/exchanges/{vh}/retry.return/bindings/source")).json()
    got = {
        (b["destination"], b["destination_type"], b["arguments"][RETRY_ORIGIN_EXCHANGE_HEADER])
        for b in binds
    }
    assert got == {(origin, "exchange", origin) for origin, _ in ORIGIN_QUEUES}


@pytest.mark.parametrize(("origin_exchange", "origin_queue"), ORIGIN_QUEUES)
async def test_expired_retry_returns_to_origin_queue(
    fast_broker: BrokerSettings,
    topo_channel: AbstractChannel,
    origin_exchange: str,
    origin_queue: str,
) -> None:
    publisher = MessagePublisher(broker_settings=fast_broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        env = make_envelope(attempt=1)
        await publisher.publish_to_retry(
            envelope=env,
            tier_delay_s=FAST_RETRY.tier_1_delay_s,
            origin_exchange=origin_exchange,
            origin_routing_key=origin_queue,
            failure_reason="TransientError: it",
        )
        msg = await wait_for_message(await topo_channel.get_queue(origin_queue), timeout_s=10)
    finally:
        await publisher.close()

    assert JobEnvelope.from_message(msg).job_id == env.job_id
    assert msg.routing_key == origin_queue
    assert msg.exchange == fast_broker.exchange_retry_return  # DLX replaces the exchange name
    assert msg.headers[RETRY_ORIGIN_EXCHANGE_HEADER] == origin_exchange  # headers survive DLX
    assert "x-death" in msg.headers
    dlq = await topo_channel.get_queue(fast_broker.queue_dead_letter)
    assert await dlq.get(no_ack=True, fail=False) is None


async def test_unmatched_retry_origin_goes_to_dead_letter_not_dropped(
    fast_broker: BrokerSettings, topo_channel: AbstractChannel
) -> None:
    publisher = MessagePublisher(broker_settings=fast_broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        env = make_envelope(attempt=1)
        await publisher.publish_to_retry(
            envelope=env,
            tier_delay_s=1,
            origin_exchange="no.such.exchange",
            origin_routing_key="email.normalize",
            failure_reason="TransientError: it",
        )
        dlq = await topo_channel.get_queue(fast_broker.queue_dead_letter)
        msg = await wait_for_message(dlq, timeout_s=10)
    finally:
        await publisher.close()
    assert JobEnvelope.from_message(msg).job_id == env.job_id
    assert "x-death" in msg.headers


async def test_transient_failures_are_redelivered_to_email_normalize(
    fast_broker: BrokerSettings, topo_channel: AbstractChannel
) -> None:
    deliveries: list[tuple[int, str | None, str | None, Any]] = []
    done = asyncio.Event()

    class FlakyNormalizer(BaseConsumer):
        async def process_job(
            self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
        ) -> None:
            deliveries.append(
                (
                    envelope.attempt,
                    raw_message.exchange,
                    raw_message.routing_key,
                    (raw_message.headers or {}).get(RETRY_ORIGIN_EXCHANGE_HEADER),
                )
            )
            if envelope.attempt < 2:
                raise TransientError(f"provider timeout #{envelope.attempt}")
            done.set()

    consumer = FlakyNormalizer(
        queue_name=fast_broker.queue_normalize,
        broker_settings=fast_broker,
        retry_settings=FAST_RETRY,
        prefetch_count=1,
    )
    publisher = MessagePublisher(broker_settings=fast_broker, retry_settings=FAST_RETRY)
    await consumer.start()
    await publisher.connect()
    try:
        await publisher.publish(
            exchange_name=fast_broker.exchange_email_process,
            routing_key=fast_broker.queue_normalize,
            envelope=make_envelope(attempt=0),
        )
        await asyncio.wait_for(done.wait(), timeout=20)  # ~1 s (tier 1) + ~2 s (tier 2)
    finally:
        await consumer.stop()
        await publisher.close()

    assert [d[0] for d in deliveries] == [0, 1, 2]
    assert deliveries[0][1] == "email.process"
    for _attempt, exchange, routing_key, origin in deliveries[1:]:
        assert exchange == fast_broker.exchange_retry_return
        assert routing_key == fast_broker.queue_normalize
        assert origin == "email.process"  # the second failure resolved its origin via header
    dlq = await topo_channel.get_queue(fast_broker.queue_dead_letter)
    assert await dlq.get(no_ack=True, fail=False) is None
```

Run: `uv run pytest tests/integration/test_retry_return_routing_integration.py`
Expected: 9 passed (1 + 6 parametrized + 1 + 1).

Before Steps 3-5, every parametrized case except `email.route` would time out: the message was dropped at `email.route`.

- [ ] **Step 8: One-time migration target for existing brokers**

Add this to `Makefile`, add `broker-migrate-retry` to the end of the `.PHONY` line, and add `	@echo "  broker-migrate-retry - One-time: delete empty stale retry queues (RA.3)"` after the `load` echo in `help`:

```make
broker-migrate-retry:
	docker exec rag-email-rabbitmq sh -c 'for q in email.retry.30s email.retry.5m email.retry.30m; do \
	  if rabbitmqctl -q -p / list_queues name arguments | grep -F "$$q" | grep -q "email.route"; then \
	    rabbitmqctl -p / delete_queue "$$q" --if-empty || exit 1; \
	  fi; \
	done'
	@echo "Stale retry queues removed; they are redeclared on the next init/worker start."
```

It deletes only retry queues that still dead-letter to `email.route`. `--if-empty` refuses to drop messages, and running it twice is a no-op. **Do not run it now.** Task 13 runs it, after asking the user.

- [ ] **Step 9: Keep the design and configuration docs in sync**

`specs/design.md` §7.1 table: change the `retry.email` row's last column to `(TTL → retry.return → origin exchange)`, and add a row after it:

```markdown
| `retry.return` | headers (alternate-exchange `dlx.email`) | — (exchange-to-exchange binding to each origin exchange, matched on header `retry-origin-exchange`) | returns expired retries to their origin |
```

`specs/design.md` §7.2: replace "queue TTL expires → dead-letters back to the origin exchange" with "queue TTL expires → dead-letters into `retry.return`, which routes it back to the origin exchange named in the `retry-origin-exchange` header (unmatched → `dlx.email`)".

`docs/configuration.md` §2.2: add a row after `BROKER__EXCHANGE_DLX`:

```markdown
| `BROKER__EXCHANGE_RETRY_RETURN` | `string` | `retry.return` | Non-empty | Headers exchange that routes expired retries back to their origin exchange (design §7.2) |
```

`.env.example`: add `BROKER__EXCHANGE_RETRY_RETURN=retry.return` after `BROKER__EXCHANGE_DLX=dlx.email`.

- [ ] **Step 10: Full verification and commit**

Run: `uv run pytest tests/unit && uv run pytest tests/integration`
Expected: all pass.

Run:
- `uv run ruff check .`
- `uv run mypy packages services tests/unit/test_retry_return_routing.py tests/unit/test_broker_exchange_resolver.py tests/integration/test_retry_return_routing_integration.py`
- `uv run ruff format --check packages/core/settings.py packages/broker/publisher.py packages/broker/topology.py packages/broker/retry.py packages/broker/consumer.py packages/broker/batch_consumer.py tests/unit/test_retry_return_routing.py tests/unit/test_broker_exchange_resolver.py tests/integration/test_retry_return_routing_integration.py`

Expected: clean. If `ruff format --check` flags `settings.py`, `consumer.py` or `batch_consumer.py` for pre-existing reasons, run `uv run ruff format <that file>` and include the reformat in this commit.

```bash
git add packages/core/settings.py packages/broker/publisher.py packages/broker/topology.py \
  packages/broker/consumer.py packages/broker/batch_consumer.py packages/broker/retry.py \
  Makefile specs/design.md docs/configuration.md .env.example \
  tests/unit/test_retry_return_routing.py tests/unit/test_broker_exchange_resolver.py \
  tests/integration/test_retry_return_routing_integration.py
git commit -m "fix(broker): return expired retries to their origin exchange via retry.return [task RA.3] [R3.5, R19.5, R19.6]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Unparseable deliveries dead-lettered verbatim (RA.4)

Today, `BaseConsumer._handle_message` (`consumer.py:174-180`) acks an unparseable body and publishes nothing. The batch consumer dead-letters a *placeholder* envelope and drops the original bytes. After this task, both publish the raw body to `dlx.email` with diagnostic headers. They never ack without a successful dead-letter publish.

**Files:**
- Modify: `packages/broker/publisher.py` (new `publish_raw_to_dead_letter`)
- Modify: `packages/broker/consumer.py` (new `_dead_letter_unparseable`; parse-failure branch)
- Modify: `packages/broker/batch_consumer.py` (parse-failure branch)
- Modify: `tests/unit/test_batch_consumer.py::test_malformed_envelope_dead_lettered_immediately`
- Create: `tests/unit/test_unparseable_dead_letter.py`
- Create: `tests/integration/test_unparseable_dead_letter_integration.py`

**Interfaces:**
- Consumes: `resolve_origin_exchange` (Task 4).
- Produces: `MessagePublisher.publish_raw_to_dead_letter(message: AbstractIncomingMessage, failure_reason: str, origin_exchange: str, origin_routing_key: str) -> None`
- Produces: `BaseConsumer._dead_letter_unparseable(message: AbstractIncomingMessage, parse_err: Exception) -> None`, inherited by `BaseBatchConsumer`.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_unparseable_dead_letter.py`:

```python
"""Unparseable deliveries are dead-lettered with their raw body (RA.4, R3.5, R3.3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher


def _raw_message(
    body: bytes,
    exchange: str = "email.process",
    routing_key: str = "email.normalize",
    headers: dict[str, Any] | None = None,
) -> MagicMock:
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = body
    msg.exchange = exchange
    msg.routing_key = routing_key
    msg.headers = headers or {}
    msg.content_type = "application/json"
    msg.content_encoding = None
    msg.message_id = "m-1"
    msg.correlation_id = None
    msg.timestamp = None  # a MagicMock timestamp would break aio_pika.Message encoding
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    return msg


class _NeverCalled(BaseConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        raise AssertionError("process_job must not run for unparseable input")


async def test_base_consumer_dead_letters_unparseable_with_raw_body() -> None:
    consumer = _NeverCalled(queue_name="email.normalize")
    pub = MagicMock(spec=MessagePublisher)
    pub.publish_raw_to_dead_letter = AsyncMock()
    consumer._publisher = pub
    msg = _raw_message(b"NOT JSON {")

    await consumer._handle_message(msg)

    pub.publish_raw_to_dead_letter.assert_awaited_once()
    kw = pub.publish_raw_to_dead_letter.await_args.kwargs
    assert kw["message"] is msg
    assert kw["failure_reason"].startswith("EnvelopeParseError")
    assert kw["origin_exchange"] == "email.process"
    assert kw["origin_routing_key"] == "email.normalize"
    msg.ack.assert_awaited_once()
    msg.nack.assert_not_awaited()


async def test_base_consumer_requeues_when_dead_letter_publish_fails() -> None:
    consumer = _NeverCalled(queue_name="email.normalize")
    pub = MagicMock(spec=MessagePublisher)
    pub.publish_raw_to_dead_letter = AsyncMock(side_effect=ConnectionError("broker gone"))
    consumer._publisher = pub
    msg = _raw_message(b"\xff\xfe")

    await consumer._handle_message(msg)

    msg.ack.assert_not_awaited()
    msg.nack.assert_awaited_once_with(requeue=True)


async def test_publish_raw_to_dead_letter_preserves_body_and_headers() -> None:
    publisher = MessagePublisher()
    exchange = AsyncMock()
    publisher._get_exchange = AsyncMock(return_value=exchange)  # type: ignore[method-assign]
    msg = _raw_message(b"NOT JSON {", headers={"trace_id": "t-1"})

    await publisher.publish_raw_to_dead_letter(
        message=msg,
        failure_reason="EnvelopeParseError: JSONDecodeError: x",
        origin_exchange="email.process",
        origin_routing_key="email.normalize",
    )

    publisher._get_exchange.assert_awaited_once_with("dlx.email")
    published = exchange.publish.call_args[0][0]
    assert published.body == b"NOT JSON {"
    assert published.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert published.headers["trace_id"] == "t-1"
    assert published.headers["x-original-routing-key"] == "email.normalize"
    assert published.headers["x-failure-reason"].startswith("EnvelopeParseError")
    assert "x-failed-at" in published.headers
    assert exchange.publish.call_args[1]["routing_key"] == "email.normalize"
```

Update `tests/unit/test_batch_consumer.py::test_malformed_envelope_dead_lettered_immediately` so it encodes the new, non-lossy behaviour. After `mock_publisher.publish_to_dead_letter = AsyncMock()`, add:

```python
        mock_publisher.publish_raw_to_dead_letter = AsyncMock()
```

Replace the last block, from `# Malformed message routed to DLQ` through `assert "EnvelopeParseError" in dlq_call["failure_reason"]`, with:

```python
        # Malformed message routed to DLQ with its original bytes
        mock_publisher.publish_raw_to_dead_letter.assert_awaited_once()
        dlq_call = mock_publisher.publish_raw_to_dead_letter.call_args.kwargs
        assert dlq_call["message"] is msg_malformed
        assert "EnvelopeParseError" in dlq_call["failure_reason"]
        mock_publisher.publish_to_dead_letter.assert_not_awaited()
```

Keep the final `assert consumer.jobs_processed == ["job-valid"]`.

Run: `uv run pytest tests/unit/test_unparseable_dead_letter.py tests/unit/test_batch_consumer.py`
Expected: the new tests fail (no `publish_raw_to_dead_letter` awaited; `AttributeError` on the real publisher), and the updated batch test fails.

- [ ] **Step 2: Implement the raw dead-letter publish**

Add this to `MessagePublisher` in `packages/broker/publisher.py`, directly after `publish_to_dead_letter`:

```python
    async def publish_raw_to_dead_letter(
        self,
        message: AbstractIncomingMessage,
        failure_reason: str,
        origin_exchange: str,
        origin_routing_key: str,
    ) -> None:
        """Dead-letter a delivery whose body could not be parsed into a JobEnvelope (R3.5).

        The original body bytes and headers are preserved verbatim so the payload can be
        inspected or replayed; diagnostic headers are added on top. ``expiration`` is not
        copied, so the dead-letter copy never expires.
        """
        headers: dict[str, Any] = dict(message.headers or {})
        headers.update(
            {
                "x-original-exchange": origin_exchange,
                "x-original-routing-key": origin_routing_key,
                "x-failure-reason": failure_reason,
                "x-failed-at": datetime.now(UTC).isoformat(),
            }
        )
        dead_message = aio_pika.Message(
            body=message.body,
            headers=headers,
            content_type=message.content_type,
            content_encoding=message.content_encoding,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            timestamp=message.timestamp,
        )
        exchange = await self._get_exchange(self.settings.exchange_dlx)
        # dlx.email is a topic exchange with a "#" binding: any key (including "") routes.
        await exchange.publish(dead_message, routing_key=origin_routing_key)
        logger.error(
            "Unparseable message from '%s' (key '%s') dead-lettered: %s",
            origin_exchange,
            origin_routing_key,
            failure_reason,
        )
```

- [ ] **Step 3: Route both consumers' parse failures through it**

In `packages/broker/consumer.py`, replace the parse-failure branch in `_handle_message`:

```python
        try:
            envelope = JobEnvelope.from_message(message)
        except Exception as parse_err:
            logger.error("Failed to parse JobEnvelope from message: %s", parse_err)
            # Cannot parse: route raw rejection to dead letter and ack to clear queue
            await message.ack()
            return
```

with:

```python
        try:
            envelope = JobEnvelope.from_message(message)
        except Exception as parse_err:
            await self._dead_letter_unparseable(message, parse_err)
            return
```

Add this method to `BaseConsumer`, directly after `get_retry_delay_s`:

```python
    async def _dead_letter_unparseable(
        self, message: AbstractIncomingMessage, parse_err: Exception
    ) -> None:
        """Dead-letter an unparseable delivery with its raw body, then ack (R3.5).

        Never acks without a successful dead-letter publish: on publish failure the message
        is requeued so it is not lost.
        """
        reason = f"EnvelopeParseError: {type(parse_err).__name__}: {parse_err}"
        logger.error("Failed to parse JobEnvelope on queue '%s': %s", self.queue_name, parse_err)
        try:
            if self._publisher is None:
                raise RuntimeError("consumer publisher is not initialised")
            await self._publisher.publish_raw_to_dead_letter(
                message=message,
                failure_reason=reason,
                origin_exchange=resolve_origin_exchange(message, self.broker_settings),
                origin_routing_key=message.routing_key or self.queue_name,
            )
        except Exception:
            logger.exception(
                "Could not dead-letter unparseable message on '%s'; requeueing", self.queue_name
            )
            await message.nack(requeue=True)
            return
        await message.ack()
```

In `packages/broker/batch_consumer.py`, replace the whole `except Exception as parse_err:` body in the envelope-parsing loop (about lines 196-218 after Task 4: the log call, the `if self._publisher is not None:` placeholder dead-letter block, and the trailing `await raw_msg.ack()`) with:

```python
                    except Exception as parse_err:
                        await self._dead_letter_unparseable(raw_msg, parse_err)
```

Then run `uv run ruff check packages/broker/batch_consumer.py`. If `import time` is now unused (F401), delete it.

- [ ] **Step 4: Run the unit tests**

Run: `uv run pytest tests/unit/test_unparseable_dead_letter.py tests/unit/test_batch_consumer.py tests/unit/test_retry_and_dead_letter.py tests/unit/test_queue_metrics.py`
Expected: all pass.

- [ ] **Step 5: Live proof on a scratch vhost**

Create `tests/integration/test_unparseable_dead_letter_integration.py`:

```python
"""An unparseable delivery reaches email.dead_letter with its original body (RA.4, R3.5)."""

from __future__ import annotations

import asyncio

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from tests.integration.isolation import scratch_vhost


async def _wait(queue: AbstractQueue, timeout_s: float) -> AbstractIncomingMessage:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        msg = await queue.get(no_ack=True, fail=False)
        if msg is not None:
            return msg
        await asyncio.sleep(0.1)
    raise AssertionError(f"no message on {queue.name} within {timeout_s}s")


class _NeverCalled(BaseConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        raise AssertionError("must not be called")


async def test_unparseable_message_is_dead_lettered_with_raw_body() -> None:
    async with scratch_vhost(AppSettings().broker, "unparseable") as broker:
        conn = await aio_pika.connect_robust(broker.url)
        consumer = _NeverCalled(queue_name=broker.queue_normalize, broker_settings=broker)
        try:
            channel = await conn.channel()
            await setup_topology(channel, broker)
            await consumer.start()
            ex = await channel.get_exchange(broker.exchange_email_process)
            await ex.publish(
                aio_pika.Message(
                    b"NOT JSON {",
                    headers={"trace_id": "t-1"},
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=broker.queue_normalize,
            )
            dlq = await channel.get_queue(broker.queue_dead_letter)
            msg = await _wait(dlq, timeout_s=5)
            norm = await channel.get_queue(broker.queue_normalize)
            leftover = await norm.get(no_ack=True, fail=False)
        finally:
            await consumer.stop()
            await conn.close()

    assert msg.body == b"NOT JSON {"
    assert msg.headers["trace_id"] == "t-1"
    assert msg.headers["x-original-exchange"] == "email.process"
    assert msg.headers["x-original-routing-key"] == "email.normalize"
    assert str(msg.headers["x-failure-reason"]).startswith("EnvelopeParseError")
    assert leftover is None
```

Run: `uv run pytest tests/integration/test_unparseable_dead_letter_integration.py`
Expected: 1 passed.

- [ ] **Step 6: Verify and commit**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run mypy packages services tests/unit/test_unparseable_dead_letter.py tests/unit/test_batch_consumer.py tests/integration/test_unparseable_dead_letter_integration.py`
Expected: clean.

```bash
git add packages/broker/publisher.py packages/broker/consumer.py packages/broker/batch_consumer.py \
  tests/unit/test_unparseable_dead_letter.py tests/unit/test_batch_consumer.py \
  tests/integration/test_unparseable_dead_letter_integration.py
git commit -m "fix(broker): dead-letter unparseable deliveries with their raw body [task RA.4] [R3.5, R3.3]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Unroutable publishes raise; consumers resume after reconnect; failed acks never duplicate (RA.5)

This task closes three silent-failure paths, all verified against aio-pika 10.0.1 source.
1. **Unroutable publishes vanish.** `Connection.channel()` defaults to `on_return_raises=False`, so a mandatory publish that matches no binding is only logged and `await publish()` succeeds.
2. **Consumers stop after a reconnect.** `get_queue(name, ensure=False)` returns a queue object that the robust channel does not register, so after a broker reconnect the consumer is not restored. `declare_queue(name, passive=True)` on a `RobustChannel` is registered, and it still does not create the queue.
3. **A failed ack duplicates the job.** The success ack sits inside the `try`. If the ack itself fails, the `except` branch publishes a retry, while the broker also requeues the unacked original.

**Files:**
- Modify: `packages/broker/publisher.py` (`connect()`)
- Modify: `packages/broker/consumer.py` (`start()`, `_handle_message`)
- Modify: `packages/broker/batch_consumer.py` (`start()`, `_handle_single_item`)
- Create: `tests/unit/test_consumer_channel_setup.py`
- Create: `tests/integration/test_unroutable_publish_integration.py`

**Interfaces:**
- Produces: every channel that `MessagePublisher.connect()`, `BaseConsumer.start()` or `BaseBatchConsumer.start()` opens raises `aio_pika.exceptions.PublishError` on an unroutable mandatory publish.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_consumer_channel_setup.py`:

```python
"""Channel setup and ack placement for consumers and publishers (RA.5, R3.1, R3.3)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelInvalidStateError

from packages.broker.batch_consumer import BaseBatchConsumer
from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher


class _RecordingChannel:
    def __init__(self) -> None:
        self.is_closed = False
        self.qos: int | None = None
        self.declared: list[tuple[str, dict[str, Any]]] = []

    async def set_qos(self, prefetch_count: int) -> None:
        self.qos = prefetch_count

    async def declare_queue(self, name: str, **kwargs: Any) -> AsyncMock:
        self.declared.append((name, kwargs))
        queue = AsyncMock()
        queue.consume = AsyncMock(return_value="ctag-1")
        return queue

    async def close(self) -> None:
        self.is_closed = True


class _RecordingConnection:
    def __init__(self) -> None:
        self.is_closed = False
        self.channel_kwargs: list[dict[str, Any]] = []
        self.channel_obj = _RecordingChannel()

    async def channel(self, **kwargs: Any) -> _RecordingChannel:
        self.channel_kwargs.append(kwargs)
        return self.channel_obj


class _OkConsumer(BaseConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        return None


class _OkBatchConsumer(BaseBatchConsumer):
    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        return None


def _envelope_message(ack: AsyncMock) -> MagicMock:
    envelope = JobEnvelope(
        idempotency_key=f"k-{uuid4()}", job_type="normalize", organization_id=str(uuid4())
    )
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = envelope.model_dump_json().encode("utf-8")
    msg.exchange = "email.process"
    msg.routing_key = "email.normalize"
    msg.headers = {}
    msg.ack = ack
    return msg


def _mock_publisher() -> MagicMock:
    pub = MagicMock(spec=MessagePublisher)
    pub.publish_to_retry = AsyncMock()
    pub.publish_to_dead_letter = AsyncMock()
    return pub


async def test_consumer_start_uses_raising_channel_and_passive_declare() -> None:
    conn = _RecordingConnection()
    consumer = _OkConsumer(queue_name="email.normalize", connection=cast(Any, conn))

    await consumer.start()

    assert conn.channel_kwargs == [{"on_return_raises": True}]
    assert conn.channel_obj.declared == [("email.normalize", {"passive": True})]


async def test_batch_consumer_start_uses_raising_channel_and_passive_declare() -> None:
    conn = _RecordingConnection()
    consumer = _OkBatchConsumer(
        queue_name="email.normalize",
        connection=cast(Any, conn),
        batch_size=2,
        batch_timeout_s=0.01,
    )

    await consumer.start()
    consumer._is_consuming = False
    assert consumer._batch_loop_task is not None
    await asyncio.wait_for(consumer._batch_loop_task, timeout=2)

    assert conn.channel_kwargs == [{"on_return_raises": True}]
    assert conn.channel_obj.declared == [("email.normalize", {"passive": True})]


async def test_publisher_connect_uses_raising_channel() -> None:
    conn = _RecordingConnection()
    publisher = MessagePublisher(connection=cast(Any, conn))

    await publisher.connect()

    assert conn.channel_kwargs == [{"on_return_raises": True}]


async def test_failed_ack_does_not_publish_a_retry() -> None:
    consumer = _OkConsumer(queue_name="email.normalize")
    pub = _mock_publisher()
    consumer._publisher = pub
    msg = _envelope_message(AsyncMock(side_effect=ChannelInvalidStateError("closed")))

    await consumer._handle_message(msg)  # must not raise

    pub.publish_to_retry.assert_not_awaited()
    pub.publish_to_dead_letter.assert_not_awaited()


async def test_batch_failed_ack_does_not_publish_a_retry() -> None:
    consumer = _OkBatchConsumer(queue_name="email.normalize", batch_size=1, batch_timeout_s=0.01)
    pub = _mock_publisher()
    consumer._publisher = pub
    msg = _envelope_message(AsyncMock(side_effect=ChannelInvalidStateError("closed")))
    consumer._is_consuming = True

    await consumer._on_message_delivered(msg)
    task = asyncio.create_task(consumer._batch_worker_loop())
    await asyncio.sleep(0.05)
    consumer._is_consuming = False
    await asyncio.wait_for(task, timeout=2)

    pub.publish_to_retry.assert_not_awaited()
    pub.publish_to_dead_letter.assert_not_awaited()
```

Run: `uv run pytest tests/unit/test_consumer_channel_setup.py`
Expected: 5 failed. The setup tests see `channel_kwargs == [{}]` and no `declare_queue` call (today's code calls `get_queue`, which `_RecordingChannel` does not have, so an `AttributeError`). The consumer ack test raises `ChannelInvalidStateError` out of `_handle_message`: the retry is published, then the retry branch's own ack fails again. The batch ack test sees `publish_to_retry` awaited once.

- [ ] **Step 2: Raising channels and passive, robust queue declaration**

`packages/broker/publisher.py`, in `connect()`: replace `self._channel = await self._connection.channel()` with:

```python
            # Unroutable mandatory publishes must raise PublishError, never vanish (R3.1).
            self._channel = await self._connection.channel(on_return_raises=True)
```

`packages/broker/consumer.py`, in `start()`:
- Replace `self._channel = await self._connection.channel()` with `self._channel = await self._connection.channel(on_return_raises=True)`.
- Replace `self._queue = await self._channel.get_queue(self.queue_name, ensure=False)` with:

```python
        # Passive declare: fails if the topology was never declared (R3.2), and on a
        # RobustChannel registers the queue so consuming resumes after a reconnect.
        self._queue = await self._channel.declare_queue(self.queue_name, passive=True)
```

In `packages/broker/batch_consumer.py` `start()`, make the same two replacements.

- [ ] **Step 3: Move the success ack out of the `try` in both consumers**

In `packages/broker/consumer.py` `_handle_message`, restructure the body of the `with (...)` block:
- The `try:` keeps recovery, lease acquisition and `await self.process_job(envelope, message)`, but no longer contains the ack.
- The `except Exception as exc:` block keeps its retry and terminal branches exactly as they are, and gains a final `return`.
- The success ack moves below the `try`/`except`:

```python
            try:
                # 3.5 Re-deliver recovery: if job is in RETRY_PENDING,
                # transition to GENERATING (R19.5, R18.2)
                await handle_job_recovery(envelope=envelope, job_store=self.job_store)

                # 3.6 Acquire lease on claim (R19.8, design.md §9)
                if self.job_store is not None and is_valid_uuid(envelope.job_id):
                    try:
                        await self.job_store.acquire_lease(
                            organization_id=envelope.organization_id,
                            job_id=UUID(envelope.job_id),
                            lease_timeout_s=self.lease_timeout_s,
                        )
                    except Exception as lease_err:
                        logger.warning(
                            "Failed to acquire lease for job %s: %s", envelope.job_id, lease_err
                        )

                # 4. Execute consumer processing
                await self.process_job(envelope, message)
            except Exception as exc:
                should_retry = self.is_transient_error(exc) and (
                    envelope.attempt < self.retry_settings.max_retries
                )

                assert self._publisher is not None

                if should_retry:
                    await handle_job_transient_failure(
                        envelope=envelope,
                        exception=exc,
                        publisher=self._publisher,
                        retry_settings=self.retry_settings,
                        origin_exchange=origin_exchange,
                        origin_routing_key=origin_routing_key,
                        queue_name=self.queue_name,
                        job_store=self.job_store,
                    )
                    # Ack original message so it doesn't block prefetch or queue
                    await message.ack()
                else:
                    await handle_job_terminal_failure(
                        envelope=envelope,
                        exception=exc,
                        publisher=self._publisher,
                        origin_exchange=origin_exchange,
                        origin_routing_key=origin_routing_key,
                        queue_name=self.queue_name,
                        job_store=self.job_store,
                    )
                    # Ack original message to prevent infinite redelivery
                    await message.ack()
                return

            # 5. Manual ACK only after all side effects commit (R3.3). Outside the try: if
            # the ack itself fails (channel lost), the broker redelivers the unacked message,
            # so publishing a retry as well would process the job twice.
            try:
                await message.ack()
            except Exception as ack_err:
                logger.warning(
                    "Ack failed for job %s on queue '%s' (%s); the broker will redeliver it",
                    envelope.job_id,
                    self.queue_name,
                    ack_err,
                )
                return
            logger.debug(
                "Job %s successfully processed and ACKed on queue '%s'",
                envelope.job_id,
                self.queue_name,
            )
```

Apply the same restructure to `_handle_single_item` in `packages/broker/batch_consumer.py`:
- The `try` ends at `await self.process_job(envelope, message)`.
- The existing `except` block gains a trailing `return`.
- The ack moves out of the `try`, guarded by the same `try/except` with the warning.

- [ ] **Step 4: Run the unit suite**

Run: `uv run pytest tests/unit/test_consumer_channel_setup.py tests/unit`
Expected: all pass. No existing unit test mocks `get_queue` (checked with grep), so nothing else needs updating.

- [ ] **Step 5: Live proof that unroutable publishes raise**

Create `tests/integration/test_unroutable_publish_integration.py`:

```python
"""Unroutable mandatory publishes raise instead of vanishing (RA.5, R3.1, R7.1)."""

from __future__ import annotations

from uuid import uuid4

import aio_pika
import pytest
from aio_pika.exceptions import PublishError

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from tests.integration.isolation import scratch_vhost


def _envelope() -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"unroutable:{uuid4()}",
        job_type="generate_reply",
        organization_id=str(uuid4()),
    )


async def test_unroutable_publish_raises() -> None:
    async with scratch_vhost(AppSettings().broker, "unroutable") as broker:
        conn = await aio_pika.connect_robust(broker.url)
        publisher = MessagePublisher(broker_settings=broker)
        try:
            await setup_topology(await conn.channel(), broker)
            await publisher.connect()
            with pytest.raises(PublishError):
                await publisher.publish(
                    exchange_name=broker.exchange_email_route,
                    routing_key="email.not_a_category.normal",
                    envelope=_envelope(),
                )
            # The channel survives a return: a routable publish still succeeds.
            await publisher.publish(
                exchange_name=broker.exchange_email_route,
                routing_key="email.billing.normal",
                envelope=_envelope(),
            )
        finally:
            await publisher.close()
            await conn.close()
```

Run: `uv run pytest tests/integration/test_unroutable_publish_integration.py`
Expected: 1 passed.

- [ ] **Step 6: Re-run the whole integration suite**

Run: `uv run pytest tests/integration`
Expected: all pass.

**If a test now fails with `PublishError`**, that test was publishing to a key nothing is bound to, and its message was being silently lost. Fix the test so that `setup_topology` (or the specific binding it needs) runs before the publish. Do **not** catch `PublishError`, and do not revert `on_return_raises`. Consumer channels are affected too (their retry and dead-letter publishes), but those targets are bound by `setup_topology`. The publisher-created channels to check are `MessagePublisher(...)` without `channel=` in `test_broker_rabbitmq.py`, `test_retry_dead_letter_integration.py`, `test_queue_metrics_integration.py`, `test_lease_reaper_integration.py`, `test_phase1_pipeline_e2e.py` and `test_observability_e2e.py`.

- [ ] **Step 7: Verify and commit**

Run: `uv run ruff check . && uv run mypy packages services tests/unit/test_consumer_channel_setup.py tests/integration/test_unroutable_publish_integration.py`
Expected: clean.

```bash
git add packages/broker/publisher.py packages/broker/consumer.py packages/broker/batch_consumer.py \
  tests/unit/test_consumer_channel_setup.py tests/integration/test_unroutable_publish_integration.py
git commit -m "fix(broker): raise on unroutable publishes, resume consuming after reconnect, ack outside try [task RA.5] [R3.1, R3.3, R7.1]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

If Step 6 required test fixes, include those files in the `git add`.

---

### Task 7: Graceful drain: stop consuming → drain in-flight → close (RA.6)

`BaseConsumer.__init__` registers its full `stop()` as a *drain* callback, and `stop()` closes the channel. In aiormq 7.0.0, closing a channel cancels every consumer callback still running on it: the in-flight job is aborted partway, never acked, and redelivered. The coordinator's ordering (`packages/observability/shutdown.py`) is correct and does not change.

```
SIGTERM ─▶ set_draining ─▶ [drain]   consumer.stop_consuming(): basic.cancel only (channel stays open)
                           [wait]    in-flight job ─▶ process_job ─▶ message.ack() ✓ (same channel)
                           [cleanup] consumer.close(): (batch: flush buffer) ─▶ channel.close ─▶ owned conn.close
timeout path: cleanup closes the channel ─▶ remaining tasks cancelled ─▶ RabbitMQ requeues (no loss)
```

**Files:**
- Modify: `packages/broker/consumer.py` (registration at lines 89-90; replace `async def stop` (about lines 182-200 after Tasks 4-6, between `start` and `_handle_message`))
- Modify: `packages/broker/batch_consumer.py` (add `local_drain_timeout_s`; delete `async def stop` (about lines 344-373 after Tasks 4-6, the last method in the class); add `_await_local_drain`; import `suppress`)
- Create: `tests/unit/test_consumer_graceful_drain.py`

**Interfaces:**
- Produces: `BaseConsumer.stop_consuming() -> None` (idempotent; cancels the subscription and keeps the channel open).
- Produces: `BaseConsumer.close() -> None` (idempotent; stops consuming, waits for the local drain, closes the channel and any owned connection).
- Produces: `BaseConsumer.stop() -> None` (backwards-compatible wrapper that calls `close()`).
- Produces: `BaseConsumer._await_local_drain() -> None` (a hook; the batch consumer overrides it).
- Produces: registration of `stop_consuming` as the drain callback and `close` as the cleanup callback.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_consumer_graceful_drain.py`:

```python
"""Graceful shutdown drain ordering for broker consumers (RA.6, R20.8, R3.3)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelInvalidStateError

from packages.broker.batch_consumer import BaseBatchConsumer
from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.observability.health import HealthRegistry
from packages.observability.shutdown import GracefulShutdownCoordinator


class FakeChannel:
    def __init__(self) -> None:
        self.is_closed = False
        self.close_calls = 0

    async def close(self, exc: Any = None) -> None:
        self.close_calls += 1
        self.is_closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.is_closed = False
        self.close_calls = 0

    async def close(self, exc: Any = None) -> None:
        self.close_calls += 1
        self.is_closed = True


class FakeQueue:
    def __init__(self) -> None:
        self.cancelled_tags: list[str] = []

    async def cancel(self, consumer_tag: str, timeout: Any = None, nowait: bool = False) -> None:
        self.cancelled_tags.append(consumer_tag)


class ChannelBoundMessage:
    """Delivery whose ack fails once its channel is closed, like aio_pika.IncomingMessage."""

    def __init__(self, envelope: JobEnvelope, channel: FakeChannel) -> None:
        self.body = envelope.model_dump_json().encode("utf-8")
        self.exchange = "email.process"
        self.routing_key = "email.normalize"
        self.headers: dict[str, Any] = {}
        self._channel = channel
        self.acked = False

    async def ack(self, multiple: bool = False) -> None:
        if self._channel.is_closed:
            raise ChannelInvalidStateError("channel closed before ack")
        self.acked = True


def make_envelope(job_id: str = "job-drain") -> JobEnvelope:
    return JobEnvelope(
        job_id=job_id,
        idempotency_key=f"idem-{job_id}",
        job_type="normalize",
        organization_id=str(uuid4()),
    )


def make_publisher() -> MessagePublisher:
    publisher = MagicMock(spec=MessagePublisher)
    publisher.publish_to_retry = AsyncMock()
    publisher.publish_to_dead_letter = AsyncMock()
    return cast(MessagePublisher, publisher)


class GatedConsumer(BaseConsumer):
    """process_job blocks until the test releases it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(queue_name="test.drain.queue", **kwargs)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        self.started.set()
        await self.release.wait()
        self.finished = True


class GatedBatchConsumer(BaseBatchConsumer):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            queue_name="test.drain.batch.queue",
            prefetch_count=4,
            batch_size=2,
            batch_timeout_s=0.01,
            **kwargs,
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        self.started.set()
        await self.release.wait()


def wire_fakes(
    consumer: BaseConsumer,
    connection: FakeConnection | None = None,
) -> tuple[FakeChannel, FakeQueue]:
    """Put the consumer in its post-start() state without a broker."""
    channel, queue = FakeChannel(), FakeQueue()
    consumer._channel = cast(Any, channel)
    consumer._queue = cast(Any, queue)
    consumer._consumer_tag = "ctag-test"
    consumer._is_consuming = True
    consumer._publisher = make_publisher()
    if connection is not None:
        consumer._connection = cast(Any, connection)
    return channel, queue


@pytest.mark.asyncio
async def test_in_flight_job_finishing_after_shutdown_is_acked() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=2.0, health_registry=HealthRegistry("t"))
    consumer = GatedConsumer(shutdown_coordinator=coord)
    channel, queue = wire_fakes(consumer)
    msg = ChannelBoundMessage(make_envelope(), channel)

    job = asyncio.create_task(consumer._handle_message(cast(AbstractIncomingMessage, msg)))
    await consumer.started.wait()

    shutdown = asyncio.create_task(coord.trigger_shutdown("TEST_SIGTERM"))
    await asyncio.sleep(0.05)

    # Drain phase: subscription cancelled, channel still open for the in-flight ack
    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.is_closed is False
    assert coord.active_jobs_count == 1

    consumer.release.set()
    await shutdown
    (result,) = await asyncio.gather(job, return_exceptions=True)

    assert result is None
    assert consumer.finished is True
    assert msg.acked is True
    assert channel.close_calls == 1  # closed in the cleanup phase, after the ack
    assert consumer._publisher is not None
    retry_mock = cast(AsyncMock, consumer._publisher.publish_to_retry)
    retry_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_closes_channel_when_drain_times_out() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=0.05)
    consumer = GatedConsumer(shutdown_coordinator=coord)
    channel, _ = wire_fakes(consumer)
    msg = ChannelBoundMessage(make_envelope("job-stuck"), channel)

    job = asyncio.create_task(consumer._handle_message(cast(AbstractIncomingMessage, msg)))
    await consumer.started.wait()

    await coord.trigger_shutdown("TEST_SIGTERM")

    # Timeout path: channel closed anyway so RabbitMQ requeues the unacked delivery
    assert channel.is_closed is True
    assert msg.acked is False
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_consuming_is_idempotent_and_keeps_channel_open() -> None:
    consumer = GatedConsumer()
    channel, queue = wire_fakes(consumer)

    await consumer.stop_consuming()
    await consumer.stop_consuming()

    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.is_closed is False


@pytest.mark.asyncio
async def test_stop_wrapper_cancels_then_closes_owned_connection_once() -> None:
    connection = FakeConnection()
    consumer = GatedConsumer()
    channel, queue = wire_fakes(consumer, connection=connection)

    await consumer.stop()
    await consumer.stop()

    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.close_calls == 1
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_close_leaves_external_connection_open() -> None:
    connection = FakeConnection()
    consumer = GatedConsumer(connection=cast(Any, connection))
    channel, _ = wire_fakes(consumer)

    await consumer.close()

    assert channel.is_closed is True
    assert connection.close_calls == 0


@pytest.mark.asyncio
async def test_batch_in_flight_job_finishing_after_shutdown_is_acked() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=2.0)
    consumer = GatedBatchConsumer(shutdown_coordinator=coord)
    channel, queue = wire_fakes(consumer)
    msg = ChannelBoundMessage(make_envelope("job-batch"), channel)

    await consumer._on_message_delivered(cast(AbstractIncomingMessage, msg))
    consumer._batch_loop_task = asyncio.create_task(consumer._batch_worker_loop())
    await consumer.started.wait()

    shutdown = asyncio.create_task(coord.trigger_shutdown("TEST_SIGTERM"))
    await asyncio.sleep(0.05)
    assert queue.cancelled_tags == ["ctag-test"]
    assert channel.is_closed is False

    consumer.release.set()
    await shutdown

    assert msg.acked is True
    assert channel.close_calls == 1
    assert consumer._batch_loop_task.done()


@pytest.mark.asyncio
async def test_coordinator_runs_drain_then_waits_for_jobs_then_cleanup() -> None:
    coord = GracefulShutdownCoordinator(drain_timeout_s=2.0)
    events: list[str] = []

    async def on_drain() -> None:
        events.append("drain")

    async def on_cleanup() -> None:
        events.append("cleanup")

    coord.register_drain_callback(on_drain)
    coord.register_cleanup_callback(on_cleanup)

    async def job() -> None:
        with coord.track_job():
            await asyncio.sleep(0.05)
            events.append("job_done")

    task = asyncio.create_task(job())
    await asyncio.sleep(0)
    await coord.trigger_shutdown("TEST")
    await task

    assert events == ["drain", "job_done", "cleanup"]
```

Run: `uv run pytest tests/unit/test_consumer_graceful_drain.py`
Expected: 3 failures. `test_in_flight_job_finishing_after_shutdown_is_acked` fails at `assert channel.is_closed is False`, and two tests fail with `AttributeError` on `stop_consuming` / `close`.

- [ ] **Step 2: Split stop into stop_consuming and close**

In `packages/broker/consumer.py`, replace lines 89-90:

```python
        if self.shutdown_coordinator is not None:
            self.shutdown_coordinator.register_drain_callback(self.stop)
```

with:

```python
        if self.shutdown_coordinator is not None:
            # R20.8 ordering: stop new deliveries during drain, but keep the channel open so
            # in-flight jobs can still ack/publish retries on it; close it only in cleanup,
            # after the coordinator has waited for tracked jobs.
            self.shutdown_coordinator.register_drain_callback(self.stop_consuming)
            self.shutdown_coordinator.register_cleanup_callback(self.close)
```

Replace the whole `async def stop(self)` method (about lines 182-200 after Tasks 4-6; it starts with the docstring "Cancel subscription and gracefully close channels.") with:

```python
    async def stop_consuming(self) -> None:
        """Cancel the broker subscription so no new deliveries arrive (R20.8 drain step).

        The channel stays open: RabbitMQ requires acks on the channel that received the
        delivery, and closing it would cancel in-flight callbacks. Idempotent.
        """
        if not self._is_consuming:
            return
        self._is_consuming = False

        if self._queue is not None and self._consumer_tag is not None:
            try:
                await self._queue.cancel(self._consumer_tag)
            except Exception as err:
                logger.warning("Error cancelling consumer tag %s: %s", self._consumer_tag, err)

        logger.info(
            "Consumer cancelled on queue '%s'; channel kept open for in-flight acks",
            self.queue_name,
        )

    async def close(self) -> None:
        """Close the channel and owned connection (R20.8 cleanup step). Idempotent.

        Call only after in-flight jobs have drained: closing the channel makes aiormq cancel
        any consumer callback still running and makes RabbitMQ requeue its unacked delivery.
        """
        await self.stop_consuming()
        await self._await_local_drain()

        if self._channel is not None and not self._channel.is_closed:
            try:
                await self._channel.close()
            except Exception as err:
                logger.warning("Error closing channel for queue '%s': %s", self.queue_name, err)

        if (
            not self._external_conn
            and self._connection is not None
            and not self._connection.is_closed
        ):
            await self._connection.close()

        logger.info("Consumer closed on queue '%s'", self.queue_name)

    async def stop(self) -> None:
        """Backwards-compatible stop: cancel the subscription, then close immediately.

        Does not wait for in-flight jobs; register with a GracefulShutdownCoordinator for that.
        """
        await self.close()

    async def _await_local_drain(self) -> None:
        """Hook: wait for work buffered inside this consumer before the channel closes.

        BaseConsumer buffers nothing (each delivery runs in its own callback task).
        """
        return None
```

- [ ] **Step 3: Batch consumer: drain its buffer in the cleanup hook**

In `packages/broker/batch_consumer.py`:
- Change `from contextlib import nullcontext` to `from contextlib import nullcontext, suppress`.
- In `__init__`, after the `self._drain_complete ...` line, add:

```python
        # Upper bound for flushing messages still buffered in _inbound_queue at close() time.
        # In-flight jobs were already awaited by the shutdown coordinator before close() runs.
        self.local_drain_timeout_s: float = 5.0
```

- Delete the whole `async def stop(self)` method (about lines 344-373 after Tasks 4-6; it starts with the docstring "Cancel subscription, drain in-flight batches, and close channels gracefully."). The batch consumer now inherits `stop_consuming`, `close` and `stop` from `BaseConsumer`.
- Add this override:

```python
    async def _await_local_drain(self) -> None:
        """Let the batch loop flush buffered deliveries before the channel closes (R20.8)."""
        task = self._batch_loop_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(self._drain_complete.wait(), timeout=self.local_drain_timeout_s)
        except TimeoutError:
            logger.warning("Batch worker loop drain timed out during shutdown")
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
```

`stop_consuming()` sets `_is_consuming = False`, so the loop condition `while self._is_consuming or not self._inbound_queue.empty()` drains the buffer by itself.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_consumer_graceful_drain.py tests/unit/test_batch_consumer.py tests/unit/test_broker.py tests/unit/test_queue_metrics.py tests/unit/test_retry_and_dead_letter.py tests/unit/test_lease_reaper.py tests/unit/test_knowledge_consumer.py tests/unit/test_observability_health_shutdown.py tests/unit/test_consumer_channel_setup.py`
Expected: all pass. `test_batch_consumer_stop_drains_inbound_queue` still passes because `stop()` → `close()` → `_await_local_drain()`.

Run: `uv run pytest tests/integration/test_observability_e2e.py`
Expected: pass. That test calls `consumer.stop()` after a coordinator shutdown, so `close()` must be idempotent, and it is.

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run mypy packages services tests/unit/test_consumer_graceful_drain.py`
Expected: clean.

```bash
git add packages/broker/consumer.py packages/broker/batch_consumer.py tests/unit/test_consumer_graceful_drain.py
git commit -m "fix(broker): stop consuming before draining and close channels only in cleanup [task RA.6] [R20.8, R3.3]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: API publish paths never report lost work as success (RA.7)

Replay (`services/api/routers/jobs.py:174-243`) publishes to `email.route` with key `queue_name or "email.triage"`. The broker drops that publish as unroutable, and the endpoint still returns `republished=True`. It also commits `RETRY_PENDING` before publishing and swallows publish errors. Upload (`services/api/routers/knowledge.py:248-260`) returns 202 when the publish fails or when no publisher exists.

```
POST /jobs/{id}/replay
  publisher None? ─────────────────────────▶ 503 PUBLISHER_UNAVAILABLE, no DB write
  exchange_for_queue(queue_name) None? ────▶ 409 JOB_NOT_ROUTABLE, no DB write
  DB: DEAD_LETTER → RETRY_PENDING
  publish(exchange, queue_name)
     ├─ ok ─────────▶ 200 republished=True
     └─ raises ─────▶ RETRY_PENDING → FAILED → DEAD_LETTER, 503 REPLAY_PUBLISH_FAILED
```

Every production DEAD_LETTER job has `queue_name` NULL today, because no producer writes it. After this task, replaying such a job returns 409 instead of silently losing it. Writing `queue_name` is W3 work (see "not in this plan").

**Files:**
- Modify: `services/api/routers/jobs.py` (imports; helper; lines 174-243)
- Modify: `services/api/routers/knowledge.py` (imports; helper; `existing` binding; lines 248-260; endpoint description)
- Modify: `tests/unit/test_job_timeline_and_replay.py` (fixture; one seeded job; new tests)
- Modify: `tests/unit/test_knowledge_api.py` (new tests)
- Modify: `tests/integration/test_knowledge_api_integration.py` (recording publisher; new test)
- Modify: `tests/integration/test_job_timeline_and_replay_integration.py` (new test)

**Interfaces:**
- Consumes: `BrokerSettings.exchange_for_queue` (Task 4); `MessagePublisher.settings`.
- Produces: HTTP contract. Replay returns 200, 409 `JOB_NOT_REPLAYABLE`, 409 `JOB_NOT_ROUTABLE`, 503 `PUBLISHER_UNAVAILABLE` or 503 `REPLAY_PUBLISH_FAILED`. Upload returns 202, or 503 `INGEST_ENQUEUE_FAILED` with `detail.document_id`.

- [ ] **Step 1: Fix the replay unit fixture and write the failing replay tests**

In `tests/unit/test_job_timeline_and_replay.py`, add `from packages.core.settings import BrokerSettings` to the imports and replace the `mock_publisher` fixture with this one. A real `BrokerSettings` is required, because a `MagicMock` settings object would make `exchange_for_queue` return a truthy mock.

```python
@pytest.fixture
def mock_publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish = AsyncMock()
    pub.settings = BrokerSettings()
    pub.broker_settings = pub.settings
    return pub
```

In `test_replay_dead_letter_job_without_attempt_reset`, add `queue_name="email.support.normal",` to the seeded `Job(...)`. The test checks the attempt count, and a NULL queue is now refused on purpose; the new 409 test covers that.

In `test_replay_dead_letter_job_success`, add these after the existing envelope assertions:

```python
        kwargs = mock_publisher.publish.await_args.kwargs
        assert kwargs["exchange_name"] == "email.route"
        assert kwargs["routing_key"] == "email.support.normal"
```

Add these methods inside `class TestJobEndpointsAndReplay`:

```python
    async def test_replay_normalize_job_publishes_to_email_process_exchange(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID, mock_publisher: MagicMock
    ) -> None:
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()
        await job_store.create_job(
            Job(
                id=job_id,
                organization_id=org_a,
                job_type="email_pipeline",
                state=JobState.DEAD_LETTER.value,
                queue_name="email.normalize",
                idempotency_key=f"idem-{job_id}",
            )
        )
        res = await client.post(
            f"/v1/jobs/{job_id}/replay",
            headers={"X-Organization-ID": str(org_a)},
            json={"reason": "fix"},
        )
        assert res.status_code == status.HTTP_200_OK
        assert res.json()["routing_key"] == "email.normalize"
        mock_publisher.publish.assert_awaited_once()
        kwargs = mock_publisher.publish.await_args.kwargs
        assert kwargs["exchange_name"] == "email.process"
        assert kwargs["routing_key"] == "email.normalize"

    async def test_replay_publish_failure_returns_503_and_restores_dead_letter(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID, mock_publisher: MagicMock
    ) -> None:
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()
        await job_store.create_job(
            Job(
                id=job_id,
                organization_id=org_a,
                job_type="generate_reply",
                state=JobState.DEAD_LETTER.value,
                attempt=5,
                max_attempts=5,
                queue_name="email.support.normal",
                idempotency_key=f"idem-{job_id}",
            )
        )
        mock_publisher.publish.side_effect = ConnectionError("broker down")

        res = await client.post(
            f"/v1/jobs/{job_id}/replay",
            headers={"X-Organization-ID": str(org_a)},
            json={"reason": "retry"},
        )
        assert res.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert res.json()["code"] == "REPLAY_PUBLISH_FAILED"

        job = await job_store.get_job(org_a, job_id)
        assert job is not None
        assert job.state == JobState.DEAD_LETTER.value  # not stranded in RETRY_PENDING
        assert job.last_error is not None and "broker down" in job.last_error
        events = await job_store.list_events_for_job(org_a, job_id)
        assert [e.state_to for e in events][-3:] == ["RETRY_PENDING", "FAILED", "DEAD_LETTER"]

        # The operator can replay again once the broker is back.
        mock_publisher.publish.side_effect = None
        again = await client.post(
            f"/v1/jobs/{job_id}/replay", headers={"X-Organization-ID": str(org_a)}, json={}
        )
        assert again.status_code == status.HTTP_200_OK

    @pytest.mark.parametrize("queue_name", [None, "email.retry.30s", "nonexistent.queue"])
    async def test_replay_unroutable_queue_returns_409_without_state_change(
        self,
        test_app: FastAPI,
        client: AsyncClient,
        org_a: UUID,
        mock_publisher: MagicMock,
        queue_name: str | None,
    ) -> None:
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()
        await job_store.create_job(
            Job(
                id=job_id,
                organization_id=org_a,
                job_type="generate_reply",
                state=JobState.DEAD_LETTER.value,
                queue_name=queue_name,
                idempotency_key=f"idem-{job_id}",
            )
        )
        res = await client.post(
            f"/v1/jobs/{job_id}/replay", headers={"X-Organization-ID": str(org_a)}, json={}
        )
        assert res.status_code == status.HTTP_409_CONFLICT
        assert res.json()["code"] == "JOB_NOT_ROUTABLE"
        job = await job_store.get_job(org_a, job_id)
        assert job is not None and job.state == JobState.DEAD_LETTER.value
        mock_publisher.publish.assert_not_awaited()

    async def test_replay_without_publisher_returns_503_without_state_change(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID
    ) -> None:
        test_app.state.publisher = None
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()
        await job_store.create_job(
            Job(
                id=job_id,
                organization_id=org_a,
                job_type="generate_reply",
                state=JobState.DEAD_LETTER.value,
                queue_name="email.support.normal",
                idempotency_key=f"idem-{job_id}",
            )
        )
        res = await client.post(
            f"/v1/jobs/{job_id}/replay", headers={"X-Organization-ID": str(org_a)}, json={}
        )
        assert res.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert res.json()["code"] == "PUBLISHER_UNAVAILABLE"
        job = await job_store.get_job(org_a, job_id)
        assert job is not None and job.state == JobState.DEAD_LETTER.value
```

Run: `uv run pytest tests/unit/test_job_timeline_and_replay.py`
Expected: 6 failed, 14 passed. The normalize test fails on the wrong exchange (`email.route` instead of `email.process`). The publish-failure, no-publisher and 3 unroutable-queue cases fail with 200 instead of 503/409. The strengthened success test already passes, because the new fixture hands the old code a real `exchange_email_route` of `email.route`. It pins the behaviour for category queues.

- [ ] **Step 2: Rewrite the replay tail**

In `services/api/routers/jobs.py`, change `from typing import Annotated` to `from typing import Annotated, Any`. Add this helper above `replay_job`:

```python
async def _return_to_dead_letter(
    job_store: Any, org_id: UUID, job_id: UUID | str, error: str
) -> None:
    """Compensate a replay whose publish failed: RETRY_PENDING -> FAILED -> DEAD_LETTER.

    The state machine has no direct RETRY_PENDING -> DEAD_LETTER edge.
    """
    payload = {"operator_replay_aborted": True, "reason": error}
    try:
        await job_store.transition_job_state(
            organization_id=org_id,
            job_id=job_id,
            target_state=JobState.FAILED,
            payload=payload,
            error_message=error,
        )
        await job_store.transition_job_state(
            organization_id=org_id,
            job_id=job_id,
            target_state=JobState.DEAD_LETTER,
            payload=payload,
        )
    except Exception:
        logger.exception("Could not return job %s to DEAD_LETTER after failed replay", job_id)
```

Replace everything in `replay_job` from `previous_state = job.state` to the end of the function. The 404 and 409 `JOB_NOT_REPLAYABLE` checks above it stay unchanged.

```python
    # Resolve the redelivery target BEFORE mutating state: an unroutable or unpublishable
    # replay must leave the job in DEAD_LETTER so the operator can retry it.
    if publisher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "Message broker publisher is not available; job was not replayed.",
                "code": "PUBLISHER_UNAVAILABLE",
            },
        )

    routing_key = job.queue_name
    exchange_name = publisher.settings.exchange_for_queue(routing_key)
    if routing_key is None or exchange_name is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    f"Job '{id}' has no routable queue_name ({routing_key!r}); "
                    "cannot determine a replay destination."
                ),
                "code": "JOB_NOT_ROUTABLE",
                "queue_name": routing_key,
            },
        )

    previous_state = job.state
    replay_payload = {
        "operator_replay": True,
        "reason": req_body.reason,
        "reset_attempts": req_body.reset_attempts,
        "exchange": exchange_name,
        "routing_key": routing_key,
    }

    try:
        replayed_job, event = await job_store.replay_job(
            organization_id=org_id,
            job_id=id,
            payload=replay_payload,
            reset_attempts=req_body.reset_attempts,
        )
    except IllegalStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": str(exc), "code": "ILLEGAL_STATE_TRANSITION"},
        ) from exc

    envelope = JobEnvelope(
        job_id=str(replayed_job.id),
        idempotency_key=replayed_job.idempotency_key,
        job_type=replayed_job.job_type,
        organization_id=str(replayed_job.organization_id),
        message_id=str(replayed_job.message_id or ""),
        thread_id=str(replayed_job.thread_id or ""),
        attempt=replayed_job.attempt,
        payload={"replayed": True, "reason": req_body.reason},
    )
    try:
        await publisher.publish(
            exchange_name=exchange_name,
            routing_key=routing_key,
            envelope=envelope,
        )
    except Exception as pub_err:
        error = f"Replay publish to {exchange_name}/{routing_key} failed: {pub_err}"
        logger.error("Failed to republish replayed job %s: %s", replayed_job.id, error)
        await _return_to_dead_letter(job_store, org_id, replayed_job.id, error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": error,
                "code": "REPLAY_PUBLISH_FAILED",
                "exchange": exchange_name,
                "routing_key": routing_key,
            },
        ) from pub_err

    logger.info(
        "Replayed job %s published to exchange %s with key %s",
        replayed_job.id,
        exchange_name,
        routing_key,
    )
    return JobReplayResponse(
        job_id=UUID(str(replayed_job.id)),
        organization_id=UUID(str(replayed_job.organization_id)),
        previous_state=previous_state,
        new_state=replayed_job.state,
        attempt=replayed_job.attempt,
        republished=True,
        routing_key=routing_key,
        replayed_at=event.created_at,
    )
```

Run: `uv run pytest tests/unit/test_job_timeline_and_replay.py`
Expected: all pass.

- [ ] **Step 3: Write the failing upload tests**

Add these to `tests/unit/test_knowledge_api.py`. Import `FakeObjectStorageClient` and `ObjectKeyBuilder` from `packages.core.storage`, `KnowledgeDocument` from `packages.domain.knowledge`, and `InMemoryKnowledgeStore` from `packages.db.knowledge`, if they are not imported already.

```python
@pytest.mark.asyncio
async def test_upload_publish_failure_returns_503_and_marks_document_failed(
    client: AsyncClient, test_app: FastAPI, org_a: UUID, mock_publisher: MagicMock
) -> None:
    mock_publisher.publish.side_effect = ConnectionError("broker down")
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        data={"title": "Policy"},
        files={"file": ("p.md", io.BytesIO(b"# Policy"), "text/markdown")},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "INGEST_ENQUEUE_FAILED"
    doc_id = UUID(body["detail"]["document_id"])
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store
    doc = await store.get_document(org_a, doc_id)
    assert doc is not None
    assert doc.status == "failed"
    assert doc.failure_reason is not None and "broker down" in doc.failure_reason


@pytest.mark.asyncio
async def test_reingest_publish_failure_keeps_previous_active_version(
    client: AsyncClient, test_app: FastAPI, org_a: UUID, mock_publisher: MagicMock
) -> None:
    store: InMemoryKnowledgeStore = test_app.state.knowledge_store
    doc_id = uuid4()
    await store.insert_document(
        KnowledgeDocument(id=doc_id, organization_id=org_a, title="ToS", version=1, status="active")
    )
    mock_publisher.publish.side_effect = ConnectionError("broker down")
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        data={"document_id": str(doc_id)},
        files={"file": ("tos.md", io.BytesIO(b"# New terms"), "text/markdown")},
    )
    assert response.status_code == 503
    doc = await store.get_document(org_a, doc_id)
    assert doc is not None and doc.status == "active" and doc.version == 1
    storage: FakeObjectStorageClient = test_app.state.storage_client
    orphan_key = ObjectKeyBuilder.knowledge_doc(
        organization_id=org_a, document_id=doc_id, version=2, filename="tos.md"
    )
    assert not await storage.object_exists("knowledge-docs", orphan_key)


@pytest.mark.asyncio
async def test_upload_without_publisher_returns_503(
    client: AsyncClient, test_app: FastAPI, org_a: UUID
) -> None:
    test_app.state.publisher = None
    response = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_a)},
        files={"file": ("p.md", io.BytesIO(b"# Policy"), "text/markdown")},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "INGEST_ENQUEUE_FAILED"
```

Run: `uv run pytest tests/unit/test_knowledge_api.py`
Expected: the 3 new tests fail (202 instead of 503).

- [ ] **Step 4: Refuse and compensate when the ingestion job cannot be enqueued**

In `services/api/routers/knowledge.py`:
- Add `import contextlib` to the stdlib imports.
- Change `from typing import Annotated` to `from typing import Annotated, Any`.
- Add this helper above `upload_document`:

```python
async def _compensate_failed_enqueue(
    *,
    knowledge_store: Any,
    storage_client: Any,
    bucket: str,
    new_doc: KnowledgeDocument,
    previous: KnowledgeDocument | None,
    reason: str,
) -> None:
    """Undo an upload whose ingestion job could not be enqueued (R9.10, R23.7).

    - New document: keep the row but mark it ``failed`` with the reason, so it is visible
      and can be re-uploaded via ``document_id``.
    - Re-ingest: restore the previous row (status/version/object_key) so an ``active``
      document stays searchable, and remove the orphaned new-version object.
    """
    try:
        if previous is None:
            await knowledge_store.update_document_status(
                new_doc.organization_id, new_doc.id, status="failed", failure_reason=reason
            )
            return
        await knowledge_store.insert_document(previous)
        if new_doc.object_key and new_doc.object_key != previous.object_key:
            with contextlib.suppress(Exception):
                await storage_client.delete_object(bucket, new_doc.object_key)
    except Exception:
        logger.exception("Failed to compensate un-enqueued upload for document %s", new_doc.id)
```

- In `upload_document`, directly above `if document_id is not None:` (in section "3. Resolve document identity"), add:

```python
        existing: KnowledgeDocument | None = None
```

- Replace the `if publisher is not None: try: ... except ...: logger.warning(...)` block with:

```python
        enqueue_error: str | None = None
        if publisher is None:
            enqueue_error = "Message broker publisher is not available"
        else:
            try:
                await publisher.publish(
                    exchange_name=settings.broker.exchange_knowledge_ingest,
                    routing_key=settings.broker.queue_knowledge,
                    envelope=envelope,
                )
            except Exception as exc:
                enqueue_error = f"Ingestion job publish failed: {exc}"

        if enqueue_error is not None:
            logger.error("Could not enqueue ingestion for document %s: %s", doc_id, enqueue_error)
            await _compensate_failed_enqueue(
                knowledge_store=knowledge_store,
                storage_client=storage_client,
                bucket=bucket,
                new_doc=saved_doc,
                previous=existing,
                reason=enqueue_error,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": (
                        f"Document stored but ingestion could not be enqueued: {enqueue_error}"
                    ),
                    "code": "INGEST_ENQUEUE_FAILED",
                    "document_id": str(doc_id),
                },
            )
```

- Append this sentence to the endpoint's `description=` string. Add it as two implicitly concatenated literals directly after the line `"state, and dispatches a job to the 'knowledge.ingest' queue."`, because a single literal exceeds the 100-character limit (E501):

```python
        " Returns 503 INGEST_ENQUEUE_FAILED (document marked failed, or the previous version "
        "restored on re-ingest) when the ingestion job cannot be enqueued."
```

Run: `uv run pytest tests/unit/test_knowledge_api.py`
Expected: all pass.

- [ ] **Step 5: Integration: a recording publisher for knowledge; a normalize replay**

In `tests/integration/test_knowledge_api_integration.py`:
- Add `from packages.broker.envelope import JobEnvelope`.
- Change the settings import to `from packages.core.settings import AppSettings, BrokerSettings`.
- Add the recording publisher and fixture. It keeps the running knowledge-worker out of these tests, and without a publisher the upload now correctly returns 503.

```python
class _RecordingPublisher:
    """Test double capturing publishes; keeps the live knowledge-worker out of these tests."""

    def __init__(self) -> None:
        self.settings = BrokerSettings()
        self.published: list[tuple[str, str, JobEnvelope]] = []
        self.fail_with: Exception | None = None

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.published.append((exchange_name, routing_key, envelope))


@pytest.fixture
def publisher() -> _RecordingPublisher:
    return _RecordingPublisher()
```

- Change the `api_app` fixture signature to take `publisher: _RecordingPublisher`, and add `app.state.publisher = publisher` before `return app`.
- Append this test:

```python
@pytest.mark.asyncio
async def test_live_upload_enqueue_failure_marks_document_failed(
    client: AsyncClient,
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
    publisher: _RecordingPublisher,
) -> None:
    await storage_client.bootstrap_buckets()
    org_id = uuid4()
    await _ensure_org(db_pool, org_id)
    publisher.fail_with = ConnectionError("broker down")
    res = await client.post(
        "/v1/knowledge/documents",
        headers={"X-Organization-ID": str(org_id)},
        files={"file": ("x.md", io.BytesIO(b"# X"), "text/markdown")},
    )
    assert res.status_code == 503
    doc_id = UUID(res.json()["detail"]["document_id"])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, failure_reason FROM knowledge_document "
            "WHERE id=$1 AND organization_id=$2",
            doc_id,
            org_id,
        )
    assert row is not None and row["status"] == "failed"
    assert "broker down" in row["failure_reason"]
```

In `tests/integration/test_job_timeline_and_replay_integration.py`, append:

```python
@pytest.mark.asyncio
async def test_live_replay_normalize_job_routes_via_email_process(
    db_pool: asyncpg.Pool, broker_channel: AbstractChannel, client: AsyncClient
) -> None:
    b_settings = AppSettings().broker
    await setup_topology(broker_channel, b_settings)
    # Private probe queue observes the publish without depending on any consumer.
    probe = await broker_channel.declare_queue("", exclusive=True, auto_delete=True)
    await probe.bind(b_settings.exchange_email_process, routing_key=b_settings.queue_normalize)

    org_id, mbx_id, thd_id, msg_id, job_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    await _seed_org_mailbox_thread_message(db_pool, org_id, mbx_id, thd_id, msg_id)
    await PostgresJobStore(db_pool).create_job(
        Job(
            id=job_id,
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thd_id,
            job_type="email_pipeline",
            state=JobState.DEAD_LETTER.value,
            queue_name=b_settings.queue_normalize,
            idempotency_key=f"idem-replay-norm-{job_id}",
        )
    )

    resp = await client.post(
        f"/v1/jobs/{job_id}/replay",
        headers={"X-Organization-ID": str(org_id)},
        json={"reason": "it"},
    )
    assert resp.status_code == 200
    assert resp.json()["routing_key"] == "email.normalize"

    msg = await probe.get(no_ack=True, fail=False, timeout=5)
    assert msg is not None
    assert JobEnvelope.model_validate_json(msg.body).job_id == str(job_id)
```

Run: `uv run pytest tests/integration/test_knowledge_api_integration.py tests/integration/test_job_timeline_and_replay_integration.py`
Expected: all pass.

- [ ] **Step 6: Verify and commit**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run mypy packages services tests/unit/test_job_timeline_and_replay.py tests/unit/test_knowledge_api.py tests/integration/test_knowledge_api_integration.py tests/integration/test_job_timeline_and_replay_integration.py`
Expected: clean.

Run: `uv run python -m services.api.openapi --check`
Expected: exit 0. That is the CI OpenAPI validation step.

Run: `uv run ruff format --check services/api/routers/jobs.py services/api/routers/knowledge.py tests/unit/test_knowledge_api.py tests/integration/test_knowledge_api_integration.py`
Expected: 4 files already formatted. `tests/unit/test_job_timeline_and_replay.py` and `tests/integration/test_job_timeline_and_replay_integration.py` were already unformatted before this plan. Their only hunks are collapsible `def` signatures in pre-existing tests, and the code this task adds to them is formatted. Leave those hunks alone.

```bash
git add services/api/routers/jobs.py services/api/routers/knowledge.py \
  tests/unit/test_job_timeline_and_replay.py tests/unit/test_knowledge_api.py \
  tests/integration/test_knowledge_api_integration.py tests/integration/test_job_timeline_and_replay_integration.py
git commit -m "fix(api): replay and upload refuse instead of reporting lost publishes as success [task RA.7] [R18.7, R23.7, R9.1]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 9: Shared worker runtime; topology declared at startup; R5.10 check hosted (RA.8)

No process calls `setup_topology` today; it is called only from tests. `BaseConsumer.start()` used `get_queue(ensure=False)`, which assumes the queue exists (after Task 6, `declare_queue(passive=True)` fails loudly instead). The two existing mains duplicate ~80 lines of lifecycle code, and the R5.10 dimension check exists only as a CLI subcommand. `WorkerRuntime` owns the lifecycle once. Each service contributes a `build_components` function.

```
WorkerRuntime.start()
  settings ─▶ logging + tracer (optional) ─▶ HealthRegistry + GracefulShutdownCoordinator (+ signals)
  ─▶ asyncpg pool ────────────────┐ readiness "database"
  ─▶ aio_pika.connect_robust ─────┤ readiness "broker"
       ├─ declare_topology (R3.2, idempotent, short-lived channel)
       └─ publish channel (on_return_raises=True) ─▶ MessagePublisher
  ─▶ build(resources) -> [start_fn, ...]   (consumers register stop_consuming/close themselves)
  ─▶ register cleanup: publish channel, connection, pool   (after build ⇒ runs after consumer.close)
  ─▶ await each start_fn ─▶ ObservabilityServer(/healthz /readyz /metrics) on PORT
WorkerRuntime.run(): start() then wait for SIGTERM-driven shutdown, then stop the health server
```

**Files:**
- Create: `packages/broker/worker_runtime.py`
- Create: `packages/broker/cli.py`
- Create: `tests/stubs/worker_resources.py`
- Modify (rewrite): `services/email_worker/main.py`
- Modify (rewrite): `services/knowledge_worker/main.py`
- Create: `tests/unit/test_worker_mains.py`
- Create: `tests/integration/test_worker_runtime_integration.py`

**Interfaces:**
- Consumes: `setup_topology` and `MessagePublisher(..., retry_settings=...)` (Task 4); consumer `close` registration (Task 7).
- Produces: `packages.broker.worker_runtime.StartFn = Callable[[], Awaitable[None]]`
- Produces: `WorkerResources` (frozen dataclass with `settings: AppSettings`, `db_pool: asyncpg.Pool[Any]`, `connection: AbstractRobustConnection`, `publisher: MessagePublisher`, `health: HealthRegistry`, `shutdown: GracefulShutdownCoordinator`, `metrics: PipelineMetrics`)
- Produces: `BuildFn = Callable[[WorkerResources], Awaitable[Sequence[StartFn]]]`
- Produces: `WorkerRuntime(*, service_name: str, settings: AppSettings, port: int, build: BuildFn, host: str = "0.0.0.0", install_signal_handlers: bool = True, configure_telemetry: bool = True)`, with methods `start() -> WorkerResources`, `wait() -> None`, `stop(reason: str = "MANUAL") -> None` and `run() -> None`
- Produces: `declare_topology(connection: AbstractRobustConnection, settings: AppSettings) -> None`
- Produces: `packages.broker.cli.declare_from_settings(settings: AppSettings) -> None`, run with `python -m packages.broker.cli declare`
- Produces: in `services.email_worker.main`, `build_consumer(res) -> EmailNormalizationConsumer` and `build_components(res) -> list[StartFn]`
- Produces: in `services.knowledge_worker.main`, `build_consumer(res) -> KnowledgeIngestConsumer`, `build_components(res) -> list[StartFn]` and `assert_vector_dimension(settings: AppSettings) -> None`
- Produces: `tests.stubs.worker_resources.fake_worker_resources(settings: AppSettings) -> WorkerResources`, used by Tasks 10 and 11

- [ ] **Step 1: Write the failing integration tests**

Create `tests/integration/test_worker_runtime_integration.py`:

```python
"""WorkerRuntime lifecycle against the isolated test DB and a scratch vhost (RA.8)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractIncomingMessage

from packages.broker.cli import declare_from_settings
from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import AppSettings
from services.knowledge_worker.main import assert_vector_dimension
from tests.integration.isolation import scratch_vhost


class _RecordingConsumer(BaseConsumer):
    def __init__(self, received: asyncio.Queue[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.received = received

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        await self.received.put(envelope.job_id)


async def test_worker_runtime_declares_topology_serves_health_and_shuts_down(
    unused_tcp_port: int,
) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    async with scratch_vhost(AppSettings().broker, "runtime") as broker:
        settings = AppSettings().model_copy(update={"broker": broker})

        async def build(res: WorkerResources) -> list[StartFn]:
            consumer = _RecordingConsumer(
                received,
                queue_name=broker.queue_normalize,
                broker_settings=broker,
                retry_settings=res.settings.retry,
                connection=res.connection,
                shutdown_coordinator=res.shutdown,
            )
            return [consumer.start]

        runtime = WorkerRuntime(
            service_name="runtime_it",
            settings=settings,
            port=unused_tcp_port,
            host="127.0.0.1",
            build=build,
            install_signal_handlers=False,
            configure_telemetry=False,
        )
        res = await runtime.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{unused_tcp_port}") as http:
                ready = await http.get("/readyz")
                metrics = await http.get("/metrics")
            assert ready.status_code == 200, ready.text
            assert set(ready.json()["checks"]) == {"database", "broker"}
            assert metrics.status_code == 200

            envelope = JobEnvelope(
                idempotency_key=f"rt-{uuid4()}",
                job_type="normalize_email",
                organization_id=str(uuid4()),
            )
            await res.publisher.publish(
                exchange_name=broker.exchange_email_process,
                routing_key=broker.queue_normalize,
                envelope=envelope,
            )
            assert await asyncio.wait_for(received.get(), timeout=5) == envelope.job_id
        finally:
            await runtime.stop("TEST")

    assert res.connection.is_closed
    assert res.db_pool.is_closing()


async def test_worker_runtime_releases_resources_when_build_fails(unused_tcp_port: int) -> None:
    captured: list[WorkerResources] = []
    async with scratch_vhost(AppSettings().broker, "runtimefail") as broker:
        settings = AppSettings().model_copy(update={"broker": broker})

        async def build(res: WorkerResources) -> list[StartFn]:
            captured.append(res)
            raise RuntimeError("bad worker config")

        runtime = WorkerRuntime(
            service_name="runtime_fail_it",
            settings=settings,
            port=unused_tcp_port,
            host="127.0.0.1",
            build=build,
            install_signal_handlers=False,
            configure_telemetry=False,
        )
        with pytest.raises(RuntimeError, match="bad worker config"):
            await runtime.start()

    assert captured[0].connection.is_closed
    assert captured[0].db_pool.is_closing()


async def test_cli_declare_creates_topology() -> None:
    async with scratch_vhost(AppSettings().broker, "cli") as broker:
        await declare_from_settings(AppSettings().model_copy(update={"broker": broker}))
        conn = await aio_pika.connect_robust(broker.url)
        try:
            channel = await conn.channel()
            for queue in (broker.queue_normalize, broker.queue_triage, broker.queue_mail_sync):
                await channel.declare_queue(queue, passive=True)  # raises if missing
        finally:
            await conn.close()


async def test_vector_dimension_check_accepts_match_and_rejects_mismatch() -> None:
    settings = AppSettings()
    await assert_vector_dimension(settings)  # migrations declare VECTOR(1536)
    wrong = settings.model_copy(
        update={"embedding": settings.embedding.model_copy(update={"dimension": 768})}
    )
    with pytest.raises(ValueError, match="Refusing to start"):
        await assert_vector_dimension(wrong)
```

`unused_tcp_port` is provided by pytest-asyncio.

Run: `uv run pytest tests/integration/test_worker_runtime_integration.py`
Expected: collection error, `ModuleNotFoundError: No module named 'packages.broker.cli'` (the first of the not-yet-created modules the test imports).

- [ ] **Step 2: Implement the runtime**

Create `packages/broker/worker_runtime.py`:

```python
"""Process runtime shared by every broker-consuming worker service (R3.2, R20.7, R20.8).

One place owns the resources a worker process needs and their lifecycle:

    settings -> logging/tracing -> health registry + shutdown coordinator
             -> PostgreSQL pool (readiness: database)
             -> RabbitMQ robust connection (readiness: broker)
                  -> topology declaration (R3.2) and a publisher channel that raises on
                     unroutable publishes
             -> build(resources) -> start functions -> /healthz /readyz /metrics server
    SIGTERM  -> drain (consumers stop consuming) -> wait for in-flight jobs -> cleanup
                (consumers close their channels, then publisher channel, connection, pool)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import aio_pika
import asyncpg
from aio_pika.abc import AbstractChannel, AbstractRobustConnection

from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.observability.health import HealthRegistry, ReadinessCheck
from packages.observability.logging import setup_logging
from packages.observability.metrics import PipelineMetrics, get_metrics
from packages.observability.server import ObservabilityServer
from packages.observability.shutdown import GracefulShutdownCoordinator
from packages.observability.tracing import init_tracer

logger = logging.getLogger(__name__)

StartFn = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class WorkerResources:
    """Shared, already-connected resources handed to a service's build function."""

    settings: AppSettings
    db_pool: asyncpg.Pool[Any]
    connection: AbstractRobustConnection
    publisher: MessagePublisher
    health: HealthRegistry
    shutdown: GracefulShutdownCoordinator
    metrics: PipelineMetrics


BuildFn = Callable[[WorkerResources], Awaitable[Sequence[StartFn]]]


async def declare_topology(connection: AbstractRobustConnection, settings: AppSettings) -> None:
    """Declare the full broker topology on a short-lived channel (R3.2, idempotent)."""
    channel = await connection.channel()
    try:
        await setup_topology(
            channel,
            broker_settings=settings.broker,
            retry_settings=settings.retry,
            routing_settings=settings.routing,
        )
    finally:
        if not channel.is_closed:
            await channel.close()


def database_check(pool: asyncpg.Pool[Any]) -> ReadinessCheck:
    """Readiness probe: the pool can run a trivial query."""

    async def check() -> tuple[bool, str]:
        try:
            async with pool.acquire() as conn:
                value = await conn.fetchval("SELECT 1")
            return (value == 1, "Database connection healthy")
        except Exception as err:
            return (False, f"Database check failed: {err}")

    return check


def broker_check(connection: AbstractRobustConnection) -> ReadinessCheck:
    """Readiness probe: the robust broker connection is open."""

    async def check() -> tuple[bool, str]:
        if connection.is_closed:
            return (False, "Broker connection closed")
        return (True, "Broker connection healthy")

    return check


class WorkerRuntime:
    """Owns a worker process's resources, startup order, health server and shutdown."""

    def __init__(
        self,
        *,
        service_name: str,
        settings: AppSettings,
        port: int,
        build: BuildFn,
        host: str = "0.0.0.0",
        install_signal_handlers: bool = True,
        configure_telemetry: bool = True,
    ) -> None:
        self._service_name = service_name
        self._settings = settings
        self._port = port
        self._host = host
        self._build = build
        self._install_signal_handlers = install_signal_handlers
        self._configure_telemetry = configure_telemetry
        self._resources: WorkerResources | None = None
        self._publish_channel: AbstractChannel | None = None
        self._server: ObservabilityServer | None = None

    @property
    def resources(self) -> WorkerResources:
        if self._resources is None:
            raise RuntimeError("WorkerRuntime.start() has not completed")
        return self._resources

    async def start(self) -> WorkerResources:
        """Connect, declare topology, build and start components, then serve health."""
        settings = self._settings
        if self._configure_telemetry:
            setup_logging(
                level=settings.telemetry.log_level,
                json_format=settings.telemetry.log_format == "json",
                service_name=self._service_name,
            )
            init_tracer(
                self._service_name.replace("_", "-"),
                otlp_endpoint=settings.telemetry.otlp_endpoint,
            )
        logger.info("Starting %s", self._service_name)

        health = HealthRegistry(service_name=self._service_name)
        shutdown = GracefulShutdownCoordinator(
            drain_timeout_s=settings.telemetry.drain_timeout_s,
            health_registry=health,
        )
        if self._install_signal_handlers:
            shutdown.attach_signal_handlers()

        db_pool = await create_pool_from_settings(settings.database)
        connection: AbstractRobustConnection | None = None
        try:
            connection = await aio_pika.connect_robust(settings.broker.url)
            health.register_readiness_check("database", database_check(db_pool))
            health.register_readiness_check("broker", broker_check(connection))

            await declare_topology(connection, settings)
            self._publish_channel = await connection.channel(on_return_raises=True)
            publisher = MessagePublisher(
                broker_settings=settings.broker,
                connection=connection,
                channel=self._publish_channel,
                retry_settings=settings.retry,
            )
            resources = WorkerResources(
                settings=settings,
                db_pool=db_pool,
                connection=connection,
                publisher=publisher,
                health=health,
                shutdown=shutdown,
                metrics=get_metrics(),
            )
            start_fns = await self._build(resources)
            # Registered after build(): consumers registered their own close() first, so
            # shared resources are released only after every consumer channel has closed.
            shutdown.register_cleanup_callback(self._release_resources)
            self._resources = resources
            for start_fn in start_fns:
                await start_fn()
        except BaseException:
            await self._abort_start(db_pool, connection)
            raise

        self._server = ObservabilityServer(host=self._host, port=self._port, health_registry=health)
        await self._server.start()
        logger.info("%s started; health server on port %d", self._service_name, self._port)
        return resources

    async def wait(self) -> None:
        """Block until the shutdown sequence completes, then stop the health server."""
        await self.resources.shutdown.wait_until_complete()
        if self._server is not None:
            await self._server.stop()
            self._server = None
        logger.info("%s stopped cleanly", self._service_name)

    async def stop(self, reason: str = "MANUAL") -> None:
        """Trigger the graceful shutdown sequence and wait for it."""
        await self.resources.shutdown.trigger_shutdown(reason)
        await self.wait()

    async def run(self) -> None:
        """Process entry: start, then serve until SIGTERM/SIGINT drains and stops us."""
        await self.start()
        try:
            await self.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self.stop("MANUAL")

    async def _release_resources(self) -> None:
        resources = self.resources
        closers: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        if self._publish_channel is not None and not self._publish_channel.is_closed:
            closers.append(("publisher channel", self._publish_channel.close))
        closers.append(("broker connection", resources.connection.close))
        closers.append(("database pool", resources.db_pool.close))
        for name, closer in closers:
            try:
                await closer()
            except Exception as err:
                logger.warning("Error closing %s: %s", name, err)

    async def _abort_start(
        self, db_pool: asyncpg.Pool[Any], connection: AbstractRobustConnection | None
    ) -> None:
        logger.error("%s failed to start; releasing resources", self._service_name)
        if connection is not None and not connection.is_closed:
            try:
                await connection.close()
            except Exception as err:
                logger.warning("Error closing broker connection: %s", err)
        try:
            await db_pool.close()
        except Exception as err:
            logger.warning("Error closing database pool: %s", err)
```

Create `packages/broker/cli.py`:

```python
"""Broker administration CLI (R3.2): ``python -m packages.broker.cli declare``.

Used by the compose ``init`` job so exchanges, queues and bindings exist before any
service publishes. Every worker also declares idempotently at startup.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aio_pika

from packages.broker.worker_runtime import declare_topology
from packages.core.settings import AppSettings


async def declare_from_settings(settings: AppSettings) -> None:
    """Connect with ``settings.broker`` and declare the full topology idempotently."""
    connection = await aio_pika.connect_robust(settings.broker.url)
    try:
        await declare_topology(connection, settings)
    finally:
        await connection.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="RabbitMQ topology administration (R3.2)")
    parser.add_argument("command", choices=["declare"])
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(declare_from_settings(AppSettings()))
    print("Broker topology declared.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Rewrite the email and knowledge worker mains on the runtime**

Replace `services/email_worker/main.py` entirely:

```python
"""Email Normalization Worker Service entrypoint (Phase 1; R4, R3.2, R20.7, R20.8).

Consumes 'email.normalize', persists canonical messages, and dispatches triage jobs.
Process lifecycle (topology, health, graceful drain) comes from WorkerRuntime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import EmailWorkerSettings
from packages.core.storage import get_storage_client
from packages.db.job import PostgresJobStore
from packages.db.message import PostgresMessageStore
from packages.db.thread import PostgresThreadStore
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.normalizer import EmailNormalizer
from services.email_worker.persister import EmailPersister

SERVICE_NAME = "email_worker"
DEFAULT_PORT = 8002


def build_consumer(res: WorkerResources) -> EmailNormalizationConsumer:
    """Compose the production normalization consumer from shared resources."""
    settings = res.settings
    return EmailNormalizationConsumer(
        normalizer=EmailNormalizer(),
        persister=EmailPersister(
            message_store=PostgresMessageStore(res.db_pool),
            thread_store=PostgresThreadStore(res.db_pool),
        ),
        storage_client=get_storage_client(settings.object_storage),
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.email_worker_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        job_store=PostgresJobStore(res.db_pool),
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    return [build_consumer(res).start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=EmailWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
```

Replace `services/knowledge_worker/main.py` entirely:

```python
"""Knowledge Ingestion Worker Service entrypoint (Phase 3; R9.1, R5.10, R3.2).

Consumes 'knowledge.ingest' and runs parse -> chunk -> embed -> persist. Refuses to start
when the configured embedding dimension disagrees with the VECTOR(n) column (R5.10).
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import AppSettings, KnowledgeWorkerSettings
from packages.core.storage import get_storage_client
from packages.db.job import PostgresJobStore
from packages.db.knowledge import PostgresKnowledgeStore
from packages.db.migrator import verify_database_vector_dimension
from packages.knowledge.embedder import get_embedder
from packages.knowledge.pipeline import KnowledgeIngestionPipeline
from services.knowledge_worker.consumer import KnowledgeIngestConsumer

SERVICE_NAME = "knowledge_worker"
DEFAULT_PORT = 8005


async def assert_vector_dimension(settings: AppSettings) -> None:
    """Fail fast when the embedding dimension disagrees with VECTOR(n) (R5.10).

    Raises ValueError (from assert_embedding_dimension) on mismatch.
    """
    await verify_database_vector_dimension(
        settings.database.asyncpg_dsn,
        configured_dimension=settings.embedding.dimension,
    )


def build_consumer(res: WorkerResources) -> KnowledgeIngestConsumer:
    """Compose the production ingestion consumer from shared resources."""
    settings = res.settings
    pipeline = KnowledgeIngestionPipeline(
        store=PostgresKnowledgeStore(res.db_pool),
        embedder=get_embedder(settings.embedding),
        storage=get_storage_client(settings.object_storage),
        bucket_name=settings.object_storage.bucket_knowledge,
    )
    return KnowledgeIngestConsumer(
        pipeline=pipeline,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.knowledge_worker_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        job_store=PostgresJobStore(res.db_pool),
        metrics=res.metrics,
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    await assert_vector_dimension(res.settings)
    return [build_consumer(res).start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=KnowledgeWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
```

The dimension check passes a DSN, not a pooled connection. `verify_database_vector_dimension` only reuses an argument that is an `asyncpg.Connection`, and a pool's proxy connection is not one.

Run: `uv run pytest tests/integration/test_worker_runtime_integration.py`
Expected: 4 passed.

- [ ] **Step 4: Composition unit tests with a shared fake-resources helper**

Create `tests/stubs/worker_resources.py`:

```python
"""Fake WorkerResources for composition-root unit tests (no broker, no database)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from prometheus_client import CollectorRegistry

from packages.broker.publisher import MessagePublisher
from packages.broker.worker_runtime import WorkerResources
from packages.core.settings import AppSettings
from packages.observability.health import HealthRegistry
from packages.observability.metrics import create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator


def fake_worker_resources(settings: AppSettings) -> WorkerResources:
    """Resources whose pool/connection/publisher are inert mocks; stores built on them do no I/O."""
    return WorkerResources(
        settings=settings,
        db_pool=cast(Any, MagicMock(name="db_pool")),
        connection=cast(Any, MagicMock(name="connection")),
        publisher=cast(MessagePublisher, MagicMock(spec=MessagePublisher)),
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )
```

Create `tests/unit/test_worker_mains.py`:

```python
"""Composition roots of the email and knowledge workers (RA.8)."""

from __future__ import annotations

from packages.core.settings import EmailWorkerSettings, KnowledgeWorkerSettings
from services.email_worker import main as email_main
from services.knowledge_worker import main as knowledge_main
from tests.stubs.worker_resources import fake_worker_resources


def test_email_worker_consumer_uses_shared_resources() -> None:
    settings = EmailWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    consumer = email_main.build_consumer(res)

    assert consumer.queue_name == settings.broker.queue_normalize
    assert consumer._connection is res.connection
    assert consumer.prefetch_count == settings.concurrency.email_worker_concurrency
    assert consumer.shutdown_coordinator is res.shutdown


def test_knowledge_worker_consumer_uses_shared_resources() -> None:
    settings = KnowledgeWorkerSettings(_env_file=None)
    res = fake_worker_resources(settings)

    consumer = knowledge_main.build_consumer(res)

    assert consumer.queue_name == settings.broker.queue_knowledge
    assert consumer._connection is res.connection
    assert consumer.prefetch_count == settings.concurrency.knowledge_worker_concurrency
    assert consumer.shutdown_coordinator is res.shutdown
```

Run: `uv run pytest tests/unit/test_worker_mains.py tests/unit/test_production_imports.py`
Expected: all pass. The import walk now also covers `packages.broker.worker_runtime` and `packages.broker.cli`.

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit && uv run pytest tests/integration`
Expected: all pass.

Run: `uv run ruff check . && uv run mypy packages services tests/stubs/worker_resources.py tests/unit/test_worker_mains.py tests/integration/test_worker_runtime_integration.py`
Expected: clean.

```bash
git add packages/broker/worker_runtime.py packages/broker/cli.py services/email_worker/main.py \
  services/knowledge_worker/main.py tests/stubs/worker_resources.py tests/unit/test_worker_mains.py \
  tests/integration/test_worker_runtime_integration.py
git commit -m "feat(runtime): shared worker runtime declares topology at startup and hosts the R5.10 check [task RA.8] [R3.2, R20.7, R20.8, R5.10]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Triage worker entrypoint (RA.9)

`services/triage_worker/` has a tested `TriageConsumer` but no `main.py`, so the compose container runs the placeholder. The composition below was prototyped against the real code with in-memory stores. A billing email shaped exactly like the email worker's envelope was classified by rule, published to `email.route` with key `email.billing.priority`, and left the job `QUEUED`, with 0 LLM calls.

**Files:**
- Modify: `packages/core/settings.py` (add `TriageSettings.templates_path`)
- Modify: `docs/configuration.md` §2.8, `.env.example` §8
- Create: `services/triage_worker/main.py`
- Create: `tests/unit/test_triage_worker_main.py`

**Interfaces:**
- Consumes: `WorkerRuntime`, `WorkerResources`, `StartFn` (Task 9); `fake_worker_resources` (Task 9).
- Produces: `TriageSettings.templates_path: str = "config/templates.yaml"`
- Produces: `services.triage_worker.main.TriageWorkerConfigError(RuntimeError)`
- Produces: `build_triage_consumer(settings: AppSettings, *, publisher: MessagePublisher, job_store: JobStore, message_store: MessageStore, draft_store: DraftStore, connection: AbstractRobustConnection | None = None, shutdown_coordinator: GracefulShutdownCoordinator | None = None) -> TriageConsumer`
- Produces: `build_components(res: WorkerResources) -> list[StartFn]`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_triage_worker_main.py`:

```python
"""Unit tests for the triage worker composition root (RA.9)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import TriageSettings, TriageWorkerSettings
from packages.db.draft import InMemoryDraftStore
from packages.db.job import InMemoryJobStore
from packages.db.message import InMemoryMessageStore
from packages.domain.entities import EmailAddress, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.llm.fake import FakeLLMProvider
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.consumer import TriageConsumer
from services.triage_worker.main import (
    TriageWorkerConfigError,
    build_components,
    build_triage_consumer,
)
from tests.stubs.worker_resources import fake_worker_resources

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_cwd(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(REPO_ROOT)  # settings paths are cwd-relative, like WORKDIR /app
    return REPO_ROOT


def _settings(**triage_overrides: str) -> TriageWorkerSettings:
    triage = TriageSettings(**triage_overrides) if triage_overrides else TriageSettings()
    return TriageWorkerSettings(_env_file=None, triage=triage)


def _publisher() -> MagicMock:
    pub = MagicMock(spec=MessagePublisher)
    pub.publish = AsyncMock()
    return pub


def _build(settings: TriageWorkerSettings, **stores: Any) -> TriageConsumer:
    return build_triage_consumer(
        settings,
        publisher=stores.get("publisher", _publisher()),
        job_store=stores.get("job_store", InMemoryJobStore()),
        message_store=stores.get("message_store", InMemoryMessageStore()),
        draft_store=stores.get("draft_store", InMemoryDraftStore()),
    )


def test_templates_path_setting_default() -> None:
    assert TriageSettings().templates_path == "config/templates.yaml"


def test_build_wires_components_from_settings(repo_cwd: Path) -> None:
    settings = _settings()
    job_store, draft_store = InMemoryJobStore(), InMemoryDraftStore()
    consumer = _build(settings, job_store=job_store, draft_store=draft_store)

    assert consumer.queue_name == "email.triage"
    assert consumer.route_exchange == "email.route"
    assert consumer.prefetch_count == settings.concurrency.triage_worker_concurrency
    assert consumer.cascade.rule_engine.rules_path == Path(settings.triage.rules_path)
    assert consumer.cascade.rule_engine.rules_count > 0
    assert isinstance(consumer.cascade.ml_classifier, MLClassifier)
    assert isinstance(consumer.cascade.llm_classifier.provider, FakeLLMProvider)
    assert consumer.cascade.gate is consumer.gate
    assert consumer.job_store is job_store and consumer.gate.job_store is job_store
    assert consumer.gate.draft_store is draft_store
    assert consumer.gate.template_registry is not None
    assert consumer.gate.template_registry.find_template("acknowledgement", "receipt_confirmation")
    assert (
        consumer.cascade.threshold_manager.get_threshold("ml")
        == settings.triage.ml_confidence_threshold
    )


@pytest.mark.parametrize(
    ("field", "label"),
    [("rules_path", "rules"), ("templates_path", "templates"), ("ml_model_path", "ML classifier")],
)
def test_build_fails_fast_when_asset_missing(
    repo_cwd: Path, tmp_path: Path, field: str, label: str
) -> None:
    settings = _settings(**{field: str(tmp_path / "missing.file")})
    with pytest.raises(TriageWorkerConfigError, match=label):
        _build(settings)


def test_build_rejects_rules_file_with_zero_rules(repo_cwd: Path, tmp_path: Path) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text("rules: [unclosed", encoding="utf-8")
    with pytest.raises(TriageWorkerConfigError, match="zero rules"):
        _build(_settings(rules_path=str(bad)))


def test_build_rejects_template_with_unresolvable_body(repo_cwd: Path, tmp_path: Path) -> None:
    tpl = tmp_path / "templates.yaml"
    tpl.write_text(
        "templates:\n"
        "  - id: t1\n    version: v1\n"
        "    match: {category: acknowledgement, intent: receipt_confirmation}\n"
        "    subject: 'Re: x'\n    body: prompts/templates/does_not_exist.txt\n",
        encoding="utf-8",
    )
    with pytest.raises(TriageWorkerConfigError, match="does_not_exist.txt"):
        _build(_settings(templates_path=str(tpl)))


async def test_composed_consumer_routes_email_worker_envelope(repo_cwd: Path) -> None:
    """Real rules+ML+gate path on the exact envelope shape the email worker emits."""
    job_store, message_store, publisher = InMemoryJobStore(), InMemoryMessageStore(), _publisher()
    consumer = _build(
        _settings(), job_store=job_store, message_store=message_store, publisher=publisher
    )

    org, mbx, mid, tid = uuid4(), uuid4(), uuid4(), uuid4()
    msg = NormalizedMessage(
        message_id=mid,
        thread_id=tid,
        mailbox_id=mbx,
        organization_id=org,
        provider="gmail",
        provider_message_id="prov-urgent-1",
        sender=EmailAddress(email="client@enterprise.com", name="Client"),
        received_at=datetime.now(UTC),
        subject="Urgent: Overdue payment failure on account",
        body_text="Your account balance is past due with repeated payment failure. Please advise.",
    )
    await message_store.insert_message(msg)
    job = Job(
        id=uuid4(),
        organization_id=org,
        idempotency_key=f"idem-{uuid4()}",
        job_type="email_pipeline",
        state=JobState.NORMALIZED,
        message_id=mid,
        thread_id=tid,
    )
    await job_store.create_job(job)

    envelope = JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"triage:{org}:{mbx}:prov-urgent-1",
        organization_id=str(org),
        mailbox_id=str(mbx),
        message_id=str(mid),
        thread_id=str(tid),
        job_type="triage_email",
        payload={
            "message_id": str(mid),
            "thread_id": str(tid),
            "organization_id": str(org),
            "mailbox_id": str(mbx),
            "provider_message_id": "prov-urgent-1",
            "direction": "inbound",
            "received_at": msg.received_at.isoformat(),
        },
    )
    await consumer.process_job(envelope, MagicMock())

    publisher.publish.assert_awaited_once()
    kwargs = publisher.publish.await_args.kwargs
    assert kwargs["exchange_name"] == "email.route"
    assert kwargs["routing_key"] == "email.billing.priority"
    assert kwargs["envelope"].job_type == "generate_reply"
    assert kwargs["envelope"].classification["decided_by"] == "rule"
    stored = await job_store.get_job(org, job.id)
    assert stored is not None and stored.state == JobState.QUEUED


async def test_build_components_returns_consumer_start(repo_cwd: Path) -> None:
    settings = _settings()
    start_fns = await build_components(fake_worker_resources(settings))
    assert len(start_fns) == 1
    start = start_fns[0]
    assert isinstance(start, MethodType)
    consumer = start.__self__
    assert isinstance(consumer, TriageConsumer)
    assert consumer.queue_name == settings.broker.queue_triage
```

Run: `uv run pytest tests/unit/test_triage_worker_main.py`
Expected: collection error, `ModuleNotFoundError: No module named 'services.triage_worker.main'`.

- [ ] **Step 2: Add the templates setting**

In `packages/core/settings.py` `TriageSettings`, directly after the `ml_model_path` field:

```python
    templates_path: str = Field(
        default="config/templates.yaml",
        description="Path to declarative response templates YAML (R6.13, R6.14)",
    )
```

In `docs/configuration.md` §2.8, add a row after `TRIAGE__ML_MODEL_PATH`:

```markdown
| `TRIAGE__TEMPLATES_PATH` | `string` | `config/templates.yaml` | Valid file path | Deterministic reply templates; the triage worker refuses to start if a template's body file cannot be resolved (R6.13, R6.14) |
```

In `.env.example`, add `TRIAGE__TEMPLATES_PATH=config/templates.yaml` after `TRIAGE__ML_MODEL_PATH=...`.

- [ ] **Step 3: Implement the entrypoint**

Create `services/triage_worker/main.py`:

```python
"""Triage Worker Service entrypoint (RA.9; R6.1, R6.2, R6.5, R7.1, R3.4).

Consumes triage jobs from 'email.triage', runs the cascading classifier
(rules -> ML -> LLM), evaluates the early-exit gate, and routes actionable mail to
'email.route' as 'email.<category>.<lane>'. Missing rules, templates, or model files stop
the worker at startup instead of degrading silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aio_pika.abc import AbstractRobustConnection

from packages.broker.publisher import MessagePublisher
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import AppSettings, TriageWorkerSettings
from packages.db.draft import DraftStore, PostgresDraftStore
from packages.db.job import JobStore, PostgresJobStore
from packages.db.message import MessageStore, PostgresMessageStore
from packages.domain.templates import TemplateRegistry
from packages.llm.factory import create_llm_provider
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.consumer import TriageConsumer
from services.triage_worker.gate import EarlyExitGate
from services.triage_worker.llm_classifier import LLMTriageClassifier
from services.triage_worker.rules import HotReloadableRuleEngine
from services.triage_worker.template_loader import load_templates_from_file
from services.triage_worker.thresholds import ThresholdManager

logger = logging.getLogger("triage_worker.service")

SERVICE_NAME = "triage_worker"
DEFAULT_PORT = 8003
_FILE_BODY_SUFFIXES = (".txt", ".j2", ".md")


class TriageWorkerConfigError(RuntimeError):
    """A required triage asset is missing or unusable; the worker must not start."""


def _require_file(raw_path: str, what: str) -> Path:
    path = Path(raw_path)
    if not path.is_file():
        raise TriageWorkerConfigError(
            f"{what} not found at '{path}' (resolved against cwd '{Path.cwd()}')"
        )
    return path


def _load_rule_engine(rules_path: Path) -> HotReloadableRuleEngine:
    # HotReloadableRuleEngine swallows parse errors and falls back to an empty engine;
    # an empty rule set at startup is a misconfiguration, not a default.
    engine = HotReloadableRuleEngine(rules_path=rules_path)
    if engine.rules_count == 0:
        raise TriageWorkerConfigError(
            f"Triage rules file '{rules_path}' produced zero rules (invalid YAML or empty list)"
        )
    return engine


def _load_template_registry(templates_path: Path) -> TemplateRegistry:
    try:
        registry = load_templates_from_file(templates_path)
    except Exception as exc:
        raise TriageWorkerConfigError(
            f"Invalid response templates file '{templates_path}': {exc}"
        ) from exc
    # The gate renders without base_dir, so file bodies resolve against cwd only; an
    # unresolved body would be rendered as the literal path string (templates.py).
    for template in registry.templates:
        body = template.body
        if body.endswith(_FILE_BODY_SUFFIXES) and registry.resolve_body(template) == body:
            raise TriageWorkerConfigError(
                f"Template '{template.id}' body file '{body}' not found "
                f"(resolved against cwd '{Path.cwd()}')"
            )
    return registry


def _load_ml_classifier(model_path: Path) -> MLClassifier:
    try:
        return MLClassifier.load_from_artifact(model_path)
    except (FileNotFoundError, ValueError) as exc:
        raise TriageWorkerConfigError(str(exc)) from exc


def build_triage_consumer(
    settings: AppSettings,
    *,
    publisher: MessagePublisher,
    job_store: JobStore,
    message_store: MessageStore,
    draft_store: DraftStore,
    connection: AbstractRobustConnection | None = None,
    shutdown_coordinator: GracefulShutdownCoordinator | None = None,
) -> TriageConsumer:
    """Compose the production TriageConsumer from settings and injected stores."""
    triage_cfg = settings.triage
    rule_engine = _load_rule_engine(_require_file(triage_cfg.rules_path, "Triage rules file"))
    template_registry = _load_template_registry(
        _require_file(triage_cfg.templates_path, "Response templates file")
    )
    ml_classifier = _load_ml_classifier(
        _require_file(triage_cfg.ml_model_path, "ML classifier artifact")
    )

    if settings.llm.provider.strip().lower() == "fake":
        logger.warning(
            "LLM provider is 'fake': Stage 3 triage returns FakeLLMProvider's canned "
            "classification. Set LLM__PROVIDER for real classification."
        )
    llm_classifier = LLMTriageClassifier(provider=create_llm_provider(settings.llm))

    gate = EarlyExitGate(
        job_store=job_store,
        template_registry=template_registry,
        draft_store=draft_store,
    )
    cascade = CascadingTriageEngine(
        rule_engine=rule_engine,
        ml_classifier=ml_classifier,
        llm_classifier=llm_classifier,
        threshold_manager=ThresholdManager(settings=triage_cfg),
        template_registry=template_registry,
        draft_store=draft_store,
        gate=gate,
    )
    return TriageConsumer(
        cascade=cascade,
        gate=gate,
        publisher=publisher,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.triage_worker_concurrency,
        connection=connection,
        job_store=job_store,
        message_store=message_store,
        shutdown_coordinator=shutdown_coordinator,
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    consumer = build_triage_consumer(
        res.settings,
        publisher=res.publisher,
        job_store=PostgresJobStore(res.db_pool),
        message_store=PostgresMessageStore(res.db_pool),
        draft_store=PostgresDraftStore(res.db_pool),
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
    )
    aclose = getattr(consumer.cascade.llm_classifier.provider, "aclose", None)
    if aclose is not None:
        # Registered after the consumer's close(): the HTTP client closes after draining.
        res.shutdown.register_cleanup_callback(aclose)
    return [consumer.start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=TriageWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
```

Design notes:
- **No `ClassificationStore` is wired.** `process_job` never passes `persist=True`, so wiring one would be a connection that does nothing. R6.7 persistence is W4.
- **Templates do not hot-reload.** `HotReloadableTemplateRegistry` has no `render()`, so `load_templates_from_file` is used. Rules do still hot-reload.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_triage_worker_main.py tests/unit/test_production_imports.py tests/unit/test_dependency_rules.py`
Expected: all pass. The dependency test confirms that `services/triage_worker/main.py` contains no provider string literals.

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run mypy packages services tests/unit/test_triage_worker_main.py`
Expected: clean.

```bash
git add packages/core/settings.py docs/configuration.md .env.example services/triage_worker/main.py \
  tests/unit/test_triage_worker_main.py
git commit -m "feat(triage): production entrypoint that fails fast on missing rules, templates or model [task RA.9] [R6.1, R6.2, R6.5, R7.1, R3.4]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Mail connector entrypoint and background jobs (RA.10)

Nothing consumes `mail.sync.requested` today, and nothing schedules subscription renewal, the queue monitor or the lease reaper. This task adds all of them.
- **`MailSyncConsumer`.** Maps `sync_mailbox` envelopes from the webhook receivers and `POST /v1/mailboxes/{id}/resync` onto `SyncOrchestrator.sync_mailbox`. An unresolvable or cross-tenant envelope is a `FatalError`, so it is dead-lettered and never retried.
- **Mail-connector hosts the periodic work:** the renewal loop, the stack's single `QueueMonitor`, and the `LeaseReaper`. The reaper is **disabled by default**, because enabling it today would re-drive parked jobs wrongly: leases are never cleared and `queue_name` is never written (W3).

```
webhook (api) ─┐                                   ┌─▶ MinIO raw-mime (archive)
resync  (api) ─┼─▶ mail.ingest ─▶ mail.sync.requested ─▶ MailSyncConsumer ─▶ SyncOrchestrator ─┼─▶ email.process/email.normalize
               │                                   │   Fatal ─▶ dlx.email ─▶ email.dead_letter
               │                                   │   Transient ─▶ retry.email.<tier> ─▶ retry.return ─▶ mail.ingest (Task 4)
mail-connector also hosts: BackgroundLoop(renewal) · QueueMonitor · LeaseReaper (only if LEASE_REAPER__ENABLED=true)
```

**Files:**
- Modify: `packages/core/settings.py` (`LeaseReaperSettings.enabled` default → `False`); `docs/configuration.md` §2.17; `.env.example`
- Modify: `services/mail_connector/orchestrator.py` (`sync_mailbox` gains `full_resync`; resolver moves inside the `try`)
- Create: `services/mail_connector/consumer.py`
- Create: `services/mail_connector/background.py`
- Create: `services/mail_connector/main.py`
- Create: `tests/unit/test_mail_sync_consumer.py`
- Create: `tests/unit/test_mail_connector_background.py`
- Create: `tests/unit/test_mail_connector_main.py`
- Modify: `tests/unit/test_sync_orchestrator.py` (append two tests)

**Interfaces:**
- Consumes: `WorkerRuntime` and friends (Task 9); `fake_worker_resources` (Task 9).
- Produces: `SyncOrchestrator.sync_mailbox(mailbox, adapter=None, *, full_resync: bool = False) -> SyncOutcome`
- Produces: `MailSyncConsumer(orchestrator, mailbox_store, adapter_resolver=None, broker_settings=None, retry_settings=None, prefetch_count=None, connection=None, shutdown_coordinator=None, metrics=None)`
- Produces: `BackgroundLoop(name: str, run_loop: Callable[[asyncio.Event], Awaitable[None]], stop_timeout_s: float = 15.0)`, with `async start()`, `async stop()` and `running: bool`
- Produces: in `services.mail_connector.main`, `build_components(res) -> list[StartFn]`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mail_sync_consumer.py`:

```python
"""Unit tests for MailSyncConsumer (RA.10; R2.1, R2.11, R3.5, R23.6)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import CollectorRegistry

from packages.adapters.exceptions import NotFound
from packages.adapters.fake import FakeProviderAdapter
from packages.broker.consumer import FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings
from packages.core.storage import FakeObjectStorageClient
from packages.db.checkpoint import InMemoryCheckpointStore
from packages.db.mailbox import InMemoryMailboxStore
from packages.domain.entities import Checkpoint, Mailbox
from packages.observability.metrics import create_pipeline_metrics
from services.mail_connector.consumer import MailSyncConsumer
from services.mail_connector.orchestrator import SyncOrchestrator


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, JobEnvelope]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((exchange_name, routing_key, envelope))


def make_mock_message(envelope: JobEnvelope) -> MagicMock:
    msg = MagicMock(spec=AbstractIncomingMessage)
    msg.body = envelope.model_dump_json().encode("utf-8")
    msg.exchange = "mail.ingest"
    msg.routing_key = "mail.sync.requested"
    msg.headers = {}
    msg.ack = AsyncMock()
    return msg


def sync_envelope(mailbox_id: str | None, org_id: str, **payload: Any) -> JobEnvelope:
    return JobEnvelope(
        idempotency_key=f"{org_id}:{mailbox_id}:sync:test",
        job_type="sync_mailbox",
        organization_id=org_id,
        mailbox_id=mailbox_id,
        payload={"provider": "fake", **payload},
    )


@pytest.fixture
def mailbox() -> Mailbox:
    return Mailbox(id=uuid4(), organization_id=uuid4(), provider="fake", address="ops@example.com")


@pytest.fixture
def adapter() -> FakeProviderAdapter:
    a = FakeProviderAdapter()
    a.seed_message(provider_message_id="m-1", raw_payload=b"From: a@b.com\n\n1", history_id="h-1")
    a.seed_message(provider_message_id="m-2", raw_payload=b"From: c@d.com\n\n2", history_id="h-2")
    return a


@pytest.fixture
def cp_store() -> InMemoryCheckpointStore:
    return InMemoryCheckpointStore()


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def mailbox_store(mailbox: Mailbox) -> InMemoryMailboxStore:
    return InMemoryMailboxStore([mailbox])


@pytest.fixture
def consumer(
    cp_store: InMemoryCheckpointStore,
    publisher: RecordingPublisher,
    mailbox_store: InMemoryMailboxStore,
    adapter: FakeProviderAdapter,
) -> MailSyncConsumer:
    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=FakeObjectStorageClient(),
        publisher=publisher,
        adapter_resolver=lambda _: adapter,
        mailbox_store=mailbox_store,
    )
    return MailSyncConsumer(
        orchestrator=orchestrator,
        mailbox_store=mailbox_store,
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )


async def test_sync_envelope_runs_orchestrator(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    publisher: RecordingPublisher,
    cp_store: InMemoryCheckpointStore,
) -> None:
    env = sync_envelope(
        str(mailbox.id), str(mailbox.organization_id), manual=True, full_resync=False
    )
    await consumer.process_job(env, make_mock_message(env))

    assert [rk for _, rk, _ in publisher.published] == ["email.normalize", "email.normalize"]
    assert {e.mailbox_id for _, _, e in publisher.published} == {str(mailbox.id)}
    cp = await cp_store.get(mailbox.id)
    assert cp is not None and cp.history_id == "h-2" and cp.sync_state == "idle"


@pytest.mark.parametrize("bad_id", [None, "", "unresolved-1a2b3c4d", "Users/u@x.com/Messages/m1"])
async def test_unresolvable_mailbox_id_is_fatal(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    publisher: RecordingPublisher,
    bad_id: str | None,
) -> None:
    env = sync_envelope(bad_id, str(mailbox.organization_id))
    with pytest.raises(FatalError, match="Unresolvable mailbox_id"):
        await consumer.process_job(env, make_mock_message(env))
    assert publisher.published == []


async def test_missing_mailbox_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, publisher: RecordingPublisher
) -> None:
    env = sync_envelope(str(uuid4()), str(mailbox.organization_id))
    with pytest.raises(FatalError, match="not found"):
        await consumer.process_job(env, make_mock_message(env))
    assert publisher.published == []


@pytest.mark.parametrize("org", ["", "not-a-uuid"])
async def test_invalid_organization_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, org: str
) -> None:
    env = sync_envelope(str(mailbox.id), org)
    with pytest.raises(FatalError, match="organization_id"):
        await consumer.process_job(env, make_mock_message(env))


async def test_tenant_mismatch_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, publisher: RecordingPublisher
) -> None:
    env = sync_envelope(str(mailbox.id), "00000000-0000-0000-0000-000000000000")
    with pytest.raises(FatalError, match="does not belong"):
        await consumer.process_job(env, make_mock_message(env))
    assert publisher.published == []


async def test_wrong_job_type_is_fatal(consumer: MailSyncConsumer, mailbox: Mailbox) -> None:
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id)).model_copy(
        update={"job_type": "normalize_email"}
    )
    with pytest.raises(FatalError, match="Unsupported job_type"):
        await consumer.process_job(env, make_mock_message(env))


@pytest.mark.parametrize("status", ["needs_reauth", "paused"])
async def test_non_syncing_status_is_skipped(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    mailbox_store: InMemoryMailboxStore,
    publisher: RecordingPublisher,
    cp_store: InMemoryCheckpointStore,
    status: str,
) -> None:
    await mailbox_store.update_status(mailbox.id, status)
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    await consumer.process_job(env, make_mock_message(env))  # no raise -> ack
    assert publisher.published == []
    assert await cp_store.get(mailbox.id) is None  # lock never taken


async def test_unregistered_provider_is_fatal_and_takes_no_lock(
    cp_store: InMemoryCheckpointStore,
    publisher: RecordingPublisher,
    mailbox: Mailbox,
    mailbox_store: InMemoryMailboxStore,
) -> None:
    def resolver(_: Mailbox) -> FakeProviderAdapter:
        raise NotFound("No mail provider adapter registered", provider="unknown")

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=FakeObjectStorageClient(),
        publisher=publisher,
        adapter_resolver=resolver,
    )
    consumer = MailSyncConsumer(
        orchestrator=orchestrator,
        mailbox_store=mailbox_store,
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(FatalError, match="No usable provider adapter"):
        await consumer.process_job(env, make_mock_message(env))
    assert await cp_store.try_acquire_lock(mailbox.id, mailbox.organization_id) is True


async def test_rate_limited_raises_transient(
    consumer: MailSyncConsumer, mailbox: Mailbox, adapter: FakeProviderAdapter
) -> None:
    adapter.inject_rate_limit(retry_after=12.0)
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(TransientError, match="rate limited"):
        await consumer.process_job(env, make_mock_message(env))


async def test_transient_provider_error_raises_transient(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    adapter: FakeProviderAdapter,
    cp_store: InMemoryCheckpointStore,
) -> None:
    adapter.inject_transient_failure()
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(TransientError):
        await consumer.process_job(env, make_mock_message(env))
    cp = await cp_store.get(mailbox.id)
    assert cp is not None and cp.sync_state == "error"  # lock released


async def test_permanent_provider_error_is_fatal(
    consumer: MailSyncConsumer, mailbox: Mailbox, adapter: FakeProviderAdapter
) -> None:
    adapter.inject_permanent_failure()
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    with pytest.raises(FatalError, match="Permanent provider failure"):
        await consumer.process_job(env, make_mock_message(env))


async def test_auth_expired_acks_and_marks_mailbox(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    adapter: FakeProviderAdapter,
    mailbox_store: InMemoryMailboxStore,
) -> None:
    adapter.inject_auth_expired()
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id))
    await consumer.process_job(env, make_mock_message(env))
    stored = await mailbox_store.get(mailbox.id)
    assert stored is not None and stored.status == "needs_reauth"


async def test_full_resync_resets_cursor(
    consumer: MailSyncConsumer,
    mailbox: Mailbox,
    cp_store: InMemoryCheckpointStore,
    publisher: RecordingPublisher,
) -> None:
    await cp_store.save(
        Checkpoint(
            mailbox_id=mailbox.id,
            organization_id=mailbox.organization_id,
            history_id="h-2",
            sync_state="idle",
        )
    )
    env = sync_envelope(
        str(mailbox.id), str(mailbox.organization_id), manual=True, full_resync=True
    )
    await consumer.process_job(env, make_mock_message(env))
    assert len(publisher.published) == 2
    cp = await cp_store.get(mailbox.id)
    assert cp is not None and cp.last_full_sync_at is not None


async def test_full_resync_while_busy_is_deferred(
    consumer: MailSyncConsumer, mailbox: Mailbox, cp_store: InMemoryCheckpointStore
) -> None:
    assert await cp_store.try_acquire_lock(mailbox.id, mailbox.organization_id)
    env = sync_envelope(str(mailbox.id), str(mailbox.organization_id), full_resync=True)
    with pytest.raises(TransientError, match="busy"):
        await consumer.process_job(env, make_mock_message(env))
    assert await cp_store.has_pending_followup(mailbox.id) is True


async def test_handle_message_dead_letters_unresolvable_and_acks(
    consumer: MailSyncConsumer, mailbox: Mailbox
) -> None:
    pub = MagicMock(spec=MessagePublisher)
    pub.settings = BrokerSettings()
    pub.publish_to_retry = AsyncMock()
    pub.publish_to_dead_letter = AsyncMock()
    consumer._publisher = pub

    env = sync_envelope("unresolved-deadbeef", str(mailbox.organization_id))
    msg = make_mock_message(env)
    await consumer._handle_message(msg)

    pub.publish_to_dead_letter.assert_awaited_once()
    kwargs = pub.publish_to_dead_letter.call_args.kwargs
    assert kwargs["origin_exchange"] == "mail.ingest"
    assert kwargs["origin_routing_key"] == "mail.sync.requested"
    pub.publish_to_retry.assert_not_awaited()
    msg.ack.assert_awaited_once()
```

Append to `tests/unit/test_sync_orchestrator.py`, adding `from packages.adapters.exceptions import NotFound` to its imports:

```python
@pytest.mark.asyncio
async def test_orchestrator_resolver_failure_releases_lock(test_mailbox: Mailbox) -> None:
    cp_store = InMemoryCheckpointStore()

    def resolver(_: Mailbox) -> FakeProviderAdapter:
        raise NotFound("unregistered provider", provider="unknown")

    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=FakeObjectStorageClient(),
        publisher=MockPublisher(),
        adapter_resolver=resolver,
    )
    with pytest.raises(NotFound):
        await orchestrator.sync_mailbox(test_mailbox)
    cp = await cp_store.get(test_mailbox.id)
    assert cp is not None and cp.sync_state == "error"


@pytest.mark.asyncio
async def test_orchestrator_full_resync_replays_window(
    test_mailbox: Mailbox, fake_adapter: FakeProviderAdapter
) -> None:
    cp_store = InMemoryCheckpointStore()
    orchestrator = SyncOrchestrator(
        checkpoint_store=cp_store,
        storage_client=FakeObjectStorageClient(),
        publisher=MockPublisher(),
        adapter_resolver=lambda _: fake_adapter,
    )
    first = await orchestrator.sync_mailbox(test_mailbox)
    again = await orchestrator.sync_mailbox(test_mailbox)
    replay = await orchestrator.sync_mailbox(test_mailbox, full_resync=True)
    assert (first.messages_synced, again.messages_synced, replay.messages_synced) == (3, 0, 3)
    cp = await cp_store.get(test_mailbox.id)
    assert cp is not None and cp.last_full_sync_at is not None and cp.sync_state == "idle"
```

Create `tests/unit/test_mail_connector_background.py`:

```python
"""BackgroundLoop start/stop semantics (RA.10, R20.8, R2.10)."""

from __future__ import annotations

import asyncio
import time

from prometheus_client import CollectorRegistry

from packages.adapters.fake import FakeProviderAdapter
from packages.db.mailbox import InMemoryMailboxStore
from packages.db.subscription import InMemorySubscriptionStore
from packages.observability.metrics import create_pipeline_metrics
from services.mail_connector.background import BackgroundLoop
from services.mail_connector.renewal import SubscriptionRenewalJob


async def test_stop_signals_event_and_waits() -> None:
    started = asyncio.Event()

    async def run(stop: asyncio.Event) -> None:
        started.set()
        await stop.wait()

    loop = BackgroundLoop("t", run, stop_timeout_s=1.0)
    await loop.start()
    await asyncio.wait_for(started.wait(), 1.0)
    await loop.stop()
    assert not loop.running


async def test_stop_cancels_loop_that_ignores_event() -> None:
    cancelled = asyncio.Event()

    async def run(stop: asyncio.Event) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    loop = BackgroundLoop("t", run, stop_timeout_s=0.05)
    await loop.start()
    await asyncio.sleep(0)
    await loop.stop()
    assert cancelled.is_set() and not loop.running


async def test_renewal_loop_stops_promptly() -> None:
    job = SubscriptionRenewalJob(
        subscription_store=InMemorySubscriptionStore(),
        mailbox_store=InMemoryMailboxStore(),
        adapter_resolver=lambda _: FakeProviderAdapter(),
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )
    loop = BackgroundLoop(
        "renewal", lambda s: job.run_loop(stop_event=s, interval_seconds=3600.0), 2.0
    )
    await loop.start()
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await loop.stop()
    assert time.monotonic() - t0 < 1.0
```

Create `tests/unit/test_mail_connector_main.py`:

```python
"""Composition root of the mail connector (RA.10)."""

from __future__ import annotations

from types import MethodType

from packages.core.settings import LeaseReaperSettings, MailConnectorSettings
from services.mail_connector.consumer import MailSyncConsumer
from services.mail_connector.main import build_components
from tests.stubs.worker_resources import fake_worker_resources


def test_lease_reaper_is_disabled_by_default() -> None:
    assert LeaseReaperSettings().enabled is False


async def test_build_components_without_lease_reaper() -> None:
    settings = MailConnectorSettings(_env_file=None, lease_reaper=LeaseReaperSettings())
    start_fns = await build_components(fake_worker_resources(settings))

    assert len(start_fns) == 3  # consumer, renewal loop, queue monitor
    start = start_fns[0]
    assert isinstance(start, MethodType)
    consumer = start.__self__
    assert isinstance(consumer, MailSyncConsumer)
    assert consumer.queue_name == settings.broker.queue_mail_sync
    assert consumer.prefetch_count == settings.concurrency.mail_connector_concurrency


async def test_build_components_with_lease_reaper_enabled() -> None:
    settings = MailConnectorSettings(_env_file=None, lease_reaper=LeaseReaperSettings(enabled=True))
    start_fns = await build_components(fake_worker_resources(settings))
    assert len(start_fns) == 4
```

Run: `uv run pytest tests/unit/test_mail_sync_consumer.py tests/unit/test_mail_connector_background.py tests/unit/test_mail_connector_main.py tests/unit/test_sync_orchestrator.py`
Expected: collection errors for the three new modules, and pytest reports `Interrupted: 3 errors during collection`. Then run `uv run pytest tests/unit/test_sync_orchestrator.py`. Expected: the two new orchestrator tests fail (`TypeError: unexpected keyword argument 'full_resync'`, and a lock left `syncing`).

- [ ] **Step 2: Orchestrator: full re-sync and the resolver inside the `try`**

In `services/mail_connector/orchestrator.py`, change the signature of `sync_mailbox`:

```python
    async def sync_mailbox(
        self,
        mailbox: Mailbox,
        adapter: MailProviderAdapter | None = None,
        *,
        full_resync: bool = False,
    ) -> SyncOutcome:
```

Add this line to its docstring: `full_resync=True resets the provider cursor before syncing (R2.11).`

Delete these two lines, which sit between the coalescing `return` and `total_synced = 0`:

```python
        if adapter is None:
            adapter = self.adapter_resolver(mailbox)
```

Insert this at the top of the `try:` block, before `while True:`:

```python
            # Resolve inside the try so a resolver failure releases the lock.
            if adapter is None:
                adapter = self.adapter_resolver(mailbox)

            if full_resync:
                reset_at = datetime.now(UTC)
                logger.info("Operator-requested full re-sync for mailbox %s (R2.11)", mailbox.id)
                await self.checkpoint_store.save(
                    Checkpoint(
                        mailbox_id=mailbox.id,
                        organization_id=mailbox.organization_id,
                        history_id=None,
                        delta_link=None,
                        sync_state="syncing",  # keep the in-flight lock held
                        last_sync_at=reset_at,
                        last_full_sync_at=reset_at,
                    )
                )
```

`datetime`, `UTC` and `Checkpoint` are already imported (`orchestrator.py:13, 28`). The existing `except Exception: release_lock("error"); raise` now covers resolver failures.

- [ ] **Step 3: The consumer**

Create `services/mail_connector/consumer.py`:

```python
"""Mail sync request consumer (RA.10; design.md §5.1, §7.1; R2.1, R2.11, R3.3, R3.5, R23.6).

Consumes `sync_mailbox` envelopes from `mail.sync.requested` (published by the webhook
receivers and POST /v1/mailboxes/{id}/resync) and runs SyncOrchestrator.sync_mailbox.
Unresolvable or cross-tenant envelopes raise FatalError (dead-letter, never retried).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.adapters.exceptions import NotFound, Permanent, Transient
from packages.adapters.protocol import MailProviderAdapter
from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.mailbox import MailboxStore
from packages.domain.entities import Mailbox
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.mail_connector.orchestrator import SyncOrchestrator, SyncOutcome

logger = logging.getLogger(__name__)

SYNC_JOB_TYPE = "sync_mailbox"
NON_SYNCING_STATUSES = frozenset({"needs_reauth", "paused"})


def _parse_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


class MailSyncConsumer(BaseConsumer):
    """Consumer mapping `sync_mailbox` envelopes onto SyncOrchestrator.sync_mailbox."""

    def __init__(
        self,
        orchestrator: SyncOrchestrator,
        mailbox_store: MailboxStore,
        adapter_resolver: Callable[[Mailbox], MailProviderAdapter] | None = None,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        settings = broker_settings or BrokerSettings()
        super().__init__(
            queue_name=settings.queue_mail_sync,
            broker_settings=settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=None,  # sync envelopes have no processing_job row
            metrics=metrics,
        )
        self.orchestrator = orchestrator
        self.mailbox_store = mailbox_store
        self.adapter_resolver = adapter_resolver or orchestrator.adapter_resolver

    async def resolve_mailbox(self, envelope: JobEnvelope) -> Mailbox:
        """Resolve and tenant-check the target mailbox, or raise FatalError."""
        org_id = _parse_uuid(envelope.organization_id)
        if org_id is None:
            raise FatalError(
                f"Missing or invalid organization_id '{envelope.organization_id}' (R23.6)"
            )
        mailbox_id = _parse_uuid(envelope.mailbox_id)
        if mailbox_id is None:
            raise FatalError(f"Unresolvable mailbox_id '{envelope.mailbox_id}'")

        mailbox = await self.mailbox_store.get(mailbox_id)  # DB errors propagate -> retry
        if mailbox is None:
            raise FatalError(f"Mailbox {mailbox_id} not found")
        if _parse_uuid(str(mailbox.organization_id)) != org_id:
            raise FatalError(
                f"Mailbox {mailbox_id} does not belong to organization {org_id} (R23.6)"
            )
        return mailbox

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        if envelope.job_type != SYNC_JOB_TYPE:
            raise FatalError(
                f"Unsupported job_type '{envelope.job_type}' on queue '{self.queue_name}'"
            )

        mailbox = await self.resolve_mailbox(envelope)

        if mailbox.status in NON_SYNCING_STATUSES:
            logger.warning(
                "Skipping sync for mailbox %s in status '%s' (R1.5, R2.10)",
                mailbox.id,
                mailbox.status,
            )
            return

        try:
            adapter = self.adapter_resolver(mailbox)
        except (NotFound, Permanent) as err:
            raise FatalError(f"No usable provider adapter for mailbox {mailbox.id}: {err}") from err

        full_resync = bool(envelope.payload.get("full_resync", False))
        if envelope.payload.get("since") or envelope.payload.get("until"):
            logger.warning(
                "Mailbox %s resync time window (since=%s, until=%s) is not supported by the "
                "adapter protocol; running checkpoint-based sync",
                mailbox.id,
                envelope.payload.get("since"),
                envelope.payload.get("until"),
            )

        try:
            outcome = await self.orchestrator.sync_mailbox(
                mailbox, adapter=adapter, full_resync=full_resync
            )
        except Transient as err:
            raise TransientError(
                f"Transient provider failure for mailbox {mailbox.id}: {err}"
            ) from err
        except (NotFound, Permanent) as err:
            raise FatalError(f"Permanent provider failure for mailbox {mailbox.id}: {err}") from err

        self._handle_outcome(mailbox, outcome, full_resync=full_resync)

    def _handle_outcome(self, mailbox: Mailbox, outcome: SyncOutcome, *, full_resync: bool) -> None:
        if outcome.status == "rate_limited":
            raise TransientError(
                f"Provider rate limited mailbox {mailbox.id} (retry_after={outcome.retry_after_s}s)"
            )
        if outcome.coalesced and full_resync:
            # A pending follow-up does not carry the full_resync flag; retry the request later.
            raise TransientError(f"Mailbox {mailbox.id} busy; full re-sync deferred")
        if outcome.status == "needs_reauth":
            logger.error("Mailbox %s marked needs_reauth during sync; not retrying", mailbox.id)
            return
        logger.info(
            "Mailbox %s sync finished: status=%s messages=%d coalesced=%s",
            mailbox.id,
            outcome.status,
            outcome.messages_synced,
            outcome.coalesced,
        )
```

- [ ] **Step 4: Background loop**

Create `services/mail_connector/background.py`:

```python
"""Owned background loop with bounded, cancellation-safe shutdown (RA.10, R20.8)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class BackgroundLoop:
    """Runs `run_loop(stop_event)` in one task; stop() signals, waits, then cancels."""

    def __init__(
        self,
        name: str,
        run_loop: Callable[[asyncio.Event], Awaitable[None]],
        stop_timeout_s: float = 15.0,
    ) -> None:
        self.name = name
        self._run_loop = run_loop
        self._stop_timeout_s = stop_timeout_s
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()

        async def _runner() -> None:
            await self._run_loop(self._stop_event)

        self._task = asyncio.create_task(_runner(), name=self.name)
        logger.info("Background loop '%s' started", self.name)

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        try:
            # wait_for cancels the task itself if the timeout elapses.
            await asyncio.wait_for(task, timeout=self._stop_timeout_s)
        except TimeoutError:
            logger.warning(
                "Background loop '%s' did not stop in %.1fs; cancelled",
                self.name,
                self._stop_timeout_s,
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            logger.exception("Background loop '%s' exited with an error", self.name)
        finally:
            self._task = None
            logger.info("Background loop '%s' stopped", self.name)
```

- [ ] **Step 5: Settings default and entrypoint**

In `packages/core/settings.py` `LeaseReaperSettings`, replace the `enabled` field with:

```python
    enabled: bool = Field(
        default=False,
        description=(
            "Enable the background lease reaper in mail-connector. Off by default: leases are "
            "not yet cleared on completion and processing_job.queue_name is not yet written, so "
            "enabling it would re-drive parked jobs (see RA.10 notes)"
        ),
    )
```

In `docs/configuration.md` §2.17, change the `LEASE_REAPER__ENABLED` row default from `` `true` `` to `` `false` ``, and append a period plus this to its description: " Off by default until leases are cleared on completion and `queue_name` is recorded (W3)."

In `.env.example`, set `LEASE_REAPER__ENABLED=false`.

Create `services/mail_connector/main.py`:

```python
"""Mail Connector Service entrypoint (RA.10; design.md §3.2, §5.1).

Hosts the MailSyncConsumer on 'mail.sync.requested', the subscription renewal loop (R2.10),
the single QueueMonitor for the stack (R7.5, R21.4), and the LeaseReaper (R19.8) only when
LEASE_REAPER__ENABLED=true. Keep this service at one replica: renewal selection has no
locking and queue-depth gauges would duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from packages.broker.lease_reaper import LeaseReaper
from packages.broker.queue_monitor import QueueMonitor
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import MailConnectorSettings
from packages.core.storage import get_storage_client
from packages.db.checkpoint import PostgresCheckpointStore
from packages.db.job import PostgresJobStore
from packages.db.mailbox import PostgresMailboxStore
from packages.db.subscription import PostgresSubscriptionStore
from services.mail_connector.background import BackgroundLoop
from services.mail_connector.consumer import MailSyncConsumer
from services.mail_connector.orchestrator import SyncOrchestrator
from services.mail_connector.renewal import SubscriptionRenewalJob

logger = logging.getLogger("mail_connector.service")

SERVICE_NAME = "mail_connector"
DEFAULT_PORT = 8001
QUEUE_MONITOR_INTERVAL_S = 15.0


async def build_components(res: WorkerResources) -> list[StartFn]:
    settings = res.settings
    mailbox_store = PostgresMailboxStore(res.db_pool)
    job_store = PostgresJobStore(res.db_pool)

    orchestrator = SyncOrchestrator(
        checkpoint_store=PostgresCheckpointStore(res.db_pool),
        storage_client=get_storage_client(settings.object_storage),
        publisher=res.publisher,
        mailbox_store=mailbox_store,
        job_store=job_store,
        metrics=res.metrics,
        settings=settings,
    )
    consumer = MailSyncConsumer(
        orchestrator=orchestrator,
        mailbox_store=mailbox_store,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.mail_connector_concurrency,
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
        metrics=res.metrics,
    )

    renewal_job = SubscriptionRenewalJob(
        subscription_store=PostgresSubscriptionStore(res.db_pool),
        mailbox_store=mailbox_store,
        metrics=res.metrics,
        settings=settings,
    )
    renewal_loop = BackgroundLoop(
        "subscription-renewal",
        lambda stop_event: renewal_job.run_loop(stop_event=stop_event),
        stop_timeout_s=settings.telemetry.drain_timeout_s,
    )
    res.shutdown.register_drain_callback(renewal_loop.stop)

    queue_monitor = QueueMonitor(  # registers its own stop() as a drain callback
        connection=res.connection,
        broker_settings=settings.broker,
        metrics=res.metrics,
        shutdown_coordinator=res.shutdown,
    )

    async def start_queue_monitor() -> None:
        await queue_monitor.start(interval_seconds=QUEUE_MONITOR_INTERVAL_S)

    start_fns: list[StartFn] = [consumer.start, renewal_loop.start, start_queue_monitor]

    if settings.lease_reaper.enabled:
        reaper = LeaseReaper(
            job_store=job_store,
            settings=settings.lease_reaper,
            publisher=res.publisher,
            metrics=res.metrics,
        )
        res.shutdown.register_drain_callback(reaper.stop)

        async def start_reaper() -> None:
            reaper.start()

        start_fns.append(start_reaper)
    else:
        logger.warning("Lease reaper disabled (LEASE_REAPER__ENABLED=false)")

    return start_fns


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=MailConnectorSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_mail_sync_consumer.py tests/unit/test_mail_connector_background.py tests/unit/test_mail_connector_main.py tests/unit/test_sync_orchestrator.py tests/unit/test_dependency_rules.py tests/unit/test_production_imports.py`
Expected: all pass.

Three assertions depend on `FakeProviderAdapter` details:
- the `retry_after` in `inject_rate_limit`;
- the cursor semantics, where the second sync yields 0 messages;
- the `sync_state == "error"` left after a transient failure.

If one of them fails, read `packages/adapters/fake.py` and correct the **test expectation** to match the adapter's documented behaviour. Do not change `MailSyncConsumer` to fit the fake.

Run: `uv run pytest tests/unit`
Expected: all pass. No existing test asserts `LeaseReaperSettings().enabled is True` (checked with grep).

- [ ] **Step 7: Verify and commit**

Run: `uv run ruff check . && uv run mypy packages services tests/unit/test_mail_sync_consumer.py tests/unit/test_mail_connector_background.py tests/unit/test_mail_connector_main.py`
Expected: clean.

```bash
git add packages/core/settings.py docs/configuration.md .env.example \
  services/mail_connector/orchestrator.py services/mail_connector/consumer.py \
  services/mail_connector/background.py services/mail_connector/main.py \
  tests/unit/test_mail_sync_consumer.py tests/unit/test_mail_connector_background.py \
  tests/unit/test_mail_connector_main.py tests/unit/test_sync_orchestrator.py
git commit -m "feat(mail-connector): sync consumer, renewal loop and queue monitor in a real entrypoint [task RA.10] [R2.1, R2.10, R2.11, R7.5, R19.8, R23.6]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 12: Production image from the lockfile with every runtime asset (RA.11)

Today's `Dockerfile` copies only `packages/` and `services/`, and it runs `uv pip install --system -e .`. That install ignores `uv.lock` and has no dev dependencies. `.dockerignore` excludes `artifacts`, which removes the tracked ML model. Every config, prompt and schema path is relative to the working directory, and migrations resolve via `migrator.py` `__file__.parents[2]`, so the image needs the source tree at `/app`. This host has **no buildx plugin**: compose uses the classic builder, so `RUN --mount` cannot be used. PyYAML is imported directly by four production modules but reaches the image only transitively, through `uvicorn[standard]`.

**Files:**
- Modify (rewrite): `Dockerfile`
- Modify (rewrite): `.dockerignore`
- Modify: `pyproject.toml`, `uv.lock` (declare `pyyaml`)
- Create: `tests/unit/test_runtime_image_contract.py`
- Create: `scripts/image_smoke.py`
- Modify: `Makefile` (new `image-smoke` target)

**Interfaces:**
- Consumes: `TriageSettings.templates_path` (Task 10); entrypoints from Tasks 9–11.
- Produces: an image whose `/app` contains `packages/`, `services/`, `config/`, `prompts/`, `schemas/`, `migrations/` and `artifacts/models/`, with dependencies installed from `uv.lock` into `/app/.venv` and no dev group.

- [ ] **Step 1: Write the failing contract test**

Create `tests/unit/test_runtime_image_contract.py`:

```python
"""Runtime image contract (RA.11, R20.1, R24.1).

The Docker image must ship every file production code loads at runtime, install from
uv.lock without dev dependencies, and never bake the host .env into /app. CI has no
docker build step, so this test parses the Dockerfile and .dockerignore instead.
"""

from __future__ import annotations

import fnmatch
import shlex
from pathlib import Path

import pytest
import yaml

from packages.core.settings import AgentProfileSettings, CategoryRoutingSettings, TriageSettings
from packages.db.migrator import DEFAULT_MIGRATIONS_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
FILE_BODY_SUFFIXES = (".txt", ".j2", ".md")


def _copied_sources() -> set[str]:
    sources: set[str] = set()
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = shlex.split(line)[1:]
        if any(p.startswith("--from") for p in parts):
            continue
        args = [p for p in parts if not p.startswith("--")]
        sources.update(src.rstrip("/").removeprefix("./") for src in args[:-1])
    return sources


def _ignore_patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [ln.strip().strip("/") for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _is_dockerignored(rel_path: str) -> bool:
    parts = rel_path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in _ignore_patterns():
        bare = pattern.removeprefix("**/")
        for prefix in prefixes:
            if fnmatch.fnmatch(prefix, pattern) or fnmatch.fnmatch(prefix.split("/")[-1], bare):
                return True
    return False


def _runtime_assets() -> list[str]:
    triage, routing, profiles = TriageSettings(), CategoryRoutingSettings(), AgentProfileSettings()
    assets = [
        triage.rules_path,
        triage.ml_model_path,
        triage.templates_path,
        routing.categories_config_path,
        profiles.config_path,
        profiles.prompts_dir,
        profiles.schemas_dir,
        DEFAULT_MIGRATIONS_DIR.relative_to(REPO_ROOT).as_posix(),
    ]
    profile_cfg = yaml.safe_load((REPO_ROOT / profiles.config_path).read_text(encoding="utf-8"))
    for profile in profile_cfg["profiles"].values():
        assets += [profile["prompt_template"], profile["output_schema"]]
    tmpl_cfg = yaml.safe_load((REPO_ROOT / triage.templates_path).read_text(encoding="utf-8"))
    assets += [
        t["body"]
        for t in tmpl_cfg["templates"]
        if str(t.get("body", "")).endswith(FILE_BODY_SUFFIXES)
    ]
    return sorted(set(assets))


@pytest.mark.parametrize("asset", _runtime_assets())
def test_runtime_asset_exists_in_repo(asset: str) -> None:
    assert (REPO_ROOT / asset).exists(), f"{asset} is loaded at runtime but missing from the repo"


@pytest.mark.parametrize("asset", _runtime_assets())
def test_runtime_asset_is_copied_into_image(asset: str) -> None:
    sources = _copied_sources()
    assert any(asset == s or asset.startswith(f"{s}/") for s in sources), (
        f"{asset} is loaded at runtime but no Dockerfile COPY covers it; sources: {sorted(sources)}"
    )


@pytest.mark.parametrize("asset", _runtime_assets())
def test_runtime_asset_is_not_dockerignored(asset: str) -> None:
    assert not _is_dockerignored(asset), f"{asset} is excluded from the build context"


def test_env_file_is_never_sent_to_the_image() -> None:
    assert _is_dockerignored(".env")
    assert "." not in _copied_sources(), "COPY . would bake the host .env into /app"


def test_image_installs_locked_runtime_dependencies_only() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv sync --locked --no-dev" in text
    assert "uv pip install" not in text, "must install from uv.lock, not re-resolve"
    assert "--no-editable" not in text, "migrator resolves migrations/ via __file__ under /app"
    assert "astral-sh/uv:latest" not in text, "pin the uv image tag"
    assert "--mount=" not in text, "compose uses the classic builder on this host (no buildx)"
    assert 'PATH="/app/.venv/bin:$PATH"' in text


def test_pyyaml_is_a_declared_runtime_dependency() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert '"pyyaml' in text, "production imports yaml directly; declare it, not rely on uvicorn"
```

Run: `uv run pytest tests/unit/test_runtime_image_contract.py`
Expected: many failures. The asset copy tests fail for `config/`, `prompts/`, `schemas/`, `migrations/` and `artifacts/models/`. The artifact is dockerignored, `.env` is not ignored, the uv install is unlocked, and pyyaml is undeclared.

- [ ] **Step 2: Declare PyYAML**

Run: `uv add "pyyaml>=6.0"`
Expected: `pyproject.toml` gains `"pyyaml>=6.0",` in `[project].dependencies`, and `uv.lock` marks pyyaml as a direct dependency. The resolved version does not change, because the package was already locked transitively.

Run: `uv lock --check`
Expected: `Resolved ... packages` with exit 0.

- [ ] **Step 3: Rewrite the Dockerfile**

Replace `Dockerfile` with:

```dockerfile
# Runtime image for every Python service (api + workers), built by docker compose.
# This host has no buildx plugin, so compose uses the classic builder
# (image label com.docker.compose.image.builder=classic): do NOT use RUN --mount.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

# curl is required by every healthcheck in docker-compose.yml
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer 1: third-party dependencies exactly as pinned in uv.lock, no dev group.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Layer 2: application source plus every file production code loads at runtime.
# Config/prompt/schema/model paths resolve against CWD (/app); migrations resolve via
# packages/db/migrator.py __file__.parents[2] (/app), so keep the editable install.
COPY README.md ./
COPY packages/ packages/
COPY services/ services/
COPY config/ config/
COPY prompts/ prompts/
COPY schemas/ schemas/
COPY migrations/ migrations/
COPY artifacts/models/ artifacts/models/

# Layer 3: install the project itself (editable, into /app/.venv).
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    SERVICE_NAME=service

EXPOSE 8000

# The frontend, ai-worker and dispatch-worker still run this stub until they are built
# (Phase 4.11+ / Phase 6); every real service overrides the command in docker-compose.yml.
CMD ["python", "services/placeholder.py"]
```

Never put a trailing `# comment` on a `COPY` line; Docker would treat it as a source path.

- [ ] **Step 4: Rewrite .dockerignore**

Replace `.dockerignore` with:

```
# VCS, local envs, caches
.git
.venv
**/__pycache__
**/*.py[cod]
.pytest_cache
.ruff_cache
.mypy_cache
.coverage
.DS_Store

# Secrets / host-only settings (AppSettings reads .env from CWD)
.env
.env.*

# Not needed at runtime (artifacts/models IS needed)
artifacts/superpowers
evaluation
docs
tests
scripts
e2e_demo
files.zip
.agent
.agents
.superpowers
.runtime-feature-audit*
frontend
```

- [ ] **Step 5: Run the contract test**

Run: `uv run pytest tests/unit/test_runtime_image_contract.py`
Expected: all pass.

- [ ] **Step 6: Real-image smoke check**

Create `scripts/image_smoke.py`:

```python
"""Runtime-image smoke check, piped into the built image by `make image-smoke` (RA.11).

Runs inside the image (not copied into it): proves pytest is absent, every entrypoint
imports, and every runtime asset the code loads resolves from WORKDIR /app.
"""

import importlib
import importlib.util
from pathlib import Path

assert importlib.util.find_spec("pytest") is None, "pytest must not be installed in the image"

for module in (
    "services.api.main",
    "services.email_worker.main",
    "services.knowledge_worker.main",
    "services.triage_worker.main",
    "services.mail_connector.main",
    "packages.broker.cli",
    "packages.db.cli",
    "packages.core.storage_cli",
):
    importlib.import_module(module)

from packages.broker.routing import load_categories_from_yaml  # noqa: E402
from packages.core.settings import AppSettings  # noqa: E402
from packages.db.migrator import discover_migrations  # noqa: E402
from packages.llm.profile import AgentProfileRegistry  # noqa: E402
from services.triage_worker.classifier import MLClassifier  # noqa: E402
from services.triage_worker.rules import load_rules_from_file  # noqa: E402
from services.triage_worker.template_loader import load_templates_from_file  # noqa: E402

settings = AppSettings()
assert discover_migrations(), "no SQL migrations found next to packages/db/migrator.py"
MLClassifier.load_from_artifact(settings.triage.ml_model_path)
load_rules_from_file(settings.triage.rules_path)
assert load_categories_from_yaml(settings.routing.categories_config_path), "no categories"
registry = AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path)
for profile in registry.profiles:
    registry.get_schema(profile)
    assert Path(profile.prompt_template).is_file(), profile.prompt_template
templates = load_templates_from_file(settings.triage.templates_path)
for template in templates.templates:
    if template.body.endswith((".txt", ".j2", ".md")):
        assert Path(template.body).is_file(), template.body
print("image smoke OK")
```

Create the `scripts/` directory first (it does not exist yet). Add this to the end of `Makefile`, append ` image-smoke` to the `.PHONY` line, and add this line after the `broker-migrate-retry` line in `help`:

```make
	@echo "  image-smoke - Build the runtime image and smoke-check its entrypoints and assets (RA.11)"
```

The target:

```make
IMAGE ?= rag-email-runtime:smoke

image-smoke:
	docker build -t $(IMAGE) .
	docker run --rm -i $(IMAGE) python - < scripts/image_smoke.py
```

Run: `make image-smoke`
Expected: the build succeeds, and the last line printed is `image smoke OK`. This builds a separately tagged image and does not touch the running stack.

- [ ] **Step 7: Verify and commit**

Run: `uv run pytest tests/unit && uv run ruff check . && uv run mypy packages services tests/unit/test_runtime_image_contract.py`
Expected: clean. `scripts/` is outside mypy's configured targets, and ruff still checks it.

```bash
git add Dockerfile .dockerignore pyproject.toml uv.lock Makefile scripts/image_smoke.py \
  tests/unit/test_runtime_image_contract.py
git commit -m "build(image): install from uv.lock without dev deps and ship every runtime asset [task RA.11] [R20.1, R24.1]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Compose wiring: commands, init job, readiness healthchecks, drain grace (RA.12)

Only `api` and `email-worker` override the placeholder `command` today, and only they receive database, broker and storage environment. Every other service would fall back to `localhost` inside its container. `make up` never rebuilds, so the running images are 89 of 92 commits old. Docker's default 10 s stop timeout is shorter than the 15 s drain.

**Files:**
- Modify (rewrite): `docker-compose.yml`
- Modify: `Makefile` (`up` target)
- Modify: `.env.example` (host ports that match compose)

**Interfaces:**
- Consumes: `python -m packages.broker.cli declare` (Task 9), `python -m services.<svc>.main` (Tasks 9–11), the image (Task 12), `make broker-migrate-retry` (Task 4).
- Produces: `init` runs migrations, then the bucket bootstrap, then topology, and every application service waits for it to complete successfully.

- [ ] **Step 1: Rewrite docker-compose.yml**

Replace the whole file with the following. The infrastructure tier is unchanged except that `x-*` anchors are added at the top.

```yaml
# Shared application settings (container-side addresses; host tools use .env instead).
x-app-env: &app-env
  DATABASE__HOST: postgres
  DATABASE__PORT: "5432"
  DATABASE__USER: ${DATABASE__USER:-postgres}
  DATABASE__PASSWORD: ${DATABASE__PASSWORD:-postgres}
  DATABASE__NAME: ${DATABASE__NAME:-rag_email}
  BROKER__HOST: rabbitmq
  BROKER__PORT: "5672"
  BROKER__USER: ${BROKER__USER:-guest}
  BROKER__PASSWORD: ${BROKER__PASSWORD:-guest}
  BROKER__VHOST: ${BROKER__VHOST:-/}
  OBJECT_STORAGE__ENDPOINT: minio:9000
  OBJECT_STORAGE__ACCESS_KEY: ${OBJECT_STORAGE__ACCESS_KEY:-minioadmin}
  OBJECT_STORAGE__SECRET_KEY: ${OBJECT_STORAGE__SECRET_KEY:-minioadmin}
  LLM__PROVIDER: ${LLM__PROVIDER:-fake}
  EMBEDDING__MOCK: ${EMBEDDING__MOCK:-true}

x-app-build: &app-build
  context: .
  dockerfile: Dockerfile

x-after-init: &after-init
  init:
    condition: service_completed_successfully

services:
  # --- Infrastructure Tier ---
  postgres:
    image: pgvector/pgvector:pg16
    container_name: rag-email-postgres
    restart: unless-stopped
    ports:
      - "${DATABASE__PORT:-5433}:5432"
    environment:
      POSTGRES_USER: ${DATABASE__USER:-postgres}
      POSTGRES_PASSWORD: ${DATABASE__PASSWORD:-postgres}
      POSTGRES_DB: ${DATABASE__NAME:-rag_email}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./deploy/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DATABASE__USER:-postgres} -d ${DATABASE__NAME:-rag_email}"]
      interval: 5s
      timeout: 5s
      retries: 5
      start_period: 10s

  rabbitmq:
    image: rabbitmq:3.13-management
    container_name: rag-email-rabbitmq
    restart: unless-stopped
    ports:
      - "${BROKER__PORT:-5672}:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: ${BROKER__USER:-guest}
      RABBITMQ_DEFAULT_PASS: ${BROKER__PASSWORD:-guest}
      RABBITMQ_DEFAULT_VHOST: ${BROKER__VHOST:-/}
    volumes:
      - rabbitmq_data:/var/lib/rabbitmq
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 5s
      timeout: 10s
      retries: 5
      start_period: 15s

  minio:
    image: quay.io/minio/minio:latest
    container_name: rag-email-minio
    restart: unless-stopped
    command: server /data --console-address ":9001"
    ports:
      - "${OBJECT_STORAGE__PORT:-9010}:9000"
      - "${OBJECT_STORAGE__CONSOLE_PORT:-9011}:9001"
    environment:
      MINIO_ROOT_USER: ${OBJECT_STORAGE__ACCESS_KEY:-minioadmin}
      MINIO_ROOT_PASSWORD: ${OBJECT_STORAGE__SECRET_KEY:-minioadmin}
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      timeout: 5s
      retries: 5
      start_period: 10s

  prometheus:
    image: prom/prometheus:v2.52.0
    container_name: rag-email-prometheus
    restart: unless-stopped
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
    ports:
      - "9090:9090"
    volumes:
      - ./monitoring/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:9090/-/healthy"]
      interval: 5s
      timeout: 5s
      retries: 5
      start_period: 10s

  grafana:
    image: grafana/grafana:11.0.0
    container_name: rag-email-grafana
    restart: unless-stopped
    ports:
      - "${GRAFANA__PORT:-3002}:3000"
    environment:
      GF_SECURITY_ADMIN_USER: admin
      GF_SECURITY_ADMIN_PASSWORD: admin
    volumes:
      - grafana_data:/var/lib/grafana
      - ./monitoring/grafana/provisioning/datasources:/etc/grafana/provisioning/datasources:ro
    depends_on:
      prometheus:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:3000/api/health"]
      interval: 5s
      timeout: 5s
      retries: 5
      start_period: 10s

  # --- One-shot bootstrap: migrations, buckets, broker topology (R5.5, R5.8, R3.2) ---
  init:
    build: *app-build
    restart: "no"
    command:
      - "sh"
      - "-c"
      - "python -m packages.db.cli up && python -m packages.core.storage_cli bootstrap && python -m packages.broker.cli declare"
    environment:
      <<: *app-env
      SERVICE_NAME: init
    depends_on:
      postgres:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy
      minio:
        condition: service_healthy

  # --- Application Tier ---
  api:
    build: *app-build
    container_name: rag-email-api
    restart: unless-stopped
    command: ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ports:
      - "8000:8000"
    environment:
      <<: *app-env
      SERVICE_NAME: api
      PORT: "8000"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 10s

  mail-connector:
    build: *app-build
    restart: unless-stopped
    command: ["python", "-m", "services.mail_connector.main"]
    stop_grace_period: 30s
    expose:
      - "8001"
    environment:
      <<: *app-env
      SERVICE_NAME: mail_connector
      PORT: "8001"
      LEASE_REAPER__ENABLED: "false"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 20s

  email-worker:
    build: *app-build
    restart: unless-stopped
    command: ["python", "-m", "services.email_worker.main"]
    stop_grace_period: 30s
    expose:
      - "8002"
    environment:
      <<: *app-env
      SERVICE_NAME: email_worker
      PORT: "8002"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 20s

  triage-worker:
    build: *app-build
    restart: unless-stopped
    command: ["python", "-m", "services.triage_worker.main"]
    stop_grace_period: 30s
    expose:
      - "8003"
    environment:
      <<: *app-env
      SERVICE_NAME: triage_worker
      PORT: "8003"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8003/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 30s

  knowledge-worker:
    build: *app-build
    restart: unless-stopped
    command: ["python", "-m", "services.knowledge_worker.main"]
    stop_grace_period: 30s
    expose:
      - "8005"
    environment:
      <<: *app-env
      SERVICE_NAME: knowledge_worker
      PORT: "8005"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8005/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 20s

  # --- Not built yet: still the Phase-0 health stub (R20.1 requires the services) ---
  ai-worker:
    build: *app-build
    restart: unless-stopped
    expose:
      - "8004"
    environment:
      SERVICE_NAME: ai_worker
      PORT: "8004"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8004/healthz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 5s

  dispatch-worker:
    build: *app-build
    restart: unless-stopped
    expose:
      - "8006"
    environment:
      SERVICE_NAME: dispatch_worker
      PORT: "8006"
    depends_on: *after-init
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8006/healthz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 5s

  frontend:
    build: *app-build
    container_name: rag-email-frontend
    restart: unless-stopped
    ports:
      - "3001:3001"
    environment:
      SERVICE_NAME: frontend
      PORT: "3001"
    depends_on:
      api:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3001/healthz"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 5s

volumes:
  postgres_data:
  rabbitmq_data:
  minio_data:
  prometheus_data:
  grafana_data:
```

- [ ] **Step 2: `make up` builds; `.env.example` matches compose host ports**

In `Makefile`, change the `up` recipe's command from `docker compose up -d;` to `docker compose up -d --build;`, and update its help line to read `up       - Build images from HEAD and boot the full stack (init runs migrations, buckets, topology)`.

In `.env.example`:
- Change `DATABASE__PORT=5432` to `DATABASE__PORT=5433`.
- Change `OBJECT_STORAGE__ENDPOINT=localhost:9000` to `OBJECT_STORAGE__ENDPOINT=localhost:9010`.
- Add this comment line above the `DATABASE__` group: `# Host-side values: docker-compose publishes Postgres on 5433 and MinIO on 9010 (containers use 5432 / minio:9000).`

- [ ] **Step 3: Validate the compose file statically**

Run: `docker compose config -q && docker compose config --services`
Expected: exit 0. The service list includes `init`, `api`, `mail-connector`, `email-worker`, `triage-worker` and `knowledge-worker`.

Run:

```bash
docker compose config --format json | python3 -c "
import json, sys
cfg = json.load(sys.stdin)['services']
real = {'mail-connector', 'email-worker', 'triage-worker', 'knowledge-worker'}
for name in ['api', *sorted(real)]:
    svc = cfg[name]
    assert svc['depends_on']['init']['condition'] == 'service_completed_successfully', name
    assert svc['environment']['DATABASE__HOST'] == 'postgres', name
    assert svc['environment']['BROKER__HOST'] == 'rabbitmq', name
    assert 'readyz' in ' '.join(svc['healthcheck']['test']), name
for name in real:
    assert 'placeholder' not in ' '.join(cfg[name]['command']), name
print('compose wiring OK')
"
```

Expected: `compose wiring OK`. The anchors expanded, every real service waits for `init`, reaches `postgres` and `rabbitmq` by service name, runs its own command, and is health-checked on `/readyz`.

- [ ] **Step 4: Migrate the dev broker and bring the stack up**

**Ask the user first.** These commands restart the running dev stack.

```bash
make broker-migrate-retry     # deletes the three empty old retry queues (refuses if non-empty)
make up
docker compose ps -a
```

Expected:
- `init` shows `Exited (0)`.
- api, mail-connector, email-worker, triage-worker and knowledge-worker show `(healthy)` within about 60 s.
- ai-worker, dispatch-worker and frontend show `(healthy)`; those are the stub.

If `init` exits non-zero, run `docker compose logs init`. A `RetryTopologyMigrationError` means Step 4's migrate command was skipped. Any other error names the failing stage.

- [ ] **Step 5: Verify consumers and topology on the live broker**

Run: `docker exec rag-email-rabbitmq rabbitmqctl list_queues name consumers | grep -E "^(mail.sync.requested|email.normalize|email.triage|knowledge.ingest)\s"`
Expected: each of the four queues shows `1` consumer.

Run: `docker exec rag-email-rabbitmq rabbitmqctl list_queues name arguments | grep email.retry`
Expected: each retry queue shows `{"x-dead-letter-exchange","retry.return"}`.

Run: `curl -fs localhost:8000/readyz && docker compose exec triage-worker curl -fs localhost:8003/readyz`
Expected: both JSON bodies report `"status":"ok"`. The triage worker lists `database` and `broker` checks. The API lists only `database`, because its publisher connects lazily and registers no broker readiness check.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml Makefile .env.example
git commit -m "build(compose): run every built service, bootstrap via init, readiness healthchecks, 30s drain grace [task RA.12] [R20.1, R20.7, R20.8, R3.2]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Live-stack smoke check and documentation (RA.13)

This task proves the RA gate end to end against the running stack. The fake provider cannot be reached in compose: `PROVIDERS__MOCK_PROVIDERS` is read by nothing, and the database forbids `provider='fake'`. The smoke check therefore injects mail at the normalize stage, exactly as the orchestrator would, after archiving raw MIME. The mail-connector is exercised through its fatal path, a cross-tenant sync request, which needs no provider credentials.

```
make smoke
  1 api /readyz 200; ≥1 consumer on mail.sync.requested, email.normalize, email.triage, knowledge.ingest
  2 billing MIME ─▶ MinIO raw-mime ─▶ job RECEIVED ─▶ email.process/email.normalize
        ─▶ email-worker (NORMALIZED) ─▶ email.triage ─▶ triage-worker rule "urgent-billing"
        ─▶ email.route key email.billing.<lane>  (probe queue on "email.#")  job QUEUED
  3 no-reply newsletter ─▶ … ─▶ rule "no-reply-sender" ─▶ early exit ─▶ job COMPLETED
  4 sync_mailbox{mailbox of org A, organization_id = org B} ─▶ mail-connector FatalError
        ─▶ dlx.email key mail.sync.requested (probe) with x-failure-reason "does not belong"
  cleanup: DELETE organization (cascades), delete raw MIME objects
```

**Files:**
- Create: `scripts/stack_smoke.py`
- Modify: `Makefile` (new `smoke` target)
- Modify: `README.md` (quickstart, tests badge, stale counts)
- Modify: `specs/tasks.md` (mark RA.1–RA.13 and record evidence)

**Interfaces:**
- Consumes: everything above, running.

- [ ] **Step 1: Write the smoke script**

Create `scripts/stack_smoke.py`:

```python
"""Live-stack smoke check for the RA gate (RA.13; R24.7 partial, R20.1).

Run on the host after `make up` (reads .env like every host tool):

    make smoke

Checks, stopping at the first failure:
  1. The API is ready and every hosted queue has at least one consumer.
  2. A billing email injected into email.normalize is normalized, classified by rule and
     routed to email.billing.<lane>; its job reaches QUEUED.
  3. A no-reply newsletter reaches COMPLETED (early exit before any AI stage).
  4. A sync request naming another tenant's mailbox is dead-lettered with its reason.
Creates one throwaway organization and deletes it at the end. The routed billing job stays
in email.billing.<lane> until an AI worker exists (task 4.11 onward).
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import httpx
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractQueue

from packages.broker.envelope import JobEnvelope
from packages.core.settings import AppSettings
from packages.core.storage import ObjectKeyBuilder, StorageProtocol, get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.db.job import PostgresJobStore
from packages.domain.entities import Job
from packages.domain.state_machine import JobState

HOSTED_QUEUES = ("mail.sync.requested", "email.normalize", "email.triage", "knowledge.ingest")
TIMEOUT_S = 45.0
API_READYZ = "http://localhost:8000/readyz"


class SmokeFailure(RuntimeError):  # noqa: N818
    """A gate check failed; the message says which and why."""


def build_mime(sender: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "support@smoke.example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid(domain="smoke.example.com")
    msg.set_content(body)  # text/plain only: HTML offload targets an unprovisioned bucket (W4)
    return msg.as_bytes()


async def check_services(settings: AppSettings) -> None:
    async with httpx.AsyncClient(timeout=5.0) as http:
        ready = await http.get(API_READYZ)
        if ready.status_code != 200:
            raise SmokeFailure(f"api /readyz returned {ready.status_code}: {ready.text}")
        vhost = quote(settings.broker.vhost, safe="")
        resp = await http.get(
            f"http://{settings.broker.host}:15672/api/queues/{vhost}",
            auth=(settings.broker.user, settings.broker.password),
            params={"columns": "name,consumers"},
        )
        resp.raise_for_status()
        consumers = {q["name"]: q["consumers"] for q in resp.json()}
    missing = [q for q in HOSTED_QUEUES if consumers.get(q, 0) < 1]
    if missing:
        raise SmokeFailure(f"queues without a consumer: {missing}")
    print("ok   api ready; consumers on " + ", ".join(HOSTED_QUEUES))


async def seed_tenant(pool: asyncpg.Pool[Any]) -> tuple[UUID, UUID]:
    org_id, mailbox_id = uuid4(), uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"smoke-{org_id.hex[:8]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, status) "
            "VALUES ($1, $2, 'gmail', $3, 'active')",
            mailbox_id,
            org_id,
            f"smoke-{mailbox_id.hex[:8]}@smoke.example.com",
        )
    return org_id, mailbox_id


async def inject_email(
    settings: AppSettings,
    pool: asyncpg.Pool[Any],
    storage: StorageProtocol,
    channel: AbstractChannel,
    org_id: UUID,
    mailbox_id: UUID,
    mime: bytes,
    raw_keys: list[str],
) -> UUID:
    """Archive raw MIME, create the RECEIVED job and publish the normalize envelope,
    exactly as SyncOrchestrator does for a fetched message."""
    provider_message_id = f"smoke-{uuid4().hex}"
    bucket = settings.object_storage.bucket_raw_mime
    key = ObjectKeyBuilder.raw_mime(org_id, mailbox_id, provider_message_id)
    await storage.put_bytes(bucket=bucket, key=key, data=mime, content_type="message/rfc822")
    raw_keys.append(key)

    job_id = uuid4()
    idem = f"smoke:{org_id}:{provider_message_id}"
    await PostgresJobStore(pool).create_job(
        Job(
            id=job_id,
            organization_id=org_id,
            job_type="email_pipeline",
            state=JobState.RECEIVED.value,
            idempotency_key=idem,
        )
    )
    envelope = JobEnvelope(
        job_id=str(job_id),
        idempotency_key=f"{idem}:normalize",
        organization_id=str(org_id),
        mailbox_id=str(mailbox_id),
        message_id=provider_message_id,
        job_type="normalize_email",
        payload={
            "job_id": str(job_id),
            "raw_object_key": key,
            "raw_bucket": bucket,
            "provider": "gmail",
            "provider_message_id": provider_message_id,
        },
    )
    exchange = await channel.get_exchange(settings.broker.exchange_email_process)
    await exchange.publish(envelope.to_message(), routing_key=settings.broker.queue_normalize)
    return job_id


async def wait_for_state(
    pool: asyncpg.Pool[Any], org_id: UUID, job_id: UUID, expected: set[str]
) -> str:
    deadline = time.monotonic() + TIMEOUT_S
    state: str | None = None
    while time.monotonic() < deadline:
        state = await pool.fetchval(
            "SELECT state FROM processing_job WHERE id=$1 AND organization_id=$2", job_id, org_id
        )
        if state in expected:
            return str(state)
        if state in {"FAILED", "DEAD_LETTER"}:
            break
        await asyncio.sleep(0.5)
    events = await pool.fetch(
        "SELECT event_type, state_from, state_to, payload FROM processing_event "
        "WHERE job_id=$1 ORDER BY created_at",
        job_id,
    )
    raise SmokeFailure(
        f"job {job_id} ended in {state!r}, expected {sorted(expected)}; "
        f"events: {[dict(e) for e in events]}"
    )


async def wait_for_job_message(queue: AbstractQueue, job_id: str) -> AbstractIncomingMessage:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        msg = await queue.get(no_ack=True, fail=False)
        if msg is None:
            await asyncio.sleep(0.2)
            continue
        if JobEnvelope.from_message(msg).job_id == job_id:
            return msg
    raise SmokeFailure(f"no message for job {job_id} on probe {queue.name} within {TIMEOUT_S}s")


async def run() -> None:
    settings = AppSettings()
    await check_services(settings)

    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    storage = get_storage_client(settings.object_storage)
    org_id: UUID | None = None
    raw_keys: list[str] = []
    try:
        channel = await connection.channel(on_return_raises=True)
        org_id, mailbox_id = await seed_tenant(pool)

        # 2. Billing email -> email.billing.<lane>, job QUEUED
        route_probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
        await route_probe.bind(settings.broker.exchange_email_route, routing_key="email.#")
        billing_job = await inject_email(
            settings,
            pool,
            storage,
            channel,
            org_id,
            mailbox_id,
            build_mime(
                "client@enterprise.example.com",
                "Urgent: Overdue payment failure on account",
                "Your account balance is past due with repeated payment failure. Please advise.",
            ),
            raw_keys,
        )
        routed = await wait_for_job_message(route_probe, str(billing_job))
        if not (routed.routing_key or "").startswith("email.billing."):
            raise SmokeFailure(f"billing email routed to {routed.routing_key!r}")
        await wait_for_state(pool, org_id, billing_job, {JobState.QUEUED.value})
        print(f"ok   billing email -> {routed.routing_key}, job QUEUED")

        # 3. No-reply newsletter -> COMPLETED (early exit)
        newsletter_job = await inject_email(
            settings,
            pool,
            storage,
            channel,
            org_id,
            mailbox_id,
            build_mime(
                "no-reply@news.example.com",
                "Your weekly product digest",
                "Here are this week's product updates. You are receiving this newsletter.",
            ),
            raw_keys,
        )
        await wait_for_state(pool, org_id, newsletter_job, {JobState.COMPLETED.value})
        print("ok   no-reply newsletter -> COMPLETED (early exit)")

        # 4. Cross-tenant sync request -> dead-lettered with its reason
        dlq_probe = await channel.declare_queue("", exclusive=True, auto_delete=True)
        await dlq_probe.bind(
            settings.broker.exchange_dlx, routing_key=settings.broker.queue_mail_sync
        )
        sync_env = JobEnvelope(
            idempotency_key=f"smoke-sync-{uuid4()}",
            job_type="sync_mailbox",
            organization_id=str(uuid4()),
            mailbox_id=str(mailbox_id),
            payload={"provider": "gmail", "manual": True},
        )
        ingest = await channel.get_exchange(settings.broker.exchange_mail_ingest)
        await ingest.publish(sync_env.to_message(), routing_key=settings.broker.queue_mail_sync)
        dead = await wait_for_job_message(dlq_probe, sync_env.job_id)
        reason = str((dead.headers or {}).get("x-failure-reason", ""))
        if "does not belong" not in reason:
            raise SmokeFailure(f"sync request dead-lettered with unexpected reason: {reason!r}")
        print(f"ok   cross-tenant sync request -> email.dead_letter ({reason})")
    finally:
        if org_id is not None:
            await pool.execute("DELETE FROM organization WHERE id=$1", org_id)
        for key in raw_keys:
            try:
                await storage.delete_object(settings.object_storage.bucket_raw_mime, key)
            except Exception as err:
                print(f"warn could not delete raw object {key}: {err}", file=sys.stderr)
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Add this to the end of `Makefile`, append ` smoke` to the `.PHONY` line, and add ` @echo "  smoke    - End-to-end check of the running stack (RA gate)"` after the `image-smoke` help line:

```make
smoke:
	$(UV) run python scripts/stack_smoke.py
```

- [ ] **Step 2: Run the smoke check against the live stack**

Run: `make smoke`
Expected:

```
ok   api ready; consumers on mail.sync.requested, email.normalize, email.triage, knowledge.ingest
ok   billing email -> email.billing.priority, job QUEUED
ok   no-reply newsletter -> COMPLETED (early exit)
ok   cross-tenant sync request -> email.dead_letter (FatalError: Mailbox <id> does not belong to organization <org-id> (R23.6))
SMOKE OK
```

The billing lane may print as `.normal` or `.priority`, depending on the rule's priority.

**If a check fails**, the message includes the job's `processing_event` trail. That is a real runtime defect, so fix its cause through the normal workflow; do not edit the smoke expectations to pass. For example, email-worker logs showing `NORMALIZED` but no triage event would mean the triage publish or consumer wiring is broken. Record any defect that is out of scope as a new task in `specs/tasks.md` (CLAUDE.md §7) and report it.

- [ ] **Step 3: Documentation**

In `README.md`:
- Change the tests badge from `Tests-395%20Passing` to `Tests-pytest` (static; counts go stale).
- Replace Quickstart subsections "2. Boot Infrastructure Stack" and "3. Run Migrations & Seed Reference Data" with:

````markdown
### 2. Configure and Boot the Stack
```bash
cp .env.example .env    # host-side ports match docker-compose (Postgres 5433, MinIO 9010)
make up                 # builds images from HEAD; `init` applies migrations, buckets and topology
make smoke              # end-to-end check: ingestion -> normalize -> triage -> routing / DLQ
```
Upgrading an existing stack whose broker was created before 2026-09-26: run
`make broker-migrate-retry` once before `make up` (retry queues changed their dead-letter
exchange, and RabbitMQ queue arguments are immutable).

### 3. Seed Reference Data (optional)
```bash
make seed
```
````

- Replace `*Current test status: 395 passed, 0 failures, 0 regressions.*` with: `Integration tests run in an isolated vhost/database (`rag_email_test`) and never touch the running stack.`
- In "8. Configuration Reference", replace the `STORAGE__*` bullet with `` - `OBJECT_STORAGE__*`: MinIO/S3 endpoint, bucket names, and credentials. ``

- [ ] **Step 4: Close out the RA block in the task queue**

In `specs/tasks.md`, mark `RA.1`–`RA.13` as `[x]`, **only for tasks whose steps all completed in this plan**. Add this note directly under the `> **RA gate:**` line, filling in the numbers you measured:

```markdown
> **Gate evidence (<date>):** `make smoke` → SMOKE OK; `uv run pytest tests/unit` → <N> passed; `uv run pytest tests/integration` → <M> passed (vhost/database `rag_email_test`). **DoD #6 caveat:** repository-wide `make lint` was already red before this block (14 mypy errors in 6 pre-existing test files; 40 files fail `ruff format --check`). RA tasks added no new errors (targeted mypy/ruff on every touched file). Fixing that baseline is tracked separately.
```

Fill in `<date>`, `<N>` and `<M>` with the measured values; do not copy numbers from this plan.

- [ ] **Step 5: Final verification**

Run:
- `uv run pytest tests/unit`
- `uv run pytest tests/integration`
- `uv run ruff check .`
- `uv run mypy packages services`
- `make image-smoke`
- `make smoke`

Expected: everything passes, and `mypy packages services` is clean.

Run: `uv run mypy packages services tests evaluation 2>&1 | tail -1`
Expected: `Found 14 errors in 6 files`. That is the pre-existing baseline, with no new errors. If the count is higher, a touched test file introduced errors; fix them.

Run: `git status --short`
Expected: only the files listed in this task are modified, plus the pre-existing untracked docs.

- [ ] **Step 6: Commit**

```bash
git add scripts/stack_smoke.py Makefile README.md specs/tasks.md
git commit -m "test(smoke): live-stack gate check and runtime documentation [task RA.13] [R24.7, R20.1]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review record

- **Spec coverage.** Every item in the audit's W1 and W2 rows maps to a task.
  - W1: the pytest leak is T2; the image is T12; entrypoints are T9, T10 and T11; topology is T4 and T9; the init job and `make up --build` are T13.
  - W2: retry routing is T4; the unparseable acknowledgement is T5; replay is T8; drain order is T7; unroutable publishes and ack placement are T6.
  - W2 also listed jitter. It is deliberately excluded, with the reason stated under "Explicitly not in this plan".
  - Hosting the R5.10 check is T9. The lease reaper is hosted but disabled (D9). The queue monitor is T11. Renewal is T11.
- **Cross-task names.**
  - `exchange_for_queue` (T4) is used in T4 `retry.py` and T8.
  - `resolve_origin_exchange` (T4) is used in T4 and T5.
  - `scratch_vhost` (T3) is used in T4, T5, T6 and T9.
  - `stop_consuming`/`close` (T7) are relied on by the T9 runtime cleanup order.
  - `WorkerResources`, `StartFn` and `fake_worker_resources` (T9) are used in T10 and T11.
  - `TriageSettings.templates_path` (T10) is used in T12.
  - `python -m packages.broker.cli declare` (T9) is used in T13.
  - `make broker-migrate-retry` (T4) is used in T13 and T14.

## Dry-run verification (2026-09-26)

On 2026-09-26, 14 sequential agents applied every task, in order, to a throwaway copy of HEAD `1ecf7b1`. The copy was made with `git archive`, plus the host `.env` and a fresh `uv sync`. The real repository was never written. Each agent ran its task's commands and reported every deviation. 49 corrections were folded back into this plan, mostly ruff E501, B009, N818, I001 and format issues, plus mypy narrowing in two tests, stale line numbers and wrong red-run expectations. One real hazard was also fixed: the Task 3 red-run probe queue could leak into vhost `/`. 29 of the 32 files this plan creates are byte-identical to the copy that passed. The other 3 differ only in docstring requirement citations corrected afterwards.

| Check (on the throwaway copy, after all tasks) | Result |
|---|---|
| `uv run pytest tests/unit` | 1197 passed |
| `uv run pytest tests/integration` (isolated `rag_email_test`) | 140 passed |
| `uv run ruff check .` / `uv run mypy packages services` | clean / clean |
| `uv run mypy packages services tests evaluation` | 14 errors in 6 files: the pre-existing baseline, no new errors |
| `make image-smoke` | `image smoke OK` (pytest absent, all entrypoints import, every asset resolves) |
| `init` command chain run inside the built image | migrations up, buckets bootstrapped, `RabbitMQ topology successfully declared: 12 exchanges, 27 queues` |
| `docker compose config` + wiring assertions (Task 13 Step 3) | `compose wiring OK` (also with `docker-compose.scale.yml`) |

**Not verified by the dry run** (live-stack steps, deliberately skipped because they restart the running dev stack):
- Task 13 Steps 4–5: `make broker-migrate-retry`, `make up`, and the consumer and readiness checks.
- Task 14 Step 2: `make smoke`.

A read-only `check_services()` against today's stack correctly failed with `queues without a consumer`, which is the state this plan fixes.
