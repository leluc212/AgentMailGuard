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
]


@pytest.fixture(autouse=True)
def guard_live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no live provider API keys leak into test executions."""
    for var in PROVIDER_SECRET_ENV_VARS:
        if var in os.environ:
            monkeypatch.delenv(var, raising=False)


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
