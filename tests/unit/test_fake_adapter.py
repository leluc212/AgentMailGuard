"""Unit and contract tests for FakeProviderAdapter.

Requirements:
- R1.7: FakeProviderAdapter driven by fixture files, usable in CI with no network access.
- R24.5: Offline, credential-free provider double.
"""

import pytest

from packages.adapters.fake import FakeProviderAdapter
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter
from packages.adapters.testing import MailProviderAdapterContractSuite


class TestFakeProviderAdapterContract(MailProviderAdapterContractSuite):
    """Verify FakeProviderAdapter satisfies all 8 shared adapter contract tests."""

    def create_adapter(self) -> MailProviderAdapter:
        adapter = FakeProviderAdapter()
        # Seed default test data required by contract assertions
        mailbox = self.make_test_mailbox()
        adapter.seed_message(
            provider_message_id="msg-001",
            provider_thread_id="th-001",
            raw_payload=b"From: test@example.com\r\nSubject: Test\r\n\r\nHello",
        )
        return adapter


def test_fake_adapter_registered() -> None:
    """Verify FakeProviderAdapter is auto-registered under 'fake'."""
    adapter = get_adapter("fake")
    assert isinstance(adapter, FakeProviderAdapter)
