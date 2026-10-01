"""Unit tests verifying external stubbing harness and credential isolation (R24.5).

Proves that:
1. No test requires live provider credentials or external network connectivity.
2. FakeMailProviderAdapter operates completely in-memory with deterministic state.
3. StubLLMProvider delivers structured completions and token accounting without API keys.
4. StubEmbedder produces unit-normalized 1536-dimension vectors.
"""

import math
import os

import pytest

from tests.stubs import FakeMailProviderAdapter, StubEmbedder, StubLLMProvider


class TestFakeMailProviderAdapter:
    """Validate in-memory mail provider adapter double (R24.5)."""

    async def test_connect_disconnect_lifecycle(
        self,
        fake_mail_adapter: FakeMailProviderAdapter,
    ) -> None:
        assert fake_mail_adapter.is_connected is False
        await fake_mail_adapter.connect()
        assert fake_mail_adapter.is_connected is True
        await fake_mail_adapter.disconnect()
        assert fake_mail_adapter.is_connected is False

    async def test_add_and_sync_messages(self, fake_mail_adapter: FakeMailProviderAdapter) -> None:
        msg1 = fake_mail_adapter.add_message(
            sender="alice@example.com",
            recipients=["support@company.com"],
            subject="Invoice issue",
            body_text="Need invoice INV-2026-001",
        )
        msg2 = fake_mail_adapter.add_message(
            sender="bob@example.com",
            recipients=["sales@company.com"],
            subject="Product inquiry",
            body_text="Pricing for enterprise tier",
        )

        synced: list[str] = []
        async for m in fake_mail_adapter.sync_messages(checkpoint=None):
            synced.append(m.provider_message_id)

        assert len(synced) == 2
        assert msg1.provider_message_id in synced
        assert msg2.provider_message_id in synced

        # Fetch raw MIME
        raw = await fake_mail_adapter.fetch_raw_mime(msg1.provider_message_id)
        assert b"From: alice@example.com" in raw

    async def test_send_and_create_draft(self, fake_mail_adapter: FakeMailProviderAdapter) -> None:
        out_id = await fake_mail_adapter.send_message(
            to=["customer@example.com"],
            subject="Re: Inquiry",
            body_text="Here is the information.",
        )
        assert out_id.startswith("out-")
        assert fake_mail_adapter.sent_count == 1

        draft_id = await fake_mail_adapter.create_draft(
            to=["customer@example.com"],
            subject="Re: Draft response",
            body_text="Draft body content.",
        )
        assert draft_id.startswith("draft-")
        assert fake_mail_adapter.draft_count == 1


class TestStubLLMProvider:
    """Validate deterministic LLM test double (R24.5)."""

    async def test_deterministic_generation(self, stub_llm: StubLLMProvider) -> None:
        prompt = "Classify this email: Urgent billing issue"
        res = await stub_llm.generate(prompt)

        assert res.model == "stub-gpt-4o"
        assert res.prompt_tokens > 0
        assert res.completion_tokens > 0
        assert stub_llm.call_count == 1
        assert "acknowledged" in res.content

    async def test_canned_structured_response(self, stub_llm: StubLLMProvider) -> None:
        canned = {
            "category": "billing",
            "priority": "high",
            "reply_required": True,
            "workflow_hint": "ai_generate",
            "confidence": 0.98,
        }
        res = await stub_llm.generate_structured(
            prompt="Classify",
            default_payload=canned,
        )
        assert res.parsed_json == canned
        assert res.parsed_json["category"] == "billing"
        assert res.parsed_json["confidence"] == 0.98

    async def test_failure_injection(self, stub_llm: StubLLMProvider) -> None:
        stub_llm.fail_on_next_call = True
        with pytest.raises(RuntimeError, match="Injected LLM provider"):
            await stub_llm.generate("Hello")

        # Subsequent call succeeds
        res = await stub_llm.generate("Hello again")
        assert res.model == "stub-gpt-4o"


class TestStubEmbedder:
    """Validate deterministic 1536-dimension embedding generator (R24.5, R5.10)."""

    def test_dimension_and_determinism(self, stub_embedder: StubEmbedder) -> None:
        text = "Enterprise customer subscription agreement"
        vec1 = stub_embedder.embed_text(text)
        vec2 = stub_embedder.embed_text(text)

        assert len(vec1) == 1536
        assert vec1 == vec2  # Exact determinism

    def test_unit_normalization(self, stub_embedder: StubEmbedder) -> None:
        vec = stub_embedder.embed_text("Sample knowledge base article")
        l2_norm = math.sqrt(sum(x * x for x in vec))
        assert abs(l2_norm - 1.0) < 1e-6  # Unit length for cosine similarity

    def test_semantic_contrast(self, stub_embedder: StubEmbedder) -> None:
        vec_a = stub_embedder.embed_text("Billing payment refund request")
        vec_b = stub_embedder.embed_text("HR benefits dental health insurance")

        sim = stub_embedder.cosine_similarity(vec_a, vec_b)
        assert sim < 0.99  # Distinct vectors for distinct inputs
        assert stub_embedder.cosine_similarity(vec_a, vec_a) > 0.999999


class TestCredentialGuard:
    """Assert zero live credentials are required or present in test environments (R24.5)."""

    def test_no_live_provider_keys_in_environment(self) -> None:
        blocked_keys = [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "COHERE_API_KEY",
            "GMAIL_CLIENT_SECRET",
            "MS_GRAPH_CLIENT_SECRET",
        ]
        for key in blocked_keys:
            assert key not in os.environ, f"Live secret '{key}' leaked into test process!"
