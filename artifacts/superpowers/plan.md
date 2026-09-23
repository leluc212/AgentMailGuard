# Implementation Plan: Category-Aware Routing (Phase 2, Task 2.10)

Implement category-aware routing to topic exchange `email.route` using routing key `email.<category>.<priority>`, separate `normal` and `priority` lanes with independent consumer scaling, dynamic declarative queue declaration from configuration at startup, and startup warnings for category queues without configured consumers, fulfilling requirements **R7.1, R7.2, R7.4, R7.6** and `specs/design.md §5.3, §7.1`.

## User Review Required

> [!IMPORTANT]
> **Queue Naming & Topic Exchange Bindings:**
> In accordance with `specs/requirements.md` R7.1 and `specs/design.md §7.1`, queues are declared as `email.<category>.<lane>` (e.g. `email.support.normal`, `email.support.priority`, `email.billing.normal`, etc.) and bound to the topic exchange `email.route` with routing key `email.<category>.<lane>`.
> Priorities from classification (`urgent`, `high`, `normal`, `low`) resolve to the two fundamental lanes (`priority` vs `normal`), with independent consumer concurrency and prefetch settings per lane (R7.2).

> [!NOTE]
> **Dynamic Startup Category Declarations (R7.4):**
> A new declarative YAML configuration `config/categories.yaml` allows adding custom categories without code changes. On startup, `setup_topology` dynamically inspects the taxonomy registry and declares corresponding `email.<category>.<lane>` queues in RabbitMQ.

> [!WARNING]
> **Unconsumed Queue Detection (R7.6):**
> Any declared category queue that does not match a configured consumer pattern (e.g. in `RoutingSettings.configured_consumers`) triggers a startup warning log naming the queue to prevent silent message accumulation.

---

## Proposed Changes

 Group files by component, ordering dependencies first.

### 1. Configuration & Domain Taxonomy

#### [NEW] [categories.yaml](file:///home/ple/Documents/antigravity/dazzling-bose/config/categories.yaml)
- Declarative YAML definition for categories, intents, default reply/retrieval flags, and priority defaults.
- Serves as the configuration source for adding new categories without code changes (R7.4).

#### [MODIFY] [taxonomy.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/domain/taxonomy.py)
- Add `load_categories_from_yaml(path: str | Path) -> list[CategoryDefinition]` to `TaxonomyRegistry`.
- Support dynamically registering categories from YAML on initialization or via function call.

#### [MODIFY] [settings.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/core/settings.py)
- Define `CategoryRoutingSettings`:
  - `categories_config_path: str = "config/categories.yaml"`
  - `priority_lanes: list[str] = ["normal", "priority"]`
  - `configured_consumers: list[str]` (defaulting to standard actionable queues, e.g. `email.support.normal`, `email.support.priority`, `email.billing.normal`, `email.billing.priority`, `email.sales.normal`, `email.sales.priority`, `email.general_inquiry.normal`, `email.general_inquiry.priority`).
- Enhance `WorkerConcurrencySettings` with independent consumer scaling for lanes (R7.2):
  - `ai_worker_normal_concurrency: int = 4`
  - `ai_worker_priority_concurrency: int = 8`
  - `ai_worker_normal_prefetch: int = 10`
  - `ai_worker_priority_prefetch: int = 5`
- Add `routing: CategoryRoutingSettings` to `AppSettings`.

---

### 2. Messaging & Routing Logic

#### [NEW] [routing.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/routing.py)
- Pure routing helpers:
  - `resolve_priority_lane(priority: str) -> str`: Maps urgency levels (`urgent`, `high`, `priority`) to `"priority"`, others (`normal`, `low`) to `"normal"`.
  - `format_routing_key(category: str, priority: str) -> str`: Constructs canonical `email.<category>.<lane>` routing key (R7.1).
  - `is_queue_consumed(queue_name: str, configured_consumers: list[str]) -> bool`: Evaluates exact matches and wildcard patterns (`fnmatch`).
  - `prepare_route_envelope(envelope: JobEnvelope, classification: Classification | dict[str, Any]) -> tuple[str, JobEnvelope]`: Prepares `generate_reply` envelope with classification snapshot (R7.3) and calculated routing key.

