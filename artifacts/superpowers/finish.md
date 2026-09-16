# Superpowers Finish Summary: Phase 1, Task 1.1 — Provider Adapter Interface & Registry

## Verification
- **Commands run:**
  - `uv run pytest tests/unit/test_domain_entities.py` (10 passed)
  - `uv run pytest tests/unit/test_adapter_exceptions.py` (5 passed)
  - `uv run pytest tests/unit/test_adapter_registry.py` (5 passed)
  - `uv run pytest tests/unit/test_mail_adapter_contract.py` (10 passed)
  - `uv run pytest tests/unit/test_dependency_rules.py` (4 passed)
  - `uv run ruff check packages/adapters packages/domain` (All passed)
  - `uv run mypy packages/adapters packages/domain` (Success: no issues found in 8 source files)
  - `uv run pytest tests/unit` (138 passed in 3.50s)
- **Results:** 100% green across all unit tests, linters, and type checkers.

## Summary of Changes
- **Domain Layer (`packages/domain`):**
  - Added pure dataclass entities: `Mailbox`, `Checkpoint`, `Subscription`, `RawMessage`, `RawThread`, `OutboundReply`, `DraftRef`, `SentRef`, `ThreadRef`, and `SyncResult`.
  - Re-exported all new domain types in `packages.domain.__all__`.
- **Adapter Exceptions (`packages/adapters/exceptions.py`):**
  - Implemented common error taxonomy: `ProviderError`, `RateLimited` (with `retry_after: float | None`), `AuthExpired`, `NotFound`, `Transient`, and `Permanent`.
- **Adapter Protocol & Registry (`packages/adapters/`):**
  - Defined `@runtime_checkable` `MailProviderAdapter(Protocol)` exposing all 7 required async methods (`subscribe`, `renew_subscription`, `synchronize`, `get_message`, `get_thread`, `create_draft`, `send_reply`).
  - Implemented provider registry in `packages/adapters/registry.py` keyed by `mailbox.provider` with helper functions (`register_adapter`, `get_adapter`, `get_adapter_for_mailbox`, `is_provider_registered`, `clear_registry`).
- **Shared Contract Test Suite (`packages/adapters/testing.py`):**
  - Implemented `MailProviderAdapterContractSuite` abstract test suite verifying all 7 methods and return types for any provider adapter.
- **Architectural Boundary Guard (`tests/unit/test_dependency_rules.py`):**
  - Added `test_services_never_reference_provider_literals` to guarantee provider names never appear in `services/*`.
- **Task Tracking (`specs/tasks.md`):**
  - Marked Task 1.1 as `[x]`.

## Review Pass
- **Blockers:** None.
- **Majors:** None.
- **Minors:** None.
- **Nits:** None.

## Follow-ups
- Proceed to **Phase 1, Task 1.2: FakeProviderAdapter** ([R1.7, R24.5]) to build the fixture-driven offline adapter using `MailProviderAdapterContractSuite`.

## How to Validate Manually
- Run `uv run pytest tests/unit/test_mail_adapter_contract.py -v` to observe contract validation against the test double.
- Run `uv run pytest tests/unit/test_dependency_rules.py -v` to confirm architectural isolation.
