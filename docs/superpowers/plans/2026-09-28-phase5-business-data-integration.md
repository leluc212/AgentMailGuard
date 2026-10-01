# Phase 5 — Business Data Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An order-status email produces a draft that states the real order status from the business tables and cites the knowledge procedure. Live runs go through the Gemini API on the OpenAI-compatible endpoint, and every business lookup is planned in code, scoped to the sender's customer inside the tenant, bounded by a deadline, and recorded for replay.

**Architecture:** The ai-worker's `ContextBuilder` builds a pure `FetchPlan` from three inputs: the typed IDs in the subject and body, the routed profile's `context_policy`, and the intent. For a non-empty plan it makes one bounded call, `BusinessDataProvider.get_business_context(org_id, sender_email, plan)`. The provider resolves the sender to one customer first and then runs fixed, `organization_id`-scoped queries. The resulting `BusinessContext` renders as one `[BUSINESS DATA]` section, the last one in the prompt of the single generation call. A timeout or error degrades every planned fact to `UNAVAILABLE` and does not fail the job.

```
inbound email ─▶ email-worker (normalize) ─▶ triage-worker (category, intent) ─▶ ai-worker
                                                                                   │
  ContextBuilder.build_context ◀──────────────────────────────────────────────────┘
    ├─ ThreadContextAssembler ─────────────────────────────▶ thread summary + recent messages
    ├─ HybridRetriever (if retrieval_required) ────────────▶ knowledge chunks (procedure)
    └─ build_fetch_plan(subject, body, profile.context_policy, intent)      pure, no I/O
          │  typed IDs (ORD-/TICK-/INV-, "order 82915") always planned
          │  snapshot: thread_plus_rag_plus_business ⇒ orders+tickets │ INTENT_ENTITIES ⇒ orders
          ▼
       FetchPlan ── empty ─▶ None (no provider call; one business_fetch log line)
          │ non-empty
          ▼
       fetch_business_context(provider, timeout_ms=BUSINESS_DATA__TIMEOUT_MS)   span business.fetch
          │  asyncio.wait_for ─▶ PostgresBusinessDataProvider (read-only txn, statement_timeout)
          │     sender ─▶ customer: FOUND │ UNKNOWN_SENDER │ AMBIGUOUS_CUSTOMER
          │     lookups scoped to (organization_id, customer_id) ─▶ FOUND │ NOT_FOUND │ NOT_LOOKED_UP
          │  timeout / error ─▶ unavailable_context(plan): every fact UNAVAILABLE, degraded=true
          ▼
       BusinessContext ─▶ ContextPackage section 7 ─▶ prompts/*.v2.j2: {{ business_data.render() }}
          │                                              [BUSINESS DATA] source=business_db as_of=…
          │                                              customer_status: FOUND
          │                                              order ORD-82915: FOUND | status=dispatched | …
          ├─▶ CONTEXT_READY payload: business_plan, customer_status, business_fact_statuses,
          │                          business_data_degraded
          └─▶ business_lookups_total{entity,status}, business_lookup_latency_ms
                                                   │
                                                   ▼
                            SinglePassGenerator: ONE generation call ─▶ draft (states status, cites procedure)
```

**Tech Stack:** Python 3.12, asyncio, asyncpg on PostgreSQL 16 (+ pgvector), aio-pika / RabbitMQ, pydantic-settings, Jinja2 prompt templates, httpx (the OpenAI-compatible provider), OpenTelemetry, prometheus_client, pytest (+ pytest-asyncio auto mode), ruff, strict mypy, uv, Docker Compose.

**Spec:**
- `specs/tasks.md` Phase 5, tasks 5.0–5.6, and the Phase 5 gate paragraph.
- `specs/design.md` §5.4 "Business data (R13)" (fetch plan, typed IDs, snapshot, statuses, timeout, label and precedence) and §13.3 (compose forwarding of the LLM settings).
- `docs/adr/0008-business-data-fetch-plan.md`: why the plan is built in code, and the identity assumption.
- `specs/requirements.md` R13.1–R13.7; also R14.7, R20.6, R21.3, R21.6, R24.5, R5.9 and R16.1, cited by the tasks.
- Source of truth: `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md` (business data versus knowledge).

## Global Constraints

- One generation call per job: business data is fetched in code before the call. The model never calls a lookup tool (design §5.4, §5.7).
- Every tenant query carries `organization_id` (`organization_id = $1` first); order and ticket lookups also carry the resolved `customer_id = $2`.
- No test needs live credentials. The fake LLM provider and `EMBEDDING__MOCK=true` are used in CI, and the live checks (`make llm-smoke`, `make phase5-gate`) are owner-run and not part of CI (R24.5).
- `packages/domain` imports only the stdlib and `packages/core` (`tests/unit/test_dependency_rules.py`), which is why `packages/domain/business.py` holds the business value objects.
- Packages never import services.
- Schema changes go through migrations. Phase 5 needs none: `customer`, `product`, `order`, `order_item` and `ticket` exist in migration 0001.
- Every new config key goes into both `.env.example` and `docs/configuration.md`, and into the compose `x-app-env` block when the app containers read it.
- Security hardening (sender verification, auth) is out of scope (CLAUDE.md §6). The sender address is the customer's identity, as ADR-0008 records.
- Commit format (CLAUDE.md §2): `feat(business): fetch plan [task 5.4] [R13.3, R13.7]`, then a blank line, a body, a blank line, and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Gemini: `LLM__PROVIDER=openai`, `LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`, `LLM__FAST_MODEL=gemma-4-26b-a4b-it`, `LLM__STRONG_MODEL=gemma-4-31b-it`, `LLM__FALLBACK_MODEL=gemini-3.1-flash-lite`.
- `BUSINESS_DATA__TIMEOUT_MS=500`, `BUSINESS_DATA__SNAPSHOT_ORDERS=3`, `BUSINESS_DATA__SNAPSHOT_TICKETS=3` (defaults).
- Integration tests run in the isolated `rag_email_test` database and vhost (`tests/integration/conftest.py`), never against the live stack. Live-stack restarts (`make up`) are run by the owner.
- Verification command: `make ci` (ruff format --check, ruff check, strict mypy over `packages services tests evaluation`, unit and integration tests).

## Review Focus

These are the input classes the spec implies and a reviewer should check first, most likely first. Each names the task step that tests it.

1. **Sender address in another case, or with a display name.** `Alice.Smith@ClientCorp.COM` and `"Alice Smith" <alice.smith@clientcorp.com>` must resolve to the one seeded customer, and a near-miss (`at.buyer@…`, a missing TLD letter) must not. The parser splits off the display name (`parseaddr`), so the provider must receive the bare address.
   Tests: Task 4 Step 1 (`test_sender_match_ignores_case_and_surrounding_space`, `test_sender_match_is_on_the_whole_address`, Summit's case-variant row in `check_business_tenant_fixtures`); Task 6 Step 10 (`test_sender_case_and_display_name_reach_the_provider_as_the_bare_address`, which feeds an already-parsed `EmailAddress` and so covers case only). The display-name split itself is proven by the existing `tests/unit/test_email_normalization.py::TestEmailNormalizer::test_normalize_multipart_alternative_prefers_plain_text` (`Sender Name <sender@example.com>` → `EmailAddress("sender@example.com", "Sender Name")`), and end to end by the Task 8 live gate, which sends Alice's email with a `Alice Smith <alice.smith@clientcorp.com>` From header.
2. **An email that names another tenant's or another customer's order number.** `ORD-82915` exists in three tenants for three different customers. Each tenant must see only its own row, and a customer must get `NOT_FOUND` for another customer's order (Edward → Dana's `ORD-9901`), with none of that order's attributes.
   Tests: Task 4 Step 1 (`test_same_email_resolves_to_each_tenants_own_customer_and_orders`, `test_another_customers_order_and_ticket_are_not_found`, `check_business_tenant_fixtures` in memory and on Postgres); Task 6 Step 16 (a second tenant with the same address and number); Task 8 Step 1 (Edward's scenario, no status leak).
3. **No data versus no answer.** A provider timeout or error must read `UNAVAILABLE` (degraded). An order that does not exist, and a snapshot for a customer with zero orders or tickets, must read `NOT_FOUND`. The two must never merge.
   Tests: Task 3 Step 6 (`test_missing_typed_references_are_not_found`, `test_empty_snapshot_is_one_not_found_fact_per_entity`); Task 4 Step 1 (Delta Alice's empty snapshot in `check_business_tenant_fixtures`); Task 6 Step 5 (`test_timeout_degrades_every_planned_fact_to_unavailable` asserts no `NOT_FOUND` count); Task 7 Step 1 (`test_degraded_context_says_unavailable_never_not_found`).
4. **Prose that looks like an ID.** "an order 2 days ago", "in order to", "ticket 3 of 5" and "order 123" must plan nothing. "Order ORD-9901" must plan once, not twice.
   Tests: Task 5 Step 1 (`test_prose_and_short_numbers_do_not_match`, `test_fixture_subject_with_prefixed_refs_after_the_words`); Task 5 Step 2 (`test_nothing_planned_is_empty`).
5. **A thread reply that quotes an old order number.** The plan reads `body_text_clean`, from which the email-worker strips quoted history. An order number that appears only in the quoted part must not be planned; the subject is still scanned.
   Tests: Task 6 Step 10 (`test_order_number_only_in_quoted_history_is_not_planned`).

## File Structure

| File | Task | Responsibility |
|---|---|---|
| `packages/core/settings.py` (modify) | 1, 6 | `LLMTiersSettings`: blank-means-default and fail-fast on a Gemini model at the OpenAI URL; `BusinessDataSettings` at `AppSettings.business_data` |
| `docker-compose.yml` (modify) | 1, 6 | Forward `LLM__OPENAI_BASE_URL`, the three model names, `LLM__PRICE_TABLE` and `BUSINESS_DATA__*` through `x-app-env` |
| `tests/conftest.py` (modify) | 1 | Autouse guard also pins `LLM__PROVIDER=fake` |
| `scripts/llm_smoke.py` (create) | 1 | Owner-run live check: one triage call, one draft call |
| `Makefile` (modify) | 1, 8 | `llm-smoke` and `phase5-gate` targets |
| `.env.example`, `docs/configuration.md` (modify) | 1, 6 | Gemini block and §2.20 `BUSINESS_DATA__*` |
| `docs/observability.md` (modify) | 6 | Business metrics, span and log line |
| `packages/db/fixtures/business.py` (modify) | 2 | `ORD-82915` for Alice with two items; dates on every order and ticket |
| `packages/db/fixtures/business_tenants.py` (create) | 2 | Fixed-id 3-tenant fixtures: overlapping emails and numbers, one ambiguous address |
| `packages/db/fixtures/emails.py`, `__init__.py` (modify) | 2 | Alice's `order_status_inquiry` email; exports |
| `packages/db/seed.py` (modify) | 2 | `upsert_business_records`, `seed_business_tenant_fixtures` |
| `packages/domain/business.py` (create) | 3 | Enums, `EntityRef`, `FetchPlan`, `BusinessFact`, `BusinessContext.render()/to_payload()`, `unavailable_context` |
| `packages/domain/entities.py` (modify) | 6 | `ContextPackage.business_data: BusinessContext \| None`; section 7 uses `render()` |
| `packages/business/protocol.py` (create) | 3 | `BusinessDataProvider` protocol |
| `packages/business/assemble.py` (create) | 3 | Resolution and fact rules shared by both providers (`CustomerLookups`) |
| `packages/business/memory.py` (create) | 3 | `InMemoryBusinessDataProvider` |
| `packages/business/postgres.py` (create) | 3 | `PostgresBusinessDataProvider`: five scoped queries, read-only txn, `statement_timeout` |
| `packages/business/testing.py` (create) | 3, 4 | `BusinessDataProviderContractSuite`, datasets, `check_business_tenant_fixtures` |
| `packages/business/identifiers.py` (create) | 5 | `extract_entity_refs` |
| `packages/business/plan.py` (create) | 5 | `INTENT_ENTITIES`, `build_fetch_plan` |
| `packages/business/fetch.py` (create) | 6 | `fetch_business_context` (deadline, degradation, span, metrics, log line), `business_payload` |
| `packages/observability/metrics.py` (modify) | 6 | `business_lookups_total`, `business_lookup_latency_ms` |
| `packages/context/builder.py` (replace) | 6, 7 | Plan + bounded fetch in `build_context`; old stub removed; precedence rule |
| `packages/context/__init__.py` (modify) | 6 | Drop old protocol/stub exports |
| `services/ai_worker/main.py` (modify) | 6 | One `AgentProfileRegistry` shared with the generator; Postgres provider |
| `packages/llm/profile.py` (modify) | 7 | `BUSINESS_DATA_PRECEDENCE_RULE` in `DEFAULT_ENTERPRISE_INSTRUCTIONS` |
| `prompts/{support,billing,sales,general}.v2.j2` (create) | 7 | One `[BUSINESS DATA]` block via `business_data.render()` |
| `config/agent_profiles.yaml` (modify) | 7 | `prompt_template` / `prompt_version` `<name>.v2` |
| `scripts/phase5_gate.py` (create) | 8 | Owner-run live Phase 5 gate |
| `specs/design.md`, `specs/tasks.md`, `docs/adr/0008-business-data-fetch-plan.md`, research note, this plan (commit only) | 0 | The owner-approved spec change, committed before any code |
| `specs/tasks.md` (modify) | 9 | Checkboxes, closing notes, gate evidence |
| Tests (create) | 1–8 | `tests/unit/test_llm_smoke.py`, `test_domain_business.py`, `test_business_provider_contract.py`, `test_business_postgres_sql.py`, `test_business_identifiers.py`, `test_business_plan.py`, `test_business_fetch.py`, `test_context_builder_business.py`, `test_business_prompt_v2.py`, `test_phase5_gate.py`; `tests/integration/test_business_seed.py`, `test_business_provider_postgres.py`, `test_business_data_e2e.py` |
| Tests (modify) | 1, 2, 6, 7 | `test_settings.py`, `test_runtime_image_contract.py`, `test_seed_fixtures.py`, `test_seed_loader.py`, `test_observability_metrics.py`, `test_context_builder.py`, `test_ai_worker_main.py`, `test_domain_entities.py`, `test_single_pass_generator.py`, `test_draft_repair_orchestration.py`, `test_complexity_router_integration.py`, `test_agent_profile.py`, `test_generation_budget.py`, `test_context_builder_postgres.py` |

**Contract notes (additive, no decision needed).** These are recorded here so a reviewer does not treat them as drift:
- `fetch_business_context` and `ContextBuilder` take an optional `metrics: PipelineMetrics | None`. The repo injects metrics (`res.metrics`), and the tests assert on isolated registries.
- `PostgresBusinessDataProvider(pool, *, snapshot_orders, snapshot_tickets, statement_timeout_ms)` takes the pool first, as `PostgresSearchBackend` does.
- Additional helper names: `FetchPlan.ordered_snapshot`, `EntityRef.to_payload`, `BusinessFact.render_line`/`to_payload`, the module `packages/business/assemble.py`, `business_payload`, `BUSINESS_FETCH_LOG_EVENT`.
- `BusinessContext.to_payload()` is the full JSON-safe form, carrying statuses only (no customer attributes). The `CONTEXT_READY` event stores the flat keys `business_plan`, `customer_status`, `business_fact_statuses` (each `BusinessFact.to_payload()`) and `business_data_degraded`, the key name tasks.md requires.
- Task order: run Task 7 right after Task 6. Between the two commits, the live yaml still points at the v1 templates, which cannot render a `BusinessContext`. CI stays green, but do not deploy between them.

---

### Task 0: Commit the approved Phase 5 spec change on its own [tasks.md 5.0–5.6, ADR-0008]

CLAUDE.md §7.4 requires the design update, the ADR and the tasks.md sync to land before any implementation. They are in the working tree but uncommitted (`git status`: ` M specs/design.md`, ` M specs/tasks.md`, `?? docs/adr/0008-business-data-fetch-plan.md`, `?? artifacts/superpowers/2026-09-28-phase5-business-data-trigger-research.md`). Committing them first keeps Task 9's commits to checkbox and evidence edits only, and puts the design and the ADR inside the Task 9 audit range (`git diff 2b4fab9..HEAD`).

**Files:**
- Commit (no edits by the implementer): `specs/design.md` (§5.4 "Business data (R13)" and the matching edits further down, including §13.3), `specs/tasks.md` (the Phase 5 rewrite and the Phase 5 entries of the traceability table), `docs/adr/0008-business-data-fetch-plan.md`, `artifacts/superpowers/2026-09-28-phase5-business-data-trigger-research.md`, and this plan.

**Interfaces:**
- Consumes: the owner's approval of the spec change.
- Produces: one `docs(spec)` commit that every later task builds on.

- [ ] **Step 1: Confirm the owner approved the spec change, and ask Open Questions 1 and 2**

The spec files are owner-reserved (CLAUDE.md §7). Do not commit them until the owner confirms the working-tree versions are approved. In the same message, ask the owner Open Questions 1 (invoice references when the sender is not resolved) and 2 (the 5.6 "lexical branch" bullet). If the owner answers either now, the owner edits `specs/tasks.md` / `specs/design.md` §5.4 to match before Step 3, so the chosen rule is in the committed spec (and, for Q1, the implementer follows the chosen rule in Task 3 Step 6 and Step 8). An unanswered question does not block Tasks 1–8, but it keeps the affected task at `[~]` in Task 9 (Step 3 for 5.3, Step 6 for 5.6).

- [ ] **Step 2: Check that only the spec change is pending**

Run: `git status --short && git diff --stat specs/design.md specs/tasks.md`
Expected: only `specs/design.md` and `specs/tasks.md` modified; the ADR, the research note and this plan untracked. `specs/tasks.md` changes only the Phase 5 section and the R14/R20/R21/R24 traceability rows. If anything else is modified, stop and ask the owner.

- [ ] **Step 3: Commit**

```bash
git add specs/design.md specs/tasks.md docs/adr/0008-business-data-fetch-plan.md artifacts/superpowers/2026-09-28-phase5-business-data-trigger-research.md docs/superpowers/plans/2026-09-28-phase5-business-data-integration.md
git commit -m "$(cat <<'EOF'
docs(spec): code-side business-data fetch plan (ADR-0008) [task 5.0–5.6] [R13.1, R13.2, R13.3, R13.4, R13.5, R13.6, R13.7, R14.7, R20.6, R21.3, R21.6, R24.5]

Design §5.4 now plans business lookups in code from typed IDs, the profile's
context_policy and the intent, resolves the sender to one customer first, and bounds
the fetch with BUSINESS_DATA__TIMEOUT_MS. ADR-0008 records why, and the identity
assumption. tasks.md Phase 5 is rewritten to match (5.0 Gemini wiring through the
OpenAI-compatible endpoint, then 5.1–5.6), with the research note and the
implementation plan alongside. Owner-approved before implementation (CLAUDE.md §7.4).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 1: Hosted OpenAI-compatible provider wiring (Gemini) and live smoke check [tasks.md 5.0]

**Files:**
- Modify: `packages/core/settings.py` (line 14 pydantic import; `LLMTiersSettings` lines 210–270: new module constants, base-URL default, blank-means-default field validator, fail-fast model validator)
- Modify: `docker-compose.yml` (x-app-env anchor, lines 16–19: five new forwarded variables)
- Modify: `tests/conftest.py` (lines 23–28: the autouse guard also pins `LLM__PROVIDER=fake`)
- Modify: `.env.example` (section 6, lines 73–88)
- Modify: `docs/configuration.md` (§2.6 table rows lines 108–121, new Gemini subsection after the price-table block that ends near line 133; §2.19 compose prose at line 282)
- Modify: `Makefile` (line 1 `.PHONY`; help block lines 5–19; new target after `retrieval-gate`)
- Create: `scripts/llm_smoke.py`
- Test: `tests/unit/test_settings.py` (append), `tests/unit/test_runtime_image_contract.py` (parametrize list at lines 133–145, plus one new test), `tests/unit/test_llm_smoke.py` (new)

**Interfaces:**
- Consumes: `create_llm_provider(settings: LLMTiersSettings | None = None, *, client: httpx.AsyncClient | None = None, **kwargs) -> LLMProvider` (packages/llm/factory.py:20); `LLMProvider.generate(*, messages, schema=None, tier=ModelTier.FAST, max_tokens=1000, temperature=0.0, **params) -> LLMResult`; `LLMTriageOutput`, `prepare_triage_prompt(ctx: EmailContext) -> list[ChatMessage]` (services/triage_worker/llm_classifier.py); `AgentProfileRegistry.from_yaml(path)`, `.resolve_profile(category)`, `.render_prompt(profile, context: ContextPackage | dict)`, `.get_schema(profile)`; `assert_schema_matches_contract(schema)`, `validate_draft_payload(content, schema) -> DraftReplyPayload` (packages/llm/validation.py); `SmokeFailure` (scripts/stack_smoke.py).
- Produces:
  - `packages.core.settings.OPENAI_DEFAULT_BASE_URL: str = "https://api.openai.com/v1"`
  - `packages.core.settings.GEMINI_OPENAI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai"`
  - `LLMTiersSettings._blank_means_default(cls, value: Any, info: ValidationInfo) -> Any` (field validator, mode="before", on `openai_base_url`, `fast_model`, `strong_model`, `fallback_model`, `price_table`)
  - `LLMTiersSettings.validate_openai_endpoint_matches_models(self) -> "LLMTiersSettings"` (model validator, mode="after")
  - `scripts/llm_smoke.py`: `CallReport`, `UsageRecorder` (httpx response hook: the last response's token usage, so a truncated or unparseable response still reports its tokens), `mask_secret(value: str | None) -> str`, `redact(text: str, secret: str | None) -> str`, `active_key(llm: LLMTiersSettings) -> str | None`, `describe_config(llm: LLMTiersSettings) -> list[str]`, `async triage_call(provider, secret, usage) -> CallReport`, `async draft_call(provider, registry, secret, usage) -> CallReport`, `async run(settings: AppSettings, client: httpx.AsyncClient) -> list[CallReport]`, `render_reports(reports) -> list[str]`, `main() -> int`
  - Makefile target `llm-smoke`

Design decision: compose forwards the five variables with a **blank** default (`${VAR:-}`), and `LLMTiersSettings` treats a blank value as "unset" and restores the field default. This keeps the defaults in one place (settings.py) instead of duplicating the model names and the JSON price table in compose, and it closes both verified failure modes: `LLM__PRICE_TABLE=""` no longer fails validation, and a blank base URL becomes the OpenAI default, which the new fail-fast validator then refuses when a Gemini/Gemma model is named.

- [ ] **Step 1: Write the failing settings tests**

Append to `tests/unit/test_settings.py` (add `LLMTiersSettings` to the existing `from packages.core.settings import (...)` block, keeping it sorted, and add `import json` at the top):

```python
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_PRICE_TABLE = {
    "gemma-4-26b-a4b-it": {"input_per_m": 0, "output_per_m": 0},
    "gemma-4-31b-it": {"input_per_m": 0, "output_per_m": 0},
    "gemini-3.1-flash-lite": {"input_per_m": 0.25, "output_per_m": 1.50},
}


@pytest.mark.parametrize("field", ["fast_model", "strong_model", "fallback_model"])
@pytest.mark.parametrize(
    "model", ["gemma-4-26b-a4b-it", "gemini-3.1-flash-lite", "Gemini-3.1-Flash-Lite"]
)
def test_openai_provider_refuses_google_models_on_the_openai_url(field: str, model: str) -> None:
    """R20.6: a Gemini/Gemma model on the OpenAI default URL fails fast; the key stays home."""
    with pytest.raises(ValidationError, match="LLM__OPENAI_BASE_URL is the OpenAI default"):
        LLMTiersSettings.model_validate(
            {"provider": "openai", "openai_api_key": "k", field: model}
        )


def test_openai_default_url_with_trailing_slash_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="LLM__OPENAI_BASE_URL is the OpenAI default"):
        LLMTiersSettings(
            provider="openai",
            openai_base_url="https://api.openai.com/v1/",
            fast_model="gemma-4-26b-a4b-it",
        )


def test_gemini_models_on_the_gemini_url_are_accepted() -> None:
    llm = LLMTiersSettings(
        provider="openai",
        openai_base_url=GEMINI_BASE_URL,
        fast_model="gemma-4-26b-a4b-it",
        strong_model="gemma-4-31b-it",
        fallback_model="gemini-3.1-flash-lite",
    )
    assert llm.openai_base_url == GEMINI_BASE_URL
    assert llm.fallback_model == "gemini-3.1-flash-lite"


def test_openai_models_on_the_openai_url_are_accepted() -> None:
    llm = LLMTiersSettings(provider="openai", fast_model="gpt-4o-mini", strong_model="gpt-4o")
    assert llm.openai_base_url == "https://api.openai.com/v1"


def test_fake_provider_ignores_google_model_names() -> None:
    """The fake provider sends nothing, so the endpoint check does not apply."""
    llm = LLMTiersSettings(provider="fake", fast_model="gemma-4-26b-a4b-it")
    assert llm.fast_model == "gemma-4-26b-a4b-it"


def test_blank_llm_env_values_keep_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose forwards an unset host variable as ''; that must mean 'use the default'."""
    for var in (
        "LLM__OPENAI_BASE_URL",
        "LLM__FAST_MODEL",
        "LLM__STRONG_MODEL",
        "LLM__FALLBACK_MODEL",
        "LLM__PRICE_TABLE",
    ):
        monkeypatch.setenv(var, "")
    llm = AppSettings(_env_file=None).llm
    defaults = LLMTiersSettings()
    assert llm.openai_base_url == defaults.openai_base_url
    assert llm.fast_model == defaults.fast_model
    assert llm.strong_model == defaults.strong_model
    assert llm.fallback_model == defaults.fallback_model
    assert llm.price_table == defaults.price_table


def test_blank_base_url_with_a_gemini_model_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM__PROVIDER", "openai")
    monkeypatch.setenv("LLM__FAST_MODEL", "gemma-4-26b-a4b-it")
    monkeypatch.setenv("LLM__OPENAI_BASE_URL", "")
    with pytest.raises(ValidationError, match="LLM__OPENAI_BASE_URL is the OpenAI default"):
        AppSettings(_env_file=None)


def test_gemini_configuration_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented Gemini block parses, including the JSON price table (R21.6)."""
    monkeypatch.setenv("LLM__PROVIDER", "openai")
    monkeypatch.setenv("LLM__OPENAI_BASE_URL", GEMINI_BASE_URL)
    monkeypatch.setenv("LLM__FAST_MODEL", "gemma-4-26b-a4b-it")
    monkeypatch.setenv("LLM__STRONG_MODEL", "gemma-4-31b-it")
    monkeypatch.setenv("LLM__FALLBACK_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("LLM__PRICE_TABLE", json.dumps(GEMINI_PRICE_TABLE))
    llm = AppSettings(_env_file=None).llm
    assert llm.provider == "openai"
    assert llm.strong_model == "gemma-4-31b-it"
    assert llm.price_table["gemini-3.1-flash-lite"] == ModelPricing(
        input_per_m=0.25, output_per_m=1.50
    )
    assert llm.price_table["gemma-4-26b-a4b-it"] == ModelPricing(input_per_m=0, output_per_m=0)


def test_tests_always_run_with_the_fake_llm_provider() -> None:
    """R24.5: the autouse guard pins the fake provider even when the host .env names a real one."""
    assert AppSettings().llm.provider == "fake"
```

- [ ] **Step 2: Write the failing compose test**

In `tests/unit/test_runtime_image_contract.py`, extend the parametrize list of `test_compose_forwards_the_switch_into_app_containers` (lines 133–145) so it reads:

```python
@pytest.mark.parametrize(
    "variable",
    [
        "ROUTER_FORCE_SINGLE_TIER",
        "ROUTER_CONFIDENCE_THRESHOLD",
        "LLM__OPENAI_API_KEY",
        "LLM__ANTHROPIC_API_KEY",
        "LLM__OPENAI_BASE_URL",
        "LLM__FAST_MODEL",
        "LLM__STRONG_MODEL",
        "LLM__FALLBACK_MODEL",
        "LLM__PRICE_TABLE",
        "EMBEDDING__MODEL_NAME",
        "EMBEDDING__BASE_URL",
        "EMBEDDING__API_KEY",
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS",
    ],
)
```

and append after that test:

```python
@pytest.mark.parametrize(
    "variable",
    [
        "LLM__OPENAI_BASE_URL",
        "LLM__FAST_MODEL",
        "LLM__STRONG_MODEL",
        "LLM__FALLBACK_MODEL",
        "LLM__PRICE_TABLE",
    ],
)
def test_compose_llm_endpoint_defaults_defer_to_settings(variable: str) -> None:
    """R20.6: an unset host value reaches the container blank, and settings restores its default.

    A non-blank compose default would duplicate settings.py (and drift); the settings-side
    blank-means-default validator is covered in tests/unit/test_settings.py.
    """
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for service in ("api", "triage-worker", "ai-worker"):
        env = compose["services"][service]["environment"]
        assert env[variable] == "${" + variable + ":-}", service
```

- [ ] **Step 3: Write the failing guard change check and run all three**

The guard test is `test_tests_always_run_with_the_fake_llm_provider` from Step 1.

Run: `uv run pytest tests/unit/test_settings.py tests/unit/test_runtime_image_contract.py -v`
Expected: FAIL — the refusal tests fail with `Failed: DID NOT RAISE <class 'pydantic_core._pydantic_core.ValidationError'>`; `test_blank_llm_env_values_keep_the_defaults` fails with `ValidationError ... llm.price_table Input should be a valid dictionary [input_value='']`; the compose tests fail with `AssertionError: assert 'LLM__OPENAI_BASE_URL' in {...}` / `KeyError: 'LLM__OPENAI_BASE_URL'`. `test_tests_always_run_with_the_fake_llm_provider` passes today only if the host `.env` leaves `LLM__PROVIDER` unset; it guards the owner switching it to `openai` later.

- [ ] **Step 4: Implement the settings validators**

In `packages/core/settings.py` change line 14 to:

```python
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
```

(ruff will wrap it; run `uv run ruff format packages/core/settings.py`.)

Directly above `class LLMTiersSettings(BaseModel):` add:

```python
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
"""The `openai` provider's default endpoint; a blank LLM__OPENAI_BASE_URL resolves to it."""

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
"""Google's OpenAI-compatible Gemini endpoint, used by the project's live runs (task 5.0)."""

_GOOGLE_MODEL_MARKERS = ("gemini", "gemma")
```

Change the `openai_base_url` field to:

```python
    openai_base_url: str = Field(
        default=OPENAI_DEFAULT_BASE_URL,
        description=(
            "OpenAI-compatible API base URL; the Gemini API uses "
            f"{GEMINI_OPENAI_BASE_URL} (R14.7)"
        ),
    )
```

Append at the end of the `LLMTiersSettings` class body (after the `price_table` field):

```python
    @field_validator(
        "openai_base_url", "fast_model", "strong_model", "fallback_model", "price_table",
        mode="before",
    )
    @classmethod
    def _blank_means_default(cls, value: Any, info: ValidationInfo) -> Any:
        """Treat a blank value as unset (R20.6).

        Docker Compose forwards an unset host variable as an empty string. An empty model
        name or price table is never a valid setting, and an empty base URL would otherwise
        make the OpenAI client fall back to api.openai.com silently.
        """
        if isinstance(value, str) and not value.strip() and info.field_name is not None:
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        return value

    @model_validator(mode="after")
    def validate_openai_endpoint_matches_models(self) -> "LLMTiersSettings":
        """Fail fast when the openai provider would send a Google key to OpenAI (R20.6, R14.7).

        Gemini and Gemma models are served by Google's OpenAI-compatible endpoint. Naming one
        while LLM__OPENAI_BASE_URL is still the OpenAI default means the configured key would
        be sent to the wrong host, so startup refuses the configuration instead.
        """
        if self.provider.lower().strip() != "openai":
            return self
        if self.openai_base_url.rstrip("/") != OPENAI_DEFAULT_BASE_URL:
            return self
        google_models = [
            model
            for model in (self.fast_model, self.strong_model, self.fallback_model)
            if any(marker in model.lower() for marker in _GOOGLE_MODEL_MARKERS)
        ]
        if google_models:
            raise ValueError(
                "LLM__PROVIDER=openai names Gemini/Gemma model(s) "
                f"{', '.join(google_models)} but LLM__OPENAI_BASE_URL is the OpenAI default; "
                f"set LLM__OPENAI_BASE_URL={GEMINI_OPENAI_BASE_URL}"
            )
        return self
```

- [ ] **Step 5: Forward the variables through compose**

In `docker-compose.yml`, replace lines 16–19:

```yaml
  LLM__PROVIDER: ${LLM__PROVIDER:-fake}
  # Real-provider keys (empty keeps the fake working) and the R15.6 cascade switch.
  LLM__OPENAI_API_KEY: ${LLM__OPENAI_API_KEY:-}
  LLM__ANTHROPIC_API_KEY: ${LLM__ANTHROPIC_API_KEY:-}
```

with:

```yaml
  LLM__PROVIDER: ${LLM__PROVIDER:-fake}
  # Real-provider keys (empty keeps the fake working) and the R15.6 cascade switch.
  LLM__OPENAI_API_KEY: ${LLM__OPENAI_API_KEY:-}
  LLM__ANTHROPIC_API_KEY: ${LLM__ANTHROPIC_API_KEY:-}
  # OpenAI-compatible endpoint (e.g. the Gemini API), its tier models and the per-model price
  # table (R14.7, R21.6). Blank means "settings default" (packages/core/settings.py), so the
  # defaults live in one place; a Gemini/Gemma model on the default URL fails fast (R20.6).
  LLM__OPENAI_BASE_URL: ${LLM__OPENAI_BASE_URL:-}
  LLM__FAST_MODEL: ${LLM__FAST_MODEL:-}
  LLM__STRONG_MODEL: ${LLM__STRONG_MODEL:-}
  LLM__FALLBACK_MODEL: ${LLM__FALLBACK_MODEL:-}
  LLM__PRICE_TABLE: ${LLM__PRICE_TABLE:-}
```

The anchor reaches init, api, mail-connector, email-worker, triage-worker, knowledge-worker and ai-worker (every service that merges `<<: *app-env`). dispatch-worker (Phase-0 stub) and frontend do not merge it and make no LLM calls.

- [ ] **Step 6: Pin the fake provider in the test guard**

In `tests/conftest.py`, replace `guard_live_credentials` (lines 23–28) with:

```python
@pytest.fixture(autouse=True)
def guard_live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no live provider API keys leak into test executions (R24.5).

    Process env outranks the .env file in pydantic-settings, so pinning LLM__PROVIDER=fake
    here keeps every AppSettings()/AIWorkerSettings() built by a test on the fake provider,
    even after the owner sets LLM__PROVIDER=openai in the host .env for live runs.
    """
    for var in PROVIDER_SECRET_ENV_VARS:
        if var in os.environ:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM__PROVIDER", "fake")
```

- [ ] **Step 7: Run the settings, compose and full unit suite**

Run: `uv run pytest tests/unit/test_settings.py tests/unit/test_runtime_image_contract.py tests/unit/test_llm_factory.py -v`
Expected: PASS.
Run: `uv run pytest tests/unit -q`
Expected: PASS (no other unit test names a Gemini/Gemma model or sets `LLM__PROVIDER`).
Run: `docker compose config --quiet && echo compose-ok`
Expected: `compose-ok` (validates interpolation only; `--quiet` prints nothing, so no key reaches the terminal).
Run: `uv run ruff format tests/unit/test_settings.py tests/unit/test_runtime_image_contract.py tests/conftest.py packages/core/settings.py`
Expected: the block appended to `tests/unit/test_settings.py` in Step 1 is not in ruff-format style as written, so this reformats it; then `uv run ruff format --check .` is clean before the Step 9 commit.

- [ ] **Step 8: Document the keys**

Replace `.env.example` section 6 (lines 73–88) with:

```dotenv
# --- 6. LLM Configuration, Tiers & Ablation Flags (R14.5, R14.7, R15.1, R15.6, R20.6, R21.6, R24.5) ---
LLM__PROVIDER=fake
LLM__FAST_MODEL=gpt-4o-mini
LLM__STRONG_MODEL=gpt-4o
LLM__FALLBACK_MODEL=claude-3-haiku
LLM__FORCE_SINGLE_TIER=false
LLM__TIMEOUT_S=15.0
LLM__OPENAI_API_KEY=
LLM__OPENAI_BASE_URL=https://api.openai.com/v1
LLM__ANTHROPIC_API_KEY=
LLM__ANTHROPIC_BASE_URL=https://api.anthropic.com/v1
# LLM__PRICE_TABLE replaces the whole per-model price table (USD per 1M tokens). A model
# missing from it stores cost_estimate = NULL (unknown), never 0. Example:
# LLM__PRICE_TABLE={"gpt-4o-mini":{"input_per_m":0.15,"output_per_m":0.60},"gpt-4o":{"input_per_m":5.0,"output_per_m":15.0}}
LLM__LOCAL_BASE_URL=http://localhost:11434/v1
LLM__LOCAL_API_KEY=ollama
#
# Google Gemini API through its OpenAI-compatible endpoint (the project's live runs, task 5.0).
# Docker Compose forwards LLM__OPENAI_BASE_URL, the three model names and LLM__PRICE_TABLE into
# the app containers; a blank value keeps the settings default. With LLM__PROVIDER=openai, a
# Gemini/Gemma model on the default OpenAI base URL fails settings validation at startup, so
# the key is never sent to api.openai.com (R20.6). Gemma is free of charge (free tier only);
# gemini-3.1-flash-lite costs $0.25 / $1.50 per 1M input / output tokens (Google pricing page,
# updated 2026-09-24) (R21.6). Check the configuration live with `make llm-smoke`.
# LLM__PROVIDER=openai
# LLM__OPENAI_API_KEY=<Gemini API key from Google AI Studio>
# LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
# LLM__FAST_MODEL=gemma-4-26b-a4b-it
# LLM__STRONG_MODEL=gemma-4-31b-it
# LLM__FALLBACK_MODEL=gemini-3.1-flash-lite
# LLM__PRICE_TABLE={"gemma-4-26b-a4b-it":{"input_per_m":0,"output_per_m":0},"gemma-4-31b-it":{"input_per_m":0,"output_per_m":0},"gemini-3.1-flash-lite":{"input_per_m":0.25,"output_per_m":1.50},"text-embedding-3-small":{"input_per_m":0.02,"output_per_m":0}}
```

In `docs/configuration.md` §2.6, replace these five table rows:

```markdown
| `LLM__FAST_MODEL` | `string` | `gpt-4o-mini` | Non-empty | Tier 1 model for triage & summarization |
| `LLM__STRONG_MODEL` | `string` | `gpt-4o` | Non-empty | Tier 2 model for complex draft generation |
| `LLM__FALLBACK_MODEL` | `string` | `claude-3-haiku`| Non-empty | Tier 3 model for retry recovery |
```
```markdown
| `LLM__OPENAI_BASE_URL` | `string` | `https://api.openai.com/v1` | URL | OpenAI API base endpoint |
```
```markdown
| `LLM__PRICE_TABLE` | `JSON object` | the four models below | `{model: {input_per_m, output_per_m}}`, USD per 1M tokens, each `≥ 0` | Replaces the **whole** price table (not merged). Keys must match the model names providers report (e.g. `LLM__FAST_MODEL`); a local model (`LLM__PROVIDER=local`) needs its own entry (R21.6) |
```

with:

```markdown
| `LLM__FAST_MODEL` | `string` | `gpt-4o-mini` | Blank = default | Tier 1 (routine) model for triage, summarization and routine drafts (R15.1) |
| `LLM__STRONG_MODEL` | `string` | `gpt-4o` | Blank = default | Tier 2 (high-capability) model for escalated drafts (R15.1) |
| `LLM__FALLBACK_MODEL` | `string` | `claude-3-haiku`| Blank = default | Tier 3 model for retry recovery |
```
```markdown
| `LLM__OPENAI_BASE_URL` | `string` | `https://api.openai.com/v1` | URL; blank = default | Base URL of any OpenAI-compatible `/chat/completions` endpoint, e.g. the Gemini API. With `LLM__PROVIDER=openai`, a Gemini/Gemma model on this default fails startup validation (R14.7, R20.6) |
```
```markdown
| `LLM__PRICE_TABLE` | `JSON object` | the four models below | `{model: {input_per_m, output_per_m}}`, USD per 1M tokens, each `≥ 0`; blank = default | Replaces the **whole** price table (not merged). Keys must match the model names providers report (e.g. `LLM__FAST_MODEL`); a local model (`LLM__PROVIDER=local`) needs its own entry (R21.6) |
```

Immediately after the paragraph that starts "A model with no entry in the table has an **unknown** cost" (end of §2.6), insert:

````markdown
#### Hosted OpenAI-compatible endpoint: Google Gemini API (R14.7, R20.6, R21.6)

The `openai` provider talks to any OpenAI-compatible `/chat/completions` endpoint. The project's live runs use the Google Gemini API this way:

```dotenv
LLM__PROVIDER=openai
LLM__OPENAI_API_KEY=<Gemini API key from Google AI Studio>
LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM__FAST_MODEL=gemma-4-26b-a4b-it
LLM__STRONG_MODEL=gemma-4-31b-it
LLM__FALLBACK_MODEL=gemini-3.1-flash-lite
LLM__PRICE_TABLE={"gemma-4-26b-a4b-it":{"input_per_m":0,"output_per_m":0},"gemma-4-31b-it":{"input_per_m":0,"output_per_m":0},"gemini-3.1-flash-lite":{"input_per_m":0.25,"output_per_m":1.50},"text-embedding-3-small":{"input_per_m":0.02,"output_per_m":0}}
```

- **Prices.** Gemma is free of charge (free tier only); `gemini-3.1-flash-lite` costs $0.25 / $1.50 per 1M input / output tokens (Google pricing page, updated 2026-09-24). Because `LLM__PRICE_TABLE` replaces the whole table, keep every model you route to in it, or its cost is recorded as unknown.
- **Fail-fast check.** With `LLM__PROVIDER=openai`, settings validation refuses a Gemini or Gemma model name while `LLM__OPENAI_BASE_URL` is still `https://api.openai.com/v1` (with or without a trailing slash), so the key is never sent to the wrong host.
- **Docker Compose.** `LLM__PROVIDER`, `LLM__OPENAI_API_KEY`, `LLM__OPENAI_BASE_URL`, `LLM__FAST_MODEL`, `LLM__STRONG_MODEL`, `LLM__FALLBACK_MODEL` and `LLM__PRICE_TABLE` are forwarded from the host `.env` into every service that uses the shared `x-app-env` block (init, api, mail-connector, email-worker, triage-worker, knowledge-worker, ai-worker). An unset host value arrives blank, and a blank value means "use the default" for the base URL, the three model names and the price table.
- **Live smoke check.** `make llm-smoke` sends one triage request and one draft request through the configured provider and reports, per request, whether the response parsed and validated, the `finish_reason` and the token counts. It never prints the key. It is run by hand and is not part of CI (R24.5); tests always run on the fake provider.
````

In §2.19 (line 282), replace:

```markdown
Under Docker Compose only `ROUTER_FORCE_SINGLE_TIER` and `ROUTER_CONFIDENCE_THRESHOLD` (with `LLM__OPENAI_API_KEY` and `LLM__ANTHROPIC_API_KEY`) are forwarded from the host `.env` into the app containers; set another router variable in `docker-compose.yml` before relying on it there.
```

with:

```markdown
Under Docker Compose only `ROUTER_FORCE_SINGLE_TIER` and `ROUTER_CONFIDENCE_THRESHOLD` (with `LLM__PROVIDER`, the API keys, `LLM__OPENAI_BASE_URL`, the three `LLM__*_MODEL` names and `LLM__PRICE_TABLE`, see §2.6) are forwarded from the host `.env` into the app containers; set another router variable in `docker-compose.yml` before relying on it there.
```

- [ ] **Step 9: Commit the wiring**

```bash
git add packages/core/settings.py docker-compose.yml tests/conftest.py tests/unit/test_settings.py tests/unit/test_runtime_image_contract.py .env.example docs/configuration.md
git commit -m "$(cat <<'EOF'
feat(llm): forward the OpenAI-compatible endpoint into compose and fail fast on a Gemini model at the OpenAI URL [task 5.0] [R14.7, R20.6, R21.6, R24.5]

Compose now forwards LLM__OPENAI_BASE_URL, the three tier model names and
LLM__PRICE_TABLE through x-app-env with blank defaults; LLMTiersSettings treats a
blank value as unset, so the defaults stay in settings.py and an empty price table
no longer fails. With provider=openai, a Gemini/Gemma model on the OpenAI default
base URL fails validation, so the key is never sent to api.openai.com. The test
guard pins LLM__PROVIDER=fake so a host .env set for live runs cannot reach tests.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 10: Write the failing smoke-script tests**

Create `tests/unit/test_llm_smoke.py`:

```python
"""scripts/llm_smoke.py reports parse/validate, finish_reason and tokens, never the key (5.0).

Requirements: R14.7, R24.5. Every request goes to an httpx.MockTransport; nothing is live.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from packages.core.settings import AppSettings, LLMTiersSettings, ModelPricing

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"
KEY = "test-gemini-key-5f2c9a"


def _load_smoke() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the script imports its sibling stack_smoke
    spec = importlib.util.spec_from_file_location("llm_smoke", SCRIPTS / "llm_smoke.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the script uses `from __future__ import annotations` and a
    # @dataclass, and dataclasses resolves string annotations via sys.modules[__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()

TRIAGE = {
    "category": "general_inquiry",
    "intent": "order_status",
    "priority": "normal",
    "reply_required": True,
    "workflow_hint": "ai",
    "retrieval_required": True,
    "confidence": 0.9,
    "reasoning": "asks for an order status",
}
DRAFT = {
    "action": "reply",
    "draft": "Your order is on its way; the courier link follows once it is scanned.",
    "confidence": 0.8,
    "knowledge_chunks": ["smoke-order-status-procedure"],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


def _settings(**llm: Any) -> AppSettings:
    fields: dict[str, Any] = {
        "provider": "openai",
        "openai_api_key": KEY,
        "openai_base_url": GEMINI,
        "fast_model": "gemma-4-26b-a4b-it",
        "strong_model": "gemma-4-31b-it",
        "fallback_model": "gemini-3.1-flash-lite",
    }
    fields.update(llm)
    return AppSettings(_env_file=None, llm=LLMTiersSettings.model_validate(fields))


def _completion(content: str, finish_reason: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 321, "completion_tokens": 45},
    }


def _client(
    seen: list[httpx.Request],
    *,
    draft_content: str | None = None,
    draft_finish: str = "stop",
    draft_status: int = 200,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]["schema"]
        if "draft" in schema.get("required", []):
            if draft_status != 200:
                # A provider that echoes the key in its error body must still not leak it.
                return httpx.Response(
                    draft_status, json={"error": {"message": f"API key {KEY} not valid"}}
                )
            content = json.dumps(DRAFT) if draft_content is None else draft_content
            return httpx.Response(200, json=_completion(content, draft_finish))
        return httpx.Response(200, json=_completion(json.dumps(TRIAGE), "stop"))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_both_calls_pass_on_schema_valid_json() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen) as http:
        triage, draft = await smoke.run(_settings(), http)

    assert (triage.name, triage.ok, draft.name, draft.ok) == ("triage", True, "draft", True)
    for report in (triage, draft):
        assert report.finish_reason == "stop"
        assert (report.input_tokens, report.output_tokens) == (321, 45)
        assert report.model == "gemma-4-26b-a4b-it"
    assert "cites the given chunk: yes" in draft.detail

    assert len(seen) == 2
    for request in seen:
        assert str(request.url) == f"{GEMINI}/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
    bodies = [json.loads(r.content) for r in seen]
    assert [b["max_tokens"] for b in bodies] == [250, 1000]
    assert [b["model"] for b in bodies] == ["gemma-4-26b-a4b-it", "gemma-4-26b-a4b-it"]


async def test_truncated_draft_reports_length_and_fails() -> None:
    seen: list[httpx.Request] = []
    truncated = '{"action": "reply", "draft": "Your ord'
    async with _client(seen, draft_content=truncated, draft_finish="length") as http:
        _, draft = await smoke.run(_settings(), http)

    assert draft.ok is False
    assert draft.finish_reason == "length"
    assert "did not parse" in draft.detail
    # HttpLLMProvider raises before it reads `usage`; the script records it from the response.
    assert (draft.input_tokens, draft.output_tokens) == (321, 45)


async def test_parsed_but_invalid_draft_fails_validation() -> None:
    seen: list[httpx.Request] = []
    missing_tier = {k: v for k, v in DRAFT.items() if k != "model_tier"}
    async with _client(seen, draft_content=json.dumps(missing_tier)) as http:
        _, draft = await smoke.run(_settings(), http)

    assert draft.ok is False
    assert "failed validation" in draft.detail
    assert draft.finish_reason == "stop"
    assert draft.input_tokens == 321


async def test_output_never_contains_the_key() -> None:
    seen: list[httpx.Request] = []
    settings = _settings()
    async with _client(seen, draft_status=401) as http:
        reports = await smoke.run(settings, http)

    lines = smoke.describe_config(settings.llm) + smoke.render_reports(reports)
    text = "\n".join(lines)
    assert KEY not in text
    assert f"set ({len(KEY)} chars)" in text
    assert reports[1].ok is False
    assert "LLMResponseError" in reports[1].detail


async def test_refuses_the_fake_provider() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(smoke.SmokeFailure, match="LLM__PROVIDER is fake"):
            await smoke.run(_settings(provider="fake"), http)


def test_config_warns_about_models_missing_from_the_price_table() -> None:
    unpriced = "\n".join(smoke.describe_config(_settings().llm))
    assert "warn" in unpriced and "gemma-4-26b-a4b-it" in unpriced

    priced = _settings(
        price_table={
            "gemma-4-26b-a4b-it": ModelPricing(input_per_m=0, output_per_m=0),
            "gemma-4-31b-it": ModelPricing(input_per_m=0, output_per_m=0),
            "gemini-3.1-flash-lite": ModelPricing(input_per_m=0.25, output_per_m=1.5),
        }
    )
    assert "warn" not in "\n".join(smoke.describe_config(priced.llm))
```

Run: `uv run pytest tests/unit/test_llm_smoke.py -v`
Expected: FAIL during collection with `FileNotFoundError: [Errno 2] No such file or directory: '.../scripts/llm_smoke.py'`.

- [ ] **Step 11: Implement the smoke script**

Create `scripts/llm_smoke.py`:

```python
"""Live LLM smoke check for task 5.0: one triage call and one draft call (R14.7, R24.5).

Run on the host by hand, never in CI (reads .env like every host tool):

    make llm-smoke

It sends two real requests through the configured provider. For the Gemini API set, in .env:
LLM__PROVIDER=openai, LLM__OPENAI_API_KEY, LLM__OPENAI_BASE_URL=
https://generativelanguage.googleapis.com/v1beta/openai and the three LLM__*_MODEL names
(docs/configuration.md §2.6).

Checks, one request each:
  1. Triage: the production triage prompt and LLMTriageOutput schema, fast tier,
     max_tokens=250 (the classifier's settings). The response must parse and validate.
  2. Draft: the technical_support profile's template and reply schema, the profile's tier,
     max_tokens=1000 (the generator's settings). The response must parse and pass
     validate_draft_payload.
For each request it prints whether the response parsed and validated, the finish_reason,
the model and the token counts. It never prints the API key: the configuration line masks
it, and error text is redacted and truncated.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError
from stack_smoke import SmokeFailure

from packages.core.settings import AppSettings, LLMTiersSettings
from packages.domain.rules import EmailContext
from packages.llm.factory import create_llm_provider
from packages.llm.profile import AgentProfileRegistry
from packages.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResult,
    LLMSchemaValidationError,
    ModelTier,
)
from packages.llm.validation import assert_schema_matches_contract, validate_draft_payload
from services.triage_worker.llm_classifier import LLMTriageOutput, prepare_triage_prompt

TRIAGE_MAX_TOKENS = 250  # LLMTriageClassifier default (services/triage_worker/llm_classifier.py)
DRAFT_MAX_TOKENS = 1000  # SinglePassGenerator default (packages/llm/generator.py)
DRAFT_CATEGORY = "support"  # resolves to the technical_support profile
LOCAL_PROVIDERS = ("local", "ollama", "vllm")
MAX_DETAIL = 300

SENDER = "alice.smith@clientcorp.com"
SUBJECT = "Order status question"
BODY = (
    "Hi,\n\nWhat is the status of order 82915? I placed it last week and have not received "
    "a tracking update yet.\n\nThanks,\nAlice"
)
SMOKE_CHUNK_ID = "smoke-order-status-procedure"
PROCEDURE = (
    "Order status questions: look up the order, tell the customer its current status, and "
    "share the courier tracking link once the order has left the warehouse."
)


@dataclass(frozen=True)
class CallReport:
    """Outcome of one smoke request, safe to print (no key, bounded detail)."""

    name: str
    ok: bool
    detail: str
    model: str | None = None
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class UsageRecorder:
    """httpx response hook that keeps the token usage of the last completion response.

    HttpLLMProvider raises LLMSchemaValidationError before it reads `usage`, so without this
    a truncated (finish_reason=length) or unparseable response would report 0/0 tokens.
    """

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    async def __call__(self, response: httpx.Response) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        if response.status_code != 200:
            return
        await response.aread()  # a response hook must read the body before inspecting it
        try:
            usage = response.json().get("usage") or {}
            self.input_tokens = int(usage.get("prompt_tokens", 0))
            self.output_tokens = int(usage.get("completion_tokens", 0))
        except (ValueError, TypeError, AttributeError):
            self.input_tokens = 0
            self.output_tokens = 0


def mask_secret(value: str | None) -> str:
    """Say whether a secret is set without revealing any of it."""
    return f"set ({len(value)} chars)" if value else "unset"


def redact(text: str, secret: str | None) -> str:
    """Remove the secret, collapse whitespace and cap the length of error text."""
    if secret:
        text = text.replace(secret, "***")
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 3] + "..."


def active_key(llm: LLMTiersSettings) -> str | None:
    """The key the configured provider sends."""
    provider = llm.provider.lower().strip()
    if provider == "anthropic":
        return llm.anthropic_api_key
    if provider in LOCAL_PROVIDERS:
        return llm.local_api_key
    return llm.openai_api_key


def describe_config(llm: LLMTiersSettings) -> list[str]:
    """Configuration lines with the key masked, plus a warning for unpriced models (R21.6)."""
    provider = llm.provider.lower().strip()
    if provider == "anthropic":
        base_url = llm.anthropic_base_url
    elif provider in LOCAL_PROVIDERS:
        base_url = llm.local_base_url
    else:
        base_url = llm.openai_base_url
    lines = [
        f"provider {provider}  base_url {base_url}  key {mask_secret(active_key(llm))}",
        f"models   fast {llm.fast_model}  strong {llm.strong_model}  "
        f"fallback {llm.fallback_model}",
    ]
    unpriced = sorted({llm.fast_model, llm.strong_model, llm.fallback_model} - set(llm.price_table))
    if unpriced:
        lines.append(
            "warn     no LLM__PRICE_TABLE entry for "
            + ", ".join(unpriced)
            + ": their cost is recorded as unknown (R21.6)"
        )
    return lines


async def _call(
    provider: LLMProvider,
    *,
    name: str,
    messages: list[ChatMessage],
    schema: dict[str, Any],
    tier: ModelTier,
    max_tokens: int,
    validate: Callable[[LLMResult], str],
    secret: str | None,
    usage: UsageRecorder,
) -> CallReport:
    try:
        result = await provider.generate(
            messages=messages, schema=schema, tier=tier, max_tokens=max_tokens, temperature=0.0
        )
    except LLMSchemaValidationError as exc:
        return CallReport(
            name,
            ok=False,
            detail="response did not parse as a JSON object: " + redact(str(exc), secret),
            finish_reason=exc.finish_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
    except LLMError as exc:
        return CallReport(name, ok=False, detail=f"{type(exc).__name__}: {redact(str(exc), secret)}")
    try:
        detail = validate(result)
    except (ValidationError, LLMSchemaValidationError) as exc:
        return CallReport(
            name,
            ok=False,
            detail="parsed, but failed validation: " + redact(str(exc), secret),
            model=result.model,
            finish_reason=result.raw_finish_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
    return CallReport(
        name,
        ok=True,
        detail=detail,
        model=result.model,
        finish_reason=result.raw_finish_reason,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


async def triage_call(provider: LLMProvider, secret: str | None, usage: UsageRecorder) -> CallReport:
    """One request with the production triage prompt and schema."""
    ctx = EmailContext(sender_email=SENDER, subject=SUBJECT, body_text=BODY, body_text_clean=BODY)

    def validate(result: LLMResult) -> str:
        parsed = LLMTriageOutput.model_validate(result.content)
        return (
            f"category {parsed.category}, intent {parsed.intent}, "
            f"confidence {parsed.confidence:.2f}"
        )

    return await _call(
        provider,
        name="triage",
        messages=prepare_triage_prompt(ctx),
        schema=LLMTriageOutput.model_json_schema(),
        tier=ModelTier.FAST,
        max_tokens=TRIAGE_MAX_TOKENS,
        validate=validate,
        secret=secret,
        usage=usage,
    )


def draft_context() -> dict[str, Any]:
    """Template variables for the draft prompt: one email and one procedure chunk.

    business_data stays None so the same context renders under the v1 and v2 templates.
    """
    return {
        "current_message": {
            "sender": f"Alice Smith <{SENDER}>",
            "subject": SUBJECT,
            "received_at": "2026-09-28T09:00:00+00:00",
            "body_text": BODY,
            "body_text_clean": BODY,
        },
        "thread_summary": None,
        "recent_messages": [],
        "retrieved_chunks": [{"chunk_id": SMOKE_CHUNK_ID, "external_id": None, "content": PROCEDURE}],
        "business_data": None,
    }


async def draft_call(
    provider: LLMProvider,
    registry: AgentProfileRegistry,
    secret: str | None,
    usage: UsageRecorder,
) -> CallReport:
    """One request with a real profile's template and reply schema."""
    profile = registry.resolve_profile(DRAFT_CATEGORY)
    schema = registry.get_schema(profile)
    assert_schema_matches_contract(schema)
    prompt = registry.render_prompt(profile, draft_context())

    def validate(result: LLMResult) -> str:
        payload = validate_draft_payload(result.content, schema)
        cited = "yes" if SMOKE_CHUNK_ID in payload.knowledge_chunks else "no"
        return (
            f"profile {profile.profile} ({profile.prompt_version}), action {payload.action}, "
            f"cites the given chunk: {cited}"
        )

    return await _call(
        provider,
        name="draft",
        messages=[ChatMessage(role="user", content=prompt)],
        schema=schema,
        tier=ModelTier(profile.model_tier),
        max_tokens=DRAFT_MAX_TOKENS,
        validate=validate,
        secret=secret,
        usage=usage,
    )


async def run(settings: AppSettings, client: httpx.AsyncClient) -> list[CallReport]:
    """Send the triage and the draft request through the configured provider."""
    llm = settings.llm
    if llm.provider.lower().strip() == "fake":
        raise SmokeFailure(
            "LLM__PROVIDER is fake; set LLM__PROVIDER=openai with the Gemini base URL, key and "
            "models in .env (docs/configuration.md §2.6)"
        )
    secret = active_key(llm)
    provider = create_llm_provider(llm, client=client)
    registry = AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path)
    usage = UsageRecorder()
    client.event_hooks["response"].append(usage)
    try:
        return [
            await triage_call(provider, secret, usage),
            await draft_call(provider, registry, secret, usage),
        ]
    finally:
        client.event_hooks["response"].remove(usage)


def render_reports(reports: list[CallReport]) -> list[str]:
    """Two printable lines per request."""
    lines: list[str] = []
    for report in reports:
        mark = "ok  " if report.ok else "FAIL"
        lines.append(f"{mark} {report.name}: {report.detail}")
        lines.append(
            f"     model {report.model or '-'}  finish_reason {report.finish_reason or '-'}  "
            f"tokens in {report.input_tokens} out {report.output_tokens}"
        )
    return lines


async def _run_with_client(settings: AppSettings) -> list[CallReport]:
    async with httpx.AsyncClient(timeout=settings.llm.timeout_s) as client:
        return await run(settings, client)


def main() -> int:
    # The HTTP client logs provider error bodies; keep output to the redacted lines below.
    logging.getLogger("packages.llm").setLevel(logging.CRITICAL)
    try:
        settings = AppSettings()
    except ValidationError as exc:
        # Print locations and messages only: input values could include the key.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        print(f"FAIL settings: {problems}", file=sys.stderr)
        return 1
    for line in describe_config(settings.llm):
        print(line)
    try:
        reports = asyncio.run(_run_with_client(settings))
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    for line in render_reports(reports):
        print(line)
    if not all(report.ok for report in reports):
        print("FAIL at least one request did not return a schema-valid response", file=sys.stderr)
        return 1
    print("LLM SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run `uv run ruff format scripts/llm_smoke.py tests/unit/test_llm_smoke.py` to wrap any line over 100 characters.

- [ ] **Step 12: Add the Makefile target**

In `Makefile` line 1, append ` llm-smoke` to the `.PHONY` list (after `retrieval-gate`). In the help block, after the `smoke` echo line, add:

```makefile
	@echo "  llm-smoke - Live check: one triage + one draft request through the configured LLM (task 5.0; not CI)"
```

After the `retrieval-gate` target, add:

```makefile
llm-smoke:
	$(UV) run python scripts/llm_smoke.py
```

- [ ] **Step 13: Run the smoke tests, lint and the full CI**

Run: `uv run pytest tests/unit/test_llm_smoke.py -v`
Expected: PASS (6 tests).
Run: `uv run ruff format --check .` (confirms the Step 7 format of `tests/unit/test_settings.py` held).
Run: `make ci`
Expected: PASS (fmt-check, ruff, mypy over packages/services/tests/evaluation, unit and integration tests; integration runs against `rag_email_test` and needs the infra containers up).

- [ ] **Step 14: Commit the smoke check**

```bash
git add scripts/llm_smoke.py tests/unit/test_llm_smoke.py Makefile
git commit -m "$(cat <<'EOF'
feat(llm): add the owner-run live LLM smoke check [task 5.0] [R14.7, R24.5]

make llm-smoke sends one triage request (production prompt and LLMTriageOutput schema
at 250 tokens) and one draft request (technical_support template and reply schema at
1000 tokens) through the configured provider, and reports whether each response
parsed and validated, its finish_reason and its token counts. The key is masked and
error text is redacted. Unit tests drive it through httpx.MockTransport only.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 15: Owner-run live check (not the implementer; not CI)**

Hand to the owner: in the host `.env` set `LLM__PROVIDER=openai` and `LLM__OPENAI_API_KEY=<Gemini key>` (the base URL and model names are already there), optionally add the `LLM__PRICE_TABLE` line from `.env.example`, then run `make llm-smoke`. Expected: two `ok` lines with `finish_reason stop` and `LLM SMOKE OK`. Then `make up` and confirm the stack reaches healthy (CLAUDE.md §3 item 3); live-stack restarts are run by the owner.

---

### Task 2: Business seed data: ORD-82915 for Alice, her fixture email, and 3-tenant business fixtures [tasks.md 5.1]

**Files:**
- Modify: `packages/db/fixtures/business.py` (whole file: add `placed_at`/`shipped_at` to orders, `opened_at` to tickets, `ORDER_82915_ID`, two order-item rows)
- Create: `packages/db/fixtures/business_tenants.py`
- Modify: `packages/db/fixtures/emails.py` (module docstring lines 1–10; append a 7th fixture before the closing `]` at line 223)
- Modify: `packages/db/fixtures/__init__.py` (imports and `__all__`)
- Modify: `packages/db/seed.py` (imports lines 13–47; replace the business block lines 205–300 with a call; add `upsert_business_records` and `seed_business_tenant_fixtures`)
- Test: `tests/unit/test_seed_fixtures.py` (imports, new tests), `tests/integration/test_business_seed.py` (new), `tests/integration/test_seed_loader.py` (count updates lines 59–67, 105, 123, 129, 145, 154)

**Interfaces:**
- Consumes: `seed_database(pool, storage=None, embedder=None, clean=False) -> SeedSummary`; `SEED_NAMESPACE` (packages/db/seed.py); `create_pool_from_settings(settings.database)`.
- Produces:
  - `packages.db.fixtures.business.ORDER_82915_ID: UUID`; every `BUSINESS_ORDERS` record gains `placed_at: datetime` and `shipped_at: datetime | None`; every `BUSINESS_TICKETS` record gains `opened_at: datetime` (all tz-aware UTC).
  - `packages.db.fixtures.business_tenants`: `BIZ_HARBOR_ORG_ID`, `BIZ_SUMMIT_ORG_ID`, `BIZ_DELTA_ORG_ID`, `BUSINESS_TENANT_ORGS`, `SHARED_CUSTOMER_EMAIL`, `AMBIGUOUS_CUSTOMER_EMAIL`, customer ids `HARBOR_ALICE_ID`, `HARBOR_BOB_ID`, `SUMMIT_ALICE_ID`, `SUMMIT_CAROL_ID`, `DELTA_ALICE_ID`, `DELTA_BRIGHTLINE_OPS_ID`, `DELTA_BRIGHTLINE_FIN_ID`, and `BUSINESS_TENANT_CUSTOMERS`, `BUSINESS_TENANT_ORDERS`, `BUSINESS_TENANT_TICKETS` (`list[dict[str, Any]]`, same record shape as `business.py`). These are the "≥3-tenant fixtures" that 5.2's contract suite and 5.3's scoping tests seed; `InMemoryBusinessDataProvider(customers=..., orders=..., tickets=...)` takes these lists directly.
  - `async def upsert_business_records(conn: asyncpg.Connection[Any], *, customers: Sequence[Mapping[str, Any]], orders: Sequence[Mapping[str, Any]], tickets: Sequence[Mapping[str, Any]], products: Sequence[Mapping[str, Any]] = (), order_items: Sequence[Mapping[str, Any]] = ()) -> None` in `packages/db/seed.py` (idempotent `ON CONFLICT (id) DO UPDATE`; `placed_at`/`opened_at` fall back to the old relative timestamps when a record omits them, so 5.2/5.3 tests may pass `uuid4()`-org dicts without dates).
  - `async def seed_business_tenant_fixtures(pool: asyncpg.Pool[Any]) -> None` in `packages/db/seed.py` (inserts the three tenant organizations and their records in one transaction; not called by `make seed`).
  - Fixture email key `order_status_inquiry` (archetype `order_status`, Alice → support@acme.com, body contains "What is the status of order 82915?"); its seeded primary message id is `uuid5(SEED_NAMESPACE, "msg:order_status_inquiry:primary")` and thread id `uuid5(SEED_NAMESPACE, "thread:order_status_inquiry")` in `DEMO_ORG_ID`, support mailbox.

Seeded values that later tasks assert (5.6 gate): `ORD-82915`, customer Alice (`CUST_ALICE_ID`), status **`dispatched`**, total `290.00`, placed `2026-09-22T10:15:00Z`, shipped `2026-09-24T16:40:00Z`, items 2 × `SKU-SENSOR-02` @ 95.00 and 4 × `SKU-CABLE-03` @ 25.00. `dispatched` is a single realistic word used by no other demo order (`processing`, `shipped`) and absent from the order-status knowledge chunk, so a draft that states it can only have taken it from `[BUSINESS DATA]`.

- [ ] **Step 1: Write the failing unit tests**

In `tests/unit/test_seed_fixtures.py`, replace the import block (lines 3–24) with:

```python
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from email import message_from_bytes
from typing import Any

from packages.db.fixtures import (
    AMBIGUOUS_CUSTOMER_EMAIL,
    BETA_ORG_ID,
    BIZ_DELTA_ORG_ID,
    BIZ_HARBOR_ORG_ID,
    BIZ_SUMMIT_ORG_ID,
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDER_ITEMS,
    BUSINESS_ORDERS,
    BUSINESS_PRODUCTS,
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_ORGS,
    BUSINESS_TENANT_TICKETS,
    BUSINESS_TICKETS,
    CUST_ALICE_ID,
    CUST_BOB_ID,
    CUST_DANA_ID,
    CUST_EDWARD_ID,
    DEMO_ORG_ID,
    FIXTURE_EMAILS,
    GAMMA_ORG_ID,
    HARBOR_ALICE_ID,
    KNOWLEDGE_DOCS,
    ORDER_9901_ID,
    ORDER_82915_ID,
    PROD_CABLE_ID,
    PROD_SENSOR_ID,
    PROD_WIDGET_ID,
    SHARED_CUSTOMER_EMAIL,
    TENANT_ORGS,
    TICKET_4402_ID,
)
from packages.db.seed import deterministic_embed
```

Append:

```python
def test_alice_order_82915_is_seeded_with_realistic_items() -> None:
    """5.1: ORD-82915 belongs to Alice in the demo org, and its items add up to its total."""
    order = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-82915")
    assert order["id"] == ORDER_82915_ID
    assert order["organization_id"] == DEMO_ORG_ID
    assert order["customer_id"] == CUST_ALICE_ID
    assert order["status"] == "dispatched"
    assert order["total"] == Decimal("290.00")
    assert order["shipped_at"] is not None and order["shipped_at"] > order["placed_at"]

    items = [i for i in BUSINESS_ORDER_ITEMS if i["order_id"] == ORDER_82915_ID]
    assert {(i["product_id"], i["quantity"]) for i in items} == {
        (PROD_SENSOR_ID, 2),
        (PROD_CABLE_ID, 4),
    }
    prices = {p["id"]: p["price"] for p in BUSINESS_PRODUCTS}
    for item in items:
        assert item["unit_price"] == prices[item["product_id"]]
        assert item["organization_id"] == DEMO_ORG_ID


def test_every_demo_order_total_equals_its_items() -> None:
    for order in BUSINESS_ORDERS:
        items = [i for i in BUSINESS_ORDER_ITEMS if i["order_id"] == order["id"]]
        assert items, order["order_number"]
        assert sum(i["quantity"] * i["unit_price"] for i in items) == order["total"]


def test_alice_order_status_fixture_email() -> None:
    """5.1: Alice asks for order 82915 in a non-billing email (support mailbox)."""
    email = next(e for e in FIXTURE_EMAILS if e.key == "order_status_inquiry")
    assert email.sender_email == "alice.smith@clientcorp.com"
    assert "What is the status of order 82915?" in email.body_text
    assert email.category == "order_status"
    assert email.identifiers == ["ORD-82915"]
    assert email.reply_required is True


def test_edward_still_asks_about_danas_order() -> None:
    """R13.4 cross-customer case: Edward's email cites ORD-9901, which is Dana's."""
    email = next(e for e in FIXTURE_EMAILS if e.key == "identifier_order_ticket")
    assert email.sender_email == "edward.norton@fightclub.org"
    assert "ORD-9901" in email.identifiers
    order = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-9901")
    assert order["customer_id"] == CUST_DANA_ID != CUST_EDWARD_ID


def _assert_dated(
    orders: Sequence[Mapping[str, Any]], tickets: Sequence[Mapping[str, Any]]
) -> None:
    for order in orders:
        placed = order["placed_at"]
        assert isinstance(placed, datetime) and placed.tzinfo is not None
    for ticket in tickets:
        opened = ticket["opened_at"]
        assert isinstance(opened, datetime) and opened.tzinfo is not None


def test_snapshot_ordering_is_deterministic_per_customer() -> None:
    """design §5.4 orders snapshots by placed_at / opened_at DESC: no ties within a customer."""
    for orders, tickets in (
        (BUSINESS_ORDERS, BUSINESS_TICKETS),
        (BUSINESS_TENANT_ORDERS, BUSINESS_TENANT_TICKETS),
    ):
        _assert_dated(orders, tickets)
        order_keys = [(o["organization_id"], o["customer_id"], o["placed_at"]) for o in orders]
        assert len(order_keys) == len(set(order_keys))
        ticket_keys = [(t["organization_id"], t["customer_id"], t["opened_at"]) for t in tickets]
        assert len(ticket_keys) == len(set(ticket_keys))


def test_business_tenant_fixtures_overlap_across_three_tenants() -> None:
    """CLAUDE.md §8: ≥3 tenants with overlapping customer emails and order numbers."""
    orgs = {o["id"] for o in BUSINESS_TENANT_ORGS}
    assert orgs == {BIZ_HARBOR_ORG_ID, BIZ_SUMMIT_ORG_ID, BIZ_DELTA_ORG_ID}
    assert orgs.isdisjoint({DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID})

    shared = [c for c in BUSINESS_TENANT_CUSTOMERS if c["email"].lower() == SHARED_CUSTOMER_EMAIL]
    assert {c["organization_id"] for c in shared} == orgs
    assert any(c["email"] != SHARED_CUSTOMER_EMAIL for c in shared)  # a case variant

    same_number = [o for o in BUSINESS_TENANT_ORDERS if o["order_number"] == "ORD-82915"]
    assert {o["organization_id"] for o in same_number} == orgs
    assert len({o["status"] for o in same_number}) == 3
    assert len({o["customer_id"] for o in same_number}) == 3

    same_ticket = [t for t in BUSINESS_TENANT_TICKETS if t["ticket_number"] == "TICK-4402"]
    assert {t["organization_id"] for t in same_ticket} == orgs

    ambiguous = [
        c
        for c in BUSINESS_TENANT_CUSTOMERS
        if c["organization_id"] == BIZ_DELTA_ORG_ID and c["email"] == AMBIGUOUS_CUSTOMER_EMAIL
    ]
    assert len(ambiguous) == 2


def test_business_tenant_fixtures_cover_snapshot_edges() -> None:
    """Harbor Alice has more orders than the default snapshot (3) and closed/resolved tickets."""
    alice_orders = [o for o in BUSINESS_TENANT_ORDERS if o["customer_id"] == HARBOR_ALICE_ID]
    assert len(alice_orders) > 3
    alice_ticket_statuses = {
        t["status"] for t in BUSINESS_TENANT_TICKETS if t["customer_id"] == HARBOR_ALICE_ID
    }
    assert {"closed", "resolved", "open"} <= alice_ticket_statuses

    with_rows = {o["customer_id"] for o in BUSINESS_TENANT_ORDERS} | {
        t["customer_id"] for t in BUSINESS_TENANT_TICKETS
    }
    assert any(c["id"] not in with_rows for c in BUSINESS_TENANT_CUSTOMERS)  # snapshot NOT_FOUND


def test_business_tenant_records_stay_inside_their_tenant() -> None:
    customer_org = {c["id"]: c["organization_id"] for c in BUSINESS_TENANT_CUSTOMERS}
    for record in (*BUSINESS_TENANT_ORDERS, *BUSINESS_TENANT_TICKETS):
        assert customer_org[record["customer_id"]] == record["organization_id"]
    records = (*BUSINESS_TENANT_CUSTOMERS, *BUSINESS_TENANT_ORDERS, *BUSINESS_TENANT_TICKETS)
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids))
```



Run: `uv run pytest tests/unit/test_seed_fixtures.py -v`
Expected: FAIL during collection with `ImportError: cannot import name 'AMBIGUOUS_CUSTOMER_EMAIL' from 'packages.db.fixtures'`.

- [ ] **Step 2: Write the failing integration test**

Create `tests/integration/test_business_seed.py`:

```python
"""Business seed data on real Postgres: ORD-82915 for Alice and the 3-tenant fixtures (5.1).

Requirements: R13.1, R5.9. Runs in the isolated rag_email_test database (tests/integration
conftest). Every query is scoped by organization_id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import uuid5

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import (
    AMBIGUOUS_CUSTOMER_EMAIL,
    BIZ_DELTA_ORG_ID,
    BUSINESS_ORDERS,
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_ORGS,
    BUSINESS_TENANT_TICKETS,
    CUST_ALICE_ID,
    DEMO_ORG_ID,
    ORDER_82915_ID,
    SHARED_CUSTOMER_EMAIL,
)
from packages.db.seed import SEED_NAMESPACE, seed_business_tenant_fixtures, seed_database


@pytest.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool[Any]]:
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def test_seed_puts_order_82915_with_items_on_alice(db_pool: asyncpg.Pool[Any]) -> None:
    await seed_database(pool=db_pool, clean=True)
    expected = next(o for o in BUSINESS_ORDERS if o["id"] == ORDER_82915_ID)

    async with db_pool.acquire() as conn:
        order = await conn.fetchrow(
            'SELECT o.id, o.status, o.total, o.placed_at, o.shipped_at, c.email FROM "order" o '
            "JOIN customer c ON c.id = o.customer_id AND c.organization_id = o.organization_id "
            "WHERE o.organization_id = $1 AND o.order_number = $2",
            DEMO_ORG_ID,
            "ORD-82915",
        )
        assert order is not None
        assert order["id"] == ORDER_82915_ID
        assert order["email"] == "alice.smith@clientcorp.com"
        assert order["status"] == "dispatched"
        assert order["placed_at"] == expected["placed_at"]
        assert order["shipped_at"] == expected["shipped_at"]

        items = await conn.fetch(
            "SELECT p.sku, i.quantity, i.unit_price FROM order_item i "
            "JOIN product p ON p.id = i.product_id AND p.organization_id = i.organization_id "
            "WHERE i.organization_id = $1 AND i.order_id = $2 ORDER BY p.sku",
            DEMO_ORG_ID,
            ORDER_82915_ID,
        )
        assert [(r["sku"], r["quantity"]) for r in items] == [
            ("SKU-CABLE-03", 4),
            ("SKU-SENSOR-02", 2),
        ]
        total = sum(r["quantity"] * r["unit_price"] for r in items)
        assert total == order["total"] == Decimal("290.00")

        newest_first = await conn.fetch(
            'SELECT order_number FROM "order" WHERE organization_id = $1 AND customer_id = $2 '
            "ORDER BY placed_at DESC",
            DEMO_ORG_ID,
            CUST_ALICE_ID,
        )
        assert [r["order_number"] for r in newest_first] == ["ORD-82915", "ORD-8820"]

        owner = await conn.fetchval(
            'SELECT c.email FROM "order" o JOIN customer c '
            "ON c.id = o.customer_id AND c.organization_id = o.organization_id "
            "WHERE o.organization_id = $1 AND o.order_number = $2",
            DEMO_ORG_ID,
            "ORD-9901",
        )
        assert owner == "dana.scully@fbi.gov"  # Edward's email cites Dana's order (R13.4)


async def test_seed_stores_alices_order_status_email(db_pool: asyncpg.Pool[Any]) -> None:
    await seed_database(pool=db_pool, clean=True)
    message = await db_pool.fetchrow(
        "SELECT sender_email, thread_id, body_text FROM email_message "
        "WHERE organization_id = $1 AND id = $2",
        DEMO_ORG_ID,
        uuid5(SEED_NAMESPACE, "msg:order_status_inquiry:primary"),
    )
    assert message is not None
    assert message["sender_email"] == "alice.smith@clientcorp.com"
    assert message["thread_id"] == uuid5(SEED_NAMESPACE, "thread:order_status_inquiry")
    assert "What is the status of order 82915?" in message["body_text"]


async def test_business_tenant_fixtures_load_into_three_tenants(
    db_pool: asyncpg.Pool[Any],
) -> None:
    await seed_business_tenant_fixtures(db_pool)
    await seed_business_tenant_fixtures(db_pool)  # idempotent
    orgs = [o["id"] for o in BUSINESS_TENANT_ORGS]

    async with db_pool.acquire() as conn:
        shared = await conn.fetch(
            "SELECT organization_id, count(*) AS n FROM customer "
            "WHERE organization_id = ANY($1::uuid[]) AND lower(email) = lower($2) "
            "GROUP BY organization_id",
            orgs,
            SHARED_CUSTOMER_EMAIL,
        )
        assert {r["organization_id"]: r["n"] for r in shared} == dict.fromkeys(orgs, 1)

        same_number = await conn.fetch(
            'SELECT organization_id, customer_id, status FROM "order" '
            "WHERE organization_id = ANY($1::uuid[]) AND order_number = $2",
            orgs,
            "ORD-82915",
        )
        assert {r["organization_id"] for r in same_number} == set(orgs)
        assert len({r["status"] for r in same_number}) == 3

        ambiguous = await conn.fetchval(
            "SELECT count(*) FROM customer WHERE organization_id = $1 AND lower(email) = lower($2)",
            BIZ_DELTA_ORG_ID,
            AMBIGUOUS_CUSTOMER_EMAIL,
        )
        assert ambiguous == 2

        crossed = await conn.fetchval(
            'SELECT count(*) FROM "order" o JOIN customer c ON c.id = o.customer_id '
            "WHERE o.organization_id = ANY($1::uuid[]) AND c.organization_id <> o.organization_id",
            orgs,
        )
        assert crossed == 0

        counts = await conn.fetchrow(
            "SELECT "
            "(SELECT count(*) FROM customer WHERE organization_id = ANY($1::uuid[])) AS customers, "
            '(SELECT count(*) FROM "order" WHERE organization_id = ANY($1::uuid[])) AS orders, '
            "(SELECT count(*) FROM ticket WHERE organization_id = ANY($1::uuid[])) AS tickets",
            orgs,
        )
        assert counts is not None
        assert (counts["customers"], counts["orders"], counts["tickets"]) == (
            len(BUSINESS_TENANT_CUSTOMERS),
            len(BUSINESS_TENANT_ORDERS),
            len(BUSINESS_TENANT_TICKETS),
        )
```

(The count assertion is scoped to the three dedicated fixture tenants, which only this fixture set writes, so it is not a global count.)

Run: `uv run pytest tests/integration/test_business_seed.py -v`
Expected: FAIL during collection with `ImportError: cannot import name 'AMBIGUOUS_CUSTOMER_EMAIL' from 'packages.db.fixtures'`.

- [ ] **Step 3: Update the seed-loader counts (fail until Step 7)**

In `tests/integration/test_seed_loader.py`:
- lines 59–61: `summary.orders_count == 3`, `summary.order_items_count == 4`, `summary.tickets_count == 2` (unchanged);
- lines 65–67: `summary.threads_count == 7`, `summary.messages_count == 10`, `summary.mime_objects_uploaded == 10`;
- line 105 (`order_count`): `assert order_count == 3`;
- line 123 (`thread_count`): `assert thread_count == 7`;
- line 129 (`msg_count`): `assert msg_count == 10`;
- lines 145 and 154 (idempotency): `summary.messages_count == 10` and `msg_count == 10`.

(Threads: 6 fixtures + `order_status_inquiry` = 7. Messages and MIME objects: 7 primaries + 3 thread-history turns = 10. Order items: 2 + 2 for ORD-82915 = 4.)

- [ ] **Step 4: Implement the demo business fixtures**

Replace `packages/db/fixtures/business.py` with:

```python
"""Business CRM and ERP record fixtures (R5.9, R13.1).

Defines realistic relational entities (customers, products, orders, items, tickets)
matching identifiers cited in fixture emails. Orders carry `placed_at` / `shipped_at` and
tickets `opened_at` (tz-aware UTC), so "most recent first" snapshots are deterministic
for every customer (design §5.4).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.db.fixtures.knowledge import DEMO_ORG_ID

# Stable customer UUIDs
CUST_ALICE_ID = UUID("10000000-0000-0000-0000-000000000001")
CUST_BOB_ID = UUID("10000000-0000-0000-0000-000000000002")
CUST_DANA_ID = UUID("10000000-0000-0000-0000-000000000003")
CUST_EDWARD_ID = UUID("10000000-0000-0000-0000-000000000004")

# Stable product UUIDs
PROD_WIDGET_ID = UUID("20000000-0000-0000-0000-000000000001")
PROD_SENSOR_ID = UUID("20000000-0000-0000-0000-000000000002")
PROD_CABLE_ID = UUID("20000000-0000-0000-0000-000000000003")

# Stable order UUIDs
ORDER_9901_ID = UUID("30000000-0000-0000-0000-000000000001")
ORDER_8820_ID = UUID("30000000-0000-0000-0000-000000000002")
ORDER_82915_ID = UUID("30000000-0000-0000-0000-000000000003")

# Stable ticket UUIDs
TICKET_4402_ID = UUID("40000000-0000-0000-0000-000000000001")
TICKET_1011_ID = UUID("40000000-0000-0000-0000-000000000002")

BUSINESS_CUSTOMERS: list[dict[str, Any]] = [
    {
        "id": CUST_ALICE_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "alice.smith@clientcorp.com",
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": CUST_BOB_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "bob.jones@enterprises.org",
        "name": "Bob Jones",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": CUST_DANA_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "dana.scully@fbi.gov",
        "name": "Dana Scully",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": CUST_EDWARD_ID,
        "organization_id": DEMO_ORG_ID,
        "email": "edward.norton@fightclub.org",
        "name": "Edward Norton",
        "account_status": "active",
        "tier": "standard",
    },
]

BUSINESS_PRODUCTS: list[dict[str, Any]] = [
    {
        "id": PROD_WIDGET_ID,
        "organization_id": DEMO_ORG_ID,
        "sku": "SKU-WIDGET-01",
        "name": "Acme Enterprise Widget",
        "price": Decimal("450.00"),
        "status": "active",
    },
    {
        "id": PROD_SENSOR_ID,
        "organization_id": DEMO_ORG_ID,
        "sku": "SKU-SENSOR-02",
        "name": "Acme Pro Sensor",
        "price": Decimal("95.00"),
        "status": "active",
    },
    {
        "id": PROD_CABLE_ID,
        "organization_id": DEMO_ORG_ID,
        "sku": "SKU-CABLE-03",
        "name": "Industrial Connector Cable",
        "price": Decimal("25.00"),
        "status": "active",
    },
]

BUSINESS_ORDERS: list[dict[str, Any]] = [
    {
        "id": ORDER_9901_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_DANA_ID,
        "order_number": "ORD-9901",
        "status": "processing",
        "total": Decimal("1350.00"),
        "placed_at": datetime(2026, 9, 10, 9, 30, tzinfo=UTC),
        "shipped_at": None,
    },
    {
        "id": ORDER_8820_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_ALICE_ID,
        "order_number": "ORD-8820",
        "status": "shipped",
        "total": Decimal("450.00"),
        "placed_at": datetime(2026, 8, 28, 14, 5, tzinfo=UTC),
        "shipped_at": datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
    },
    # The Phase 5 gate order: Alice asks "What is the status of order 82915?" (task 5.1).
    {
        "id": ORDER_82915_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_ALICE_ID,
        "order_number": "ORD-82915",
        "status": "dispatched",
        "total": Decimal("290.00"),
        "placed_at": datetime(2026, 9, 22, 10, 15, tzinfo=UTC),
        "shipped_at": datetime(2026, 9, 24, 16, 40, tzinfo=UTC),
    },
]

BUSINESS_ORDER_ITEMS: list[dict[str, Any]] = [
    {
        "id": UUID("35000000-0000-0000-0000-000000000001"),
        "organization_id": DEMO_ORG_ID,
        "order_id": ORDER_9901_ID,
        "product_id": PROD_WIDGET_ID,
        "quantity": 3,
        "unit_price": Decimal("450.00"),
    },
    {
        "id": UUID("35000000-0000-0000-0000-000000000002"),
        "organization_id": DEMO_ORG_ID,
        "order_id": ORDER_8820_ID,
        "product_id": PROD_WIDGET_ID,
        "quantity": 1,
        "unit_price": Decimal("450.00"),
    },
    {
        "id": UUID("35000000-0000-0000-0000-000000000003"),
        "organization_id": DEMO_ORG_ID,
        "order_id": ORDER_82915_ID,
        "product_id": PROD_SENSOR_ID,
        "quantity": 2,
        "unit_price": Decimal("95.00"),
    },
    {
        "id": UUID("35000000-0000-0000-0000-000000000004"),
        "organization_id": DEMO_ORG_ID,
        "order_id": ORDER_82915_ID,
        "product_id": PROD_CABLE_ID,
        "quantity": 4,
        "unit_price": Decimal("25.00"),
    },
]

BUSINESS_TICKETS: list[dict[str, Any]] = [
    {
        "id": TICKET_4402_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_EDWARD_ID,
        "ticket_number": "TICK-4402",
        "subject": "Replacement shipment inquiry for delayed order",
        "status": "open",
        "priority": "high",
        "opened_at": datetime(2026, 9, 25, 11, 0, tzinfo=UTC),
    },
    {
        "id": TICKET_1011_ID,
        "organization_id": DEMO_ORG_ID,
        "customer_id": CUST_ALICE_ID,
        "ticket_number": "TICK-1011",
        "subject": "PDF export crash investigation",
        "status": "pending",
        "priority": "critical",
        "opened_at": datetime(2026, 9, 18, 9, 0, tzinfo=UTC),
    },
]
```

- [ ] **Step 5: Implement the 3-tenant business fixtures**

Create `packages/db/fixtures/business_tenants.py`:

```python
"""Multi-tenant business fixtures for the business-data provider tests (R13.1, R13.4).

Three dedicated tenants, separate from the demo seed so `make seed` and its counts stay
unchanged, with deliberately overlapping content (CLAUDE.md §8):

- the same customer email in all three tenants (letter case differs in Summit);
- the same order number ORD-82915 and ticket TICK-4402 in all three tenants, each owned by
  a different customer with a different status;
- two customers sharing one email inside Delta (AMBIGUOUS_CUSTOMER, design §5.4);
- a Harbor customer with more orders than the default snapshot (3) and with closed and
  resolved tickets, and a Delta customer with no orders or tickets (snapshot NOT_FOUND).

Records have the same shape as packages/db/fixtures/business.py. Integration tests load them
with `packages.db.seed.seed_business_tenant_fixtures`; unit tests pass the lists to an
in-memory provider. `make seed` does not load them.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

BIZ_HARBOR_ORG_ID = UUID("00000000-0000-0000-0000-0000000000a1")  # Harbor Supply Co.
BIZ_SUMMIT_ORG_ID = UUID("00000000-0000-0000-0000-0000000000a2")  # Summit Retail
BIZ_DELTA_ORG_ID = UUID("00000000-0000-0000-0000-0000000000a3")  # Delta Parts

BUSINESS_TENANT_ORGS: list[dict[str, Any]] = [
    {
        "id": BIZ_HARBOR_ORG_ID,
        "name": "Harbor Supply Co.",
        "settings": {"tier": "standard", "domain": "harbor-supply.example"},
    },
    {
        "id": BIZ_SUMMIT_ORG_ID,
        "name": "Summit Retail",
        "settings": {"tier": "standard", "domain": "summit-retail.example"},
    },
    {
        "id": BIZ_DELTA_ORG_ID,
        "name": "Delta Parts",
        "settings": {"tier": "standard", "domain": "delta-parts.example"},
    },
]

SHARED_CUSTOMER_EMAIL = "alice.smith@clientcorp.com"
"""A customer of all three tenants (the demo org's Alice uses the same address)."""

AMBIGUOUS_CUSTOMER_EMAIL = "orders@brightline-hvac.com"
"""Two Delta customer records share this address, so resolution is AMBIGUOUS_CUSTOMER."""

HARBOR_ALICE_ID = UUID("11000000-0000-0000-0000-000000000001")
HARBOR_BOB_ID = UUID("11000000-0000-0000-0000-000000000002")
SUMMIT_ALICE_ID = UUID("11000000-0000-0000-0000-000000000003")
SUMMIT_CAROL_ID = UUID("11000000-0000-0000-0000-000000000004")
DELTA_ALICE_ID = UUID("11000000-0000-0000-0000-000000000005")
DELTA_BRIGHTLINE_OPS_ID = UUID("11000000-0000-0000-0000-000000000006")
DELTA_BRIGHTLINE_FIN_ID = UUID("11000000-0000-0000-0000-000000000007")

BUSINESS_TENANT_CUSTOMERS: list[dict[str, Any]] = [
    {
        "id": HARBOR_ALICE_ID,
        "organization_id": BIZ_HARBOR_ORG_ID,
        "email": SHARED_CUSTOMER_EMAIL,
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": HARBOR_BOB_ID,
        "organization_id": BIZ_HARBOR_ORG_ID,
        "email": "bob.jones@enterprises.org",
        "name": "Bob Jones",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": SUMMIT_ALICE_ID,
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "email": "Alice.Smith@ClientCorp.com",
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": SUMMIT_CAROL_ID,
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "email": "carol.white@summit-retail.example",
        "name": "Carol White",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": DELTA_ALICE_ID,
        "organization_id": BIZ_DELTA_ORG_ID,
        "email": SHARED_CUSTOMER_EMAIL,
        "name": "Alice Smith",
        "account_status": "active",
        "tier": "standard",
    },
    {
        "id": DELTA_BRIGHTLINE_OPS_ID,
        "organization_id": BIZ_DELTA_ORG_ID,
        "email": AMBIGUOUS_CUSTOMER_EMAIL,
        "name": "Brightline HVAC Operations",
        "account_status": "active",
        "tier": "enterprise",
    },
    {
        "id": DELTA_BRIGHTLINE_FIN_ID,
        "organization_id": BIZ_DELTA_ORG_ID,
        "email": AMBIGUOUS_CUSTOMER_EMAIL,
        "name": "Brightline HVAC Finance",
        "account_status": "active",
        "tier": "enterprise",
    },
]

BUSINESS_TENANT_ORDERS: list[dict[str, Any]] = [
    {
        "id": UUID("31000000-0000-0000-0000-000000000001"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-82915",
        "status": "shipped",
        "total": Decimal("640.00"),
        "placed_at": datetime(2026, 9, 21, 8, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000002"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-7003",
        "status": "delivered",
        "total": Decimal("120.00"),
        "placed_at": datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 9, 6, 9, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000003"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-7002",
        "status": "cancelled",
        "total": Decimal("75.50"),
        "placed_at": datetime(2026, 8, 18, 15, 30, tzinfo=UTC),
        "shipped_at": None,
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000004"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "order_number": "ORD-7001",
        "status": "delivered",
        "total": Decimal("310.00"),
        "placed_at": datetime(2026, 7, 30, 11, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 8, 1, 7, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000005"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_BOB_ID,
        "order_number": "ORD-9901",
        "status": "processing",
        "total": Decimal("980.00"),
        "placed_at": datetime(2026, 9, 26, 14, 0, tzinfo=UTC),
        "shipped_at": None,
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000006"),
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "customer_id": SUMMIT_ALICE_ID,
        "order_number": "ORD-82915",
        "status": "processing",
        "total": Decimal("215.00"),
        "placed_at": datetime(2026, 9, 24, 9, 45, tzinfo=UTC),
        "shipped_at": None,
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000007"),
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "customer_id": SUMMIT_CAROL_ID,
        "order_number": "ORD-9901",
        "status": "delivered",
        "total": Decimal("45.00"),
        "placed_at": datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
        "shipped_at": datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
    },
    {
        "id": UUID("31000000-0000-0000-0000-000000000008"),
        "organization_id": BIZ_DELTA_ORG_ID,
        "customer_id": DELTA_BRIGHTLINE_OPS_ID,
        "order_number": "ORD-82915",
        "status": "on_hold",
        "total": Decimal("1875.00"),
        "placed_at": datetime(2026, 9, 19, 16, 20, tzinfo=UTC),
        "shipped_at": None,
    },
]

BUSINESS_TENANT_TICKETS: list[dict[str, Any]] = [
    {
        "id": UUID("41000000-0000-0000-0000-000000000001"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "ticket_number": "TICK-4402",
        "subject": "Courier has not scanned ORD-82915",
        "status": "open",
        "priority": "high",
        "opened_at": datetime(2026, 9, 25, 8, 30, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000002"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "ticket_number": "TICK-5001",
        "subject": "Invoice copy request",
        "status": "closed",
        "priority": "normal",
        "opened_at": datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000003"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_ALICE_ID,
        "ticket_number": "TICK-5002",
        "subject": "Damaged packaging on ORD-7001",
        "status": "resolved",
        "priority": "low",
        "opened_at": datetime(2026, 8, 20, 9, 15, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000004"),
        "organization_id": BIZ_HARBOR_ORG_ID,
        "customer_id": HARBOR_BOB_ID,
        "ticket_number": "TICK-5003",
        "subject": "Change delivery address for ORD-9901",
        "status": "open",
        "priority": "normal",
        "opened_at": datetime(2026, 9, 26, 15, 0, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000005"),
        "organization_id": BIZ_SUMMIT_ORG_ID,
        "customer_id": SUMMIT_ALICE_ID,
        "ticket_number": "TICK-4402",
        "subject": "Gift wrapping for ORD-82915",
        "status": "pending",
        "priority": "normal",
        "opened_at": datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
    },
    {
        "id": UUID("41000000-0000-0000-0000-000000000006"),
        "organization_id": BIZ_DELTA_ORG_ID,
        "customer_id": DELTA_BRIGHTLINE_FIN_ID,
        "ticket_number": "TICK-4402",
        "subject": "Credit hold on ORD-82915",
        "status": "open",
        "priority": "high",
        "opened_at": datetime(2026, 9, 22, 13, 0, tzinfo=UTC),
    },
]
```

- [ ] **Step 6: Add Alice's fixture email and the exports**

In `packages/db/fixtures/emails.py`, change the module docstring (lines 1–10) to:

```python
"""Standard fixture emails covering core operational archetypes (R5.9).

Provides realistic, RFC 822 compliant email test cases across 7 key archetypes:
1. support: Technical bug report requiring assistance and investigation.
2. billing: Invoice and payment inquiry citing invoice identifier.
3. newsletter: Marketing email with List-Unsubscribe header (zero reply required).
4. auto_reply: Out-of-office automated responder (zero reply required).
5. long_thread: Multi-turn thread structure with In-Reply-To and References.
6. identifier_bearing: Order and ticket inquiry citing explicit business IDs.
7. order_status: A customer asking for her own order's status by bare number (R13, task 5.1).
"""
```

Insert before the closing `]` of `FIXTURE_EMAILS` (after the `identifier_order_ticket` entry ending at line 222):

```python
    # 7. Order-status question by bare order number (Phase 5 gate email, R13)
    EmailFixture(
        key="order_status_inquiry",
        category="order_status",
        subject="Order status question",
        sender_email="alice.smith@clientcorp.com",
        sender_name="Alice Smith",
        recipients=["support@acme.com"],
        body_text=(
            "Hi,\n\n"
            "What is the status of order 82915? I placed it last week and have not "
            "received a tracking update yet.\n\n"
            "Thanks,\n"
            "Alice"
        ),
        identifiers=["ORD-82915"],
        reply_required=True,
        workflow_hint="ai_generate",
    ),
```

In `packages/db/fixtures/__init__.py`, add `ORDER_82915_ID` to the `from packages.db.fixtures.business import (...)` list (after `ORDER_9901_ID`: ruff's isort orders the numbers naturally, `ORDER_8820_ID`, `ORDER_9901_ID`, `ORDER_82915_ID`), add a new import block after it:

```python
from packages.db.fixtures.business_tenants import (
    AMBIGUOUS_CUSTOMER_EMAIL,
    BIZ_DELTA_ORG_ID,
    BIZ_HARBOR_ORG_ID,
    BIZ_SUMMIT_ORG_ID,
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_ORGS,
    BUSINESS_TENANT_TICKETS,
    DELTA_ALICE_ID,
    DELTA_BRIGHTLINE_FIN_ID,
    DELTA_BRIGHTLINE_OPS_ID,
    HARBOR_ALICE_ID,
    HARBOR_BOB_ID,
    SHARED_CUSTOMER_EMAIL,
    SUMMIT_ALICE_ID,
    SUMMIT_CAROL_ID,
)
```

and replace `__all__` with:

```python
__all__ = [
    "AMBIGUOUS_CUSTOMER_EMAIL",
    "BETA_ORG_ID",
    "BIZ_DELTA_ORG_ID",
    "BIZ_HARBOR_ORG_ID",
    "BIZ_SUMMIT_ORG_ID",
    "BUSINESS_CUSTOMERS",
    "BUSINESS_ORDERS",
    "BUSINESS_ORDER_ITEMS",
    "BUSINESS_PRODUCTS",
    "BUSINESS_TENANT_CUSTOMERS",
    "BUSINESS_TENANT_ORDERS",
    "BUSINESS_TENANT_ORGS",
    "BUSINESS_TENANT_TICKETS",
    "BUSINESS_TICKETS",
    "CUST_ALICE_ID",
    "CUST_BOB_ID",
    "CUST_DANA_ID",
    "CUST_EDWARD_ID",
    "ChunkFixture",
    "DELTA_ALICE_ID",
    "DELTA_BRIGHTLINE_FIN_ID",
    "DELTA_BRIGHTLINE_OPS_ID",
    "DEMO_ORG_ID",
    "EmailFixture",
    "FIXTURE_EMAILS",
    "GAMMA_ORG_ID",
    "HARBOR_ALICE_ID",
    "HARBOR_BOB_ID",
    "KNOWLEDGE_DOCS",
    "KnowledgeDocFixture",
    "ORDER_82915_ID",
    "ORDER_8820_ID",
    "ORDER_9901_ID",
    "PROD_CABLE_ID",
    "PROD_SENSOR_ID",
    "PROD_WIDGET_ID",
    "SHARED_CUSTOMER_EMAIL",
    "SUMMIT_ALICE_ID",
    "SUMMIT_CAROL_ID",
    "TENANT_ORGS",
    "TICKET_1011_ID",
    "TICKET_4402_ID",
    "ThreadMessage",
    "compute_chunk_id",
    "compute_doc_id",
]
```

(Run `uv run ruff check --fix packages/db/fixtures/__init__.py` if RUF022-style sorting differs; the repo's `select` does not include RUF, so the order above only needs to stay readable.)

- [ ] **Step 7: Make the seed insert dates and expose the business upsert**

In `packages/db/seed.py`:

Add `from collections.abc import Mapping, Sequence` to the stdlib imports (after `import math`), and add after the `from packages.db.fixtures.business import (...)` block:

```python
from packages.db.fixtures.business_tenants import (
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_ORGS,
    BUSINESS_TENANT_TICKETS,
)
```

Add these two functions directly above `async def seed_database(`:

```python
async def upsert_business_records(
    conn: asyncpg.Connection[Any],
    *,
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    tickets: Sequence[Mapping[str, Any]],
    products: Sequence[Mapping[str, Any]] = (),
    order_items: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Upsert business CRM/ERP rows by id (R13.1). Idempotent; the caller owns the transaction.

    Records use the packages/db/fixtures/business.py shape. An order without `placed_at` is
    placed two days ago and a ticket without `opened_at` was opened a day ago, as before.
    """
    for cust in customers:
        await conn.execute(
            """
            INSERT INTO customer (id, organization_id, email, name, account_status, tier)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                account_status = EXCLUDED.account_status,
                tier = EXCLUDED.tier
            """,
            cust["id"],
            cust["organization_id"],
            cust["email"],
            cust["name"],
            cust.get("account_status", "active"),
            cust.get("tier", "standard"),
        )

    for prod in products:
        await conn.execute(
            """
            INSERT INTO product (id, organization_id, sku, name, price, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id) DO UPDATE SET
                sku = EXCLUDED.sku,
                name = EXCLUDED.name,
                price = EXCLUDED.price,
                status = EXCLUDED.status
            """,
            prod["id"],
            prod["organization_id"],
            prod["sku"],
            prod["name"],
            prod["price"],
            prod["status"],
        )

    for order in orders:
        await conn.execute(
            """
            INSERT INTO "order" (
                id, organization_id, customer_id, order_number, status, total,
                placed_at, shipped_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                COALESCE($7::timestamptz, now() - INTERVAL '2 days'), $8::timestamptz
            )
            ON CONFLICT (id) DO UPDATE SET
                order_number = EXCLUDED.order_number,
                status = EXCLUDED.status,
                total = EXCLUDED.total,
                placed_at = EXCLUDED.placed_at,
                shipped_at = EXCLUDED.shipped_at
            """,
            order["id"],
            order["organization_id"],
            order["customer_id"],
            order["order_number"],
            order["status"],
            order["total"],
            order.get("placed_at"),
            order.get("shipped_at"),
        )

    for item in order_items:
        await conn.execute(
            """
            INSERT INTO order_item (
                id, organization_id, order_id, product_id, quantity, unit_price
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id) DO NOTHING
            """,
            item["id"],
            item["organization_id"],
            item["order_id"],
            item["product_id"],
            item["quantity"],
            item["unit_price"],
        )

    for ticket in tickets:
        await conn.execute(
            """
            INSERT INTO ticket (
                id, organization_id, customer_id, ticket_number, status,
                priority, subject, opened_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                COALESCE($8::timestamptz, now() - INTERVAL '1 day')
            )
            ON CONFLICT (id) DO UPDATE SET
                ticket_number = EXCLUDED.ticket_number,
                status = EXCLUDED.status,
                priority = EXCLUDED.priority,
                subject = EXCLUDED.subject,
                opened_at = EXCLUDED.opened_at
            """,
            ticket["id"],
            ticket["organization_id"],
            ticket["customer_id"],
            ticket["ticket_number"],
            ticket["status"],
            ticket["priority"],
            ticket["subject"],
            ticket.get("opened_at"),
        )


async def seed_business_tenant_fixtures(pool: asyncpg.Pool[Any]) -> None:
    """Load the 3-tenant business test fixtures (CLAUDE.md §8, R13.4). Idempotent.

    Test-only: `make seed` does not call this, so the demo seed and its counts stay as they are.
    """
    async with pool.acquire() as conn, conn.transaction():
        for org in BUSINESS_TENANT_ORGS:
            await conn.execute(
                """
                INSERT INTO organization (id, name, settings)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    settings = EXCLUDED.settings
                """,
                org["id"],
                org["name"],
                json.dumps(org["settings"]),
            )
        await upsert_business_records(
            conn,
            customers=BUSINESS_TENANT_CUSTOMERS,
            orders=BUSINESS_TENANT_ORDERS,
            tickets=BUSINESS_TENANT_TICKETS,
        )
```

Replace the whole business block inside `seed_database` — from the line `# 3. Seed Business CRM & ERP Records` (line 205) through the closing `)` of the `for ticket in BUSINESS_TICKETS:` loop (line 300) — with:

```python
        # 3. Seed Business CRM & ERP Records
        logger.info("Seeding business records (customers, products, orders, tickets)...")
        await upsert_business_records(
            conn,
            customers=BUSINESS_CUSTOMERS,
            products=BUSINESS_PRODUCTS,
            orders=BUSINESS_ORDERS,
            order_items=BUSINESS_ORDER_ITEMS,
            tickets=BUSINESS_TICKETS,
        )
```

(The `# 4. Seed Knowledge Documents...` block follows unchanged.)

- [ ] **Step 8: Run the tests to pass**

Run: `uv run pytest tests/unit/test_seed_fixtures.py -v`
Expected: PASS.
Run: `uv run pytest tests/integration/test_business_seed.py tests/integration/test_seed_loader.py -v`
Expected: PASS (needs the postgres, rabbitmq and minio containers up; the package conftest recreates `rag_email_test`).
Run: `uv run ruff check --fix packages/db tests/unit/test_seed_fixtures.py tests/integration/test_business_seed.py tests/integration/test_seed_loader.py && uv run ruff format packages/db tests/unit/test_seed_fixtures.py tests/integration/test_business_seed.py tests/integration/test_seed_loader.py`
Expected: no remaining findings (the import blocks above are already in ruff's natural order; this catches any drift before the commit).
Run: `make ci`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add packages/db/fixtures/business.py packages/db/fixtures/business_tenants.py packages/db/fixtures/emails.py packages/db/fixtures/__init__.py packages/db/seed.py tests/unit/test_seed_fixtures.py tests/integration/test_business_seed.py tests/integration/test_seed_loader.py
git commit -m "$(cat <<'EOF'
feat(business): seed ORD-82915 for Alice, her order-status email and 3-tenant business fixtures [task 5.1] [R13.1, R5.9]

Alice now owns ORD-82915 (dispatched, 290.00, two order_item rows) and asks "What is
the status of order 82915?" in a new order_status fixture email. Orders and tickets
carry fixed placed_at/shipped_at/opened_at, which the seed writes, so newest-first
snapshots are deterministic. A separate 3-tenant fixture set (Harbor, Summit, Delta)
overlaps customer emails, ORD-82915 and TICK-4402 across tenants and has an ambiguous
email inside Delta; seed_business_tenant_fixtures loads it for provider tests without
changing make seed. The business upserts move into upsert_business_records.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

**Overview of Tasks 3–4 (5.2, 5.3).** Every code block in these two tasks was run in a scratch copy of the repository while the plan was written: ruff, strict mypy and the unit tests were clean, and the PostgreSQL contract suite passed on a throwaway database.

```
                         packages/domain/business.py  (stdlib only)
      FetchPlan ─ EntityRef ─ BusinessFact ─ BusinessContext.render()/to_payload() ─ unavailable_context()
                                     ▲
 packages/business/protocol.py       │ returns
   BusinessDataProvider.get_business_context(org_id, sender_email, plan)
          ▲                                   ▲
 memory.py InMemoryBusinessDataProvider   postgres.py PostgresBusinessDataProvider
   (dict lookups)                           (one read-only txn, SET LOCAL statement_timeout,
          │                                   5 fixed queries, all "organization_id = $1")
          └──────────────┬────────────────────┘
                         ▼  both implement CustomerLookups
 packages/business/assemble.py  assemble_business_context()
   resolve sender (lower, whole address, in org) ─▶ 0 rows UNKNOWN_SENDER │ >1 AMBIGUOUS_CUSTOMER
                                                   └▶ 1 row: typed refs ─▶ snapshot orders ─▶ snapshot tickets
                         ▲
 packages/business/testing.py  BusinessDataProviderContractSuite  (run by both subclasses)
   tests/unit/test_business_provider_contract.py      tests/integration/test_business_provider_postgres.py
```

The resolution and status rules live once in `assemble.py`; each implementation supplies only its five scoped queries, so the two providers cannot drift. Task 3 does not wire anything into the ai-worker and leaves `packages/context/builder.py` untouched (the old `get_business_data` protocol is removed in 5.4).

---

### Task 3: BusinessDataProvider interface & implementations [tasks.md 5.2]

**Files:**
- Create: `packages/domain/business.py`
- Create: `packages/business/protocol.py`
- Create: `packages/business/assemble.py`
- Create: `packages/business/memory.py`
- Create: `packages/business/postgres.py`
- Create: `packages/business/testing.py`
- Modify: `packages/domain/__init__.py` (import block lines 6-28: add one `from packages.domain.business import (...)` block before the `entities` import; `__all__` lines 73-128: add 10 names)
- Test (create): `tests/unit/test_domain_business.py`
- Test (create): `tests/unit/test_business_provider_contract.py`
- Test (create): `tests/integration/test_business_provider_postgres.py`
- Not touched here: `packages/business/__init__.py` (keeps its docstring; importers use module paths, which avoids merge conflicts with 5.4's `identifiers.py` / `plan.py` / `fetch.py`), `packages/context/builder.py`, `packages/context/__init__.py`, `tests/unit/test_context_builder.py` (all 5.4).

**Interfaces:**
- Consumes: `asyncpg` pool from `packages.db.connection.create_pool_from_settings(AppSettings().database)`; tables `customer`, `"order"`, `ticket` from `migrations/0001_core_schema.up.sql`.
- Produces (contract names, exact):
  - `class CustomerStatus(StrEnum)`: `FOUND`, `UNKNOWN_SENDER`, `AMBIGUOUS_CUSTOMER`, `UNAVAILABLE`
  - `class FactStatus(StrEnum)`: `FOUND`, `NOT_FOUND`, `NOT_LOOKED_UP`, `UNAVAILABLE`
  - `class EntityType(StrEnum)`: `ORDER="order"`, `TICKET="ticket"`, `INVOICE="invoice"`
  - `class NotLookedUpReason(StrEnum)`: `UNSUPPORTED_ENTITY="unsupported_entity"`, `UNKNOWN_SENDER="unknown_sender"`, `AMBIGUOUS_CUSTOMER="ambiguous_customer"`
  - `@dataclass(frozen=True) class EntityRef(entity: EntityType, reference: str)` (+ `to_payload() -> dict[str, str]`)
  - `@dataclass(frozen=True) class FetchPlan(refs: tuple[EntityRef, ...] = (), snapshot: frozenset[EntityType] = frozenset())`; `is_empty -> bool` (property); `ordered_snapshot -> tuple[EntityType, ...]` (property, additive); `to_payload() -> dict[str, Any]`
  - `@dataclass(frozen=True) class BusinessFact(entity, reference: str | None, status: FactStatus, reason: str | None = None, attributes: tuple[tuple[str, str], ...] = ())` (+ `render_line() -> str`, `to_payload() -> dict[str, Any]`)
  - `@dataclass(frozen=True) class BusinessContext(customer_status, as_of: datetime, customer=(), facts=(), source="business_db", degraded=False)`; `render() -> str`; `to_payload() -> dict[str, Any]`
  - `def unavailable_context(plan: FetchPlan, as_of: datetime) -> BusinessContext`
  - `class BusinessDataProvider(Protocol)` (`@runtime_checkable`): `async def get_business_context(self, organization_id: UUID, sender_email: str, plan: FetchPlan) -> BusinessContext`
  - `class InMemoryBusinessDataProvider(customers=(), orders=(), tickets=(), *, snapshot_orders=3, snapshot_tickets=3)`
  - `class PostgresBusinessDataProvider(pool, *, snapshot_orders=3, snapshot_tickets=3, statement_timeout_ms=500)`
  - `class BusinessDataProviderContractSuite(ABC)` with abstract `async make_provider(dataset: BusinessDataset, *, snapshot_orders=3, snapshot_tickets=3) -> BusinessDataProvider`

Render format that 5.4/5.5/5.6 assert against (one `key: value` line per fact, R13.5):

```
[BUSINESS DATA] source=business_db as_of=2026-09-28T10:00:00+00:00
degraded: true                                   <- only when degraded
customer_status: FOUND
customer.name: Alice Smith
customer.account_status: active
customer.tier: enterprise
order ORD-82915: FOUND | status=shipped | total=450.00 | placed_at=2026-09-20
order ORD-9901: NOT_FOUND
invoice INV-2026-8891: NOT_LOOKED_UP | reason=unsupported_entity
open tickets: NOT_FOUND                          <- snapshot kind with no rows
```

- [ ] **Step 1: Write the failing test for the domain models**

Create `tests/unit/test_domain_business.py`:

```python
"""Unit tests for the business-data domain models (R13.2, R13.5, R13.6, R13.7)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
    unavailable_context,
)

AS_OF = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def test_enum_values_match_the_contract() -> None:
    assert [s.value for s in CustomerStatus] == [
        "FOUND",
        "UNKNOWN_SENDER",
        "AMBIGUOUS_CUSTOMER",
        "UNAVAILABLE",
    ]
    assert [s.value for s in FactStatus] == ["FOUND", "NOT_FOUND", "NOT_LOOKED_UP", "UNAVAILABLE"]
    assert [e.value for e in EntityType] == ["order", "ticket", "invoice"]
    assert [r.value for r in NotLookedUpReason] == [
        "unsupported_entity",
        "unknown_sender",
        "ambiguous_customer",
    ]


def test_empty_plan_is_empty_and_serialises() -> None:
    plan = FetchPlan()
    assert plan.is_empty
    assert plan.to_payload() == {"refs": [], "snapshot": []}


def test_plan_payload_keeps_ref_order_and_fixed_snapshot_order() -> None:
    plan = FetchPlan(
        refs=(
            EntityRef(EntityType.TICKET, "TICK-4402"),
            EntityRef(EntityType.ORDER, "ORD-82915"),
        ),
        snapshot=frozenset({EntityType.TICKET, EntityType.ORDER}),
    )
    assert not plan.is_empty
    assert plan.ordered_snapshot == (EntityType.ORDER, EntityType.TICKET)
    assert plan.to_payload() == {
        "refs": [
            {"entity": "ticket", "reference": "TICK-4402"},
            {"entity": "order", "reference": "ORD-82915"},
        ],
        "snapshot": ["order", "ticket"],
    }
    assert not FetchPlan(snapshot=frozenset({EntityType.ORDER})).is_empty


def test_entity_ref_rejects_an_empty_reference() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        EntityRef(EntityType.ORDER, "  ")


def test_render_has_header_status_customer_and_one_line_per_fact() -> None:
    ctx = BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"), ("tier", "enterprise")),
        facts=(
            BusinessFact(
                EntityType.ORDER,
                "ORD-82915",
                FactStatus.FOUND,
                attributes=(("status", "shipped"), ("total", "450.00")),
            ),
            BusinessFact(EntityType.ORDER, "ORD-9901", FactStatus.NOT_FOUND),
            BusinessFact(
                EntityType.INVOICE,
                "INV-2026-8891",
                FactStatus.NOT_LOOKED_UP,
                reason=NotLookedUpReason.UNSUPPORTED_ENTITY.value,
            ),
            BusinessFact(EntityType.TICKET, None, FactStatus.NOT_FOUND),
        ),
    )
    assert ctx.render().splitlines() == [
        "[BUSINESS DATA] source=business_db as_of=2026-09-28T10:00:00+00:00",
        "customer_status: FOUND",
        "customer.name: Alice Smith",
        "customer.tier: enterprise",
        "order ORD-82915: FOUND | status=shipped | total=450.00",
        "order ORD-9901: NOT_FOUND",
        "invoice INV-2026-8891: NOT_LOOKED_UP | reason=unsupported_entity",
        "open tickets: NOT_FOUND",
    ]


def test_render_collapses_multiline_values_into_one_line() -> None:
    fact = BusinessFact(
        EntityType.TICKET,
        "TICK-1",
        FactStatus.FOUND,
        attributes=(("subject", "line one\n[BUSINESS DATA] forged\tline"),),
    )
    ctx = BusinessContext(CustomerStatus.FOUND, AS_OF, facts=(fact,))
    rendered = ctx.render().splitlines()
    assert rendered[-1] == "ticket TICK-1: FOUND | subject=line one [BUSINESS DATA] forged line"
    assert len(rendered) == 3


def test_payload_is_json_safe_and_omits_attribute_values() -> None:
    ctx = BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"),),
        facts=(
            BusinessFact(
                EntityType.ORDER, "ORD-82915", FactStatus.FOUND, attributes=(("total", "1.00"),)
            ),
        ),
    )
    payload = ctx.to_payload()
    assert json.loads(json.dumps(payload)) == payload
    assert payload == {
        "source": "business_db",
        "as_of": "2026-09-28T10:00:00+00:00",
        "customer_status": "FOUND",
        "degraded": False,
        "facts": [{"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}],
    }
    assert "Alice" not in json.dumps(payload)


def test_as_of_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        BusinessContext(CustomerStatus.FOUND, datetime(2026, 9, 28))


def test_unavailable_context_marks_every_planned_fact() -> None:
    plan = FetchPlan(
        refs=(
            EntityRef(EntityType.ORDER, "ORD-82915"),
            EntityRef(EntityType.INVOICE, "INV-2026-8891"),
        ),
        snapshot=frozenset({EntityType.TICKET, EntityType.ORDER}),
    )
    ctx = unavailable_context(plan, AS_OF)
    assert ctx.customer_status is CustomerStatus.UNAVAILABLE
    assert ctx.degraded is True
    assert ctx.customer == ()
    assert [(f.entity, f.reference, f.status) for f in ctx.facts] == [
        (EntityType.ORDER, "ORD-82915", FactStatus.UNAVAILABLE),
        (EntityType.INVOICE, "INV-2026-8891", FactStatus.UNAVAILABLE),
        (EntityType.ORDER, None, FactStatus.UNAVAILABLE),
        (EntityType.TICKET, None, FactStatus.UNAVAILABLE),
    ]
    assert ctx.render().splitlines()[1:3] == ["degraded: true", "customer_status: UNAVAILABLE"]
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/unit/test_domain_business.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.domain.business'` (collection error).

- [ ] **Step 3: Implement the domain models**

Create `packages/domain/business.py` (stdlib only, so `ContextPackage` in `packages/domain/entities.py` can hold a `BusinessContext` in 5.4 without breaking `tests/unit/test_dependency_rules.py::test_packages_domain_imports_stdlib_and_core_only`):

```python
"""Business-data value objects: fetch plan, facts and the rendered context (R13).

Requirements:
- R13.2: Provider-neutral models, so a CRM/ERP adapter returns the same types.
- R13.4: Customer resolution is recorded once as `customer_status`.
- R13.5: Facts render as one labelled `[BUSINESS DATA]` block with source and as-of.
- R13.6: A missing entity is an explicit `NOT_FOUND` fact, never an omission.
- R13.7: `unavailable_context` marks every planned fact `UNAVAILABLE` and sets `degraded`.
- specs/design.md §5.4 "Business data (R13)"; docs/adr/0008-business-data-fetch-plan.md.
- CLAUDE.md: packages/domain imports standard library and packages/core ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class CustomerStatus(StrEnum):
    """Outcome of resolving the sender address to one customer record (R13.4)."""

    FOUND = "FOUND"
    UNKNOWN_SENDER = "UNKNOWN_SENDER"
    AMBIGUOUS_CUSTOMER = "AMBIGUOUS_CUSTOMER"
    UNAVAILABLE = "UNAVAILABLE"


class FactStatus(StrEnum):
    """Outcome of one planned lookup or snapshot entry (R13.6, R13.7)."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_LOOKED_UP = "NOT_LOOKED_UP"
    UNAVAILABLE = "UNAVAILABLE"


class EntityType(StrEnum):
    """Business entity kinds a plan can name. Definition order is the render order."""

    ORDER = "order"
    TICKET = "ticket"
    INVOICE = "invoice"


class NotLookedUpReason(StrEnum):
    """Why a planned fact was not looked up (design §5.4 "Statuses")."""

    UNSUPPORTED_ENTITY = "unsupported_entity"
    UNKNOWN_SENDER = "unknown_sender"
    AMBIGUOUS_CUSTOMER = "ambiguous_customer"


SNAPSHOT_LABELS: dict[EntityType, str] = {
    EntityType.ORDER: "recent orders",
    EntityType.TICKET: "open tickets",
    EntityType.INVOICE: "invoices",
}
"""Render key of a fact with no reference (a snapshot entry that found no rows)."""


def _one_line(value: str) -> str:
    """Collapse whitespace so a stored value can never open a new prompt line."""
    return " ".join(str(value).split())


@dataclass(frozen=True)
class EntityRef:
    """A typed reference extracted from the email, in stored form (e.g. `ORD-82915`)."""

    entity: EntityType
    reference: str

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("EntityRef.reference must not be empty")

    def to_payload(self) -> dict[str, str]:
        return {"entity": self.entity.value, "reference": self.reference}


@dataclass(frozen=True)
class FetchPlan:
    """What to look up for one job: typed references plus snapshot entity kinds."""

    refs: tuple[EntityRef, ...] = ()
    snapshot: frozenset[EntityType] = frozenset()

    @property
    def is_empty(self) -> bool:
        """True when nothing is planned: no provider call and no customer resolution."""
        return not self.refs and not self.snapshot

    @property
    def ordered_snapshot(self) -> tuple[EntityType, ...]:
        """Snapshot kinds in the fixed EntityType order, so output never depends on set order."""
        return tuple(entity for entity in EntityType if entity in self.snapshot)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form for the CONTEXT_READY payload (replay)."""
        return {
            "refs": [ref.to_payload() for ref in self.refs],
            "snapshot": [entity.value for entity in self.ordered_snapshot],
        }


@dataclass(frozen=True)
class BusinessFact:
    """One planned lookup or snapshot row with its status and ordered attributes."""

    entity: EntityType
    reference: str | None
    status: FactStatus
    reason: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def render_line(self) -> str:
        """One `key: value` line, e.g. `order ORD-82915: FOUND | status=shipped`."""
        key = (
            f"{self.entity.value} {_one_line(self.reference)}"
            if self.reference
            else SNAPSHOT_LABELS[self.entity]
        )
        parts = [self.status.value]
        if self.reason:
            parts.append(f"reason={self.reason}")
        parts.extend(f"{name}={_one_line(value)}" for name, value in self.attributes)
        return f"{key}: {' | '.join(parts)}"

    def to_payload(self) -> dict[str, Any]:
        """Statuses only: attribute values stay out of the persisted event payload."""
        return {
            "entity": self.entity.value,
            "reference": self.reference,
            "status": self.status.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BusinessContext:
    """Everything the business subsystem contributed to one job's context."""

    customer_status: CustomerStatus
    as_of: datetime
    customer: tuple[tuple[str, str], ...] = ()
    facts: tuple[BusinessFact, ...] = ()
    source: str = "business_db"
    degraded: bool = False

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("BusinessContext.as_of must be timezone-aware")

    def render(self) -> str:
        """The `[BUSINESS DATA]` block: header, customer_status, then one line per fact."""
        lines = [f"[BUSINESS DATA] source={self.source} as_of={self.as_of.isoformat()}"]
        if self.degraded:
            lines.append("degraded: true")
        lines.append(f"customer_status: {self.customer_status.value}")
        lines.extend(f"customer.{name}: {_one_line(value)}" for name, value in self.customer)
        lines.extend(fact.render_line() for fact in self.facts)
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form for the CONTEXT_READY payload; carries no customer attributes."""
        return {
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "customer_status": self.customer_status.value,
            "degraded": self.degraded,
            "facts": [fact.to_payload() for fact in self.facts],
        }


def unavailable_context(plan: FetchPlan, as_of: datetime) -> BusinessContext:
    """The degraded context: customer and every planned fact `UNAVAILABLE` (R13.7)."""
    facts = [BusinessFact(ref.entity, ref.reference, FactStatus.UNAVAILABLE) for ref in plan.refs]
    facts.extend(
        BusinessFact(entity, None, FactStatus.UNAVAILABLE) for entity in plan.ordered_snapshot
    )
    return BusinessContext(
        customer_status=CustomerStatus.UNAVAILABLE,
        as_of=as_of,
        facts=tuple(facts),
        degraded=True,
    )
```

Modify `packages/domain/__init__.py`: insert this block directly above `from packages.domain.entities import (` (line 6):

```python
from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
    unavailable_context,
)
```

and add these entries to `__all__` in its existing alphabetical-by-case order: `"BusinessContext"`, `"BusinessFact"` after `"AttachmentRef"`; `"CustomerStatus"` after `"Checkpoint"`; `"EntityRef"`, `"EntityType"`, `"FactStatus"`, `"FetchPlan"` after `"EmbeddingRecord"`; `"NotLookedUpReason"` after `"NormalizedMessage"`; `"unavailable_context"` after `"transition_job"`.

- [ ] **Step 4: Run the domain tests and the dependency rule**

Run: `uv run pytest tests/unit/test_domain_business.py tests/unit/test_dependency_rules.py tests/unit/test_domain_entities.py -v`
Expected: PASS (9 new tests; the dependency rule and existing entity tests still pass).

- [ ] **Step 5: Commit the domain models**

```bash
git add packages/domain/business.py packages/domain/__init__.py tests/unit/test_domain_business.py
git commit -m "feat(domain): business-data plan, fact and context models [task 5.2] [R13.2, R13.5, R13.6, R13.7]" \
  -m "FetchPlan, BusinessFact and BusinessContext live in the domain so ContextPackage can hold them. render() gives the one [BUSINESS DATA] block with source and as_of; to_payload() carries statuses only; unavailable_context() is the degraded form." \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Write the failing contract suite and its in-memory run**

Create `packages/business/testing.py` (same pattern as `packages/retrieval/testing.py`: an ABC collected only through `Test*` subclasses; `asyncio_mode = "auto"` makes the `async def test_*` methods run without markers):

```python
"""Reusable contract test suite for BusinessDataProvider implementations.

Requirements:
- R13.2: One shared contract suite that every implementation (in-memory, PostgreSQL, a future
  CRM/ERP adapter) must pass.
- R13.4: Sender → customer resolution and customer/tenant scoping.
- R13.6: Missing entities are explicit NOT_FOUND / NOT_LOOKED_UP facts.
- CLAUDE.md §8: The scoping cases run over ≥3 tenants with overlapping emails and numbers.
- specs/design.md §5.4 "Business data (R13)".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from packages.business.protocol import BusinessDataProvider
from packages.domain.business import (
    BusinessContext,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
"""Base timestamp for seeded rows; tests offset from it so ordering is explicit."""


@dataclass
class BusinessDataset:
    """Customer/order/ticket dicts in the `packages/db/fixtures/business.py` record shape."""

    customers: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def organization_ids(self) -> list[UUID]:
        seen: dict[UUID, None] = {}
        for row in (*self.customers, *self.orders, *self.tickets):
            seen.setdefault(row["organization_id"], None)
        return list(seen)

    def add_customer(
        self,
        organization_id: UUID,
        email: str,
        name: str = "Test Customer",
        *,
        tier: str = "standard",
        account_status: str = "active",
    ) -> UUID:
        customer_id = uuid4()
        self.customers.append(
            {
                "id": customer_id,
                "organization_id": organization_id,
                "email": email,
                "name": name,
                "account_status": account_status,
                "tier": tier,
            }
        )
        return customer_id

    def add_order(
        self,
        organization_id: UUID,
        customer_id: UUID,
        order_number: str,
        status: str,
        *,
        total: Decimal = Decimal("10.00"),
        placed_at: datetime = T0,
        shipped_at: datetime | None = None,
    ) -> UUID:
        order_id = uuid4()
        self.orders.append(
            {
                "id": order_id,
                "organization_id": organization_id,
                "customer_id": customer_id,
                "order_number": order_number,
                "status": status,
                "total": total,
                "placed_at": placed_at,
                "shipped_at": shipped_at,
            }
        )
        return order_id

    def add_ticket(
        self,
        organization_id: UUID,
        customer_id: UUID,
        ticket_number: str,
        status: str,
        *,
        priority: str = "normal",
        subject: str = "Help needed",
        opened_at: datetime = T0,
    ) -> UUID:
        ticket_id = uuid4()
        self.tickets.append(
            {
                "id": ticket_id,
                "organization_id": organization_id,
                "customer_id": customer_id,
                "ticket_number": ticket_number,
                "subject": subject,
                "status": status,
                "priority": priority,
                "opened_at": opened_at,
            }
        )
        return ticket_id


def order_ref(number: str) -> EntityRef:
    return EntityRef(EntityType.ORDER, number)


def ticket_ref(number: str) -> EntityRef:
    return EntityRef(EntityType.TICKET, number)


def invoice_ref(number: str) -> EntityRef:
    return EntityRef(EntityType.INVOICE, number)


def fact_rows(ctx: BusinessContext) -> list[tuple[str, str | None, str, str | None]]:
    """(entity, reference, status, reason) per fact, for compact assertions."""
    return [(f.entity.value, f.reference, f.status.value, f.reason) for f in ctx.facts]


class BusinessDataProviderContractSuite(ABC):
    """Abstract contract suite. Subclasses implement `make_provider` only."""

    @abstractmethod
    async def make_provider(
        self,
        dataset: BusinessDataset,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> BusinessDataProvider:
        """Load `dataset` into the implementation under test and return a provider over it."""
        raise NotImplementedError

    async def test_satisfies_protocol(self) -> None:
        provider = await self.make_provider(BusinessDataset())
        assert isinstance(provider, BusinessDataProvider)

    async def test_typed_order_found_with_attributes(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        alice = data.add_customer(org, "alice@example.com", "Alice Smith", tier="enterprise")
        data.add_order(
            org,
            alice,
            "ORD-82915",
            "shipped",
            total=Decimal("450.5"),
            placed_at=T0,
            shipped_at=T0 + timedelta(days=2),
        )
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "alice@example.com", FetchPlan(refs=(order_ref("ORD-82915"),))
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ctx.customer == (
            ("name", "Alice Smith"),
            ("account_status", "active"),
            ("tier", "enterprise"),
        )
        assert ctx.source == "business_db"
        assert ctx.degraded is False
        assert ctx.as_of.tzinfo is not None
        assert len(ctx.facts) == 1
        fact = ctx.facts[0]
        assert (fact.entity, fact.reference, fact.status) == (
            EntityType.ORDER,
            "ORD-82915",
            FactStatus.FOUND,
        )
        assert fact.attributes == (
            ("status", "shipped"),
            ("total", "450.50"),
            ("placed_at", "2026-09-01"),
            ("shipped_at", "2026-09-03"),
        )

    async def test_typed_ticket_found_even_when_closed(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        ed = data.add_customer(org, "ed@example.com")
        data.add_ticket(org, ed, "TICK-4402", "closed", priority="high", subject="Late parcel")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "ed@example.com", FetchPlan(refs=(ticket_ref("TICK-4402"),))
        )

        assert fact_rows(ctx) == [("ticket", "TICK-4402", "FOUND", None)]
        assert ctx.facts[0].attributes == (
            ("status", "closed"),
            ("priority", "high"),
            ("subject", "Late parcel"),
            ("opened_at", "2026-09-01"),
        )

    async def test_missing_typed_references_are_not_found(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        data.add_customer(org, "alice@example.com")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "alice@example.com",
            FetchPlan(refs=(order_ref("ORD-11111"), ticket_ref("TICK-22222"))),
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert fact_rows(ctx) == [
            ("order", "ORD-11111", "NOT_FOUND", None),
            ("ticket", "TICK-22222", "NOT_FOUND", None),
        ]

    async def test_invoice_reference_is_not_looked_up(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        data.add_customer(org, "bob@example.com")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "bob@example.com", FetchPlan(refs=(invoice_ref("INV-2026-8891"),))
        )

        assert fact_rows(ctx) == [
            ("invoice", "INV-2026-8891", "NOT_LOOKED_UP", "unsupported_entity")
        ]

    async def test_order_snapshot_is_newest_first_and_limited(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        for day, number in enumerate(["ORD-1001", "ORD-1002", "ORD-1003", "ORD-1004"]):
            data.add_order(org, c, number, "processing", placed_at=T0 + timedelta(days=day))
        provider = await self.make_provider(data, snapshot_orders=3)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.ORDER}))
        )

        assert [f.reference for f in ctx.facts] == ["ORD-1004", "ORD-1003", "ORD-1002"]
        assert {f.status for f in ctx.facts} == {FactStatus.FOUND}

    async def test_snapshot_ties_break_by_number_descending(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        for number in ["ORD-2001", "ORD-2003", "ORD-2002"]:
            data.add_order(org, c, number, "processing", placed_at=T0)
        provider = await self.make_provider(data, snapshot_orders=2)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.ORDER}))
        )

        assert [f.reference for f in ctx.facts] == ["ORD-2003", "ORD-2002"]

    async def test_ticket_snapshot_skips_closed_and_resolved(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_ticket(org, c, "TICK-3001", "open", opened_at=T0)
        data.add_ticket(org, c, "TICK-3002", "Closed", opened_at=T0 + timedelta(days=1))
        data.add_ticket(org, c, "TICK-3003", "resolved", opened_at=T0 + timedelta(days=2))
        data.add_ticket(org, c, "TICK-3004", "pending", opened_at=T0 + timedelta(days=3))
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.TICKET}))
        )

        assert fact_rows(ctx) == [
            ("ticket", "TICK-3004", "FOUND", None),
            ("ticket", "TICK-3001", "FOUND", None),
        ]

    async def test_empty_snapshot_is_one_not_found_fact_per_entity(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_ticket(org, c, "TICK-3002", "closed")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "c@example.com",
            FetchPlan(snapshot=frozenset({EntityType.TICKET, EntityType.ORDER})),
        )

        assert fact_rows(ctx) == [
            ("order", None, "NOT_FOUND", None),
            ("ticket", None, "NOT_FOUND", None),
        ]

    async def test_facts_follow_plan_order_without_repeating_typed_rows(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_order(org, c, "ORD-4001", "shipped", placed_at=T0)
        data.add_order(org, c, "ORD-4002", "processing", placed_at=T0 + timedelta(days=1))
        data.add_ticket(org, c, "TICK-4001", "open")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "c@example.com",
            FetchPlan(
                refs=(ticket_ref("TICK-9999"), order_ref("ORD-4001")),
                snapshot=frozenset({EntityType.TICKET, EntityType.ORDER}),
            ),
        )

        assert fact_rows(ctx) == [
            ("ticket", "TICK-9999", "NOT_FOUND", None),
            ("order", "ORD-4001", "FOUND", None),
            ("order", "ORD-4002", "FOUND", None),
            ("ticket", "TICK-4001", "FOUND", None),
        ]

    async def test_snapshot_limit_zero_disables_that_entity(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "c@example.com")
        data.add_order(org, c, "ORD-5001", "shipped")
        provider = await self.make_provider(data, snapshot_orders=0)

        ctx = await provider.get_business_context(
            org, "c@example.com", FetchPlan(snapshot=frozenset({EntityType.ORDER}))
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ctx.facts == ()

    async def test_unknown_sender_marks_every_planned_fact_not_looked_up(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        c = data.add_customer(org, "known@example.com")
        data.add_order(org, c, "ORD-6001", "shipped")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "stranger@example.com",
            FetchPlan(
                refs=(order_ref("ORD-6001"), invoice_ref("INV-2026-1")),
                snapshot=frozenset({EntityType.TICKET}),
            ),
        )

        assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER
        assert ctx.customer == ()
        assert fact_rows(ctx) == [
            ("order", "ORD-6001", "NOT_LOOKED_UP", "unknown_sender"),
            ("invoice", "INV-2026-1", "NOT_LOOKED_UP", "unsupported_entity"),
            ("ticket", None, "NOT_LOOKED_UP", "unknown_sender"),
        ]

    async def test_ambiguous_customer_marks_every_planned_fact_not_looked_up(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        first = data.add_customer(org, "shared@example.com", "First")
        data.add_customer(org, "Shared@Example.com", "Second")
        data.add_order(org, first, "ORD-7001", "shipped")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(
            org,
            "shared@example.com",
            FetchPlan(refs=(order_ref("ORD-7001"),), snapshot=frozenset({EntityType.ORDER})),
        )

        assert ctx.customer_status is CustomerStatus.AMBIGUOUS_CUSTOMER
        assert ctx.customer == ()
        assert fact_rows(ctx) == [
            ("order", "ORD-7001", "NOT_LOOKED_UP", "ambiguous_customer"),
            ("order", None, "NOT_LOOKED_UP", "ambiguous_customer"),
        ]

    async def test_empty_plan_resolves_customer_and_returns_no_facts(self) -> None:
        org = uuid4()
        data = BusinessDataset()
        data.add_customer(org, "c@example.com")
        provider = await self.make_provider(data)

        ctx = await provider.get_business_context(org, "c@example.com", FetchPlan())

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ctx.facts == ()
```

Create `tests/unit/test_business_provider_contract.py`:

```python
"""The in-memory BusinessDataProvider passes the shared contract suite (R13.2, R13.4, R13.6)."""

from __future__ import annotations

from packages.business.memory import InMemoryBusinessDataProvider
from packages.business.protocol import BusinessDataProvider
from packages.business.testing import BusinessDataProviderContractSuite, BusinessDataset


class TestInMemoryBusinessDataProviderContract(BusinessDataProviderContractSuite):
    async def make_provider(
        self,
        dataset: BusinessDataset,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> BusinessDataProvider:
        return InMemoryBusinessDataProvider(
            dataset.customers,
            dataset.orders,
            dataset.tickets,
            snapshot_orders=snapshot_orders,
            snapshot_tickets=snapshot_tickets,
        )
```

- [ ] **Step 7: Run it to see it fail**

Run: `uv run pytest tests/unit/test_business_provider_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.business.protocol'`.

**Owner-dependent rule (Open Question 1).** The invoice row above (`unsupported_entity` when the sender is unresolved) is the plan's reading of design §5.4. tasks.md 5.3 literally says *every* planned fact gets `unknown_sender` / `ambiguous_customer`. Use the owner's answer from Task 0 Step 1 if there is one:
- If the owner chose the literal tasks.md rule, change the expected invoice row here to `("invoice", "INV-2026-1", "NOT_LOOKED_UP", "unknown_sender")`, and in Step 8 make `_not_looked_up` emit `BusinessFact(ref.entity, ref.reference, FactStatus.NOT_LOOKED_UP, reason.value)` for every ref and `BusinessFact(entity, None, FactStatus.NOT_LOOKED_UP, reason.value)` for every snapshot entity, with no invoice branch.
- If the owner kept the design §5.4 reading, or has not answered yet, implement it as written. Task 9 Step 3 then keeps 5.3 at `[~]` until the owner has answered and the spec (tasks.md 5.3 or design §5.4) states the chosen rule.

- [ ] **Step 8: Implement the protocol, the shared assembly and the in-memory provider**

Create `packages/business/protocol.py`:

```python
"""The replaceable business-data provider interface (R13.2).

Requirements:
- R13.2: One interface, so the local PostgreSQL implementation can later be replaced by a
  CRM/ERP adapter without touching the Context Builder.
- R13.4: The provider resolves the sender to a customer first and scopes every lookup to it.
- specs/design.md §5.4 "Business data (R13)"; docs/adr/0008-business-data-fetch-plan.md.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from packages.domain.business import BusinessContext, FetchPlan


@runtime_checkable
class BusinessDataProvider(Protocol):
    """Executes a FetchPlan for one sender inside one organization.

    Implementations resolve `sender_email` to a customer (case-insensitive, whole address,
    within `organization_id`) and run only fixed, parameterised lookups scoped to
    `(organization_id, customer_id)`. They raise on infrastructure errors; the caller
    (`fetch_business_context`, task 5.4) turns a timeout or error into
    `unavailable_context`.
    """

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext: ...
```

Create `packages/business/assemble.py`:

```python
"""Provider-neutral resolution and fact assembly shared by every implementation (R13.4, R13.6).

Each implementation supplies only a `CustomerLookups` object (its five scoped queries); the
rules that turn rows into statuses live here once, so the in-memory and PostgreSQL providers
cannot drift apart.

Requirements:
- R13.4: Sender → customer resolution first; every lookup scoped to the resolved customer.
- R13.6: A missing entity is an explicit NOT_FOUND fact, never an omission.
- specs/design.md §5.4 "Snapshot" and "Statuses"; docs/adr/0008-business-data-fetch-plan.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
)

Row = Mapping[str, Any]
Attributes = tuple[tuple[str, str], ...]


class CustomerLookups(Protocol):
    """The scoped queries an implementation must provide. Every one takes organization_id."""

    async def customers_by_email(self, organization_id: UUID, sender_email: str) -> Sequence[Row]:
        """Customers whose email equals `sender_email` ignoring case (at most 2 needed)."""
        ...

    async def order_by_number(
        self, organization_id: UUID, customer_id: UUID, order_number: str
    ) -> Row | None: ...

    async def ticket_by_number(
        self, organization_id: UUID, customer_id: UUID, ticket_number: str
    ) -> Row | None: ...

    async def recent_orders(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> Sequence[Row]:
        """Newest first: `placed_at DESC, order_number DESC`."""
        ...

    async def open_tickets(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> Sequence[Row]:
        """Status not closed/resolved, newest first: `opened_at DESC, ticket_number DESC`."""
        ...


CLOSED_TICKET_STATUSES: frozenset[str] = frozenset({"closed", "resolved"})
"""Ticket statuses the snapshot leaves out (design §5.4 "Snapshot")."""


def normalise_email(sender_email: str) -> str:
    """The comparison form of an address: surrounding whitespace removed, lower case."""
    return sender_email.strip().lower()


def _day(value: datetime) -> str:
    return value.astimezone(UTC).date().isoformat()


def _money(value: Any) -> str:
    return f"{Decimal(str(value)):.2f}"


def customer_attributes(row: Row) -> tuple[tuple[str, str], ...]:
    return (
        ("name", str(row["name"])),
        ("account_status", str(row.get("account_status") or "active")),
        ("tier", str(row.get("tier") or "standard")),
    )


def order_attributes(row: Row) -> tuple[tuple[str, str], ...]:
    attrs: list[tuple[str, str]] = [
        ("status", str(row["status"])),
        ("total", _money(row["total"])),
    ]
    if row.get("placed_at") is not None:
        attrs.append(("placed_at", _day(row["placed_at"])))
    if row.get("shipped_at") is not None:
        attrs.append(("shipped_at", _day(row["shipped_at"])))
    return tuple(attrs)


def ticket_attributes(row: Row) -> tuple[tuple[str, str], ...]:
    attrs: list[tuple[str, str]] = [
        ("status", str(row["status"])),
        ("priority", str(row["priority"])),
        ("subject", str(row["subject"])),
    ]
    if row.get("opened_at") is not None:
        attrs.append(("opened_at", _day(row["opened_at"])))
    return tuple(attrs)


def _unsupported(entity: EntityType, reference: str | None) -> BusinessFact:
    return BusinessFact(
        entity,
        reference,
        FactStatus.NOT_LOOKED_UP,
        reason=NotLookedUpReason.UNSUPPORTED_ENTITY.value,
    )


def _not_looked_up(plan: FetchPlan, reason: NotLookedUpReason) -> tuple[BusinessFact, ...]:
    """Every planned fact when resolution gave no single customer.

    Invoices keep `unsupported_entity`: they are never looked up, whoever the sender is.
    """
    facts: list[BusinessFact] = []
    for ref in plan.refs:
        if ref.entity is EntityType.INVOICE:
            facts.append(_unsupported(ref.entity, ref.reference))
        else:
            facts.append(
                BusinessFact(ref.entity, ref.reference, FactStatus.NOT_LOOKED_UP, reason.value)
            )
    for entity in plan.ordered_snapshot:
        if entity is EntityType.INVOICE:
            facts.append(_unsupported(entity, None))
        else:
            facts.append(BusinessFact(entity, None, FactStatus.NOT_LOOKED_UP, reason.value))
    return tuple(facts)


async def assemble_business_context(
    lookups: CustomerLookups,
    *,
    organization_id: UUID,
    sender_email: str,
    plan: FetchPlan,
    snapshot_orders: int,
    snapshot_tickets: int,
    as_of: datetime,
) -> BusinessContext:
    """Resolve the sender, then run the plan's lookups scoped to that one customer.

    Fact order: typed references in plan order, then snapshot orders, then snapshot
    tickets. A snapshot row already reported by a typed reference is not repeated. A
    snapshot kind with no rows yields one NOT_FOUND fact (reference None); a snapshot
    limit of 0 disables that kind.
    """
    customers = await lookups.customers_by_email(organization_id, normalise_email(sender_email))
    if len(customers) == 0:
        return BusinessContext(
            customer_status=CustomerStatus.UNKNOWN_SENDER,
            as_of=as_of,
            facts=_not_looked_up(plan, NotLookedUpReason.UNKNOWN_SENDER),
        )
    if len(customers) > 1:
        return BusinessContext(
            customer_status=CustomerStatus.AMBIGUOUS_CUSTOMER,
            as_of=as_of,
            facts=_not_looked_up(plan, NotLookedUpReason.AMBIGUOUS_CUSTOMER),
        )

    customer = customers[0]
    customer_id: UUID = customer["id"]
    facts: list[BusinessFact] = []
    typed: dict[EntityType, set[str]] = {entity: set() for entity in EntityType}

    for ref in plan.refs:
        typed[ref.entity].add(ref.reference)
        if ref.entity is EntityType.ORDER:
            row = await lookups.order_by_number(organization_id, customer_id, ref.reference)
            facts.append(
                BusinessFact(ref.entity, ref.reference, FactStatus.NOT_FOUND)
                if row is None
                else BusinessFact(
                    ref.entity, ref.reference, FactStatus.FOUND, attributes=order_attributes(row)
                )
            )
        elif ref.entity is EntityType.TICKET:
            row = await lookups.ticket_by_number(organization_id, customer_id, ref.reference)
            facts.append(
                BusinessFact(ref.entity, ref.reference, FactStatus.NOT_FOUND)
                if row is None
                else BusinessFact(
                    ref.entity, ref.reference, FactStatus.FOUND, attributes=ticket_attributes(row)
                )
            )
        else:
            facts.append(_unsupported(ref.entity, ref.reference))

    for entity in plan.ordered_snapshot:
        if entity is EntityType.ORDER and snapshot_orders > 0:
            rows = await lookups.recent_orders(organization_id, customer_id, snapshot_orders)
            facts.extend(_snapshot_facts(entity, rows, "order_number", order_attributes, typed))
        elif entity is EntityType.TICKET and snapshot_tickets > 0:
            rows = await lookups.open_tickets(organization_id, customer_id, snapshot_tickets)
            facts.extend(_snapshot_facts(entity, rows, "ticket_number", ticket_attributes, typed))
        elif entity is EntityType.INVOICE:
            facts.append(_unsupported(entity, None))

    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=as_of,
        customer=customer_attributes(customer),
        facts=tuple(facts),
    )


def _snapshot_facts(
    entity: EntityType,
    rows: Sequence[Row],
    number_key: str,
    attributes: Callable[[Row], Attributes],
    typed: dict[EntityType, set[str]],
) -> list[BusinessFact]:
    if not rows:
        return [BusinessFact(entity, None, FactStatus.NOT_FOUND)]
    return [
        BusinessFact(entity, str(row[number_key]), FactStatus.FOUND, attributes=attributes(row))
        for row in rows
        if str(row[number_key]) not in typed[entity]
    ]
```

Create `packages/business/memory.py`:

```python
"""In-memory BusinessDataProvider for unit tests (R13.2).

Holds customer/order/ticket dicts shaped like `packages/db/fixtures/business.py` records.
Optional keys: `placed_at` / `shipped_at` on orders and `opened_at` on tickets (aware
datetimes); a missing timestamp sorts oldest, as a stand-in for the database default.

Requirements:
- R13.2: A second implementation of the same interface, proven by the shared contract suite.
- R13.4: Every lookup filters on organization_id and the resolved customer_id.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from packages.business.assemble import (
    CLOSED_TICKET_STATUSES,
    Row,
    assemble_business_context,
    normalise_email,
)
from packages.domain.business import BusinessContext, FetchPlan

_OLDEST = datetime.min.replace(tzinfo=UTC)


class InMemoryBusinessDataProvider:
    """Dict-backed provider with the same resolution and ordering rules as PostgreSQL."""

    def __init__(
        self,
        customers: Sequence[dict[str, Any]] = (),
        orders: Sequence[dict[str, Any]] = (),
        tickets: Sequence[dict[str, Any]] = (),
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> None:
        self._customers: list[Row] = [dict(c) for c in customers]
        self._orders: list[Row] = [dict(o) for o in orders]
        self._tickets: list[Row] = [dict(t) for t in tickets]
        self.snapshot_orders = snapshot_orders
        self.snapshot_tickets = snapshot_tickets

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        return await assemble_business_context(
            self,
            organization_id=organization_id,
            sender_email=sender_email,
            plan=plan,
            snapshot_orders=self.snapshot_orders,
            snapshot_tickets=self.snapshot_tickets,
            as_of=datetime.now(UTC),
        )

    # --- CustomerLookups -------------------------------------------------------------

    async def customers_by_email(self, organization_id: UUID, sender_email: str) -> list[Row]:
        wanted = normalise_email(sender_email)
        return [
            c
            for c in self._customers
            if c["organization_id"] == organization_id and str(c["email"]).lower() == wanted
        ][:2]

    async def order_by_number(
        self, organization_id: UUID, customer_id: UUID, order_number: str
    ) -> Row | None:
        rows = self._sorted_orders(organization_id, customer_id)
        return next((o for o in rows if o["order_number"] == order_number), None)

    async def ticket_by_number(
        self, organization_id: UUID, customer_id: UUID, ticket_number: str
    ) -> Row | None:
        rows = self._sorted_tickets(organization_id, customer_id)
        return next((t for t in rows if t["ticket_number"] == ticket_number), None)

    async def recent_orders(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> list[Row]:
        return self._sorted_orders(organization_id, customer_id)[:limit]

    async def open_tickets(self, organization_id: UUID, customer_id: UUID, limit: int) -> list[Row]:
        rows = self._sorted_tickets(organization_id, customer_id)
        return [t for t in rows if str(t["status"]).lower() not in CLOSED_TICKET_STATUSES][:limit]

    def _sorted_orders(self, organization_id: UUID, customer_id: UUID) -> list[Row]:
        rows = [
            o
            for o in self._orders
            if o["organization_id"] == organization_id and o["customer_id"] == customer_id
        ]
        return sorted(
            rows, key=lambda o: (o.get("placed_at") or _OLDEST, o["order_number"]), reverse=True
        )

    def _sorted_tickets(self, organization_id: UUID, customer_id: UUID) -> list[Row]:
        rows = [
            t
            for t in self._tickets
            if t["organization_id"] == organization_id and t["customer_id"] == customer_id
        ]
        return sorted(
            rows, key=lambda t: (t.get("opened_at") or _OLDEST, t["ticket_number"]), reverse=True
        )
```

- [ ] **Step 9: Run the in-memory contract suite**

Run: `uv run pytest tests/unit/test_business_provider_contract.py -v`
Expected: PASS (14 tests).

- [ ] **Step 10: Write the failing PostgreSQL contract run and timeout tests**

Create `tests/integration/test_business_provider_postgres.py` (local `db_pool` fixture per file and org-on-demand seeding, as in `tests/integration/test_postgres_search_backend.py`; fresh `uuid4()` ids per test because the database is reset once per package run, not per test):

```python
"""PostgresBusinessDataProvider on real PostgreSQL (rag_email_test).

Requirements:
- R13.1: Reads the migration-0001 customer / "order" / ticket tables.
- R13.2: Passes the same contract suite as the in-memory provider.
- R13.4: Resolution and scoping proven on real Postgres (the scoping cases span 3 tenants).
- R13.7: The transaction-local statement_timeout cancels a blocked statement.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from packages.business.postgres import PostgresBusinessDataProvider
from packages.business.protocol import BusinessDataProvider
from packages.business.testing import (
    BusinessDataProviderContractSuite,
    BusinessDataset,
    order_ref,
)
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.domain.business import FetchPlan


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    """Pool on the isolated test database (tests/integration/conftest.py)."""
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


async def seed_business_rows(pool: asyncpg.Pool[Any], dataset: BusinessDataset) -> None:
    """Insert the dataset's organizations, customers, orders and tickets."""
    async with pool.acquire() as conn, conn.transaction():
        for org_id in dataset.organization_ids:
            await conn.execute(
                "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                org_id,
                f"Business Org {org_id}",
            )
        for c in dataset.customers:
            await conn.execute(
                "INSERT INTO customer (id, organization_id, email, name, account_status, tier) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                c["id"],
                c["organization_id"],
                c["email"],
                c["name"],
                c["account_status"],
                c["tier"],
            )
        for o in dataset.orders:
            await conn.execute(
                'INSERT INTO "order" (id, organization_id, customer_id, order_number, status, '
                "total, placed_at, shipped_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                o["id"],
                o["organization_id"],
                o["customer_id"],
                o["order_number"],
                o["status"],
                o["total"],
                o["placed_at"],
                o["shipped_at"],
            )
        for t in dataset.tickets:
            await conn.execute(
                "INSERT INTO ticket (id, organization_id, customer_id, ticket_number, status, "
                "priority, subject, opened_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                t["id"],
                t["organization_id"],
                t["customer_id"],
                t["ticket_number"],
                t["status"],
                t["priority"],
                t["subject"],
                t["opened_at"],
            )


class TestPostgresBusinessDataProviderContract(BusinessDataProviderContractSuite):
    """Proves PostgresBusinessDataProvider passes the canonical suite on live PostgreSQL."""

    @pytest.fixture(autouse=True)
    def setup_provider(self, db_pool: asyncpg.Pool[Any]) -> None:
        self.pool = db_pool

    async def make_provider(
        self,
        dataset: BusinessDataset,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
    ) -> BusinessDataProvider:
        await seed_business_rows(self.pool, dataset)
        return PostgresBusinessDataProvider(
            self.pool, snapshot_orders=snapshot_orders, snapshot_tickets=snapshot_tickets
        )


async def test_statement_timeout_cancels_a_blocked_lookup(db_pool: asyncpg.Pool[Any]) -> None:
    """A lookup stuck behind a lock is cancelled by statement_timeout, not left hanging."""
    org = uuid4()
    data = BusinessDataset()
    data.add_customer(org, "blocked@example.com")
    await seed_business_rows(db_pool, data)
    provider = PostgresBusinessDataProvider(db_pool, statement_timeout_ms=100)

    async with db_pool.acquire() as locker, locker.transaction():
        await locker.execute("LOCK TABLE customer IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(asyncpg.exceptions.QueryCanceledError):
            await asyncio.wait_for(
                provider.get_business_context(
                    org, "blocked@example.com", FetchPlan(refs=(order_ref("ORD-1"),))
                ),
                timeout=5,
            )


async def test_statement_timeout_does_not_leak_to_the_pooled_connection(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """set_config(..., is_local=true) ends with the transaction; the next user sees the default."""
    provider = PostgresBusinessDataProvider(db_pool, statement_timeout_ms=123)
    await provider.get_business_context(uuid4(), "nobody@example.com", FetchPlan())
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SHOW statement_timeout") != "123ms"


def test_constructor_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="statement_timeout_ms"):
        PostgresBusinessDataProvider(object(), statement_timeout_ms=0)
```

- [ ] **Step 11: Run it to see it fail**

Run: `uv run pytest tests/integration/test_business_provider_postgres.py -v` (needs `make up` for PostgreSQL on 5433 and the RabbitMQ management API; the package conftest resets and migrates `rag_email_test`).
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.business.postgres'`.

- [ ] **Step 12: Implement the PostgreSQL provider**

Create `packages/business/postgres.py` (pool first in the constructor, as `PostgresThreadStateStore` and `PostgresSearchBackend`; `$n` parameters; `"order"` quoted; the timeout is set with `set_config(..., is_local => true)` because `SET LOCAL` takes no bind parameter; `COLLATE "C"` makes the tie-break byte-ordered, matching Python string order in the in-memory provider):

```python
"""PostgreSQL BusinessDataProvider over the migration-0001 business tables (R13.1, R13.2).

Every query is a fixed, parameterised statement whose first predicate is
`organization_id = $1`; order and ticket queries also carry `customer_id = $2`. The whole
call runs in one read-only transaction with a transaction-local `statement_timeout`
(design §5.4 "Timeout and degradation"); the outer deadline is `fetch_business_context`'s.

Requirements:
- R13.2: The local implementation behind the replaceable interface.
- R13.4: Case-insensitive whole-address resolution inside the organization; lookups scoped
  to the resolved customer.
- R13.7: `statement_timeout` bounds each statement; errors propagate to the caller.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from packages.business.assemble import Row, assemble_business_context
from packages.domain.business import BusinessContext, FetchPlan

logger = logging.getLogger(__name__)

SET_STATEMENT_TIMEOUT_SQL = "SELECT set_config('statement_timeout', $1, true)"

CUSTOMERS_BY_EMAIL_SQL = """
SELECT id, name, account_status, tier
FROM customer
WHERE organization_id = $1 AND lower(email) = lower($2)
ORDER BY id
LIMIT 2
"""

ORDER_BY_NUMBER_SQL = """
SELECT order_number, status, total, placed_at, shipped_at
FROM "order"
WHERE organization_id = $1 AND customer_id = $2 AND order_number = $3
ORDER BY placed_at DESC, id DESC
LIMIT 1
"""

TICKET_BY_NUMBER_SQL = """
SELECT ticket_number, status, priority, subject, opened_at
FROM ticket
WHERE organization_id = $1 AND customer_id = $2 AND ticket_number = $3
ORDER BY opened_at DESC, id DESC
LIMIT 1
"""

RECENT_ORDERS_SQL = """
SELECT order_number, status, total, placed_at, shipped_at
FROM "order"
WHERE organization_id = $1 AND customer_id = $2
ORDER BY placed_at DESC, order_number COLLATE "C" DESC
LIMIT $3
"""

OPEN_TICKETS_SQL = """
SELECT ticket_number, status, priority, subject, opened_at
FROM ticket
WHERE organization_id = $1 AND customer_id = $2
  AND lower(status) NOT IN ('closed', 'resolved')
ORDER BY opened_at DESC, ticket_number COLLATE "C" DESC
LIMIT $3
"""

TENANT_SCOPED_QUERIES: tuple[str, ...] = (
    CUSTOMERS_BY_EMAIL_SQL,
    ORDER_BY_NUMBER_SQL,
    TICKET_BY_NUMBER_SQL,
    RECENT_ORDERS_SQL,
    OPEN_TICKETS_SQL,
)
"""Every business query; a unit test asserts each is scoped (R13.4, CLAUDE.md §4)."""


class _ConnectionLookups:
    """CustomerLookups bound to one connection inside the provider's transaction."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def customers_by_email(self, organization_id: UUID, sender_email: str) -> list[Row]:
        rows = await self._conn.fetch(CUSTOMERS_BY_EMAIL_SQL, organization_id, sender_email)
        return [dict(r) for r in rows]

    async def order_by_number(
        self, organization_id: UUID, customer_id: UUID, order_number: str
    ) -> Row | None:
        row = await self._conn.fetchrow(
            ORDER_BY_NUMBER_SQL, organization_id, customer_id, order_number
        )
        return None if row is None else dict(row)

    async def ticket_by_number(
        self, organization_id: UUID, customer_id: UUID, ticket_number: str
    ) -> Row | None:
        row = await self._conn.fetchrow(
            TICKET_BY_NUMBER_SQL, organization_id, customer_id, ticket_number
        )
        return None if row is None else dict(row)

    async def recent_orders(
        self, organization_id: UUID, customer_id: UUID, limit: int
    ) -> list[Row]:
        rows = await self._conn.fetch(RECENT_ORDERS_SQL, organization_id, customer_id, limit)
        return [dict(r) for r in rows]

    async def open_tickets(self, organization_id: UUID, customer_id: UUID, limit: int) -> list[Row]:
        rows = await self._conn.fetch(OPEN_TICKETS_SQL, organization_id, customer_id, limit)
        return [dict(r) for r in rows]


class PostgresBusinessDataProvider:
    """Business lookups against the local PostgreSQL business subsystem."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        snapshot_orders: int = 3,
        snapshot_tickets: int = 3,
        statement_timeout_ms: int = 500,
    ) -> None:
        if statement_timeout_ms < 1:
            raise ValueError("statement_timeout_ms must be >= 1")
        self._pool = pool
        self.snapshot_orders = snapshot_orders
        self.snapshot_tickets = snapshot_tickets
        self.statement_timeout_ms = statement_timeout_ms

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.fetchval(SET_STATEMENT_TIMEOUT_SQL, str(self.statement_timeout_ms))
            return await assemble_business_context(
                _ConnectionLookups(conn),
                organization_id=organization_id,
                sender_email=sender_email,
                plan=plan,
                snapshot_orders=self.snapshot_orders,
                snapshot_tickets=self.snapshot_tickets,
                as_of=datetime.now(UTC),
            )
```

- [ ] **Step 13: Run the PostgreSQL suite and the unit tests**

Run: `uv run pytest tests/integration/test_business_provider_postgres.py -v`
Expected: PASS (14 contract tests + 3 provider tests).

Run: `uv run pytest tests/unit/test_domain_business.py tests/unit/test_business_provider_contract.py tests/unit/test_dependency_rules.py -v`
Expected: PASS.

- [ ] **Step 14: Lint, format and type-check**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy packages services tests evaluation`
Expected: no findings.

- [ ] **Step 15: Commit the providers**

```bash
git add packages/business/protocol.py packages/business/assemble.py packages/business/memory.py packages/business/postgres.py packages/business/testing.py tests/unit/test_business_provider_contract.py tests/integration/test_business_provider_postgres.py
git commit -m "feat(business): provider protocol with in-memory and Postgres implementations [task 5.2] [R13.2, R13.4, R13.7]" \
  -m "get_business_context(organization_id, sender_email, plan) resolves the sender first and runs fixed, organization-scoped queries in one read-only transaction with a local statement_timeout. The rules live once in assemble.py; one contract suite runs against both implementations. Not wired into the ai-worker yet (5.4)." \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Sender → customer resolution & scoping proof [tasks.md 5.3]

**Files:**
- Modify: `packages/business/testing.py` (insert the three-tenant dataset block after `fact_rows()`, i.e. directly above `class BusinessDataProviderContractSuite(ABC):`; append 8 test methods at the end of that class; append `check_business_tenant_fixtures` at the end of the module)
- Modify: `tests/unit/test_business_provider_contract.py` (replace the import block; append two module-level tests)
- Modify: `tests/integration/test_business_provider_postgres.py` (two imports; one test over the seeded 5.1 fixtures)
- Test (create): `tests/unit/test_business_postgres_sql.py`
- Production code: none expected. Task 3 implemented the rules; this task proves them on three tenants and on the real seeded fixtures. If a test here fails, fix `assemble.py` / `memory.py` / `postgres.py`, never the assertion.
- Docs: none. The identity assumption is already recorded in `docs/adr/0008-business-data-fetch-plan.md` ("## Identity assumption", line 33). No verification control is added (CLAUDE.md §6).

**Interfaces:**
- Consumes: `BusinessDataProviderContractSuite`, `BusinessDataset`, `T0`, `order_ref`, `ticket_ref`, `fact_rows` (Task 3); `InMemoryBusinessDataProvider`; `PostgresBusinessDataProvider` and its `TENANT_SCOPED_QUERIES`, `CUSTOMERS_BY_EMAIL_SQL`, `ORDER_BY_NUMBER_SQL`; fixtures `BUSINESS_CUSTOMERS`, `BUSINESS_ORDERS`, `BUSINESS_TICKETS`, `DEMO_ORG_ID`, and Task 2's fixed-id 3-tenant set `BUSINESS_TENANT_CUSTOMERS`, `BUSINESS_TENANT_ORDERS`, `BUSINESS_TENANT_TICKETS`, `BIZ_HARBOR_ORG_ID`, `BIZ_SUMMIT_ORG_ID`, `BIZ_DELTA_ORG_ID`, `SHARED_CUSTOMER_EMAIL`, `AMBIGUOUS_CUSTOMER_EMAIL` from `packages.db.fixtures`; `seed_business_tenant_fixtures(pool)` from `packages.db.seed`.
- Produces: `SHARED_EMAIL`, `TWIN_EMAIL`, `RIVAL_EMAIL`, `SHARED_ORDER`, `@dataclass(frozen=True) class MultiTenantBusinessData(dataset, org_a, org_b, org_c)`, `def build_multi_tenant_dataset() -> MultiTenantBusinessData`, `async def check_business_tenant_fixtures(provider: BusinessDataProvider) -> None` in `packages/business/testing.py`.

Two 3-tenant datasets serve different purposes. `build_multi_tenant_dataset()` makes fresh `uuid4()` ids on every call, so the contract suite can insert it into the shared integration database once per test. Task 2's fixed-id fixtures are the ones tasks.md 5.1 asks for, and `check_business_tenant_fixtures` runs the same scoping checks over them in memory and on PostgreSQL (tasks.md 5.3: "Integration tests on real Postgres over the ≥3-tenant fixtures from 5.1").

```
            tenant A                      tenant B                    tenant C
 pat.buyer@example.com  Pat Alpha   Pat.Buyer@Example.COM Pat Beta   pat.buyer@example.com Pat Gamma
   ORD-50001 shipped                  ORD-50001 cancelled             ORD-50002 delivered
   TICK-7001 open
 rival@example.com  Rival Alpha     (no rival)                       twin@example.com  Twin Gamma
   ORD-60002, TICK-7002
 twin@example.com ×2  (ambiguous)
```

- [ ] **Step 1: Write the scoping tests**

In `packages/business/testing.py`, insert directly above `class BusinessDataProviderContractSuite(ABC):`:

```python
SHARED_EMAIL = "pat.buyer@example.com"
"""One address that is a different customer in each of the three tenants."""
TWIN_EMAIL = "twin@example.com"
"""Two customers in tenant A, one in tenant C."""
RIVAL_EMAIL = "rival@example.com"
"""A second customer of tenant A only."""
SHARED_ORDER = "ORD-50001"
"""One order number stored in tenants A and B for different customers."""


@dataclass(frozen=True)
class MultiTenantBusinessData:
    """Three tenants with overlapping customer emails and order numbers (CLAUDE.md §8)."""

    dataset: BusinessDataset
    org_a: UUID
    org_b: UUID
    org_c: UUID


def build_multi_tenant_dataset() -> MultiTenantBusinessData:
    """Fresh ids on every call, so it can be inserted into a database that is not reset.

    Tenant A: Pat (ORD-50001 shipped, TICK-7001 open); Rival (ORD-60002, TICK-7002);
              two customers sharing TWIN_EMAIL.
    Tenant B: Pat stored in mixed case (ORD-50001 cancelled). No Rival.
    Tenant C: Pat with only ORD-50002; one TWIN_EMAIL customer.
    """
    org_a, org_b, org_c = uuid4(), uuid4(), uuid4()
    data = BusinessDataset()

    pat_a = data.add_customer(org_a, SHARED_EMAIL, "Pat Alpha")
    data.add_order(org_a, pat_a, SHARED_ORDER, "shipped", placed_at=T0)
    data.add_ticket(org_a, pat_a, "TICK-7001", "open")
    rival_a = data.add_customer(org_a, RIVAL_EMAIL, "Rival Alpha")
    data.add_order(org_a, rival_a, "ORD-60002", "processing", placed_at=T0 + timedelta(days=1))
    data.add_ticket(org_a, rival_a, "TICK-7002", "open", opened_at=T0 + timedelta(days=1))
    data.add_customer(org_a, TWIN_EMAIL, "Twin One")
    data.add_customer(org_a, TWIN_EMAIL.upper(), "Twin Two")

    pat_b = data.add_customer(org_b, "Pat.Buyer@Example.COM", "Pat Beta")
    data.add_order(org_b, pat_b, SHARED_ORDER, "cancelled", placed_at=T0)

    pat_c = data.add_customer(org_c, SHARED_EMAIL, "Pat Gamma")
    data.add_order(org_c, pat_c, "ORD-50002", "delivered", placed_at=T0)
    data.add_customer(org_c, TWIN_EMAIL, "Twin Gamma")

    return MultiTenantBusinessData(dataset=data, org_a=org_a, org_b=org_b, org_c=org_c)
```

Append at the end of `class BusinessDataProviderContractSuite` (after `test_empty_plan_resolves_customer_and_returns_no_facts`):

```python
    # --- Sender resolution and scoping over three tenants (task 5.3, R13.4) -----------

    async def test_sender_match_ignores_case_and_surrounding_space(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(mt.org_b, "  PAT.BUYER@example.com ", FetchPlan())

        assert ctx.customer_status is CustomerStatus.FOUND
        assert ("name", "Pat Beta") in ctx.customer

    async def test_sender_match_is_on_the_whole_address(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        for near_miss in ("at.buyer@example.com", "pat.buyer@example.co", "pat.buyer", ""):
            ctx = await provider.get_business_context(mt.org_a, near_miss, FetchPlan())
            assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER, near_miss

    async def test_same_email_resolves_to_each_tenants_own_customer_and_orders(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)
        plan = FetchPlan(refs=(order_ref(SHARED_ORDER),))

        seen = {}
        for org in (mt.org_a, mt.org_b, mt.org_c):
            ctx = await provider.get_business_context(org, SHARED_EMAIL, plan)
            assert ctx.customer_status is CustomerStatus.FOUND
            fact = ctx.facts[0]
            seen[org] = (
                dict(ctx.customer)["name"],
                fact.status,
                dict(fact.attributes).get("status"),
            )

        assert seen == {
            mt.org_a: ("Pat Alpha", FactStatus.FOUND, "shipped"),
            mt.org_b: ("Pat Beta", FactStatus.FOUND, "cancelled"),
            mt.org_c: ("Pat Gamma", FactStatus.NOT_FOUND, None),
        }

    async def test_another_customers_order_and_ticket_are_not_found(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(
            mt.org_a,
            SHARED_EMAIL,
            FetchPlan(refs=(order_ref("ORD-60002"), ticket_ref("TICK-7002"))),
        )

        assert ctx.customer_status is CustomerStatus.FOUND
        assert fact_rows(ctx) == [
            ("order", "ORD-60002", "NOT_FOUND", None),
            ("ticket", "TICK-7002", "NOT_FOUND", None),
        ]
        assert all(f.attributes == () for f in ctx.facts)

    async def test_ambiguous_in_one_tenant_is_found_in_another(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)
        plan = FetchPlan(refs=(order_ref(SHARED_ORDER),))

        in_a = await provider.get_business_context(mt.org_a, TWIN_EMAIL, plan)
        in_c = await provider.get_business_context(mt.org_c, TWIN_EMAIL, plan)

        assert in_a.customer_status is CustomerStatus.AMBIGUOUS_CUSTOMER
        assert fact_rows(in_a) == [("order", SHARED_ORDER, "NOT_LOOKED_UP", "ambiguous_customer")]
        assert in_c.customer_status is CustomerStatus.FOUND
        assert ("name", "Twin Gamma") in in_c.customer
        assert fact_rows(in_c) == [("order", SHARED_ORDER, "NOT_FOUND", None)]

    async def test_customer_of_another_tenant_is_an_unknown_sender(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(
            mt.org_b,
            RIVAL_EMAIL,
            FetchPlan(refs=(order_ref("ORD-60002"),), snapshot=frozenset({EntityType.ORDER})),
        )

        assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER
        assert fact_rows(ctx) == [
            ("order", "ORD-60002", "NOT_LOOKED_UP", "unknown_sender"),
            ("order", None, "NOT_LOOKED_UP", "unknown_sender"),
        ]

    async def test_snapshot_holds_only_the_senders_rows_in_the_senders_tenant(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)
        plan = FetchPlan(snapshot=frozenset({EntityType.ORDER, EntityType.TICKET}))

        in_a = await provider.get_business_context(mt.org_a, SHARED_EMAIL, plan)
        in_b = await provider.get_business_context(mt.org_b, SHARED_EMAIL, plan)

        assert fact_rows(in_a) == [
            ("order", SHARED_ORDER, "FOUND", None),
            ("ticket", "TICK-7001", "FOUND", None),
        ]
        assert fact_rows(in_b) == [
            ("order", SHARED_ORDER, "FOUND", None),
            ("ticket", None, "NOT_FOUND", None),
        ]
        assert dict(in_b.facts[0].attributes)["status"] == "cancelled"

    async def test_unknown_organization_resolves_nobody(self) -> None:
        mt = build_multi_tenant_dataset()
        provider = await self.make_provider(mt.dataset)

        ctx = await provider.get_business_context(
            uuid4(), SHARED_EMAIL, FetchPlan(refs=(order_ref(SHARED_ORDER),))
        )

        assert ctx.customer_status is CustomerStatus.UNKNOWN_SENDER
        assert fact_rows(ctx) == [("order", SHARED_ORDER, "NOT_LOOKED_UP", "unknown_sender")]
```

Append at the end of `packages/business/testing.py` (module level, after the class) the check over Task 2's fixed-id ≥3-tenant fixtures, and add `from packages.db.fixtures.business_tenants import AMBIGUOUS_CUSTOMER_EMAIL, BIZ_DELTA_ORG_ID, BIZ_HARBOR_ORG_ID, BIZ_SUMMIT_ORG_ID, SHARED_CUSTOMER_EMAIL` to its imports, after the `packages.business.protocol` import (ruff wraps it). It lives here so that the in-memory unit test and the PostgreSQL integration test run the same assertions. `business_tenants.py` is a pure data module, and no dependency rule forbids `packages.business` → `packages.db`.

```python
async def check_business_tenant_fixtures(provider: BusinessDataProvider) -> None:
    """Scoping over the 5.1 fixtures (packages/db/fixtures/business_tenants.py, R13.4).

    `provider` must hold BUSINESS_TENANT_CUSTOMERS / _ORDERS / _TICKETS. ORD-82915 exists in all
    three tenants for three different customers; Summit stores Alice's address in mixed case;
    two Delta customers share AMBIGUOUS_CUSTOMER_EMAIL; Delta's Alice has no orders or tickets.
    """
    plan = FetchPlan(refs=(order_ref("ORD-82915"),))

    harbor = await provider.get_business_context(BIZ_HARBOR_ORG_ID, SHARED_CUSTOMER_EMAIL, plan)
    assert harbor.customer_status is CustomerStatus.FOUND
    assert fact_rows(harbor) == [("order", "ORD-82915", "FOUND", None)]
    assert dict(harbor.facts[0].attributes)["status"] == "shipped"

    summit = await provider.get_business_context(
        BIZ_SUMMIT_ORG_ID, SHARED_CUSTOMER_EMAIL.upper(), plan
    )
    assert summit.customer_status is CustomerStatus.FOUND
    assert dict(summit.facts[0].attributes)["status"] == "processing"

    delta = await provider.get_business_context(BIZ_DELTA_ORG_ID, SHARED_CUSTOMER_EMAIL, plan)
    assert delta.customer_status is CustomerStatus.FOUND
    assert fact_rows(delta) == [("order", "ORD-82915", "NOT_FOUND", None)]  # Brightline's order

    ambiguous = await provider.get_business_context(
        BIZ_DELTA_ORG_ID, AMBIGUOUS_CUSTOMER_EMAIL, plan
    )
    assert ambiguous.customer_status is CustomerStatus.AMBIGUOUS_CUSTOMER
    assert fact_rows(ambiguous) == [
        ("order", "ORD-82915", "NOT_LOOKED_UP", "ambiguous_customer")
    ]

    snapshot = FetchPlan(snapshot=frozenset({EntityType.ORDER, EntityType.TICKET}))
    harbor_snapshot = await provider.get_business_context(
        BIZ_HARBOR_ORG_ID, SHARED_CUSTOMER_EMAIL, snapshot
    )
    assert fact_rows(harbor_snapshot) == [  # 4 orders, limit 3; closed/resolved tickets skipped
        ("order", "ORD-82915", "FOUND", None),
        ("order", "ORD-7003", "FOUND", None),
        ("order", "ORD-7002", "FOUND", None),
        ("ticket", "TICK-4402", "FOUND", None),
    ]
    empty_snapshot = await provider.get_business_context(
        BIZ_DELTA_ORG_ID, SHARED_CUSTOMER_EMAIL, snapshot
    )
    assert fact_rows(empty_snapshot) == [  # a customer with zero orders and tickets
        ("order", None, "NOT_FOUND", None),
        ("ticket", None, "NOT_FOUND", None),
    ]
```

In `tests/unit/test_business_provider_contract.py`, replace the line `from packages.business.testing import BusinessDataProviderContractSuite, BusinessDataset` with:

```python
from packages.business.testing import (
    BusinessDataProviderContractSuite,
    BusinessDataset,
    check_business_tenant_fixtures,
    fact_rows,
    order_ref,
    ticket_ref,
)
from packages.db.fixtures import (
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDERS,
    BUSINESS_TENANT_CUSTOMERS,
    BUSINESS_TENANT_ORDERS,
    BUSINESS_TENANT_TICKETS,
    BUSINESS_TICKETS,
    DEMO_ORG_ID,
)
from packages.domain.business import CustomerStatus, FetchPlan
```

and append these two tests at the end of the file:

```python
async def test_seeded_fixtures_keep_the_cross_customer_case_not_found() -> None:
    """Edward's fixture email cites Dana's ORD-9901 and his own TICK-4402 (tasks.md 5.1/5.3)."""
    provider = InMemoryBusinessDataProvider(BUSINESS_CUSTOMERS, BUSINESS_ORDERS, BUSINESS_TICKETS)

    ctx = await provider.get_business_context(
        DEMO_ORG_ID,
        "Edward.Norton@FightClub.org",
        FetchPlan(refs=(order_ref("ORD-9901"), ticket_ref("TICK-4402"))),
    )

    assert ctx.customer_status is CustomerStatus.FOUND
    assert fact_rows(ctx) == [
        ("order", "ORD-9901", "NOT_FOUND", None),
        ("ticket", "TICK-4402", "FOUND", None),
    ]


async def test_seeded_business_tenant_fixtures_are_scoped_per_tenant() -> None:
    """tasks.md 5.3 over the 5.1 fixtures, in memory; PostgreSQL runs the same check."""
    await check_business_tenant_fixtures(
        InMemoryBusinessDataProvider(
            BUSINESS_TENANT_CUSTOMERS, BUSINESS_TENANT_ORDERS, BUSINESS_TENANT_TICKETS
        )
    )
```

In `tests/integration/test_business_provider_postgres.py`, add `check_business_tenant_fixtures` to the `packages.business.testing` import, add `from packages.db.seed import seed_business_tenant_fixtures` after the `packages.db.connection` import, and append:

```python
async def test_seeded_business_tenant_fixtures_are_scoped_on_postgres(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """tasks.md 5.3: the 5.1 ≥3-tenant fixtures, loaded by the seed helper, on real PostgreSQL."""
    await seed_business_tenant_fixtures(db_pool)  # idempotent upserts by fixed id
    await check_business_tenant_fixtures(PostgresBusinessDataProvider(db_pool))
```

Create `tests/unit/test_business_postgres_sql.py`:

```python
"""Static scoping proof for PostgresBusinessDataProvider's fixed queries (R13.4, CLAUDE.md §4)."""

from __future__ import annotations

import re

import pytest

from packages.business import postgres
from packages.business.postgres import TENANT_SCOPED_QUERIES


def test_every_sql_constant_is_listed_as_tenant_scoped() -> None:
    constants = {
        name
        for name, value in vars(postgres).items()
        if name.endswith("_SQL") and name != "SET_STATEMENT_TIMEOUT_SQL" and isinstance(value, str)
    }
    listed = {name for name, value in vars(postgres).items() if value in TENANT_SCOPED_QUERIES}
    assert constants == listed
    assert len(TENANT_SCOPED_QUERIES) == 5


@pytest.mark.parametrize("sql", TENANT_SCOPED_QUERIES)
def test_query_filters_on_organization_first(sql: str) -> None:
    where = re.search(r"WHERE\s+(.*?)(ORDER BY|LIMIT|$)", sql, re.S)
    assert where is not None
    assert where.group(1).strip().startswith("organization_id = $1")


@pytest.mark.parametrize("sql", TENANT_SCOPED_QUERIES[1:])
def test_entity_queries_are_scoped_to_the_resolved_customer(sql: str) -> None:
    assert "customer_id = $2" in sql


def test_sender_match_is_case_insensitive_on_the_whole_address() -> None:
    assert "lower(email) = lower($2)" in postgres.CUSTOMERS_BY_EMAIL_SQL
    assert "LIKE" not in postgres.CUSTOMERS_BY_EMAIL_SQL.upper()
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/unit/test_business_provider_contract.py tests/unit/test_business_postgres_sql.py -v`
Expected: PASS (24 contract/fixture tests + 11 SQL tests). This is a proof task: Task 3 already implemented the rules, so the tests are expected to pass first time. Step 3 shows they fail when the rules are broken.

Run: `uv run pytest tests/integration/test_business_provider_postgres.py -v`
Expected: PASS (22 contract tests over three tenants on real PostgreSQL + 3 provider tests + 1 seeded-fixture test).

- [ ] **Step 3: Prove the tests have teeth (mutation check, then revert)**

Temporarily change `ORDER_BY_NUMBER_SQL` in `packages/business/postgres.py` from `WHERE organization_id = $1 AND customer_id = $2 AND order_number = $3` to `WHERE organization_id = $1 AND order_number = $3 AND $2::uuid IS NOT NULL`, and `CUSTOMERS_BY_EMAIL_SQL` from `lower(email) = lower($2)` to `email = $2`.

Run: `uv run pytest tests/unit/test_business_postgres_sql.py tests/integration/test_business_provider_postgres.py -v`
Expected: 9 FAILED (8 verified in the scratch run, plus the seeded-fixture test added here): `test_seeded_business_tenant_fixtures_are_scoped_on_postgres` (Summit's upper-case lookup and Delta's order scoping), `test_entity_queries_are_scoped_to_the_resolved_customer[...ORDER_BY_NUMBER_SQL...]`, `test_sender_match_is_case_insensitive_on_the_whole_address`, and on PostgreSQL `test_ambiguous_customer_marks_every_planned_fact_not_looked_up`, `test_sender_match_ignores_case_and_surrounding_space`, `test_same_email_resolves_to_each_tenants_own_customer_and_orders`, `test_another_customers_order_and_ticket_are_not_found` (ORD-60002 comes back FOUND), `test_ambiguous_in_one_tenant_is_found_in_another`, `test_snapshot_holds_only_the_senders_rows_in_the_senders_tenant`.

Revert: `git checkout -- packages/business/postgres.py`
Run: `uv run pytest tests/unit/test_business_postgres_sql.py tests/integration/test_business_provider_postgres.py -v`
Expected: PASS.

- [ ] **Step 4: Lint, format and type-check**

Run: `uv run ruff format packages/business/testing.py tests/unit/test_business_provider_contract.py tests/unit/test_business_postgres_sql.py tests/integration/test_business_provider_postgres.py` (the insertions into `packages/business/testing.py` are not in ruff-format style as written).
Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy packages services tests evaluation`
Expected: no findings.

- [ ] **Step 5: Commit**

```bash
git add packages/business/testing.py tests/unit/test_business_provider_contract.py tests/unit/test_business_postgres_sql.py tests/integration/test_business_provider_postgres.py
git commit -m "test(business): prove sender resolution and customer scoping over three tenants [task 5.3] [R13.4]" \
  -m "The contract suite gains a three-tenant dataset with one address per tenant, one shared order number, a same-org ambiguous address and a second customer. Both providers must match the whole address ignoring case, return UNKNOWN_SENDER / AMBIGUOUS_CUSTOMER with NOT_LOOKED_UP facts, and report another customer's order as NOT_FOUND. A static test pins organization_id = \$1 and customer_id = \$2 in every fixed query." \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

**Overview of Tasks 5–7 (5.4, 5.5).**

**Preconditions for Tasks 5–7.** Tasks 3 and 4 (5.2, 5.3) are committed. They provide, with the binding contract names:
`packages/domain/business.py` (`CustomerStatus`, `FactStatus`, `EntityType`, `NotLookedUpReason`, `EntityRef`, `FetchPlan`, `BusinessFact`, `BusinessContext` with `render()` / `to_payload()`, `unavailable_context`),
`packages/business/protocol.py` (`BusinessDataProvider`), `packages/business/memory.py` (`InMemoryBusinessDataProvider`) and `packages/business/postgres.py` (`PostgresBusinessDataProvider`).
Task 2 (5.1) seeded `ORD-82915` for Alice. Most tests here seed their own rows; `test_sender_case_and_display_name_reach_the_provider_as_the_bare_address` and `test_invoice_reference_reaches_the_context_as_not_looked_up` read the Task 2 fixtures.

How the pieces meet after Task 7:

```
NormalizedMessage ──subject+body──▶ extract_entity_refs ─┐
Classification.category ─▶ registry.resolve_profile ─────┼─▶ build_fetch_plan ─▶ FetchPlan
Classification.intent ──────────────────────────────────┘          │
                                                                   ▼
                                  fetch_business_context(provider, plan, timeout_ms, metrics)
                                  empty plan ─▶ None (log line only, no call)
                                  wait_for(provider.get_business_context) ─ ok ─▶ BusinessContext
                                                         └─ timeout/error ─▶ unavailable_context (degraded)
                                        │ span business.fetch · business_lookups_total · latency · log business_fetch
                                        ▼
                  ContextPackage.business_data (section 7) ──▶ prompts/*.v2.j2 {{ business_data.render() }}
                  CONTEXT_READY payload: business_plan, customer_status, business_fact_statuses, business_data_degraded
```

---

### Task 5: Typed entity extractor and pure fetch plan [tasks.md 5.4 — part a]

**Files:**
- Create: `packages/business/identifiers.py`
- Create: `packages/business/plan.py`
- Test: `tests/unit/test_business_identifiers.py` (new)
- Test: `tests/unit/test_business_plan.py` (new)

**Interfaces:**
- Consumes: `packages.domain.business.EntityRef`, `EntityType`, `FetchPlan` (Task 3); `packages.llm.profile.ContextPolicy` (existing StrEnum, `packages/llm/profile.py:32`).
- Produces:
  - `def extract_entity_refs(text: str) -> tuple[EntityRef, ...]` — deduplicated, in order of first appearance.
  - `INTENT_ENTITIES: Mapping[str, frozenset[EntityType]]` — read-only (`MappingProxyType`).
  - `def build_fetch_plan(*, subject: str, body: str, context_policy: str, intent: str | None) -> FetchPlan`

Rules implemented (design §5.4): prefixed `ORD-`/`ORDER-`, `TICK-`/`TICKET-`, `INV-<digits>[-<digits>…]`; bare `order`/`ticket` + optional `#`, `no.`, `number` + at least 4 digits; case-insensitive; orders normalise to `ORD-<digits>`, tickets to `TICK-<digits>`, invoices to upper-case `INV-…`. Typed IDs are always planned. The snapshot is `{ORDER, TICKET}` when `context_policy == thread_plus_rag_plus_business`, else the `INTENT_ENTITIES` entry for the intent, else nothing. `INV-` refs are planned so the provider records them as `NOT_LOOKED_UP` (`unsupported_entity`, Task 3); the plan never marks a status itself.

- [ ] **Step 1: Write the failing extractor tests**

Create `tests/unit/test_business_identifiers.py`:

```python
"""Typed business-reference extractor (R13.3, design.md §5.4, ADR-0008).

Requirements: R13.3. The extractor keeps the entity type, normalises to the stored
`order_number` / `ticket_number` format and runs on every job.
"""

from __future__ import annotations

import pytest

from packages.business.identifiers import extract_entity_refs
from packages.domain.business import EntityRef, EntityType


def _order(reference: str) -> EntityRef:
    return EntityRef(entity=EntityType.ORDER, reference=reference)


def _ticket(reference: str) -> EntityRef:
    return EntityRef(entity=EntityType.TICKET, reference=reference)


def _invoice(reference: str) -> EntityRef:
    return EntityRef(entity=EntityType.INVOICE, reference=reference)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What is the status of order 82915?", _order("ORD-82915")),
        ("Where is my order #82915?", _order("ORD-82915")),
        ("Where is my order#82915?", _order("ORD-82915")),
        ("Checking order no. 82915 please", _order("ORD-82915")),
        ("Order Number 82915 has not arrived", _order("ORD-82915")),
        ("Reference ORDER-58192 from the proposal", _order("ORD-58192")),
        ("lower-case ord-9901 works", _order("ORD-9901")),
        ("Status for ORD-9901", _order("ORD-9901")),
        ("Escalated as TICKET-4402", _ticket("TICK-4402")),
        ("See tick-4402", _ticket("TICK-4402")),
        ("My ticket number 4402 is still open", _ticket("TICK-4402")),
        ("Following up on ticket #4402.", _ticket("TICK-4402")),
        ("Discrepancy on Invoice INV-2026-8891", _invoice("INV-2026-8891")),
        ("proposal example inv-2026-01829", _invoice("INV-2026-01829")),
    ],
)
def test_each_accepted_form_is_typed_and_normalised(text: str, expected: EntityRef) -> None:
    assert extract_entity_refs(text) == (expected,)


@pytest.mark.parametrize(
    "text",
    [
        "We changed the config in order to fix the outage.",
        "I placed an order 2 days ago and nothing happened.",
        "This is ticket 3 of 5 in the batch.",
        "order 123 is too short to be an order number",
        "",
    ],
)
def test_prose_and_short_numbers_do_not_match(text: str) -> None:
    assert extract_entity_refs(text) == ()


def test_refs_are_deduplicated_in_order_of_first_appearance() -> None:
    text = (
        "TICK-4402 is about ORD-9901. To repeat: order 9901 and ticket #4402. "
        "Also invoice INV-2026-8891."
    )
    assert extract_entity_refs(text) == (
        _ticket("TICK-4402"),
        _order("ORD-9901"),
        _invoice("INV-2026-8891"),
    )


def test_fixture_subject_with_prefixed_refs_after_the_words() -> None:
    """Edward's fixture subject: the bare pattern must not fire on 'Order ORD-9901'."""
    subject = "Status update for Order ORD-9901 and Ticket TICK-4402"
    assert extract_entity_refs(subject) == (_order("ORD-9901"), _ticket("TICK-4402"))
```

- [ ] **Step 2: Write the failing plan tests**

Create `tests/unit/test_business_plan.py`:

```python
"""Pure FetchPlan builder (R13.3, design.md §5.4, ADR-0008).

Requirements: R13.3. Typed IDs are always planned, the routed profile's context_policy gates
the full snapshot, INTENT_ENTITIES maps a few intents to entities, nothing planned is empty.
"""

from __future__ import annotations

from typing import cast

import pytest

from packages.business.plan import INTENT_ENTITIES, build_fetch_plan
from packages.domain.business import EntityRef, EntityType, FetchPlan
from packages.llm.profile import ContextPolicy

NON_BUSINESS = ContextPolicy.THREAD_PLUS_RAG.value
BUSINESS = ContextPolicy.THREAD_PLUS_RAG_PLUS_BUSINESS.value


def test_typed_ids_override_a_non_business_policy() -> None:
    plan = build_fetch_plan(
        subject="Quick question",
        body="Where is my order #82915?",
        context_policy=NON_BUSINESS,
        intent=None,
    )
    assert plan == FetchPlan(refs=(EntityRef(entity=EntityType.ORDER, reference="ORD-82915"),))
    assert not plan.is_empty


def test_business_policy_plans_the_orders_and_tickets_snapshot() -> None:
    plan = build_fetch_plan(
        subject="My account",
        body="Could you check my account? I think I was charged twice.",
        context_policy=BUSINESS,
        intent=None,
    )
    assert plan.refs == ()
    assert plan.snapshot == frozenset({EntityType.ORDER, EntityType.TICKET})


@pytest.mark.parametrize(
    "intent",
    ["invoice_inquiry", "receipt_lookup", "payment_failure", "refund_request", " Refund_Request "],
)
def test_mapped_intent_plans_only_its_entities(intent: str) -> None:
    plan = build_fetch_plan(
        subject="Help", body="I need help with this.", context_policy=NON_BUSINESS, intent=intent
    )
    assert plan.refs == ()
    assert plan.snapshot == frozenset({EntityType.ORDER})


def test_business_policy_wins_over_the_intent_map() -> None:
    plan = build_fetch_plan(
        subject="Invoice", body="Question about my bill.", context_policy=BUSINESS,
        intent="invoice_inquiry",
    )
    assert plan.snapshot == frozenset({EntityType.ORDER, EntityType.TICKET})


def test_nothing_planned_is_empty() -> None:
    plan = build_fetch_plan(
        subject="Thanks",
        body="Thank you, in order to close this out we are done.",
        context_policy=NON_BUSINESS,
        intent="thank_you",
    )
    assert plan.is_empty
    assert plan == FetchPlan()


def test_unmapped_intent_and_missing_intent_plan_no_snapshot() -> None:
    for intent in (None, "", "general_question"):
        plan = build_fetch_plan(
            subject="Hi", body="Hello there.", context_policy=NON_BUSINESS, intent=intent
        )
        assert plan.is_empty


def test_invoice_reference_is_planned_for_the_provider_to_mark_unsupported() -> None:
    plan = build_fetch_plan(
        subject="Discrepancy on Invoice INV-2026-8891",
        body="We were billed twice on invoice INV-2026-8891.",
        context_policy=NON_BUSINESS,
        intent=None,
    )
    assert plan.refs == (EntityRef(entity=EntityType.INVOICE, reference="INV-2026-8891"),)
    assert plan.snapshot == frozenset()


def test_subject_and_body_are_both_scanned_and_deduplicated() -> None:
    plan = build_fetch_plan(
        subject="Status update for Order ORD-9901 and Ticket TICK-4402",
        body="Could you update me on ticket TICK-4402 and the replacement for order ORD-9901?",
        context_policy=NON_BUSINESS,
        intent=None,
    )
    assert plan.refs == (
        EntityRef(entity=EntityType.ORDER, reference="ORD-9901"),
        EntityRef(entity=EntityType.TICKET, reference="TICK-4402"),
    )


def test_intent_entities_matches_design_and_is_read_only() -> None:
    assert dict(INTENT_ENTITIES) == {
        "invoice_inquiry": frozenset({EntityType.ORDER}),
        "receipt_lookup": frozenset({EntityType.ORDER}),
        "payment_failure": frozenset({EntityType.ORDER}),
        "refund_request": frozenset({EntityType.ORDER}),
    }
    with pytest.raises(TypeError):
        cast(dict[str, frozenset[EntityType]], INTENT_ENTITIES)["order_status"] = frozenset()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_business_identifiers.py tests/unit/test_business_plan.py -v`
Expected: FAIL (collection error) with `ModuleNotFoundError: No module named 'packages.business.identifiers'` and `No module named 'packages.business.plan'`.

- [ ] **Step 4: Implement the extractor**

Create `packages/business/identifiers.py`:

```python
"""Typed business-reference extractor (R13.3, design.md §5.4, ADR-0008).

Separate from the retrieval query builder's untyped identifier regex, which stays as it is:
this extractor keeps the entity type and normalises each match to the stored number format
(`order_number` = ``ORD-<digits>``, `ticket_number` = ``TICK-<digits>``). It runs on every job,
independent of ``retrieval_required``.

Accepted forms, case-insensitive:
- prefixed: ``ORD-<digits>`` / ``ORDER-<digits>``, ``TICK-<digits>`` / ``TICKET-<digits>``,
  ``INV-<digits>[-<digits>...]``;
- bare: the word ``order`` or ``ticket``, optionally followed by ``#``, ``no.`` or ``number``,
  then at least 4 digits ("order 82915", "order #82915", "ticket number 4402").

"in order to", "an order 2 days ago" and "ticket 3 of 5" do not match.
"""

from __future__ import annotations

import re

from packages.domain.business import EntityRef, EntityType

ORDER_PREFIX = "ORD-"
TICKET_PREFIX = "TICK-"
INVOICE_PREFIX = "INV-"

_PREFIXED_ORDER = re.compile(r"\bORD(?:ER)?-(\d+)\b", re.IGNORECASE)
_PREFIXED_TICKET = re.compile(r"\bTICK(?:ET)?-(\d+)\b", re.IGNORECASE)
_INVOICE = re.compile(r"\bINV-(\d+(?:-\d+)*)\b", re.IGNORECASE)
_BARE = re.compile(
    r"\b(order|ticket)(?:\s+(?:no\.|number|#)\s*|\s*#\s*|\s+)(\d{4,})\b",
    re.IGNORECASE,
)


def extract_entity_refs(text: str) -> tuple[EntityRef, ...]:
    """Return typed, normalised references in order of first appearance, without duplicates."""
    found: list[tuple[int, EntityRef]] = []
    for match in _PREFIXED_ORDER.finditer(text):
        ref = EntityRef(entity=EntityType.ORDER, reference=f"{ORDER_PREFIX}{match.group(1)}")
        found.append((match.start(), ref))
    for match in _PREFIXED_TICKET.finditer(text):
        ref = EntityRef(entity=EntityType.TICKET, reference=f"{TICKET_PREFIX}{match.group(1)}")
        found.append((match.start(), ref))
    for match in _INVOICE.finditer(text):
        ref = EntityRef(entity=EntityType.INVOICE, reference=f"{INVOICE_PREFIX}{match.group(1)}")
        found.append((match.start(), ref))
    for match in _BARE.finditer(text):
        is_order = match.group(1).lower() == "order"
        entity = EntityType.ORDER if is_order else EntityType.TICKET
        prefix = ORDER_PREFIX if is_order else TICKET_PREFIX
        found.append((match.start(), EntityRef(entity=entity, reference=f"{prefix}{match.group(2)}")))

    found.sort(key=lambda item: item[0])
    seen: set[EntityRef] = set()
    refs: list[EntityRef] = []
    for _, ref in found:
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return tuple(refs)
```

- [ ] **Step 5: Implement the plan builder**

Create `packages/business/plan.py`:

```python
"""Pure FetchPlan builder: what business data to fetch, decided in code (R13.3, ADR-0008).

No I/O and no model call. Inputs are signals the pipeline already has (design.md §5.4):
1. typed IDs in subject + body are always planned (they override ``context_policy``);
2. snapshot: the routed profile's ``context_policy`` == ``thread_plus_rag_plus_business``
   plans orders + tickets; otherwise an intent in ``INTENT_ENTITIES`` plans only its entities;
3. nothing planned means an empty plan, so the caller makes no provider call.

``INV-`` references are planned like any typed ID; the provider records them as
``NOT_LOOKED_UP`` with reason ``unsupported_entity`` because there is no invoice table.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from packages.business.identifiers import extract_entity_refs
from packages.domain.business import EntityType, FetchPlan
from packages.llm.profile import ContextPolicy

_ORDERS_ONLY = frozenset({EntityType.ORDER})
_FULL_SNAPSHOT = frozenset({EntityType.ORDER, EntityType.TICKET})

INTENT_ENTITIES: Mapping[str, frozenset[EntityType]] = MappingProxyType(
    {
        "invoice_inquiry": _ORDERS_ONLY,
        "receipt_lookup": _ORDERS_ONLY,
        "payment_failure": _ORDERS_ONLY,
        "refund_request": _ORDERS_ONLY,
    }
)
"""Intents that name transactional entities without a typed ID (design.md §5.4)."""


def build_fetch_plan(
    *, subject: str, body: str, context_policy: str, intent: str | None
) -> FetchPlan:
    """Plan the lookups for one job from its text, profile policy and intent."""
    refs = extract_entity_refs(f"{subject}\n{body}")
    if context_policy == ContextPolicy.THREAD_PLUS_RAG_PLUS_BUSINESS:
        snapshot = _FULL_SNAPSHOT
    else:
        key = intent.strip().lower() if intent else ""
        snapshot = INTENT_ENTITIES.get(key, frozenset())
    return FetchPlan(refs=refs, snapshot=snapshot)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_business_identifiers.py tests/unit/test_business_plan.py tests/unit/test_dependency_rules.py -v`
Expected: PASS (all parametrized cases green; dependency rules still green — `packages/business` imports only `packages.domain` and `packages.llm`).

Run: `uv run ruff format packages/business tests/unit/test_business_identifiers.py tests/unit/test_business_plan.py && uv run ruff check packages/business tests/unit && uv run mypy packages services tests evaluation`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add packages/business/identifiers.py packages/business/plan.py \
  tests/unit/test_business_identifiers.py tests/unit/test_business_plan.py
git commit -m "$(cat <<'EOF'
feat(business): typed entity extractor and pure fetch plan [task 5.4] [R13.3]

Adds the typed ORD/TICK/INV extractor with the design §5.4 forms and negatives, and the
pure build_fetch_plan: typed IDs always planned, the profile's context_policy gates the
orders+tickets snapshot, INTENT_ENTITIES maps four intents to orders, nothing planned is
empty. No wiring yet.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Bounded fetch, settings, Context Builder + ai-worker wiring, observability [tasks.md 5.4 — part b]

**Files:**
- Create: `packages/business/fetch.py`
- Modify: `packages/core/settings.py` (add `BusinessDataSettings` after `RetrievalSettings`, ~line 285; add the `business_data` field to `AppSettings` after `agent_profiles`, ~line 680)
- Modify: `packages/observability/metrics.py` (bucket constant ~line 33; dataclass fields ~lines 101 and 118; constructor entries ~lines 300 and 362)
- Modify: `packages/domain/entities.py:151-205` (`ContextPackage.business_data` type and section 7, Step 9)
- Replace: `packages/context/builder.py` (whole file)
- Modify: `packages/context/__init__.py` (drop the old protocol/stub exports)
- Modify: `services/ai_worker/main.py:83-131`
- Modify: `docker-compose.yml` (x-app-env, after `RETRIEVAL__RETRIEVAL_TIMEOUT_MS`)
- Modify: `.env.example` (append section 20)
- Modify: `docs/configuration.md` (append §2.20)
- Modify: `docs/observability.md` (§2.2 table + new subsection before the `---` at ~line 132)
- Test: `tests/unit/test_business_fetch.py` (new), `tests/unit/test_context_builder_business.py` (new)
- Test (modify): `tests/unit/test_settings.py`, `tests/unit/test_observability_metrics.py:13-53`, `tests/unit/test_context_builder.py:1-78`, `tests/unit/test_ai_worker_main.py`, `tests/unit/test_runtime_image_contract.py:132-143`, `tests/unit/test_domain_entities.py:125-192`, `tests/unit/test_single_pass_generator.py:60`, `tests/unit/test_draft_repair_orchestration.py:67`, `tests/unit/test_complexity_router_integration.py:94`, `tests/unit/test_agent_profile.py:199-212`
- Test (modify): `tests/integration/test_context_builder_postgres.py` (imports + one new test)

**Interfaces:**
- Consumes: `build_fetch_plan` (Task 5); `BusinessDataProvider.get_business_context(organization_id, sender_email, plan)`, `PostgresBusinessDataProvider(pool, *, snapshot_orders, snapshot_tickets, statement_timeout_ms)`, `InMemoryBusinessDataProvider(customers=(), orders=(), tickets=(), *, snapshot_orders=3, snapshot_tickets=3)`, `unavailable_context(plan, as_of)`, `FetchPlan.to_payload()`, `BusinessContext.render()` (Task 3); `AgentProfileRegistry.resolve_profile(category: str | None)`; `trace_span`, `bind_log_context`, `PipelineMetrics`.
- Produces:
  - `async def fetch_business_context(provider: BusinessDataProvider, *, organization_id: UUID, sender_email: str, plan: FetchPlan, timeout_ms: int, metrics: PipelineMetrics | None = None) -> BusinessContext | None` (the `metrics` kwarg is an addition; see "Contract notes" under File Structure)
  - `def business_payload(plan: FetchPlan, context: BusinessContext | None) -> dict[str, Any]` → keys `business_plan`, `customer_status`, `business_fact_statuses`, `business_data_degraded`
  - `BUSINESS_FETCH_LOG_EVENT = "business_fetch"`, span name `"business.fetch"`
  - `class BusinessDataSettings(BaseModel)`: `timeout_ms: int = 500 (ge=10)`, `snapshot_orders: int = 3 (ge=0)`, `snapshot_tickets: int = 3 (ge=0)`; `AppSettings.business_data`
  - `PipelineMetrics.business_lookups_total: Counter` (labels `entity`, `status`), `PipelineMetrics.business_lookup_latency_ms: Histogram` (no labels, `BUSINESS_LOOKUP_BUCKETS`)
  - `ContextBuilder.__init__(..., profile_registry: AgentProfileRegistry | None = None, business_timeout_ms: int = 500, metrics: PipelineMetrics | None = None)`; `ContextPackage.business_data: BusinessContext | None = None`

- [ ] **Step 1: Write the failing settings and metrics tests**

In `tests/unit/test_settings.py`, add `BusinessDataSettings,` to the `from packages.core.settings import (...)` block (alphabetical, between `AppSettings,` and `DatabaseSettings,`), then append:

```python
def test_business_data_settings_defaults_and_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R13.7 / R20.6: the business_data group validates and reads BUSINESS_DATA__*."""
    defaults = AppSettings(_env_file=None).business_data
    assert (defaults.timeout_ms, defaults.snapshot_orders, defaults.snapshot_tickets) == (500, 3, 3)

    monkeypatch.setenv("BUSINESS_DATA__TIMEOUT_MS", "750")
    monkeypatch.setenv("BUSINESS_DATA__SNAPSHOT_ORDERS", "5")
    monkeypatch.setenv("BUSINESS_DATA__SNAPSHOT_TICKETS", "0")
    overridden = AppSettings(_env_file=None).business_data
    assert (overridden.timeout_ms, overridden.snapshot_orders, overridden.snapshot_tickets) == (
        750,
        5,
        0,
    )


def test_business_data_settings_reject_out_of_range_values() -> None:
    with pytest.raises(ValidationError, match="timeout_ms"):
        BusinessDataSettings(timeout_ms=5)
    with pytest.raises(ValidationError, match="snapshot_orders"):
        BusinessDataSettings(snapshot_orders=-1)
    with pytest.raises(ValidationError, match="snapshot_tickets"):
        BusinessDataSettings(snapshot_tickets=-1)
```

In `tests/unit/test_observability_metrics.py::test_metrics_registry_initialization_all_r21_metrics`, insert after `assert m.draft_validation_failures_total is not None`:

```python
    assert m.business_lookups_total is not None
```

and after `assert m.raw_payload_size_bytes is not None`:

```python
    assert m.business_lookup_latency_ms is not None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_settings.py tests/unit/test_observability_metrics.py -v`
Expected: FAIL — `ImportError: cannot import name 'BusinessDataSettings'` and `AttributeError: 'PipelineMetrics' object has no attribute 'business_lookups_total'`.

- [ ] **Step 3: Implement the settings group and the two instruments**

In `packages/core/settings.py`, insert after the `RetrievalSettings` class (after its `retrieval_timeout_ms` field, before `class TriageSettings`):

```python
class BusinessDataSettings(BaseModel):
    """Business-data lookup deadline and snapshot sizes (R13.4, R13.7, design.md §5.4)."""

    timeout_ms: int = Field(
        default=500,
        ge=10,
        description=(
            "Deadline for one business-data provider call in milliseconds; also the Postgres "
            "statement_timeout (R13.7)"
        ),
    )
    snapshot_orders: int = Field(
        default=3, ge=0, description="Most recent orders in a customer snapshot (design.md §5.4)"
    )
    snapshot_tickets: int = Field(
        default=3,
        ge=0,
        description="Open tickets (not closed or resolved) in a customer snapshot (design.md §5.4)",
    )
```

In `AppSettings`, insert after `agent_profiles: AgentProfileSettings = Field(default_factory=AgentProfileSettings)`:

```python
    business_data: BusinessDataSettings = Field(default_factory=BusinessDataSettings)
```

In `packages/observability/metrics.py`:

After the line `RERANK_BUCKETS = (10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)` add:

```python
# 500 is the default BUSINESS_DATA__TIMEOUT_MS, so a p95 against the deadline reads a real edge.
BUSINESS_LOOKUP_BUCKETS = (5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)
```

In the `PipelineMetrics` dataclass, after `    citation_mismatches_total: Counter` add `    business_lookups_total: Counter`, and after `    raw_payload_size_bytes: Histogram` add `    business_lookup_latency_ms: Histogram`.

In `create_pipeline_metrics`, after the `citation_mismatches_total=Counter(...)` entry (before `# Histograms (latency and calls per job)`) add:

```python
        business_lookups_total=Counter(
            "business_lookups_total",
            "Business facts produced per lookup, by entity and status (R13.6, R13.7)",
            ["entity", "status"],
            registry=reg,
        ),
```

and after the `raw_payload_size_bytes=Histogram(...)` entry (before `# Gauges`) add:

```python
        business_lookup_latency_ms=Histogram(
            "business_lookup_latency_ms",
            "Duration of one bounded business-data provider call in milliseconds (R13.7)",
            buckets=BUSINESS_LOOKUP_BUCKETS,
            registry=reg,
        ),
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/test_settings.py tests/unit/test_observability_metrics.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing fetch-wrapper tests**

Create `tests/unit/test_business_fetch.py`:

```python
"""Bounded business-data fetch: deadline, degradation, span, metrics, log line.

Requirements: R13.7 (timeout + recorded degradation), R21.3 (structured log line),
R13.6 (UNAVAILABLE never becomes NOT_FOUND). Design: design.md §5.4, ADR-0008.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span

import packages.business.fetch as fetch_module
from packages.business.fetch import (
    BUSINESS_FETCH_LOG_EVENT,
    business_payload,
    fetch_business_context,
)
from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
)
from packages.observability.context import bind_log_context
from packages.observability.logging import StructuredJSONFormatter
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from packages.observability.tracing import init_tracer
from packages.observability.tracing import trace_span as real_trace_span

ORDER_REF = EntityRef(entity=EntityType.ORDER, reference="ORD-82915")
TICKET_REF = EntityRef(entity=EntityType.TICKET, reference="TICK-4402")
PLAN = FetchPlan(refs=(ORDER_REF,))
AS_OF = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
SENDER = "alice.smith@clientcorp.com"
LOGGER = "packages.business.fetch"


def _found() -> BusinessContext:
    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"),),
        facts=(
            BusinessFact(
                entity=EntityType.ORDER,
                reference="ORD-82915",
                status=FactStatus.FOUND,
                attributes=(("status", "shipped"),),
            ),
        ),
    )


class SpyProvider:
    """Records calls; optionally sleeps or raises (a real async provider, no mock library)."""

    def __init__(
        self,
        result: BusinessContext | None = None,
        *,
        delay_s: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.delay_s = delay_s
        self.error = error
        self.calls: list[tuple[UUID, str, FetchPlan]] = []

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        self.calls.append((organization_id, sender_email, plan))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _lookups(metrics: PipelineMetrics, entity: str, status: str) -> float:
    value = metrics.registry.get_sample_value(
        "business_lookups_total", {"entity": entity, "status": status}
    )
    return value or 0.0


def _latency_count(metrics: PipelineMetrics) -> float:
    return metrics.registry.get_sample_value("business_lookup_latency_ms_count") or 0.0


def _fetch_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == BUSINESS_FETCH_LOG_EVENT]


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Wrap the module's trace_span so each finished span's name and attributes are kept."""
    init_tracer("business-fetch-test")
    recorded: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def _recording(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
        with real_trace_span(name, attributes=attributes) as span:
            yield span
        assert isinstance(span, ReadableSpan)
        recorded.append((name, dict(span.attributes or {})))

    monkeypatch.setattr(fetch_module, "trace_span", _recording)
    return recorded


async def test_empty_plan_makes_no_provider_call_and_writes_one_log_line(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    provider = SpyProvider(_found())
    metrics = create_pipeline_metrics()

    result = await fetch_business_context(
        provider,
        organization_id=uuid4(),
        sender_email=SENDER,
        plan=FetchPlan(),
        timeout_ms=500,
        metrics=metrics,
    )

    assert result is None
    assert provider.calls == []
    assert spans == []
    assert _latency_count(metrics) == 0.0
    lines = _fetch_lines(caplog)
    assert len(lines) == 1
    fields = lines[0].__dict__["fields"]
    assert fields["planned"] is False
    assert fields["outcome"] == "not_planned"


async def test_found_context_is_returned_with_span_metrics_and_correlated_log(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    context = _found()
    provider = SpyProvider(context)
    metrics = create_pipeline_metrics()
    org_id = uuid4()

    with bind_log_context(trace_id="trace-5-4", job_id="job-5-4", organization_id=str(org_id)):
        result = await fetch_business_context(
            provider,
            organization_id=org_id,
            sender_email=SENDER,
            plan=PLAN,
            timeout_ms=500,
            metrics=metrics,
        )
        lines = _fetch_lines(caplog)
        assert len(lines) == 1
        rendered_line = json.loads(StructuredJSONFormatter().format(lines[0]))

    assert result is context
    assert provider.calls == [(org_id, SENDER, PLAN)]
    assert _lookups(metrics, "order", "FOUND") == 1.0
    assert _latency_count(metrics) == 1.0

    assert [name for name, _ in spans] == ["business.fetch"]
    attributes = spans[0][1]
    assert attributes["outcome"] == "ok"
    assert attributes["customer_status"] == "FOUND"
    assert attributes["business_data_degraded"] is False
    assert attributes["planned_refs"] == 1

    assert rendered_line["trace_id"] == "trace-5-4"
    assert rendered_line["job_id"] == "job-5-4"
    assert rendered_line["organization_id"] == str(org_id)
    assert rendered_line["fields"]["facts"] == [
        {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
    ]
    assert rendered_line["fields"]["business_data_degraded"] is False
    assert SENDER not in json.dumps(rendered_line)


async def test_timeout_degrades_every_planned_fact_to_unavailable(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    provider = SpyProvider(_found(), delay_s=1.0)
    metrics = create_pipeline_metrics()
    plan = FetchPlan(refs=(ORDER_REF, TICKET_REF))

    result = await fetch_business_context(
        provider,
        organization_id=uuid4(),
        sender_email=SENDER,
        plan=plan,
        timeout_ms=20,
        metrics=metrics,
    )

    assert result is not None
    assert result.customer_status == CustomerStatus.UNAVAILABLE
    assert result.degraded is True
    assert {(f.reference, f.status) for f in result.facts} == {
        ("ORD-82915", FactStatus.UNAVAILABLE),
        ("TICK-4402", FactStatus.UNAVAILABLE),
    }
    assert _lookups(metrics, "order", "UNAVAILABLE") == 1.0
    assert _lookups(metrics, "ticket", "UNAVAILABLE") == 1.0
    # A timeout must never read as "we have no such order" (R13.6).
    assert _lookups(metrics, "order", "NOT_FOUND") == 0.0
    assert spans[0][1]["outcome"] == "timeout"
    fields = _fetch_lines(caplog)[0].__dict__["fields"]
    assert fields["outcome"] == "timeout"
    assert fields["business_data_degraded"] is True


async def test_provider_error_degrades_and_keeps_error_text_out_of_logs(
    caplog: pytest.LogCaptureFixture, spans: list[tuple[str, dict[str, Any]]]
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    provider = SpyProvider(error=RuntimeError(f"connection reset while reading {SENDER}"))

    result = await fetch_business_context(
        provider,
        organization_id=uuid4(),
        sender_email=SENDER,
        plan=PLAN,
        timeout_ms=500,
        metrics=create_pipeline_metrics(),
    )

    assert result is not None
    assert result.customer_status == CustomerStatus.UNAVAILABLE
    assert result.degraded is True
    assert spans[0][1]["outcome"] == "error"
    all_text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in all_text
    assert SENDER not in all_text


def test_business_payload_records_plan_statuses_and_degraded_flag() -> None:
    payload = business_payload(PLAN, _found())
    assert payload == {
        "business_plan": PLAN.to_payload(),
        "customer_status": "FOUND",
        "business_fact_statuses": [
            {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
        ],
        "business_data_degraded": False,
    }
    json.dumps(payload)  # JSON-safe for the processing_event jsonb column

    assert business_payload(FetchPlan(), None) == {
        "business_plan": FetchPlan().to_payload(),
        "customer_status": None,
        "business_fact_statuses": [],
        "business_data_degraded": False,
    }
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/unit/test_business_fetch.py -v`
Expected: FAIL (collection error) with `ModuleNotFoundError: No module named 'packages.business.fetch'`.

- [ ] **Step 7: Implement the fetch wrapper**

Create `packages/business/fetch.py`:

```python
"""Bounded business-data fetch: deadline, degradation and telemetry (R13.7, R21.3).

The provider call runs under ``BUSINESS_DATA__TIMEOUT_MS``. On timeout or any provider error
the customer status and every planned fact become ``UNAVAILABLE`` and the context is marked
degraded, so the draft is still written and a timeout never reads as "no such order"
(design.md §5.4). An empty plan makes no provider call and returns ``None``.

Telemetry per job: one ``business.fetch`` span around the provider call, one
``business_lookups_total{entity, status}`` increment per fact, one
``business_lookup_latency_ms`` observation, and exactly one ``business_fetch`` log line
(also for an empty plan). Correlation ids come from the bound log context. The sender
address and provider error text are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry.trace import StatusCode

from packages.domain.business import BusinessContext, FetchPlan, unavailable_context
from packages.observability.metrics import get_metrics
from packages.observability.tracing import trace_span

if TYPE_CHECKING:
    from packages.business.protocol import BusinessDataProvider
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)

BUSINESS_FETCH_LOG_EVENT = "business_fetch"
"""Message of the one structured log line written per job by the business step (R21.3)."""

BUSINESS_FETCH_SPAN = "business.fetch"


def fact_statuses(context: BusinessContext | None) -> list[dict[str, Any]]:
    """One `BusinessFact.to_payload()` row per fact: entity, reference, status, reason."""
    if context is None:
        return []
    return [fact.to_payload() for fact in context.facts]


def business_payload(plan: FetchPlan, context: BusinessContext | None) -> dict[str, Any]:
    """CONTEXT_READY payload fields for replay (design.md §5.4): plan, statuses, degradation."""
    return {
        "business_plan": plan.to_payload(),
        "customer_status": context.customer_status.value if context is not None else None,
        "business_fact_statuses": fact_statuses(context),
        "business_data_degraded": context.degraded if context is not None else False,
    }


async def fetch_business_context(
    provider: BusinessDataProvider,
    *,
    organization_id: UUID,
    sender_email: str,
    plan: FetchPlan,
    timeout_ms: int,
    metrics: PipelineMetrics | None = None,
) -> BusinessContext | None:
    """Run one bounded provider call for a non-empty plan; degrade instead of failing."""
    if plan.is_empty:
        _log_fetch(organization_id, plan, None, outcome="not_planned", latency_ms=0.0)
        return None

    started = time.perf_counter()
    outcome = "ok"
    attributes: dict[str, Any] = {
        "organization_id": str(organization_id),
        "planned_refs": len(plan.refs),
        "snapshot": ",".join(sorted(entity.value for entity in plan.snapshot)),
        "timeout_ms": timeout_ms,
    }
    with trace_span(BUSINESS_FETCH_SPAN, attributes=attributes) as span:
        try:
            context = await asyncio.wait_for(
                provider.get_business_context(organization_id, sender_email, plan),
                timeout=timeout_ms / 1000,
            )
        except TimeoutError:
            outcome = "timeout"
            logger.warning(
                "Business data fetch timed out after %d ms for tenant %s",
                timeout_ms,
                organization_id,
            )
            context = unavailable_context(plan, datetime.now(UTC))
        except Exception as err:
            outcome = "error"
            logger.warning(
                "Business data fetch failed for tenant %s: %s",
                organization_id,
                type(err).__name__,
            )
            context = unavailable_context(plan, datetime.now(UTC))
        latency_ms = (time.perf_counter() - started) * 1000.0
        span.set_attribute("outcome", outcome)
        span.set_attribute("customer_status", context.customer_status.value)
        span.set_attribute("business_data_degraded", context.degraded)
        if outcome != "ok":
            span.set_status(StatusCode.ERROR, outcome)

    _record_metrics(metrics, context, latency_ms)
    _log_fetch(organization_id, plan, context, outcome=outcome, latency_ms=latency_ms)
    return context


def _record_metrics(
    metrics: PipelineMetrics | None, context: BusinessContext, latency_ms: float
) -> None:
    m = metrics or get_metrics()
    try:
        m.business_lookup_latency_ms.observe(latency_ms)
        for fact in context.facts:
            m.business_lookups_total.labels(
                entity=fact.entity.value, status=fact.status.value
            ).inc()
    except Exception:
        logger.warning("Failed to record business lookup metrics", exc_info=True)


def _log_fetch(
    organization_id: UUID,
    plan: FetchPlan,
    context: BusinessContext | None,
    *,
    outcome: str,
    latency_ms: float,
) -> None:
    with contextlib.suppress(Exception):  # logging must never fail the job
        logger.info(
            BUSINESS_FETCH_LOG_EVENT,
            extra={
                "organization_id": str(organization_id),
                "fields": {
                    "planned": not plan.is_empty,
                    "refs": [ref.reference for ref in plan.refs],
                    "snapshot": sorted(entity.value for entity in plan.snapshot),
                    "customer_status": (
                        context.customer_status.value if context is not None else None
                    ),
                    "facts": fact_statuses(context),
                    "business_data_degraded": (
                        context.degraded if context is not None else False
                    ),
                    "outcome": outcome,
                    "latency_ms": round(latency_ms, 2),
                },
            },
        )
```

- [ ] **Step 8: Run to verify it passes**

Run: `uv run pytest tests/unit/test_business_fetch.py -v`
Expected: PASS (5 tests).

- [ ] **Step 9: Switch `ContextPackage.business_data` to `BusinessContext | None`**

Task 3 left `ContextPackage` unchanged, so this step makes the switch. In `packages/domain/entities.py`, add `from packages.domain.business import BusinessContext` after the `from uuid import UUID, uuid4` import. Replace

```python
    business_data: dict[str, Any] = field(default_factory=dict)
```

with

```python
    business_data: BusinessContext | None = None
```

and replace

```python
        if self.business_data:
            biz_lines = [f"{k}: {v}" for k, v in sorted(self.business_data.items())]
            sections.append(("business_data", "[BUSINESS DATA]\n" + "\n".join(biz_lines)))
```

with

```python
        if self.business_data is not None:
            sections.append(("business_data", self.business_data.render()))
```

Test edits that follow from the type change:

`tests/unit/test_domain_entities.py` — add

```python
from packages.domain.business import BusinessContext, BusinessFact, CustomerStatus, EntityType, FactStatus
```

after `import pytest` (ruff will wrap it), replace `business_data={"order_id": "8821", "status": "Shipped"},` with `business_data=business,`, insert before `pkg = ContextPackage(` in `test_context_package_fixed_assembly_order`:

```python
    business = BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        facts=(
            BusinessFact(
                entity=EntityType.ORDER,
                reference="ORD-8821",
                status=FactStatus.FOUND,
                attributes=(("status", "Shipped"),),
            ),
        ),
    )
```

and replace `assert "status: Shipped" in sections[6][1]` with:

```python
    assert sections[6][1] == business.render()
    assert "ORD-8821" in sections[6][1]
    assert "Shipped" in sections[6][1]
```

Delete the line `        business_data={"customer_tier": "gold"},` in `tests/unit/test_single_pass_generator.py` (~line 60), `tests/unit/test_draft_repair_orchestration.py` (~line 67) and `tests/unit/test_complexity_router_integration.py` (~line 94); none of them asserts business content.

`tests/unit/test_agent_profile.py::test_registry_render_prompt_with_context_package` — delete the line `        business_data={"plan": "Enterprise"},` and the line `    assert "plan: Enterprise" in rendered`. The live yaml still points at the v1 templates, which iterate `business_data.items()` and cannot render a `BusinessContext`; Task 7 restores a business assertion against the v2 template.

Run: `uv run pytest tests/unit/test_domain_entities.py tests/unit/test_single_pass_generator.py tests/unit/test_draft_repair_orchestration.py tests/unit/test_complexity_router_integration.py tests/unit/test_agent_profile.py -v`
Expected: PASS.

- [ ] **Step 10: Write the failing Context Builder business tests**

Create `tests/unit/test_context_builder_business.py`:

```python
"""Context Builder business-data wiring (R13.3, R13.7, design.md §5.4, ADR-0008).

Typed IDs are always planned; the routed profile's context_policy gates the snapshot; nothing
planned means no provider call; a timeout degrades instead of failing; the plan, statuses and
degradation flag go into the CONTEXT_READY payload.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.business.memory import InMemoryBusinessDataProvider
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.db.fixtures import (
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDERS,
    BUSINESS_TICKETS,
    DEMO_ORG_ID,
    FIXTURE_EMAILS,
)
from packages.db.job import InMemoryJobStore
from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
)
from packages.domain.entities import (
    Classification,
    EmailAddress,
    Job,
    NormalizedMessage,
    ProcessingEvent,
)
from packages.domain.state_machine import JobState
from packages.llm.profile import AgentProfileRegistry

REGISTRY_PATH = "config/agent_profiles.yaml"
ALICE = "alice.smith@clientcorp.com"
ORDER_PLAN = FetchPlan(refs=(EntityRef(entity=EntityType.ORDER, reference="ORD-82915"),))


def _found_order() -> BusinessContext:
    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        customer=(("name", "Alice Smith"),),
        facts=(
            BusinessFact(
                entity=EntityType.ORDER,
                reference="ORD-82915",
                status=FactStatus.FOUND,
                attributes=(("status", "shipped"),),
            ),
        ),
    )


class SpyProvider:
    def __init__(self, result: BusinessContext | None = None, *, delay_s: float = 0.0) -> None:
        self.result = result
        self.delay_s = delay_s
        self.calls: list[tuple[UUID, str, FetchPlan]] = []

    async def get_business_context(
        self, organization_id: UUID, sender_email: str, plan: FetchPlan
    ) -> BusinessContext:
        self.calls.append((organization_id, sender_email, plan))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        assert self.result is not None
        return self.result


def _message(
    org_id: UUID,
    *,
    subject: str,
    body: str,
    sender: str = ALICE,
    sender_name: str | None = None,
    body_clean: str | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id=f"prov-{uuid4()}",
        sender=EmailAddress(email=sender, name=sender_name),
        recipients=[EmailAddress(email="support@acme.com")],
        subject=subject,
        body_text=body,
        body_text_clean=body if body_clean is None else body_clean,
        received_at=datetime.now(UTC),
    )


async def _queued_job(store: InMemoryJobStore, msg: NormalizedMessage) -> Job:
    job = Job(
        id=uuid4(),
        organization_id=msg.organization_id,
        thread_id=msg.thread_id,
        message_id=msg.message_id,
        state=JobState.QUEUED,
        idempotency_key=f"job-{msg.message_id}",
    )
    await store.create_job(job)
    return job


async def _context_ready(store: InMemoryJobStore, job: Job) -> ProcessingEvent:
    events = await store.list_events_for_job(job.organization_id, job.id)
    return next(e for e in events if e.state_to == JobState.CONTEXT_READY.value)


def _builder(
    provider: SpyProvider | InMemoryBusinessDataProvider | None,
    store: InMemoryJobStore,
    *,
    timeout_ms: int = 500,
) -> ContextBuilder:
    return ContextBuilder(
        thread_assembler=ThreadContextAssembler(),
        business_data_provider=provider,
        job_store=store,
        profile_registry=AgentProfileRegistry.from_yaml(REGISTRY_PATH),
        business_timeout_ms=timeout_ms,
    )


async def test_typed_order_id_is_fetched_even_for_a_general_inquiry() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Quick question", body="What is the status of order 82915?")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="general_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert provider.calls == [(org_id, ALICE, ORDER_PLAN)]
    assert pkg.business_data is provider.result
    assert [name for name, _ in pkg.get_ordered_sections()][-1] == "business_data"

    ready = await _context_ready(store, job)
    assert ready.payload["business_plan"] == ORDER_PLAN.to_payload()
    assert ready.payload["customer_status"] == "FOUND"
    assert ready.payload["business_fact_statuses"] == [
        {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
    ]
    assert ready.payload["business_data_degraded"] is False
    assert ready.payload["retrieval_performed"] is False


async def test_billing_profile_plans_the_snapshot_without_ids() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="My account", body="I think I was charged twice.")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    await _builder(provider, store).build_context(
        job, msg, Classification(category="billing", retrieval_required=False), thread_messages=[msg]
    )

    assert provider.calls == [
        (org_id, ALICE, FetchPlan(snapshot=frozenset({EntityType.ORDER, EntityType.TICKET})))
    ]


async def test_mapped_intent_plans_orders_under_a_non_business_profile() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Refund", body="Please refund my last purchase.")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="support", intent="refund_request", retrieval_required=False),
        thread_messages=[msg],
    )

    assert provider.calls == [(org_id, ALICE, FetchPlan(snapshot=frozenset({EntityType.ORDER})))]


async def test_no_classification_uses_the_default_profile_and_plans_nothing() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Hello", body="Just saying thanks for the help.")
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(job, msg, None, thread_messages=[msg])

    assert provider.calls == []
    assert pkg.business_data is None
    assert "business_data" not in [name for name, _ in pkg.get_ordered_sections()]
    ready = await _context_ready(store, job)
    assert ready.payload["business_plan"] == FetchPlan().to_payload()
    assert ready.payload["customer_status"] is None
    assert ready.payload["business_fact_statuses"] == []
    assert ready.payload["business_data_degraded"] is False


async def test_provider_timeout_degrades_and_the_job_still_reaches_context_ready() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Order", body="Where is order #82915?")
    provider = SpyProvider(_found_order(), delay_s=1.0)
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store, timeout_ms=20).build_context(
        job,
        msg,
        Classification(category="general_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert pkg.business_data is not None
    assert pkg.business_data.customer_status == CustomerStatus.UNAVAILABLE
    assert pkg.business_data.degraded is True
    assert job.state == JobState.CONTEXT_READY
    ready = await _context_ready(store, job)
    assert ready.payload["business_data_degraded"] is True
    assert ready.payload["customer_status"] == "UNAVAILABLE"
    assert [row["status"] for row in ready.payload["business_fact_statuses"]] == ["UNAVAILABLE"]


async def test_without_a_provider_nothing_is_planned_or_fetched() -> None:
    org_id = uuid4()
    msg = _message(org_id, subject="Invoice", body="Question on invoice INV-2026-001, order 82915.")
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(None, store).build_context(
        job,
        msg,
        Classification(category="billing", intent="invoice_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert pkg.business_data is None
    ready = await _context_ready(store, job)
    assert ready.payload["business_plan"] == FetchPlan().to_payload()
    assert ready.payload["business_data_degraded"] is False


async def test_invoice_reference_reaches_the_context_as_not_looked_up() -> None:
    """Bob's billing fixture email: INV- is planned and recorded, never NOT_FOUND (R13.6)."""
    fixture = next(f for f in FIXTURE_EMAILS if f.key == "billing_invoice")
    msg = _message(
        DEMO_ORG_ID,
        subject=fixture.subject,
        body=fixture.body_text,
        sender=fixture.sender_email,
    )
    provider = InMemoryBusinessDataProvider(
        customers=BUSINESS_CUSTOMERS, orders=BUSINESS_ORDERS, tickets=BUSINESS_TICKETS
    )
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(
        job, msg, Classification(category="billing", retrieval_required=False), thread_messages=[msg]
    )

    assert pkg.business_data is not None
    assert pkg.business_data.customer_status == CustomerStatus.FOUND
    invoice = next(f for f in pkg.business_data.facts if f.entity == EntityType.INVOICE)
    assert invoice.reference == "INV-2026-8891"
    assert invoice.status == FactStatus.NOT_LOOKED_UP
    assert invoice.reason == NotLookedUpReason.UNSUPPORTED_ENTITY


async def test_sender_case_and_display_name_reach_the_provider_as_the_bare_address() -> None:
    """Review focus 1: the parser splits off the display name; the provider ignores case."""
    msg = _message(
        DEMO_ORG_ID,
        subject="Order status question",
        body="What is the status of order 82915?",
        sender="Alice.Smith@ClientCorp.COM",
        sender_name="Alice Smith",
    )
    classification = Classification(category="general_inquiry", retrieval_required=False)

    spy = SpyProvider(_found_order())
    spy_store = InMemoryJobStore()
    await _builder(spy, spy_store).build_context(
        await _queued_job(spy_store, msg), msg, classification, thread_messages=[msg]
    )
    assert [sender for _, sender, _ in spy.calls] == ["Alice.Smith@ClientCorp.COM"]

    provider = InMemoryBusinessDataProvider(
        customers=BUSINESS_CUSTOMERS, orders=BUSINESS_ORDERS, tickets=BUSINESS_TICKETS
    )
    store = InMemoryJobStore()
    pkg = await _builder(provider, store).build_context(
        await _queued_job(store, msg), msg, classification, thread_messages=[msg]
    )
    assert pkg.business_data is not None
    assert pkg.business_data.customer_status == CustomerStatus.FOUND
    order = next(f for f in pkg.business_data.facts if f.reference == "ORD-82915")
    assert order.status == FactStatus.FOUND
    assert dict(order.attributes)["status"] == "dispatched"  # seeded by Task 2


async def test_order_number_only_in_quoted_history_is_not_planned() -> None:
    """Review focus 5: the plan reads body_text_clean, which has the quoted history stripped."""
    org_id = uuid4()
    clean = "Thanks, that answers my question."
    msg = _message(
        org_id,
        subject="Re: Thanks",
        body=f"{clean}\n\nOn Mon, 21 Sep 2026, Alice wrote:\n> Where is order #77001?",
        body_clean=clean,
    )
    provider = SpyProvider(_found_order())
    store = InMemoryJobStore()
    job = await _queued_job(store, msg)

    pkg = await _builder(provider, store).build_context(
        job,
        msg,
        Classification(category="general_inquiry", retrieval_required=False),
        thread_messages=[msg],
    )

    assert provider.calls == []
    assert pkg.business_data is None
```

In `tests/unit/test_context_builder.py`: delete the docstring line `- BusinessDataProvider protocol and StubBusinessDataProvider (R13 stub).`; replace the import block

```python
from packages.context.builder import (
    BusinessDataProvider,
    ContextBuilder,
    DefaultInstructionProvider,
    InstructionProvider,
    StubBusinessDataProvider,
)
```

with

```python
from packages.context.builder import (
    ContextBuilder,
    DefaultInstructionProvider,
    InstructionProvider,
)
```

and delete the whole function `test_business_data_provider_protocols_and_stub` (lines 67-78). The protocol is now `packages.business.protocol.BusinessDataProvider` and is covered by Task 3's contract suite; the stub has no successor (no provider means no fetch).

- [ ] **Step 11: Run to verify the builder tests fail**

Run: `uv run pytest tests/unit/test_context_builder_business.py -v`
Expected: FAIL — `TypeError: ContextBuilder.__init__() got an unexpected keyword argument 'profile_registry'` in every test.

- [ ] **Step 12: Replace `packages/context/builder.py`**

Write the whole file:

```python
"""Context Builder orchestrator for prompt assembly, RAG gating and business data.

Orchestrates (R14.8, R6.6, R13, R18.1):
1. Resolving static agent and category instructions (cacheable prefix).
2. Gathering thread conversation context via ThreadContextAssembler (Task 4.3).
3. Conditionally invoking hybrid RAG only when retrieval_required=True (R6.6).
4. Planning business lookups in code and running them under a deadline (R13.3, R13.7,
   design.md §5.4, ADR-0008). The routed profile's context_policy comes from the
   AgentProfileRegistry; the instruction source is unchanged.
5. Emitting ContextPackage in fixed assembly order (R14.8, design.md §5.4).
6. Transitioning processing job state from QUEUED to CONTEXT_READY (R18.1), with the
   business plan, statuses and degradation flag in the payload for replay.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from packages.business.fetch import business_payload, fetch_business_context
from packages.business.plan import build_fetch_plan
from packages.domain.business import BusinessContext, FetchPlan
from packages.domain.entities import (
    Candidate as DomainCandidate,
)
from packages.domain.entities import (
    Classification,
    ContextPackage,
    Job,
    NormalizedMessage,
    ThreadState,
)
from packages.domain.state_machine import JobState
from packages.llm.profile import ContextPolicy
from packages.observability.context import bind_log_context
from packages.retrieval.query_builder import RetrievalQueryBuilder

if TYPE_CHECKING:
    from packages.business.protocol import BusinessDataProvider
    from packages.context.assembly import ThreadContextAssembler
    from packages.db.job import JobStore
    from packages.llm.profile import AgentProfileRegistry
    from packages.observability.metrics import PipelineMetrics
    from packages.retrieval.retriever import HybridRetriever

logger = logging.getLogger(__name__)

DEFAULT_BUSINESS_TIMEOUT_MS = 500
"""Matches BusinessDataSettings.timeout_ms; the ai-worker passes the configured value."""


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


@runtime_checkable
class InstructionProvider(Protocol):
    """Protocol for providing agent and category prompt instructions (R14.1, R14.8)."""

    def get_instructions(self, category: str) -> tuple[str, str]:
        """Return (agent_instructions, category_instructions)."""
        ...


class DefaultInstructionProvider:
    """Default static instruction provider returning cacheable prompt instructions."""

    DEFAULT_AGENT_INSTRUCTIONS = (
        "You are an enterprise AI assistant for customer email correspondence. "
        "Provide professional, concise, and helpful responses grounded in the "
        "provided thread history and reference knowledge."
    )

    CATEGORY_INSTRUCTIONS: dict[str, str] = {
        "billing": (
            "Provide clear account and billing guidance, citing invoice details when applicable."
        ),
        "support": (
            "Address technical questions with structured diagnostic steps and procedural guidance."
        ),
        "sales": "Offer product capabilities, tier options, and next steps for procurement.",
        "general_inquiry": "Answer inquiries accurately and provide polite assistance.",
        "scheduling": "Coordinate dates, times, and calendar confirmations efficiently.",
        "administration": (
            "Process account changes following administrative verification protocols."
        ),
    }

    def get_instructions(self, category: str) -> tuple[str, str]:
        """Return static (agent_instructions, category_instructions) for prompt-prefix caching."""
        cat_key = category.lower().strip() if category else "general_inquiry"
        cat_instr = self.CATEGORY_INSTRUCTIONS.get(
            cat_key,
            f"Process {category} requests adhering to enterprise operational standards.",
        )
        return self.DEFAULT_AGENT_INSTRUCTIONS, cat_instr


class ContextBuilder:
    """Orchestrates thread context, hybrid RAG, and business data into ContextPackage (R14.8).

    Enforces:
    - R6.6: Skip hybrid RAG when retrieval_required == False.
    - R13.3 / R13.7: business lookups planned in code and bounded by a deadline; with no
      business_data_provider nothing is planned or fetched.
    - R14.8: Strict fixed assembly order: agent_instructions, category_instructions,
      thread_summary, recent_messages, current_email, retrieved_knowledge, business_data.
    - R18.1: Transition Job state QUEUED -> CONTEXT_READY.
    """

    def __init__(
        self,
        thread_assembler: ThreadContextAssembler,
        retriever: HybridRetriever | None = None,
        query_builder: RetrievalQueryBuilder | None = None,
        business_data_provider: BusinessDataProvider | None = None,
        instruction_provider: InstructionProvider | None = None,
        job_store: JobStore | None = None,
        top_k: int = 5,
        profile_registry: AgentProfileRegistry | None = None,
        business_timeout_ms: int = DEFAULT_BUSINESS_TIMEOUT_MS,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self.thread_assembler = thread_assembler
        self.retriever = retriever
        self.query_builder = query_builder or RetrievalQueryBuilder()
        self.business_data_provider = business_data_provider
        self.instruction_provider = instruction_provider or DefaultInstructionProvider()
        self.job_store = job_store
        self.top_k = top_k
        self.profile_registry = profile_registry
        self.business_timeout_ms = business_timeout_ms
        self.metrics = metrics

    async def build_context(
        self,
        job: Job,
        message: NormalizedMessage,
        classification: Classification | None = None,
        thread_messages: list[NormalizedMessage] | None = None,
        thread_state: ThreadState | None = None,
    ) -> ContextPackage:
        """Gather context from all subsystems and emit ordered ContextPackage."""
        org_id = _to_uuid(job.organization_id)
        thread_id = _to_uuid(job.thread_id or message.thread_id)

        # 1. Resolve static agent and category instructions (cacheable prefix)
        category = classification.category if classification else "general_inquiry"
        agent_instr, cat_instr = self.instruction_provider.get_instructions(category)

        # 2. Assemble thread context (Task 4.3, R8.5)
        thread_ctx = await self.thread_assembler.assemble(
            organization_id=org_id,
            thread_id=thread_id,
            current_message=message,
            thread_messages=thread_messages,
            thread_state=thread_state,
        )

        # 3. Conditional Hybrid RAG (R6.6)
        retrieval_required = (
            classification.retrieval_required if classification is not None else True
        )

        retrieved_chunks: list[DomainCandidate] = []
        if retrieval_required and self.retriever is not None:
            query = self.query_builder.build(
                message=message,
                classification=classification,
                thread_summary=thread_ctx.summary,
            )
            retrieval_result = await self.retriever.retrieve(query)
            top_candidates = retrieval_result.candidates[: self.top_k]
            for c in top_candidates:
                ext_id = (
                    getattr(c, "external_id", None) or c.metadata.get("external_id") or c.chunk_id
                )
                retrieved_chunks.append(
                    DomainCandidate(
                        chunk_id=c.chunk_id,
                        document_id=c.document_id,
                        content=c.content,
                        metadata=dict(c.metadata),
                        external_id=ext_id,
                        lexical_rank=c.lexical_rank,
                        vector_rank=c.vector_rank,
                        lexical_score=c.lexical_score,
                        vector_score=c.vector_score,
                        fused_score=c.fused_score,
                        rerank_score=c.rerank_score,
                    )
                )

        # 4. Transactional business data: code-side plan, one bounded fetch (R13, ADR-0008)
        plan, business_data = await self._business_data(job, org_id, message, classification)

        # 5. Emit ContextPackage with fixed assembly order (R14.8)
        pkg = ContextPackage(
            agent_instructions=agent_instr,
            category_instructions=cat_instr,
            current_message=message,
            thread_summary=thread_ctx.summary,
            recent_messages=thread_ctx.recent_messages,
            retrieved_chunks=retrieved_chunks,
            business_data=business_data,
        )

        # 6. State Machine transition: QUEUED -> CONTEXT_READY (R18.1)
        if self.job_store is not None and job.state == JobState.QUEUED:
            updated_job, _ = await self.job_store.transition_job_state(
                organization_id=org_id,
                job_id=_to_uuid(job.id),
                target_state=JobState.CONTEXT_READY,
                payload={
                    "retrieval_performed": retrieval_required,
                    "retrieved_chunks_count": len(retrieved_chunks),
                    "thread_has_summary": thread_ctx.has_summary,
                    "tokens_saved": thread_ctx.tokens_saved,
                    **business_payload(plan, business_data),
                },
            )
            job.state = updated_job.state

        return pkg

    def _context_policy(self, classification: Classification | None) -> str:
        """The routed profile's context_policy, resolved by the generator's rule (§5.4)."""
        if self.profile_registry is None:
            return ContextPolicy.THREAD_PLUS_RAG.value
        category = classification.category if classification is not None else None
        return str(self.profile_registry.resolve_profile(category).context_policy)

    async def _business_data(
        self,
        job: Job,
        org_id: UUID,
        message: NormalizedMessage,
        classification: Classification | None,
    ) -> tuple[FetchPlan, BusinessContext | None]:
        if self.business_data_provider is None:
            return FetchPlan(), None
        plan = build_fetch_plan(
            subject=message.subject,
            body=message.body_text_clean or message.body_text,
            context_policy=self._context_policy(classification),
            intent=classification.intent if classification is not None else None,
        )
        # The consumer already binds these; binding here keeps the business_fetch line
        # correlated when the builder runs outside a consumer (R21.3).
        with bind_log_context(
            trace_id=job.trace_id, job_id=str(job.id), organization_id=str(org_id)
        ):
            business_data = await fetch_business_context(
                self.business_data_provider,
                organization_id=org_id,
                sender_email=message.sender.email,
                plan=plan,
                timeout_ms=self.business_timeout_ms,
                metrics=self.metrics,
            )
        return plan, business_data
```

Replace `packages/context/__init__.py` with:

```python
"""Context assembly and thread summarization package (Phase 4, R8, R14)."""

from packages.context.assembly import (
    AssembledThreadContext,
    ThreadContextAssembler,
)
from packages.context.builder import (
    ContextBuilder,
    DefaultInstructionProvider,
    InstructionProvider,
)
from packages.context.policy import (
    THREAD_SUMMARY_SCHEMA,
    SummarizationDecision,
    SummarizationPolicy,
)
from packages.context.summarizer import (
    SummarizationResult,
    ThreadSummarizer,
)

__all__ = [
    "THREAD_SUMMARY_SCHEMA",
    "AssembledThreadContext",
    "ContextBuilder",
    "DefaultInstructionProvider",
    "InstructionProvider",
    "SummarizationDecision",
    "SummarizationPolicy",
    "SummarizationResult",
    "ThreadContextAssembler",
    "ThreadSummarizer",
]
```

- [ ] **Step 13: Run the builder tests to verify they pass**

Run: `uv run pytest tests/unit/test_context_builder_business.py tests/unit/test_context_builder.py tests/unit/test_business_fetch.py tests/unit/test_dependency_rules.py -v`
Expected: PASS. (`test_build_context_with_retrieval_required_true` still sees no `business_data` section: that builder has no provider.)

- [ ] **Step 14: Write the failing ai-worker wiring test**

In `tests/unit/test_ai_worker_main.py`, add `from packages.business.postgres import PostgresBusinessDataProvider` to the imports (before `from packages.core.settings import ...`) and append:

```python
def test_context_builder_shares_the_generator_registry_and_uses_the_postgres_provider() -> None:
    """5.4: the builder reads context_policy from the generator's registry; no stub remains."""
    settings = AIWorkerSettings()
    res = fake_worker_resources(settings)
    first = build_consumers(res, token_counter=TokenCounter())[0]
    builder = first.context_builder

    assert builder.profile_registry is first.drafting.generator.profile_registry
    assert isinstance(builder.business_data_provider, PostgresBusinessDataProvider)
    assert builder.business_timeout_ms == settings.business_data.timeout_ms
    assert builder.metrics is res.metrics
```

Run: `uv run pytest tests/unit/test_ai_worker_main.py -v`
Expected: FAIL — `AssertionError` on `builder.profile_registry is ...` (the builder's registry is `None`).

- [ ] **Step 15: Wire the ai-worker composition root**

In `services/ai_worker/main.py` add `from packages.business.postgres import PostgresBusinessDataProvider` after `from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime`.

In `build_consumers`, after `states = PostgresThreadStateStore(res.db_pool)` add:

```python
    # One registry: the generator picks the template, the builder reads context_policy (§5.4).
    profile_registry = AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path)
    business = settings.business_data
```

In the `ContextBuilder(...)` call, replace

```python
        job_store=jobs,
        top_k=settings.retrieval.top_k,
    )
```

with

```python
        business_data_provider=PostgresBusinessDataProvider(
            res.db_pool,
            snapshot_orders=business.snapshot_orders,
            snapshot_tickets=business.snapshot_tickets,
            statement_timeout_ms=business.timeout_ms,
        ),
        job_store=jobs,
        top_k=settings.retrieval.top_k,
        profile_registry=profile_registry,
        business_timeout_ms=business.timeout_ms,
        metrics=res.metrics,
    )
```

and in `SinglePassGenerator(...)` replace `profile_registry=AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path),` with `profile_registry=profile_registry,`.

Run: `uv run pytest tests/unit/test_ai_worker_main.py -v`
Expected: PASS (the provider constructor does no I/O on the `MagicMock` pool).

- [ ] **Step 16: Add the real-Postgres wiring test**

In `tests/integration/test_context_builder_postgres.py`, add imports: `from decimal import Decimal` after `from datetime import UTC, datetime, timedelta`; `from packages.business.postgres import PostgresBusinessDataProvider` before `from packages.context.assembly import ThreadContextAssembler`; `from packages.domain.business import CustomerStatus, EntityType, FactStatus` after `from packages.db.thread_state import PostgresThreadStateStore`; `from packages.llm.profile import AgentProfileRegistry` before `from packages.retrieval.postgres import PostgresSearchBackend`. Append:

```python
async def _seed_customer_with_order(
    pool: asyncpg.Pool[Any], org_id: UUID, *, email: str, order_number: str, status: str
) -> None:
    await _seed_org(pool, org_id)
    customer_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer (id, organization_id, email, name) VALUES ($1, $2, $3, $4);",
            customer_id,
            org_id,
            email,
            "Alice Example",
        )
        await conn.execute(
            'INSERT INTO "order" (id, organization_id, customer_id, order_number, status, total) '
            "VALUES ($1, $2, $3, $4, $5, $6);",
            uuid4(),
            org_id,
            customer_id,
            order_number,
            status,
            Decimal("120.00"),
        )


@pytest.mark.asyncio
async def test_context_builder_postgres_fetches_the_senders_typed_order(
    db_pool: asyncpg.Pool[Any],
) -> None:
    """R13.3/R13.7 wiring: a typed order number in a general inquiry is looked up for the
    sender inside the tenant, rendered as section 7 and recorded in CONTEXT_READY."""
    org_id, other_org = uuid4(), uuid4()
    mbx_id, thread_id, msg_id = uuid4(), uuid4(), uuid4()
    await _seed_thread(db_pool, org_id, mbx_id, thread_id, subject="Order status")
    await _seed_customer_with_order(
        db_pool, org_id, email="alice@customer.com", order_number="ORD-82915", status="shipped"
    )
    # Same address and order number in another tenant must not leak (CLAUDE.md §8).
    await _seed_customer_with_order(
        db_pool, other_org, email="alice@customer.com", order_number="ORD-82915", status="cancelled"
    )

    curr_msg = NormalizedMessage(
        message_id=msg_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thread_id,
        provider="gmail",
        provider_message_id=f"prov-{msg_id}",
        sender=EmailAddress(email="alice@customer.com", name="Alice"),
        recipients=[EmailAddress(email="support@example.com")],
        subject="Order status",
        body_text="Hi, what is the status of order 82915?",
        body_text_clean="Hi, what is the status of order 82915?",
        received_at=datetime.now(UTC),
    )
    await _seed_message(db_pool, curr_msg)

    job_store = PostgresJobStore(db_pool)
    job = Job(
        id=uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        message_id=msg_id,
        state=JobState.QUEUED,
        idempotency_key=f"job-{msg_id}",
    )
    await job_store.create_job(job)

    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(
            message_store=PostgresMessageStore(db_pool),
            thread_state_store=PostgresThreadStateStore(db_pool),
        ),
        job_store=job_store,
        business_data_provider=PostgresBusinessDataProvider(db_pool),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
        business_timeout_ms=2000,
    )

    pkg = await builder.build_context(
        job=job,
        message=curr_msg,
        classification=Classification(category="general_inquiry", retrieval_required=False),
    )

    business = pkg.business_data
    assert business is not None
    assert business.customer_status == CustomerStatus.FOUND
    assert business.degraded is False
    order = next(
        f for f in business.facts if f.entity == EntityType.ORDER and f.reference == "ORD-82915"
    )
    assert order.status == FactStatus.FOUND
    assert dict(order.attributes)["status"] == "shipped"
    assert pkg.get_ordered_sections()[-1][0] == "business_data"

    events = await job_store.list_events_for_job(org_id, job.id)
    ready = next(e for e in events if e.state_to == JobState.CONTEXT_READY.value)
    assert ready.payload["customer_status"] == "FOUND"
    assert ready.payload["business_data_degraded"] is False
    assert {
        "entity": "order",
        "reference": "ORD-82915",
        "status": "FOUND",
        "reason": None,
    } in ready.payload["business_fact_statuses"]
```

Run: `uv run pytest tests/integration/test_context_builder_postgres.py -v` (runs against the isolated `rag_email_test` database via `tests/integration/conftest.py`)
Expected: PASS.

- [ ] **Step 17: Forward and document the settings; document the telemetry**

Extend the parametrize list of `test_compose_forwards_the_switch_into_app_containers` in `tests/unit/test_runtime_image_contract.py`: after `"RETRIEVAL__RETRIEVAL_TIMEOUT_MS",` add

```python
        "BUSINESS_DATA__TIMEOUT_MS",
        "BUSINESS_DATA__SNAPSHOT_ORDERS",
        "BUSINESS_DATA__SNAPSHOT_TICKETS",
```

Run: `uv run pytest tests/unit/test_runtime_image_contract.py -v` — Expected: FAIL for the three new names (`AssertionError: assert 'BUSINESS_DATA__TIMEOUT_MS' in {...}`).

In `docker-compose.yml`, directly after the line `  RETRIEVAL__RETRIEVAL_TIMEOUT_MS: ${RETRIEVAL__RETRIEVAL_TIMEOUT_MS:-500}` add:

```yaml
  # Business-data lookup deadline (also the Postgres statement_timeout) and snapshot sizes (R13.7).
  BUSINESS_DATA__TIMEOUT_MS: ${BUSINESS_DATA__TIMEOUT_MS:-500}
  BUSINESS_DATA__SNAPSHOT_ORDERS: ${BUSINESS_DATA__SNAPSHOT_ORDERS:-3}
  BUSINESS_DATA__SNAPSHOT_TICKETS: ${BUSINESS_DATA__SNAPSHOT_TICKETS:-3}
```

Run: `uv run pytest tests/unit/test_runtime_image_contract.py -v` — Expected: PASS.

Append to `.env.example` (the file already ends with a blank line):

```dotenv
# --- 20. Business Data Lookups (R13.4, R13.7, design.md §5.4, ADR-0008) ---
BUSINESS_DATA__TIMEOUT_MS=500
BUSINESS_DATA__SNAPSHOT_ORDERS=3
BUSINESS_DATA__SNAPSHOT_TICKETS=3

```

Append to `docs/configuration.md`:

```markdown
### 2.20 Business Data Lookups (`BUSINESS_DATA__*`)
*Deadline and snapshot sizes for the transactional lookups the Context Builder plans in code (R13.4, R13.7, design.md §5.4, ADR-0008).*

The ai-worker decides in code which business facts to fetch before the one generation call: typed order and ticket numbers in the email are always looked up, and the routed profile's `context_policy` (`thread_plus_rag_plus_business`, set per profile in `config/agent_profiles.yaml`) or a mapped intent adds a snapshot of the sender's recent orders and open tickets. The provider call runs under `BUSINESS_DATA__TIMEOUT_MS`, and the Postgres provider also sets it as the transaction's `statement_timeout`. On timeout or error every planned fact becomes `UNAVAILABLE`, `business_data_degraded=true` is recorded on the `CONTEXT_READY` event, and the draft is still written. Under Docker Compose the three keys are forwarded into the app containers.

| Variable | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `BUSINESS_DATA__TIMEOUT_MS` | `integer` | `500` | $\ge 10$ | Deadline for one business-data provider call in milliseconds; also the Postgres `statement_timeout` (R13.7) |
| `BUSINESS_DATA__SNAPSHOT_ORDERS` | `integer` | `3` | $\ge 0$ | Most recent orders (by `placed_at`) in a customer snapshot (design.md §5.4) |
| `BUSINESS_DATA__SNAPSHOT_TICKETS` | `integer` | `3` | $\ge 0$ | Newest tickets whose status is not `closed` or `resolved` in a customer snapshot (design.md §5.4) |

```

In `docs/observability.md` §2.2, after the `generated_draft_cost_total` table row add:

```markdown
| `business_lookups_total` | Counter | `entity`, `status` | One increment per business fact the business step produced: `entity` is `order`, `ticket` or `invoice`; `status` is `FOUND`, `NOT_FOUND`, `NOT_LOOKED_UP` or `UNAVAILABLE`. Customer resolution is not counted here (R13.6, R13.7). |
| `business_lookup_latency_ms` | Histogram | — | Duration of one bounded business-data provider call, timeouts included (R13.7). |
```

and after the paragraph ending `...and such drafts are excluded from cost sums rather than counted as free.` (before the `---`) add:

````markdown
#### Business data lookups (R13.7, R21.3)

`packages/business/fetch.py::fetch_business_context` wraps the one provider call per job. A
non-empty plan opens a `business.fetch` span (attributes `organization_id`, `planned_refs`,
`snapshot`, `timeout_ms`, `outcome` = `ok`/`timeout`/`error`, `customer_status`,
`business_data_degraded`; status ERROR on timeout or error), increments
`business_lookups_total` once per fact and observes `business_lookup_latency_ms`. Every job
writes exactly one JSON log line `business_fetch`, also when nothing was planned
(`outcome=not_planned`, no span, no metrics). Its `fields` carry `planned, refs, snapshot,
customer_status, facts[{entity, reference, status, reason}], business_data_degraded, outcome,
latency_ms`, and the line carries `trace_id`, `job_id` and `organization_id`. The sender
address and provider error text are never logged. The same plan, statuses and
`business_data_degraded` are stored on the job's `CONTEXT_READY` event for replay.

```promql
# Share of business facts that degraded to UNAVAILABLE (timeouts and errors, R13.7)
sum(rate(business_lookups_total{status="UNAVAILABLE"}[5m])) / sum(rate(business_lookups_total[5m]))

# p95 lookup latency against the 500 ms default deadline
histogram_quantile(0.95, sum by (le) (rate(business_lookup_latency_ms_bucket[5m])))
```
````

- [ ] **Step 18: Run the full verification**

Run: `make ci`
Expected: fmt-check, `ruff check .`, `mypy packages services tests evaluation`, `pytest tests/unit` and `pytest tests/integration` (against `rag_email_test`) all green. If `ruff format --check` flags the new files, run `uv run ruff format <file>` and re-run.

Also run: `docker compose config --quiet` — Expected: exit 0 (the new interpolations parse). A `make up` health check is left to the owner (live-stack restarts are owner-run).

- [ ] **Step 19: Commit**

```bash
git add packages/business/fetch.py packages/core/settings.py packages/observability/metrics.py \
  packages/domain/entities.py packages/context/builder.py packages/context/__init__.py \
  services/ai_worker/main.py docker-compose.yml .env.example docs/configuration.md \
  docs/observability.md \
  tests/unit/test_business_fetch.py tests/unit/test_context_builder_business.py \
  tests/unit/test_settings.py tests/unit/test_observability_metrics.py \
  tests/unit/test_context_builder.py tests/unit/test_ai_worker_main.py \
  tests/unit/test_runtime_image_contract.py tests/unit/test_domain_entities.py \
  tests/unit/test_single_pass_generator.py tests/unit/test_draft_repair_orchestration.py \
  tests/unit/test_complexity_router_integration.py tests/unit/test_agent_profile.py \
  tests/integration/test_context_builder_postgres.py
git commit -m "$(cat <<'EOF'
feat(business): bounded fetch wired into the context builder and ai-worker [task 5.4] [R13.3, R13.7, R20.6, R21.3]

The Context Builder builds the fetch plan from typed IDs, the routed profile's
context_policy (same registry instance as the generator) and the intent, then calls the
Postgres provider under BUSINESS_DATA__TIMEOUT_MS. Timeouts and errors degrade every
planned fact to UNAVAILABLE and the draft is still produced. CONTEXT_READY carries the
plan, customer_status, fact statuses and business_data_degraded; the step emits the
business.fetch span, business_lookups_total{entity,status}, business_lookup_latency_ms
and one business_fetch log line. New business_data settings group, forwarded by compose
and documented. The old stub provider is removed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

Run Task 7 next, before any deploy: between the two commits the live yaml still points at the v1 templates, which cannot render a `BusinessContext`.

---

### Task 7: One `[BUSINESS DATA]` block, precedence rule, prompts v2 [tasks.md 5.5]

**Files:**
- Create: `prompts/support.v2.j2`, `prompts/billing.v2.j2`, `prompts/sales.v2.j2`, `prompts/general.v2.j2`
- Modify: `config/agent_profiles.yaml:15,17,30,32,46,48,62,64`
- Modify: `packages/llm/profile.py:25-29` (precedence constant + `DEFAULT_ENTERPRISE_INSTRUCTIONS`)
- Modify: `packages/context/builder.py` (`DefaultInstructionProvider.DEFAULT_AGENT_INSTRUCTIONS`, import line)
- Test: `tests/unit/test_business_prompt_v2.py` (new)
- Test (modify): `tests/unit/test_agent_profile.py:30-43,65,127,158-212`, `tests/unit/test_single_pass_generator.py:105,336`, `tests/unit/test_generation_budget.py:621`
- Kept unchanged: `prompts/*.v1.j2` (history for drafts recorded as `*.v1`; `tests/unit/test_single_pass_generator.py:252` still uses `support.v1.j2` with a `business_data=None` context)

**Interfaces:**
- Consumes: `BusinessContext.render()`, `unavailable_context`, `FetchPlan` (Task 3); `ContextPackage.business_data: BusinessContext | None` (Task 6); `AgentProfileRegistry.render_prompt` / `get_instructions`.
- Produces: `BUSINESS_DATA_PRECEDENCE_RULE: str` in `packages/llm/profile.py`; `DEFAULT_ENTERPRISE_INSTRUCTIONS` ending with it; `DefaultInstructionProvider.DEFAULT_AGENT_INSTRUCTIONS is DEFAULT_ENTERPRISE_INSTRUCTIONS`; profile `prompt_template` `prompts/<x>.v2.j2`, `prompt_version` `<x>.v2` for x in support/billing/sales/general.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_business_prompt_v2.py`:

```python
"""Business facts render as one [BUSINESS DATA] block; the precedence rule reaches the model.

Requirements: R13.5 (business facts labelled distinctly from knowledge), R13.6 (missing
entities are explicit facts; NOT_FOUND and UNAVAILABLE stay distinct). Design: design.md §5.4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from packages.context.builder import DefaultInstructionProvider
from packages.domain.business import (
    BusinessContext,
    BusinessFact,
    CustomerStatus,
    EntityRef,
    EntityType,
    FactStatus,
    FetchPlan,
    NotLookedUpReason,
    unavailable_context,
)
from packages.domain.entities import Candidate, ContextPackage, EmailAddress, NormalizedMessage
from packages.llm.profile import (
    BUSINESS_DATA_PRECEDENCE_RULE,
    DEFAULT_ENTERPRISE_INSTRUCTIONS,
    AgentProfileRegistry,
)

REGISTRY_PATH = "config/agent_profiles.yaml"
PROFILES = {
    "technical_support": "support",
    "billing": "billing",
    "sales": "sales",
    "general_inquiry": "general",
}
OLD_HEADERS = (
    "--- Customer & System Account Context ---",
    "--- Customer Account & Billing Records ---",
    "--- CRM & Opportunity Context ---",
    "--- Business Context ---",
)
AS_OF = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _business() -> BusinessContext:
    return BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=AS_OF,
        customer=(("name", "Alice Smith"), ("tier", "enterprise")),
        facts=(
            BusinessFact(
                entity=EntityType.ORDER,
                reference="ORD-82915",
                status=FactStatus.FOUND,
                attributes=(("status", "shipped"), ("placed_at", "2026-09-20")),
            ),
            BusinessFact(entity=EntityType.ORDER, reference="ORD-9901", status=FactStatus.NOT_FOUND),
            BusinessFact(
                entity=EntityType.INVOICE,
                reference="INV-2026-8891",
                status=FactStatus.NOT_LOOKED_UP,
                reason=NotLookedUpReason.UNSUPPORTED_ENTITY.value,
            ),
        ),
    )


def _package(business: BusinessContext | None) -> ContextPackage:
    agent, category = DefaultInstructionProvider().get_instructions("general_inquiry")
    msg = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-5-5",
        sender=EmailAddress(email="alice.smith@clientcorp.com", name="Alice Smith"),
        subject="Order status",
        body_text="What is the status of order 82915?",
        body_text_clean="What is the status of order 82915?",
        received_at=AS_OF,
    )
    chunk = Candidate(
        chunk_id="chunk-orders",
        document_id="doc-fulfillment",
        content="Customers can check real-time order status online or via support inquiry.",
        external_id="KB-ORDERS-01",
    )
    return ContextPackage(
        agent_instructions=agent,
        category_instructions=category,
        current_message=msg,
        retrieved_chunks=[chunk],
        business_data=business,
    )


@pytest.mark.parametrize(("profile_name", "stem"), PROFILES.items())
def test_every_profile_uses_its_v2_template_and_version(profile_name: str, stem: str) -> None:
    profile = AgentProfileRegistry.from_yaml(REGISTRY_PATH).get_profile(profile_name)
    assert profile is not None
    assert profile.prompt_template == f"prompts/{stem}.v2.j2"
    assert profile.prompt_version == f"{stem}.v2"
    source = Path(profile.prompt_template).read_text(encoding="utf-8")
    assert "business_data.render()" in source
    assert ".items()" not in source


@pytest.mark.parametrize("profile_name", PROFILES)
def test_business_block_renders_once_after_knowledge_with_every_status(profile_name: str) -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    business = _business()
    block = business.render()

    rendered = registry.render_prompt(registry.get_profile(profile_name) or profile_name, _package(business))

    assert rendered.count(block) == 1
    assert rendered.index("[CITATION: KB-ORDERS-01]") < rendered.index(block)
    for header in OLD_HEADERS:
        assert header not in rendered
    for text in ("ORD-82915", "shipped", "ORD-9901", "NOT_FOUND", "INV-2026-8891", "NOT_LOOKED_UP"):
        assert text in block


@pytest.mark.parametrize("profile_name", PROFILES)
def test_no_business_data_renders_no_block(profile_name: str) -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    header_line = _business().render().splitlines()[0]
    rendered = registry.render_prompt(registry.get_profile(profile_name) or profile_name, _package(None))
    assert header_line not in rendered


def test_degraded_context_says_unavailable_never_not_found() -> None:
    plan = FetchPlan(refs=(EntityRef(entity=EntityType.ORDER, reference="ORD-82915"),))
    degraded = unavailable_context(plan, AS_OF)
    block = degraded.render()
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)

    rendered = registry.render_prompt("general_inquiry", _package(degraded))

    assert block in rendered
    assert "UNAVAILABLE" in block
    assert "NOT_FOUND" not in block


def test_precedence_rule_is_identical_on_both_instruction_paths() -> None:
    default_agent, _ = DefaultInstructionProvider().get_instructions("billing")
    registry_agent, _ = AgentProfileRegistry.from_yaml(REGISTRY_PATH).get_instructions("billing")
    assert BUSINESS_DATA_PRECEDENCE_RULE in default_agent
    assert default_agent == registry_agent == DEFAULT_ENTERPRISE_INSTRUCTIONS
    assert BUSINESS_DATA_PRECEDENCE_RULE == (
        "Order, ticket and invoice status, dates and amounts come only from [BUSINESS DATA]; "
        "if a fact is NOT_FOUND or UNAVAILABLE, say so; knowledge chunks explain procedure only."
    )


def test_rendered_prompt_carries_the_precedence_rule() -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    rendered = registry.render_prompt("billing", _package(_business()))
    assert BUSINESS_DATA_PRECEDENCE_RULE in rendered
```

Update the v1 assertions (they describe the live profiles):
- `tests/unit/test_agent_profile.py::test_prompt_template_files_exist` — replace the four `"prompts/<x>.v1.j2",` entries with `"prompts/support.v2.j2"`, `"prompts/billing.v2.j2"`, `"prompts/sales.v2.j2"`, `"prompts/general.v2.j2"`.
- `tests/unit/test_agent_profile.py:65` — `assert tech["prompt_template"] == "prompts/support.v2.j2"`.
- `tests/unit/test_agent_profile.py:127` — `assert support_prof.prompt_version == "support.v2"`.
- `tests/unit/test_single_pass_generator.py:105` — `assert result.prompt_version == "support.v2"`; `:336` — `assert result.prompt_version == "general.v2"`.
- `tests/unit/test_generation_budget.py:621` — `assert result.prompt_version == "billing.v2"`.
- Leave `tests/unit/test_agent_profile.py:80-110` (standalone `AgentProfile` literals, no yaml) and `tests/unit/test_draft_builder.py:64-102` (literal names) as they are.

Restore the business assertion removed in Task 6, in `tests/unit/test_agent_profile.py::test_registry_render_prompt_with_context_package`: add a second local import directly **above** its `from packages.domain.entities import Candidate, ContextPackage, EmailAddress, NormalizedMessage` line (ruff's isort puts `packages.domain.business` before `packages.domain.entities`):

```python
    from packages.domain.business import (
        BusinessContext,
        BusinessFact,
        CustomerStatus,
        EntityType,
        FactStatus,
    )
```

insert before `context_pkg = ContextPackage(`:

```python
    business = BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=datetime.now(UTC),
        customer=(("plan", "Enterprise"),),
        facts=(
            BusinessFact(entity=EntityType.TICKET, reference="TICK-4402", status=FactStatus.NOT_FOUND),
        ),
    )
```

add `business_data=business,` after `retrieved_chunks=[chunk],` in that `ContextPackage(...)`, and after `assert "[CITATION: DOC-PG-01]" in rendered` add:

```python
    assert business.render() in rendered
    assert "Enterprise" in rendered
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_business_prompt_v2.py tests/unit/test_agent_profile.py tests/unit/test_single_pass_generator.py tests/unit/test_generation_budget.py -v`
Expected: FAIL — `ImportError: cannot import name 'BUSINESS_DATA_PRECEDENCE_RULE' from 'packages.llm.profile'`; the other files fail on `prompt_version == "support.v2"` (`'support.v1' != 'support.v2'`) and `Template prompts/support.v2.j2 must exist`.

- [ ] **Step 3: Add the precedence rule once, used by both instruction paths**

In `packages/llm/profile.py`, replace

```python
DEFAULT_ENTERPRISE_INSTRUCTIONS = (
    "You are an enterprise AI assistant for customer email correspondence. "
    "Provide professional, concise, and helpful responses grounded in the "
    "provided thread history and reference knowledge."
)
```

with

```python
BUSINESS_DATA_PRECEDENCE_RULE = (
    "Order, ticket and invoice status, dates and amounts come only from [BUSINESS DATA]; "
    "if a fact is NOT_FOUND or UNAVAILABLE, say so; knowledge chunks explain procedure only."
)
"""Business facts beat knowledge chunks for transactional facts (R13.3, R13.5, design.md §5.4)."""

DEFAULT_ENTERPRISE_INSTRUCTIONS = (
    "You are an enterprise AI assistant for customer email correspondence. "
    "Provide professional, concise, and helpful responses grounded in the "
    "provided thread history and reference knowledge. "
) + BUSINESS_DATA_PRECEDENCE_RULE
```

In `packages/context/builder.py`, replace `from packages.llm.profile import ContextPolicy` with `from packages.llm.profile import DEFAULT_ENTERPRISE_INSTRUCTIONS, ContextPolicy`, and replace

```python
    DEFAULT_AGENT_INSTRUCTIONS = (
        "You are an enterprise AI assistant for customer email correspondence. "
        "Provide professional, concise, and helpful responses grounded in the "
        "provided thread history and reference knowledge."
    )
```

with

```python
    # The live path sends this text; it is the registry's default, so the two cannot drift.
    DEFAULT_AGENT_INSTRUCTIONS = DEFAULT_ENTERPRISE_INSTRUCTIONS
```

- [ ] **Step 4: Create the v2 templates**

Each v2 file is its v1 file with two changes: the business block becomes `{{ business_data.render() }}` (one `[BUSINESS DATA]` block with `source` and `as_of`, replacing the four headers), and instruction 2 stops sending transactional facts to the knowledge base.

`prompts/support.v2.j2`:

```jinja
{{ agent_instructions }}

{{ category_instructions }}

{% if thread_summary %}
--- Thread Summary ---
{{ thread_summary }}
{% endif %}

{% if recent_messages %}
--- Prior Messages in Thread ---
{% for msg in recent_messages %}
From: {{ msg.sender }}
Subject: {{ msg.subject }}
Received: {{ msg.received_at }}
Body:
{{ msg.body_text_clean or msg.body_text }}
---
{% endfor %}
{% endif %}

--- Current Inbound Email ---
From: {{ current_message.sender }}
Subject: {{ current_message.subject }}
Received: {{ current_message.received_at }}
Body:
{{ current_message.body_text_clean or current_message.body_text }}

{% if retrieved_chunks %}
--- Relevant Knowledge Base Context ---
{% for chunk in retrieved_chunks %}
[CITATION: {{ chunk.external_id or chunk.chunk_id }}]
{{ chunk.content }}
{% endfor %}
{% endif %}

{% if business_data %}
{{ business_data.render() }}
{% endif %}

Instructions:
1. Formulate a structured draft response to the customer addressing technical concerns directly.
2. Ground procedural steps in the provided knowledge base context; take order, ticket and invoice facts only from [BUSINESS DATA].
3. Every cited chunk must be included in `knowledge_chunks` with its exact citation ID.
4. Output must strictly conform to the required JSON schema.
```

`prompts/billing.v2.j2`: identical to `support.v2.j2` except the two instruction lines:

```jinja
{{ agent_instructions }}

{{ category_instructions }}

{% if thread_summary %}
--- Thread Summary ---
{{ thread_summary }}
{% endif %}

{% if recent_messages %}
--- Prior Messages in Thread ---
{% for msg in recent_messages %}
From: {{ msg.sender }}
Subject: {{ msg.subject }}
Received: {{ msg.received_at }}
Body:
{{ msg.body_text_clean or msg.body_text }}
---
{% endfor %}
{% endif %}

--- Current Inbound Email ---
From: {{ current_message.sender }}
Subject: {{ current_message.subject }}
Received: {{ current_message.received_at }}
Body:
{{ current_message.body_text_clean or current_message.body_text }}

{% if retrieved_chunks %}
--- Relevant Knowledge Base Context ---
{% for chunk in retrieved_chunks %}
[CITATION: {{ chunk.external_id or chunk.chunk_id }}]
{{ chunk.content }}
{% endfor %}
{% endif %}

{% if business_data %}
{{ business_data.render() }}
{% endif %}

Instructions:
1. Formulate a precise, formal billing reply adhering strictly to verified invoice and payment facts.
2. Take billing amounts, dates, statuses and account details only from [BUSINESS DATA]; use the knowledge base context for procedure.
3. Every cited chunk must be included in `knowledge_chunks` with its exact citation ID.
4. Output must strictly conform to the required JSON schema.
```

`prompts/sales.v2.j2` (keeps its own knowledge header):

```jinja
{{ agent_instructions }}

{{ category_instructions }}

{% if thread_summary %}
--- Thread Summary ---
{{ thread_summary }}
{% endif %}

{% if recent_messages %}
--- Prior Messages in Thread ---
{% for msg in recent_messages %}
From: {{ msg.sender }}
Subject: {{ msg.subject }}
Received: {{ msg.received_at }}
Body:
{{ msg.body_text_clean or msg.body_text }}
---
{% endfor %}
{% endif %}

--- Current Inbound Email ---
From: {{ current_message.sender }}
Subject: {{ current_message.subject }}
Received: {{ current_message.received_at }}
Body:
{{ current_message.body_text_clean or current_message.body_text }}

{% if retrieved_chunks %}
--- Relevant Product & Pricing Knowledge ---
{% for chunk in retrieved_chunks %}
[CITATION: {{ chunk.external_id or chunk.chunk_id }}]
{{ chunk.content }}
{% endfor %}
{% endif %}

{% if business_data %}
{{ business_data.render() }}
{% endif %}

Instructions:
1. Formulate a persuasive, professional reply addressing product features, tier options, and next steps for procurement.
2. Ground all pricing and product claims in the provided knowledge base context; take order and ticket facts only from [BUSINESS DATA].
3. Every cited chunk must be included in `knowledge_chunks` with its exact citation ID.
4. Output must strictly conform to the required JSON schema.
```

`prompts/general.v2.j2`:

```jinja
{{ agent_instructions }}

{{ category_instructions }}

{% if thread_summary %}
--- Thread Summary ---
{{ thread_summary }}
{% endif %}

{% if recent_messages %}
--- Prior Messages in Thread ---
{% for msg in recent_messages %}
From: {{ msg.sender }}
Subject: {{ msg.subject }}
Received: {{ msg.received_at }}
Body:
{{ msg.body_text_clean or msg.body_text }}
---
{% endfor %}
{% endif %}

--- Current Inbound Email ---
From: {{ current_message.sender }}
Subject: {{ current_message.subject }}
Received: {{ current_message.received_at }}
Body:
{{ current_message.body_text_clean or current_message.body_text }}

{% if retrieved_chunks %}
--- Relevant Knowledge Base Context ---
{% for chunk in retrieved_chunks %}
[CITATION: {{ chunk.external_id or chunk.chunk_id }}]
{{ chunk.content }}
{% endfor %}
{% endif %}

{% if business_data %}
{{ business_data.render() }}
{% endif %}

Instructions:
1. Formulate a polite, helpful, and concise response answering the inquiry accurately.
2. Ground all factual assertions in the provided context; take order, ticket and invoice facts only from [BUSINESS DATA].
3. Every cited chunk must be included in `knowledge_chunks` with its exact citation ID.
4. Output must strictly conform to the required JSON schema.
```

`{% if business_data %}` (not `is not none`) is deliberate: `render_prompt`'s dict path may leave `business_data` undefined, and a Jinja `Undefined` is falsy but not `none`.

- [ ] **Step 5: Bump the four profiles to v2**

In `config/agent_profiles.yaml` change, per profile:
- `technical_support`: `prompt_template: prompts/support.v2.j2`, `prompt_version: support.v2`
- `billing`: `prompt_template: prompts/billing.v2.j2`, `prompt_version: billing.v2`
- `sales`: `prompt_template: prompts/sales.v2.j2`, `prompt_version: sales.v2`
- `general_inquiry`: `prompt_template: prompts/general.v2.j2`, `prompt_version: general.v2`

(`context_policy` is unchanged: only `billing` has `thread_plus_rag_plus_business`.)

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest tests/unit/test_business_prompt_v2.py tests/unit/test_agent_profile.py tests/unit/test_single_pass_generator.py tests/unit/test_generation_budget.py tests/unit/test_context_builder.py tests/unit/test_context_builder_business.py -v`
Expected: PASS.

Run: `uv run ruff check --fix tests/unit/test_business_prompt_v2.py tests/unit/test_agent_profile.py && uv run ruff format tests/unit/test_business_prompt_v2.py tests/unit/test_agent_profile.py packages/llm/profile.py packages/context/builder.py` (several `registry.render_prompt(...)` lines in `test_business_prompt_v2.py` exceed 100 characters as written).

Run: `make ci`
Expected: all green (the composed ai-worker integration tests now render v2 templates; `prompt_version` recorded on drafts is `<x>.v2`).

- [ ] **Step 7: Commit**

```bash
git add prompts/support.v2.j2 prompts/billing.v2.j2 prompts/sales.v2.j2 prompts/general.v2.j2 \
  config/agent_profiles.yaml packages/llm/profile.py packages/context/builder.py \
  tests/unit/test_business_prompt_v2.py tests/unit/test_agent_profile.py \
  tests/unit/test_single_pass_generator.py tests/unit/test_generation_budget.py
git commit -m "$(cat <<'EOF'
feat(prompts): one [BUSINESS DATA] block and the precedence rule in v2 prompts [task 5.5] [R13.5, R13.6]

All four profile templates render business facts through BusinessContext.render() as one
[BUSINESS DATA] section with source and as_of, after retrieved knowledge, replacing the
four old headers. NOT_FOUND, NOT_LOOKED_UP and UNAVAILABLE facts appear explicitly. The
precedence rule is one constant used by both DefaultInstructionProvider and the registry
default. Profiles move to prompts/*.v2.j2 and prompt_version *.v2; v1 files stay for
reproducing drafts recorded under v1.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

**Overview of Tasks 8–9 (5.6 and the Phase 5 close).**

Tasks 8–9 write no production behaviour. It proves the behaviour built in Tasks 1–7 through the real ai-worker path, adds the owner-run live gate, and closes the phase.

```
 fixture rows (demo tenant) ──copy──▶ fresh org ──▶ email_message + QUEUED job
                                                        │ envelope (category lane)
                                                        ▼
  AIWorkerConsumer ─▶ ContextBuilder ─┬─ thread
                                      ├─ HybridRetriever ─▶ procedure chunk (doc category = lane category)
                                      └─ build_fetch_plan ─▶ fetch_business_context ─▶ PostgresBusinessDataProvider
                                                        │
                        CONTEXT_READY payload ◀─────────┤  (business_data, business_data_degraded)
                                                        ▼
                             SinglePassGenerator ─▶ FakeLLMProvider.recorded_calls   (CI: Task 8a)
                                                  ─▶ Gemini via openai provider      (live: Task 8b gate)
```

The CI test and the gate check the same things from two directions. The CI test reads the prompt the model receives. The live gate reads the draft the model wrote.

---

### Task 8: End-to-end business-data scenario test & live gate [tasks.md 5.6]

**Files:**
- Create: `tests/integration/test_business_data_e2e.py`
- Create: `scripts/phase5_gate.py`
- Create: `tests/unit/test_phase5_gate.py`
- Modify: `Makefile` (line 1 `.PHONY`; the help block, lines 5–19; add a target after `retrieval-gate`, line 96)

**Interfaces:**
- Consumes (from Tasks 2, 3, 6 and 7, exact names):
  - Fixture rows from Task 2:
    - `BUSINESS_ORDERS` holds a row with `order_number == "ORD-82915"` and `customer_id == CUST_ALICE_ID`.
    - `FIXTURE_EMAILS` holds an email with `sender_email == "alice.smith@clientcorp.com"` whose `body_text` contains `82915`.
    - `BUSINESS_TICKETS` has `TICK-4402` with `customer_id == CUST_EDWARD_ID` and `status == "open"` (already true).
  - `ContextBuilder(..., profile_registry=..., business_data_provider=..., business_timeout_ms=..., metrics=...)` as wired by Task 6 in `services/ai_worker/main.py::build_consumers(res, *, llm_provider=None, token_counter=None, embedder=None)`.
  - The `CONTEXT_READY` payload keys that Task 6 writes (`packages/business/fetch.py::business_payload`):
    - `customer_status`: `str`, or `None` when nothing was planned.
    - `business_fact_statuses`: `list[{"entity", "reference", "status", "reason"}]`, one `BusinessFact.to_payload()` per fact.
    - `business_plan`: `FetchPlan.to_payload()`; `business_data_degraded`: `bool`.
    - `retrieved_chunks_count`: `int` (existing).
  - `BusinessContext.render()`, from Tasks 3 and 7:
    - The block starts with the header `[BUSINESS DATA] source=... as_of=...` and is the last section of the prompt (section 7).
    - Each fact is one line that holds its reference, its `FactStatus` value and its attributes.
  - Metrics: `business_lookups_total{entity, status}` on the injected `res.metrics` registry.
- Produces:
  - `scripts/phase5_gate.py` with these pure, unit-tested helpers:
    - `check_settings(settings: AppSettings) -> None`
    - `lane_categories(settings: AppSettings) -> list[str]`
    - `check_context(payload: dict[str, Any], reference: str) -> None`
    - `check_draft(body: str, citations: Any, mismatch: bool, status: str, doc_ids: set[str]) -> str`
  - The async `run()` and the sync `main() -> int`.
  - A `make phase5-gate` target.

**Why the scenario test builds its own tenant, not `seed_database`.** Retrieval filters documents by the classification category. `RetrievalQueryBuilder.build` sets `filters["category"]` unless the category is in `{"general", "other", "unknown"}` (`packages/retrieval/query_builder.py:389`). Both SQL branches then apply `d.category = $4` (`packages/retrieval/postgres.py:171, :220`). The seeded procedure document has category `fulfillment` (`packages/db/seed.py:311` stores `doc.source_type`), and no ai-worker lane has that category. So no lane can retrieve it from the demo tenant.

For that reason, each test copies the demo tenant's business rows, one fixture email and the procedure chunk into a fresh `uuid4()` organization. It files the procedure document under the lane category the envelope uses. This follows the repo rule that integration tests use fresh org ids and never rely on the fixed seed ids, which `tests/integration/test_seed_loader.py` also writes.

**Which branch carries the chunk.** The lexical branch cannot. `websearch_to_tsquery` ANDs every term, and Alice's lexical text is `ORDER 82915 status order alice` (verified with `RetrievalQueryBuilder`). The procedure chunk contains no `82915`, and it must not, or RAG would carry the status (R13.3). With `EMBEDDING__MOCK=true`, the vector branch returns the organization's only chunk in that category, because pgvector applies no similarity floor. That proves wiring, not semantic quality, the same caveat `scripts/retrieval_gate.py` records. See "Open questions for the owner", item 2.

- [ ] **Step 1: Write the scenario test**

Create `tests/integration/test_business_data_e2e.py`:

```python
"""Phase 5 scenario on a real broker and Postgres: business facts reach the draft (5.6).

The stub LLM cannot state a status or cite a chunk (packages/llm/fake.py FAKE_REPLY), so these
tests prove the context side: the draft prompt the model receives, the CONTEXT_READY replay
payload and the business metrics. The live draft is checked by scripts/phase5_gate.py.

Each test copies the demo tenant's business rows, one fixture email and the order-status
procedure chunk into a fresh organization. The integration database is shared across the
package run, so the fixed ids of packages/db/seed.py are not reused. The procedure document is
filed under the envelope's category, because retrieval filters documents by the classification
category (packages/retrieval/query_builder.py:389).

Requirements: R13.3, R13.4, R13.5, R13.6, R16.1
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import aio_pika
import asyncpg
import pytest
from aio_pika.abc import AbstractChannel

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import setup_topology
from packages.broker.worker_runtime import WorkerResources
from packages.core.settings import (
    AIWorkerSettings,
    AppSettings,
    BrokerSettings,
    RetryLadderSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import (
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDERS,
    BUSINESS_TICKETS,
    CUST_ALICE_ID,
    CUST_DANA_ID,
    CUST_EDWARD_ID,
    DEMO_ORG_ID,
    FIXTURE_EMAILS,
    KNOWLEDGE_DOCS,
    EmailFixture,
)
from packages.db.job import PostgresJobStore
from packages.db.seed import deterministic_embed
from packages.domain.entities import Job
from packages.domain.state_machine import JobState
from packages.knowledge.embedder import FakeEmbedder
from packages.knowledge.token_counter import TokenCounter
from packages.llm import FakeLLMProvider
from packages.observability.health import HealthRegistry
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.ai_worker.main import build_consumers
from tests.integration.isolation import scratch_vhost

FAST_RETRY = RetryLadderSettings(
    tier_1_delay_s=1, tier_2_delay_s=2, tier_3_delay_s=3, max_retries=3
)
PROCEDURE_DOC_TITLE = "Acme Order Fulfillment & Tracking Guidelines"
PROCEDURE = next(
    chunk
    for doc in KNOWLEDGE_DOCS
    if doc.org_id == DEMO_ORG_ID and doc.title == PROCEDURE_DOC_TITLE
    for chunk in doc.chunks
    if chunk.chunk_index == 0
)
ALICE_EMAIL = next(
    f
    for f in FIXTURE_EMAILS
    if f.sender_email == "alice.smith@clientcorp.com" and "82915" in f.body_text
)
EDWARD_EMAIL = next(f for f in FIXTURE_EMAILS if f.key == "identifier_order_ticket")
ORDER_82915 = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-82915")
ORDER_9901 = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-9901")
TICKET_4402 = next(t for t in BUSINESS_TICKETS if t["ticket_number"] == "TICK-4402")


@pytest.fixture
async def broker() -> AsyncIterator[BrokerSettings]:
    async with scratch_vhost(AppSettings().broker, "bizsvc") as fast:
        yield fast


@pytest.fixture
async def channel(broker: BrokerSettings) -> AsyncIterator[AbstractChannel]:
    conn = await aio_pika.connect_robust(broker.url)
    ch = await conn.channel()
    await setup_topology(ch, broker, FAST_RETRY)
    try:
        yield ch
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool_from_settings(AppSettings().database)
    try:
        yield p
    finally:
        await p.close()


async def _seed_tenant(
    pool: asyncpg.Pool, fixture: EmailFixture, *, category: str
) -> tuple[Job, uuid.UUID]:
    """Copy the demo tenant's business rows, one email and the procedure chunk into a new org.

    Returns the QUEUED job for the email and the procedure chunk id.
    """
    org_id, mbx_id, thread_id, msg_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    doc_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    customer_ids = {
        c["id"]: uuid.uuid4() for c in BUSINESS_CUSTOMERS if c["organization_id"] == DEMO_ORG_ID
    }
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"biz-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Box', 'active')",
            mbx_id,
            org_id,
            f"box-{mbx_id.hex[:6]}@example.com",
        )
        for c in BUSINESS_CUSTOMERS:
            if c["id"] not in customer_ids:
                continue
            await conn.execute(
                "INSERT INTO customer (id, organization_id, email, name, account_status, tier)"
                " VALUES ($1, $2, $3, $4, $5, $6)",
                customer_ids[c["id"]],
                org_id,
                c["email"],
                c["name"],
                c.get("account_status", "active"),
                c.get("tier", "standard"),
            )
        for o in BUSINESS_ORDERS:
            if o["customer_id"] not in customer_ids:
                continue
            await conn.execute(
                'INSERT INTO "order" (id, organization_id, customer_id, order_number, status,'
                " total) VALUES ($1, $2, $3, $4, $5, $6)",
                uuid.uuid4(),
                org_id,
                customer_ids[o["customer_id"]],
                o["order_number"],
                o["status"],
                o["total"],
            )
        for t in BUSINESS_TICKETS:
            if t["customer_id"] not in customer_ids:
                continue
            await conn.execute(
                "INSERT INTO ticket (id, organization_id, customer_id, ticket_number, status,"
                " priority, subject) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                uuid.uuid4(),
                org_id,
                customer_ids[t["customer_id"]],
                t["ticket_number"],
                t["status"],
                t["priority"],
                t["subject"],
            )
        await conn.execute(
            "INSERT INTO knowledge_document (id, organization_id, title, category, status)"
            " VALUES ($1, $2, $3, $4, 'active')",
            doc_id,
            org_id,
            PROCEDURE_DOC_TITLE,
            category,
        )
        await conn.execute(
            "INSERT INTO knowledge_chunk (id, document_id, organization_id, chunk_index, section,"
            " category, content, version, content_tsv)"
            " VALUES ($1, $2, $3, 0, $4, $5, $6, 1, to_tsvector('english', $6))",
            chunk_id,
            doc_id,
            org_id,
            PROCEDURE.title,
            category,
            PROCEDURE.content,
        )
        await conn.execute(
            "INSERT INTO embedding_record (chunk_id, organization_id, model, dim, embedding)"
            " VALUES ($1, $2, 'text-embedding-3-small', 1536, $3)",
            chunk_id,
            org_id,
            deterministic_embed(PROCEDURE.content, dim=1536),
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text,
                received_at
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', $6, $7, '[]', $8, $9, $10)
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
            fixture.sender_email,
            fixture.sender_name,
            fixture.subject,
            fixture.body_text,
            datetime.now(UTC),
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.QUEUED.value,
            idempotency_key=f"bizsvc-{uuid.uuid4()}",
        )
    )
    return job, chunk_id


def _envelope(job: Job, category: str) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"gen-{job.id}",
        job_type="generate_reply",
        organization_id=str(job.organization_id),
        message_id=str(job.message_id),
        classification={"category": category, "priority": "normal", "retrieval_required": True},
    )


async def _resources(
    broker: BrokerSettings, pool: asyncpg.Pool, metrics: PipelineMetrics
) -> WorkerResources:
    settings = AIWorkerSettings(broker=broker, retry=FAST_RETRY)
    connection = await aio_pika.connect_robust(broker.url)
    publisher = MessagePublisher(
        broker_settings=broker, connection=connection, retry_settings=FAST_RETRY
    )
    return WorkerResources(
        settings=settings,
        db_pool=pool,
        connection=connection,
        publisher=publisher,
        health=HealthRegistry(service_name="test"),
        shutdown=GracefulShutdownCoordinator(),
        metrics=metrics,
    )


async def _publish(broker: BrokerSettings, lane: str, envelope: JobEnvelope) -> None:
    publisher = MessagePublisher(broker_settings=broker, retry_settings=FAST_RETRY)
    await publisher.connect()
    try:
        await publisher.publish(broker.exchange_email_route, lane, envelope)
    finally:
        await publisher.close()


async def _wait_for_state(pool: asyncpg.Pool, job: Job, state: JobState, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    current = None
    while asyncio.get_running_loop().time() < deadline:
        stored = await PostgresJobStore(pool).get_job(job.organization_id, job.id)
        current = stored.state if stored else None
        if current == state.value:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {job.id} ended {current}, expected {state.value}")


async def _draft_through_worker(
    broker: BrokerSettings,
    pool: asyncpg.Pool,
    job: Job,
    category: str,
    metrics: PipelineMetrics,
) -> FakeLLMProvider:
    """Run the composed ai-worker lane for `category` until the job is DRAFTED."""
    fake = FakeLLMProvider()
    res = await _resources(broker, pool, metrics)
    lane = f"email.{category}.normal"
    consumers = build_consumers(
        res, llm_provider=fake, token_counter=TokenCounter(), embedder=FakeEmbedder()
    )
    consumer = next(c for c in consumers if c.queue_name == lane)
    await consumer.start()
    try:
        await _publish(broker, lane, _envelope(job, category))
        await _wait_for_state(pool, job, JobState.DRAFTED, timeout_s=20)
    finally:
        await consumer.stop()
        await res.connection.close()
    return fake


def _draft_prompt(fake: FakeLLMProvider) -> str:
    calls = [
        c
        for c in fake.recorded_calls
        if "draft" in set((c["schema"] or {}).get("required", []))
    ]
    assert len(calls) == 1, f"expected one draft call, got {len(calls)}"
    return "\n".join(str(m.content) for m in calls[0]["messages"])


def _split_prompt(prompt: str) -> tuple[str, str]:
    """(everything before the business block, the business block and what follows it).

    The precedence rule and the v2 templates' instruction lines also name [BUSINESS DATA], so
    split on the block header `[BUSINESS DATA] source=`, which only render() writes.
    """
    head, sep, business = prompt.rpartition("[BUSINESS DATA] source=")
    assert sep, "the draft prompt has no [BUSINESS DATA] section"
    return head, business


def _fact_line(business: str, reference: str) -> str:
    lines = [ln for ln in business.splitlines() if reference in ln]
    assert len(lines) == 1, f"expected one {reference} line in [BUSINESS DATA]: {business!r}"
    return lines[0]


async def _context_ready_payload(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    events = await PostgresJobStore(pool).list_events_for_job(job.organization_id, job.id)
    event = next(e for e in events if e.state_to == JobState.CONTEXT_READY.value)
    return dict(event.payload or {})


def _payload_fact(payload: dict[str, Any], reference: str) -> dict[str, Any]:
    facts = [f for f in payload["business_fact_statuses"] if f["reference"] == reference]
    assert len(facts) == 1, f"expected one {reference} fact: {payload['business_fact_statuses']}"
    return dict(facts[0])


def _count(metrics: PipelineMetrics, name: str, **labels: str) -> float:
    total = 0.0
    for family in metrics.registry.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                total += sample.value
    return total


async def _draft_count(pool: asyncpg.Pool, job: Job) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
                job.organization_id,
                job.id,
            )
        )


def test_scenario_fixtures_hold_what_the_gate_assumes() -> None:
    """The seeded pairs behind both scenarios, and a procedure chunk that carries no status."""
    assert ORDER_82915["customer_id"] == CUST_ALICE_ID
    assert ORDER_9901["customer_id"] == CUST_DANA_ID
    assert TICKET_4402["customer_id"] == CUST_EDWARD_ID
    assert TICKET_4402["status"] == "open"
    # R13.3: the status can only reach the draft from the business tables, never from RAG.
    assert str(ORDER_82915["status"]).lower() not in PROCEDURE.content.lower()


async def test_order_status_email_carries_the_seeded_status_and_the_procedure_chunk(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Alice's "status of order 82915" email: ORD-82915 FOUND in [BUSINESS DATA], procedure
    chunk in the retrieved knowledge, job DRAFTED with one schema-valid draft."""
    category = "general_inquiry"  # thread_plus_rag: the typed ID alone plans the lookup
    metrics = create_pipeline_metrics()
    job, chunk_id = await _seed_tenant(pool, ALICE_EMAIL, category=category)

    fake = await _draft_through_worker(broker, pool, job, category, metrics)

    head, business = _split_prompt(_draft_prompt(fake))
    line = _fact_line(business, "ORD-82915")
    assert re.search(r"\bFOUND\b", line), line
    assert re.search(rf"\b{re.escape(str(ORDER_82915['status']))}\b", line), line
    assert f"[CITATION: {chunk_id}]" in head
    assert PROCEDURE.content in head

    payload = await _context_ready_payload(pool, job)
    assert payload["business_data_degraded"] is False
    assert payload["customer_status"] == "FOUND"
    assert _payload_fact(payload, "ORD-82915")["status"] == "FOUND"
    assert payload["retrieved_chunks_count"] >= 1
    assert _count(metrics, "business_lookups_total", entity="order", status="FOUND") == 1
    assert await _draft_count(pool, job) == 1


async def test_cross_customer_order_is_not_found_and_own_ticket_is_found(
    broker: BrokerSettings, channel: AbstractChannel, pool: asyncpg.Pool
) -> None:
    """Edward asks about Dana's ORD-9901 and his own TICK-4402 (R13.4, R13.6)."""
    category = "support"
    metrics = create_pipeline_metrics()
    job, _ = await _seed_tenant(pool, EDWARD_EMAIL, category=category)

    fake = await _draft_through_worker(broker, pool, job, category, metrics)

    _, business = _split_prompt(_draft_prompt(fake))
    order_line = _fact_line(business, "ORD-9901")
    assert "NOT_FOUND" in order_line, order_line
    # Dana's order exists in this tenant; its status must not leak into Edward's context.
    assert str(ORDER_9901["status"]) not in order_line
    ticket_line = _fact_line(business, "TICK-4402")
    assert re.search(r"\bFOUND\b", ticket_line), ticket_line
    assert re.search(r"\bopen\b", ticket_line), ticket_line

    payload = await _context_ready_payload(pool, job)
    assert payload["business_data_degraded"] is False
    assert payload["customer_status"] == "FOUND"
    assert _payload_fact(payload, "ORD-9901")["status"] == "NOT_FOUND"
    assert _payload_fact(payload, "TICK-4402")["status"] == "FOUND"
    assert _count(metrics, "business_lookups_total", entity="order", status="NOT_FOUND") == 1
    assert _count(metrics, "business_lookups_total", entity="ticket", status="FOUND") == 1
    assert await _draft_count(pool, job) == 1
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/integration/test_business_data_e2e.py -v`

Expected: 3 passed. This is a scenario test over behaviour that Tasks 1–7 built, so it should pass on first run if they are correct. If it fails, the failure names a defect in an earlier task, for example:
- a missing `[BUSINESS DATA]` section means the wiring is broken;
- a fact line without its status means `render()` is wrong;
- a missing `business_data` payload key means the Task 6 payload is wrong.

Fix the cause in that task's code, not in this test.

- [ ] **Step 3: Prove the test is not vacuous**

In `services/ai_worker/main.py`, delete the `business_data_provider=PostgresBusinessDataProvider(...)` argument from the `ContextBuilder(...)` call in `build_consumers`. The file is committed at this point.

Run: `uv run pytest tests/integration/test_business_data_e2e.py -v -k "order_status or cross_customer"`

Expected: 2 FAILED with `AssertionError: the draft prompt has no [BUSINESS DATA] section`. With no provider there is no fetch, so no block header is rendered. The precedence rule and the template instructions still mention the label, which is why `_split_prompt` splits on the header.

Then restore the file: `git checkout -- services/ai_worker/main.py`

Run: `uv run pytest tests/integration/test_business_data_e2e.py -v`

Expected: 3 passed.

- [ ] **Step 4: Commit the scenario test**

```bash
git add tests/integration/test_business_data_e2e.py
git commit -m "$(cat <<'EOF'
test(business): order-status and cross-customer scenarios through the ai-worker [task 5.6] [R13.3, R13.4, R13.5, R13.6, R16.1]

Drives Alice's order-82915 email and Edward's ORD-9901/TICK-4402 email through the composed
ai-worker lane on a real broker and Postgres with the stub LLM. Asserts the draft prompt's
[BUSINESS DATA] lines (ORD-82915 FOUND with its seeded status; ORD-9901 NOT_FOUND without
Dana's status; TICK-4402 FOUND open), the procedure chunk's citation line, the CONTEXT_READY
business payload and business_lookups_total. The procedure document is filed under the lane
category because retrieval filters documents by the classification category.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Write the failing unit test for the gate's pure checks**

Create `tests/unit/test_phase5_gate.py`:

```python
"""scripts/phase5_gate.py checks what the Phase 5 gate claims (5.6; R13.3, R13.5, R16.1)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from packages.core.settings import AppSettings, EmbeddingSettings, LLMTiersSettings
from packages.db.fixtures import CUST_ALICE_ID

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
DOC_A = "0b3f6f0e-0000-4000-8000-00000000000a"
DOC_B = "0b3f6f0e-0000-4000-8000-00000000000b"
GEMINI_KEY = "gemini-secret-key-for-test"


def _load_gate() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the script imports its sibling stack_smoke
    spec = importlib.util.spec_from_file_location("phase5_gate", SCRIPTS / "phase5_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _gemini(*, mock_embedder: bool) -> AppSettings:
    return AppSettings(
        _env_file=None,
        llm=LLMTiersSettings(
            provider="openai",
            openai_api_key=GEMINI_KEY,
            openai_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            fast_model="gemma-4-26b-a4b-it",
            strong_model="gemma-4-31b-it",
            fallback_model="gemini-3.1-flash-lite",
        ),
        embedding=EmbeddingSettings(mock=mock_embedder),
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "retrieved_chunks_count": 1,
        "business_data_degraded": False,
        "customer_status": "FOUND",
        "business_fact_statuses": [
            {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
        ],
    }
    payload.update(overrides)
    return payload


def test_the_fixture_pair_is_alices_order_and_email() -> None:
    assert gate.ORDER["order_number"] == "ORD-82915"
    assert gate.ORDER["customer_id"] == CUST_ALICE_ID
    assert gate.EMAIL.sender_email == gate.ALICE["email"]
    assert "82915" in gate.EMAIL.body_text
    # R13.3: the procedure the draft cites cannot supply the status.
    assert str(gate.ORDER["status"]).lower() not in gate.PROCEDURE.content.lower()


def test_check_settings_rejects_the_fake_model() -> None:
    with pytest.raises(gate.SmokeFailure, match="real model"):
        gate.check_settings(AppSettings(_env_file=None))


def test_check_settings_rejects_a_live_embedder() -> None:
    with pytest.raises(gate.SmokeFailure, match="EMBEDDING__MOCK"):
        gate.check_settings(_gemini(mock_embedder=False))


def test_check_settings_accepts_gemini_and_never_prints_the_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate.check_settings(_gemini(mock_embedder=True))
    out = capsys.readouterr().out
    assert "gemma-4-26b-a4b-it" in out
    assert GEMINI_KEY not in out


def test_lane_categories_follow_the_ai_worker_lanes() -> None:
    categories = gate.lane_categories(AppSettings(_env_file=None))
    assert {"support", "billing", "sales", "general_inquiry"} <= set(categories)
    assert len(categories) == len(set(categories))


def test_check_context_accepts_a_found_order() -> None:
    gate.check_context(_payload(), "ORD-82915")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"business_data_degraded": True}, "degraded"),
        ({"customer_status": None}, "customer"),
        ({"customer_status": "UNKNOWN_SENDER", "business_fact_statuses": []}, "customer"),
        (
            {"business_fact_statuses": [{"reference": "ORD-82915", "status": "NOT_FOUND"}]},
            "ORD-82915",
        ),
        ({"retrieved_chunks_count": 0}, "retrieved"),
    ],
)
def test_check_context_rejects(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(gate.SmokeFailure, match=message):
        gate.check_context(_payload(**overrides), "ORD-82915")


def test_check_draft_accepts_the_status_and_a_procedure_citation() -> None:
    citations = json.dumps([{"citation_id": "c1", "chunk_id": "x", "document_id": DOC_A}])
    hit = gate.check_draft(
        "Your order ORD-82915 has Shipped.", citations, False, "shipped", {DOC_A, DOC_B}
    )
    assert hit == DOC_A


def test_check_draft_accepts_the_spaced_form_of_an_underscored_status() -> None:
    citations = [{"document_id": DOC_B}]
    hit = gate.check_draft(
        "It is out for delivery today.", citations, False, "out_for_delivery", {DOC_B}
    )
    assert hit == DOC_B


@pytest.mark.parametrize(
    ("body", "citations", "mismatch", "message"),
    [
        ("We are looking into it.", [{"document_id": DOC_A}], False, "seeded status"),
        ("It was unshipped.", [{"document_id": DOC_A}], False, "seeded status"),
        ("It has shipped.", [{"document_id": "another-doc"}], False, "procedure"),
        ("It has shipped.", [], False, "procedure"),
        ("It has shipped.", [{"document_id": DOC_A}], True, "mismatch"),
    ],
)
def test_check_draft_rejects(body: str, citations: Any, mismatch: bool, message: str) -> None:
    with pytest.raises(gate.SmokeFailure, match=message):
        gate.check_draft(body, citations, mismatch, "shipped", {DOC_A})
```

- [ ] **Step 6: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_phase5_gate.py -v`

Expected: collection ERROR with `FileNotFoundError: ... scripts/phase5_gate.py`, because the script does not exist yet.

- [ ] **Step 7: Write the gate script**

Create `scripts/phase5_gate.py`:

```python
"""Live-stack check for the Phase 5 gate (specs/tasks.md 5.6; R13.3, R13.5, R16.1).

Run on the host by the owner, never under pytest (it needs a live model key, R24.5). The
stack must run the openai provider on the Gemini endpoint (task 5.0) and the mock embedder:

    # .env: LLM__PROVIDER=openai, LLM__OPENAI_API_KEY=<Gemini key>,
    #       LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai,
    #       LLM__FAST_MODEL / LLM__STRONG_MODEL / LLM__FALLBACK_MODEL, EMBEDDING__MOCK=true
    make up
    make phase5-gate

The check reads the host .env through AppSettings. Compose forwards the same keys into the
containers (task 5.0), so the host settings stand in for the containers' settings.

Checks, stopping at the first failure:
  1. The settings name a real model and the mock embedder. The key is never printed.
  2. The API is ready and every hosted queue has a consumer.
  3. The order-status procedure (the seeded "Order Status & Tracking Numbers" chunk) is uploaded
     once per ai-worker lane category and ingested to `active`. Retrieval filters documents by
     the triage category (packages/retrieval/query_builder.py:389), and the live triage decides
     the category, so every lane gets a copy.
  4. Alice's fixture email, from her address, reaches DRAFTED through
     normalize -> triage -> ai-worker.
  5. Its CONTEXT_READY event records customer FOUND, ORD-82915 FOUND, business data not
     degraded, and at least one retrieved chunk.
  6. The one draft states the exact seeded status (case-insensitive, whole word; an underscored
     status also matches its spaced form), cites a chunk of a procedure copy, and has no
     citation mismatch.
The procedure text does not contain the status (asserted by tests/unit/test_phase5_gate.py),
so a draft stating it took it from [BUSINESS DATA]. With EMBEDDING__MOCK=true the procedure
reaches the prompt through the vector branch (no similarity floor, one chunk per category): the
lexical branch ANDs the order number, which the procedure does not contain. That proves
wiring, not semantic retrieval quality.
Creates one throwaway organization and deletes it at the end, with its raw MIME and knowledge
objects.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from email.utils import formataddr
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import asyncpg
import httpx
import stack_smoke
from stack_smoke import (
    SmokeFailure,
    build_mime,
    check_services,
    inject_email,
    seed_tenant,
    wait_for_state,
)

from packages.core.settings import AppSettings
from packages.core.storage import get_storage_client
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures import BUSINESS_CUSTOMERS, BUSINESS_ORDERS, FIXTURE_EMAILS, KNOWLEDGE_DOCS
from packages.domain.state_machine import JobState
from services.ai_worker.main import resolve_lane_queues

API = "http://localhost:8000/v1"
INGEST_TIMEOUT_S = 60.0
# Two live model calls (triage may use the LLM stage, then the draft) on a free-tier endpoint.
DRAFT_TIMEOUT_S = 120.0
PROCEDURE_DOC_TITLE = "Acme Order Fulfillment & Tracking Guidelines"

ALICE = next(c for c in BUSINESS_CUSTOMERS if c["email"] == "alice.smith@clientcorp.com")
ORDER = next(o for o in BUSINESS_ORDERS if o["order_number"] == "ORD-82915")
EMAIL = next(f for f in FIXTURE_EMAILS if f.sender_email == ALICE["email"] and "82915" in f.body_text)
PROCEDURE = next(
    chunk
    for doc in KNOWLEDGE_DOCS
    if doc.org_id == ALICE["organization_id"] and doc.title == PROCEDURE_DOC_TITLE
    for chunk in doc.chunks
    if chunk.chunk_index == 0
)


def check_settings(settings: AppSettings) -> None:
    """Fail unless a real model and the mock embedder are configured; never print the key."""
    if settings.llm.provider == "fake":
        raise SmokeFailure(
            "LLM__PROVIDER is 'fake': the gate needs a real model (set it up as in task 5.0)"
        )
    if not settings.embedding.mock:
        raise SmokeFailure(
            "EMBEDDING__MOCK must be true: a live embedder is out of Phase 5 scope"
        )
    print(
        f"ok   provider {settings.llm.provider} at {settings.llm.openai_base_url}; models "
        f"{settings.llm.fast_model} / {settings.llm.strong_model}; mock embedder"
    )


def lane_categories(settings: AppSettings) -> list[str]:
    """The categories of the lanes the ai-worker consumes (`email.<category>.<lane>`)."""
    return sorted({queue.split(".")[1] for queue in resolve_lane_queues(settings)})


def check_context(payload: dict[str, Any], reference: str) -> None:
    """The CONTEXT_READY payload records the customer and the order as FOUND, not degraded."""
    if payload.get("business_data_degraded") is not False:
        raise SmokeFailure(
            f"business data degraded or unrecorded: {payload.get('business_data_degraded')!r}"
        )
    if payload.get("customer_status") != "FOUND":
        raise SmokeFailure(f"customer_status {payload.get('customer_status')!r}, expected FOUND")
    rows = payload.get("business_fact_statuses") or []
    facts = [f for f in rows if f.get("reference") == reference]
    if [f.get("status") for f in facts] != ["FOUND"]:
        raise SmokeFailure(f"{reference} facts {facts}, expected one FOUND fact")
    if not payload.get("retrieved_chunks_count"):
        raise SmokeFailure(
            f"retrieved_chunks_count {payload.get('retrieved_chunks_count')!r}, expected >= 1"
        )


def check_draft(body: str, citations: Any, mismatch: bool, status: str, doc_ids: set[str]) -> str:
    """The draft states the seeded status and cites a procedure copy; returns the cited doc id."""
    forms = {status.lower(), status.lower().replace("_", " ")}
    if not any(re.search(rf"\b{re.escape(form)}\b", body.lower()) for form in forms):
        raise SmokeFailure(f"draft does not state the seeded status {status!r}: {body[:400]!r}")
    if mismatch:
        raise SmokeFailure("draft has a citation mismatch")
    parsed = json.loads(citations) if isinstance(citations, str) else (citations or [])
    cited = {str(c.get("document_id")) for c in parsed}
    hits = sorted(cited & doc_ids)
    if not hits:
        raise SmokeFailure(
            f"draft cites documents {sorted(cited)}, none of the procedure copies {sorted(doc_ids)}"
        )
    return hits[0]


async def seed_business(pool: asyncpg.Pool[Any], org_id: UUID) -> None:
    """Alice and her ORD-82915, copied from the demo fixtures into the throwaway org."""
    customer_id = uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO customer (id, organization_id, email, name, account_status, tier) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            customer_id,
            org_id,
            ALICE["email"],
            ALICE["name"],
            ALICE.get("account_status", "active"),
            ALICE.get("tier", "standard"),
        )
        await conn.execute(
            'INSERT INTO "order" (id, organization_id, customer_id, order_number, status, total) '
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid4(),
            org_id,
            customer_id,
            ORDER["order_number"],
            ORDER["status"],
            ORDER["total"],
        )
    print(f"ok   seeded {ALICE['email']} with {ORDER['order_number']} ({ORDER['status']})")


async def upload_procedure(
    http: httpx.AsyncClient, org_id: UUID, category: str
) -> tuple[str, str | None]:
    """Upload one procedure copy under `category`; return its id and object key once active."""
    headers = {"X-Organization-ID": str(org_id)}
    resp = await http.post(
        f"{API}/knowledge/documents",
        headers=headers,
        files={"file": ("order-status.txt", PROCEDURE.content.encode(), "text/plain")},
        data={"title": f"{PROCEDURE.title} ({category})", "category": category},
    )
    if resp.status_code != 202:
        raise SmokeFailure(f"upload returned {resp.status_code}: {resp.text}")
    document = resp.json()["document"]
    doc_id = str(document["id"])
    object_key = document.get("object_key")
    deadline = time.monotonic() + INGEST_TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        got = await http.get(f"{API}/knowledge/documents/{doc_id}", headers=headers)
        status = got.json().get("status") if got.status_code == 200 else None
        if status == "active":
            return doc_id, object_key
        if status == "failed":
            break
        await asyncio.sleep(1.0)
    raise SmokeFailure(f"document {doc_id} ({category}) ended in {status!r}, expected 'active'")


async def run() -> None:
    settings = AppSettings()
    check_settings(settings)
    await check_services(settings)
    stack_smoke.TIMEOUT_S = DRAFT_TIMEOUT_S  # wait_for_state reads it at call time
    pool = await create_pool_from_settings(settings.database)
    connection = await aio_pika.connect_robust(settings.broker.url)
    storage = get_storage_client(settings.object_storage)
    org_id: UUID | None = None
    raw_keys: list[str] = []
    object_keys: list[str] = []
    try:
        channel = await connection.channel(on_return_raises=True)
        org_id, mailbox_id = await seed_tenant(pool)
        await seed_business(pool, org_id)

        doc_ids: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=10.0) as http:
            for category in lane_categories(settings):
                doc_id, object_key = await upload_procedure(http, org_id, category)
                doc_ids[doc_id] = category
                if object_key:
                    object_keys.append(object_key)
        print(f"ok   procedure ingested -> active for {sorted(doc_ids.values())}")

        job_id = await inject_email(
            settings,
            pool,
            storage,
            channel,
            org_id,
            mailbox_id,
            # Display-name From header, so the parseaddr split runs end to end (Review Focus 1).
            build_mime(
                formataddr((EMAIL.sender_name, EMAIL.sender_email)), EMAIL.subject, EMAIL.body_text
            ),
            raw_keys,
        )
        await wait_for_state(pool, org_id, job_id, {JobState.DRAFTED.value})
        print(f"ok   {EMAIL.subject!r} from {EMAIL.sender_email} -> ai-worker, job DRAFTED")

        raw_payload = await pool.fetchval(
            "SELECT payload::text FROM processing_event WHERE organization_id = $1 "
            "AND job_id = $2 AND state_to = 'CONTEXT_READY' ORDER BY created_at LIMIT 1",
            org_id,
            job_id,
        )
        if raw_payload is None:
            raise SmokeFailure(f"job {job_id} has no CONTEXT_READY event")
        payload = json.loads(raw_payload)
        check_context(payload, str(ORDER["order_number"]))
        print(
            f"ok   CONTEXT_READY: customer FOUND, {ORDER['order_number']} FOUND, not degraded, "
            f"{payload['retrieved_chunks_count']} chunk(s) retrieved"
        )

        rows = await pool.fetch(
            "SELECT body, citations, citation_mismatch, model_tier, escalation_reason "
            "FROM generated_draft WHERE organization_id = $1 AND job_id = $2",
            org_id,
            job_id,
        )
        if len(rows) != 1:
            raise SmokeFailure(f"job {job_id} has {len(rows)} drafts, expected 1")
        draft = rows[0]
        hit = check_draft(
            draft["body"] or "",
            draft["citations"],
            bool(draft["citation_mismatch"]),
            str(ORDER["status"]),
            set(doc_ids),
        )
        print(
            f"ok   draft states {ORDER['status']!r} and cites the procedure copy filed under "
            f"{doc_ids[hit]!r}; tier {draft['model_tier']} ({draft['escalation_reason']})"
        )
        print(f"     draft: {draft['body']}")
    finally:
        if org_id is not None:
            await pool.execute("DELETE FROM organization WHERE id=$1", org_id)
        for key in raw_keys:
            try:
                await storage.delete_object(settings.object_storage.bucket_raw_mime, key)
            except Exception as err:
                print(f"warn could not delete raw object {key}: {err}", file=sys.stderr)
        for key in object_keys:
            try:
                await storage.delete_object(settings.object_storage.bucket_knowledge, key)
            except Exception as err:
                print(f"warn could not delete knowledge object {key}: {err}", file=sys.stderr)
        await connection.close()
        await pool.close()


def main() -> int:
    try:
        asyncio.run(run())
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("PHASE 5 GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Add the Makefile target**

In `Makefile`, line 1, append ` phase5-gate` to the `.PHONY` list, right after `retrieval-gate`.

In the help block, add this line after the `smoke` echo (line 19):

```make
	@echo "  phase5-gate - Live Phase 5 gate on a real model, owner-run (task 5.6)"
```

After the `retrieval-gate` target (line 96), add:

```make

phase5-gate:
	$(UV) run python scripts/phase5_gate.py
```

- [ ] **Step 9: Run the unit test to pass, then lint**

Run: `uv run pytest tests/unit/test_phase5_gate.py -v`

Expected: 18 passed (8 plain tests, plus 5 + 5 parametrized cases).

Run:

```
uv run ruff format scripts/phase5_gate.py tests/unit/test_phase5_gate.py tests/integration/test_business_data_e2e.py
uv run ruff check scripts tests
uv run mypy tests
```

Expected: clean. `scripts/` is ruff-checked but not mypy-checked. Ruff may reorder `import stack_smoke` within the third-party block. Accept its order.

Run: `make -n phase5-gate`

Expected output: `uv run python scripts/phase5_gate.py`.

- [ ] **Step 10: Commit the gate**

```bash
git add scripts/phase5_gate.py tests/unit/test_phase5_gate.py Makefile
git commit -m "$(cat <<'EOF'
feat(gate): live Phase 5 gate and make phase5-gate [task 5.6] [R13.3, R13.5, R16.1, R24.5]

Owner-run script: seeds Alice and ORD-82915 in a throwaway org, uploads the order-status
procedure once per ai-worker lane category (retrieval filters by triage category), drives
Alice's email through normalize -> triage -> ai-worker on the configured real model, and
checks that CONTEXT_READY records ORD-82915 FOUND, that the draft states the exact seeded
status and cites a procedure copy. Refuses to run on the fake model or a live embedder and
never prints the key. Pure checks are unit-tested offline.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Phase 5 close: `make ci`, completion audit, tasks.md and gate evidence [tasks.md 5.0–5.6, Phase 5 gate]

**Files:**
- Modify: `specs/tasks.md`:
  - the Phase 5 task entries, lines 609–670 (checkbox and closing note per task);
  - a new gate-evidence block right after the `> **Phase 5 gate:**` paragraph (line 672).

**Interfaces:**
- Consumes: all of Tasks 0–8 committed (Task 0 put the approved design, ADR-0008 and the Phase 5 tasks.md rewrite in history); the owner's live run of `make up` and `make phase5-gate`; the owner's answers to Open Questions 1 and 2.
- Produces: the flipped checkboxes and the Phase 5 gate evidence block.

- [ ] **Step 1: Full CI**

Run: `make ci`

Expected: exit 0. The run covers `ruff format --check .`, `ruff check .`, strict mypy on `packages services tests evaluation`, `pytest tests/unit` and `pytest tests/integration` (in `rag_email_test`).

Record the unit and integration pass counts from the two pytest summary lines. If any step fails, fix the cause in the owning task's code and re-run. Do not edit tests to go green.

- [ ] **Step 2: Completion audit, one per task 5.0–5.6**

Dispatch a reviewer (`superpowers:requesting-code-review`) over `git diff 2b4fab9..HEAD`. The brief for each task:
- Trace every bullet in its `specs/tasks.md` entry, and every cited requirement ID, to code and to a test that fails without it.
- Check CLAUDE.md §3 DoD items 1–6:
  - new keys are in both `.env.example` and `docs/configuration.md` (`LLM__*` from 5.0, `BUSINESS_DATA__*` from 5.4);
  - the R21 metrics and the `business_fetch` log line are emitted.
- For 5.2: the shared contract suite covers typed-ID lookups, snapshot rows, `NOT_FOUND`, `NOT_LOOKED_UP` and the provider-level statuses `FOUND`, `UNKNOWN_SENDER` and `AMBIGUOUS_CUSTOMER`. `UNAVAILABLE` is a caller-level status set by `fetch_business_context` on timeout or error; confirm it is covered by `tests/unit/test_business_fetch.py` and the `statement_timeout` integration test, and record that split in the verdict.
- Check the rules:
  - every tenant query carries `organization_id`;
  - `packages/domain/business.py` imports only stdlib and `packages.core`;
  - no test needs live credentials.

Record the verdict per task as PASS, PASS WITH NOTES or FAIL. For each finding, fix it with a RED→GREEN test and re-run `make ci`. Do this before any checkbox flips.

- [ ] **Step 3: Flip the checkboxes the audit allows**

In `specs/tasks.md`, flip a task to `[x]` only if three things hold: its audit passed, it has no owner-run leg outstanding, and no owner decision on its written bullets is outstanding (CLAUDE.md §3 and §7: do not close on an interpretation the owner has not confirmed). Mark a task `[~]` in these cases:
- 5.0 until the owner has run its live smoke script.
- 5.3 until the owner has answered Open Question 1 and tasks.md 5.3 (or design §5.4) states the chosen rule for invoice references when the sender is unresolved, and the code and `test_unknown_sender_marks_every_planned_fact_not_looked_up` follow it. Sub-bullet: `Left: owner decision on Open Question 1 (invoice reason when the sender is unresolved).`
- 5.6 until the owner has run `make phase5-gate` **and** has answered Open Question 2 by editing the tasks.md 5.6 bullet (and design, if needed) to the vector-branch wording.

Under each flipped task, append a closing sub-bullet in the form the repo already uses (see 3.16, `specs/tasks.md:374`). For example, for 5.2:

```markdown
  - Closed 2026-MM-DD after a completion audit (PASS WITH NOTES; `make ci` green, unit N, integration M). Both providers pass `BusinessDataProviderContractSuite` (typed-ID lookups, snapshot rows, `NOT_FOUND`, `NOT_LOOKED_UP`, and the provider-level statuses `FOUND`, `UNKNOWN_SENDER`, `AMBIGUOUS_CUSTOMER`). `UNAVAILABLE` is set by the caller, `fetch_business_context`, and is covered by `tests/unit/test_business_fetch.py` and the `statement_timeout` integration test. The fix pass before flipping:
    - <one line per audit finding and the test that pinned it>
```

N and M are the counts from Step 1. The findings list comes from Step 2. Write "none" if there were none.

For a `[~]` task, write one sub-bullet that says what is left and who does it. For example: `Left: the owner runs make phase5-gate against Gemini (see the Phase 5 gate evidence below).`

- [ ] **Step 4: Commit the audit close**

```bash
git add specs/tasks.md
git commit -m "$(cat <<'EOF'
docs(tasks): close Phase 5 tasks after their completion audit [task 5.0–5.6] [R13.1, R13.2, R13.3, R13.4, R13.5, R13.6, R13.7, R5.9, R14.7, R20.6, R21.3, R21.6, R24.5]

Records the audit verdicts, the fix pass and make ci counts for tasks 5.0–5.6, and
flips the tasks the audit allows. 5.0 and 5.6 stay [~] until the owner runs the live
smoke script and make phase5-gate; 5.3 and 5.6 also stay [~] while Open Questions 1
and 2 are unanswered. Adjust the tag and this body to the tasks actually closed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: The owner runs the live gate**

The owner runs this; Claude does not restart the live stack. Hand over these steps:

1. In `.env`, set:
   - `LLM__PROVIDER=openai`
   - the Gemini key in `LLM__OPENAI_API_KEY`
   - `EMBEDDING__MOCK=true`

   The base URL and the model names are already set in `.env`.
2. Run `make up` so the images rebuild from HEAD and compose forwards the new keys.
3. Run `make phase5-gate`.
4. Regression: run `make smoke`, `make retrieval-gate` and `make phase4-gate`. `phase4-gate` now runs on the real model, so its citation leg may show a non-zero count.

Expected: `PHASE 5 GATE OK`, after these lines:
- `ok   provider openai ...`
- `ok   api ready ...`
- `ok   seeded alice.smith@clientcorp.com ...`
- `ok   procedure ingested ...`
- `ok   ... job DRAFTED`
- `ok   CONTEXT_READY ...`
- `ok   draft states ...`
- `     draft: ...`

If it prints `FAIL ...`, the message names the check that failed. Debug the cause with `superpowers:systematic-debugging` and fix it with a RED→GREEN test. The owner then re-runs the gate. Record each defect under "Defects the gate found and fixed".

A model that paraphrases the status fails the check. For example, "on its way" for `shipped` is a real gate failure, not a flaky check. The precedence rule (Task 7) tells the model to state the status from `[BUSINESS DATA]`. Record the draft line in that case, and do not loosen `check_draft`.

- [ ] **Step 6: Write the Phase 5 gate evidence block**

Add the block after the `> **Phase 5 gate:**` paragraph (`specs/tasks.md:672`). Mirror the Phase 4 block (`specs/tasks.md:588-599`): a dated header naming the script and who ran the stack, one bullet per gate clause, then "Not live", then "Defects", then the CI line.

Take every value from the owner's gate output and the Step 1 counts. Do not add any value that the output does not show.

```markdown
>
> **Gate evidence (2026-MM-DD, live stack, `scripts/phase5_gate.py` / `make phase5-gate`; the user ran `make up` with `LLM__PROVIDER=openai` on the Gemini OpenAI-compatible endpoint, models `<fast>` / `<strong>`, `EMBEDDING__MOCK=true`):**
> - Order status: Alice's "What is the status of order 82915?" email went normalize → triage → ai-worker and reached `DRAFTED` with one draft. `CONTEXT_READY` recorded customer `FOUND`, `ORD-82915` `FOUND`, `business_data_degraded=false`, and `<n>` retrieved chunk(s).
> - Knowledge/transactional split: the draft (tier `<tier>`, `<escalation_reason>`) states the seeded status `<status>` and cites the order-status procedure copy filed under `<category>`, with no citation mismatch. The procedure text does not contain the status (asserted by the gate's unit test and by `test_scenario_fixtures_hold_what_the_gate_assumes`), so the status came from `[BUSINESS DATA]`. Draft: "<first sentence of the draft>".
> - Context side in CI: `tests/integration/test_business_data_e2e.py` drives the composed ai-worker with the stub LLM.
>   - Alice: `ORD-82915` `FOUND` with its seeded status in `[BUSINESS DATA]`, and the procedure chunk's `[CITATION: …]` line in the retrieved knowledge.
>   - Edward: `ORD-9901` (Dana's) is `NOT_FOUND` with no status leak, and `TICK-4402` is `FOUND` `open`.
>   - `business_lookups_total` moved for each.
> - Retrieval: with the mock embedder the procedure reaches the prompt through the vector branch. The lexical branch ANDs the order number, which the procedure does not contain. This proves wiring, not semantic quality, as with `make retrieval-gate`.
> - Not live: `UNAVAILABLE`/timeout degradation (proven by the Task 6 unit and integration tests with a slow provider), and `UNKNOWN_SENDER`/`AMBIGUOUS_CUSTOMER` (proven by the 5.3 Postgres tests).
> - Regression: `make smoke`, `make retrieval-gate` and `make phase4-gate` still pass.
> - Defects the gate found and fixed:
>   - <one line per defect with its RED→GREEN test, or "none">
>
>   `make ci` passed afterwards (unit N, integration M).
```

Then flip 5.0 to `[x]` with its closing sub-bullet if the owner has run the 5.0 smoke script. Flip 5.6 to `[x]` only if the owner has also edited the tasks.md 5.6 bullet (and design, if needed) to the vector-branch wording (Open Question 2); the evidence above says the vector branch carried the chunk, and the current bullet says the lexical branch does, so closing against the current bullet would close a task whose written bullet is knowingly unmet (CLAUDE.md §3, §7.4). Otherwise leave 5.6 at `[~]` with `Left: owner decision on Open Question 2 (5.6 lexical-branch bullet).`

- [ ] **Step 7: Commit the gate evidence**

Name only the tasks this commit actually closes in the subject and the `[task …]` tag (CLAUDE.md §2). If 5.6 stays `[~]` for Open Question 2, the subject becomes `record the Phase 5 gate evidence and close 5.0 [task 5.0, 5.6]`. When the owner later answers Open Question 1 or 2, close 5.3 or 5.6 in its own `docs(tasks)` commit tagged with that task and its requirement IDs (5.3: R13.4; 5.6: R13.3, R13.5, R16.1).

```bash
git add specs/tasks.md
git commit -m "$(cat <<'EOF'
docs(tasks): record the Phase 5 gate evidence and close 5.0 and 5.6 [task 5.0, 5.6] [R13.3, R13.5, R16.1, R14.7, R20.6, R21.6, R24.5]

The owner ran make phase5-gate on the Gemini endpoint: the order-status draft states the
seeded ORD-82915 status from [BUSINESS DATA] and cites the order-status procedure.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

## Open questions for the owner

The plan resolves every other conflict between its parts. These seven need an owner decision, or an owner edit to a spec that CLAUDE.md reserves for the owner. Task 0 Step 1 asks Questions 1 and 2 before any code. None of them blocks Tasks 1–8 as written, but Questions 1 and 2 block closing a task: 5.3 stays `[~]` until Question 1 is answered and the spec states the rule, and 5.6 stays `[~]` until Question 2 is answered with a spec edit (Task 9 Steps 3 and 6).

1. **Invoice references when the sender is not resolved (Task 3).** tasks.md 5.3 says that for `UNKNOWN_SENDER` / `AMBIGUOUS_CUSTOMER`, *every* planned fact is `NOT_LOOKED_UP` with reason `unknown_sender` / `ambiguous_customer`. The plan keeps `unsupported_entity` for `INV-` refs in that case, because an invoice is never looked up for any sender and design §5.4 gives it that reason. To follow the literal text instead, change `_not_looked_up` in `packages/business/assemble.py` and the expected invoice row in `test_unknown_sender_marks_every_planned_fact_not_looked_up` (Task 3, the note before Step 8). Either way, the chosen rule must be written into tasks.md 5.3 or design §5.4 before 5.3 can be closed.
2. **tasks.md 5.6 says "the lexical branch carries the procedure chunk"; for the gate email it cannot.** `websearch_to_tsquery` ANDs every term (`packages/retrieval/postgres.py:169`). Alice's lexical text is `ORDER 82915 status order alice`, and the procedure must not contain `82915` (R13.3). With `EMBEDDING__MOCK=true`, the chunk reaches the prompt through the vector branch, because pgvector applies no similarity floor. That proves wiring, not semantic quality. Proposed wording: "the procedure chunk reaches the prompt through the vector branch with the mock embedder (wiring, not semantic quality)". This is a spec edit, so the plan does not make it, and 5.6 stays `[~]` until the owner makes it (Task 9 Step 6).
3. **Seeded knowledge is unreachable from every ai-worker lane (pre-existing, outside Phase 5).** Retrieval filters on `d.category = <classification category>` unless the category is `general`, `other` or `unknown` (`packages/retrieval/query_builder.py:389`). The seed files documents under their `source_type` (`fulfillment`, `policy`, …), and no lane category matches those. Task 8 therefore files the procedure under the lane category in a fresh org, and the live gate uploads one copy per lane category. Decide whether to accept this workaround, or to open a follow-up task: map document categories to triage categories, or add `general_inquiry` to `ignored_categories_for_filter`.
4. **Design §13.3 says "every app container".** Only the services that merge `x-app-env` receive the forwarded LLM and `BUSINESS_DATA__*` variables: init, api, mail-connector, email-worker, triage-worker, knowledge-worker and ai-worker. dispatch-worker (a Phase-0 stub) and frontend make no LLM calls. Should §13.3's wording be changed to match?
5. **`EMBEDDING__MOCK` in the test guard.** Task 1 makes the autouse guard in `tests/conftest.py` pin `LLM__PROVIDER=fake`, so a host `.env` set up for live runs cannot reach tests (R24.5). `EMBEDDING__MOCK=false` in the host `.env` has the same exposure. Pinning it too is outside 5.0's scope. Should the guard also pin `EMBEDDING__MOCK=true`?
6. **Products are not looked up.** The proposal lists `product` among the Phase 5 tables. `product` and `order_item` are seeded (R13.1), including the two items of `ORD-82915`, but `EntityType` has no product value. So SKUs and item lines never reach `[BUSINESS DATA]`. Is that acceptable for Phase 5, or should the order fact carry item attributes?
7. **Small spec-conformant edges, left as specified.**
   - A bare "order 2026-09-20" matches `ORD-2026` under the "at least 4 digits" rule. The provider then answers `NOT_FOUND`, which is honest.
   - `business.fetch` is a direct child of `broker.consume`, because the `context.build` span shown in design §10 does not exist yet.
   - A reply whose subject still reads "Re: Order ORD-…" plans that order again: the subject is always scanned, and only the quoted body is excluded.

   Confirm, or open follow-ups.
