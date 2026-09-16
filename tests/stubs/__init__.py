"""Test doubles and stubs for hermetic, credential-free testing (R24.5)."""

from tests.stubs.embedding import StubEmbedder
from tests.stubs.llm import LLMGenerationResult, StubLLMProvider
from tests.stubs.mail_provider import FakeMailProviderAdapter, FakeStoredMessage

__all__ = [
    "FakeMailProviderAdapter",
    "FakeStoredMessage",
    "LLMGenerationResult",
    "StubEmbedder",
    "StubLLMProvider",
]
