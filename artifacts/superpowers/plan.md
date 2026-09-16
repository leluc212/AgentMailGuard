# Superpowers Plan: Phase 1, Task 1.2 — FakeProviderAdapter

## Goal
Implement `FakeProviderAdapter` as an offline, deterministic, fixture-driven implementation of the `MailProviderAdapter` protocol. Support incremental sync windows with pagination, expired checkpoint detection, configurable failure injection, and integration with the shared contract suite, adhering to requirements R1.7 and R24.5.

## Assumptions
1. `FakeProviderAdapter` runs completely offline with zero external network connectivity or credentials (R24.5).
2. Conforms strictly to the `MailProviderAdapter` protocol defined in Task 1.1 and passes `MailProviderAdapterContractSuite`.
3. Auto-registers under provider key `"fake"` in `packages.adapters.registry`.
4. Existing Phase 0 tests using `tests.stubs.mail_provider.FakeMailProviderAdapter` remain backward-compatible.

## Plan

1. Step 1: Implement FakeProviderAdapter Core & Protocol Conformance (Est: 4 mins)
   - Files: `packages/adapters/fake.py`, `packages/adapters/__init__.py`, `tests/unit/test_fake_adapter.py`
   - Change: Implement `FakeProviderAdapter` with in-memory stores for messages, threads, drafts, and subscriptions. Implement all 7 protocol methods. Register `"fake"` in adapter registry. Inherit `MailProviderAdapterContractSuite` in `tests/unit/test_fake_adapter.py`.
   - Verify: `uv run pytest tests/unit/test_fake_adapter.py -k "TestFakeProviderAdapterContract"`

2. Step 2: Add Fixture Loading, Sync Windows & Pagination (Est: 4 mins)
   - Files: `packages/adapters/fake.py`, `tests/unit/test_fake_adapter.py`, `tests/fixtures/mail/sample_messages.json`
   - Change: Implement fixture ingestion (`load_fixtures_from_json`, `seed_messages`), monotonic `history_id` generation, configurable `batch_size` pagination, and `has_more` calculation across sync windows.
   - Verify: `uv run pytest tests/unit/test_fake_adapter.py -k "test_sync_windows or test_pagination or test_fixtures"`

3. Step 3: Implement Expired Checkpoints & Failure Injection (Est: 4 mins)
   - Files: `packages/adapters/fake.py`, `tests/unit/test_fake_adapter.py`
   - Change: Support checkpoint expiration (`expire_checkpoint`, `min_valid_history_id`) returning `requires_full_resync=True`. Add fault injection methods (`inject_rate_limit`, `inject_auth_expired`, `inject_transient`, `inject_permanent`, `inject_not_found`) with decremental countdowns and `retry_after` hints.
   - Verify: `uv run pytest tests/unit/test_fake_adapter.py -k "test_expired_checkpoint or test_failure_injection"`

4. Step 4: Ensure Stub Compatibility, Linting & Regression Suite (Est: 3 mins)
   - Files: `tests/stubs/mail_provider.py`, `tests/unit/test_ci_credential_guard.py`
   - Change: Align `FakeMailProviderAdapter` with `FakeProviderAdapter` to ensure existing Phase 0 tests pass without regression. Run linters, type checks, and full unit test suite.
   - Verify: `uv run ruff check packages/adapters && uv run mypy packages/adapters && uv run pytest tests/unit`

## Risks & mitigations
- **Risk:** Breaking existing tests in `test_ci_credential_guard.py` that rely on `FakeMailProviderAdapter`.
  - **Mitigation:** Retain `tests/stubs/mail_provider.py` methods (`add_message`, `connect`, `sent_count`, `draft_count`) or delegate them to `FakeProviderAdapter`.
- **Risk:** Non-deterministic ordering in multi-page sync queries.
  - **Mitigation:** Sort stored messages strictly by `(received_at, provider_message_id)` before evaluating window offsets.

## Rollback plan
- Revert changes via `git checkout -- packages/adapters/ tests/` and remove `packages/adapters/fake.py`.
