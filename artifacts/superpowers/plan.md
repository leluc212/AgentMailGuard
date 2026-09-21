# Implementation Plan: Phase 2 — Task 2.2 Rule Engine (Triage Stage 1)

**Spec Alignment:** `specs/tasks.md Task 2.2` · `specs/requirements.md (R6.1, R6.8, R24.3)` · `specs/design.md §5.3` · `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md §11` · `GEMINI.md §2, §4, §8`

### Goal
Implement the declarative, hot-reloadable rule engine (Triage Stage 1) capable of evaluating sender patterns, header signals (`List-Unsubscribe`, `Auto-Submitted`), subject patterns, and body patterns without hard-coded conditionals, returning authoritative `Classification` objects (`category`, `intent`, `priority`, `reply_required`, `workflow_hint`, `retrieval_required`, `confidence`, `decided_by='rule'`) and verified by pure unit tests and a fixture email regression suite.

### Assumptions
1. The rule engine is a pure component (R24.3) with sub-2ms evaluation latency that executes as Stage 1 of the cascading classifier (R6.1).
2. Rule conditions are declarative data structures (YAML/JSON) with composite logic (`any`, `all`, `not`) and regex/equality operators on headers, sender, subject, and body (R6.8).
3. The hot-reload mechanism detects file modifications dynamically via `mtime` without restarting the worker and tolerates syntax/regex errors gracefully by retaining the previous valid ruleset.
4. When a rule matches, it produces a `Classification` entity with `decided_by='rule'`. When no rule matches, it returns `None`, allowing the cascade to proceed to Stage 2 (lightweight ML).
5. `NormalizedMessage` captures relevant email headers (case-insensitive) so header predicates (`Auto-Submitted`, `List-Unsubscribe`, `Precedence`) evaluate directly.

---

### Plan

1. **Step 1: Header Capture in Email Normalization (`packages/domain/entities.py`, `services/email_worker/`)**
   - Files: `packages/domain/entities.py`, `services/email_worker/parser.py`, `services/email_worker/normalizer.py`
   - Change:
     - Add `headers: dict[str, str] = field(default_factory=dict)` to `NormalizedMessage` and `ParsedHeaders`.
     - In `parser.py`, capture all RFC 822 MIME headers into a case-insensitive dictionary preserving headers like `Auto-Submitted`, `List-Unsubscribe`, `Precedence`, and `Content-Type`.
     - Wire headers through `EmailNormalizer.normalize()`.
   - Verify: `uv run pytest tests/unit/test_email_normalization.py -v`

2. **Step 2: Pure Domain Rule Engine (`packages/domain/rules.py`, `packages/domain/__init__.py`)**
   - Files: `packages/domain/rules.py` (new), `packages/domain/__init__.py` (modify)
   - Change:
     - Implement `RuleCondition`, `RuleAction`, `Rule`, `EmailContext`, and `RuleEngine`.
     - Support field paths: `header.<name>`, `sender.email`, `sender.name`, `subject`, `body`, `attachments`.
     - Support operators: `exists`, `matches` (compiled regex), `equals`, `contains`, `starts_with`, `ends_with`, and boolean composites `any`, `all`, `not`.
     - Return `Classification` with `decided_by="rule"`, `latency_ms`, and rule audit payload on match, or `None` on fall-through.
     - Ensure stdlib-only imports in `packages/domain/rules.py` conforming to architectural boundary rules.
   - Verify: `uv run ruff check packages/domain/ && uv run mypy packages/domain/ && uv run pytest tests/unit/test_dependency_rules.py`

3. **Step 3: Hot-Reloadable Engine & YAML Loader (`services/triage_worker/rules.py`, `services/triage_worker/__init__.py`)**
   - Files: `services/triage_worker/rules.py` (new), `services/triage_worker/__init__.py` (modify)
   - Change:
     - Implement `HotReloadableRuleEngine` wrapping `RuleEngine`.
     - Add YAML file loader supporting `config/triage_rules.yaml`.
     - Implement `mtime`-based auto-reloading on access with manual `reload()` override.
     - Implement fail-safe error isolation: invalid YAML or malformed regex logs error and retains the previous valid ruleset without crashing.
   - Verify: `uv run ruff check services/triage_worker/ && uv run mypy services/triage_worker/`