#### [MODIFY] [topology.py](file:///home/ple/Documents/antigravity/dazzling-bose/packages/broker/topology.py)
- Extend `setup_topology`:
  - Accept optional `routing_settings: CategoryRoutingSettings | None`.
  - If `categories_config_path` is present, load definitions into `TaxonomyRegistry`.
  - For each registered category and priority lane (`normal`, `priority`):
    - Declare durable queue `email.<category>.<lane>` (with quorum args if enabled).
    - Bind queue to topic exchange `exchange_email_route` (`email.route`) using routing key `email.<category>.<lane>`.
  - For each declared category queue:
    - Check if the queue is covered by `configured_consumers`.
    - If unconsumed, emit warning log: `logger.warning("Category queue '%s' has no configured consumer (R7.6). Messages may accumulate.", queue_name)`.
  - Include `category_queues: dict[str, AbstractQueue]` and `unconsumed_queues: list[str]` in `BrokerTopology`.

---

### 3. Triage Worker Service Consumer

#### [NEW] [consumer.py](file:///home/ple/Documents/antigravity/dazzling-bose/services/triage_worker/consumer.py)
- Implement `TriageConsumer(BaseConsumer)`:
  - Consumes from `queue_triage` (`email.triage`).
  - Runs cascading triage (`TriageCascade.classify`).
  - Evaluates early-exit gate (`EarlyExitGate.evaluate_and_persist`).
  - If `EARLY_EXIT`: acknowledges message, no downstream publish.
  - If `TEMPLATE_REPLY`: generates draft, acknowledges message, no downstream publish.
  - If `PROCEED_RAG` or `PROCEED_NO_RAG` (actionable AI generation):
    - Generates routing key `email.<category>.<priority>`.
    - Updates envelope to `job_type="generate_reply"` with classification snapshot (R7.3).
    - Publishes to `exchange_email_route` (`email.route`) topic exchange.
    - Acknowledges message from `email.triage` after side effects are committed.

---

### 4. Documentation & Environment Configuration

#### [MODIFY] [.env.example](file:///home/ple/Documents/antigravity/dazzling-bose/.env.example)
- Add new configuration keys for category routing and lane-specific concurrency.

#### [MODIFY] [configuration.md](file:///home/ple/Documents/antigravity/dazzling-bose/docs/configuration.md)
- Document `CategoryRoutingSettings`, `WorkerConcurrencySettings` lane scaling, and `config/categories.yaml`.

#### [MODIFY] [tasks.md](file:///home/ple/Documents/antigravity/dazzling-bose/specs/tasks.md)
- Mark Task 2.10 `[x]` upon complete verification.

---

## Verification Plan

### Automated Tests
1. **Unit Tests:** `tests/unit/test_category_routing.py`
   - Test priority lane resolution (`urgent`/`high` -> `priority`, `normal`/`low` -> `normal`).
   - Test routing key construction (`email.<category>.<priority>`).
   - Test YAML category loading and dynamic taxonomy registration (R7.4).
   - Test unconsumed queue detection logic (R7.6).
   - Test independent consumer scaling configuration for normal and priority lanes (R7.2).
   - Command: `.venv/bin/pytest tests/unit/test_category_routing.py -v`

2. **Integration Tests:** `tests/integration/test_category_routing_integration.py`
   - Run with live PostgreSQL and RabbitMQ containers.
   - Test dynamic category queue declaration at startup in `setup_topology`.
   - Test topic exchange message routing: publish with routing key `email.billing.priority` and assert message lands in `email.billing.priority` queue.
   - Test wildcard bindings and lane-specific routing.
   - Test startup warning log emission for an unconsumed category queue using `caplog` (R7.6).
   - Test end-to-end `TriageConsumer` processing from `email.triage` to `email.<category>.<priority>` queue.
   - Command: `.venv/bin/pytest tests/integration/test_category_routing_integration.py -v`

3. **Full Regression Suite & Linters:**
   - Command: `.venv/bin/pytest tests/unit tests/integration`
   - Command: `.venv/bin/ruff check .`
   - Command: `.venv/bin/mypy packages services tests`
