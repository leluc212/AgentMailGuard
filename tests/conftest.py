"""Global pytest configuration, fixtures, and credential guards.

Enforces R24.5: zero live credentials required in CI; all external provider,
LLM, and embedding interactions must use test doubles.
"""

import os

import pytest

from tests.stubs import FakeMailProviderAdapter, StubEmbedder, StubLLMProvider

PROVIDER_SECRET_ENV_VARS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "COHERE_API_KEY",
    "GMAIL_CLIENT_SECRET",
    "MS_GRAPH_CLIENT_SECRET",
    # Short-lived mailbox tokens for the owner-run live gate (6.10); the registry falls back to
    # them for any mailbox without credentials_ref (packages/adapters/registry.py).
    "GMAIL_ACCESS_TOKEN",
    "GRAPH_ACCESS_TOKEN",
]


@pytest.fixture(autouse=True)
def guard_live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no live provider API keys leak into test executions (R24.5).

    Process env outranks the .env file in pydantic-settings, so pinning LLM__PROVIDER=fake
    and EMBEDDING__MOCK=true here keeps every AppSettings()/AIWorkerSettings() built by a
    test on the fake LLM provider and the mock embedder, even after the owner sets
    LLM__PROVIDER=openai or EMBEDDING__MOCK=false in the host .env for live runs.
    """
    for var in PROVIDER_SECRET_ENV_VARS:
        if var in os.environ:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM__PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING__MOCK", "true")


@pytest.fixture
def fake_mail_adapter() -> FakeMailProviderAdapter:
    """Provide a clean FakeMailProviderAdapter instance."""
    return FakeMailProviderAdapter(mailbox_id="mbx-fixture-test")


@pytest.fixture
def stub_llm() -> StubLLMProvider:
    """Provide a clean StubLLMProvider instance."""
    return StubLLMProvider()


@pytest.fixture
def stub_embedder() -> StubEmbedder:
    """Provide a clean StubEmbedder instance producing 1536-dimension vectors."""
    return StubEmbedder(dimension=1536)