4. **Step 4: Author Declarative Ruleset & Settings Integration (`config/triage_rules.yaml`, `packages/core/settings.py`, `.env.example`, `docs/configuration.md`)**
   - Files: `config/triage_rules.yaml` (new), `packages/core/settings.py` (modify), `.env.example` (modify), `docs/configuration.md` (modify)
   - Change:
     - Create declarative rules in `config/triage_rules.yaml` covering: `auto-submitted`, `newsletter`, `precedence-bulk`, `no-reply-sender`, `out-of-office`, `delivery-status-notification`, `invoice-reference`, `urgent-billing`, `calendar-invite`, and `receipt-acknowledgement`.
     - Add `rules_path: str = Field(default="config/triage_rules.yaml")` to `TriageSettings`.
     - Document `TRIAGE__RULES_PATH` in `.env.example` and `docs/configuration.md`.
   - Verify: `uv run python -c "from packages.core.settings import AppSettings; s = AppSettings(); assert s.triage.rules_path == 'config/triage_rules.yaml'"`

5. **Step 5: Fixture Email Regression Suite (`tests/fixtures/triage/*.eml`)**
   - Files: `tests/fixtures/triage/` (new directory with 9 fixture `.eml` files)
   - Change:
     - Create realistic RFC 822 MIME fixture emails: `01_auto_submitted.eml`, `02_newsletter.eml`, `03_no_reply.eml`, `04_out_of_office.eml`, `05_delivery_status_notification.eml`, `06_invoice_inquiry.eml`, `07_urgent_billing.eml`, `08_calendar_invite.eml`, `09_actionable_support.eml` (fall-through).
   - Verify: `python3 -c "import os; assert len(os.listdir('tests/fixtures/triage')) == 9"`

6. **Step 6: Comprehensive Unit Tests & Benchmarks (`tests/unit/test_rule_engine.py`)**
   - Files: `tests/unit/test_rule_engine.py` (new)
   - Change:
     - Test unit predicates for each field type and operator (`header.Auto-Submitted`, `header.List-Unsubscribe`, `sender.email`, `subject`, `body`, composite `any`/`all`/`not`).
     - Test hot-reloading: file updates, automatic reload on `mtime` bump, syntax error recovery.
     - Test regression suite executing all 9 fixture emails through both raw and hot-reloadable engines.
     - Assert evaluation latency < 2ms per message (~1ms target).
   - Verify: `uv run pytest tests/unit/test_rule_engine.py -v`

7. **Step 7: Full Verification Gate & Task 2.2 Sign-Off**
   - Files: `specs/tasks.md`
   - Change:
     - Run `make lint && make test` to ensure zero regressions across unit and integration tests.
     - Mark Task 2.2 as completed (`[x]`) in `specs/tasks.md`.
   - Verify: `make lint && make test`

---

### Risks & mitigations
- **Risk:** Malformed rule configuration (bad YAML or invalid regex) causes worker crash during hot-reload.
  - *Mitigation:* `HotReloadableRuleEngine` validates YAML schema and precompiles all regular expressions in a temporary structure before atomically swapping the active rule engine; any error leaves the prior valid engine intact and logs an error alert.
- **Risk:** Architectural boundary violation (importing PyYAML in `packages/domain`).
  - *Mitigation:* Keep `packages/domain/rules.py` 100% pure stdlib (`json`, `re`, `dataclasses`); place YAML parsing in `services/triage_worker/rules.py` where third-party packages are permitted.

### Rollback plan
- Revert added files and modifications using `git checkout -- packages/ services/ config/ tests/ docs/ .env.example`.
